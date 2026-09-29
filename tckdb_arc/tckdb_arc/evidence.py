"""Strict reader for ARC's parser evidence and legacy TCKDB evidence sidecars."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, Mapping


logger = logging.getLogger("tckdb_arc")

SUPPORTED_OUTPUT_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
EVIDENCE_SCHEMA_NAME = "arc-tckdb-evidence"
SUPPORTED_EVIDENCE_SCHEMA_VERSIONS = frozenset({"1.0"})
EVIDENCE_FILENAME = "tckdb_evidence.json"
_EVIDENCE_CONTRACTS = {
    "parser_evidence": ("arc-parser-evidence", "parser_evidence.json"),
    "tckdb_evidence": (EVIDENCE_SCHEMA_NAME, EVIDENCE_FILENAME),
}
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024

_DOCUMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REASONS = frozenset({"missing_source", "unsupported_source", "parse_failed", "empty_result", "hessian_frame_unavailable"})
_KINDS = frozenset({"freq_hessian", "irc", "gsm"})


@dataclass(frozen=True)
class EvidenceIssue:
    key: str
    message: str


@dataclass(frozen=True)
class EvidenceLookup:
    state: str
    value: Mapping[str, Any] | None = None
    reason: str | None = None
    issue: EvidenceIssue | None = None

    @classmethod
    def available(cls, value: Mapping[str, Any]) -> "EvidenceLookup":
        return cls("available", value=value)

    @classmethod
    def unavailable(cls, reason: str) -> "EvidenceLookup":
        return cls("unavailable", reason=reason)

    @classmethod
    def fallback(cls, key: str, message: str) -> "EvidenceLookup":
        return cls("fallback", issue=EvidenceIssue(key, message))


def validate_output_schema(output_doc: Mapping[str, Any]) -> str:
    """Return the supported output schema version or raise clearly."""
    if not isinstance(output_doc, Mapping):
        raise ValueError("ARC output document must be a mapping")
    version = output_doc.get("schema_version")
    if version not in SUPPORTED_OUTPUT_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported ARC output schema_version {version!r}; "
            f"supported versions are {sorted(SUPPORTED_OUTPUT_SCHEMA_VERSIONS)}"
        )
    return str(version)


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], where: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        raise ValueError(f"{where} keys invalid (unknown={sorted(unknown)}, missing={sorted(missing)})")


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    return float(value)


def _integer(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{where} must be an integer >= {minimum}")
    return value


def _xyz(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be canonical XYZ text")
    lines = value.rstrip("\n").splitlines()
    if len(lines) < 3:
        raise ValueError(f"{where} is missing XYZ header/body")
    try:
        count = int(lines[0].strip())
    except ValueError as exc:
        raise ValueError(f"{where} atom count is invalid") from exc
    if count <= 0 or len(lines) != count + 2:
        raise ValueError(f"{where} atom count/body mismatch")
    symbols = []
    for line in lines[2:]:
        parts = line.split()
        if len(parts) != 4:
            raise ValueError(f"{where} atom row is invalid")
        symbols.append(parts[0])
        for token in parts[1:]:
            _number(float(token), where)
    return tuple(symbols)


def _paths(value: Any, where: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} must be a list of paths")


def _validate_hessian(value: Mapping[str, Any]) -> None:
    required = {
        "source_log", "geometry_xyz_text", "atom_count", "matrix_dimension", "packing",
        "units", "source", "parser_version", "lower_triangle",
    }
    _exact_keys(value, required | {"frame"}, required, "freq_hessian.value")
    if "frame" in value:
        expected_frame = {"parsed_log": "gaussian_input_orientation", "parsed_hess": "orca_hess_atoms"}
        if value["frame"] != expected_frame.get(value.get("source")):
            raise ValueError("unsupported or mismatched Hessian frame")
    symbols = _xyz(value["geometry_xyz_text"], "freq_hessian.geometry_xyz_text")
    atom_count = _integer(value["atom_count"], "freq_hessian.atom_count", minimum=1)
    dimension = _integer(value["matrix_dimension"], "freq_hessian.matrix_dimension", minimum=1)
    if atom_count != len(symbols) or dimension != 3 * atom_count:
        raise ValueError("freq_hessian atom_count/matrix_dimension mismatch")
    if value["packing"] != "lower_triangle_row_major_including_diagonal":
        raise ValueError("unsupported Hessian packing")
    if value["units"] != "hartree_per_bohr_squared":
        raise ValueError("unsupported Hessian units")
    if value["source"] not in {"parsed_log", "parsed_hess"} or value["parser_version"] != "arc-hessian-1":
        raise ValueError("unsupported Hessian source/parser")
    triangle = value["lower_triangle"]
    if not isinstance(triangle, list) or len(triangle) != dimension * (dimension + 1) // 2:
        raise ValueError("Hessian triangle length mismatch")
    for item in triangle:
        _number(item, "freq_hessian.lower_triangle")


def _validate_irc(value: Mapping[str, Any]) -> None:
    allowed = {"parser_version", "trajectories", "omitted_source_paths"}
    _exact_keys(value, allowed, {"parser_version", "trajectories"}, "irc.value")
    if value["parser_version"] != "arc-irc-path-1":
        raise ValueError("unsupported IRC parser version")
    if "omitted_source_paths" in value:
        _paths(value["omitted_source_paths"], "irc.omitted_source_paths")
    trajectories = value["trajectories"]
    if not isinstance(trajectories, list) or not trajectories:
        raise ValueError("IRC trajectories must be non-empty")
    seen = set()
    for ti, trajectory in enumerate(trajectories):
        if not isinstance(trajectory, Mapping):
            raise ValueError("IRC trajectory must be a mapping")
        keys = {"source_log", "declared_direction", "points"}
        _exact_keys(trajectory, keys, keys, "irc.trajectory")
        direction = trajectory["declared_direction"]
        if direction not in {None, "forward", "reverse"}:
            raise ValueError("invalid IRC declared_direction")
        points = trajectory["points"]
        if not isinstance(points, list) or not points:
            raise ValueError("IRC points must be non-empty")
        for point in points:
            allowed_point = {
                "source_point_index", "direction", "geometry_xyz_text", "electronic_energy_hartree",
                "reaction_coordinate_sqrt_amu_bohr", "max_gradient_hartree_per_bohr",
                "rms_gradient_hartree_per_bohr",
            }
            if not isinstance(point, Mapping):
                raise ValueError("IRC point must be a mapping")
            _exact_keys(point, allowed_point, {"source_point_index", "direction", "geometry_xyz_text"}, "irc.point")
            index = _integer(point["source_point_index"], "irc.source_point_index")
            marker = (ti, index)
            if marker in seen:
                raise ValueError("duplicate IRC source point index")
            seen.add(marker)
            if point["direction"] not in {None, "forward", "reverse"}:
                raise ValueError("invalid IRC direction")
            _xyz(point["geometry_xyz_text"], "irc.geometry_xyz_text")
            for key in allowed_point - {"source_point_index", "direction", "geometry_xyz_text"}:
                if key in point:
                    _number(point[key], f"irc.{key}")


def _validate_gsm(value: Mapping[str, Any]) -> None:
    required = {"source_stringfile", "parser_version", "method", "selected_source_point_index", "points"}
    _exact_keys(value, required | {"ograd_invocations"}, required, "gsm.value")
    invocations = {}
    invocation_records = value.get("ograd_invocations", [])
    if not isinstance(invocation_records, list):
        raise ValueError("GSM invocations must be a list")
    for invocation in invocation_records:
        if not isinstance(invocation, Mapping):
            raise ValueError("GSM invocation must be a mapping")
        allowed_invocation = {"invocation_id", "electronic_energy_hartree", "max_gradient_hartree_per_bohr", "rms_gradient_hartree_per_bohr"}
        _exact_keys(invocation, allowed_invocation, {"invocation_id", "electronic_energy_hartree"}, "gsm.invocation")
        identifier = invocation["invocation_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", identifier) or identifier in invocations:
            raise ValueError("invalid or duplicate GSM invocation id")
        for key in allowed_invocation - {"invocation_id"}:
            if key in invocation:
                _number(invocation[key], f"gsm.invocation.{key}")
        invocations[identifier] = invocation
    if value["parser_version"] != "arc-gsm-stringfile-1" or value["method"] != "gsm":
        raise ValueError("unsupported GSM parser/method")
    selected = _integer(value["selected_source_point_index"], "gsm.selected_source_point_index")
    points = value["points"]
    if not isinstance(points, list) or not points:
        raise ValueError("GSM points must be non-empty")
    indices = set()
    symbols = None
    coordinates = []
    attached_ids = set()
    for point in points:
        allowed = {
            "source_point_index", "node_label", "geometry_xyz_text", "path_coordinate_angstrom",
            "electronic_energy_hartree", "stringfile_relative_energy_kcal_mol",
            "max_gradient_hartree_per_bohr", "rms_gradient_hartree_per_bohr",
            "cumulative_com_superposed_displacement_angstrom",
            "geometry_matched_ograd_invocation_id", "geometry_match_displacement_angstrom",
        }
        required_point = {"source_point_index", "geometry_xyz_text"}
        if not isinstance(point, Mapping):
            raise ValueError("GSM point must be a mapping")
        _exact_keys(point, allowed, required_point, "gsm.point")
        index = _integer(point["source_point_index"], "gsm.source_point_index")
        if index in indices:
            raise ValueError("duplicate GSM source point index")
        indices.add(index)
        label = point.get("node_label")
        if label is not None:
            _integer(label, "gsm.node_label")
        point_symbols = _xyz(point["geometry_xyz_text"], "gsm.geometry_xyz_text")
        if symbols is None:
            symbols = point_symbols
        elif symbols != point_symbols:
            raise ValueError("GSM atom ordering is inconsistent")
        coordinate_key = "cumulative_com_superposed_displacement_angstrom" if "cumulative_com_superposed_displacement_angstrom" in point else "path_coordinate_angstrom"
        if coordinate_key in point:
            coordinates.append(_number(point[coordinate_key], f"gsm.{coordinate_key}"))
        identifier = point.get("geometry_matched_ograd_invocation_id")
        if "ograd_invocations" in value and identifier is None and any(
            key in point for key in (
                "electronic_energy_hartree", "max_gradient_hartree_per_bohr", "rms_gradient_hartree_per_bohr"
            )
        ):
            raise ValueError("GSM invocation values require a geometry match")
        if identifier is not None or "geometry_match_displacement_angstrom" in point:
            if not isinstance(identifier, str) or identifier not in invocations or identifier in attached_ids:
                raise ValueError("invalid or reused GSM geometry-matched invocation")
            displacement = _number(point.get("geometry_match_displacement_angstrom"), "gsm.geometry_match_displacement_angstrom")
            if not 0 <= displacement <= 1e-3:
                raise ValueError("GSM geometry match exceeds producer tolerance")
            attached_ids.add(identifier)
            for key in ("electronic_energy_hartree", "max_gradient_hartree_per_bohr", "rms_gradient_hartree_per_bohr"):
                if key in point and point[key] != invocations[identifier].get(key):
                    raise ValueError("GSM attached value disagrees with invocation")
        for key in allowed - required_point - {"node_label", "geometry_matched_ograd_invocation_id"}:
            if key in point:
                _number(point[key], f"gsm.{key}")
    if selected not in indices:
        raise ValueError("selected GSM index is absent")
    if coordinates and len(coordinates) != len(points):
        raise ValueError("GSM path coordinates must be all present or all absent")
    if coordinates and (coordinates[0] != 0.0 or any(b < a for a, b in zip(coordinates, coordinates[1:]))):
        raise ValueError("GSM path coordinates must start at zero and be monotonic")


_VALUE_VALIDATORS = {"freq_hessian": _validate_hessian, "irc": _validate_irc, "gsm": _validate_gsm}


class EvidenceStore:
    """Lazy, adapter-lifetime cache for one output/evidence generation pair."""

    def __init__(self, project_directory: str | Path | None):
        self._project_directory = Path(project_directory) if project_directory is not None else None
        self._loaded_for: int | None = None
        self._records: dict[tuple[str, str], Mapping[str, Any]] = {}
        self._record_issues: dict[tuple[str, str], EvidenceIssue] = {}
        self._document_issue: EvidenceIssue | None = None
        self._warned: set[str] = set()

    def _warn_once(self, issue: EvidenceIssue) -> None:
        if issue.key not in self._warned:
            logger.warning("ARC evidence ignored (%s); using parser fallback", issue.message)
            self._warned.add(issue.key)

    def _failure(self, key: str, message: str) -> None:
        self._document_issue = EvidenceIssue(key, message)
        self._warn_once(self._document_issue)

    def for_output_doc(self, output_doc: Mapping[str, Any]) -> "EvidenceStore":
        identity = id(output_doc)
        if self._loaded_for == identity:
            return self
        self._loaded_for = identity
        self._records = {}
        self._record_issues = {}
        self._document_issue = None
        try:
            version = validate_output_schema(output_doc)
            # The current producer uses a parser-neutral name. Prefer its
            # descriptor when present; never revive a stale legacy sidecar if
            # the current descriptor is malformed or its generation mismatches.
            descriptor_key = "parser_evidence" if "parser_evidence" in output_doc else "tckdb_evidence"
            schema_name, filename = _EVIDENCE_CONTRACTS[descriptor_key]
            descriptor = output_doc.get(descriptor_key)
            if version == "1.0" or descriptor is None:
                return self
            if not isinstance(descriptor, Mapping):
                raise ValueError("evidence descriptor must be a mapping")
            descriptor_keys = {"path", "schema_name", "schema_version", "document_id"}
            _exact_keys(descriptor, descriptor_keys, descriptor_keys, "evidence descriptor")
            if descriptor["path"] != filename:
                raise ValueError(f"evidence path must be {filename}")
            path = Path(str(descriptor["path"]))
            if path.is_absolute() or len(path.parts) != 1 or path.name != filename:
                raise ValueError("unsafe evidence path")
            if self._project_directory is None:
                raise ValueError("project_directory is required for evidence")
            output_dir = (self._project_directory / "output").resolve()
            evidence_path = output_dir / path.name
            if evidence_path.is_symlink() and not evidence_path.resolve().is_relative_to(output_dir):
                raise ValueError("evidence symlink resolves outside output directory")
            with evidence_path.open("rb") as handle:
                size = evidence_path.stat().st_size
                if size > MAX_EVIDENCE_BYTES:
                    raise ValueError("evidence file exceeds 256 MiB")
                raw = handle.read(MAX_EVIDENCE_BYTES + 1)
            if len(raw) > MAX_EVIDENCE_BYTES:
                raise ValueError("evidence file exceeds 256 MiB")
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
            if not isinstance(document, Mapping):
                raise ValueError("evidence root must be a mapping")
            top_keys = {"schema_name", "schema_version", "document_id", "output_schema_version", "producer", "records"}
            _exact_keys(document, top_keys, top_keys, "evidence document")
            if document["schema_name"] != schema_name or descriptor["schema_name"] != schema_name:
                raise ValueError("evidence schema name mismatch")
            evidence_version = document["schema_version"]
            if evidence_version not in SUPPORTED_EVIDENCE_SCHEMA_VERSIONS or descriptor["schema_version"] != evidence_version:
                raise ValueError("unsupported or mismatched evidence schema version")
            document_id = document["document_id"]
            if not isinstance(document_id, str) or not _DOCUMENT_ID_RE.fullmatch(document_id):
                raise ValueError("invalid document_id")
            if descriptor["document_id"] != document_id:
                raise ValueError("document_id mismatch")
            if document["output_schema_version"] != version:
                raise ValueError("output schema version mismatch")
            producer = document["producer"]
            if not isinstance(producer, Mapping):
                raise ValueError("producer must be a mapping")
            _exact_keys(
                producer,
                {"name", "version", "git_commit", "arkane_version", "arkane_git_commit"},
                {"name", "version", "git_commit"},
                "producer",
            )
            if producer["name"] != "ARC" or not isinstance(producer["version"], str) or not (
                producer["git_commit"] is None or isinstance(producer["git_commit"], str)
            ):
                raise ValueError("producer metadata is invalid")
            for key in ("arkane_version", "arkane_git_commit"):
                if producer.get(key) is not None and not isinstance(producer[key], str):
                    raise ValueError(f"producer {key} must be a string or null")
            output_keys = {
                (kind, record.get("label"))
                for section, kind in (("species", "species"), ("transition_states", "transition_state"))
                for record in (output_doc.get(section) or []) if isinstance(record, Mapping)
            }
            records = document["records"]
            if not isinstance(records, list):
                raise ValueError("records must be a list")
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    self._warn_once(EvidenceIssue(f"record:{index}", "evidence record must be a mapping"))
                    continue
                kind, label = record.get("record_kind"), record.get("label")
                if kind not in {"species", "transition_state"} or not isinstance(label, str):
                    self._warn_once(EvidenceIssue(f"record:{index}", "invalid evidence record identity"))
                    continue
                key = (kind, label)
                if key in self._records or key in self._record_issues:
                    self._records.pop(key, None)
                    self._record_issues[key] = EvidenceIssue(
                        f"record:{kind}:{label}", "duplicate evidence record identity",
                    )
                    continue
                try:
                    allowed = {"record_kind", "label", *_KINDS}
                    _exact_keys(record, allowed, {"record_kind", "label"}, "evidence record")
                    if key not in output_keys:
                        raise ValueError("evidence record is absent from output.yml")
                    self._records[key] = record
                except Exception as exc:
                    self._record_issues[key] = EvidenceIssue(f"record:{kind}:{label}", str(exc))
            logger.info("Accepted ARC evidence schema %s with %d records", evidence_version, len(self._records))
        except Exception as exc:  # evidence is optional; fallback is the compatibility contract
            self._records = {}
            self._record_issues = {}
            self._failure("document", str(exc))
        return self

    def lookup(self, output_doc: Mapping[str, Any], record_kind: str, label: str, evidence_kind: str) -> EvidenceLookup:
        self.for_output_doc(output_doc)
        if evidence_kind not in _KINDS:
            raise ValueError(f"unknown evidence kind {evidence_kind!r}")
        if self._document_issue is not None:
            return EvidenceLookup("fallback", issue=self._document_issue)
        identity = (record_kind, label)
        record_issue = self._record_issues.get(identity)
        if record_issue is not None:
            self._warn_once(record_issue)
            return EvidenceLookup("fallback", issue=record_issue)
        record = self._records.get(identity)
        if record is None or evidence_kind not in record:
            return EvidenceLookup.fallback(f"missing:{record_kind}:{label}:{evidence_kind}", "evidence entry is absent")
        envelope = record[evidence_kind]
        try:
            if not isinstance(envelope, Mapping):
                raise ValueError("evidence envelope must be a mapping")
            status = envelope.get("status")
            if status == "unavailable":
                _exact_keys(envelope, {"status", "reason", "source_paths"}, {"status", "reason"}, "unavailable envelope")
                if envelope["reason"] not in _REASONS:
                    raise ValueError("invalid unavailable reason")
                if "source_paths" in envelope:
                    _paths(envelope["source_paths"], "source_paths")
                return EvidenceLookup.unavailable(str(envelope["reason"]))
            if status != "available":
                raise ValueError("invalid evidence status")
            _exact_keys(envelope, {"status", "value"}, {"status", "value"}, "available envelope")
            value = envelope["value"]
            if not isinstance(value, Mapping):
                raise ValueError("available evidence value must be a mapping")
            _VALUE_VALIDATORS[evidence_kind](value)
            return EvidenceLookup.available(value)
        except Exception as exc:
            issue = EvidenceIssue(f"entry:{record_kind}:{label}:{evidence_kind}", str(exc))
            self._warn_once(issue)
            return EvidenceLookup("fallback", issue=issue)
