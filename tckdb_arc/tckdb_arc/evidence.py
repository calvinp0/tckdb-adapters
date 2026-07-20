"""Strict reader for ARC's versioned ``tckdb_evidence.json`` sidecar."""

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
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024

_DOCUMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REASONS = frozenset({"missing_source", "unsupported_source", "parse_failed", "empty_result"})
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
    _exact_keys(value, required, required, "freq_hessian.value")
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
    _exact_keys(value, required, required, "gsm.value")
    if value["parser_version"] != "arc-gsm-stringfile-1" or value["method"] != "gsm":
        raise ValueError("unsupported GSM parser/method")
    selected = _integer(value["selected_source_point_index"], "gsm.selected_source_point_index")
    points = value["points"]
    if not isinstance(points, list) or not points:
        raise ValueError("GSM points must be non-empty")
    indices = set()
    symbols = None
    coordinates = []
    for point in points:
        allowed = {
            "source_point_index", "node_label", "geometry_xyz_text", "path_coordinate_angstrom",
            "electronic_energy_hartree", "stringfile_relative_energy_kcal_mol",
            "max_gradient_hartree_per_bohr", "rms_gradient_hartree_per_bohr",
        }
        required_point = {"source_point_index", "node_label", "geometry_xyz_text"}
        if not isinstance(point, Mapping):
            raise ValueError("GSM point must be a mapping")
        _exact_keys(point, allowed, required_point, "gsm.point")
        index = _integer(point["source_point_index"], "gsm.source_point_index")
        if index in indices:
            raise ValueError("duplicate GSM source point index")
        indices.add(index)
        label = point["node_label"]
        if label is not None:
            _integer(label, "gsm.node_label")
        point_symbols = _xyz(point["geometry_xyz_text"], "gsm.geometry_xyz_text")
        if symbols is None:
            symbols = point_symbols
        elif symbols != point_symbols:
            raise ValueError("GSM atom ordering is inconsistent")
        if "path_coordinate_angstrom" in point:
            coordinates.append(_number(point["path_coordinate_angstrom"], "gsm.path_coordinate_angstrom"))
        for key in allowed - required_point - {"path_coordinate_angstrom"}:
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
        self._document_issue = None
        try:
            version = validate_output_schema(output_doc)
            descriptor = output_doc.get("tckdb_evidence")
            if version == "1.0" or descriptor is None:
                return self
            if not isinstance(descriptor, Mapping):
                raise ValueError("evidence descriptor must be a mapping")
            descriptor_keys = {"path", "schema_name", "schema_version", "document_id"}
            _exact_keys(descriptor, descriptor_keys, descriptor_keys, "evidence descriptor")
            if descriptor["path"] != EVIDENCE_FILENAME:
                raise ValueError("evidence path must be tckdb_evidence.json")
            path = Path(str(descriptor["path"]))
            if path.is_absolute() or len(path.parts) != 1 or path.name != EVIDENCE_FILENAME:
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
            if document["schema_name"] != EVIDENCE_SCHEMA_NAME or descriptor["schema_name"] != EVIDENCE_SCHEMA_NAME:
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
            _exact_keys(producer, {"name", "version", "git_commit"}, {"name", "version", "git_commit"}, "producer")
            if producer["name"] != "ARC" or not isinstance(producer["version"], str) or not (
                producer["git_commit"] is None or isinstance(producer["git_commit"], str)
            ):
                raise ValueError("producer metadata is invalid")
            output_keys = {
                (kind, record.get("label"))
                for section, kind in (("species", "species"), ("transition_states", "transition_state"))
                for record in (output_doc.get(section) or []) if isinstance(record, Mapping)
            }
            records = document["records"]
            if not isinstance(records, list):
                raise ValueError("records must be a list")
            for record in records:
                if not isinstance(record, Mapping):
                    raise ValueError("evidence record must be a mapping")
                allowed = {"record_kind", "label", *_KINDS}
                _exact_keys(record, allowed, {"record_kind", "label"}, "evidence record")
                key = (record["record_kind"], record["label"])
                if record["record_kind"] not in {"species", "transition_state"} or not isinstance(record["label"], str):
                    raise ValueError("invalid evidence record identity")
                if key in self._records:
                    raise ValueError("duplicate evidence record identity")
                if key not in output_keys:
                    raise ValueError("evidence record is absent from output.yml")
                self._records[key] = record
            logger.info("Accepted ARC evidence schema %s with %d records", evidence_version, len(self._records))
        except Exception as exc:  # evidence is optional; fallback is the compatibility contract
            self._records = {}
            self._failure("document", str(exc))
        return self

    def lookup(self, output_doc: Mapping[str, Any], record_kind: str, label: str, evidence_kind: str) -> EvidenceLookup:
        self.for_output_doc(output_doc)
        if evidence_kind not in _KINDS:
            raise ValueError(f"unknown evidence kind {evidence_kind!r}")
        if self._document_issue is not None:
            return EvidenceLookup("fallback", issue=self._document_issue)
        record = self._records.get((record_kind, label))
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
