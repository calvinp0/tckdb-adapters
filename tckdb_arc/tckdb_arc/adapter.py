"""Adapter that turns an ARC ``output.yml`` record into a TCKDB conformer upload.

The adapter is the only ARC module that knows the shape of a TCKDB
upload. It builds a JSON payload matching ``ConformerUploadRequest``,
writes it to disk, and (optionally) hands it to ``tckdb-client``.

Source of truth for upload data is ``<project>/output/output.yml`` —
``arc/output.py`` was designed for this consumer (see its module
docstring). The adapter therefore takes two dicts: the full output
document (for top-level levels-of-theory, ARC version, etc.) and one
species record from ``output_doc['species']`` or
``output_doc['transition_states']``. This keeps the adapter decoupled
from ARC's live object model and makes a separate replay path (read
output.yml later, post payloads, no ARC needed) trivial.

Three guarantees:

1. If the adapter is disabled or no config is provided, it is a no-op.
2. The payload is on disk *before* any network call. Replay tooling
   only needs ``payload_file + endpoint + idempotency_key``.
3. By default, an upload failure is logged + recorded in the sidecar
   but does not raise. ``strict=True`` flips that.
"""

import base64
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tckdb_client import TCKDBClient
from tckdb_client.errors import TCKDBError
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.fragments.refs import (
    correction_table_method_stem,
    W_SOFTWARE_RELEASE_VERSION_IS_COMPOSITE,
    SoftwareReleaseRef,
)
from tckdb_schemas.utils import normalize_tunneling_model

from tckdb_arc import arc13
from tckdb_arc._logging import LOGGER_NAME as _LOGGER_NAME
from tckdb_arc._logging import get_logger
from tckdb_arc.adaptive import (
    RUN_LEVEL,
    UNDETERMINABLE,
    AdaptiveLevels,
    RestartInfo,
    detect_adaptive_levels,
    read_restart_info,
)
from tckdb_arc.warning_codes import ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE, ArcWarning
from tckdb_arc.config import (
    IMPLEMENTED_ARTIFACT_KINDS,
    TCKDBConfig,
    UPLOAD_MODE_COMPUTED_REACTION,
    UPLOAD_MODE_COMPUTED_SPECIES,
)

from tckdb_arc.idempotency import (
    ArtifactIdempotencyInputs,
    IdempotencyInputs,
    build_artifact_idempotency_key,
    build_idempotency_key,
)
from tckdb_arc.evidence import EvidenceStore
from tckdb_core.constraints import serialize_constraints
from tckdb_core.level_rules import method_identity_key
from tckdb_core.payload_writer import (
    ArtifactSidecarMetadata,
    PayloadWriter,
    SidecarMetadata,
    WrittenArtifact,
    WrittenPayload,
    _utcnow_iso,
)
# The producer-agnostic upload pipeline, outcome types, endpoint constants and
# key helpers moved to ``tckdb_core`` (batch L1). Every name that lived here is
# re-exported so ``tckdb_arc.adapter.<name>`` keeps resolving for callers and
# tests; the leading-underscore names are part of that surface.
from tckdb_core import composition as _composition
from tckdb_core import isotopes as _isotopes
from tckdb_core import reaction_flatten as _reaction_flatten
from tckdb_core import rmg_units as _rmg_units
from tckdb_core import rules as _rules
from tckdb_core import software_release as _software_release
from tckdb_core import thermo_numerics as _thermo_numerics
from tckdb_core import ts_evidence as _ts_evidence
from tckdb_core.adapter_warnings import AdapterWarning, WarningSink
from tckdb_core.constants import (  # noqa: F401
    ARTIFACTS_ENDPOINT_TEMPLATE,
    COMPUTED_REACTION_ENDPOINT,
    COMPUTED_REACTION_KIND,
    COMPUTED_SPECIES_ENDPOINT,
    COMPUTED_SPECIES_KIND,
    CONFORMER_UPLOAD_ENDPOINT,
    PAYLOAD_KIND,
    PREFLIGHT_BASE_DELAY_SECONDS,
    PREFLIGHT_MAX_ATTEMPTS,
    PREFLIGHT_MAX_DELAY_SECONDS,
    TRANSITION_STATE_ENDPOINT,
    TRANSITION_STATE_KIND,
    VALID_TCKDB_ORIGIN_KINDS,
    _OUTPUT_LOG_ALLOWED_EXTS,
)
from tckdb_core.keys import (  # noqa: F401
    _KEY_PART_RE,
    _calc_prefix_for_actor,
    _local_key_for_actor,
    _safe_key_part,
)
from tckdb_core.outcomes import (  # noqa: F401
    ArtifactUploadOutcome,
    TCKDBReadinessError,
    UploadOutcome,
    _ArtifactBatchResult,
    _PreparedArtifactUpload,
)
from tckdb_core.uploader import (  # noqa: F401
    _BUNDLE_MODES_WITH_INLINE_ARTIFACTS,
    _PUBLIC_REF_ECHO_KEYS,
    _W_CALCULATION_REF_NOT_RETURNED,
    TCKDBUploaderBase,
    _append_request_id,
    _artifact_batch_bodies,
    _artifact_batch_digest,
    _attach_preflight,
    _build_readiness_error,
    _calculation_ref_not_returned_warning as _core_calculation_ref_not_returned_warning,
    _close_quietly,
    _coerce_artifact_filename,
    _extract_calc_refs,
    _extract_submission_refs,
    _extract_tckdb_public_refs,
    _format_readiness_message,
    _headers_from,
    _preflight_sleep,
    _readyz_body_is_ready,
    _request_id_from,
    _server_warnings,
    _skip,
    _summarize_artifact_batch_results,
    _summarize_response_body,
    _artifact_batch_idempotency_prefix as _core_artifact_batch_idempotency_prefix,
)


def _serialize_calc_constraints(source) -> list[dict]:
    """Translate a parser-shaped constraint list into the TCKDB payload shape.

    ``source`` is whatever ``arc/output.py`` attached to the record (a
    list of parser dicts) or what the caller passed explicitly. Empty /
    None / unrecognised input produces ``[]``. Wraps
    ``tckdb_core.constraints.serialize_constraints`` so per-calc parse
    failures never bubble up into payload generation.
    """
    if not source:
        return []
    if not isinstance(source, (list, tuple)):
        logger.warning("TCKDB constraints: expected list, got %s; emitting [].",
                       type(source).__name__)
        return []
    try:
        return serialize_constraints(source, log=logger)
    except Exception as exc:  # defensive — serializer logs internally too
        logger.warning("TCKDB constraints: serialization failed: %s; emitting [].",
                       exc)
        return []


def _scan_entries_from_record(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return legacy TCKDB-shaped scan entries, translating neutral ARC scans.

    ARC output 1.1 records parser-native ``rotor_scans``. Older consolidated
    outputs carried ``additional_calculations`` already shaped like the TCKDB
    calculation model; retain that path for replay compatibility.
    """
    legacy = record.get("additional_calculations")
    if isinstance(legacy, list):
        return [entry for entry in legacy if isinstance(entry, Mapping)]
    scans = record.get("rotor_scans")
    if not isinstance(scans, list):
        return []
    translated: list[Mapping[str, Any]] = []
    for scan in scans:
        if not isinstance(scan, Mapping):
            continue
        key = scan.get("key")
        result = scan.get("result")
        if not isinstance(key, str) or not key or not isinstance(result, Mapping):
            continue
        scan_result = _neutral_scan_result_to_tckdb(result, record)
        if scan_result is None:
            continue
        translated.append({
            "key": key,
            "type": _CALC_KEY_SCAN,
            "scan_result": scan_result,
            "constraints": scan.get("constraints") or [],
            # ARC's ``rotor_scans[].source_log``: the scan's own ESS log, the
            # output_log artifact of this scan calculation.
            "source_log": scan.get("source_log"),
            # Output.yml 1.3: the program and banner of the scan's own log.
            "ess_software": scan.get("ess_software"),
            "ess_version": scan.get("ess_version"),
        })
    return translated


def _neutral_scan_result_to_tckdb(
    result: Mapping[str, Any], record: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    coordinate = result.get("coordinate")
    samples = result.get("samples")
    if not isinstance(coordinate, Mapping) or not isinstance(samples, list) or not samples:
        return None
    atoms = coordinate.get("atom_indices")
    if not isinstance(atoms, list) or not atoms:
        return None
    # index_base defaults to 1 when the key is absent (matches ARC's own
    # rotor-scan writer, which always emits index_base=1 for the only
    # coordinate kind it produces — a dihedral scan; ARC's schema pins
    # it to `const: 1`). Unlike the constraints[] route (C-1), a missing
    # key here is safe on every reachable path. But an out-of-range
    # *explicit* value must not be applied blindly (PHASE_C_PLAN.md C-2)
    # — guard it the same way constraints.py does, rather than only
    # bounding via the len<=4 check below.
    try:
        index_base = int(coordinate.get("index_base", 1))
    except (TypeError, ValueError):
        return None
    if index_base not in (0, 1):
        logger.warning(
            "TCKDB scan coordinate: unsupported index_base=%r; omitting "
            "scan result.", index_base,
        )
        return None
    try:
        one_based = [int(atom) - index_base + 1 for atom in atoms]
    except (TypeError, ValueError):
        return None
    kind = {
        "cartesian": "cartesian_atom",
        "distance": "bond",
        "angle": "angle",
        "dihedral": "dihedral",
    }.get(str(coordinate.get("coordinate_type") or ""))
    if kind is None or len(one_based) > 4:
        return None
    coord_out: dict[str, Any] = {
        "coordinate_index": 1,
        "coordinate_kind": kind,
        "step_count": len(samples),
        "value_unit": str(coordinate.get("unit") or "degree"),
    }
    for position, atom in enumerate(one_based, start=1):
        coord_out[f"atom{position}_index"] = atom
    optional_map = {
        "symmetry_number": "symmetry_number",
        "requested_step_size": "step_size",
        "requested_start": "start_value",
        "requested_end": "end_value",
    }
    for source_key, target_key in optional_map.items():
        if coordinate.get(source_key) is not None:
            coord_out[target_key] = coordinate[source_key]
    # ``resolution_degrees`` is a degree-specific echo of the generic
    # ``step_size`` above (TCKDB's CoordinateUnit is only {angstrom,
    # degree} — a bond/cartesian scan's step is in Angstrom, not
    # degrees). Only stamp it when the coordinate's own unit says
    # degree; otherwise a distance-kind scan's Angstrom step would land
    # in a field named degrees (PHASE_C_PLAN.md C-5). Unreachable from
    # real ARC output today — ARC's rotor-scan writer only ever emits
    # dihedral/degree scans, and its schema pins both as consts — but
    # this function also serves hand-written/third-party output.yml,
    # which evidence.py does not validate against that schema.
    if (
        coordinate.get("requested_step_size") is not None
        and str(coordinate.get("unit") or "").strip().lower() == "degree"
    ):
        coord_out["resolution_degrees"] = coordinate["requested_step_size"]

    points: list[dict[str, Any]] = []
    for point_index, sample in enumerate(samples, start=1):
        if not isinstance(sample, Mapping) or sample.get("angle_degrees") is None:
            return None
        point: dict[str, Any] = {
            "point_index": point_index,
            "coordinate_values": [{
                "coordinate_index": 1,
                "coordinate_value": float(sample["angle_degrees"]),
                "value_unit": str(coordinate.get("unit") or "degree"),
            }],
        }
        for key in ("electronic_energy_hartree", "relative_energy_kj_mol"):
            if sample.get(key) is not None:
                point[key] = float(sample[key])
        geometry = _normalize_xyz_text(sample.get("geometry_xyz"), f"scan_point_{point_index}")
        if geometry is not None:
            # The sample's own stated isotopes (1.3 ``geometry_isotopes``); for a
            # substituted species a sample that states none is left out.
            payload = (
                _geometry_payload(record, geometry, sample.get("geometry_isotopes"))
                if record is not None else {"xyz_text": geometry})
            if payload is not None:
                point["geometry"] = payload
        points.append(point)
    out: dict[str, Any] = {
        "dimension": int(result.get("dimension", 1)),
        "is_relaxed": bool(result.get("relaxed", True)),
        "coordinates": [coord_out],
        "points": points,
    }
    if result.get("zero_energy_reference_hartree") is not None:
        out["zero_energy_reference_hartree"] = float(result["zero_energy_reference_hartree"])
    return out


_ENERGY_UNITS = frozenset({"hartree", "kj_mol", "kcal_mol"})
# ``AtomParamApplication`` (tckdb-schemas 0.62).
_ATOM_PARAM_APPLICATIONS = frozenset({"subtracted", "added"})


def _atom_params_from_reference_atom_energies(
    table: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """``(atom_params, unit)`` from ARC's ``reference_atom_energies`` block.

    The block is ``{unit, applied_as, values: {element: energy}}``. Returns
    ``([], None)`` (so the scheme is sent without parameters, which the
    contract allows) when the unit is missing or not a TCKDB energy unit
    (never guessed: ``scheme.units`` decides how TCKDB reads every value), or
    when any element or value is unusable (a partial table would misstate
    the scheme).
    """
    if not isinstance(table, Mapping):
        return [], None
    unit = table.get("unit")
    values = table.get("values")
    if unit not in _ENERGY_UNITS or not isinstance(values, Mapping) or not values:
        return [], None
    params: list[dict[str, Any]] = []
    for key, value in sorted(values.items(), key=lambda kv: str(kv[0])):
        element = str(key).strip()
        if (
            not 1 <= len(element) <= 3
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return [], None
        params.append({"element": element, "value": float(value)})
    if len({p["element"] for p in params}) != len(params):
        return [], None
    return params, str(unit)


def _is_output_schema_1_0(output_doc: Mapping[str, Any]) -> bool:
    """Whether the document is ARC output schema 1.0 (keyed on the version alone).

    1.0 wrote thermo Cp points as ``thermo.cp_data`` and the atom-energy table
    as ``parameter_table``; 1.1 renamed them ``thermo_points`` and
    ``reference_atom_energies``. The old names are read only for 1.0 documents.
    """
    return output_doc.get("schema_version") == "1.0"


def _is_output_schema_1_3_or_later(output_doc: Mapping[str, Any]) -> bool:
    """Whether the document is ARC output schema 1.3 or later (keyed on the version alone).

    1.3 states what the Arkane run behind each number actually did: a Petersson
    correction's ``components`` are the bonds it applied (``skipped_components``
    the bonds it could not), a statmech block carries ``arkane_treatment`` and the
    E0 switches, a TS record its frequencies in ESS order and the reaction-
    coordinate index, a reaction its ``reversible`` and the kinetics run's
    ``comment`` / ``atom_corrections_applied`` / ``ts_validation``, and the header
    the ``rmg_database`` identity. A document of an earlier version never states
    these, so they are read from 1.3 documents only (BRIDGE_ROADMAP A19).
    """
    version = output_doc.get("schema_version") if isinstance(output_doc, Mapping) else None
    if not isinstance(version, str):
        return False
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (1, 3)


def _skipped_bonds_note(skipped: Any) -> str | None:
    """The correction note naming the bonds a Petersson table had no parameter for.

    ``skipped`` is ARC's ``skipped_components`` (``[{bond, count}]``); ``None``
    when nothing was skipped or the list is not usable.
    """
    if not isinstance(skipped, list):
        return None
    parts = []
    for entry in skipped:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("bond"), str):
            return None
        count = entry.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return None
        parts.append(f"{entry['bond']} x{count}")
    if not parts:
        return None
    return (
        "Arkane's Petersson table has no parameter for " + ", ".join(parts)
        + "; those bonds contributed nothing to this total, and the components "
        "listed are the bonds it did apply."
    )


def _correction_records_from_record(
    record: Mapping[str, Any], *, legacy_1_0: bool = False, schema_1_3: bool = False,
) -> list[dict[str, Any]]:
    """Translate neutral ARC correction facts to the legacy adapter boundary.

    ``legacy_1_0`` (an output.yml 1.0 document) also reads an atom-energy
    ``parameter_table``, the 1.0 name of what 1.1 calls
    ``reference_atom_energies``. ``schema_1_3`` (an output.yml 1.3 document)
    reads a Petersson record's ``skipped_components`` and states the skipped
    bonds in the correction's note; the record's ``components`` are then the
    bonds applied, summing to ``total``."""
    legacy = record.get("applied_energy_corrections")
    if isinstance(legacy, list):
        return [dict(entry) for entry in legacy if isinstance(entry, Mapping)]
    neutral = record.get("energy_corrections")
    if not isinstance(neutral, list):
        return []
    out: list[dict[str, Any]] = []
    for correction in neutral:
        if not isinstance(correction, Mapping):
            continue
        correction_type = correction.get("correction_type")
        model = correction.get("model")
        total = correction.get("total")
        if not schema_1_3:
            # Before 1.3 ARC kept the record of a correction its thermo run did not
            # apply (1.3 drops it); the thermo block states which, so do not
            # deposit as applied what the same record says was switched off.
            thermo = record.get("thermo")
            switch = None
            if isinstance(thermo, Mapping):
                switch = thermo.get(
                    "atom_corrections_applied" if correction_type == "atom_energy"
                    else "bond_corrections_applied")
            if switch is False:
                continue
        if not isinstance(total, Mapping) or total.get("value") is None:
            continue
        if correction_type == "atom_energy":
            application_role = "aec_total"
            scheme_kind = "atom_energy"
        elif correction_type == "bond_additivity" and model in ("petersson", "melius"):
            application_role = "bac_total"
            scheme_kind = "bac_petersson" if model == "petersson" else "bac_melius"
        else:
            continue
        # Unit is read, never guessed (PHASE_C_PLAN.md C-6). ARC's
        # producer (arc/scripts/get_species_corrections.py) always
        # hardcodes 'hartree' for atom-energy and 'kcal_mol' for both
        # bond-additivity models, so real ARC output never reaches the
        # missing-unit branch below — but ARC's own docs have stated the
        # wrong unit for this table before, and a hand-written/
        # third-party output.yml isn't schema-validated (evidence.py
        # only checks schema_version). ``value_unit`` is a *required*
        # field on TCKDB's AppliedEnergyCorrectionUploadPayload, so
        # there is no valid way to emit this record without one: omit
        # the whole correction rather than assert a unit ARC didn't
        # report.
        raw_unit = total.get("unit")
        if not raw_unit:
            logger.warning(
                "TCKDB energy correction: %s correction has no reported "
                "unit; omitting rather than guessing hartree/kcal_mol.",
                correction_type,
            )
            continue
        unit = str(raw_unit)
        scheme: dict[str, Any] = {
            "kind": scheme_kind,
            "name": scheme_kind,
            "level_of_theory": correction.get("level_of_theory"),
            "units": unit,
        }
        if correction_type == "atom_energy":
            # ARC writes Arkane's per-element reference atomic energies as
            # ``reference_atom_energies`` (never ``parameter_table``, which only
            # the Petersson record carries). TCKDB's ``scheme.atom_params[]``
            # for an ``atom_energy`` scheme is the element-keyed atomic-energy
            # table (backend ``EnergyCorrectionSchemeAtomParam``: "atom_hf,
            # atom_thermal, SOC, atom_energies"), which is what these are: the
            # bare atomic electronic energies Arkane subtracts. They are the
            # scheme's parameters, not per-atom corrections; the applied total
            # and its components are sent separately and never rebuilt from them.
            reference = correction.get("reference_atom_energies")
            if not isinstance(reference, Mapping):
                reference = {}
            atom_params, params_unit = _atom_params_from_reference_atom_energies(
                reference
            )
            legacy_table = correction.get("parameter_table") if legacy_1_0 else None
            if (
                not atom_params
                and isinstance(legacy_table, Mapping)
                and isinstance(legacy_table.get("values"), Mapping)
            ):
                # output.yml 1.0: the table carried no unit of its own; the
                # correction's unit is what the scheme's values are in.
                scheme["atom_params"] = [
                    {"element": str(key), "value": float(value)}
                    for key, value in sorted(legacy_table["values"].items())
                ]
            if atom_params:
                scheme["atom_params"] = atom_params
                applied_as = reference.get("applied_as")
                if applied_as in _ATOM_PARAM_APPLICATIONS:
                    # tckdb-schemas 0.62 ``EnergyCorrectionSchemeRef.atom_params_applied_as``:
                    # "How ``atom_params`` enter the corrected energy: ``subtracted`` or
                    # ``added``. Applies to every entry of ``atom_params`` and to nothing
                    # else... not inferred when omitted"; it requires ``atom_params``.
                    # ARC records it with the table, and Arkane's atom_energy tables are
                    # ``subtracted`` ("count * value is removed from the energy"), so it is
                    # sent exactly as ARC states it, only beside the atom_params it covers.
                    scheme["atom_params_applied_as"] = applied_as
                if applied_as == "subtracted":
                    # The atom_params are bare atomic energies. Arkane's full per-atom
                    # term (RMG-Py arkane/encorr/corr.py, get_atom_correction, steps 1-2)
                    # is -E_atom + (atom_hf - atom_thermal): the atom's gas-phase
                    # formation enthalpy (less its thermal correction, kcal/mol in
                    # RMG-database data.py) is added as well, and that addend is no
                    # atom_param, so ``atom_params_applied_as`` (which covers atom_params
                    # alone) does not state it. The subtraction itself is structured now,
                    # so the note keeps only the part it cannot carry.
                    scheme["note"] = (
                        "Arkane also adds each atom's gas-phase formation enthalpy less "
                        "its thermal correction (RMG-database atom_hf - atom_thermal); "
                        "those addends are not atom_params."
                    )
                # ``scheme.units`` is the unit the scheme's parameter values
                # are expressed in; the applied total keeps its own value_unit.
                scheme["units"] = params_unit
        else:
            table = correction.get("parameter_table")
            if (
                model == "petersson"
                and isinstance(table, Mapping)
                and isinstance(table.get("values"), Mapping)
            ):
                scheme["bond_params"] = [
                    {"bond_key": str(key), "value": float(value)}
                    for key, value in sorted(table["values"].items())
                ]
        converted: dict[str, Any] = {
            "application_role": application_role,
            "value": float(total["value"]),
            "value_unit": unit,
            "scheme": scheme,
            "components": correction.get("components") or [],
            # Arkane's database key the table came from; read by
            # ``_build_applied_energy_corrections`` for ``scheme.software``,
            # never sent itself.
            "matched_arkane_key": correction.get("matched_arkane_key"),
        }
        if schema_1_3 and scheme_kind == "bac_petersson":
            # Output 1.3: ``components`` are the bonds the BAC applied (they
            # sum to the total) and ``skipped_components`` the bonds without a
            # parameter. The skipped bonds belong to this species' total, not
            # to the scheme (a table's identity does not depend on the species
            # it was applied to), so they go in the correction's note.
            converted["components_are_applied_bonds"] = True
            skipped_note = _skipped_bonds_note(correction.get("skipped_components"))
            if skipped_note is not None:
                converted["note"] = skipped_note
        out.append(converted)
    return out


logger = get_logger()

#: Source tag of this adapter's sidecar warnings: ``context.source == "tckdb_arc_self_check"``.
_PRODUCER_TAG = "tckdb_arc"


def _self_check(
    code: str, message: str, field: str | None = None, context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The sidecar dict of one self-check finding, rendered through ``AdapterWarning``.

    ``context`` is the finding's own details (``action``, ids, ...); ``source`` is added
    first. Appending the result to a plain list or to a ``WarningSink`` stores the same
    ``{code, message, field, context}`` dict the sidecar has always held.
    """
    return AdapterWarning(code, message, field, context or {}).to_dict(_PRODUCER_TAG)


# ---------------------------------------------------------------------------
# Names that moved to ``tckdb_core`` in batch L2 (the producer-agnostic numerics, rules
# and tables). Each keeps resolving here, and the functions that log do so through this
# module's ``logger`` so ``mock.patch("tckdb_arc.adapter.logger")`` still intercepts them.
# ---------------------------------------------------------------------------

_ARC_TO_TCKDB_A_UNITS = _rmg_units.RMG_TO_TCKDB_A_UNITS
_ARC_TO_TCKDB_EA_UNITS = _rmg_units.RMG_TO_TCKDB_EA_UNITS
_normalize_unit_key = _rmg_units.normalize_unit_key


def arc_to_tckdb_a_units(arc_units: str | None) -> str | None:
    """Map an ARC ``A_units`` string to a TCKDB ``ArrheniusAUnits`` enum value.

    ``tckdb_core.rmg_units.a_units_to_tckdb`` with this module's logger: ``None`` for
    null/empty or unrecognised units (a debug line says which).
    """
    return _rmg_units.a_units_to_tckdb(arc_units, log=logger)


def arc_to_tckdb_ea_units(arc_units: str | None) -> str | None:
    """Map an ARC ``Ea_units`` (or ``dEa_units``) string to a TCKDB ``ActivationEnergyUnits`` value.

    ``tckdb_core.rmg_units.ea_units_to_tckdb`` with this module's logger.
    """
    return _rmg_units.ea_units_to_tckdb(arc_units, log=logger)


_GAS_CONSTANT_J_MOL_K = _thermo_numerics.GAS_CONSTANT_J_MOL_K
_T298_K = _thermo_numerics.T298_K
_nasa_h298_kj_mol = _thermo_numerics.nasa_h298_kj_mol


def _build_nasa_block(nasa_low: Any, nasa_high: Any) -> dict[str, Any] | None:
    """ARC's two NASA blocks as ``ThermoNASACreate`` (``tckdb_core.thermo_numerics``), or ``None``."""
    return _thermo_numerics.build_nasa_block(nasa_low, nasa_high, log=logger)


def _build_thermo_points(thermo_points: Any) -> list[dict[str, Any]]:
    """ARC's ``thermo_points`` as ``ThermoPointCreate`` dicts (``tckdb_core.thermo_numerics``)."""
    return _thermo_numerics.build_thermo_points(thermo_points, log=logger)


_REACTION_FLAT_RESULT_FIELDS = _reaction_flatten.REACTION_FLAT_RESULT_FIELDS
_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE = _reaction_flatten.REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE
_REACTION_RESULT_FIELDS_NOT_CARRIED = _reaction_flatten.REACTION_RESULT_FIELDS_NOT_CARRIED
_REACTION_MODE_FIELDS_HANDLED = _reaction_flatten.REACTION_MODE_FIELDS_HANDLED
_REACTION_MODE_FIELDS_NOT_CARRIED = _reaction_flatten.REACTION_MODE_FIELDS_NOT_CARRIED
_flatten_result_fields = _reaction_flatten.flatten_result_fields
_flatten_all_reaction_calcs = _reaction_flatten.flatten_all_reaction_calcs

_xyz_element_symbols = _composition.xyz_element_symbols
_FORMULA_TERM_RE = _composition.FORMULA_TERM_RE
_formula_element_symbols = _composition.formula_element_symbols
_is_single_atom_geometry = _composition.is_single_atom_geometry
_formula = _composition.formula

_XTB_BANNER_RE = _software_release.XTB_BANNER_RE
_split_ess_version_banner = _software_release.split_version_banner

_calculations_by_key = _rules.calculations_by_key
_describe_level = _rules.describe_level
_finite_float = _ts_evidence.finite_float


def _energy_level_declaration(
    energy_level: Mapping[str, Any] | None,
    *,
    calc_keys_by_role: Mapping[str, str],
    calculations: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """Return ``(role, level)`` to declare as ``energy_level_of_theory``, or ``None``.

    The rule is TCKDB's (``assert_role_consistency``) and lives in
    :func:`tckdb_core.rules.energy_level_declaration`; reading ARC's stated energy level
    (``energy_level``, see ``_thermo_energy_level``) into the ``LevelOfTheoryRef`` shape
    is the ARC part. Nothing is declared when ARC's energy level is not stated (adaptive
    runs it cannot attribute).
    """
    if not isinstance(energy_level, Mapping):
        return None
    return _rules.energy_level_declaration(
        _arc_level_to_tckdb_lot(energy_level),
        energy_level_description=_describe_level(energy_level),
        calc_keys_by_role=calc_keys_by_role,
        calculations=calculations,
        producer_name="ARC",
        role_order=(_CALC_KEY_SP, _CALC_KEY_OPT),
        log=logger,
    )


# Bundle-only calculation keys that ``_build_ts_block`` layers onto each
# calc dict for the computed-reaction wire shape (local cross-reference
# identity + dependency edges). The standalone TS endpoint consumes a
# plain ``CalculationWithResultsPayload`` (``extra="forbid"``) for
# primary_opt / additional_calculations, so these must be stripped before
# upload. Dependency wiring (e.g. path_search -> primary_opt) is
# re-derived server-side for the standalone endpoint.
_TS_STANDALONE_STRIP_CALC_KEYS = ("key", "depends_on", "geometry_key", "artifacts")


# Local calculation-key namespace within a computed-species bundle.
# These keys are referenced from `depends_on.parent_calculation_key` and
# `thermo.source_calculations[].calculation_key`. They have no relation
# to TCKDB-assigned calculation_ids — the bundle endpoint mints those
# server-side and returns them in the response.
_CALC_KEY_OPT = "opt"
_CALC_KEY_OPT_COARSE = "opt_coarse"
_CALC_KEY_FREQ = "freq"
_CALC_KEY_SP = "sp"

# ``ThermoCalculationRole`` / ``StatmechCalculationRole`` ``composite``: the
# calculation is a composite-method job (CBS-QB3, G4, ...). TCKDB has no
# composite *calculation type*; the role "describes a scientific origin rather
# than a specific job type" and accepts any type, so a composite run's job (filed
# as the record's ``opt``, see ``_with_composite_role``) is linked under it.
_ROLE_COMPOSITE = "composite"

# Version tag stamped onto every parsed Hessian payload. Bump when the
# Cartesian-Hessian parsing in ``arc/parser/adapters/{gaussian,orca}.py``
# changes in a way that would alter the emitted lower-triangle values.
_HESSIAN_PARSER_VERSION = "arc-hessian-1"

# ESS name (from ``arc.parser.parser.determine_ess``) → TCKDB
# ``HessianSource`` enum value. Gaussian prints the Hessian inside the log
# (``parsed_log``); Orca writes it to a sibling ``.hess`` file
# (``parsed_hess``). Other ESS engines have no Cartesian-Hessian parser
# wired up, so they are absent and the Hessian is simply skipped.
_HESSIAN_SOURCE_BY_ESS = {
    "gaussian": "parsed_log",
    "orca": "parsed_hess",
}

# Per-calc record-field map for output_log artifacts. Same convention as
# `_LOG_FIELD_BY_CALC_KEY` in the existing artifact path: the species
# record carries `<job>_log` paths that come straight from
# `arc/output.py::_spc_to_dict`.
_LOG_FIELD_BY_CALC_KEY = {
    _CALC_KEY_OPT: "opt_log",
    _CALC_KEY_OPT_COARSE: "coarse_opt_log",
    _CALC_KEY_FREQ: "freq_log",
    _CALC_KEY_SP: "sp_log",
    # ts_guess role is method-dispatched at lookup time (see
    # ``_resolve_log_field``) because the producing TS-guess adapter
    # determines which record field carries the log path
    # (``neb_log`` for orca_neb, ``gsm_log`` for xtb_gsm). The gate in
    # ``_build_ts_block`` is what decides whether the calc is emitted
    # at all.
}

# Same shape, for input-deck paths. `arc/output.py` emits these per-job
# (with per-job software → per-job filename), only when the deck file
# actually exists on disk; null when the deck wasn't kept (archived runs).
# (No coarse_opt_input field today — coarse jobs share the engine's
# input filename with the fine opt; the deck is keyed off the log dir.)
_INPUT_FIELD_BY_CALC_KEY = {
    _CALC_KEY_OPT: "opt_input",
    _CALC_KEY_FREQ: "freq_input",
    _CALC_KEY_SP: "sp_input",
}

# Held-fixed coordinate constraints emitted onto the species record by
# ``arc/output.py::_spc_to_dict`` (one list per ESS-job calc). Scan calcs
# carry their constraints inline on the ``additional_calculations`` entry
# rather than via species_record, so they're not in this map.
_CONSTRAINTS_FIELD_BY_CALC_KEY = {
    _CALC_KEY_OPT: "opt_constraints",
    _CALC_KEY_FREQ: "freq_constraints",
    _CALC_KEY_SP: "sp_constraints",
}

# (artifact_kind, record-field-map) pairs that the inline-artifact
# helper iterates per calc. Keeping the mapping data-driven so adding
# checkpoints (or any future kind) is one tuple, not a code branch.
_INLINE_ARTIFACT_SOURCES: tuple[tuple[str, dict[str, str]], ...] = (
    ("output_log", _LOG_FIELD_BY_CALC_KEY),
    ("input", _INPUT_FIELD_BY_CALC_KEY),
)

# TS-side calc role; reaction bundles can also carry an irc on the TS,
# but ARC may not always have one parsed cleanly, so it's optional.
_CALC_KEY_IRC = "irc"

# TS guess parent calculation (only emitted when the chosen guess was
# itself a real path-search calculation — orca_neb or xtb_gsm).
# Geometry-only guesses (heuristics, AutoTST/KinBot wrappers, GCN,
# user-supplied XYZ) stay as ``calculation_input_geometry`` and never
# produce a calc node here, per the calculation_dependency contract:
# parent must be a real calc.
_CALC_KEY_TS_GUESS = "ts_guess"

# ARC's TSGuess.method strings → TCKDB ``path_search_result.method``
# enum value. Lookup is case- and whitespace-insensitive at the call
# site (producer strings are stable identifiers but historically have
# casing variations like ``orca_neb`` vs ``ORCA_NEB`` and module-name
# vs class-name forms ``xtb_gsm`` vs ``xTB-GSM``). Geometry-only
# methods (heuristics, AutoTST, KinBot, GCN, user XYZ) are deliberately
# absent — they have no path-search log to anchor a parent calc.
_TS_GUESS_PATH_SEARCH_METHODS: dict[str, str] = {
    "orca_neb": "neb",
    "xtb_gsm": "gsm",
    "xtb-gsm": "gsm",
}

# Per-method record-field name carrying the path-search log path.
# ``neb_log`` is populated by ``arc/output.py`` from ``paths['neb']``
# (set by the scheduler from ``tsg.log_path`` when orca_neb wins) and
# ``gsm_log`` from ``paths['gsm']`` (xtb_gsm wins). Both fields are
# read by ``_resolve_ts_guess_path_search`` to gate emission of the
# ``ts_guess`` parent calc.
_TS_GUESS_LOG_FIELD_BY_METHOD: dict[str, str] = {
    "neb": "neb_log",
    "gsm": "gsm_log",
}

# Per-method gate for emitting the path-search log as an inline
# ``output_log`` artifact. Gated by what the backend's ESS-signature
# validator accepts: NEB logs are real ORCA output files (pass); GSM
# stringfiles are multi-frame XYZ trajectories (fail signature check —
# backend supports cfour/gaussian/molpro/nwchem/orca/psi4/qchem/turbomole
# only). For GSM, the per-frame geometries already ride inside
# ``path_search_result.points``, so the raw stringfile would be
# redundant provenance even if the backend accepted it.
_TS_GUESS_UPLOAD_LOG_AS_ARTIFACT: dict[str, bool] = {
    "neb": True,
    "gsm": False,
}

# Per-method static properties of the path-search algorithm itself.
# These are intrinsic to the *method*, not the run — both ARC-supported
# adapters (orca_neb, xtb_gsm) are double-ended path-search methods that
# consume two endpoint geometries (reactant + product). Single-ended
# methods (growing-string, freezing-string) would set is_double_ended
# to False and source_endpoint_count to 1; not currently exposed by
# any ARC adapter, so absent here.
_PATH_SEARCH_METHOD_PROPERTIES: dict[str, dict[str, Any]] = {
    "neb": {"is_double_ended": True, "source_endpoint_count": 2},
    "gsm": {"is_double_ended": True, "source_endpoint_count": 2},
}

# Thermochemical kcal → kJ. ``molecularGSM`` stringfile comment-line
# energies are relative energies in kcal/mol (first node = 0.0), so the
# stringfile fallback below converts with this factor. (Node-output
# energies, when preserved, are absolute Hartrees and use ``E_h_kJmol``
# instead — see the primary energy path in
# ``_build_path_search_result_payload``.)
_KCAL_MOL_TO_KJ_MOL = 4.184
# A GSM stringfile whose relative-energy column spans less than this
# (kcal/mol) is treated as the molecularGSM "no energy emitted" sentinel
# (the entire column is ``0.000000`` in ARC's current xtb_gsm build)
# rather than a real, physically-flat profile — so we leave relative
# energies null instead of uploading a fabricated all-zero profile.
_GSM_STRINGFILE_ENERGY_EPS = 1e-6


def _resolve_ts_guess_path_search(method: object) -> str | None:
    """Return the TCKDB ``path_search_result.method`` enum value for an
    ARC TSGuess.method string, or ``None`` for geometry-only / unknown
    methods.

    Conservative: only matches strings explicitly listed in
    ``_TS_GUESS_PATH_SEARCH_METHODS`` (case- and whitespace-insensitive).
    Non-string and unknown values return ``None`` — the caller falls
    back to geometry-only TS provenance (no parent calc emitted).
    """
    if not isinstance(method, str):
        return None
    return _TS_GUESS_PATH_SEARCH_METHODS.get(method.strip().lower())


def _resolve_ts_guess_path_search_for_record(record: Mapping[str, Any]) -> str | None:
    """Return the TCKDB ``path_search_result.method`` enum for a TS record.

    First tries the chosen guess's primary method (``chosen_ts_method``) — unchanged behaviour.
    When that is geometry-only (e.g. ``gcn``) but a path-search method (``xtb_gsm`` / ``orca_neb``)
    was merged into the chosen guess during ARC's equivalent-guess clustering, recover it from the
    chosen ``ts_guesses`` entry's ``method_sources``. This gates emission of the ``path_search``
    parent calc off the merged provenance rather than the single primary method — it does NOT
    change ARC's TS selection (``chosen_ts_method`` is untouched).

    When several path-search methods merged into the chosen guess, prefer the one whose log field
    (``neb_log`` / ``gsm_log``) is actually populated on the record — the scheduler preserves exactly
    one path source's log — so this gate and the subsequent log lookup agree. Returns ``None`` for
    geometry-only / unknown methods.
    """
    method = _resolve_ts_guess_path_search(record.get("chosen_ts_method"))
    if method is not None:
        return method
    candidates: list[str] = []
    for tsg in (record.get("ts_guesses") or []):
        if isinstance(tsg, Mapping) and tsg.get("chosen"):
            for source in (tsg.get("method_sources") or []):
                resolved = _resolve_ts_guess_path_search(source)
                if resolved is not None and resolved not in candidates:
                    candidates.append(resolved)
            break
    for candidate in candidates:
        log_field = _TS_GUESS_LOG_FIELD_BY_METHOD.get(candidate)
        if log_field and record.get(log_field):
            return candidate
    return candidates[0] if candidates else None


def _resolve_log_field(role: str, record: Mapping[str, Any]) -> str | None:
    """Resolve the species-record field carrying the output-log path for ``role``.

    Ordinary roles dispatch through ``_LOG_FIELD_BY_CALC_KEY``. The
    ``ts_guess`` role is method-dispatched: the field name depends on
    which TS-guess adapter produced the chosen guess (orca_neb →
    ``neb_log``, xtb_gsm → ``gsm_log``). Returns ``None`` when the role
    has no log mapping or the TS-guess method is geometry-only.
    """
    if role == _CALC_KEY_TS_GUESS:
        method_enum = _resolve_ts_guess_path_search_for_record(record)
        if method_enum is None:
            return None
        # Some path-search methods (notably GSM) produce log artifacts
        # the backend's ESS-signature validator rejects. Skip the
        # artifact upload for those — the geometric data still rides
        # inside ``path_search_result.points``.
        if not _TS_GUESS_UPLOAD_LOG_AS_ARTIFACT.get(method_enum, False):
            return None
        return _TS_GUESS_LOG_FIELD_BY_METHOD.get(method_enum)
    return _LOG_FIELD_BY_CALC_KEY.get(role)


# Bundle-local calc role for rotor scans. Per-rotor keys are minted as
# ``f"{_CALC_KEY_SCAN}_rotor_{i}"`` by ``arc/output.py``; the adapter
# accepts whatever key the species record provides without re-minting,
# so the source-of-truth for naming stays on the producer side.
_CALC_KEY_SCAN = "scan"


def _warn_primary_opt_placeholder(
    species_record: Mapping[str, Any],
    warnings: list[dict[str, Any]] | None,
    *,
    field: str,
) -> None:
    """Warn that a molecule's primary opt is a placeholder for a job ARC did not run as an opt.

    TCKDB's computed-species and computed-reaction routes require every
    conformer of two or more atoms to carry an ``opt`` primary calculation
    (``ConformerInBundle.validate_primary_is_opt`` and the reaction ``ConformerIn``
    equivalent). Two shapes of 1.3 record have none: a composite run, whose
    geometry came from a job whose internal optimisation level ARC does not
    export, and a record that exports an sp or freq log but no opt job. Each is
    filed as a marked placeholder opt and reported. The calculation model has no
    ``note`` field to carry the statement.

    A single atom is not a placeholder case: since tckdb-schemas 0.59 (TCKDB#610)
    its primary is its real ``sp`` (``_build_monatomic_primary_sp``), so nothing
    is filed or warned for it.
    """
    if _is_single_atom_geometry(species_record.get("xyz")):
        return
    kind = arc13.primary_opt_placeholder(species_record)
    if kind is None:
        return
    code = (_W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED if kind == "composite"
            else _W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB)
    label = species_record.get("label") or "<unlabeled>"
    message = (
        f"The primary opt filed for {label!r} is a placeholder (tckdb_origin "
        f"placeholder_primary_opt_{kind}): "
        + ("its geometry came from a composite-method job, whose internal "
           "optimisation level ARC does not export, so the opt carries the composite level ARC states."
           if kind == "composite" else
           "ARC exports no optimisation job for it, so the opt carries the header opt level; "
           "only ARC's stated convergence is sent, with no step count or energy.")
        + " TCKDB requires a primary opt and has no composite calculation type."
    )
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append(_self_check(
            code, message, field,
            {"action": "placeholder_primary_opt_filed"}))


_W_IRC_ENDPOINT_SPECIES_SKIPPED = ArcWarning.IRC_ENDPOINT_SPECIES_SKIPPED.value


def _irc_endpoint_skip(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any],
) -> "UploadOutcome | None":
    """A ``skipped`` outcome when the species is one of ARC's IRC endpoints.

    Output.yml 1.3 marks them (``irc_endpoint_of``, BRIDGE_ROADMAP B6/A17).
    Older output writes no marker, but ``restart.yml`` records ``irc_label``
    (see ``RestartInfo.irc_endpoint_ts``); without ``restart.yml`` or the
    species' entry there is no answer and the species is treated as an
    ordinary one. These species only ever get an opt
    (ARC skips their freq and sp and computes no thermo), and are not wells.
    """
    label = species_record.get("label")
    if "irc_endpoint_of" in species_record:
        # Output.yml 1.3 marks them itself (``irc_endpoint_of``: the TS label for
        # an endpoint species, ``null`` for every ordinary one). That statement is
        # authoritative both ways, so restart.yml is not consulted.
        ts_label = species_record.get("irc_endpoint_of")
        if not isinstance(ts_label, str) or not ts_label:
            return None
        source = "output.yml irc_endpoint_of"
        direction = species_record.get("irc_endpoint_direction")
        if direction in ("forward", "reverse"):
            source += f", {direction} IRC job"
    else:
        restart = _restart_levels(output_doc)
        if restart is None:
            return None
        ts_labels = {
            str(r.get("label")) for r in (output_doc.get("transition_states") or [])
            if isinstance(r, Mapping) and r.get("label")
        }
        ts_label = restart.irc_endpoint_ts(label, ts_labels)
        if ts_label is None:
            return None
        source = "restart.yml irc_label"
    message = (
        f"{label!r} is an IRC endpoint of {ts_label!r} ({source}), "
        "not a stationary species of the run, so it is not uploaded."
    )
    logger.warning("TCKDB %s: %s", _W_IRC_ENDPOINT_SPECIES_SKIPPED, message)
    return UploadOutcome(
        status="skipped", payload_path=None, sidecar_path=None, idempotency_key="",
        warnings=[_self_check(
            _W_IRC_ENDPOINT_SPECIES_SKIPPED, message, "species",
            {"action": "species_skipped", "ts_label": ts_label})],
    )


class TCKDBAdapter(TCKDBUploaderBase):
    """Build, write, and optionally upload one conformer/calculation payload.

    The adapter holds the config and a payload writer. ``client_factory``
    is overridable so tests can inject a mocked ``TCKDBClient`` without
    touching the network.

    Reading ARC's output and building the payloads is ARC-specific and lives
    here; the upload, sidecar, readiness and artifact-batch pipeline is the
    producer-agnostic :class:`tckdb_core.uploader.TCKDBUploaderBase`.
    """

    PRODUCER_TAG = _PRODUCER_TAG
    LOGGER_NAME = _LOGGER_NAME
    PRODUCER_NAME = "ARC"
    IDEMPOTENCY_NAMESPACE = "arc"

    @property
    def _log(self):
        # Resolved at call time so ``tckdb_arc.adapter.logger`` (patched by tests
        # and by anyone redirecting ARC's TCKDB logging) sees the shared pipeline's
        # records exactly as it saw them before the pipeline moved to tckdb_core.
        return logger

    def _sleep_between_probes(self, seconds: float) -> None:
        # Resolved at call time through this module's global so
        # ``mock.patch("tckdb_arc.adapter._preflight_sleep")`` still skips the wait.
        _preflight_sleep(seconds)

    def _project_label_for(self, output_doc: Mapping[str, Any]) -> Any:
        return output_doc.get("project")

    def __init__(
        self,
        config: TCKDBConfig,
        *,
        project_directory: str | Path | None = None,
        client_factory=None,
        input_dict: Mapping[str, Any] | None = None,
    ):
        # ARC's parsed ``input.yml`` when the caller has it (the CLI), used with
        # the project's ``restart.yml`` / ``input.yml`` to detect
        # ``adaptive_levels`` runs (see ``tckdb_arc.adaptive``).
        self._input_dict = input_dict
        self._adaptive_levels_checked = False
        self._adaptive_levels: AdaptiveLevels | None = None
        self._restart_info: RestartInfo | None = None
        # Payloads land under the active ARC project when ``payload_dir`` is relative.
        super().__init__(
            config, project_directory=project_directory, client_factory=client_factory,
        )
        self._evidence = EvidenceStore(self._project_directory)
        # Emit the "artifacts unsupported for standalone TS uploads"
        # warning at most once per adapter instance rather than once per
        # TS (or per calc) — a sweep of many TSs would otherwise spam it.
        self._warned_ts_artifacts_unsupported = False

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def _with_adaptive_levels(
        self,
        output_doc: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        """Attach what the project's ``restart.yml`` / ``input.yml`` say about levels.

        Returns ``output_doc`` unchanged when neither file says anything;
        otherwise a shallow copy carrying ``_RESTART_KEY`` (``restart.yml``'s
        adaptive ranges, IRC level and conformer level, see
        ``tckdb_arc.adaptive``) and, for an ``adaptive_levels`` run,
        ``_ADAPTIVE_LEVELS_KEY``. ``_resolve_level`` then attributes each
        adaptively-chosen level exactly where ``restart.yml`` allows and
        otherwise states nothing (``_report_adaptive_omissions`` lists what
        was left out after the build).
        """
        if not self._adaptive_levels_checked:
            self._adaptive_levels = detect_adaptive_levels(
                self._project_directory, self._input_dict)
            self._restart_info = read_restart_info(self._project_directory)
            self._adaptive_levels_checked = True
        detection, restart = self._adaptive_levels, self._restart_info
        contradictions = output_doc.get("schema_version") == "1.3"
        # Schema 1.3 states ``adaptive_levels`` in the header itself: the job types
        # its entries name join those found in the project files, so a job whose
        # level the record does not state is not filled from a run-level header.
        header_types = _header_adaptive_job_types(output_doc)
        if header_types:
            detection = AdaptiveLevels(
                job_types=frozenset(header_types) | (
                    detection.job_types if detection is not None else frozenset()),
                sources=("output.yml adaptive_levels",) + (
                    detection.sources if detection is not None else ()),
            )
        if detection is None and restart is None and not contradictions:
            return output_doc
        marked = dict(output_doc)
        if contradictions:
            marked[_CONTRADICTIONS_KEY] = []
        if restart is not None:
            marked[_RESTART_KEY] = restart
        if detection is not None:
            marked[_ADAPTIVE_LEVELS_KEY] = {
                "named": detection.job_types,
                "sources": detection.sources,
                # kind -> labels whose level could not be attributed
                "omitted": {},
            }
        return marked

    @staticmethod
    def _report_adaptive_omissions(
        output_doc: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None,
    ) -> None:
        """One warning per level kind that ``adaptive_levels`` left unattributed."""
        marker = _adaptive_marker(output_doc)
        if marker is not None:
            _warn_adaptive_omissions(warnings, marker)
        noted = output_doc.get(_CONTRADICTIONS_KEY)
        for label, kind, detail in (noted if isinstance(noted, list) else ()):
            message = (
                f"The {kind} calculation of {label!r} was not uploaded: its recorded level "
                f"(requested) contradicts the keyword line the job ran with ({detail}). "
                "Observed outranks requested, and the adapter never sends both as consistent.")
            logger.warning("TCKDB %s: %s", _W_LEVEL_CONTRADICTED_BY_ROUTE, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_LEVEL_CONTRADICTED_BY_ROUTE, message, f"{kind}.level_of_theory",
                    {"action": f"{kind}_calculation_omitted", "label": label}))

    def submit_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_index: int = 0,
        extra_label: str | None = None,
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one conformer payload.

        ``output_doc`` is the full parsed ``output.yml``. ``species_record``
        is one entry from ``output_doc['species']`` or
        ``output_doc['transition_states']``.

        Returns ``None`` if the adapter is disabled (so callers can write
        ``adapter.submit_from_output(...)`` without an enabled-check).
        """
        if not self._config.enabled:
            return None

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        species_record = _withhold_unreliable_composite_energies(output_doc, species_record, build_warnings)
        skipped = _irc_endpoint_skip(output_doc, species_record)
        if skipped is not None:
            return skipped
        payload = self._build_payload(
            output_doc=output_doc,
            species_record=species_record,
            warnings=build_warnings,
        )
        self._report_adaptive_omissions(output_doc, build_warnings)

        species_label = species_record.get("label") or "unlabeled"
        conformer_label = extra_label or f"conf{conformer_index}"
        project_label = self._config.project_label or output_doc.get("project")
        idempotency_inputs = IdempotencyInputs.from_payload(
            project_label=project_label,
            species_label=species_label,
            conformer_label=conformer_label,
            payload_kind=PAYLOAD_KIND,
            payload=payload,
        )
        idempotency_key = build_idempotency_key(idempotency_inputs)

        written = self._writer.write(
            label=f"{species_label}.{conformer_label}",
            payload=payload,
            endpoint=CONFORMER_UPLOAD_ENDPOINT,
            idempotency_key=idempotency_key,
            payload_kind=PAYLOAD_KIND,
            base_url=self._config.base_url,
            warnings=build_warnings,
        )
        logger.info(
            "TCKDB payload written: %s (key=%s)",
            written.payload_path,
            idempotency_key,
        )

        if not self._config.upload:
            return self._finalize_skipped(written)

        return self._upload(written, payload)

    def submit_artifacts_for_calculation(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calculation_id: int | None = None,
        calculation_ref: str | None = None,
        calculation_type: str,
        file_path: str | Path,
        kind: str = "output_log",
    ) -> ArtifactUploadOutcome | None:
        """Upload one local file as an artifact attached to a TCKDB calculation.

        ``output_doc`` and ``species_record`` mirror :meth:`submit_from_output`.
        ``file_path`` may be absolute or relative — relative paths are
        resolved against ``project_directory`` (passed at construction).
        ``kind`` selects the TCKDB ArtifactKind; defaults to
        ``"output_log"`` for back-compat with v1 callers.

        Returns ``None`` only if the adapter itself is disabled. Otherwise
        returns an :class:`ArtifactUploadOutcome` whose ``status`` is one
        of ``uploaded`` / ``failed`` / ``skipped``. Skip reasons:
        artifact upload disabled, kind not in config.kinds, kind not yet
        implemented in ARC, file path missing, or file exceeds
        ``max_size_mb``.

        ``calculation_ref`` is the calculation's ``calc_`` ref from the upload
        response (``calculation_ref``); it names the calculation in the URL and
        in the artifact idempotency key. ``calculation_id`` is the integer
        fallback for a response that carries no ref (an older server): it is used
        only then, with a warning, and never preferred over a ref.

        ``calculation_type`` is recorded in the sidecar but does not feed
        the URL — the endpoint takes the calculation handle directly.
        """
        outcomes = self.submit_artifact_batch_for_calculation(
            output_doc=output_doc,
            species_record=species_record,
            calculation_id=calculation_id,
            calculation_ref=calculation_ref,
            calculation_type=calculation_type,
            artifacts=[(kind, file_path)],
        )
        if outcomes is None:
            return None
        return outcomes[0] if outcomes else None

    def submit_artifact_batch_for_calculation(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calculation_id: int | None = None,
        calculation_ref: str | None = None,
        calculation_type: str,
        artifacts: list[tuple[str, str | Path]],
    ) -> list[ArtifactUploadOutcome] | None:
        """Upload artifacts for one calculation with client-side batch grouping.

        The calculation is named by ``calculation_ref`` (``calc_...``); the
        integer ``calculation_id`` is a fallback, warned about, for a response that
        returned no ref.
        """
        if not self._config.enabled:
            return None
        calculation_ref = calculation_ref or None
        if calculation_ref is None:
            if calculation_id is None:
                raise ValueError("an artifact upload needs a calculation_ref or a calculation_id")
            logger.warning(
                "TCKDB %s: the upload response carried no calculation_ref for calculation "
                "%s; naming it by its integer id, which the server deprecates.",
                _W_CALCULATION_REF_NOT_RETURNED, calculation_id,
            )
        handle = calculation_ref if calculation_ref else calculation_id

        species_label = species_record.get("label") or "unlabeled"
        artifact_cfg = self._config.artifacts
        outcomes: list[ArtifactUploadOutcome] = []
        prepared: list[_PreparedArtifactUpload] = []

        for kind, file_path in artifacts:
            prepared_item = self._prepare_artifact_upload(
                output_doc=output_doc,
                species_label=species_label,
                calculation_id=calculation_id,
                calculation_ref=calculation_ref,
                kind=kind,
                file_path=file_path,
                artifact_cfg=artifact_cfg,
            )
            if isinstance(prepared_item, ArtifactUploadOutcome):
                outcomes.append(prepared_item)
            else:
                prepared.append(prepared_item)

        if not prepared:
            return outcomes

        batch_digest = _artifact_batch_digest(prepared)
        first = prepared[0]
        prepared[0] = _PreparedArtifactUpload(
            written=first.written,
            calculation_key=(f"{handle}-{batch_digest}" if isinstance(handle, str) else f"calc{handle}-{batch_digest}"),
            calculation_id=first.calculation_id,
            path=first.path,
            kind=first.kind,
            label=first.label,
            sha256=first.sha256,
            bytes=first.bytes,
            filename=first.filename,
            calculation_ref=first.calculation_ref,
        )

        batch_outcomes = self._upload_artifact_batch(
            prepared=prepared,
            idempotency_key_prefix=_artifact_batch_idempotency_prefix(
                self._config.project_label or output_doc.get("project"),
                species_label,
            ),
        )
        return outcomes + batch_outcomes

    # ------------------------------------------------------------------
    # Computed-species bundle path (POST /uploads/computed-species)
    # ------------------------------------------------------------------

    def submit_computed_species_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_index: int = 0,
        extra_label: str | None = None,
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one computed-species bundle.

        Bundles species_entry + conformer geometry + opt/freq/sp + thermo
        + (optionally) inline artifacts into a single
        ``ComputedSpeciesUploadRequest`` and POSTs to
        ``/uploads/computed-species``. Returns ``None`` if the adapter is
        disabled, mirroring :meth:`submit_from_output`.

        Build failures (e.g. missing opt level, missing xyz) raise; the
        caller in scheduler/processor is responsible for wrapping the
        per-species call in a try/except so one bad species doesn't take
        down the rest of the run — same shape as the conformer path.
        """
        if not self._config.enabled:
            return None

        species_label = species_record.get("label") or "unlabeled"
        conformer_label = extra_label or f"conf{conformer_index}"
        project_label = self._config.project_label or output_doc.get("project")

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        species_record = _withhold_unreliable_composite_energies(output_doc, species_record, build_warnings)
        skipped = _irc_endpoint_skip(output_doc, species_record)
        if skipped is not None:
            return skipped
        payload = self._build_computed_species_payload(
            output_doc=output_doc,
            species_record=species_record,
            conformer_key=conformer_label,
            warnings=build_warnings,
        )
        self._report_adaptive_omissions(output_doc, build_warnings)

        idempotency_inputs = IdempotencyInputs.from_payload(
            project_label=project_label,
            species_label=species_label,
            conformer_label=conformer_label,
            payload_kind=COMPUTED_SPECIES_KIND,
            payload=payload,
        )
        idempotency_key = build_idempotency_key(idempotency_inputs)

        written = self._writer.write(
            label=f"{species_label}.{conformer_label}",
            payload=payload,
            endpoint=COMPUTED_SPECIES_ENDPOINT,
            idempotency_key=idempotency_key,
            payload_kind=COMPUTED_SPECIES_KIND,
            base_url=self._config.base_url,
            subdir=PayloadWriter.COMPUTED_SPECIES_SUBDIR,
            warnings=build_warnings,
        )
        logger.info(
            "TCKDB computed-species payload written: %s (key=%s)",
            written.payload_path,
            idempotency_key,
        )

        if not self._config.upload:
            return self._finalize_skipped(written)

        return self._upload(written, payload, endpoint=COMPUTED_SPECIES_ENDPOINT)

    def _build_computed_species_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_key: str,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ComputedSpeciesUploadRequest dict.

        Reuses the existing per-calc and species-entry shapers; the only
        bundle-specific surface is the conformer wrapper, dependency
        edges (declared by local calc keys), inline artifacts, and the
        optional thermo block. Producer-side omissions (a thermo block the
        producer self-check refuses) are appended to ``warnings``.
        """
        _warn_primary_opt_placeholder(species_record, warnings, field="conformers[0].primary_calculation")
        # Scans ARC exported that could not be built, key -> reason; the
        # statmech builder drops torsion links to them (``torsion_scan_not_built``).
        unbuilt_scans: dict[str, str] = {}
        included_keys, conformer_block = self._build_conformer_block(
            output_doc=output_doc,
            species_record=species_record,
            conformer_key=conformer_key,
            unbuilt_scans=unbuilt_scans,
        )

        # Additional screened conformers, if ARC surfaced them in the
        # output.yml record. Only the selected conformer carries full
        # opt/freq/sp/thermo provenance — the rest land as honest
        # "geometry + bare opt" observations so the conformer screen
        # isn't lost to the database. Selected stays first for stable
        # ordering and so consumers reading conformers[0] still get the
        # canonical conformer.
        selected_xyz = conformer_block["geometry"]["xyz_text"]
        alt_blocks = self._build_alt_conformer_blocks(
            output_doc=output_doc,
            species_record=species_record,
            selected_xyz_text=selected_xyz,
            selected_key=conformer_key,
            warnings=warnings,
        )

        bundle: dict[str, Any] = {
            "species_entry": self._species_entry_payload(species_record),
            "conformers": [conformer_block, *alt_blocks],
        }

        omitted_bacs: list[str] = []
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                species_record, legacy_1_0=_is_output_schema_1_0(output_doc),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=(
                _CALC_KEY_SP if _CALC_KEY_SP in included_keys else None
            ),
            warnings=warnings,
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, species_record),
            omitted_bac_reasons=omitted_bacs,
        )
        if applied_corrections:
            bundle["applied_energy_corrections"] = applied_corrections

        energy_level = (
            _thermo_energy_level(output_doc, species_record)
            if isinstance(species_record.get("thermo"), Mapping) else None
        )
        # The level TCKDB checks the thermo and statmech energy declarations
        # against is that of the sp calculation they link (A11), so it is read
        # off the calculations just built, not from ARC's bare level.
        energy_declaration = _energy_level_declaration(
            _thermo_energy_level(output_doc, species_record),
            calc_keys_by_role={key: key for key in included_keys},
            calculations=_calculations_by_key(
                conformer_block["primary_calculation"],
                conformer_block["additional_calculations"]),
        )
        thermo_block = _build_thermo_block(
            species_record.get("thermo"),
            # Computed-species has a single, unscoped calc namespace, so
            # each included role's own literal ("opt"/"freq"/"sp") is
            # also its bundle-local key — an identity map.
            calc_keys_by_role=_with_composite_role(
                species_record, {key: key for key in included_keys}),
            # This bundle's root is ComputedSpeciesUploadRequest, whose
            # thermo field is ``ThermoInBundle`` — the shape that accepts
            # ``source_calculations``.
            target_model="ThermoInBundle",
            warnings=warnings,
            energy_level=energy_level,
            header_corrections_level=output_doc.get("arkane_level_of_theory"),
            element_symbols=_species_element_symbols(species_record),
            energy_level_unattributable=_energy_level_unattributable(output_doc, energy_level),
            energy_declaration=energy_declaration,
            legacy_cp_data=_is_output_schema_1_0(output_doc),
        )
        # This route has no bundle-level analysis_software_release (the
        # reaction bundle does, and its thermo/statmech inherit it), so the
        # Arkane release goes on the thermo and statmech blocks themselves.
        arkane_release = _arc_analysis_software_release(output_doc)
        _note_omitted_bac_on_thermo(
            thermo_block, omitted_bacs, _bond_corrections_flag(species_record))
        _warn_bac_type_not_stated(output_doc, species_record, warnings)
        if thermo_block is not None:
            if arkane_release is not None:
                thermo_block["software_release"] = dict(arkane_release)
            bundle["thermo"] = thermo_block

        # Workflow-tool release at bundle level (in addition to per-calc):
        # mirrors what the conformer adapter records and lets the server
        # tag the species_entry with the producer.
        arc_wt = _arc_workflow_tool_release(output_doc)
        if arc_wt is not None:
            bundle["workflow_tool_release"] = arc_wt

        # Statmech block: carries frequency-scale-factor provenance,
        # plus richer base statmech metadata (external_symmetry,
        # is_linear, rigid_rotor_kind, statmech_treatment, point_group)
        # and slim torsion summaries when ARC populated them in the
        # species record's ``statmech`` subdict (written by
        # ``arc/output.py::_statmech_to_dict``). The container is omitted
        # entirely when nothing useful resolves — no empty containers.
        # Computed-species emits unscoped role keys (``opt`` / ``freq``
        # / ``sp``) directly as the bundle-local calc keys.
        species_calc_keys_by_role: dict[str, str] = {
            role: role for role in (_CALC_KEY_OPT, _CALC_KEY_FREQ, _CALC_KEY_SP)
            if role in included_keys
        }
        statmech_block = _build_statmech_block_for_species(
            output_doc=output_doc,
            species_record=species_record,
            calc_keys_by_role=_with_composite_role(
                species_record, species_calc_keys_by_role),
            workflow_tool_release=arc_wt,
            # This bundle's root is ComputedSpeciesUploadRequest, whose
            # statmech field is ``StatmechInBundle``.
            target_model="StatmechInBundle",
            unbuilt_scans=unbuilt_scans,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            warnings=warnings,
            warning_field="statmech",
            energy_declaration=energy_declaration,
        )
        if statmech_block is not None:
            if arkane_release is not None:
                statmech_block["software_release"] = dict(arkane_release)
            bundle["statmech"] = statmech_block

        return bundle

    def _build_conformer_block(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_key: str,
        unbuilt_scans: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Build one ConformerInBundle and return (included_calc_keys, block).

        A rotor scan that cannot be built is skipped with a warning and, when
        ``unbuilt_scans`` is given, recorded there as ``key -> reason``.

        The keys list is what's actually present in the bundle's calc
        namespace, used to drive thermo's source_calculations links.

        The conformer's reference xyz (the optimized geometry) is
        normalized once and threaded through to ``_build_calc_in_bundle``
        so freq + sp can declare it as their explicit input geometry —
        ARC's invariant guarantees they ran on this geometry. Server
        auto-fill would also reach the same answer for freq/sp, but
        being explicit makes the bundle self-describing.
        """
        # Required: the bundle's ConformerInBundle.geometry, also reused
        # as the explicit input_geometries entry on freq + sp.
        conformer_xyz_text = _require_xyz_text(species_record)

        if _is_single_atom_geometry(conformer_xyz_text):
            # A single atom has no geometry to optimise: its primary calculation
            # is its own single point (tckdb-schemas 0.59, TCKDB#610), and it has
            # no opt, coarse opt, freq or rotor scan to carry.
            primary_calc = self._build_monatomic_primary_sp(
                output_doc=output_doc, species_record=species_record,
                calc_key=_CALC_KEY_SP, conformer_xyz_text=conformer_xyz_text)
            block: dict[str, Any] = {
                "key": conformer_key,
                "geometry": _geometry_payload(
                    species_record, conformer_xyz_text, species_record.get("xyz_isotopes")),
                "primary_calculation": primary_calc,
                "additional_calculations": [],
            }
            if species_record.get("label"):
                block["label"] = str(species_record["label"])[:64]
            return [_CALC_KEY_SP], block

        # Coarse-opt provenance. Emitted only when ``coarse_opt_log``
        # exists AND ``coarse_opt_output_xyz`` is populated — the
        # output xyz is what chains the fine opt to the coarse opt
        # (it's both opt_coarse's output and the fine opt's declared
        # input). When the coarse log existed but its geometry didn't
        # parse cleanly, ``arc/output.py`` deliberately leaves
        # ``coarse_opt_output_xyz`` null so we fall back to single-stage
        # bundle shape rather than emit a half-described opt_coarse.
        opt_coarse_calc = self._build_opt_coarse_calc(
            output_doc=output_doc, species_record=species_record,
        )

        # Fine opt's depends_on: gain an ``optimized_from → opt_coarse``
        # edge when coarse was emitted. Otherwise no upstream calc.
        fine_opt_depends_on: list[Mapping[str, Any]] | None = None
        if opt_coarse_calc is not None:
            fine_opt_depends_on = [
                {"parent_calculation_key": _CALC_KEY_OPT_COARSE,
                 "role": "optimized_from"}
            ]

        primary_calc = self._build_calc_in_bundle(
            output_doc=output_doc,
            species_record=species_record,
            calc_key=_CALC_KEY_OPT,
            calc_type="opt",
            level_kind="opt",
            ess_job_key="opt",
            result_field="opt_result",
            result_payload=_opt_result_payload(species_record),
            depends_on=fine_opt_depends_on,
            tckdb_origin=None,
            conformer_xyz_text=conformer_xyz_text,
        )

        included: list[str] = [_CALC_KEY_OPT]
        additional: list[dict[str, Any]] = []
        # Coarse opt is an *additional* calc, not primary — fine opt is
        # the geometry of record. Order matters for thermo-source-link
        # determinism: coarse goes first so the bundle reads
        # opt → opt_coarse → freq → sp in additional_calculations.
        if opt_coarse_calc is not None:
            additional.append(opt_coarse_calc)
            included.append(_CALC_KEY_OPT_COARSE)

        freq_result = _freq_result_payload(species_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=_CALC_KEY_FREQ,
                    calc_type="freq",
                    level_kind="freq",
                    ess_job_key="freq",
                    result_field="freq_result",
                    result_payload=freq_result,
                    depends_on=[{"parent_calculation_key": _CALC_KEY_OPT, "role": "freq_on"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                ))
                included.append(_CALC_KEY_FREQ)
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-species: freq calculation skipped for label=%s: %s",
                    species_record.get("label"), exc,
                )

        sp_result = _sp_result_payload(species_record)
        if sp_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=_CALC_KEY_SP,
                    calc_type="sp",
                    level_kind="sp",
                    ess_job_key="sp",
                    result_field="sp_result",
                    result_payload=sp_result,
                    depends_on=[{"parent_calculation_key": _CALC_KEY_OPT, "role": "single_point_on"}],
                    tckdb_origin=(
                        _sp_reuse_origin(output_doc, species_record)
                    ),
                    conformer_xyz_text=conformer_xyz_text,
                ))
                included.append(_CALC_KEY_SP)
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-species: sp calculation skipped for label=%s: %s",
                    species_record.get("label"), exc,
                )

        # Rotor scans. ``arc/output.py`` populates the species record's
        # ``additional_calculations`` list with one entry per successful
        # 1D rotor whose log was parseable. Each entry already carries a
        # bundle-local ``key`` (``scan_rotor_<i>``) and a TCKDB-shaped
        # ``scan_result`` dict; the adapter only needs to layer level /
        # software / workflow_tool_release on top. An explicit scan level
        # is required; the optimization level does not identify the scan. The
        # ``depends_on`` edge points back to opt — the scan is a series
        # of constrained reoptimizations from that geometry.
        for scan_entry in _scan_entries_from_record(species_record):
            if not isinstance(scan_entry, Mapping):
                continue
            if scan_entry.get("type") != _CALC_KEY_SCAN:
                continue
            scan_key = scan_entry.get("key")
            scan_result = scan_entry.get("scan_result")
            if not isinstance(scan_key, str) or not scan_key:
                continue
            if not isinstance(scan_result, Mapping):
                continue
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=scan_key,
                    calc_type=_CALC_KEY_SCAN,
                    level_kind="scan",
                    ess_job_key="scan",
                    result_field="scan_result",
                    result_payload=scan_result,
                    depends_on=[{"parent_calculation_key": _CALC_KEY_OPT,
                                 "role": "scan_parent"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    calc_role=_CALC_KEY_SCAN,
                    source_constraints=scan_entry.get("constraints"),
                    output_log_path=scan_entry.get("source_log"),
                    observed_software=scan_entry.get("ess_software"),
                    observed_version=scan_entry.get("ess_version"),
                    level_job_type=_scan_job_type(output_doc, species_record, scan_key),
                ))
                included.append(scan_key)
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-species: scan calculation %s skipped for label=%s: %s",
                    scan_key, species_record.get("label"), exc,
                )
                if unbuilt_scans is not None:
                    unbuilt_scans[scan_key] = str(exc)

        block: dict[str, Any] = {
            "key": conformer_key,
            "geometry": _geometry_payload(
                species_record, conformer_xyz_text, species_record.get("xyz_isotopes")),
            "primary_calculation": primary_calc,
            "additional_calculations": additional,
        }
        label = species_record.get("label")
        if label:
            block["label"] = str(label)[:64]
        return included, block

    def _build_monatomic_primary_sp(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calc_key: str,
        conformer_xyz_text: str,
    ) -> dict[str, Any]:
        """The ``sp`` primary calculation of a single atom (tckdb-schemas 0.59, TCKDB#610).

        Contract (``ConformerInBundle.validate_primary_is_opt``): "A monatomic species
        has no geometry to optimise ... send that single point, once, as
        ``primary_calculation`` with ``type: "sp"``, its ``sp_result``, and the atom's
        one-atom XYZ as the conformer ``geometry``. Do not relabel it as an ``opt``."
        It is the atom's own job: its log, level, program, energy and settings as ARC
        states them for the sp (an output.yml 1.3 monoatomic's sp log is also its
        ``opt_log``, and ``levels.sp`` is its level). It carries no ``depends_on``
        (there is no opt to depend on) and no reused-result marker. ``SPResultPayload``
        has no convergence field, so ARC's ``converged`` for the atom is not sent. The
        energy is ``sp_energy_hartree``, else (non-composite atoms only) the
        ``opt_final_energy_hartree`` ARC parsed from the same one log
        (``_monatomic_sp_result_payload``).
        """
        sp_result = _monatomic_sp_result_payload(species_record)
        if sp_result is None:
            raise _atom_without_sp_energy_error(species_record)
        return self._build_calc_in_bundle(
            output_doc=output_doc,
            species_record=species_record,
            calc_key=calc_key,
            calc_role=_CALC_KEY_SP,
            calc_type="sp",
            level_kind="sp",
            ess_job_key="sp",
            result_field="sp_result",
            result_payload=sp_result,
            depends_on=None,
            tckdb_origin=None,
            conformer_xyz_text=conformer_xyz_text,
        )

    def _build_alt_conformer_blocks(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        selected_xyz_text: str,
        selected_key: str,
        warnings: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Additional ``ConformerInBundle`` dicts for ARC's screened conformers.

        ARC's ``output.yml`` species record lists the conformers its screen
        optimized alongside the selected one (``conformers``, xyz strings).
        Each needs a primary ``opt`` calculation, and TCKDB requires a
        ``level_of_theory`` on every calculation
        (``ConformerInBundle.primary_calculation``), so there is no honest
        partial form.

        Output.yml 1.3 states the level of each conformer geometry's optimization
        (``conformer_levels``); each conformer is then filed at its own level, with
        the program of the header ``conformer_opt_level`` when that names the same
        level (``_stated_conformer_level``), and its electronic energy only with a
        stated kind and level (``_stated_conformer_opt_result``,
        ``_stated_conformer_sp``). A ``null`` entry (force field, user-supplied) is
        not an ESS calculation and is omitted (``conformer_geometry_not_esss_optimized``).
        Everything below describes output.yml 1.2 and older.

        ARC screens conformers at its *conformer* level (``conformer_opt_level``;
        default ``wb97xd/def2svp``, lower than the ``opt_level`` default), and
        ``output.yml`` exports no conformer level. The project's ``restart.yml``
        does record ``conformer_opt_level`` and ``job_types`` (``arc/main.py``
        ``as_dict``), so the screened conformers are filed at that level, with
        the program the level names, when all of these hold
        (``_conformer_screen_level``):

        * ``restart.yml`` records the level and ``job_types['conf_opt']`` is true
          (otherwise the geometries are force-field ones, never optimized by an
          ESS);
        * the adaptive levels do not name ``conf_opt`` for the species' range
          (a species-specific screening level);
        * the conformer's own ``conformer_energies`` entry is not null (ARC
          fills it, and replaces the force-field geometry with the optimized
          one, only when that conformer's ``conf_opt`` finished; a missing or
          misaligned energies list leaves every conformer unverified).

        Gap: when no conformer converged ARC re-runs all of them at a
        troubleshooting level (``arc/scheduler.py`` 5050-5078,
        ``ess_trsh_methods`` ``'conf_opt: <level>'``), but nothing durable
        records that (it lives in the job objects, kept in ``restart.yml`` only
        while running), so ``conformer_opt_level`` may then be wrong.

        Otherwise, and whenever the level lacks a program, they are omitted and
        reported once per species as ``conformer_level_not_stated`` (ARC should
        export the conformer level, BRIDGE_ROADMAP B3). Each conformer is a bare
        ``opt`` marked as a screened-conformer anchor (``tckdb_origin``), with
        no ``opt_result`` and no relative energy: ``conformer_energies`` are
        workflow-local values (the reference is the lowest of ARC's screening
        set), which TCKDB, being workflow-tool agnostic, does not receive.

        Skips: an empty ``conformers`` list, xyz strings that fail to normalize,
        and exact duplicates of the selected geometry or of an earlier
        candidate. Failures log and continue; they never fail the bundle.
        """
        raw_conformers = species_record.get("conformers")
        if not isinstance(raw_conformers, (list, tuple)) or not raw_conformers:
            return []
        if _is_single_atom_geometry(selected_xyz_text):
            # An atom has one geometry; an alternative conformer would need the
            # bare opt that TCKDB does not take for an atom.
            return []

        label = species_record.get("label")
        candidates: list[str] = []
        unoptimized: list[str] = []
        candidate_index: dict[str, int] = {}
        seen_xyz: set[str] = {selected_xyz_text}
        energies = species_record.get("conformer_energies")
        lockstep = isinstance(energies, (list, tuple)) and len(energies) == len(raw_conformers)
        conformer_levels = species_record.get("conformer_levels")
        # Schema 1.3 states the level of the optimization behind each conformer
        # geometry (``conformer_levels``, in lockstep with ``conformers``).
        stated_levels = (
            isinstance(conformer_levels, list) and len(conformer_levels) == len(raw_conformers))
        # Schema 1.3 (ARC #1059 ebc88ec8) also states the program and banner of each conformer's own
        # optimization log. Where those lists are present the program is theirs, never the header's.
        observed_programs = _conformer_observed_lists(species_record) is not None
        for index, raw_xyz in enumerate(raw_conformers):
            normalized = _normalize_xyz_text(raw_xyz, label)
            if normalized is None or normalized in seen_xyz:
                continue
            seen_xyz.add(normalized)
            candidates.append(normalized)
            candidate_index[normalized] = index
            if stated_levels:
                if not isinstance(conformer_levels[index], Mapping):
                    unoptimized.append(normalized)
                continue
            # ARC replaces conformers[i] with the optimized geometry only when
            # its conf_opt finished (arc/scheduler.py:3133-3134, else it just
            # warns), and fills conformer_energies[i] at the same time; a null
            # energy means the geometry is still the force-field one.
            if not lockstep or energies[index] is None:
                unoptimized.append(normalized)
        if not candidates:
            return []

        level = None if stated_levels else _conformer_screen_level(output_doc, species_record)
        blocks: list[dict[str, Any]] = []
        no_program: list[str] = []
        no_isotopes: list[str] = []
        if level is not None or stated_levels:
            for normalized in candidates:
                if normalized in unoptimized:
                    continue
                result_payload = None
                observed_version = None
                if stated_levels:
                    index = candidate_index[normalized]
                    level = _stated_conformer_level(output_doc, species_record, index)
                    if level is None:
                        no_program.append(normalized)
                        continue
                    if observed_programs:
                        observed_version = _conformer_observed_provenance(species_record, index)[1]
                    result_payload = _stated_conformer_opt_result(
                        species_record, index, conformer_levels[index])
                alt_key = f"alt{len(blocks)}"
                if alt_key == selected_key:
                    # Caller picked a selected key in our alt-key namespace;
                    # skip rather than silently shadow the selected entry.
                    logger.warning(
                        "TCKDB computed-species: alt conformer for label=%s "
                        "would collide with selected key %r; skipping.",
                        label, selected_key,
                    )
                    continue
                try:
                    opt_calc = self._calculation_payload(
                        output_doc, species_record,
                        calc_type="opt",
                        level=level,
                        ess_job_key="conf_opt",
                        result_field="opt_result",
                        result_payload=result_payload,
                        # ``tckdb_origin`` tags the row as a screened-conformer
                        # anchor, NOT a parsed opt job of the selected conformer.
                        tckdb_origin=_screened_conformer_origin(),
                        # The program of the conformer's own optimization log and its banner
                        # (``conformer_ess_software`` / ``conformer_ess_version``), when ARC states them.
                        observed_software=level["software"] if observed_programs else None,
                        observed_version=observed_version,
                    )
                except ValueError as exc:
                    logger.warning(
                        "TCKDB computed-species: alt conformers of label=%s "
                        "skipped (opt calc not buildable): %s", label, exc,
                    )
                    break
                opt_calc["key"] = f"{alt_key}_opt"
                # Anchor opt's output_geometries to the alt xyz explicitly: the
                # backend's auto-fill would pin every conformer's opt output to
                # the selected conformer's geometry of record.
                alt_geometry = _geometry_payload(
                    species_record, normalized, _conformer_isotopes(species_record, candidate_index[normalized]))
                if alt_geometry is None:
                    no_isotopes.append(normalized)
                    continue
                opt_calc["output_geometries"] = [
                    {"geometry": dict(alt_geometry), "role": "final"},
                ]
                alt_additional: list[dict[str, Any]] = []
                conformer_sp = (
                    _stated_conformer_sp(
                        output_doc, species_record, candidate_index[normalized],
                        conformer_levels[candidate_index[normalized]])
                    if stated_levels else None)
                if conformer_sp is not None:
                    sp_level, sp_energy = conformer_sp
                    try:
                        sp_calc = self._calculation_payload(
                            output_doc, species_record,
                            calc_type="sp", level=sp_level, ess_job_key="conf_sp",
                            result_field="sp_result",
                            result_payload={"electronic_energy_hartree": sp_energy},
                            tckdb_origin=_screened_conformer_origin("sp"),
                        )
                    except ValueError as exc:
                        logger.warning(
                            "TCKDB computed-species: conformer sp of label=%s skipped: %s",
                            label, exc)
                    else:
                        sp_calc["key"] = f"{alt_key}_sp"
                        sp_calc["depends_on"] = [
                            {"parent_calculation_key": f"{alt_key}_opt", "role": "single_point_on"}]
                        sp_calc["input_geometries"] = [dict(alt_geometry)]
                        alt_additional.append(sp_calc)
                block: dict[str, Any] = {
                    "key": alt_key,
                    "geometry": alt_geometry,
                    "primary_calculation": opt_calc,
                    "additional_calculations": alt_additional,
                }
                if label:
                    block["label"] = str(label)[:64]
                blocks.append(block)
        omitted = len(candidates) - len(blocks)
        if no_isotopes:
            _warn_isotopes_not_stated(
                warnings, label=label, what=f"{len(no_isotopes)} screened conformer geometr(ies)")
            omitted -= len(no_isotopes)
        if stated_levels:
            if unoptimized:
                _warn_conformer_not_esss_optimized(
                    warnings, label=label, omitted=len(unoptimized),
                    force_field=species_record.get("conformer_force_field"))
            if no_program:
                _warn_conformer_program_not_stated(
                    warnings, label=label, omitted=len(no_program), observed=observed_programs)
        elif omitted:
            _warn_conformer_level_not_stated(warnings, label=label, omitted=omitted)
        return blocks

    def _build_calc_in_bundle(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calc_key: str,
        calc_type: str,
        level_kind: str,
        ess_job_key: str,
        result_field: str,
        result_payload: Mapping[str, Any] | None,
        depends_on: list[Mapping[str, Any]] | None,
        tckdb_origin: Mapping[str, Any] | None,
        conformer_xyz_text: str | None = None,
        calc_role: str | None = None,
        source_constraints: list | None = None,
        include_artifacts: bool = True,
        level_job_type: str | None = None,
        level_override: Mapping[str, Any] | None = None,
        output_log_path: str | None = None,
        observed_software: str | None = None,
        observed_version: str | None = None,
    ) -> dict[str, Any]:
        """Build one CalculationInBundle dict.

        ``observed_software`` / ``observed_version`` are the program and banner a
        rotor scan states for its own log (output.yml 1.3 ``rotor_scans[]``).

        ``output_log_path`` names the calculation's own output log when it is
        not a field of the species record (a rotor scan's
        ``rotor_scans[].source_log``); it is used for the ``output_log``
        artifact of a ``scan`` calculation and ignored for other roles.

        ``calc_key`` is the bundle-local identity (e.g. ``"opt"`` for a
        single-species bundle, ``"r0_opt"`` for a reaction bundle).
        ``calc_role`` is the chemistry role used for policy lookups
        (input/output geometry rules, log-field map). When ``calc_role``
        is ``None`` it defaults to ``calc_key`` — that's the
        single-species case where the two are identical and the existing
        callers don't have to change.

        Reuses :meth:`_calculation_payload` for the level/software/result
        plumbing, then layers on the bundle-specific fields: ``key``,
        ``depends_on``, ``input_geometries``, and inline ``artifacts``.

        Input-geometry policy (matches TCKDB v0 backend):
        - ``opt``: emit ``input_geometries`` only when ARC has the
          actual pre-opt xyz on the record (``opt_input_xyz``). Never
          fall back to the conformer's optimized geometry — that's
          opt's *output*, not its input. If absent, the field is
          omitted; backend has no auto-fill for opt.
        - ``freq`` / ``sp``: emit ``input_geometries`` set to the
          conformer's optimized geometry (passed in via
          ``conformer_xyz_text``). ARC's invariant guarantees these
          ran on that geometry. Backend would auto-fill the same value
          if we omitted, but explicit is better — keeps the bundle
          self-describing and removes ambiguity for any consumer that
          doesn't replicate the auto-fill rule.
        """
        level = (
            level_override if level_override is not None
            else _resolve_level(output_doc, level_kind, species_record,
                                job_type=level_job_type)
        )
        role = calc_role if calc_role is not None else calc_key
        final_settings = _final_settings_for_calc(
            species_record=species_record,
            calc_role=role,
        )
        calc = self._calculation_payload(
            output_doc, species_record,
            calc_type=calc_type,
            level=level,
            ess_job_key=ess_job_key,
            result_field=result_field,
            result_payload=result_payload,
            tckdb_origin=tckdb_origin,
            final_settings=final_settings,
            scf_stability_target=(role == _CALC_KEY_OPT),
            observed_software=observed_software,
            observed_version=observed_version,
        )
        calc["key"] = calc_key
        if depends_on:
            calc["depends_on"] = [dict(d) for d in depends_on]
        input_geometries = self._input_geometries_for_calc(
            calc_role=role,
            species_record=species_record,
            conformer_xyz_text=conformer_xyz_text,
        )
        if input_geometries:
            calc["input_geometries"] = input_geometries
        output_geometries = self._output_geometries_for_calc(
            calc_role=role,
            species_record=species_record,
            conformer_xyz_text=conformer_xyz_text,
        )
        if output_geometries:
            calc["output_geometries"] = output_geometries
        # Optional inline Cartesian Hessian for freq calcs (DR-0030). Parsed
        # from the freq job's ESS output; skipped silently when unavailable so
        # it can never break payload construction.
        if role == _CALC_KEY_FREQ:
            hessian = self._build_freq_hessian_payload(
                output_doc=output_doc,
                species_record=species_record,
                geometry_xyz_text=conformer_xyz_text,
            )
            if hessian is not None:
                calc["hessian"] = hessian
        # ``include_artifacts=False`` short-circuits the read+base64 of
        # potentially multi-MB logs. The standalone transition-state
        # endpoint has no artifact slot, so building them here would waste
        # I/O and then be stripped downstream anyway (see
        # ``_ts_calc_to_standalone``). We skip the work entirely rather
        # than build-then-drop.
        if include_artifacts:
            artifacts = self._inline_artifacts_for_calc(
                species_record, calc_role=role, output_log_path=output_log_path)
            # Schema defaults `artifacts: []`. Emit explicitly only when we
            # have bytes to send (or when artifact upload is enabled and we
            # want to signal "no log available" with an empty list); omit
            # otherwise.
            if artifacts:
                calc["artifacts"] = artifacts

        # Held-fixed coordinate constraints. ``source_constraints`` wins
        # when caller supplies it explicitly (scan calcs pull from the
        # ``additional_calculations`` entry). Otherwise we pick up the
        # per-job list ``arc/output.py`` populated on the species record.
        constraints_source = source_constraints
        if constraints_source is None:
            field = _CONSTRAINTS_FIELD_BY_CALC_KEY.get(role)
            if field:
                constraints_source = species_record.get(field)
        constraint_payload = _serialize_calc_constraints(constraints_source)
        if constraint_payload:
            calc["constraints"] = constraint_payload
        return calc

    @staticmethod
    def _output_geometries_for_calc(
        *,
        calc_role: str,
        species_record: Mapping[str, Any],
        conformer_xyz_text: str | None,
    ) -> list[dict[str, Any]]:
        """Compute the ``output_geometries`` list for one calc, per-kind policy.

        Each entry is shaped ``{"geometry": {"xyz_text": ...}, "role":
        "final"}`` per TCKDB's ``OutputGeometryEntry``. Returns ``[]``
        when nothing should be emitted; the caller drops empty lists.

        Policy mirrors what the calc actually produced:

        - ``opt`` (fine): produced the conformer's geometry of record.
          Emit ``conformer_xyz_text`` with ``role=final``. If absent
          (e.g., bundle-build path that skipped requiring xyz), the
          backend still has its single-stage fallback that links opt
          to the conformer geometry — but that fallback is server-
          side and shouldn't be relied on once we're declaring outputs
          explicitly elsewhere.
        - ``opt_coarse``: produced ``coarse_opt_output_xyz``. Emit it
          with ``role=final``.
        - ``freq`` / ``sp``: don't move atoms; we don't surface a
          standalone "freq output geometry" or "sp output geometry"
          today. Backend's freq/sp fallback now creates zero output
          rows for these (per the new contract), so omitting is correct.
        """
        if calc_role == _CALC_KEY_OPT:
            if not conformer_xyz_text:
                return []
            geometry = _geometry_payload(
                species_record, conformer_xyz_text, species_record.get("xyz_isotopes"))
            return [{"geometry": geometry, "role": "final"}] if geometry else []
        if calc_role == _CALC_KEY_OPT_COARSE:
            coarse_out = species_record.get("coarse_opt_output_xyz")
            if not coarse_out:
                return []
            normalized = _normalize_xyz_text(coarse_out, species_record.get("label"))
            if not normalized:
                return []
            geometry = _geometry_payload(
                species_record, normalized, species_record.get("coarse_opt_output_xyz_isotopes"))
            return [{"geometry": geometry, "role": "final"}] if geometry else []
        # freq / sp / irc / others: no output_geometries today.
        return []

    @staticmethod
    def _input_geometries_for_calc(
        *,
        calc_role: str,
        species_record: Mapping[str, Any],
        conformer_xyz_text: str | None,
    ) -> list[dict[str, Any]]:
        """Compute the ``input_geometries`` list for one calc, per-kind policy.

        Returns ``[]`` when nothing should be emitted. The caller checks
        truthiness — empty lists are dropped from the payload so we don't
        send a zero-length array where None is more accurate.
        """
        if calc_role == _CALC_KEY_OPT:
            opt_input_xyz = species_record.get("opt_input_xyz")
            if not opt_input_xyz:
                return []
            normalized = _normalize_xyz_text(opt_input_xyz, species_record.get("label"))
            if not normalized:
                return []
            geometry = _geometry_payload(
                species_record, normalized, species_record.get("opt_input_xyz_isotopes"))
            return [geometry] if geometry else []
        if calc_role == _CALC_KEY_OPT_COARSE:
            # Coarse opt's input is the species' truly-initial xyz —
            # ``coarse_opt_input_xyz`` from arc/output.py. The caller
            # only invokes this when the coarse stage actually ran, so
            # an absent value here is a real bug; skip rather than
            # fabricate (the parent ``_build_opt_coarse_calc`` short-
            # circuits before reaching us if either coarse field is
            # missing).
            coarse_in = species_record.get("coarse_opt_input_xyz")
            if not coarse_in:
                return []
            normalized = _normalize_xyz_text(coarse_in, species_record.get("label"))
            if not normalized:
                return []
            geometry = _geometry_payload(
                species_record, normalized, species_record.get("coarse_opt_input_xyz_isotopes"))
            return [geometry] if geometry else []
        if calc_role in (_CALC_KEY_FREQ, _CALC_KEY_SP, _CALC_KEY_IRC):
            # ARC invariant: freq, sp, and (TS) irc all run on the
            # conformer's optimized xyz. Surface it explicitly rather
            # than relying on backend auto-fill — keeps the bundle
            # self-describing.
            if not conformer_xyz_text:
                return []
            return [_geometry_payload(
                species_record, conformer_xyz_text, species_record.get("xyz_isotopes"))]
        return []

    def _build_opt_coarse_calc(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calc_key: str = _CALC_KEY_OPT_COARSE,
    ) -> dict[str, Any] | None:
        """Build the coarse opt's CalculationInBundle dict, or return None.

        ``calc_key`` is the bundle-local identity. Computed-species uses
        the bare role (``"opt_coarse"``); computed-reaction passes a
        species-namespaced variant (``"r0_opt_coarse"``, ``"p1_opt_coarse"``)
        so it stays globally unique across the bundle. The chemistry
        role passed to ``_build_calc_in_bundle`` stays
        ``_CALC_KEY_OPT_COARSE`` either way — it's what drives the
        ``coarse_opt_log`` / ``coarse_opt_input_xyz`` lookups on the
        species record.

        Returns ``None`` when:
        - ``coarse_opt_log`` is absent (no coarse stage ran)
        - ``coarse_opt_output_xyz`` is null (coarse log existed but its
          final geometry couldn't be parsed; modeling the calc without
          its output geometry would create a half-described provenance
          row, so we drop the whole opt_coarse rather than mislead)

        Note on output geometry: TCKDB's bundle workflow auto-anchors
        every calc's ``CalculationOutputGeometry`` to the conformer
        geometry (the FINE opt's output). Until the bundle schema
        gains an ``output_geometries`` field on ``CalculationInBundle``,
        opt_coarse's output-geometry row will incorrectly point at
        the fine geometry server-side. The input chain
        (``calculation_input_geometry``) is correct — that's what was
        empty before this change and is what the producer can fix.
        """
        if not species_record.get("coarse_opt_log"):
            return None
        if not species_record.get("coarse_opt_output_xyz"):
            logger.debug(
                "TCKDB: coarse_opt_log present but coarse_opt_output_xyz "
                "is null for label=%s — falling back to single-stage opt "
                "bundle.",
                species_record.get("label"),
            )
            return None
        result_payload = _coarse_opt_result_payload(species_record)
        # Reuse the standard calc-in-bundle builder. opt_coarse takes
        # the same level/software as the fine opt (ARC runs both
        # stages at the configured opt level — the difference is
        # convergence criterion, not method/basis). No depends_on
        # (it's the chain head). No tckdb_origin (it's a real ESS run,
        # not a reuse of another calc's result).
        try:
            coarse = self._build_calc_in_bundle(
                output_doc=output_doc,
                species_record=species_record,
                calc_key=calc_key,
                calc_role=_CALC_KEY_OPT_COARSE,
                calc_type="opt",
                level_kind="opt",
                ess_job_key="opt",
                result_field="opt_result",
                result_payload=result_payload,
                depends_on=None,
                tckdb_origin=None,
                conformer_xyz_text=None,  # opt_coarse's input is its own xyz, not the conformer
            )
            # ``opt_route`` is the keyword line of the fine opt, not of the coarse stage.
            coarse.pop("parameters", None)
            return coarse
        except ValueError as exc:
            logger.warning(
                "TCKDB: opt_coarse calculation skipped for label=%s "
                "(calc_key=%s): %s",
                species_record.get("label"), calc_key, exc,
            )
            return None

    def _inline_artifacts_for_calc(
        self,
        species_record: Mapping[str, Any],
        *,
        calc_role: str,
        output_log_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the inline artifact list for one calc within a bundle.

        ``calc_role`` is the chemistry role (``opt``/``freq``/``sp``/
        ``opt_coarse``/``irc``) used to look up the matching record
        field name (``opt_log`` etc.). The bundle-local key — e.g.
        ``r0_opt`` for a reaction — is irrelevant here; ARC stores the
        log paths on the species record under role-keyed names.

        Iterates ``_INLINE_ARTIFACT_SOURCES`` (currently ``output_log``
        and ``input``) and emits one ArtifactIn dict per kind whose
        record path resolves to a real file on disk. Each kind is
        independently gated on ``config.artifacts.kinds``, so a user
        can opt into logs but not decks (or vice versa).

        Skip rules per (calc_role, kind):
            - artifacts globally disabled              → entire list = []
            - kind not in config.artifacts.kinds       → that kind only
            - record-field path missing / null         → that kind only
            - resolved file not on disk                → that kind only
            - file > artifacts.max_size_mb             → that kind only (warn)
        """
        artifact_cfg = self._config.artifacts
        if not artifact_cfg.upload:
            return []
        artifacts: list[dict[str, Any]] = []
        for kind, field_map in _INLINE_ARTIFACT_SOURCES:
            if kind not in artifact_cfg.kinds:
                continue
            # ``output_log`` for ``ts_guess`` is method-dispatched
            # (NEB → ``neb_log``, GSM → ``gsm_log``); other (kind, role)
            # combinations look up in the static map.
            path_value = None
            if kind == "output_log":
                record_field = _resolve_log_field(calc_role, species_record)
                if calc_role == _CALC_KEY_SCAN:
                    # A scan's log is per rotor, so it is carried on the scan
                    # entry rather than in a species-record field.
                    path_value = output_log_path
            else:
                record_field = field_map.get(calc_role)
            if (calc_role == _CALC_KEY_OPT and arc13.is_composite_run(species_record)
                    and arc13.software_job_key(species_record, _CALC_KEY_OPT) == "composite"
                    and not species_record.get(record_field or "")):
                # A composite run's geometry job is its composite job: its log and
                # input deck are what the record's ``opt`` calculation ran.
                record_field = {"output_log": "composite_log", "input": "composite_input"}[kind]
            artifact = self._read_inline_artifact(
                species_record,
                calc_role=calc_role,
                kind=kind,
                record_field=record_field,
                path_value=path_value,
            )
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    def _read_inline_artifact(
        self,
        species_record: Mapping[str, Any],
        *,
        calc_role: str,
        kind: str,
        record_field: str | None,
        path_value: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve, read, hash, and base64-encode one artifact for the bundle.

        ``path_value`` supplies the path directly (a rotor scan's log);
        otherwise it is read from ``record_field`` on the species record.

        Returns ``None`` (with a debug or warning log) on any of:
        unknown calc_key, missing/null record path, file not on disk, or
        file exceeding ``max_size_mb``. Otherwise returns the
        ``ArtifactIn``-shaped dict ready to drop into ``calc.artifacts``.
        """
        if path_value is None:
            if record_field is None:
                return None
            path_value = species_record.get(record_field)
        if not path_value:
            return None
        return self._read_artifact_file(
            path_value,
            calc_role=calc_role,
            kind=kind,
            label=species_record.get("label"),
        )

    def _freq_hessian_available(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
    ) -> bool:
        """Whether parser evidence (or the log fallback) yields a freq Hessian.

        This is the evidence that Arkane had a force-constant matrix for the
        species, which ``statmech_treatment`` depends on. It reuses
        ``_build_freq_hessian_payload``, so it is True exactly when a
        Hessian payload can be built for the freq calculation.
        """
        return self._build_freq_hessian_payload(
            output_doc=output_doc, species_record=species_record,
            geometry_xyz_text=None,
        ) is not None

    def _build_freq_hessian_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        geometry_xyz_text: str | None,
    ) -> dict[str, Any] | None:
        """Build the optional ``hessian`` payload for a freq calculation.

        Parses the Cartesian Hessian from the freq job's ESS output (Gaussian
        log block or Orca sibling ``.hess`` file) via the parser-layer adapters,
        and packs it into the TCKDB ``HessianPayload`` shape:
        ``{"geometry": {"xyz_text": ...}, "lower_triangle_hartree_bohr2":
        [...], "source": "parsed_log"|"parsed_hess", "parser_version": ...}``.

        The Hessian is native atomic units (hartree/bohr²) straight from the
        parser — no unit conversion is applied here. Portable evidence carries
        the geometry in the Hessian's own Cartesian frame, which must be used
        unchanged even when the conformer uses a different orientation.
        Raw parser fallback also requires the parser's frame-matched geometry;
        the conformer's geometry cannot establish the Hessian coordinate frame.

        Returns ``None`` — never raises — when the Hessian is unavailable for
        any reason (no geometry, no freq log on disk, unknown/unsupported ESS,
        missing FC block / ``.hess``, monatomic species, or any parse error).
        The Hessian is strictly optional and must never break payload
        construction.
        """
        record_kind = "transition_state" if species_record.get("is_ts") else "species"
        lookup = self._evidence.lookup(
            output_doc, record_kind, str(species_record.get("label") or ""), "freq_hessian"
        )
        if lookup.state == "available":
            value = lookup.value or {}
            return {
                "geometry": {"xyz_text": value["geometry_xyz_text"]},
                "lower_triangle_hartree_bohr2": list(value["lower_triangle"]),
                "source": value["source"],
                "parser_version": value["parser_version"],
            }
        if lookup.state == "unavailable":
            return None
        log_path = species_record.get(_LOG_FIELD_BY_CALC_KEY[_CALC_KEY_FREQ])
        if not log_path:
            return None
        resolved = self._resolve_local_path(log_path)
        if resolved is None or not resolved.is_file():
            return None
        try:
            # Late import via the optional-ARC boundary: keeps the ESS parser
            # adapters (heavyweight, ARC-only) off the import path unless a
            # Hessian is actually being built. When ARC isn't installed these
            # raise OptionalArcUnavailable (an ImportError), caught by the
            # surrounding ``except Exception`` → the Hessian is omitted.
            from tckdb_arc._arc_optional import determine_ess, ess_factory

            ess_name = determine_ess(str(resolved), raise_error=False)
            source = _HESSIAN_SOURCE_BY_ESS.get(ess_name) if ess_name else None
            if source is None:
                return None
            ess_adapter = ess_factory(str(resolved), ess_name)
            parse = getattr(ess_adapter, "parse_cartesian_hessian_lower_triangle", None)
            if parse is None:
                return None
            triangle = parse()
            if not triangle:
                return None
            parse_frame = getattr(ess_adapter, "parse_cartesian_hessian_geometry", None)
            frame_xyz, frame = parse_frame() if parse_frame else (None, None)
            expected_frame = {"gaussian": "gaussian_input_orientation", "orca": "orca_hess_atoms"}
            if frame_xyz is None or frame != expected_frame.get(ess_name):
                logger.warning(
                    "TCKDB bundle: Hessian omitted for label=%s: matching Cartesian frame geometry unavailable",
                    species_record.get("label"),
                )
                return None
            from tckdb_arc._vendor import xyz_to_str

            geometry_xyz_text = _normalize_xyz_text(xyz_to_str(frame_xyz), species_record.get("label"))
            if geometry_xyz_text is None:
                return None
            dimension = 3 * int(geometry_xyz_text.splitlines()[0])
            if len(triangle) != dimension * (dimension + 1) // 2:
                logger.warning("TCKDB bundle: Hessian omitted: matrix dimension disagrees with frame geometry")
                return None
        except Exception as exc:  # noqa: BLE001 — Hessian is best-effort only.
            logger.debug(
                "TCKDB bundle: Hessian parse skipped for label=%s (%s)",
                species_record.get("label"), exc,
            )
            return None
        if not triangle:
            return None
        return {
            "geometry": {"xyz_text": geometry_xyz_text},
            "lower_triangle_hartree_bohr2": triangle,
            "source": source,
            "parser_version": _HESSIAN_PARSER_VERSION,
        }

    def _parse_irc_trajectories(
        self,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Parse each IRC log into a trajectory dict for payload assembly.

        Returns one dict per parsed log:
            ``{
                "direction": "forward"|"reverse"|None,
                "rich_points": [<rich-point dict>, ...] | None,
                "geom_points": [<xyz_dict>, ...] | None,
            }``

        Each log is first attempted with the rich parser
        (:func:`arc.parser.parser.parse_irc_path`) which carries energies,
        gradients, reaction coordinates, and per-point direction labels.
        On rich-parser failure (or for ESS backends that haven't
        implemented it), the geometry-only :func:`parse_irc_traj` runs
        as a fallback so the upload retains the geometry-only IRC payload
        behavior described in the task spec.

        Direction-resolution order (used when a per-point direction
        isn't present in the rich data):
        1. Rich parser's per-point ``direction`` (Gaussian's
           ``FORWARD/REVERSE path direction.`` announcement).
        2. ``ts_record['irc_log_directions'][i]`` — the authoritative
           value the scheduler captured from ``job.irc_direction`` and
           ``arc/output.py`` paired with ``irc_logs``. Production ARC
           IRC log filenames are just ``output.log`` inside an
           ``irc_<server_id>`` folder, so filename detection alone
           always returns None on real runs.
        3. ``_detect_irc_direction(filename)`` — back-compat fallback
           for output.yml files written before the paired-list
           tracking, and for test fixtures that hand-craft directional
           filenames.
        4. ``None`` — last resort. The trajectory is still emitted;
           per-point direction is omitted (the schema allows nullable
           ``IRCDirection`` on each point).

        Returns ``None`` when no logs resolve, no logs exist on disk, or
        every parse attempt failed. The caller uses ``None`` as the
        signal to omit ``irc_result`` (partial-data fallback per spec).
        """
        lookup = self._evidence.lookup(
            output_doc, "transition_state", str(ts_record.get("label") or ""), "irc"
        )
        if lookup.state == "available":
            return [
                {
                    "direction": trajectory.get("declared_direction"),
                    "rich_points": [
                        {
                            "point_number": point["source_point_index"],
                            "direction": point.get("direction"),
                            "geometry_xyz_text": point["geometry_xyz_text"],
                            "electronic_energy_hartree": point.get("electronic_energy_hartree"),
                            "reaction_coordinate": point.get("reaction_coordinate_sqrt_amu_bohr"),
                            "max_gradient": point.get("max_gradient_hartree_per_bohr"),
                            "rms_gradient": point.get("rms_gradient_hartree_per_bohr"),
                        }
                        for point in trajectory["points"]
                    ],
                    "geom_points": None,
                }
                for trajectory in (lookup.value or {})["trajectories"]
            ]
        if lookup.state == "unavailable":
            return None
        log_paths = ts_record.get("irc_logs") or []
        if not log_paths:
            return None
        # Lazy import via the optional-ARC boundary: parser imports drag in
        # heavyweight ESS adapters (ARC-only). These wrappers import cleanly and
        # only raise OptionalArcUnavailable at *call* time, where the existing
        # per-log ``try/except`` degrades to the geometry-only / omitted path.
        from tckdb_arc._arc_optional import parse_irc_path, parse_irc_traj

        log_directions = list(ts_record.get("irc_log_directions") or [])
        trajectories: list[dict[str, Any]] = []
        for i, log_path in enumerate(log_paths):
            resolved = self._resolve_local_path(log_path)
            if resolved is None or not resolved.is_file():
                logger.debug(
                    "TCKDB computed-reaction: IRC log path not found on disk: %r",
                    log_path,
                )
                continue
            # Resolve the trajectory-level direction once: scheduler-
            # tracked first, filename heuristic second, None last. This
            # is used as a per-point fallback when the rich parser
            # doesn't carry FORWARD/REVERSE labels (currently only
            # Gaussian does).
            direction = log_directions[i] if i < len(log_directions) else None
            if direction not in (_IRC_DIRECTION_FORWARD, _IRC_DIRECTION_REVERSE):
                direction = _detect_irc_direction(str(log_path))
            rich_points: list[dict[str, Any]] | None = None
            try:
                rich_points = parse_irc_path(log_file_path=str(resolved))
            except Exception as exc:
                logger.debug(
                    "TCKDB computed-reaction: parse_irc_path failed for %s: %s",
                    resolved, exc,
                )
            geom_points: list[dict[str, Any]] | None = None
            if not rich_points:
                # Geometry-only fallback so non-Gaussian logs (and
                # malformed Gaussian logs) keep producing a usable IRC
                # payload, matching the pre-rich-parser behavior.
                try:
                    geom_points = parse_irc_traj(log_file_path=str(resolved))
                except Exception as exc:
                    logger.debug(
                        "TCKDB computed-reaction: parse_irc_traj failed for %s: %s",
                        resolved, exc,
                    )
                    continue
                if not geom_points:
                    continue
            trajectories.append({
                "direction": direction,
                "rich_points": rich_points,
                "geom_points": geom_points,
            })
        return trajectories or None

    # ------------------------------------------------------------------
    # Computed-reaction bundle path (POST /uploads/computed-reaction)
    # ------------------------------------------------------------------

    def submit_computed_reaction_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
        is_partial: bool = False,
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one computed-reaction bundle.

        Walks the reaction's ``reactant_labels``/``product_labels`` against
        ``output_doc['species']`` (and ``output_doc['transition_states']``
        for ``ts_label``) and assembles a self-contained
        ``ComputedReactionUploadRequest`` covering species, TS, and
        modified-Arrhenius kinetics — all cross-referenced by local
        string keys, no DB ids.

        ``is_partial=True`` marks the bundle as a deliberately
        incomplete record (phase-1 partial sidecar for a reaction whose
        TS search failed). The caller is responsible for stripping
        ``ts_label`` and ``kinetics`` from ``reaction_record`` before
        calling — the adapter does not edit the input. Partial bundles
        are sidecar-only in phase-1: the network POST is skipped
        regardless of ``config.upload``, the payload + sidecar land on
        disk with a ``.partial`` filename infix and ``is_partial=true``
        metadata, and the returned :class:`UploadOutcome` has
        ``status="skipped"``.

        Returns ``None`` if the adapter is disabled. Build failures (e.g.
        missing reactant species, missing opt level) raise; the caller is
        responsible for wrapping the per-reaction call in a try/except so
        one bad reaction doesn't take down the rest of the run.
        """
        if not self._config.enabled:
            return None

        reaction_label = reaction_record.get("label") or "unlabeled"
        project_label = self._config.project_label or output_doc.get("project")

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        output_doc = _withhold_unreliable_energies_in_doc(output_doc, build_warnings)
        reaction_record = _withhold_reaction_kinetics(output_doc, reaction_record, build_warnings)
        payload = self._build_computed_reaction_payload(
            output_doc=output_doc,
            reaction_record=reaction_record,
            warnings=build_warnings,
        )
        self._report_adaptive_omissions(output_doc, build_warnings)

        idempotency_inputs = IdempotencyInputs.from_payload(
            project_label=project_label,
            species_label=reaction_label,
            # The reaction has no conformer concept at the bundle level —
            # just one fit per upload — so the conformer slot in the key
            # carries the TS label (or "noTS"). This keeps the key shape
            # consistent with the species path while still uniquely
            # identifying the reaction within a project.
            conformer_label=str(reaction_record.get("ts_label") or "noTS"),
            payload_kind=COMPUTED_REACTION_KIND,
            payload=payload,
        )
        idempotency_key = build_idempotency_key(idempotency_inputs)

        written = self._writer.write(
            label=reaction_label,
            payload=payload,
            endpoint=COMPUTED_REACTION_ENDPOINT,
            idempotency_key=idempotency_key,
            payload_kind=COMPUTED_REACTION_KIND,
            base_url=self._config.base_url,
            subdir=PayloadWriter.COMPUTED_REACTION_SUBDIR,
            is_partial=is_partial,
            warnings=build_warnings,
        )
        if is_partial:
            # Phase-1 policy: partial reaction sidecars never POST. The
            # server has not been verified to accept transition_state=null
            # bundles, and partial-record semantics on the server side
            # are out of scope until a follow-up. Sidecar lands on disk
            # with the .partial infix; status reuses "skipped" because
            # is_partial=true on the sidecar already disambiguates this
            # from an upload=false skip.
            logger.info(
                "TCKDB partial computed-reaction sidecar written; "
                "live upload skipped: %s (key=%s)",
                written.payload_path,
                idempotency_key,
            )
            return self._finalize_skipped(written)

        logger.info(
            "TCKDB computed-reaction payload written: %s (key=%s)",
            written.payload_path,
            idempotency_key,
        )

        if not self._config.upload:
            return self._finalize_skipped(written)

        return self._upload(written, payload, endpoint=COMPUTED_REACTION_ENDPOINT)

    def _build_computed_reaction_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ``ComputedReactionUploadRequest`` dict.

        Resolves reactant/product/TS records from ``output_doc``,
        delegates per-actor block construction (species + TS) to the
        shared per-actor helpers, and stitches in a single
        modified-Arrhenius kinetics fit when ARC produced one.
        Producer-side omissions are appended to ``warnings``.
        """
        species_index = _index_species(output_doc)
        ts_index = _index_transition_states(output_doc)
        reaction_record = _with_stated_participants(
            reaction_record,
            ts_record=ts_index.get(reaction_record.get("ts_label")),
            species_index=species_index,
            warnings=warnings,
        )

        reactant_labels = list(reaction_record.get("reactant_labels") or [])
        product_labels = list(reaction_record.get("product_labels") or [])
        if not reactant_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no reactant_labels."
            )
        if not product_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no product_labels."
            )

        # Build reactant/product species blocks under namespaced keys.
        species_blocks: list[dict[str, Any]] = []
        reactant_keys: list[str] = []
        product_keys: list[str] = []
        # Per-actor calc-role → bundle-key map; consumed by the kinetics
        # builder so source_calculations references resolve to the same
        # local keys the species blocks declared.
        actor_calc_keys: dict[str, dict[str, str]] = {}

        # One species block per distinct ARC species label. A species that
        # appears on both sides (degenerate H2 + H <=> H + H2) or twice on
        # one side (H + H <=> H2) is declared once and *referenced* from
        # every slot: ComputedReactionUploadRequest allows the same key in
        # reactant_keys / product_keys (the schema's own example is
        # ``reactant_keys: ["h", "h"]``) and the server resolves
        # participants by position. Re-declaring it would deposit the
        # species' calculations, conformer observation and thermo twice.
        # The first slot to see a label names the block (r0_H2), so
        # non-degenerate reactions keep their existing keys.
        key_by_label: dict[str, str] = {}
        for side, prefix, labels, keys_out in (
            ("reactant", "r", reactant_labels, reactant_keys),
            ("product", "p", product_labels, product_keys),
        ):
            for i, label in enumerate(labels):
                existing_key = key_by_label.get(label)
                if existing_key is not None:
                    keys_out.append(existing_key)
                    continue
                actor_key = _local_key_for_actor(prefix, i, label)
                calc_prefix = _calc_prefix_for_actor(prefix, i)
                record = species_index.get(label)
                if record is None:
                    raise ValueError(
                        f"reaction {reaction_record.get('label')!r}: {side} "
                        f"label {label!r} not found in output_doc.species."
                    )
                block, calc_keys = self._build_reaction_species_block(
                    output_doc=output_doc,
                    species_record=record,
                    actor_key=actor_key,
                    calc_prefix=calc_prefix,
                    warnings=warnings,
                )
                species_blocks.append(block)
                keys_out.append(actor_key)
                key_by_label[label] = actor_key
                actor_calc_keys[actor_key] = calc_keys

        # TS block (inline). Optional — a reaction with no TS still
        # carries kinetics but server-side it's a thinner record.
        ts_label = reaction_record.get("ts_label")
        ts_block: dict[str, Any] | None = None
        ts_calc_keys: dict[str, str] = {}
        atom_map_block: dict[str, Any] | None = None
        if ts_label:
            ts_record = ts_index.get(ts_label)
            if ts_record is None:
                raise ValueError(
                    f"reaction {reaction_record.get('label')!r}: ts_label "
                    f"{ts_label!r} not found in output_doc.transition_states."
                )
            ts_block, ts_calc_keys = self._build_ts_block(
                output_doc=output_doc,
                ts_record=ts_record,
                ts_label=ts_label,
                reaction_multiplicity=reaction_record.get("multiplicity"),
                unmapped_smiles=_ts_unmapped_smiles_handle(
                    ts_record=ts_record,
                    reaction_record=reaction_record,
                    species_index=species_index,
                ),
                warnings=warnings,
                reaction_record=reaction_record,
                species_index=species_index,
            )
            ts_geometry = ts_block.get("geometry")
            if isinstance(ts_geometry, Mapping) and ts_geometry.get("key"):
                slots = []
                for side, keys, labels in (("reactant", reactant_keys, reactant_labels),
                                           ("product", product_keys, product_labels)):
                    for position, (species_key, label) in enumerate(zip(keys, labels), start=1):
                        block = next(b for b in species_blocks if b["key"] == species_key)
                        slots.append({"side": side, "index": position, "species_key": species_key,
                                      "label": label, "geometry": block["conformers"][0].get("geometry")})
                atom_map_block = _tckdb_reaction_atom_map(
                    reaction_record, ts_label=ts_label, ts_xyz_text=ts_geometry.get("xyz_text"),
                    ts_geometry_key=ts_geometry["key"], participants=slots,
                    irc_evidence=ts_block.get("validation_evidence") or (), warnings=warnings)
            else:
                _warn_atom_map_not_sent(reaction_record, ts_label=ts_label, warnings=warnings)
            # ARC's electronic-energy ordering verdict (``ts_checks['e_elect']``) needs every
            # participant's own sp calculation, which only the bundle carries: the standalone
            # TS upload refuses ``energy_ordering``, so it is built here and not in the TS block.
            ordering = _ts_energy_ordering_validation_evidence(
                ts_record,
                ts_sp_key=ts_calc_keys.get(_CALC_KEY_SP),
                reactant_labels=reactant_labels,
                product_labels=product_labels,
                reactant_keys=reactant_keys,
                product_keys=product_keys,
                actor_calc_keys=actor_calc_keys,
                species_index=species_index,
                ts_label=ts_label,
                warnings=warnings,
            )
            if ordering:
                ts_block["validation_evidence"] = [*ts_block.get("validation_evidence", []), *ordering]

        # Kinetics. ARC produces at most one fit per reaction today.
        kinetics_payload = reaction_record.get("kinetics")
        kinetics_blocks: list[dict[str, Any]] = []
        if isinstance(kinetics_payload, Mapping):
            kinetics_block = _build_kinetics_block(
                kinetics_record=kinetics_payload,
                reactant_keys=reactant_keys,
                product_keys=product_keys,
                actor_calc_keys=actor_calc_keys,
                ts_calc_keys=ts_calc_keys,
                long_kinetic_description=reaction_record.get(
                    "long_kinetic_description"
                ),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc),
            )
            if kinetics_block is not None:
                kinetics_blocks.append(kinetics_block)

        bundle: dict[str, Any] = {
            "species": species_blocks,
            "reactant_keys": reactant_keys,
            "product_keys": product_keys,
        }
        # Output 1.3 states ``reversible`` per reaction (arc/output.py
        # ``_get_reversible``: True for ``<=>``, False for ``=>``, null for any
        # other arrow), and a stated bool is sent as stated. Otherwise (before
        # 1.3, or null) it is omitted, so the schema's default (True) applies.
        if (_is_output_schema_1_3_or_later(output_doc)
                and isinstance(reaction_record.get("reversible"), bool)):
            bundle["reversible"] = reaction_record["reversible"]
        if ts_block is not None:
            bundle["transition_state"] = ts_block
        if atom_map_block is not None:
            # Participants are the slots of reactant_keys / product_keys; each counts into the
            # conformer geometry of its species block, and the map names the TS geometry.
            bundle["atom_map"] = atom_map_block
        if kinetics_blocks:
            bundle["kinetics"] = kinetics_blocks

        # Final pass: flatten every calc's wrapped result into the
        # network_pdep flat fields the computed-reaction endpoint
        # expects. Keeping this as a single end-of-build walker means
        # any new calc-emit site automatically gets the right shape.
        _flatten_all_reaction_calcs(bundle)

        family = reaction_record.get("family")
        if family:
            bundle["reaction_family"] = str(family)
            # The server validates against a canonical-family list and
            # demands a source_note when the supplied name is unknown.
            # We don't have that list at the producer, so always tag
            # the source — a no-op for canonical names, a safety net
            # for non-canonical ones.
            bundle["reaction_family_source_note"] = "ARC-reported family"

        arc_version = output_doc.get("arc_version")
        arc_git_commit = output_doc.get("arc_git_commit")
        if arc_version or arc_git_commit:
            wt: dict[str, Any] = {"name": "ARC"}
            if arc_version:
                wt["version"] = str(arc_version)
            if arc_git_commit:
                wt["git_commit"] = str(arc_git_commit)
            bundle["workflow_tool_release"] = wt

        analysis_release = _arc_analysis_software_release(output_doc)
        if analysis_release is not None:
            bundle["analysis_software_release"] = analysis_release

        return bundle

    def _build_reaction_species_block(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        actor_key: str,
        calc_prefix: str,
        warnings: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build one ``BundleSpeciesIn`` dict + a calc-role → bundle-key map.

        ``actor_key`` (e.g. ``"r0_CHO"``) becomes the species block's
        ``key`` and the geometry/conformer key tails. ``calc_prefix``
        (e.g. ``"r0"``) is the namespace for calculation keys —
        deliberately shorter than ``actor_key`` so source_calculations
        references stay compact.

        The returned map (e.g. ``{"opt": "r0_opt", "freq": "r0_freq",
        "sp": "r0_sp"}``) is what the kinetics builder uses to wire
        ``source_calculations`` back to the freshly-minted local keys.
        It only contains the roles whose calculation actually made it
        into the bundle.
        """
        _warn_primary_opt_placeholder(
            species_record, warnings,
            field=f"species[{actor_key}].conformers[0].calculation")
        conformer_xyz_text = _require_xyz_text(species_record)
        opt_key = f"{calc_prefix}_{_CALC_KEY_OPT}"
        opt_coarse_key = f"{calc_prefix}_{_CALC_KEY_OPT_COARSE}"
        freq_key = f"{calc_prefix}_{_CALC_KEY_FREQ}"
        sp_key = f"{calc_prefix}_{_CALC_KEY_SP}"
        geom_key = f"{actor_key}_geom"
        conf_key = f"{actor_key}_conf0"
        monatomic = _is_single_atom_geometry(conformer_xyz_text)

        # Coarse-opt provenance, parallel to the computed-species path.
        # Same gate (both ``coarse_opt_log`` and ``coarse_opt_output_xyz``
        # must be present) — when only one resolves, we fall back to
        # single-stage rather than emit a half-described opt_coarse.
        # Namespacing the calc key with ``calc_prefix`` keeps it globally
        # unique across reactant/product/TS, which the bundle's
        # ``validate_unique_keys`` requires.
        opt_coarse_calc = self._build_opt_coarse_calc(
            output_doc=output_doc,
            species_record=species_record,
            calc_key=opt_coarse_key,
        )
        fine_opt_depends_on: list[Mapping[str, Any]] | None = None
        if opt_coarse_calc is not None:
            fine_opt_depends_on = [
                {"parent_calculation_key": opt_coarse_key,
                 "role": "optimized_from"}
            ]

        if monatomic:
            # An atom's primary is its own single point (tckdb-schemas 0.59,
            # TCKDB#610): no opt, coarse opt, freq or rotor scan exists to carry.
            primary_calc = self._build_monatomic_primary_sp(
                output_doc=output_doc, species_record=species_record,
                calc_key=sp_key, conformer_xyz_text=conformer_xyz_text)
            opt_coarse_calc = None
            fine_opt_depends_on = None
            calc_keys: dict[str, str] = {_CALC_KEY_SP: sp_key}
        else:
            primary_calc = self._build_calc_in_bundle(
                output_doc=output_doc,
                species_record=species_record,
                calc_key=opt_key,
                calc_role=_CALC_KEY_OPT,
                calc_type="opt",
                level_kind="opt",
                ess_job_key="opt",
                result_field="opt_result",
                result_payload=_opt_result_payload(species_record),
                depends_on=fine_opt_depends_on,
                tckdb_origin=None,
                conformer_xyz_text=conformer_xyz_text,
            )
            calc_keys = {_CALC_KEY_OPT: opt_key}
        additional: list[dict[str, Any]] = []
        # opt_coarse is type=opt, so the schema validator at
        # computed_reaction_upload.py:446 exempts it from needing
        # geometry_key — leave it bare.
        if opt_coarse_calc is not None:
            # Anchor to the conformer observation without claiming the coarse
            # geometry is the final conformer geometry.
            opt_coarse_calc["conformer_key"] = conf_key
            additional.append(opt_coarse_calc)
            calc_keys[_CALC_KEY_OPT_COARSE] = opt_coarse_key

        freq_result = _freq_result_payload(species_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None and not monatomic:
            try:
                freq_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=freq_key,
                    calc_role=_CALC_KEY_FREQ,
                    calc_type="freq",
                    level_kind="freq",
                    ess_job_key="freq",
                    result_field="freq_result",
                    result_payload=freq_result,
                    depends_on=[{"parent_calculation_key": opt_key, "role": "freq_on"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                )
                # Server requires non-opt species calcs to reference a
                # conformer geometry by key (BundleSpeciesIn validator
                # validate_calc_geometry_keys).
                freq_calc["geometry_key"] = geom_key
                additional.append(freq_calc)
                calc_keys[_CALC_KEY_FREQ] = freq_key
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-reaction: %s freq calculation skipped: %s",
                    actor_key, exc,
                )

        sp_result = _sp_result_payload(species_record)
        if sp_result is not None and not monatomic:
            try:
                sp_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=sp_key,
                    calc_role=_CALC_KEY_SP,
                    calc_type="sp",
                    level_kind="sp",
                    ess_job_key="sp",
                    result_field="sp_result",
                    result_payload=sp_result,
                    depends_on=[{"parent_calculation_key": opt_key, "role": "single_point_on"}],
                    tckdb_origin=(
                        _sp_reuse_origin(output_doc, species_record)
                    ),
                    conformer_xyz_text=conformer_xyz_text,
                )
                sp_calc["geometry_key"] = geom_key
                additional.append(sp_calc)
                calc_keys[_CALC_KEY_SP] = sp_key
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-reaction: %s sp calculation skipped: %s",
                    actor_key, exc,
                )

        # Rotor scans. Mirrors ``_build_conformer_block``'s scan loop,
        # with two reaction-path-specific deltas:
        #
        #  - Calc keys are namespaced (``r0_scan_rotor_0``, etc.). The
        #    bundle schema's ``validate_unique_keys`` requires *globally*
        #    unique calc keys across all species + the TS, so two
        #    reactants both reporting ``scan_rotor_0`` would collide.
        #    The torsion's ``source_scan_calculation_key`` gets rewritten
        #    in lockstep via ``scan_key_renames`` so the reference still
        #    resolves.
        #  - ``geometry_key`` is set: non-opt species calcs in reaction
        #    bundles must point at the conformer geometry (same reason
        #    freq/sp set it above).
        scan_key_renames: dict[str, str] = {}
        unbuilt_scans: dict[str, str] = {}
        for scan_entry in ([] if monatomic else _scan_entries_from_record(species_record)):
            if not isinstance(scan_entry, Mapping):
                continue
            if scan_entry.get("type") != _CALC_KEY_SCAN:
                continue
            original_scan_key = scan_entry.get("key")
            scan_result = scan_entry.get("scan_result")
            if not isinstance(original_scan_key, str) or not original_scan_key:
                continue
            if not isinstance(scan_result, Mapping):
                continue
            namespaced_scan_key = f"{calc_prefix}_{original_scan_key}"
            try:
                scan_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=namespaced_scan_key,
                    calc_role=_CALC_KEY_SCAN,
                    calc_type=_CALC_KEY_SCAN,
                    level_kind="scan",
                    ess_job_key="scan",
                    result_field="scan_result",
                    result_payload=scan_result,
                    depends_on=[{"parent_calculation_key": opt_key,
                                 "role": "scan_parent"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    source_constraints=scan_entry.get("constraints"),
                    output_log_path=scan_entry.get("source_log"),
                    observed_software=scan_entry.get("ess_software"),
                    observed_version=scan_entry.get("ess_version"),
                    level_job_type=_scan_job_type(
                        output_doc, species_record, original_scan_key),
                )
                scan_calc["geometry_key"] = geom_key
                additional.append(scan_calc)
                scan_key_renames[original_scan_key] = namespaced_scan_key
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-reaction: %s scan calculation %s skipped: %s",
                    actor_key, original_scan_key, exc,
                )
                unbuilt_scans[original_scan_key] = str(exc)

        species_block: dict[str, Any] = {
            "key": actor_key,
            "species_entry": self._species_entry_payload(species_record),
            "conformers": [
                {
                    "key": conf_key,
                    "geometry": _geometry_payload(
                        species_record, conformer_xyz_text, species_record.get("xyz_isotopes"),
                        key=geom_key),
                    "calculation": primary_calc,
                }
            ],
            "calculations": additional,
        }
        # ``ConformerIn.label`` (computed-reaction) is the same optional
        # conformer label the computed-species route already sends.
        conformer_label = species_record.get("label")
        if conformer_label:
            species_block["conformers"][0]["label"] = str(conformer_label)[:64]
        energy_level = (
            _thermo_energy_level(output_doc, species_record)
            if isinstance(species_record.get("thermo"), Mapping) else None
        )
        # See the computed-species route: declared from the linked sp's own level.
        energy_declaration = _energy_level_declaration(
            _thermo_energy_level(output_doc, species_record),
            calc_keys_by_role=calc_keys,
            calculations=_calculations_by_key(primary_calc, additional),
        )
        thermo_block = _build_thermo_block(
            species_record.get("thermo"),
            # The current reaction root accepts species-scoped thermo provenance.
            calc_keys_by_role=_with_composite_role(species_record, calc_keys),
            target_model="BundleThermoIn",
            warnings=warnings,
            warning_field=f"species[{actor_key}].thermo",
            energy_level=energy_level,
            header_corrections_level=output_doc.get("arkane_level_of_theory"),
            element_symbols=_species_element_symbols(species_record),
            energy_level_unattributable=_energy_level_unattributable(output_doc, energy_level),
            energy_declaration=energy_declaration,
            legacy_cp_data=_is_output_schema_1_0(output_doc),
        )
        if thermo_block is not None:
            species_block["thermo"] = thermo_block

        # Per-species AEC/BAC corrections target this species's resolved
        # species_entry. Anchor each correction to this species's own SP
        # calc (e.g. r0_sp / p1_sp); never to ts_sp or to a sibling
        # species's SP key — the server enforces ownership in
        # ``_persist_species_applied_corrections`` and would 422 on a
        # cross-species reference.
        omitted_bacs: list[str] = []
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                species_record, legacy_1_0=_is_output_schema_1_0(output_doc),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=calc_keys.get(_CALC_KEY_SP),
            warnings=warnings,
            warning_field=f"species[{actor_key}].applied_energy_corrections",
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, species_record),
            omitted_bac_reasons=omitted_bacs,
        )
        _note_omitted_bac_on_thermo(
            species_block.get("thermo"), omitted_bacs,
            _bond_corrections_flag(species_record))
        _warn_bac_type_not_stated(output_doc, species_record, warnings)
        if applied_corrections:
            species_block["applied_energy_corrections"] = applied_corrections

        # Statmech block: carries frequency-scale-factor provenance plus
        # the schema-supported base statmech fields for this reactant/
        # product (external_symmetry, is_linear, rigid_rotor_kind,
        # statmech_treatment, point_group, slim torsions, and
        # source_calculations referencing this species's own opt/freq/
        # sp calcs). This bundle's root is ComputedReactionUploadRequest,
        # whose per-species statmech field is ``BundleStatmechIn`` — it
        # currently accepts the same field set this builder can produce
        # as ``StatmechInBundle`` (the computed-species root) does (see
        # ``_STATMECH_FIELDS_BY_TARGET``), but that parity is incidental
        # rather than guaranteed, so ``target_model`` is passed
        # explicitly below rather than assumed. The only mode-specific
        # input is the calc-key namespace — we pass the *species-scoped*
        # ``calc_keys`` (e.g. ``{"opt": "r0_opt", ...}``) so the
        # server-side ownership check sees only this species's own
        # calculations. Sibling species and the TS use disjoint
        # namespaces (``r1_*``/``p0_*``/``ts_*``) and therefore can't
        # leak in here. The helper returns ``None`` (and we skip the
        # whole statmech block) when nothing useful resolves, keeping
        # payloads backward-compatible with FSF-less runs.
        species_statmech = _build_statmech_block_for_species(
            output_doc=output_doc,
            species_record=species_record,
            calc_keys_by_role=_with_composite_role(species_record, calc_keys),
            workflow_tool_release=_arc_workflow_tool_release(output_doc),
            target_model="BundleStatmechIn",
            scan_key_renames=scan_key_renames or None,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            unbuilt_scans=unbuilt_scans,
            warnings=warnings,
            warning_field=f"species[{actor_key}].statmech",
            energy_declaration=energy_declaration,
        )
        if species_statmech is not None:
            species_block["statmech"] = species_statmech

        return species_block, calc_keys

    def _build_ts_block(
        self,
        *,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
        ts_label: str,
        reaction_multiplicity: int | None,
        unmapped_smiles: str | None = None,
        include_artifacts: bool = True,
        warnings: list[dict[str, Any]] | None = None,
        reaction_record: Mapping[str, Any] | None = None,
        species_index: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build one ``BundleTransitionStateIn`` dict + a calc-role → key map.

        ARC stores TS records in ``output_doc['transition_states']`` with
        the same shape as species records (the TS is just a stationary
        point with ``is_ts: true``); the multiplicity falls back to the
        reaction's multiplicity when the TS record doesn't carry one.

        ``unmapped_smiles`` is a deterministic textual handle for the
        TS, computed by :func:`_ts_unmapped_smiles_handle` at the
        caller. ``None`` is the common case (TS has no Lewis structure
        and no upstream textual identifier was derivable); the field is
        omitted from the payload, leaving the server to store NULL.
        The producer never fabricates a normal-molecule SMILES from
        the TS geometry — the ``mol`` field stays absent regardless.

        IRC provenance: when ARC has ``irc_logs`` populated, emit a
        ``ts_irc`` calc with ``depends_on(role=irc_start)`` pointing at
        ``ts_opt`` — IRC is seeded from the optimized TS saddle, so
        ``ts_opt`` is its primary geometry-producing parent (the TS freq
        validates the saddle but isn't the geometry source). When the
        IRC log files parse cleanly, attach a structured ``irc_result``
        with forward/reverse points and producer-declared
        ``output_geometries`` (``irc_forward``/``irc_reverse`` endpoints).
        Forward/reverse are ESS path-direction labels — the producer
        does NOT infer reactant/product side from them.
        """
        conformer_xyz_text = _require_xyz_text(ts_record)
        if arc13.primary_opt_placeholder(ts_record) is not None:
            _warn_primary_opt_placeholder(
                ts_record, warnings, field="transition_state.calculation")
        ts_opt_key = f"ts_{_CALC_KEY_OPT}"
        # ``_CALC_KEY_TS_GUESS`` already starts with ``ts_``; using it
        # bare keeps the bundle key as ``ts_guess`` (don't double-prefix).
        ts_guess_key = _CALC_KEY_TS_GUESS
        ts_freq_key = f"ts_{_CALC_KEY_FREQ}"
        ts_sp_key = f"ts_{_CALC_KEY_SP}"
        ts_irc_key = f"ts_{_CALC_KEY_IRC}"
        ts_geom_key = "ts_geom"

        # Path-search ts_guess provenance. Emit a parent calculation
        # only when the chosen TS guess is itself a real path-search
        # calculation (orca_neb → method=neb, xtb_gsm → method=gsm) AND
        # the producer recorded the corresponding log path
        # (``neb_log`` / ``gsm_log``). Heuristics / AutoTST / KinBot /
        # GCN / user-supplied guesses stay geometry-only on the
        # ts_opt: ``calculation_dependency`` requires a real parent
        # calculation, never a geometry. The artifact path picks up
        # the log via ``_resolve_log_field`` when artifact upload is
        # enabled.
        ts_guess_calc: dict[str, Any] | None = None
        ts_guess_method = _resolve_ts_guess_path_search_for_record(ts_record)
        ts_guess_log_field = (
            _TS_GUESS_LOG_FIELD_BY_METHOD.get(ts_guess_method)
            if ts_guess_method else None
        )
        # The path-search calculation's level. ARC exports ``neb_level`` (the
        # ORCA NEB level, arc/output.py) and ``ess_software.neb`` /
        # ``ess_versions.neb`` for NEB, and nothing for GSM (xtb-gsm has no
        # level export). TCKDB requires a level on every calculation, so a
        # guess whose level ARC does not state is not filed at all: never at
        # ``opt_level``, which is not what the path search ran at.
        ts_guess_level: Mapping[str, Any] | None = None
        ts_guess_level_kind: str | None = None
        if (
            ts_guess_method
            and ts_guess_log_field
            and ts_record.get(ts_guess_log_field)
        ):
            # ``neb_level`` is ORCA's (orca_neb_settings) but ARC's Level
            # deduces a software from the method alone (wb97xd/def2tzvp gives
            # gaussian), so ``neb_level.software`` says nothing about what
            # ran. The program is the one observed on the NEB log
            # (``ess_software.neb``), or none: then nothing is filed. Output.yml
            # 1.3 states the GSM level too (``gsm_level``: GFN2-xTB, only when
            # every archived xtb node output shows it) with the program observed
            # on those outputs (``ess_software.gsm``); older output exports none
            # (``output_doc`` has no ``gsm_level``) and the GSM is not filed.
            path_search_level = (
                _resolve_level(output_doc, ts_guess_method)
                if ts_guess_method in ("neb", "gsm") else None)
            observed = ts_record.get("ess_software")
            if path_search_level is not None and not (
                    isinstance(observed, Mapping) and observed.get(ts_guess_method)):
                _warn_ts_guess_level_not_stated(
                    warnings, ts_label=ts_label, method=ts_guess_method,
                    reason=f"ARC recorded no ess_software.{ts_guess_method} for the "
                           f"{ts_guess_method.upper()} log",
                    software=True)
            elif path_search_level is None:
                _warn_ts_guess_level_not_stated(
                    warnings, ts_label=ts_label, method=ts_guess_method,
                    schema_states_level=("gsm_level" in output_doc if ts_guess_method == "gsm" else None))
            else:
                ts_guess_level = path_search_level
                ts_guess_level_kind = ts_guess_method
        if ts_guess_level is not None and ts_guess_level_kind is not None:
            # Resolve the on-disk log path (neb_log is stored run-relative on
            # the record). NEB has no image parser, so the payload is the
            # single TS-guess point from ``opt_input_xyz``.
            log_local = self._resolve_local_path(
                ts_record.get(ts_guess_log_field),
            )
            path_search_payload = _build_path_search_result_payload(
                method=ts_guess_method,
                log_path=str(log_local) if log_local is not None else None,
                fallback_xyz_text=ts_record.get("opt_input_xyz"),
            )
            if path_search_payload is None:
                logger.warning(
                    "TCKDB computed-reaction: ts_guess (%s) calc skipped — "
                    "could not build path_search_result.points (no parseable "
                    "log and no fallback xyz).",
                    ts_guess_method,
                )
            else:
                try:
                    ts_guess_calc = self._build_calc_in_bundle(
                        output_doc=output_doc,
                        species_record=ts_record,
                        calc_key=ts_guess_key,
                        calc_role=_CALC_KEY_TS_GUESS,
                        calc_type="path_search",
                        level_kind=ts_guess_level_kind,
                        ess_job_key=ts_guess_level_kind,
                        result_field="path_search_result",
                        result_payload=path_search_payload,
                        depends_on=None,
                        tckdb_origin=None,
                        conformer_xyz_text=None,
                        include_artifacts=include_artifacts,
                    )
                except ValueError as exc:
                    logger.warning(
                        "TCKDB computed-reaction: ts_guess (%s) calc skipped: %s",
                        ts_guess_method, exc,
                    )
                    _warn_ts_guess_level_not_stated(
                        warnings, ts_label=ts_label, method=ts_guess_method,
                        reason=str(exc))

        ts_opt_depends_on: list[Mapping[str, Any]] | None = None
        if ts_guess_calc is not None:
            ts_opt_depends_on = [
                {"parent_calculation_key": ts_guess_key,
                 "role": "optimized_from"}
            ]

        # ARC's selected TS-guess method (heuristics, AutoTST, KinBot,
        # GCN, user, …) is workflow narrative — TCKDB has no
        # schema-reviewed slot for "what generated the guess that fed
        # ts_opt". The optimized TS itself (geometry, freq imag mode,
        # IRC connectivity to reactant/product) is the scientific
        # evidence the database stores; the upstream guess source is
        # ARC-internal context that lives in ARC's artifacts. Do not
        # re-introduce ``selected_ts_guess`` as a ``tckdb_origin``
        # marker on ts_opt unless TCKDB grows a dedicated field.
        # NEB/GSM is a separate case: ``path_search_result`` ships
        # actual scientific path-search data (per-image energies,
        # geometries) on the ts_guess calc, accepted by the schema.
        primary_calc = self._build_calc_in_bundle(
            output_doc=output_doc,
            species_record=ts_record,
            calc_key=ts_opt_key,
            calc_role=_CALC_KEY_OPT,
            calc_type="opt",
            level_kind="opt",
            ess_job_key="opt",
            result_field="opt_result",
            result_payload=_opt_result_payload(ts_record),
            depends_on=ts_opt_depends_on,
            tckdb_origin=None,
            conformer_xyz_text=conformer_xyz_text,
            include_artifacts=include_artifacts,
        )

        calc_keys: dict[str, str] = {_CALC_KEY_OPT: ts_opt_key}
        additional: list[dict[str, Any]] = []
        # Order: ts_guess (parent) first, then freq/sp/irc descend
        # from ts_opt. ``BundleTransitionStateIn`` doesn't enforce a
        # geometry_key on additional calcs (unlike the species-side
        # validator), so type=path_search stays bare.
        if ts_guess_calc is not None:
            additional.append(ts_guess_calc)
            calc_keys[_CALC_KEY_TS_GUESS] = ts_guess_key

        freq_result = _freq_result_payload(ts_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=ts_freq_key,
                    calc_role=_CALC_KEY_FREQ,
                    calc_type="freq",
                    level_kind="freq",
                    ess_job_key="freq",
                    result_field="freq_result",
                    result_payload=freq_result,
                    depends_on=[{"parent_calculation_key": ts_opt_key, "role": "freq_on"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    include_artifacts=include_artifacts,
                ))
                calc_keys[_CALC_KEY_FREQ] = ts_freq_key
            except ValueError as exc:
                logger.warning("TCKDB computed-reaction: ts freq calc skipped: %s", exc)

        sp_result = _sp_result_payload(ts_record)
        if sp_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=ts_sp_key,
                    calc_role=_CALC_KEY_SP,
                    calc_type="sp",
                    level_kind="sp",
                    ess_job_key="sp",
                    result_field="sp_result",
                    result_payload=sp_result,
                    depends_on=[{"parent_calculation_key": ts_opt_key, "role": "single_point_on"}],
                    tckdb_origin=(
                        _sp_reuse_origin(output_doc, ts_record)
                    ),
                    conformer_xyz_text=conformer_xyz_text,
                    include_artifacts=include_artifacts,
                ))
                calc_keys[_CALC_KEY_SP] = ts_sp_key
            except ValueError as exc:
                logger.warning("TCKDB computed-reaction: ts sp calc skipped: %s", exc)

        # IRC: emit a calc when ARC has irc_logs populated. Always carry
        # depends_on(role=irc_start) → ts_opt, because IRC is seeded from
        # the optimized TS saddle. When the logs parse cleanly, attach a
        # structured irc_result with forward/reverse points and producer-
        # declared output_geometries for the irc_forward/irc_reverse
        # endpoints. When parsing fails we still emit the type=irc calc
        # (so kinetics.source_calculations(role=irc) and the dependency
        # edge can stand on their own) but omit irc_result rather than
        # fabricating incomplete structured data.
        # The IRC level: the adaptive level ``restart.yml`` gives this TS when the
        # adaptive levels name ``irc``, else the ``irc_level`` restart.yml
        # records (ARC records it whenever it differs from the settings
        # default), else assumed (below). A named ``irc`` that cannot be
        # attributed states no level, so no IRC calculation.
        irc_level, irc_level_unattributable = _irc_level(output_doc, ts_record)
        if (irc_level is None and irc_level_unattributable and ts_record.get("irc_logs")
                and arc13.has_levels(ts_record)):
            _warn_irc_level_not_stated(warnings, ts_label=ts_label, ts_record=ts_record)
        if (irc_level is not None and not irc_level.get("software")
                and arc13.has_levels(ts_record)):
            # Schema 1.3 states the program of the IRC logs, or it is not stated.
            logger.warning(
                "TCKDB irc_software_not_stated: TS %r IRC calculation not filed: "
                "ARC recorded no ess_software.irc.", ts_label)
            if warnings is not None:
                warnings.append(_self_check(
                    ArcWarning.IRC_SOFTWARE_NOT_STATED.value,
                    f"The IRC calculation of {ts_label!r} was not uploaded: ARC "
                        "states the program of the IRC logs in ess_software.irc, "
                        "only when every IRC log was identified as the same program, "
                        "and it did not here.",
                    "transition_state.irc.software_release",
                    {"action": "irc_calculation_omitted"}))
            irc_level_unattributable = True
        elif irc_level is not None and not irc_level.get("software"):
            # ARC's IRC program rule gives no ESS this adapter can name.
            logger.warning(
                "TCKDB irc_software_not_stated: TS %r IRC calculation not filed: "
                "its level's method (%s) is not run in Gaussian, and ARC records no "
                "IRC program.", ts_label, irc_level.get("method"))
            if warnings is not None:
                warnings.append(_self_check(
                    ArcWarning.IRC_SOFTWARE_NOT_STATED.value,
                    f"The IRC calculation of {ts_label!r} was not uploaded: ARC runs "
                        "an IRC in Gaussian (arc/level.py deduce_software) except for "
                        f"UMA/torchani/xtb-type methods ({irc_level.get('method')!r}), "
                        "and records no program for it.",
                    "transition_state.irc.software_release",
                    {"action": "irc_calculation_omitted"}))
            irc_level_unattributable = True
        if ts_record.get("irc_logs") and not irc_level_unattributable:
            try:
                irc_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=ts_irc_key,
                    calc_role=_CALC_KEY_IRC,
                    calc_type="irc",
                    # ARC runs IRC at ``irc_level`` (arc/scheduler.py
                    # ``run_irc_job``), which output.yml does not export.
                    # When restart.yml records it (or the adaptive levels
                    # give it), that is the level, and the program is the one
                    # the level names (there is no ``ess_software.irc``). If
                    # not, the level is the run's settings default, which is
                    # unreadable here; it is assumed to be ``opt_level`` and
                    # reported (``irc_level_assumed_opt_level``, below).
                    level_kind="irc" if irc_level is not None else "opt",
                    ess_job_key="irc" if irc_level is not None else "opt",
                    level_override=irc_level,
                    result_field=None,
                    result_payload=None,
                    depends_on=[{"parent_calculation_key": ts_opt_key, "role": "irc_start"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    include_artifacts=include_artifacts,
                )
                # IRC output geometries are derived by the server from
                # ``irc_result.points``: ``_persist_irc_result`` writes a
                # ``calculation_output_geometry`` row (role=irc_forward /
                # irc_reverse) for every forward/reverse point. Emitting
                # explicit ``output_geometries`` here would double-claim
                # those geometries and the
                # ``attach_calculation_output_geometries`` uniqueness check
                # would 422. We therefore attach only ``irc_result`` and
                # let the server own the output-geometry links.
                irc_parsed = self._parse_irc_trajectories(output_doc, ts_record)
                if irc_parsed is not None:
                    zero_ref = _resolve_irc_zero_energy_reference(
                        output_doc=output_doc,
                        ts_record=ts_record,
                        irc_level=irc_level,
                    )
                    # Build the optional TS marker from the seed-saddle
                    # record. ``conformer_xyz_text`` is the same TS xyz
                    # the TS block already uses for ``ts_opt``'s output
                    # geometry, so the marker points at the canonical
                    # saddle — no separate xyz_to_str pass needed.
                    ts_marker: dict[str, Any] | None = None
                    if conformer_xyz_text:
                        ts_marker = {
                            "xyz_text": conformer_xyz_text,
                            # Pair the marker's energy with the same
                            # zero_ref the per-point relatives are
                            # computed against — keeps relative_energy
                            # internally consistent (TS marker → 0).
                            "electronic_energy_hartree": zero_ref,
                        }
                    irc_result = _build_irc_result_payload(
                        irc_parsed,
                        zero_energy_reference_hartree=zero_ref,
                        ts_marker=ts_marker,
                        warnings=warnings,
                    )
                    if irc_result is not None:
                        irc_calc["irc_result"] = irc_result
                additional.append(irc_calc)
                calc_keys[_CALC_KEY_IRC] = ts_irc_key
                if irc_level is None:
                    _warn_irc_level_assumed(warnings, irc_calc["level_of_theory"])
            except ValueError as exc:
                logger.debug("TCKDB computed-reaction: ts irc calc skipped: %s", exc)

        # ARC exports successful TS rotor scans through the same record
        # contract as stable species. Preserve the calculation and its parent
        # even though the TS upload root has no statmech/torsion slot.
        for scan_entry in _scan_entries_from_record(ts_record):
            scan_key = scan_entry.get("key")
            scan_result = scan_entry.get("scan_result")
            if (scan_entry.get("type") != _CALC_KEY_SCAN
                    or not isinstance(scan_key, str) or not scan_key
                    or not isinstance(scan_result, Mapping)):
                continue
            try:
                scan_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=f"ts_{scan_key}",
                    calc_role=_CALC_KEY_SCAN,
                    calc_type=_CALC_KEY_SCAN,
                    level_kind="scan",
                    ess_job_key="scan",
                    result_field="scan_result",
                    result_payload=scan_result,
                    depends_on=[{"parent_calculation_key": ts_opt_key,
                                 "role": "scan_parent"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    source_constraints=scan_entry.get("constraints"),
                    output_log_path=scan_entry.get("source_log"),
                    observed_software=scan_entry.get("ess_software"),
                    observed_version=scan_entry.get("ess_version"),
                    include_artifacts=include_artifacts,
                    level_job_type=_scan_job_type(output_doc, ts_record, scan_key),
                )
                additional.append(scan_calc)
            except ValueError as exc:
                logger.warning("TCKDB transition-state: scan %s skipped: %s", scan_key, exc)

        # TS multiplicity: the TS record's own field is authoritative;
        # fall back to the reaction's multiplicity when missing (some
        # ARC runs only carry it on the reaction).
        # ARC creates the TS species with the reaction's multiplicity
        # (arc/scheduler.py ``ARCSpecies(is_ts=True, multiplicity=
        # rxn.multiplicity, ...)``), so the reaction's value is ARC's own
        # statement, not a default.
        ts_mult = ts_record.get("multiplicity")
        if ts_mult is None:
            ts_mult = reaction_multiplicity
        if ts_mult is None:
            raise ValueError(
                f"transition state {ts_label!r} has no multiplicity and the "
                "reaction record has none either; cannot build TS block."
            )
        ts_mult_record = {"multiplicity": ts_mult}

        ts_block: dict[str, Any] = {
            "charge": _stated_integer(ts_record, "charge", f"transition state {ts_label!r}"),
            "multiplicity": _stated_integer(
                ts_mult_record, "multiplicity", f"transition state {ts_label!r}", minimum=1),
            "geometry": _geometry_payload(
                ts_record, conformer_xyz_text, ts_record.get("xyz_isotopes"), key=ts_geom_key),
            "calculation": primary_calc,
            "calculations": additional,
            "label": str(ts_label)[:64],
        }
        if unmapped_smiles:
            ts_block["unmapped_smiles"] = str(unmapped_smiles)

        # ARC's verdict on whether the IRC connects the declared reactants and
        # products, from ``ts_checks['IRC']`` and nothing else (never
        # ``irc_converged``, which only says the IRC jobs finished).
        validation_evidence = _ts_irc_validation_evidence(
            ts_record,
            irc_calc_key=calc_keys.get(_CALC_KEY_IRC),
            ts_label=ts_label,
            warnings=warnings,
            reaction_record=reaction_record,
            species_index=species_index,
            ts_xyz_text=conformer_xyz_text,
        )
        # ARC's verdict on the TS frequency calculation (``ts_checks['freq']``), read from the
        # ``freq_result`` sent for it, so the record cannot contradict that result.
        validation_evidence += _ts_imaginary_mode_validation_evidence(
            ts_record,
            freq_calc_key=calc_keys.get(_CALC_KEY_FREQ),
            freq_result=freq_result,
            ts_label=ts_label,
            warnings=warnings,
        )
        if validation_evidence:
            ts_block["validation_evidence"] = validation_evidence

        # TS-side AEC/BAC corrections target the TS entry directly via
        # ``target_transition_state_entry_id`` server-side — never via the
        # reaction entry. Anchor to ts_sp specifically; species-side SP
        # keys (r0_sp / p1_sp) belong to other species and a cross-owner
        # reference would 422.
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                ts_record, legacy_1_0=_is_output_schema_1_0(output_doc),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=calc_keys.get(_CALC_KEY_SP),
            warnings=warnings,
            warning_field="transition_state.applied_energy_corrections",
            target_kind="transition_state",
            target_label=str(ts_label) or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, ts_record),
        )
        if applied_corrections:
            ts_block["applied_energy_corrections"] = applied_corrections

        return ts_block, calc_keys

    # ------------------------------------------------------------------
    # Standalone transition-state path (POST /uploads/transition-states)
    # ------------------------------------------------------------------

    def submit_computed_ts_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one standalone TS payload.

        Composes a ``TransitionStateUploadRequest`` for a single converged
        transition state and POSTs it to ``/uploads/transition-states``.
        The embedded reaction (reactants/products by identity, family,
        reversibility) is drawn from ``reaction_record`` — the TS's
        reaction entry in ``output_doc['reactions']`` — while the saddle
        geometry and opt/freq/sp/irc/path_search calculations come from
        ``ts_record`` (one entry of ``output_doc['transition_states']``).

        Returns ``None`` if the adapter is disabled, mirroring the other
        ``submit_*`` entry points. Build failures (missing xyz, missing
        opt level, a reactant/product absent from ``output_doc.species``)
        raise; the caller (``_run_ts_sweep``) wraps the per-TS call in a
        try/except so one bad TS doesn't take down the rest of the sweep.
        """
        if not self._config.enabled:
            return None

        ts_label = (
            ts_record.get("label") or ts_record.get("original_label") or "unlabeled-ts"
        )
        project_label = self._config.project_label or output_doc.get("project")

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        output_doc = _withhold_unreliable_energies_in_doc(output_doc, build_warnings)
        ts_record = _withhold_unreliable_composite_energies(output_doc, ts_record, build_warnings)
        reaction_record = _withhold_reaction_kinetics(output_doc, reaction_record, build_warnings)
        payload = self._compose_transition_state_request(
            output_doc=output_doc,
            ts_record=ts_record,
            reaction_record=reaction_record,
            warnings=build_warnings,
        )
        self._report_adaptive_omissions(output_doc, build_warnings)

        idempotency_inputs = IdempotencyInputs.from_payload(
            project_label=project_label,
            species_label=str(ts_label),
            # A TS has no conformer concept at this endpoint — one saddle
            # per upload. The conformer slot carries the reaction label so
            # the same TS geometry reused across two reaction channels
            # keeps distinct keys; the payload hash tail still makes any
            # content change replay as a new upload.
            conformer_label=str(reaction_record.get("label") or "ts"),
            payload_kind=TRANSITION_STATE_KIND,
            payload=payload,
        )
        idempotency_key = build_idempotency_key(idempotency_inputs)

        written = self._writer.write(
            label=str(ts_label),
            payload=payload,
            endpoint=TRANSITION_STATE_ENDPOINT,
            idempotency_key=idempotency_key,
            payload_kind=TRANSITION_STATE_KIND,
            base_url=self._config.base_url,
            subdir=PayloadWriter.TRANSITION_STATE_SUBDIR,
            warnings=build_warnings,
        )
        logger.info(
            "TCKDB transition-state payload written: %s (key=%s)",
            written.payload_path,
            idempotency_key,
        )

        if not self._config.upload:
            return self._finalize_skipped(written)

        return self._upload(written, payload, endpoint=TRANSITION_STATE_ENDPOINT)

    def _compose_transition_state_request(
        self,
        *,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ``TransitionStateUploadRequest`` dict.

        Producer-side omissions (an IRC result whose direction ARC does not
        state) are appended to ``warnings``.

        Reuses :meth:`_build_ts_block` — the same TS composer the
        computed-reaction bundle uses — for the saddle geometry and the
        opt/freq/sp/irc/path_search calculations (with their wrapped
        ``opt_result``/``freq_result``/… shape, which is exactly what the
        standalone endpoint's ``CalculationWithResultsPayload`` fragment
        expects). The bundle-local calc keys (``key``, ``depends_on``,
        ``geometry_key``, ``artifacts``) are stripped via
        :meth:`_ts_calc_to_standalone` because the standalone fragment
        forbids extra fields; the backend re-derives the
        path_search -> primary_opt dependency for this endpoint.

        The embedded ``reaction`` is built from ``reaction_record``:
        each reactant/product label is resolved through
        ``output_doc['species']`` into a ``SpeciesEntryIdentityPayload``
        (the same ``_species_entry_payload`` shape the species path uses).

        One piece of computed-reaction provenance is intentionally NOT carried
        here because the ``/uploads/transition-states`` schema has no slot for it:

        * **Inline artifacts** (ESS logs / input decks). The bundle path
          base64-inlines these under each calc when
          ``config.artifacts.upload`` is set; the standalone endpoint has
          no artifact field, so ``_build_ts_block`` is called with
          ``include_artifacts=False`` to skip the read+encode entirely
          (rather than build-then-drop), and a one-time WARNING tells the
          user their request can't be honored on this path.

        Since tckdb-schemas 0.64 the request also takes rotor-``scan``
        calculations (with ``scan_result``) and ``applied_energy_corrections``
        (AEC/BAC; with no source keys, since the payload has no key
        namespace), both carried here, and an ``atom_map`` (participants with
        their own ``key`` and ``geometry``, the saddle point named by
        ``geometry_key``). The map is built from ARC's ``ts_atom_map`` (output
        1.3 at ARC #1059 ebc88ec8) by the same function the bundle uses
        (:func:`_tckdb_reaction_atom_map`); without it the slot stays unset and the
        reason is reported (``reaction_ts_atom_map_not_sent``, or
        ``reaction_atom_map_ts_order_not_stated`` for a document that predates the key).
        """
        species_index = _index_species(output_doc)
        reaction_record = _with_stated_participants(
            reaction_record, ts_record=ts_record, species_index=species_index,
            warnings=warnings)

        ts_label = (
            ts_record.get("label") or ts_record.get("original_label") or "unlabeled-ts"
        )

        if self._config.artifacts.upload and not self._warned_ts_artifacts_unsupported:
            logger.warning(
                "TCKDB: artifact upload (config.artifacts.upload=true) is not "
                "supported for standalone transition-state uploads "
                "(/uploads/transition-states has no artifact slot); ESS logs / "
                "input decks will NOT be uploaded for TS records in this run. "
                "Use computed_species/conformer mode for artifact uploads."
            )
            self._warned_ts_artifacts_unsupported = True

        ts_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        ts_block, _ = self._build_ts_block(
            output_doc=output_doc,
            ts_record=ts_record,
            ts_label=str(ts_label),
            reaction_multiplicity=reaction_record.get("multiplicity"),
            unmapped_smiles=_ts_unmapped_smiles_handle(
                ts_record=ts_record,
                reaction_record=reaction_record,
                species_index=species_index,
            ),
            # The standalone TS endpoint has no artifact slot — skip the
            # (potentially multi-MB) read+base64 rather than build+strip.
            include_artifacts=False,
            warnings=ts_warnings,
            reaction_record=reaction_record,
            species_index=species_index,
        )
        # ARC's ts_atom_map (ebc88ec8) becomes TCKDB's atom_map here too, from the same data as on
        # the bundle: each participant carries its own ``key`` and ``geometry`` (the geometries the
        # map counts into) and the request names the saddle-point geometry by ``geometry_key``.
        ts_geometry = ts_block.get("geometry")
        slots = _ts_route_atom_map_slots(reaction_record, species_index)
        atom_map_block = None
        if isinstance(ts_geometry, Mapping):
            atom_map_block = _tckdb_reaction_atom_map(
                reaction_record, ts_label=str(ts_label), ts_xyz_text=ts_geometry.get("xyz_text"),
                ts_geometry_key=_TS_ROUTE_TS_GEOMETRY_KEY, participants=slots,
                irc_evidence=ts_block.get("validation_evidence") or (), warnings=ts_warnings)
        else:
            _warn_atom_map_not_sent(
                reaction_record, ts_label=str(ts_label), warnings=ts_warnings)
        if warnings is not None:
            warnings.extend(ts_warnings)

        # primary_opt is required and must be type=opt; _build_ts_block
        # always emits ts_block["calculation"] as the type=opt primary.
        primary_opt = self._ts_calc_to_standalone(ts_block["calculation"])
        additional_calculations = []
        # tckdb-schemas 0.64: "``additional_calculations`` now accepts ``scan``, and
        # ``CalculationWithResultsPayload`` gains ``scan_result``", so a rotor scan travels
        # with its points on this route too.
        for calc in ts_block.get("calculations", []):
            additional_calculations.append(self._ts_calc_to_standalone(calc))

        request: dict[str, Any] = {
            "reaction": self._build_ts_reaction_upload(
                reaction_record=reaction_record,
                species_index=species_index,
                schema_1_3=_is_output_schema_1_3_or_later(output_doc),
            ),
            "charge": ts_block["charge"],
            "multiplicity": ts_block["multiplicity"],
            "geometry": {k: v for k, v in ts_block["geometry"].items() if k != "key"},
            "primary_opt": primary_opt,
        }
        if atom_map_block is not None:
            members = {"reactant": request["reaction"]["reactants"], "product": request["reaction"]["products"]}
            for slot in slots:
                member = members[slot["side"]][slot["index"] - 1]
                member["key"] = slot["species_key"]
                member["geometry"] = dict(slot["geometry"])
            request["geometry_key"] = _TS_ROUTE_TS_GEOMETRY_KEY
            request["atom_map"] = atom_map_block
        if additional_calculations:
            request["additional_calculations"] = additional_calculations
        # 0.64: ``applied_energy_corrections`` takes no ``source_calculation_key`` or
        # ``source_conformer_key`` ("the payload has no key namespace") and no frequency
        # scale factor, which "is defined by the frequency calculation it was applied to".
        corrections = [
            {k: v for k, v in correction.items()
             if k not in ("source_calculation_key", "source_conformer_key")}
            for correction in ts_block.get("applied_energy_corrections") or []
            if correction.get("frequency_scale_factor") is None
        ]
        if corrections:
            request["applied_energy_corrections"] = corrections
        # The standalone route has no calculation-key namespace: evidence binds
        # to the upload's single irc additional calculation and must omit
        # ``source_calculation_key``.
        # ``energy_ordering`` is refused here outright (no calculations for the wells), and
        # ``_build_ts_block`` never builds it; ``imaginary_mode`` binds to the one freq calculation.
        if ts_block.get("validation_evidence"):
            request["validation_evidence"] = [
                {k: v for k, v in evidence.items() if k != "source_calculation_key"}
                for evidence in ts_block["validation_evidence"]
                if evidence["kind"] != "energy_ordering"
            ]
        unmapped_smiles = ts_block.get("unmapped_smiles")
        if unmapped_smiles:
            request["unmapped_smiles"] = str(unmapped_smiles)
        label = ts_block.get("label")
        if label:
            request["label"] = str(label)[:64]
        return request

    @staticmethod
    def _ts_calc_to_standalone(calc: Mapping[str, Any]) -> dict[str, Any]:
        """Strip bundle-only keys, yielding a plain CalculationWithResultsPayload.

        ``_build_ts_block`` produces calc dicts carrying the computed-
        reaction bundle's cross-reference fields (``key``, ``depends_on``)
        and, for freq, an inline ``artifacts`` list. The standalone TS
        endpoint validates each calc against ``CalculationWithResultsPayload``
        (``extra="forbid"``), which accepts none of those. Everything else
        — ``type``, ``software_release``, ``level_of_theory``, the wrapped
        result block, ``parameters_json`` (origin metadata), input/output
        geometries, ``hessian``, and ``constraints`` — is preserved.
        """
        return {
            key: value
            for key, value in calc.items()
            if key not in _TS_STANDALONE_STRIP_CALC_KEYS
        }

    def _build_ts_reaction_upload(
        self,
        *,
        reaction_record: Mapping[str, Any],
        species_index: Mapping[str, Mapping[str, Any]],
        schema_1_3: bool = False,
    ) -> dict[str, Any]:
        """Build the embedded ``TSReactionUpload`` for a standalone TS upload.

        Ordered reactant/product participants resolve each label through
        ``species_index`` into a ``SpeciesEntryIdentityPayload`` via
        :meth:`_species_entry_payload`. A missing label raises — the same
        loud failure the computed-reaction path uses — so a TS is never
        uploaded with an incomplete reaction description.

        ``reversible`` is required by the schema (no default). Output 1.3
        states it per reaction (``True`` for the ``<=>`` arrow, ``False`` for
        ``=>``, ``null`` when the arrow is unknown), and a stated bool is sent as
        stated. Otherwise (a document before 1.3, or a ``null``) this sends True
        (the same default the computed-reaction schema applies). Kept by
        maintainer decision (adapter 0.6.0): refusing would block every
        standalone TS upload whose reaction does not state it. TCKDB issue
        https://github.com/TCKDB/TCKDB/issues/583 asks the transition-state
        route to default it like the computed-reaction route does.
        """
        reactant_labels = list(reaction_record.get("reactant_labels") or [])
        product_labels = list(reaction_record.get("product_labels") or [])
        if not reactant_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no "
                "reactant_labels; cannot build the embedded TS reaction."
            )
        if not product_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no "
                "product_labels; cannot build the embedded TS reaction."
            )

        def _participants(labels: list[str], side: str) -> list[dict[str, Any]]:
            participants: list[dict[str, Any]] = []
            for label in labels:
                record = species_index.get(label)
                if record is None:
                    raise ValueError(
                        f"reaction {reaction_record.get('label')!r}: {side} "
                        f"label {label!r} not found in output_doc.species."
                    )
                participants.append(
                    {"species_entry": self._species_entry_payload(record)}
                )
            return participants

        stated_reversible = reaction_record.get("reversible") if schema_1_3 else None
        reaction: dict[str, Any] = {
            "reversible": stated_reversible if isinstance(stated_reversible, bool) else True,
            "reactants": _participants(reactant_labels, "reactant"),
            "products": _participants(product_labels, "product"),
        }
        family = reaction_record.get("family")
        # Mirror the backend's ``normalize_optional_text``: a
        # whitespace-only family would pass a plain truthy gate, ship with
        # the source note, then normalize to None server-side and 422
        # (source_note without a family). Gate on the stripped value so a
        # blank family is omitted entirely, like the backend treats it.
        if family is not None and str(family).strip():
            reaction["reaction_family"] = str(family).strip()
            # The server validates the family against its canonical list
            # and demands a source_note for non-canonical names. We don't
            # have that list at the producer, so always tag the source —
            # a no-op for canonical names, a safety net otherwise. Mirrors
            # the computed-reaction path.
            reaction["reaction_family_source_note"] = "ARC-reported family"
        return reaction

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    @staticmethod
    def _species_entry_payload(record: Mapping[str, Any]) -> dict[str, Any]:
        smiles = record.get("smiles")
        if not smiles:
            raise ValueError(
                f"output.yml record for label={record.get('label')!r} has no smiles; "
                "TCKDB upload requires a SMILES on the species_entry."
            )
        smiles_text = str(smiles)
        is_ts = bool(record.get("is_ts"))
        if is_ts:
            # Every production call site passes a record sourced from
            # ``output_doc['species']`` (never ``output_doc
            # ['transition_states']``), and arc/output.py's
            # ``build_output_dict`` routes an entry into ``species`` or
            # ``transition_states`` by ``spc.is_ts`` at construction time
            # — so no path inside ARC's own pipeline is known to reach
            # here with ``is_ts=True``. This is the same situation as the
            # freq_n_imag contradictions in ``_freq_result_payload``: the
            # live threat is a hand-written/older/third-party output.yml
            # that carries ``is_ts=True`` on a ``species[]`` entry —
            # evidence.py's schema check only verifies ``schema_version``,
            # not the full output.yml JSON schema, so nothing upstream
            # would stop it reaching here. TCKDB 0.22.0's
            # ``StationaryPointKind`` has no ``transition_state`` member
            # at all (only ``minimum``/``vdw_complex``), and neither is a
            # defensible stand-in for a genuine TS — a transition state
            # belongs in ``output_doc['transition_states']`` and its own
            # upload path, not folded into a species_entry. Applying the
            # same standard as ``_freq_result_payload``'s raises: refuse
            # the record rather than emit an invalid enum value or
            # silently reclassify it as a minimum.
            raise ValueError(
                f"output.yml record for label={record.get('label')!r} has "
                f"is_ts=True in a species-entry context (species/reactant/"
                f"product), but TCKDB 0.22.0's StationaryPointKind has no "
                f"'transition_state' member. Refusing to deposit this "
                f"record rather than emit an invalid enum value or "
                f"silently reclassify it as a minimum."
            )
        # ``electronic_state_kind`` is not sent (adapter 0.6.0, maintainer
        # decision). ARC does not state it: it computes the multiplicity it
        # was given, which need not be the ground state (singlet O2 or
        # CH2). The field is optional, so TCKDB applies its own default
        # rather than the adapter asserting one. The other identity-
        # disambiguation fields (stereo_label, electronic_state_label,
        # term_symbol[_raw], isotopologue_label) are likewise left null:
        # ARC has no reliable source for them, the TCKDB backend derives stereo_label
        # from the uploaded 3D geometry when applicable, and the dedupe
        # uniqueness key uses ``nulls_not_distinct`` so paired nulls collapse
        # to the same row rather than fragmenting it.
        entry: dict[str, Any] = {
            "molecule_kind": "molecule",
            "smiles": smiles_text,
            "charge": _stated_integer(record, "charge", f"species {record.get('label')!r}"),
            "multiplicity": _stated_integer(
                record, "multiplicity", f"species {record.get('label')!r}", minimum=1),
            # is_ts is always False here now — the raise above refuses
            # any is_ts=True record before this point.
            "species_entry_kind": "minimum",
        }
        # ``unmapped_smiles`` is not sent for a species: ARC's output.yml
        # writes no such key, and the adapter never derives one by string
        # manipulation of the SMILES (atom-map stripping is not attempted).
        return entry

    def _build_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ConformerUploadRequest dict.

        Besides the opt (primary) and freq/sp (additional) calculations, it
        carries the statmech block and the applied energy corrections, built by
        the same builders as the computed-species route (Hessian-gated
        treatments, BAC omission, ``atom_params``, the Arkane release, the
        declared energy level). The conformer route accepts only freq and sp as
        additional calculations, so no rotor scan is sent and each torsion drops
        its scan link (``torsion_scan_not_built``); ARC's rejected rotors go to
        ``statmech.torsions[].invalidated_reason``. There is no thermo slot.
        Producer-side omissions are appended to ``warnings``.
        """
        _warn_primary_opt_placeholder(species_record, warnings, field="calculation")
        species_entry = self._species_entry_payload(species_record)
        geometry_payload = _geometry_payload(
            species_record, _require_xyz_text(species_record), species_record.get("xyz_isotopes"))
        primary, additional = self._build_calculations(output_doc, species_record)

        # Local keys, so the statmech links and the corrections' source name
        # the calculations of this request (as computed-species' role keys do).
        primary["key"] = _CALC_KEY_SP if primary["type"] == "sp" else _CALC_KEY_OPT
        for calc in additional:
            calc["key"] = calc["type"]
        calc_keys_by_role = {
            calc["key"]: calc["key"] for calc in (primary, *additional)}

        payload: dict[str, Any] = {
            "species_entry": species_entry,
            "geometry": geometry_payload,
            "calculation": primary,
            "scientific_origin": "computed",
        }
        if additional:
            payload["additional_calculations"] = additional

        omitted_bacs: list[str] = []
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                species_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=(
                _CALC_KEY_SP if _CALC_KEY_SP in calc_keys_by_role else None
            ),
            warnings=warnings,
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, species_record),
            omitted_bac_reasons=omitted_bacs,
        )
        if applied_corrections:
            payload["applied_energy_corrections"] = applied_corrections

        arc_wt = _arc_workflow_tool_release(output_doc)
        # The route accepts no scan calculations: every scan ARC exported is
        # one this request cannot carry.
        unbuilt_scans = {
            entry["key"]: "the conformer upload accepts only freq and sp as "
                          "additional calculations"
            for entry in _scan_entries_from_record(species_record)
            if entry.get("type") == _CALC_KEY_SCAN
            and isinstance(entry.get("key"), str) and entry["key"]
            and isinstance(entry.get("scan_result"), Mapping)
        }
        statmech_block = _build_statmech_block_for_species(
            output_doc=output_doc,
            species_record=species_record,
            calc_keys_by_role=_with_composite_role(species_record, calc_keys_by_role),
            workflow_tool_release=arc_wt,
            target_model="ConformerUploadStatmechPayload",
            unbuilt_scans=unbuilt_scans,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            warnings=warnings,
            warning_field="statmech",
            energy_declaration=_energy_level_declaration(
                _thermo_energy_level(output_doc, species_record),
                calc_keys_by_role=calc_keys_by_role,
                calculations=_calculations_by_key(primary, additional),
            ),
        )
        if statmech_block is not None:
            arkane_release = _arc_analysis_software_release(output_doc)
            if arkane_release is not None:
                statmech_block["software_release"] = dict(arkane_release)
            # The computed-species route names ARC once, at bundle level, and
            # its statmech inherits it; this request has no such slot.
            if arc_wt is not None:
                statmech_block["workflow_tool_release"] = dict(arc_wt)
            payload["statmech"] = statmech_block

        label = species_record.get("label")
        if label:
            payload["label"] = str(label)[:64]
        return payload

    @classmethod
    def _build_calculations(
        cls,
        output_doc: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return (primary opt calculation, [freq, sp] additional calculations).

        Additional calculations are skipped (with a warning) when their
        result fields are absent or malformed, or when no level of theory
        is available. Skipping an optional calc never fails the upload.

        A single atom (its own XYZ has one atom) returns its ``sp`` as the primary
        calculation and no additional calculations (tckdb-schemas 0.59,
        TCKDB#610; ``ConformerUploadRequest`` gives a one-atom ``sp`` primary
        the conformer geometry link), see ``_build_monatomic_primary_sp``.
        """
        if _is_single_atom_geometry(_require_xyz_text(record)):
            sp_result = _monatomic_sp_result_payload(record)
            if sp_result is None:
                raise _atom_without_sp_energy_error(record)
            return cls._calculation_payload(
                output_doc,
                record,
                calc_type="sp",
                level=_resolve_level(output_doc, "sp", record),
                ess_job_key="sp",
                result_field="sp_result",
                result_payload=sp_result,
                tckdb_origin=None,
                final_settings=_final_settings_for_calc(species_record=record, calc_role=_CALC_KEY_SP),
            ), []
        primary = cls._calculation_payload(
            output_doc,
            record,
            calc_type="opt",
            level=_resolve_level(output_doc, "opt", record),
            ess_job_key="opt",
            result_field="opt_result",
            result_payload=_opt_result_payload(record),
            scf_stability_target=True,
        )

        additional: list[dict[str, Any]] = []
        freq_result = _freq_result_payload(record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None:
            freq_level = _resolve_level(output_doc, "freq", record)
            try:
                additional.append(
                    cls._calculation_payload(
                        output_doc,
                        record,
                        calc_type="freq",
                        level=freq_level,
                        ess_job_key="freq",
                        result_field="freq_result",
                        result_payload=freq_result,
                    )
                )
            except ValueError as exc:
                logger.warning(
                    "TCKDB freq additional calculation skipped for label=%s: %s",
                    record.get("label"), exc,
                )

        sp_result = _sp_result_payload(record)
        if sp_result is not None:
            sp_level = _resolve_level(output_doc, "sp", record)
            sp_origin = _sp_reuse_origin(output_doc, record)
            try:
                additional.append(
                    cls._calculation_payload(
                        output_doc,
                        record,
                        calc_type="sp",
                        level=sp_level,
                        ess_job_key="sp",
                        result_field="sp_result",
                        result_payload=sp_result,
                        tckdb_origin=sp_origin,
                    )
                )
            except ValueError as exc:
                logger.warning(
                    "TCKDB sp additional calculation skipped for label=%s: %s",
                    record.get("label"), exc,
                )

        return primary, additional

    @staticmethod
    def _calculation_payload(
        output_doc: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        calc_type: str,
        level: Mapping[str, Any] | None,
        ess_job_key: str,
        result_field: str | None = None,
        result_payload: Mapping[str, Any] | None = None,
        tckdb_origin: Mapping[str, Any] | None = None,
        final_settings: Mapping[str, Any] | None = None,
        scf_stability_target: bool = False,
        observed_software: str | None = None,
        observed_version: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(level, Mapping):
            adaptive = (
                f" ARC ran with adaptive_levels naming {calc_type}, and the level "
                f"this species ran at cannot be attributed "
                f"({_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.get(calc_type, calc_type + '_level_adaptive_not_attributable')})."
                if _adaptive_kind_named(output_doc, calc_type) else ""
            )
            if arc13.has_levels(record) and not adaptive:
                adaptive = (
                    f" ARC output.yml 1.3 states no level for the {calc_type} job of this "
                    "record (its levels entry is null and no log of that job is exported, "
                    "or the level was not recorded); a header level is not borrowed for it.")
            raise ValueError(
                f"no level of theory available for {calc_type} calculation;"
                f"{adaptive} cannot build TCKDB calculation payload."
            )
        method = level.get("method")
        if not method:
            raise ValueError(
                f"level of theory for {calc_type} is missing method; "
                "cannot build TCKDB calculation payload."
            )
        if "//" in str(method):
            raise ValueError(
                f"{_W_LEVEL_METHOD_IS_COMPOUND}: the {calc_type} level method {str(method)!r} "
                "contains '//' (two levels of theory, which TCKDB refuses in a method); which "
                "half belongs to this calculation is not stated, so the calculation is not built."
            )
        if not arc13.has_levels(record):
            # Before output.yml 1.3 the scan's own program was not relied on.
            observed_software = observed_version = None
        contradiction = _route_contradiction(record, calc_type, ess_job_key, level)
        if contradiction is not None:
            _note_route_contradiction(output_doc, record, calc_type, contradiction)
            raise ValueError(
                f"level_contradicted_by_route: the {calc_type} calculation's recorded level "
                f"contradicts the keyword line the job ran with ({contradiction}); observed "
                "outranks requested, so the calculation is not built.")
        placeholder = (
            arc13.primary_opt_placeholder(record)
            if calc_type == "opt" and ess_job_key == "opt" and tckdb_origin is None else None)
        if placeholder is not None:
            tckdb_origin = _placeholder_origin(placeholder)
        if calc_type == _CALC_KEY_SCAN and arc13.has_levels(record) and not observed_software:
            # Output.yml 1.3 states each rotor scan's own program (identified from
            # its log); the header scan_level's software is only a deduction.
            raise ValueError(
                "ARC states no program for this rotor scan (rotor_scans[].ess_software "
                "is null: its log is missing or was not identified); the scan_level's "
                "software is only a deduction, so the scan calculation is not built."
            )
        # ARC's per-job banner identification names the program that actually
        # ran; the requested level may name a different troubleshooting ESS.
        # On a schema-1.3 record the program of a calculation whose level came from
        # another job (a composite run's geometry and energy, a monoatomic's sp log)
        # is that job's (``arc13.software_job_key``). ``observed_software`` /
        # ``observed_version`` are the provenance a caller read from the job's own
        # record (a rotor scan states its own program and banner).
        ess_software = record.get("ess_software")
        software_key = arc13.software_job_key(record, ess_job_key)
        if observed_software is None and isinstance(ess_software, Mapping):
            observed_software = ess_software.get(software_key)
        software_name = observed_software or level.get("software")
        if not software_name:
            raise ValueError(
                f"level of theory for {calc_type} is missing software; "
                "cannot identify the ESS for TCKDB."
            )

        level_of_theory = _arc_level_to_tckdb_lot(level)
        if level_of_theory is not None and level_of_theory["method"] != str(level["method"]):
            message = (
                f"ARC's {calc_type} level method {str(level['method'])!r} names a correction "
                f"table, not a method; the {calc_type} calculation is sent with method "
                f"{level_of_theory['method']!r} (the calculation that ran)."
            )
            logger.warning("TCKDB %s: %s: %s", f"calculation.{calc_type}",
                           _W_CORRECTION_TABLE_METHOD_SPLIT, message)
        if level_of_theory is None:
            # method was already validated above; this is defensive against
            # a future change to _arc_level_to_tckdb_lot that drops the row.
            raise ValueError(
                f"could not project level of theory for {calc_type} onto "
                "TCKDB LevelOfTheoryRef shape."
            )

        # These are the references recorded from the actual freq/SP inputs.
        # ARC's stability verdict describes the opt job's wavefunction, so it
        # goes on the primary opt only (``scf_stability_target``), never on
        # freq/sp (their own SCF may land on another solution), scans or IRC.
        scf_reference = record.get("scf_reference")
        scf_stability: dict[str, Any] | None = None
        if calc_type in {_CALC_KEY_FREQ, _CALC_KEY_SP} and isinstance(scf_reference, Mapping):
            reference = scf_reference.get(f"{calc_type}_reference")
            if reference in {"restricted", "unrestricted", "restricted_open"}:
                level_of_theory["spin_treatment"] = reference
        if scf_stability_target:
            scf_stability = _scf_stability_payload(record)

        software_release: dict[str, Any] = {"name": str(software_name)}
        ess_versions = record.get("ess_versions")
        if observed_version:
            software_release.update(
                _split_ess_version_banner(software_release["name"], str(observed_version)))
        elif isinstance(ess_versions, Mapping):
            # ess_versions is keyed by job type ('opt', 'freq', 'sp', 'neb'),
            # not by software name. Fall back to opt's version if the
            # job-specific entry is missing (often the case for combined
            # opt+freq runs or shared sp/freq logs).
            ess_version = ess_versions.get(software_key)
            if not ess_version and ess_job_key not in (_CALC_KEY_SCAN, "conf_opt", "irc"):
                # Never borrow optimization provenance for a scan or a
                # conformer screen, or pair
                # another program's banner with this program.
                opt_software = (
                    ess_software.get("opt") if isinstance(ess_software, Mapping)
                    else (_resolve_level(output_doc, "opt", record) or {}).get("software")
                )
                if (opt_software and str(opt_software).lower() == str(software_name).lower()):
                    ess_version = ess_versions.get("opt")
            if ess_version:
                software_release.update(
                    _split_ess_version_banner(software_release["name"], str(ess_version)))

        calc: dict[str, Any] = {
            "type": calc_type,
            "quality": "raw",
            "software_release": software_release,
            "level_of_theory": level_of_theory,
        }

        arc_version = output_doc.get("arc_version")
        arc_git_commit = output_doc.get("arc_git_commit")
        if arc_version or arc_git_commit:
            wt: dict[str, Any] = {"name": "ARC"}
            if arc_version:
                wt["version"] = str(arc_version)
            if arc_git_commit:
                wt["git_commit"] = str(arc_git_commit)
            calc["workflow_tool_release"] = wt

        if result_field and result_payload:
            calc[result_field] = dict(result_payload)

        parameters: list[dict[str, Any]] = []
        hessian_method = record.get("freq_hessian_method")
        if calc_type == _CALC_KEY_FREQ and hessian_method in {
            "analytic", "finite_difference_gradient", "finite_difference_energy",
        }:
            parameters.append({
                "raw_key": "freq_hessian_method",
                "raw_value": hessian_method,
                "canonical_key": "freq.hessian_method",
                "canonical_value": hessian_method,
                "section": "freq",
                "value_type": "string",
            })
        # The observed ESS keyword line (schema 1.3 ``*_route``): the one execution
        # control ARC states whole. TCKDB's ``CalculationParameterObservation`` is its
        # home (``raw_key`` is software-specific, ``section`` the job); it is sent as
        # stated, never split into keywords or rebuilt from the level.
        parameters.extend(_route_parameters(record, calc_type, ess_job_key))
        if parameters:
            calc["parameters"] = parameters

        # Optional S**2 spin-contamination diagnostic for the sp calc. Every
        # sp-calc construction site funnels through here, so attaching it once
        # covers computed-species / computed-reaction / TS / species-entry
        # paths uniformly. Emitted only for open-shell/unrestricted single
        # points where ARC parsed ``<S**2>`` (``sp_spin_diagnostic`` on the
        # record); restricted / closed-shell records yield None and the block
        # is omitted entirely (never an all-null / fabricated block).
        if calc_type == _CALC_KEY_SP:
            spin_diagnostic = _spin_diagnostic_payload(record)
            if spin_diagnostic is not None:
                calc["spin_diagnostic"] = spin_diagnostic
            # Output 1.3's ``sp_t1_diagnostic`` is the T1 diagnostic ARC parsed
            # from the sp log (stated only for a coupled-cluster or QCISD sp
            # level); TCKDB's home for it is the sp calculation's
            # ``wavefunction_diagnostic.t1_diagnostic``.
            if _is_output_schema_1_3_or_later(output_doc):
                wavefunction_diagnostic = _wavefunction_diagnostic_payload(record)
                if wavefunction_diagnostic is not None:
                    calc["wavefunction_diagnostic"] = wavefunction_diagnostic

        if scf_stability is not None:
            calc["scf_stability"] = scf_stability

        # ``parameters_json`` is the single per-calc slot for free-form
        # qualifier metadata. Two writers feed it: ``tckdb_origin``
        # (provenance qualifier — reused-result / screened-conformer)
        # and ``final_settings`` (the calc's effective scientific
        # settings, e.g. ``{"fine": True}``, distinct from LoT-identity
        # keywords). The merge helper drops the field entirely when
        # both sources are empty.
        parameters_json = _merge_parameters_json(
            tckdb_origin=tckdb_origin,
            final_settings=final_settings,
        )
        if parameters_json is not None:
            calc["parameters_json"] = parameters_json

        return calc

    # ------------------------------------------------------------------
    # Upload + sidecar finalization
    # ------------------------------------------------------------------


    def _log_readiness_recovery(self) -> None:
        """Tell the user the payloads are on disk and how to re-run manually.

        A readiness failure means the science is done and the payload was
        written — only the network POST didn't happen. The upload is fully
        replayable via the standalone CLI once the server is back, so emit
        the exact command on its own line for copy-paste. The input.yml
        path isn't in scope at this layer, so we give the invariant form
        (``<project_dir>/input.yml``, the CLI's default location) with the
        real project directory and upload mode we do have.
        """
        payload_dir = self._writer.root
        project_dir = self._project_directory
        mode = self._config.upload_mode
        if project_dir is not None:
            input_ref = f"{project_dir}/input.yml"
            recovery_cmd = (
                f"tckdb-arc-upload {input_ref} "
                f"-p {project_dir} --upload-mode {mode}"
            )
        else:
            # No project directory in scope — give the invariant shape with
            # a clear placeholder rather than a wrong absolute path.
            recovery_cmd = (
                f"tckdb-arc-upload <input.yml> --upload-mode {mode}"
            )
        logger.warning(
            "TCKDB server was not ready after %d attempts; payloads were "
            "written to %s. Re-run the upload once it is up:\n%s",
            PREFLIGHT_MAX_ATTEMPTS, payload_dir, recovery_cmd,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_CONTRADICTIONS_KEY = "_tckdb_route_contradictions"
_W_LEVEL_CONTRADICTED_BY_ROUTE = ArcWarning.LEVEL_CONTRADICTED_BY_ROUTE.value


def _route_contradiction(
    record: Mapping[str, Any], calc_type: str, ess_job_key: str, level: Mapping[str, Any],
) -> str | None:
    """Description of a clear contradiction between a 1.3 record's level and the route the job ran with."""
    if not arc13.has_levels(record):
        return None
    if calc_type == _CALC_KEY_IRC:
        routes = record.get("irc_log_routes")
        for route in routes if isinstance(routes, list) else ():
            found = arc13.route_contradicts_level(route, level)
            if found:
                return found
        return None
    if calc_type != ess_job_key or calc_type not in arc13.ROUTE_FIELDS:
        return None
    if arc13.software_job_key(record, calc_type) == "composite":
        return None
    return arc13.route_contradicts_level(arc13.route_for_job(record, calc_type), level)


def _note_route_contradiction(
    output_doc: Mapping[str, Any], record: Mapping[str, Any], calc_type: str, detail: str,
) -> None:
    noted = output_doc.get(_CONTRADICTIONS_KEY)
    if isinstance(noted, list):
        entry = (str(record.get("label")), calc_type, detail)
        if entry not in noted:
            noted.append(entry)


_W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED = ArcWarning.COMPOSITE_GEOMETRY_LEVEL_NOT_STATED.value
_W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB = ArcWarning.PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB.value


def _placeholder_origin(kind: str) -> dict[str, Any]:
    """``tckdb_origin`` marking a primary ``opt`` that stands in for a job ARC did not run as an opt."""
    reason = (
        "placeholder: the geometry came from a composite-method job whose internal "
        "optimisation level ARC does not export; filed at the composite level ARC states"
        if kind == "composite" else
        "placeholder: ARC exports no optimisation job for this record; TCKDB requires a "
        "primary opt, filed at the run's header opt level"
    )
    return {
        "origin_kind": "derived",
        "origin_detail": f"placeholder_primary_opt_{kind}",
        "reason": reason,
        "independent_ess_job": False,
        "producer": "ARC",
    }


def _route_parameters(
    record: Mapping[str, Any], calc_type: str, ess_job_key: str,
) -> list[dict[str, Any]]:
    """``parameters`` observations for the ESS keyword line(s) of a 1.3 record's job."""
    if calc_type == _CALC_KEY_IRC:
        routes = record.get("irc_log_routes")
        if not arc13.has_levels(record) or not isinstance(routes, list):
            return []
        stated = [r.strip() for r in routes if isinstance(r, str) and r.strip()]
        if not stated:
            return []
        if len(set(stated)) == 1 and len(stated) == len(routes):
            return [_route_observation(stated[0], "irc")]
        return [
            _route_observation(r.strip(), "irc", index=i)
            for i, r in enumerate(routes) if isinstance(r, str) and r.strip()
        ]
    if calc_type != ess_job_key or calc_type not in arc13.ROUTE_FIELDS:
        return []
    route = arc13.route_for_job(record, calc_type)
    return [_route_observation(route, calc_type)] if route else []


def _route_observation(route: str, section: str, *, index: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "raw_key": "route", "raw_value": route, "section": section, "value_type": "string",
    }
    if index is not None:
        out["parameter_index"] = index
    return out


def _sp_reuse_origin(
    output_doc: Mapping[str, Any], record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The ``tckdb_origin`` of a record's sp calculation when its energy is reused, else ``None``.

    A schema-1.3 record states it: with no ``sp_log`` the energy is read from the
    optimization log, or on a composite run from the composite log
    (``arc13.sp_energy_source``). Older output has only the header levels
    (``_sp_is_reused_from_opt``).
    """
    if arc13.has_levels(record):
        source = arc13.sp_energy_source(record)
        return _reused_origin(source) if source else None
    return _reused_origin("opt") if _sp_is_reused_from_opt(output_doc) else None


def _sp_is_reused_from_opt(output_doc: Mapping[str, Any]) -> bool:
    """Whether ARC's SP energy is reused from the opt calculation.

    The signal is structural equality between ``sp_level`` and
    ``opt_level``. Two flavors both count:

    - ``sp_level`` absent / null in output.yml — the common ARC case
      where the user only declares ``opt_level=`` and ARC reuses the
      opt energy as the SP energy.
    - ``sp_level`` explicitly set to a dict structurally equal to
      ``opt_level`` — same outcome, no separate ESS job.

    A ``sp_level`` that differs from ``opt_level`` (different method,
    basis, software, etc.) means a real SP job was executed, so we
    return False and the calculation gets no reused-result marker.
    """
    opt_level = output_doc.get("opt_level")
    if not isinstance(opt_level, Mapping):
        return False
    sp_level = output_doc.get("sp_level")
    if sp_level is None:
        return True
    if not isinstance(sp_level, Mapping):
        return False
    return dict(sp_level) == dict(opt_level)


def _reused_origin(reused_from_calc_type: str) -> dict[str, Any]:
    """Build the ``tckdb_origin`` payload for a reused-result calculation.

    Lives under ``parameters_json.tckdb_origin`` on the calculation row.
    The DAG edge between calculations carries the relational link
    (e.g. ``opt -> sp`` with role ``single_point_on``); this dict
    carries the qualifier — *this* row's energy is reused, not freshly
    computed — so downstream consumers can tell aggregate-from-opt SP
    rows apart from independently executed SP jobs.
    """
    # TCKDB has no composite calculation type: a composite job is filed as the
    # record's ``opt`` calculation (it produced the geometry), so the energy is
    # reused from that ``opt`` row and ``source_job`` names the ARC job.
    reused_from = (
        {"calculation_type": "opt", "source_job": "composite"}
        if reused_from_calc_type == "composite"
        else {"calculation_type": reused_from_calc_type}
    )
    return {
        "origin_kind": "reused_result",
        "reused_from": reused_from,
        "reason": (
            f"sp_level equals {reused_from_calc_type}_level; "
            f"{reused_from_calc_type} electronic energy reused as SP energy"
            if reused_from_calc_type == "opt" else
            f"the {reused_from_calc_type} job's electronic energy is the SP energy "
            "(ARC read it from that job's log)"
        ),
        "independent_ess_job": False,
        "producer": "ARC",
    }


# Per-calc-role mapping to the ``<role>_final_settings`` field name on the
# species record. Roles not in the map have no current producer-side
# source of final-settings data (ARC's ``output.yml`` writes only the opt,
# coarse-opt, freq and sp fields; there is no ``irc_final_settings``); the
# helper returns ``None`` for them and the adapter omits
# ``parameters_json.final_settings`` from the calc.
_FINAL_SETTINGS_FIELD_BY_CALC_ROLE: Mapping[str, str] = {
    "opt": "opt_final_settings",
    "opt_coarse": "coarse_opt_final_settings",
    "freq": "freq_final_settings",
    "sp": "sp_final_settings",
}


def _final_settings_for_calc(
    *,
    species_record: Mapping[str, Any],
    calc_role: str,
) -> dict[str, Any] | None:
    """Return the calc's ``final_settings`` dict from the species record.

    ``final_settings`` carries the final effective scientific/numerical
    knobs that defined this calculation — distinct from
    ``level_of_theory.keywords`` (which carries LoT-identity settings)
    and from operational/scheduler metadata (server, queue, runtime,
    job ids — explicitly out of scope).

    Today the producer (``arc/output.py``) populates these honestly
    from observable run state (e.g. ``opt_final_settings.fine = True``
    when a coarse stage ran, since that proves the fine opt ran with
    ``fine=True``). Calls return ``None`` when:
    * the calc role has no producer-side source today,
    * the field is missing from the species record (older output.yml),
    * the field is present but empty / not a mapping.
    """
    field = _FINAL_SETTINGS_FIELD_BY_CALC_ROLE.get(calc_role)
    if field is None:
        return None
    raw = species_record.get(field)
    if not isinstance(raw, Mapping) or not raw:
        return None
    return {k: v for k, v in raw.items() if v is not None}


def _merge_parameters_json(
    *,
    existing: Mapping[str, Any] | None = None,
    tckdb_origin: Mapping[str, Any] | None = None,
    final_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Merge optional calculation metadata into ``parameters_json``.

    ``parameters_json`` is the single calc-level slot TCKDB exposes for
    free-form qualifier metadata (per-row, opaque to the schema). Today
    two writers populate it:

    * ``tckdb_origin`` — origin/qualifier markers (see :func:`_reused_origin`,
      :func:`_screened_conformer_origin`). Identifies non-standard rows
      so consumers don't mistake them for independently executed jobs.
    * ``final_settings`` — the final effective scientific/numerical
      settings of the calculation (e.g. ``{"fine": True}``). Distinct
      from ``level_of_theory.keywords``, which carries LoT-identity
      settings that participate in dedup hashing.

    ``existing`` lets callers fold both writers on top of a caller-
    provided base. Keys collide explicitly: the merge intentionally
    does NOT silently overwrite a pre-existing ``tckdb_origin`` /
    ``final_settings`` — that would mask bugs. Callers should pass
    only one source for each key.

    Returns ``None`` when the merged dict is empty, so the caller can
    omit the field entirely rather than emit an empty container.
    """
    merged: dict[str, Any] = dict(existing) if existing else {}
    if tckdb_origin:
        merged["tckdb_origin"] = dict(tckdb_origin)
    if final_settings:
        merged["final_settings"] = dict(final_settings)
    return merged or None


def _screened_conformer_origin(job: str = "opt") -> dict[str, Any]:
    """Build the ``tckdb_origin`` payload for an alt-conformer's opt row.

    TCKDB's ``ConformerInBundle`` requires every conformer to carry a
    ``primary_calculation`` of type ``opt``. For screened conformers
    that ARC didn't select, no parsed ESS opt log lands on the species
    record — we only have a converged geometry plus a relative kJ/mol
    energy from the conformer screen. This marker makes that explicit
    on the wire so downstream consumers can tell screened-conformer
    anchor rows apart from independently executed opt jobs.

    ``origin_kind`` maps to ``derived``: the backend hoists this value
    into the validated ``CalculationWithResultsPayload.origin_kind`` enum
    (``{executed, reused_result, imported, derived}``), and a screened
    conformer's geometry is *derived* from ARC's conformer screen rather
    than produced by an independently executed ESS opt job. The
    ARC-specific ``screened_conformer`` distinction is preserved verbatim
    under ``origin_detail`` (opaque to the backend enum) so downstream
    consumers can still single these rows out. Emitting
    ``origin_kind="screened_conformer"`` here previously caused a 422 —
    it is not an enum member.
    """
    return {
        "origin_kind": "derived",
        "origin_detail": "screened_conformer",
        "reason": (
            "alt conformer geometry anchored to the bundle; ARC did "
            "not parse an independent opt job for this conformer"
            if job == "opt" else
            "alt conformer single-point energy from ARC's conformer screen; "
            "ARC did not parse an independent sp job for this conformer"
        ),
        "independent_ess_job": False,
        "producer": "ARC",
    }


def _stated_integer(
    record: Mapping[str, Any],
    field: str,
    subject: str,
    *,
    minimum: int | None = None,
) -> int:
    """Return ARC's own integer ``record[field]``, or refuse the upload.

    Charge and multiplicity are required identity fields with no TCKDB
    default, and ARC writes both for every species and TS
    (arc/output.py ``_spc_to_dict``). A record without a usable value is
    malformed; before adapter 0.6.0 it silently became charge 0 or a
    singlet, which is a different species identity (a radical deposited as
    a singlet). Raise ``ValueError`` instead: the per-record build fails
    and the sweep reports it, like every other unbuildable record.
    """
    value = record.get(field)
    usable = (
        not isinstance(value, bool)
        and (isinstance(value, int) or (isinstance(value, float) and value.is_integer()))
        and (minimum is None or value >= minimum)
    )
    if not usable:
        raise ValueError(
            f"{subject}: ARC's record states no usable {field} ({value!r}). "
            f"TCKDB requires {field} and the adapter does not default it; "
            f"refusing to build this upload."
        )
    return int(value)


def _resolve_level(
    output_doc: Mapping[str, Any],
    job_kind: str,
    record: Mapping[str, Any] | None = None,
    *,
    job_type: str | None = None,
) -> Mapping[str, Any] | None:
    """Return the level-of-theory dict for ``job_kind`` ('opt'/'freq'/'sp').

    The opt level is always required by the primary calculation. For
    freq/sp, ARC users very often run all three at the same level and
    only declare ``opt_level=`` — ``output.yml`` then writes ``freq_level:
    null`` / ``sp_level: null``. To avoid silently dropping the freq/sp
    additional calculations in the common case, fall back to ``opt_level``
    when the job-specific level is absent. Scans require their own level;
    ARC can run them at a different method from optimization. ``neb`` is the
    ORCA NEB path-search level (``neb_level``, exported only when the
    ``orca_neb`` TS adapter was configured) and never falls back to the opt
    level either: the NEB runs at ``orca_neb_settings['level']``, not at any of
    the run's opt/freq/sp levels. A present-but-distinct
    ``freq_level`` / ``sp_level`` is treated as authoritative.

    The fallback follows ARC's own semantics (kept in adapter 0.6.0 by
    maintainer decision): ARC itself sets ``freq_level = opt_level`` when a
    frequency job is requested without one (arc/main.py
    ``set_levels_of_theory``), and a null ``sp_level`` means no sp job was
    requested, so ARC parses the species' electronic energy from the
    optimization log (arc/scheduler.py: ``parse_opt_e_elect`` is called
    when ``not self.job_types['sp']``); an ``sp_level`` equal to
    ``opt_level`` likewise reuses the optimization output (``run_sp_job``).
    output.yml's ``sp_energy_hartree`` is that ``e_elect``.

    Under ARC ``adaptive_levels`` (``output_doc`` marked by
    ``TCKDBAdapter._with_adaptive_levels``) a job type the adaptive levels name
    has a per-species level that output.yml does not record. With ``record``
    (the species or TS) and a ``restart.yml`` holding the adaptive spec and
    the species, ARC's own choice is replayed exactly
    (``tckdb_arc.adaptive.RestartInfo.attribute_level``); a job type the
    species' range does not name runs at the run's regular level, as
    everywhere else. Otherwise ``None`` is returned (and noted, for the
    warning) and the caller states no level. ``job_type`` names the ARC job
    type when a kind has several (``scan`` / ``directed_scan``). A null
    ``sp_level`` reuses the optimization's energy, so it follows the
    (attributed) opt level; a null ``freq_level`` beside an adaptive opt has
    no stated source and yields ``None``.
    """
    # Schema 1.3 states the level of each job whose log the record exports
    # (``levels``), also under ``adaptive_levels``: it is authoritative and
    # replaces the restart.yml replay. It states no ``software`` (the program is
    # ``ess_software``). What it leaves unstated falls through to the pre-1.3 rules.
    if record is not None and arc13.has_levels(record):
        recorded = arc13.recorded_level(record, job_kind)
        if recorded is not None:
            if (recorded.level is None and job_kind == "opt"
                    and arc13.primary_opt_placeholder(record) == "no_opt_job"):
                # ARC exports an sp/freq log but no opt job; the primary opt TCKDB
                # requires is filed as a marked placeholder at the header opt level.
                header = output_doc.get("opt_level")
                return header if isinstance(header, Mapping) else None
            return recorded.level
        if job_kind == "scan":
            # ARC exports no per-species scan level: the header ``scan_level`` is the
            # requested level of the run, ``null`` when rotor scans were not requested
            # or when an adaptive entry names ``scan`` (the level then depends on the
            # species).
            level = output_doc.get("scan_level")
            if not isinstance(level, Mapping) and _adaptive_kind_named(output_doc, "scan"):
                _note_adaptive_omission(output_doc, "scan", record)
            return level if isinstance(level, Mapping) else None
    if _adaptive_kind_named(output_doc, job_kind) if job_type is None else (
            job_type in (_adaptive_marker(output_doc) or {}).get("named", ())):
        types = (job_type,) if job_type else _JOB_TYPES_BY_KIND.get(job_kind, (job_kind,))
        status = (
            _adaptive_level_status(output_doc, record, types[0])
            if len(types) == 1 else UNDETERMINABLE
        )
        if isinstance(status, Mapping):
            return status
        if status == UNDETERMINABLE:
            _note_adaptive_omission(output_doc, job_kind, record)
            return None
        # RUN_LEVEL: the species' range does not name it; the run level applies.
    if job_kind in {"opt", "scan", "neb", "gsm"}:
        level = output_doc.get(f"{job_kind}_level")
        return level if isinstance(level, Mapping) else None
    job_level = output_doc.get(f"{job_kind}_level")
    if isinstance(job_level, Mapping):
        return job_level
    if job_kind == "freq" and _adaptive_kind_named(output_doc, "opt"):
        _note_adaptive_omission(output_doc, job_kind, record)
        return None
    return _resolve_level(output_doc, "opt", record)


def _opt_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    # opt_converged is emitted by arc/output.py::_spc_to_dict (line 541) and
    # accepted by TCKDB's OptResultPayload as ``converged: bool | None``.
    # Without this mapping the calc_opt_result.converged column lands NULL
    # even for known-converged species. Coerce to bool defensively — ARC
    # writes True/False but the schema is strict about the type.
    if record.get("opt_converged") is not None:
        out["converged"] = bool(record["opt_converged"])
    if record.get("opt_n_steps") is not None:
        out["n_steps"] = record["opt_n_steps"]
    if record.get("opt_final_energy_hartree") is not None:
        out["final_energy_hartree"] = record["opt_final_energy_hartree"]
    return out or None


def _coarse_opt_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Like :func:`_opt_result_payload` but reads the ``coarse_opt_*`` fields.

    The coarse opt is treated as ``converged=True`` whenever it ran to
    completion — output.yml only records ``coarse_opt_log`` after a
    successful coarse run (see ``_spc_to_dict`` line 555: ``if converged
    and coarse_path``). A separate ``coarse_opt_converged`` field would
    be slightly more honest, but ARC doesn't currently emit one and the
    convention "the coarse log only exists if it converged" holds.
    """
    out: dict[str, Any] = {}
    # The presence of the coarse log indicates a successful coarse stage.
    out["converged"] = True
    if record.get("coarse_opt_n_steps") is not None:
        out["n_steps"] = record["coarse_opt_n_steps"]
    if record.get("coarse_opt_final_energy_hartree") is not None:
        out["final_energy_hartree"] = record["coarse_opt_final_energy_hartree"]
    return out


_FREQ_FIELD_SPECS = (
    # (record_key, payload_key, coerce)
    ("freq_n_imag", "n_imag", int),
    ("imag_freq_cm1", "imag_freq_cm1", float),
    ("zpe_hartree", "zpe_hartree", float),
)


# ARC's own criterion for a transition state's *major* mode (the
# reaction coordinate): the unique imaginary mode whose magnitude falls
# strictly inside this window. Anything outside it is either numerical
# noise (too soft — a grid/optimisation artifact) or implausibly stiff
# (an SCF/parse artifact), per ``arc/checks/ts.py::
# check_imaginary_frequencies`` and ``arc/settings/settings.py``'s
# ``LOWEST_MAJOR_TS_FREQ`` / ``HIGHEST_MAJOR_TS_FREQ``.
_TS_MAJOR_MODE_MIN_CM1 = 75.0
_TS_MAJOR_MODE_MAX_CM1 = 10000.0


def _designate_reaction_coordinate_index(
    imaginary_values: list[float],
) -> int | None:
    """1-based position in ``imaginary_values`` of the unique major TS mode.

    The rule (largest-magnitude candidate inside the window, ``None`` on a tie or no
    candidate) is :func:`tckdb_core.rules.designate_reaction_coordinate_index`; the
    window is ARC's own, (75, 10000) cm-1, mirroring ``arc/checks/ts.py::
    check_imaginary_frequencies`` (``_TS_MAJOR_MODE_MIN_CM1`` / ``_TS_MAJOR_MODE_MAX_CM1``).
    """
    return _rules.designate_reaction_coordinate_index(
        imaginary_values, window_cm1=(_TS_MAJOR_MODE_MIN_CM1, _TS_MAJOR_MODE_MAX_CM1))


# TCKDB's noise floor for an imaginary mode when no protocol is recorded
# (tckdb_schemas.stationary_point.TAU_PROTOCOL_NOT_RECORDED_CM1): deliberately the
# finite-difference-from-gradients value, since assuming the better case would
# flag modes that are only noise. ARC states no tau, so this is used.
_TS_TAU_PROTOCOL_NOT_RECORDED_CM1 = _rules.TAU_PROTOCOL_NOT_RECORDED_CM1
_W_TS_REACTION_COORDINATE_NOT_DESIGNATED = ArcWarning.TS_REACTION_COORDINATE_NOT_DESIGNATED.value


class TSReactionCoordinateNotDesignated(ValueError):
    """A 1.3 TS whose reaction coordinate neither ARC nor tau can designate.

    ``warning`` is the structured record (code, message, field, context) of
    what the sidecar would carry; the message starts with its code so that a
    sweep, which only reports the exception text, names it too.
    """

    def __init__(self, *, label: str, n_imag: int, imaginary_cm1: list[float],
                 stated_index: Any, n_above_tau: int):
        reason = (
            "no imaginary mode is at or above tau" if n_above_tau == 0
            else f"{n_above_tau} imaginary modes are at or above tau")
        message = (
            f"[{_W_TS_REACTION_COORDINATE_NOT_DESIGNATED}] label={label!r} is a "
            f"transition state with {n_imag} imaginary modes {imaginary_cm1} cm-1; "
            f"ARC states no usable reaction_coordinate_mode_index "
            f"({stated_index!r}) and {reason} (tau = "
            f"{_TS_TAU_PROTOCOL_NOT_RECORDED_CM1:.0f} cm-1, TCKDB's value when no "
            f"protocol is recorded). TCKDB requires exactly one designated reaction "
            f"coordinate (transition_state_reaction_coordinate_not_designated) and "
            f"refuses to guess; refusing to deposit this record."
        )
        super().__init__(message)
        self.warning = _self_check(
            _W_TS_REACTION_COORDINATE_NOT_DESIGNATED, message, "transition_state.freq_result.reaction_coordinate_mode_index",
            {"action": "record_refused", "label": label, "imaginary_cm1": imaginary_cm1, "tau_cm1": _TS_TAU_PROTOCOL_NOT_RECORDED_CM1, "n_above_tau": n_above_tau})
        logger.warning("TCKDB %s: %s", _W_TS_REACTION_COORDINATE_NOT_DESIGNATED, message)


def _freq_result_payload(
    record: Mapping[str, Any], *, schema_1_3: bool = False,
) -> dict[str, Any] | None:
    """Build a FreqResultPayload-shaped dict from an output.yml record.

    ``schema_1_3`` (an output.yml 1.3 document): a TS record's
    ``freq_frequencies_cm1_ess_order`` lists every frequency of the job as the
    ESS printed it, imaginary modes (negative) in place, and
    ``reaction_coordinate_mode_index`` is the 1-based position in it of the mode
    ARC's normal-mode-displacement check validated as the reaction coordinate
    (``null`` unless that check genuinely passed). The ``modes`` are then that
    list, numbered by their position, and the designation is ARC's index, so
    neither the re-insertion of imaginary frequencies nor the (75, 10000) cm-1
    window of ``_designate_reaction_coordinate_index`` is needed (both remain
    for earlier documents). With no stated index, TCKDB's tau (50 cm-1 when no
    protocol is recorded) decides: exactly one imaginary mode at or above it is the
    reaction coordinate and the rest are ``unassigned``; otherwise the record is
    refused (``TSReactionCoordinateNotDesignated``).

    Returns ``None`` when no freq fields are populated. Returns ``None``
    and logs a warning when any present field cannot be coerced to the
    expected numeric type — the task spec mandates skipping the whole
    additional calculation rather than uploading a partial freq row.

    Raises ``ValueError`` on a genuine contradiction between ARC's own
    record and the stationary-point claim this freq result will be
    uploaded under. Callers deliberately do NOT catch it (unlike the
    ``ValueError`` a downstream calc builder can raise for e.g. a missing
    level of theory, which every call site wraps in a local
    try/except-and-skip): it is left to propagate out of the enclosing
    ``submit_*`` method, matching the documented "build failures raise;
    the caller wraps the per-record call in a try/except" contract those
    methods already advertise. That caller is ``sweep.py``, whose
    per-record ``except Exception`` counts the record ``failed`` and
    prints ``failed: <label> — <error>`` — a loud, attributable failure
    the operator cannot miss, unlike a ``logger.warning`` buried in the
    run log.

    TCKDB 0.22.0 cross-checks ``freq_n_imag`` against the stationary-
    point claim this record is uploaded under: species_entry_kind=
    'minimum' for every species/reactant/product path (``_species_entry_
    payload`` now refuses any record with ``is_ts=True`` before it can
    reach this function at all — see its own docstring — so 'minimum' is
    the only value that ever gets here), and "this is the
    transition_state block" for a TS. ``_species_entry_payload`` derives
    that claim from ARC's ``is_ts`` flag completely independently of the
    n_imag read here, so nothing guarantees they agree.

    No concrete path inside ARC's own pipeline is known to produce this
    contradiction. ``Scheduler.check_negative_freq`` (arc/scheduler.py)
    refuses to mark a non-TS species converged while any negative
    frequency remains, and refuses to mark a TS converged with zero. A
    species loaded from a pre-existing Arkane YAML
    (``species.yml_path``) looked like a candidate bypass —
    ``Scheduler.schedule_jobs`` sets ``self.output[label]['convergence']
    = True`` unconditionally for it ("Species is loaded from an Arkane
    YAML file (no need to execute any job)"), with no frequency sanity
    check at all — but it isn't one: ``ARCSpecies.from_yml_file``
    (arc/species/species.py) loads only ``final_xyz``, ``mol``,
    ``multiplicity``, ``charge``, and a handful of Arkane-conformer
    fields; it never sets ``spc.freqs``. ``_get_imaginary_freqs``
    (arc/output.py) reads only ``spc.freqs`` and ``spc.
    ts_guesses[chosen].imaginary_freqs``, neither of which the YAML load
    touches, so a YAML-loaded species reports ``freq_n_imag=None`` — the
    check below is skipped for it, not contradicted.

    These checks are therefore defensive validation for an ``output.yml``
    document this adapter did not produce — hand-written, third-party,
    or from a future/older ARC — which the adapter genuinely accepts:
    ``evidence.py::validate_output_schema`` checks only
    ``schema_version``, never the document shape, so nothing upstream
    would stop a record like that from reaching this function. If a
    concrete producer inside ARC's own pipeline is later found to trigger
    this, update this note with it; until then, treat these raises as
    precautionary rather than evidence of a live bug.

    Uploading the contradiction verbatim would 422 the whole payload
    (``n_imag_contradicts_minimum`` / ``transition_state_no_imaginary_
    mode`` / ``transition_state_reaction_coordinate_not_designated``).
    Quietly dropping just the offending field(s) is not a fix: it
    defeats TCKDB's own guard (``evaluate_transition_state_frequency``
    and ``evaluate_species_entry_frequency`` both report nothing when
    n_imag is absent — "absence is never contradiction") and deposits
    the record under a classification its own evidence disputes, with
    the disproving evidence quietly removed. Refusing the whole record
    is the honest alternative: nothing is deposited that asserts
    something ARC's own frequency evidence contradicts.
    """
    statmech = record.get("statmech") or {}
    raw_freqs = statmech.get("harmonic_frequencies_cm1")
    ess_order = record.get("freq_frequencies_cm1_ess_order") if (
        schema_1_3 and record.get("is_ts")) else None
    ess_order_used = isinstance(ess_order, list) and bool(ess_order)
    if ess_order_used:
        raw_freqs = ess_order
    has_modes_source = bool(raw_freqs)
    if not has_modes_source and all(
        record.get(rkey) is None for rkey, _, _ in _FREQ_FIELD_SPECS
    ):
        return None
    out: dict[str, Any] = {}
    for record_key, payload_key, coerce in _FREQ_FIELD_SPECS:
        value = record.get(record_key)
        if value is None:
            continue
        try:
            out[payload_key] = coerce(value)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "TCKDB freq additional calculation skipped for label=%s: "
                "malformed %s=%r (%s)",
                record.get("label"), record_key, value, exc,
            )
            return None

    label = record.get("label")
    is_ts = bool(record.get("is_ts"))
    n_imag = out.get("n_imag")

    if n_imag is not None and not is_ts and n_imag > 0:
        raise ValueError(
            f"TCKDB freq: label={label!r} is_ts=False (species_entry_kind="
            f"'minimum') but ARC reports freq_n_imag={n_imag} — TCKDB "
            f"0.22.0 refuses a minimum with an imaginary mode "
            f"(n_imag_contradicts_minimum). Refusing to deposit this "
            f"record rather than silently drop the disproving evidence. "
            f"No path inside ARC's own pipeline is known to produce this; "
            f"likely cause is a hand-written, third-party, or non-current-"
            f"ARC output.yml whose is_ts and frequency evidence disagree. "
            f"Re-optimise on a tighter integration grid, fix a stale "
            f"is_ts, or declare the entry as what it actually is."
        )
    if n_imag is not None and is_ts and n_imag <= 0:
        raise ValueError(
            f"TCKDB freq: label={label!r} is_ts=True (transition state) "
            f"but ARC reports freq_n_imag={n_imag} — TCKDB 0.22.0 refuses "
            f"a transition state with no imaginary mode "
            f"(transition_state_no_imaginary_mode). Refusing to deposit "
            f"this record rather than silently drop the disproving "
            f"evidence. No path inside ARC's own pipeline is known to "
            f"produce this; likely cause is a hand-written, third-party, "
            f"or non-current-ARC output.yml whose is_ts and frequency "
            f"evidence disagree. Re-run the saddle-point search, or "
            f"deposit the structure as a species entry."
        )

    modes: list[dict[str, Any]] = []
    if has_modes_source:
        try:
            for f in raw_freqs:
                freq = float(f)
                modes.append({"frequency_cm1": freq, "is_imaginary": freq < 0})
        except (TypeError, ValueError) as exc:
            logger.warning(
                "TCKDB freq additional calculation skipped for label=%s: "
                "malformed harmonic_frequencies_cm1=%r (%s)",
                label, raw_freqs, exc,
            )
            return None

    # ARC's statmech ``harmonic_frequencies_cm1`` lists only the REAL
    # vibrational modes for a TS (arc/output.py excludes every negative
    # frequency — not just the reaction coordinate — whenever ``spc.
    # is_ts``, and may be entirely absent even though imaginary
    # frequencies were recorded — see the ``ts_guesses`` fallback in
    # ``arc/output.py::_get_imaginary_freqs``). That is the common case,
    # but not the only one: a record can also already carry every
    # imaginary value inline in ``harmonic_frequencies_cm1`` (e.g. a
    # hand-written/foreign output.yml, or a producer that doesn't follow
    # ARC's REAL-modes-only convention). Re-insert the imaginary mode(s)
    # from whatever source is available only when none are present yet —
    # independent of whether ``has_modes_source`` — so ``modes`` is
    # internally consistent with ``n_imag``: the TCKDB FreqResultPayload
    # validator requires count(is_imaginary) == n_imag whenever both are
    # present.
    if n_imag and not any(m["is_imaginary"] for m in modes) and not ess_order_used:
        all_imaginary = record.get("imaginary_frequencies_cm1")
        imag = record.get("imag_freq_cm1")
        if isinstance(all_imaginary, (list, tuple)) and len(all_imaginary) == n_imag:
            try:
                imaginary_values = [float(f) for f in all_imaginary]
            except (TypeError, ValueError):
                imaginary_values = None
            if imaginary_values is not None:
                imaginary_modes = [
                    {"frequency_cm1": -abs(v), "is_imaginary": True}
                    for v in imaginary_values
                ]
                modes = imaginary_modes + modes
        elif imag is not None and n_imag == 1:
            # Older/malformed records without the plural field: fall back
            # to reinserting only the one scalar ARC reports. Only
            # reconciles n_imag == 1 — no designation is required there.
            try:
                modes = [{
                    "frequency_cm1": -abs(float(imag)),
                    "is_imaginary": True,
                }] + modes
            except (TypeError, ValueError):
                pass
        elif n_imag > 1:
            # No plural imaginary-frequency list, or its length disagrees
            # with n_imag: there is no honest way to designate a unique
            # reaction coordinate. Refuse rather than deposit an
            # undesignated multi-imaginary TS TCKDB would 422 anyway.
            raise ValueError(
                f"TCKDB freq: label={label!r} is a transition state with "
                f"freq_n_imag={n_imag} but ARC's "
                f"imaginary_frequencies_cm1={all_imaginary!r} does not "
                f"give exactly one value per imaginary mode, so the "
                f"reaction coordinate cannot be designated (TCKDB "
                f"0.22.0: transition_state_reaction_coordinate_not_"
                f"designated). Refusing to deposit this record rather "
                f"than guess."
            )

    # Designation is a separate concern from reinsertion above, and must
    # run whenever n_imag > 1 regardless of WHERE the imaginary mode(s) in
    # ``modes`` came from: reinserted just now, or already present in
    # ``harmonic_frequencies_cm1`` before this function ever ran.
    # Gating designation on "reinsertion happened" left a hole — a record
    # whose harmonic list already included every imaginary value (see the
    # comment above) skipped this block entirely and reached TCKDB with
    # ``modes`` populated but no ``reaction_coordinate_mode_index``,
    # tripping the exact ``transition_state_reaction_coordinate_not_
    # designated`` finding this function exists to prevent.
    reaction_coordinate_mode_index: int | None = None
    if ess_order_used:
        imaginary_entries = [
            (i, m) for i, m in enumerate(modes, start=1) if m["is_imaginary"]
        ]
        stated_index = record.get("reaction_coordinate_mode_index")
        index_is_usable = (
            isinstance(stated_index, int) and not isinstance(stated_index, bool)
            and 1 <= stated_index <= len(modes) and modes[stated_index - 1]["is_imaginary"]
        )
        if record.get("nmd_forced") is True:
            # ``nmd_forced`` (ARC #1059 ebc88ec8): the normal mode displacement check failed and
            # ``skip_nmd`` forced the pass. ARC states the index only for a genuine pass, so an index
            # beside ``nmd_forced: true`` is a contradiction, and the index is not a designation by
            # the check. TCKDB's tau rule below designates instead.
            if stated_index is not None:
                logger.warning(
                    "TCKDB freq: %r states reaction_coordinate_mode_index=%r and nmd_forced=true; "
                    "the index is not used as the NMD designation.", label, stated_index)
            index_is_usable = False
        if index_is_usable:
            reaction_coordinate_mode_index = stated_index
            # Every other imaginary mode is "extra" once one is designated
            # (ADR 0012); ARC does not say what it is, so it is declared
            # ``unassigned`` (see the pre-1.3 branch below for why).
            for i, m in imaginary_entries:
                if i != stated_index:
                    m["imaginary_disposition"] = "unassigned"
        elif n_imag and n_imag > 1:
            # ARC states no index (its normal mode displacement check did not
            # genuinely pass, e.g. a ``skip_nmd`` run, or no frequency matched).
            # TCKDB's own noise floor decides instead: with exactly one
            # imaginary mode at or above tau and every other below it, the
            # others are numerical noise and the one real mode is the reaction
            # coordinate. Two or more at or above tau is a genuine higher-order
            # saddle TCKDB blocks without a designation: refused.
            tau_index, n_above_tau = _rules.designate_by_tau(
                [(i, m["frequency_cm1"]) for i, m in imaginary_entries], n_imag)
            if tau_index is not None:
                reaction_coordinate_mode_index = tau_index
                for i, m in imaginary_entries:
                    if i != tau_index:
                        m["imaginary_disposition"] = "unassigned"
            else:
                values = [m["frequency_cm1"] for _, m in imaginary_entries]
                raise TSReactionCoordinateNotDesignated(
                    label=str(label), n_imag=n_imag, imaginary_cm1=values,
                    stated_index=stated_index, n_above_tau=n_above_tau,
                )
    elif n_imag and n_imag > 1:
        imaginary_entries = [
            (i, m) for i, m in enumerate(modes, start=1) if m["is_imaginary"]
        ]
        if len(imaginary_entries) == n_imag:
            # ADR 0012 requires exactly one designated reaction coordinate
            # whenever n_imag > 1. See
            # ``_designate_reaction_coordinate_index`` for why this is
            # ARC's own (75, 10000) cm-1 window plus largest-magnitude
            # tie-break among in-window candidates, not "most negative
            # wins" over every mode.
            designated_values = [m["frequency_cm1"] for _, m in imaginary_entries]
            designated_position = _designate_reaction_coordinate_index(designated_values)
            if designated_position is None:
                raise ValueError(
                    f"TCKDB freq: label={label!r} is a transition state "
                    f"with {n_imag} imaginary modes {designated_values} "
                    f"cm-1, and no single one is the unique major TS mode "
                    f"in ARC's own (75, 10000) cm-1 window "
                    f"(arc/checks/ts.py::"
                    f"check_imaginary_frequencies) — zero candidates "
                    f"qualify, or more than one ties for the largest "
                    f"magnitude. TCKDB 0.22.0 requires exactly one "
                    f"designated reaction coordinate "
                    f"(transition_state_reaction_coordinate_not_"
                    f"designated) and refuses to guess; refusing to "
                    f"deposit this record rather than pick one "
                    f"arbitrarily."
                )
            reaction_coordinate_mode_index = imaginary_entries[designated_position - 1][0]
            # Every OTHER imaginary mode is "extra" once one is designated
            # (ADR 0012), and stationary_point.py's ambiguity check blocks
            # whenever an undeclared extra is at least as stiff as the
            # designated one — which the excluded, out-of-window mode(s)
            # that motivated this window criterion often are (that is the
            # whole point: an SCF/parse artifact can be *more* negative
            # than the real reaction coordinate). Declare them
            # ``unassigned`` — ImaginaryModeDisposition's honest "I do not
            # know what this mode is, but it is not the reaction
            # coordinate" — rather than leave them undeclared and let a
            # stiffer artifact, or a smaller distinct-magnitude in-window
            # sibling, block the upload. ``unassigned`` still keeps ADR
            # 0012's structural flag (via the below-designation-magnitude/
            # tau warning path); it only lifts the hard block.
            for i, m in imaginary_entries:
                if i != reaction_coordinate_mode_index:
                    m["imaginary_disposition"] = "unassigned"
        # else: the imaginary-mode count found in ``modes`` doesn't match
        # n_imag (malformed/insufficient data). No honest designation is
        # possible; fall through to the reconciliation check below, which
        # omits ``modes`` entirely with a warning rather than guess.

    imag_count = sum(1 for m in modes if m["is_imaginary"])
    if n_imag == 0 and imag_count > 0:
        # is_ts=True with n_imag<=0 already raised above, so reaching
        # here with n_imag==0 means is_ts=False (species_entry_kind=
        # 'minimum'). ARC's own harmonic_frequencies_cm1 shows an
        # imaginary mode that freq_n_imag=0 says doesn't exist — the same
        # n_imag_contradicts_minimum contradiction guarded above, just
        # surfaced through the frequency list instead of the freq_n_imag
        # field itself. Quietly stripping the negative value(s) and
        # depositing freq_n_imag=0 anyway would defeat TCKDB's guard with
        # the disproving evidence removed — the same failure mode this
        # function's docstring rejects for every other contradiction.
        raise ValueError(
            f"TCKDB freq: label={label!r} is_ts=False (species_entry_kind="
            f"'minimum') reports freq_n_imag=0, but ARC's own "
            f"statmech.harmonic_frequencies_cm1 contains {imag_count} "
            f"negative value(s) "
            f"{[m['frequency_cm1'] for m in modes if m['is_imaginary']]} — "
            f"TCKDB 0.22.0 refuses a minimum with an imaginary mode "
            f"(n_imag_contradicts_minimum). Refusing to deposit this "
            f"record rather than silently drop the disproving evidence."
        )
    if n_imag is not None and imag_count != n_imag:
        # Cannot reconcile modes with n_imag (e.g. a higher-order saddle
        # with a single stored imag_freq_cm1, or malformed statmech data).
        # Emit the scalar n_imag/imag_freq_cm1 without ``modes`` rather
        # than a payload the backend validator would reject.
        logger.warning(
            "TCKDB freq modes omitted for label=%s: could not reconcile "
            "n_imag=%s with %d imaginary mode(s) from statmech.",
            label, n_imag, imag_count,
        )
    elif modes:
        for i, m in enumerate(modes, start=1):
            m["mode_index"] = i
        out["modes"] = modes
        if reaction_coordinate_mode_index is not None:
            out["reaction_coordinate_mode_index"] = reaction_coordinate_mode_index
            # Keep the scalar in agreement with the designation: without
            # this, ``imag_freq_cm1`` can carry ARC's raw min() (which may
            # be an out-of-window artifact, e.g. -12000.0) while
            # ``reaction_coordinate_mode_index`` names a different mode
            # (e.g. -1320.5) — two different "the reaction coordinate"
            # answers in the same payload. TCKDB does not cross-check
            # them, but ``imag_freq_cm1`` is documented as "Value of the
            # imaginary frequency" and is what a tunneling consumer reads.
            designated_mode = next(
                m for m in modes if m["mode_index"] == reaction_coordinate_mode_index
            )
            out["imag_freq_cm1"] = designated_mode["frequency_cm1"]
    return out or None


def _sp_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    # ``sp_energy_hartree`` is the only key ARC's output.yml writes for a
    # species' single-point energy; the TCKDB-side name
    # ``electronic_energy_hartree`` is not an ARC key and is not read.
    record_key = "sp_energy_hartree"
    energy = record.get(record_key)
    if energy is None:
        return None
    try:
        return {"electronic_energy_hartree": float(energy)}
    except (TypeError, ValueError) as exc:
        logger.warning(
            "TCKDB sp additional calculation skipped for label=%s: "
            "malformed %s=%r (%s)",
            record.get("label"), record_key, energy, exc,
        )
        return None


def _spin_diagnostic_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the TCKDB ``SpinDiagnosticPayload`` dict for a species/TS sp calc.

    Reads the ``sp_spin_diagnostic`` block that ``arc/output.py`` writes into
    ``output.yml`` for open-shell/unrestricted single points (parsed ``<S**2>``).
    Emits the block ONLY when a finite ``s_squared`` is present; restricted /
    closed-shell records carry ``sp_spin_diagnostic=None`` (or omit the key),
    for which this returns ``None`` and the caller leaves ``spin_diagnostic``
    unset — never an all-null or fabricated block.

    The ``s_squared`` field is required by the backend schema; the two
    companion fields (``s_squared_expected`` / ``s_squared_annihilated``) are
    optional and are only included when present and finite. All values are
    clamped to ``ge=0`` by construction of the source parser (S(S+1) and
    ``<S**2>`` are non-negative); a defensively-negative value is dropped.
    """
    block = record.get("sp_spin_diagnostic")
    if not isinstance(block, Mapping):
        return None
    s_squared = block.get("s_squared")
    if s_squared is None:
        return None
    try:
        s_squared = float(s_squared)
    except (TypeError, ValueError):
        return None
    if s_squared < 0:
        return None
    payload: dict[str, Any] = {"s_squared": s_squared}
    for optional in ("s_squared_expected", "s_squared_annihilated"):
        value = block.get(optional)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        payload[optional] = value
    return payload


def _wavefunction_diagnostic_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``WavefunctionDiagnosticPayload`` for an sp calc from ``sp_t1_diagnostic``, or ``None``.

    Only a finite, non-negative number is sent (TCKDB: ``t1_diagnostic >= 0``);
    ARC writes ``null`` for every level that is not coupled-cluster or QCISD and
    when the log prints none, and an all-null block is refused by TCKDB, so none
    is built then.
    """
    t1 = record.get("sp_t1_diagnostic")
    if isinstance(t1, bool) or not isinstance(t1, (int, float)):
        return None
    t1 = float(t1)
    if not math.isfinite(t1) or t1 < 0:
        return None
    return {"t1_diagnostic": t1}


# ARC's ``wavefunction_stability.verdict`` (arc/parser/adapters gaussian.py and
# orca.py, documented at arc/output.py ``_parse_wavefunction_stability``) onto
# the contract's ``scf_stability.status``. ``'unknown'`` means an analysis ran
# whose verdict ARC could not read, which is the contract's ``inconclusive``
# ("clearly attempted but its result could not be parsed"). Any verdict not
# listed here is not sent: the adapter never guesses a status.
_ARC_STABILITY_VERDICT_TO_STATUS = {
    "stable": "stable",
    "internal_instability": "unstable",
    "external_instability": "unstable",
    "unattributed_instability": "unstable",
    "unknown": "inconclusive",
}
_ARC_STABILITY_INSTABILITY_TYPE = {
    "internal_instability": "internal",
    "external_instability": "external",
}


def _scf_stability_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the primary opt's ``scf_stability``, or ``None``.

    ARC runs its wavefunction stability analysis once per species, from the
    optimization job: at the optimization level, on the geometry that
    optimization converged to, with the orbitals it wrote
    (``arc/scheduler.py::run_stability_job``), so that its SCF reproduces the
    wavefunction under test. ``wavefunction_stability`` describes that
    wavefunction, the opt's, and the block goes on the opt calculation only.
    A freq or sp job runs its own SCF, which may land on another solution, so
    the analysis is not an observation of theirs.

    Nothing is sent when ARC's own record shows the opt sent is not the one
    tested: ``scf_reference.source == 'derived'`` (ARC adopted the verdict and
    re-optimized at another reference, ``arc/output.py::_scf_reference_block``)
    or ``measured_on_ts_guess`` (the verdict was measured on an abandoned TS
    guess).

    ``stable`` is sent only for ARC's ``'stable'`` verdict, and not when the ORCA
    reader reports it followed an instability to a stable solution
    (``followed_to_stable``), which contradicts a stable verdict.
    ``lowest_eigenvalue`` is ARC's own (the tested wavefunction's). ARC exports
    no instability count, and ``followed_to_stable`` is not mapped to
    ``stabilized``: the tested wavefunction stayed unstable and no
    re-optimization on its solution is recorded.
    """
    stability = record.get("wavefunction_stability")
    scf_reference = record.get("scf_reference")
    if not isinstance(stability, Mapping) or not isinstance(scf_reference, Mapping):
        return None
    status = _ARC_STABILITY_VERDICT_TO_STATUS.get(stability.get("verdict"))
    if status is None:
        return None
    if scf_reference.get("measured_on_ts_guess") is not None:
        return None
    if scf_reference.get("source") == "derived":
        return None
    if status == "stable" and stability.get("followed_to_stable") is True:
        return None
    out: dict[str, Any] = {"status": status}
    lowest = stability.get("lowest_eigenvalue")
    if isinstance(lowest, (int, float)) and not isinstance(lowest, bool) and math.isfinite(lowest):
        out["lowest_eigenvalue"] = float(lowest)
    instability_type = _ARC_STABILITY_INSTABILITY_TYPE.get(stability.get("verdict"))
    if instability_type is not None:
        out["instability_type"] = instability_type
    return out


_APPLIED_CORRECTION_COMPONENT_FIELDS = (
    "component_kind", "key", "multiplicity", "parameter_value", "contribution_value",
)

# TCKDB's LevelOfTheoryRef primary-key set — used by _level_keys_match
# for conservative LoT equality. Note the names here are TCKDB's
# (post-translation), not ARC's (output.yml writes `auxiliary_basis` /
# `cabs`); _level_keys_match compares projected dicts.
_TCKDB_LOT_REF_FIELDS = ("method", "basis", "aux_basis", "cabs_basis")

# Field-name translation from ARC's Level (output.yml shape) to TCKDB's
# LevelOfTheoryRef. ARC's Level.as_dict() emits `auxiliary_basis` / `cabs` /
# `solvation_method`; TCKDB's LoT uses `aux_basis` / `cabs_basis` /
# `solvent_model`. `software` / `software_version` belong on
# `software_release`, not the LoT, and are intentionally dropped here.
# `method_type` / `year` / `solvation_scheme_level` / `compatible_ess` have
# no TCKDB LoT counterpart and are also dropped.
_ARC_TO_TCKDB_LOT_FIELDS = {
    "method": "method",
    "basis": "basis",
    "auxiliary_basis": "aux_basis",
    "cabs": "cabs_basis",
    "dispersion": "dispersion",
    "solvent": "solvent",
    "solvation_method": "solvent_model",
}


def _arc_args_to_keywords(args: Any) -> str | None:
    """Flatten ARC's nested ``args`` dict to TCKDB's flat ``keywords`` string.

    ARC stores ESS/runtime options as a nested mapping, commonly with
    categories such as ``keyword`` and ``block``. TCKDB stores the projected
    level-of-theory options in a single deterministic string that participates
    in ``lot_hash`` deduplication.

    The serialization is intentionally category-prefixed and sorted so that
    equivalent dictionaries produce identical strings regardless of insertion
    order.
    """
    if not isinstance(args, Mapping):
        return None

    parts: list[str] = []

    for category in sorted(args):
        entries = args.get(category)
        if not isinstance(entries, Mapping) or not entries:
            continue

        for key in sorted(entries):
            value = entries[key]
            if value is None:
                continue

            value_text = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            parts.append(f"{category}:{key}={value_text}")

    if not parts:
        return None

    return "; ".join(parts)


_W_LEVEL_METHOD_IS_COMPOUND = ArcWarning.LEVEL_METHOD_IS_COMPOUND.value
_W_CORRECTION_TABLE_METHOD_SPLIT = ArcWarning.CORRECTION_TABLE_METHOD_SPLIT.value


def _arc_level_to_tckdb_lot(
    level: Mapping[str, Any] | None, *, split_table: bool = True,
) -> dict[str, Any] | None:
    """Project ARC's per-job level dict (output.yml shape) onto TCKDB's
    ``LevelOfTheoryRef`` shape, applying field-name translation and
    flattening ``args`` into ``keywords``.

    A method that names a correction table (``cbs-qb3-paraskevas``, which ARC accepts as a
    composite method and runs as CBS-QB3) is sent as its stem (``cbs-qb3``): tckdb-schemas
    0.67 would store the table name as a separate level of theory. ``split_table=False``
    keeps the string, for a scheme level whose caller names the table on the scheme.

    Returns ``None`` if ``level`` is missing or has no ``method`` —
    callers decide whether to error or skip.
    """
    if not isinstance(level, Mapping):
        return None
    if not level.get("method"):
        return None
    if "//" in str(level["method"]):
        # ``energy//geometry`` is two levels of theory, not one method (tckdb-schemas
        # 0.67 refuses it: ``level_of_theory_method_is_compound``). ARC splits the shorthand
        # when it reads it (arc/main.py), so a compound method here is a hand-edited or
        # foreign document; which half belongs to this job is not stated, so the level is
        # refused rather than forwarded or guessed.
        logger.warning(
            "TCKDB %s: level method %r contains '//' (two levels of theory); the level is "
            "not sent, since which half belongs to this calculation is not stated.",
            _W_LEVEL_METHOD_IS_COMPOUND, str(level["method"]),
        )
        return None
    out: dict[str, Any] = {}
    for src, dst in _ARC_TO_TCKDB_LOT_FIELDS.items():
        v = level.get(src)
        if v:
            out[dst] = str(v)
    keywords = _arc_args_to_keywords(level.get("args"))
    if keywords:
        out["keywords"] = keywords
    if split_table:
        stem = correction_table_method_stem(out["method"])
        if stem is not None:
            out["method"] = stem
    return out


def _scheme_level_of_theory(scheme: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project ARC's per-species scheme.level_of_theory dict onto TCKDB's
    ``LevelOfTheoryRef`` shape. The scheme dict comes from the same
    ``_level_to_dict(arkane_level_of_theory)`` producer as opt/freq/sp
    levels, so the same field-name translation applies."""
    return _arc_level_to_tckdb_lot(scheme.get("level_of_theory"), split_table=False)


#: Scheme kinds Arkane keys on ``CompositeLevelOfTheory(freq=..., energy=...)``.
#: Atom-energy schemes are keyed on the energy level alone and never carry a frequency level.
_FREQ_KEYED_SCHEME_KINDS = frozenset({"bac_petersson", "bac_melius"})


def _bac_frequency_level(
    output_doc: Mapping[str, Any], record: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """ARC's stated frequency level behind a record's BAC (``scheme.frequency_level_of_theory``).

    Arkane keys a Petersson or Melius BAC on ``energy//freq`` and ARC's correction
    record names the energy half only (``level_of_theory``), so the frequency half
    is the level of the record's frequency job, and only when ARC states it:

    * an output 1.3 record: ``levels.freq``. A record whose frequencies are the
      composite job's own (``levels.freq`` null by design, or a ``freq_log`` that is
      the composite log) names none, so none is sent;
    * an earlier record: the header ``freq_level``, only when the adapter attributes
      it to this record's frequency calculation that way (``_resolve_level``: not
      under ``adaptive_levels``, where the level of a job type the adaptive levels
      name is per species and not exported). A null header ``freq_level`` (ARC wrote
      it null because the frequencies share the opt level) states nothing about the
      BAC, so none is sent: the opt level is not substituted.

    ``None`` means no frequency level is stated; the scheme is then sent without one,
    as before 0.10.
    """
    if record is None:
        return None
    if arc13.has_levels(record):
        recorded = arc13.recorded_level(record, "freq")
        if recorded is None or recorded.level is None or recorded.job_key == "composite":
            return None
        return recorded.level
    header = output_doc.get("freq_level")
    if not isinstance(header, Mapping) or not header.get("method"):
        return None
    if _adaptive_kind_named(output_doc, "freq") or _adaptive_kind_named(output_doc, "opt"):
        return None
    return header


_ARKANE_COMPOSITE_KEY_RE = re.compile(
    r"^\s*CompositeLevelOfTheory\(\s*freq\s*=\s*LevelOfTheory\((?P<freq>[^()]*)\)\s*,"
    r"\s*energy\s*=\s*LevelOfTheory\((?P<energy>[^()]*)\)\s*\)\s*$",
    re.DOTALL,
)
_ARKANE_KEY_FIELD_RE = re.compile(r"\s*(?P<name>[A-Za-z_]+)\s*=\s*'(?P<value>[^']*)'\s*(?:,|$)")
_W_BAC_FREQUENCY_LEVEL_CONFLICT = ArcWarning.BAC_FREQUENCY_LEVEL_CONFLICT.value


def _arkane_key_half(body: str) -> dict[str, str] | None:
    """Parse the ``name='value',...`` body of one ``LevelOfTheory(...)`` into a dict, or ``None``."""
    fields: dict[str, str] = {}
    pos = 0
    body = body.strip()
    while pos < len(body):
        m = _ARKANE_KEY_FIELD_RE.match(body, pos)
        if m is None or m.group("name") in fields:
            return None
        fields[m.group("name")] = m.group("value")
        pos = m.end()
    return fields or None


def _arkane_composite_key_halves(matched_arkane_key: Any) -> dict[str, dict[str, str]] | None:
    """``{"freq": {...}, "energy": {...}}`` for a ``CompositeLevelOfTheory(freq=..., energy=...)`` key.

    ``None`` for any other key (a single ``LevelOfTheory``, no key, an unparseable string).
    """
    if not isinstance(matched_arkane_key, str):
        return None
    shape = _ARKANE_COMPOSITE_KEY_RE.match(matched_arkane_key)
    if shape is None:
        return None
    freq, energy = _arkane_key_half(shape.group("freq")), _arkane_key_half(shape.group("energy"))
    if not freq or not energy or not freq.get("method"):
        return None
    return {"freq": freq, "energy": energy}


def _norm_level_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _bac_key_frequency_level(
    matched_arkane_key: Any,
    arc_freq_level: Mapping[str, Any] | None,
    *,
    kind: str,
    warning_field: str,
    warnings: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """The ``frequency_level_of_theory`` of a BAC scheme: the freq half of Arkane's composite key.

    tckdb-schemas 0.66: only a scheme Arkane keys on ``CompositeLevelOfTheory(freq=..., energy=...)``
    carries a frequency level; "for an atom-energy scheme or a scheme keyed on one level, send
    nothing new". So the record's ``matched_arkane_key`` decides: a single-level key, no key or an
    unparseable one sends none. For a composite key the level is the key's freq half (the table's
    own key, not ARC's job level). If ARC states a frequency-job level (``arc_freq_level``) that
    disagrees with the key's freq half (method, basis, and software when both state it), which one
    keys the table is unclear, so none is sent and a warning says so.
    """
    halves = _arkane_composite_key_halves(matched_arkane_key)
    if halves is None:
        return None
    key_freq = halves["freq"]
    if isinstance(arc_freq_level, Mapping) and arc_freq_level.get("method"):
        mismatched = [
            f for f in ("method", "basis", "software")
            if f in key_freq and arc_freq_level.get(f)
            and _norm_level_token(key_freq[f]) != _norm_level_token(arc_freq_level.get(f))
        ]
        if mismatched:
            arc_stated = {k: arc_freq_level.get(k) for k in ("method", "basis", "software")}
            message = (
                f"The {kind} table's Arkane key {matched_arkane_key!r} has frequency level "
                f"{key_freq!r}, but ARC states the frequency job ran at {arc_stated!r} "
                f"(differs in {', '.join(mismatched)}). Which one keys the table is unclear, so "
                f"scheme.frequency_level_of_theory is omitted."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_BAC_FREQUENCY_LEVEL_CONFLICT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_BAC_FREQUENCY_LEVEL_CONFLICT, message, f"{warning_field}.scheme.frequency_level_of_theory",
                    {"action": "scheme_frequency_level_omitted", "scheme_kind": kind, "arkane_key_freq": dict(key_freq), "arc_freq_level": arc_stated}))
            return None
    return _arc_level_to_tckdb_lot(key_freq)


_ARKANE_KEY_RE = re.compile(r"^\s*LevelOfTheory\((?P<body>.*)\)\s*$", re.DOTALL)
_ARKANE_KEY_SOFTWARE_RE = re.compile(r"(?:^|,)\s*software\s*=\s*'(?P<name>[A-Za-z0-9_.+-]+)'\s*(?=,|$)")
_W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT = ArcWarning.ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT.value


def _arkane_key_software(matched_arkane_key: Any) -> str | None:
    """The ``software='<name>'`` in Arkane's matched database key, or ``None``.

    ARC records the key as the entry's repr, e.g.
    ``LevelOfTheory(method='b3lyp2023',basis='def2tzvp',software='gaussian')``
    (arc/output.py ``matched_arkane_key``). Only a key of that shape with
    exactly one clean, single-quoted ``software`` token counts; anything
    else (no key, a key without software, an unparseable string) is
    ``None``, never a guess.
    """
    if not isinstance(matched_arkane_key, str):
        return None
    shape = _ARKANE_KEY_RE.match(matched_arkane_key)
    if shape is None:
        return None
    names = [m.group("name") for m in _ARKANE_KEY_SOFTWARE_RE.finditer(shape.group("body"))]
    if len(names) != 1 or shape.group("body").count("software") != 1:
        return None
    return names[0]


_W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML = ArcWarning.ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML.value
_W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE = ArcWarning.BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE.value


def _bond_corrections_flag(record: Mapping[str, Any]) -> bool | None:
    """``thermo.bond_corrections_applied`` (output.yml 1.2) when a bool, else None."""
    thermo = record.get("thermo")
    flag = thermo.get("bond_corrections_applied") if isinstance(thermo, Mapping) else None
    return flag if isinstance(flag, bool) else None


_W_BAC_TYPE_NOT_STATED = ArcWarning.BAC_TYPE_NOT_STATED.value


def _warn_bac_type_not_stated(
    output_doc: Mapping[str, Any], record: Mapping[str, Any], warnings: list[dict[str, Any]] | None,
) -> None:
    """Warn when a thermo says bond corrections were applied but the header ``bac_type`` is null.

    Output 1.3 at ARC #1059 ebc88ec8 requires ``bac_type`` to be ``p`` or ``m`` whenever a thermo
    (or statmech) says bond corrections were applied, so ``bond_corrections_applied: true`` beside a
    null ``bac_type`` is a document ARC's own schema refuses. It only warns: the BAC scheme is still built
    from the record's ``energy_corrections``, and ``bac_type`` is never inferred from them.
    """
    if _bond_corrections_flag(record) is not True or output_doc.get("bac_type") in ("p", "m"):
        return
    message = (
        f"{record.get('label')!r}: thermo.bond_corrections_applied is true but the header bac_type is "
        f"{output_doc.get('bac_type')!r}; ARC's output schema requires 'p' or 'm' then. The bond "
        "additivity correction scheme is taken from the record's energy_corrections as before, and bac_type "
        "is not inferred."
    )
    logger.warning("TCKDB %s: %s", _W_BAC_TYPE_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_BAC_TYPE_NOT_STATED, message, "thermo",
            {"action": "none_scheme_from_energy_corrections", "bac_type": str(output_doc.get("bac_type"))}))


def _note_omitted_bac_on_thermo(
    thermo_block: dict[str, Any] | None,
    reasons: list[str],
    bond_corrections_applied: Any = None,
) -> None:
    """Record on the thermo ``note`` that an exported BAC is not deposited.

    Arkane skips bond types missing from its Petersson table and applies the
    rest, so the thermo enthalpy can include a partial BAC while the deposit
    holds no applied correction. ``bond_corrections_applied`` is output.yml
    1.2's ``thermo.bond_corrections_applied``: True asserts Arkane applied it;
    False means nothing is missing from the deposit, so no note; absent (None)
    keeps the inference and words the note as what ARC exported. Appended,
    never overwriting.
    """
    if thermo_block is None or not reasons or bond_corrections_applied is False:
        return
    if reasons[0] == "no_bond_had_parameter":
        text = (
            "ARC exported a Petersson bond additivity correction total for this species, "
            "but no bond had a parameter in Arkane's table, so no applied correction is "
            "deposited.")
        existing = thermo_block.get("note")
        thermo_block["note"] = f"{existing}; {text}" if existing else text
        return
    if bond_corrections_applied is True:
        subject = "A Petersson bond additivity correction was applied by Arkane to this species"
    else:
        subject = "ARC exported a Petersson bond additivity correction total for this species"
    text = (
        f"{subject} but it is not deposited as an applied correction because its "
        f"bond decomposition was incomplete ({reasons[0]})."
    )
    existing = thermo_block.get("note")
    thermo_block["note"] = f"{existing}; {text}" if existing else text


def _component_is_usable(component: Any) -> bool:
    return (
        isinstance(component, Mapping)
        and component.get("parameter_value") is not None
        and component.get("contribution_value") is not None
    )


# kcal/mol. ARC computes a component as ``count * parameter`` and the total from
# Arkane, so a real record agrees to float round-off; this admits only the
# rounding of a hand-copied value, and is far below the contribution of any bond
# a decomposition could be missing.
_BAC_COMPONENT_SUM_TOLERANCE = 1e-3


def _petersson_bac_omission_reason(
    rec: Mapping[str, Any],
    components: Any,
    *,
    target_kind: str,
    element_symbols: Any,
) -> tuple[str, str] | None:
    """``(reason, detail)`` when a Petersson ``bac_total`` must not be sent.

    TCKDB's ``assert_bac_total_has_required_components`` refuses a
    ``bac_petersson`` total with no component of kind ``bond`` when it
    targets a transition state or a species with at least one bond; only a
    monatomic species has an honest componentless total. It does not check
    that the components sum to the total, so a partial decomposition (a
    component missing ``parameter_value``/``contribution_value``) would be
    stored silently. ARC itself drops the whole list when any bond lacks a
    parameter. ``None`` means send it (also for every other role or kind:
    ``bac_melius`` is exempt on TCKDB's side).
    """
    scheme = rec.get("scheme")
    if rec.get("application_role") != "bac_total" or not isinstance(scheme, Mapping):
        return None
    if scheme.get("kind") != "bac_petersson":
        return None
    items = components if isinstance(components, list) else []
    applied_bonds = rec.get("components_are_applied_bonds") is True
    if any(not _component_is_usable(c) for c in items):
        return ("component_unusable",
                "a bond component lacks a parameter or contribution value, so the "
                "decomposition would not sum to the total.")
    has_bond = any(c.get("component_kind") == "bond" for c in items)
    if has_bond:
        if applied_bonds:
            # Output 1.3 states that the components are the bonds Arkane applied
            # and that they sum to the total, so a partial BAC (bonds with no
            # parameter listed as ``skipped_components``) is a complete, exact
            # decomposition of what was applied. TCKDB does not check the sum,
            # so verify what ARC promises before sending it.
            try:
                contributions = sum(float(c["contribution_value"]) for c in items)
                total = float(rec["value"])
            except (TypeError, ValueError, KeyError):
                contributions = total = math.nan
            if not math.isclose(contributions, total, rel_tol=1e-6, abs_tol=_BAC_COMPONENT_SUM_TOLERANCE):
                return ("components_do_not_sum",
                        f"the bond components sum to {contributions!r} but the total is "
                        f"{rec.get('value')!r}, although ARC states that they sum to it.")
        return None
    monatomic = (
        target_kind == "species"
        and isinstance(element_symbols, (list, tuple))
        and len(element_symbols) == 1
    )
    if monatomic:
        return None
    if not items:
        if applied_bonds:
            return ("no_bond_had_parameter",
                    "the record lists no bond Arkane applied (every bond of the "
                    "species is in skipped_components, or none is listed).")
        return ("no_components",
                "the record carries no bond components (ARC drops them all when a "
                "bond has no parameter in Arkane's Petersson table).")
    return ("no_bond_component", "the record carries no component of kind 'bond'.")


def _build_applied_energy_corrections(
    applied_records: Any,
    *,
    source_calculation_key: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "applied_energy_corrections",
    target_kind: Literal["species", "transition_state"] = "species",
    element_symbols: Any = None,
    target_label: str | None = None,
    arkane_release: Mapping[str, Any] | None = None,
    omitted_bac_reasons: list[str] | None = None,
    scheme_data_revisions: Mapping[str, str] | None = None,
    aec_yml_digest: str | None = None,
    bac_frequency_level: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Translate ``output.yml`` per-species ``applied_energy_corrections``
    into the TCKDB ``AppliedEnergyCorrectionUploadPayload`` shape.

    The output.yml shape is already very close to the upload schema (same
    ``application_role`` / ``value`` / ``value_unit`` / ``scheme`` fields),
    so this is mostly a passthrough plus three small adaptations:

    1. Drop output.yml-only fields from components (e.g. ``parameter_unit``)
       — TCKDB's ``AppliedCorrectionComponentPayload`` rejects unknowns.
    2. Drop component rows whose ``parameter_value`` is null — the upload
       schema requires a real number, and the producer marks them null
       precisely when reconstruction wasn't reliable.
    3. Attach ``source_calculation_key`` when the caller has resolved it
       to a real SP key in this bundle. AEC and BAC are corrections to the
       electronic-energy reference, so SP is the right anchor; the field
       is omitted when no key is supplied rather than guessed.

    The caller resolves the SP key against the relevant namespace —
    bundle-global (``"sp"``) for computed-species, scoped (``"r0_sp"`` /
    ``"p0_sp"`` / ``"ts_sp"``) for computed-reaction. The helper does
    not know about modes; it just takes the resolved key (or ``None``)
    and stamps it on every emitted entry.

    A Petersson ``bac_total`` is only sent with a complete bond
    decomposition (see ``_petersson_bac_omission_reason``): TCKDB refuses
    the whole upload for a componentless one (``bac_total_requires_components``)
    and does not check that a partial decomposition sums to the total. When
    the decomposition is unusable the BAC correction alone is omitted, with a
    ``bac_correction_omitted_components_incomplete`` warning; the AEC
    correction and the rest of the payload are unaffected. ``target_kind``
    and ``element_symbols`` (the species' composition, ``None`` when
    unknown) say whether a componentless total is honest: only a monatomic
    species has no bond to decompose. ``omitted_bac_reasons`` collects the
    reason of each omitted BAC so the caller can note it on the thermo record.

    ``bac_frequency_level`` (see ``_bac_frequency_level``) is ARC's stated frequency level
    behind a BAC; it is sent as ``scheme.frequency_level_of_theory`` on ``bac_petersson`` and
    ``bac_melius`` schemes that carry a ``level_of_theory`` (tckdb-schemas 0.66: the field is
    refused on any other kind and without the energy level) and never on an atom-energy
    scheme. It joins the scheme's identity: one new scheme row per BAC scheme the adapter
    sent before without it.

    ``arkane_release`` (see ``_arkane_workflow_tool_release``) is stamped as
    ``scheme.workflow_tool_release`` on ``atom_energy``, ``bac_petersson`` and
    ``bac_melius`` schemes, the kinds built from Arkane's tables. An output.yml
    1.3 document also identifies the tables themselves (the RMG-database
    ``quantum_corrections/data.py`` Arkane loaded); ``scheme_data_revisions`` (see
    ``_scheme_data_revisions``) then names that revision per scheme kind as
    ``scheme.data_revision`` (tckdb-schemas 0.62), which joins the scheme's
    identity and makes the Arkane build provenance only.

    ``aec_yml_digest`` (output 1.3's ``arc_aec_yml_sha256``, see
    ``_arc_aec_yml_digest``) says ARC rendered Arkane's ``atomEnergies`` from its own
    ``data/AEC.yml``. ARC recomputes the exported ``atom_energy`` record from
    ``data.py`` regardless, so for a level both files cover the record would
    describe a table other than the one Arkane applied: that record is then not
    deposited (``atom_energy_record_not_deposited_aec_yml``).
    """
    if not isinstance(applied_records, list):
        return []

    out: list[dict[str, Any]] = []
    for rec in applied_records:
        if not isinstance(rec, Mapping):
            continue
        if rec.get("value") is None or rec.get("scheme") is None:
            continue

        if aec_yml_digest and rec["scheme"].get("kind") == "atom_energy":
            message = (
                f"The atom_energy correction was not sent for "
                f"{target_label or 'this target'}: ARC rendered Arkane's atom energies "
                f"from its own data/AEC.yml (sha256 {aec_yml_digest}) but recomputes "
                f"the exported record from the RMG-database data.py, so the record may "
                f"describe a table other than the one Arkane applied.")
            logger.warning("TCKDB %s: %s: %s", warning_field,
                           _W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML, message, warning_field,
                    {"action": "atom_energy_correction_omitted", "arc_aec_yml_sha256": aec_yml_digest, "species": target_label}))
            continue
        components_in = rec.get("components") or []
        omission = _petersson_bac_omission_reason(
            rec, components_in,
            target_kind=target_kind, element_symbols=element_symbols,
        )
        if omission is not None:
            reason, detail = omission
            message = (
                f"The Petersson bac_total was not sent for "
                f"{target_label or 'this target'}: {detail} TCKDB refuses a "
                f"bac_total without a bond decomposition and does not check that "
                f"a partial one sums to the total, so the correction is omitted "
                f"rather than sent unsupported. The rest of the payload is unchanged."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field,
                           _W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE, message)
            if omitted_bac_reasons is not None:
                omitted_bac_reasons.append(reason)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE, message, warning_field,
                    {"action": "bac_correction_omitted", "reason": reason, "target_kind": target_kind, "species": target_label}))
            continue
        components_out: list[dict[str, Any]] = []
        for c in components_in:
            if not isinstance(c, Mapping):
                continue
            if c.get("parameter_value") is None or c.get("contribution_value") is None:
                continue
            components_out.append({
                k: c[k] for k in _APPLIED_CORRECTION_COMPONENT_FIELDS if k in c
            })

        scheme_in = rec["scheme"]
        scheme_out = {
            k: scheme_in[k] for k in scheme_in
            if k not in {"level_of_theory", "version"}
        }
        # Older ARC-shaped payloads carried a free-form scheme version.
        # Current TCKDB identifies schemes by scientific/software provenance;
        # retain a reported legacy label as a note, not an invented release.
        legacy_version = scheme_in.get("version")
        if legacy_version is not None:
            version_note = f"Legacy scheme version: {legacy_version}"
            scheme_out["note"] = "; ".join(
                str(value) for value in (scheme_out.get("note"), version_note) if value
            )
        lot_ref = _scheme_level_of_theory(scheme_in)
        if lot_ref is not None:
            table_stem = correction_table_method_stem(lot_ref["method"])
            if table_stem is not None:
                # ARC's Arkane level string names a correction table, not a method
                # (``cbs-qb3-paraskevas``, ``cbsqb32023``): the calculation that ran is
                # the stem. tckdb-schemas 0.67 warns on such a method and stores it as a
                # separate level of theory; it says to send the method and name the table on
                # the scheme. The scheme's ``name`` is that name (the table label, as ARC
                # states it); the level carries the method.
                table_name = lot_ref["method"]
                lot_ref = {**lot_ref, "method": table_stem}
                scheme_out["name"] = table_name
                message = (
                    f"ARC's Arkane level string {table_name!r} names a correction table, not a "
                    f"method. The {scheme_out.get('kind')} scheme is sent with level method "
                    f"{table_stem!r} and the table name as the scheme name."
                )
                logger.warning("TCKDB %s: %s: %s", warning_field, _W_CORRECTION_TABLE_METHOD_SPLIT, message)
                if warnings is not None:
                    warnings.append(_self_check(
                        _W_CORRECTION_TABLE_METHOD_SPLIT, message, f"{warning_field}.scheme.level_of_theory.method",
                        {"action": "correction_table_named_on_scheme", "table": table_name, "method": table_stem, "scheme_kind": str(scheme_out.get("kind"))}))
            scheme_out["level_of_theory"] = lot_ref
            if scheme_out.get("kind") in _FREQ_KEYED_SCHEME_KINDS:
                freq_ref = _bac_key_frequency_level(
                    rec.get("matched_arkane_key"), bac_frequency_level,
                    kind=str(scheme_out.get("kind")), warning_field=warning_field, warnings=warnings,
                )
                if freq_ref is not None:
                    scheme_out["frequency_level_of_theory"] = freq_ref
        # ``scheme.software`` is the program that computed the scheme's
        # parameters (contract: "The program release that computed this
        # scheme's parameters"). That is the ``software`` of the Arkane
        # database entry the table came from, recorded by ARC as
        # ``matched_arkane_key`` (e.g. LevelOfTheory(method='b3lyp2023',
        # basis='def2tzvp', software='gaussian')). The record's
        # ``level_of_theory`` is ARC's own level, not the table's: ARC's
        # matcher (arc/statmech/arkane.py) accepts a key without software
        # for any program, so crediting the table to the level's software
        # could name a program that never computed it. Hence: the key's
        # software, name only (ARC records no release); omitted when the
        # key is absent or names none; omitted with a warning when it
        # disagrees with the level's software. Never Arkane or ARC (they
        # looked the table up; that is ``workflow_tool_release``).
        if "software" not in scheme_out:
            key_software = _arkane_key_software(rec.get("matched_arkane_key"))
            scheme_level = scheme_in.get("level_of_theory")
            level_software = (
                scheme_level.get("software") if isinstance(scheme_level, Mapping) else None
            )
            if key_software is None:
                pass
            elif (isinstance(level_software, str) and level_software.strip()
                    and level_software.strip().lower() != key_software.lower()):
                message = (
                    f"The {scheme_out.get('kind')} table came from Arkane's "
                    f"{rec.get('matched_arkane_key')!r} (software={key_software!r}), "
                    f"but ARC's correction level names software={level_software!r}. "
                    f"Which program computed the table is unclear, so "
                    f"scheme.software is omitted."
                )
                logger.warning("TCKDB %s: %s: %s", warning_field,
                               _W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT, message)
                if warnings is not None:
                    warnings.append(_self_check(
                        _W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT, message, f"{warning_field}.scheme.software",
                        {"action": "scheme_software_omitted", "scheme_kind": str(scheme_out.get("kind")), "arkane_key_software": key_software, "level_software": level_software}))
            else:
                scheme_out["software"] = {"name": key_software}

        if scheme_out.get("kind") in _RMG_DATABASE_SCHEME_KINDS:
            if arkane_release is not None and "workflow_tool_release" not in scheme_out:
                scheme_out["workflow_tool_release"] = dict(arkane_release)
            revision = (scheme_data_revisions or {}).get(scheme_out["kind"])
            if revision is not None and "data_revision" not in scheme_out:
                scheme_out["data_revision"] = revision

        payload: dict[str, Any] = {
            "application_role": rec["application_role"],
            "value": float(rec["value"]),
            "value_unit": rec["value_unit"],
            "scheme": scheme_out,
            "components": components_out,
        }
        if source_calculation_key is not None:
            payload["source_calculation_key"] = source_calculation_key

        note = rec.get("note")
        if note is not None:
            payload["note"] = note

        out.append(payload)

    return out


# Explicit target field sets guard against future divergence of the two
# bundle roots. Both current roots accept calculation provenance.
_THERMO_FIELDS_BY_TARGET: dict[str, frozenset[str]] = {
    "ThermoInBundle": frozenset({
        "h298_kj_mol", "s298_j_mol_k", "tmin_k", "tmax_k",
        "nasa", "points", "source_calculations",
        "enthalpy_reference_kind", "reference_pressure_bar",
        "energy_level_of_theory",
    }),
    "BundleThermoIn": frozenset({
        "h298_kj_mol", "s298_j_mol_k", "tmin_k", "tmax_k",
        "nasa", "points", "source_calculations",
        "enthalpy_reference_kind", "reference_pressure_bar",
        "energy_level_of_theory",
    }),
}

# TCKDB #520: a thermo record carrying enthalpy content must declare its
# enthalpy basis. Arkane's H298 and NASA a6/b6 (and the H/G points ARC
# evaluates from that fit) are formation enthalpies from the elements at
# 298.15 K, which is exactly ``formation_298k``.
_THERMO_ENTHALPY_REFERENCE_KIND = "formation_298k"

# That holds only when Arkane subtracted the atom energies of the level the
# species' energies were computed at. With none for the level of theory,
# ARC runs Arkane with useAtomCorrections=False and every enthalpy (H298,
# NASA a6/b6, point H/G) is the raw absolute energy. output.yml 1.2 records
# that switch (``thermo.atom_corrections_applied``) and the level whose
# atom energies were used (``thermo.atom_corrections_level``), which can be
# a stand-in for the energy level (ARC only warns). Enthalpies failing
# either check are stripped from the block, keeping its entropy and Cp.
_W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED = ArcWarning.ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED.value
_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH = ArcWarning.ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH.value
_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE = ArcWarning.ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE.value
_W_ENTHALPY_NOT_FINITE = ArcWarning.ENTHALPY_NOT_FINITE.value
# Levels compare by effective method (the method with its dispersion
# folded in) and basis, normalized exactly as ARC matches levels against
# Arkane's database: case, hyphens and spaces and a trailing refit year are
# ignored, and ``b3lyp`` + ``gd3bj``, ``b3lyp-d3bj`` and ``b3lyp-d3(bj)`` are
# one method, while ``wb97xd``/``wb97xd3`` and ``ccsd(t)``/``ccsdt`` stay
# two. Software, year, method_type and args never matter. Ported, since the
# adapter must not import ARC, from ARC 1977e53b: arc/statmech/arkane.py
# ``_normalize_name`` and ``_split_method_year``; arc/main.py
# ``DISPERSION_SUFFIX_REGEX``, ``_canonical_dispersion`` and
# ``_normalized_method_and_basis``. A parity test runs against ARC when it
# has them.
_METHOD_REFIT_YEAR_RE = re.compile(r"^(.*?)(\d{4})$")
_DISPERSION_SUFFIX_RE = re.compile(r"g?d[234](\(?bj\)?)?$")
# ARC's Arkane key match and data/AEC.yml lookup read only the method
# string and basis, ignoring these level fields, so when either level sets
# one, a matching ``atom_corrections_level`` cannot show that the applied
# atom energies carried it (ARC applies plain B3LYP atom energies to
# B3LYP + GD3BJ, gas-phase ones to SMD).
_LEVEL_FIELDS_ARC_MATCHING_IGNORES = ("dispersion", "solvation_method")

# Magnitude backstop, and the first check for thermo whose flag is null or
# absent (pre-1.2 output, species Arkane loaded from its own YAML). Raw
# absolute energies are about -1e5 kJ/mol per heavy atom, while the largest
# real |ΔHf| are O(10^3-10^4) kJ/mol, so anything beyond this bound cannot
# be a formation enthalpy. It misses every species whose raw total energy
# is below about 7.6 hartree (2e4 kJ/mol): H, H2, He, the Li atom.
_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL = 2.0e4
_W_ENTHALPY_NOT_FORMATION_MAGNITUDE = ArcWarning.ENTHALPY_NOT_FORMATION_MAGNITUDE.value
# Interim checks for a null or absent flag, until every run writes output.yml
# 1.2's ``atom_corrections_applied`` (ARC PR #1059), which supersedes them.
#
# Declaring ``formation_298k`` on output without the 1.2 flags is a
# deliberate, maintainer-approved interpretation ("option (d)"), not a
# default filled in for a convention ARC left unstated. Arkane's corrected
# H298 (and the NASA fit and point H/G built from it) is a formation
# enthalpy by construction: Arkane subtracts the atom energies of the level
# of theory and adds the elements' experimental formation enthalpies. What
# pre-1.2 output does not record is whether that correction ran, and at
# which level. The non-finite check is exact; the magnitude, light-species,
# header-level and dispersion/solvation checks are heuristics that make an
# uncorrected (or wrong-level) enthalpy very unlikely to pass, not proof that
# the correction ran at the right level. A block failing any of them loses
# its enthalpy content. When ARC writes the 1.2 flags, they decide instead.
#
# * The magnitude guard's blind spot. A species whose raw (uncorrected)
#   total energy is below the bound looks like a formation enthalpy either
#   way, so its enthalpy cannot be verified without the flag. The raw
#   magnitude is estimated from the composition as a sum of approximate
#   atomic total energies (hartree). Only H, He and Li are listed: any
#   heavier atom alone (Be is about 14.7 hartree) puts the species past
#   the bound, where the magnitude guard decides.
# * The header ``arkane_level_of_theory``, which ARC 1.1 writes from the
#   level it ran Arkane's atom corrections at (the user's setting, else
#   ``composite_method``, else ``sp_level``; null when none is resolved).
#   It must be the energy level; a null header cannot be checked.
# * Dispersion and solvation fields, as for a true flag: ARC's Arkane
#   matching ignores them, so the plain gas-phase method's atom energies
#   were applied.
_LIGHT_ATOM_TOTAL_ENERGY_HARTREE = {"H": 0.50, "He": 2.90, "Li": 7.43}
_W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES = ArcWarning.ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES.value
_HEADER_CORRECTIONS_LEVEL_SOURCE = "output_header.arkane_level_of_theory"

# TCKDB #529 and the producer contract: ``reference_pressure_bar`` is never
# defaulted, and "if your source does not say which convention a number
# follows, leave the field out". ARC states the standard-state pressure only
# as ``thermo.standard_state_pressure_pa`` (arc/output.py
# ``_thermo_to_dict``; ``None`` when the thermo did not come from a statmech
# run that recorded one). When it is absent or unusable the adapter omits
# ``reference_pressure_bar`` (TCKDB stores "not stated") and reports
# ``thermo_reference_pressure_not_stated``; it never fills RMG's 1 atm in.
_W_THERMO_REFERENCE_PRESSURE_NOT_STATED = ArcWarning.THERMO_REFERENCE_PRESSURE_NOT_STATED.value
# Plausible standard-state pressures, in bar: a validity check on the value
# ARC recorded, never a source of one. Every real convention (1 bar, 1 atm)
# sits well inside; a value in bar mistaken for Pa (1.01325 -> 1e-5 bar) or
# a YAML boolean (True -> 1e-5 bar) falls far outside.
_THERMO_REFERENCE_PRESSURE_WINDOW_BAR = (0.5, 2.0)


def _thermo_reference_pressure_bar(
    thermo_record: Mapping[str, Any],
) -> tuple[float | None, str | None]:
    """Return ``(pressure_bar, None)`` or ``(None, why it is not stated)``.

    The pressure is ARC's recorded ``standard_state_pressure_pa`` converted
    to bar, when it is a real number in Pa inside
    ``_THERMO_REFERENCE_PRESSURE_WINDOW_BAR``. Otherwise there is no
    pressure, and the second item says whether ARC recorded none
    (``not_recorded``) or an unusable one (``malformed``).
    """
    recorded = thermo_record.get("standard_state_pressure_pa")
    if recorded is None:
        return None, "not_recorded"
    pressure_bar = math.nan
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        pressure_bar = float(recorded) / 1e5
    low, high = _THERMO_REFERENCE_PRESSURE_WINDOW_BAR
    if low <= pressure_bar <= high:
        return pressure_bar, None
    logger.warning(
        "TCKDB thermo: malformed standard_state_pressure_pa=%r; "
        "omitting reference_pressure_bar.", recorded,
    )
    return None, "malformed"


def _thermo_energy_level(
    output_doc: Mapping[str, Any], record: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    """Return the level a species' energies were computed at.

    ``composite_method`` when set, else the SP level (``opt_level`` when
    ``sp_level`` is null, as ARC then reuses the opt energy). output.yml
    records one level per run, so under ARC ``adaptive_levels`` the level is
    the one ``restart.yml`` attributes to ``record`` (see ``_resolve_level``),
    or ``None`` when that cannot be worked out.
    """
    # Schema 1.3: the record states the level of the job its energy was read from
    # (the composite job on a composite run, else the sp job, or the opt job when no
    # sp ran), also under ``adaptive_levels``.
    if record is not None and arc13.has_levels(record):
        recorded = arc13.recorded_level(
            record, "composite" if record.get("composite_log") else "sp")
        if recorded is not None and recorded.level is not None:
            return recorded.level
    composite = output_doc.get("composite_method")
    if isinstance(composite, Mapping):
        if _adaptive_kind_named(output_doc, "composite"):
            status = _adaptive_level_status(output_doc, record, "composite")
            if isinstance(status, Mapping):
                return status
            if status == UNDETERMINABLE:
                return None
        return composite
    return _resolve_level(output_doc, "sp", record)


def _normalize_level_name(name: Any) -> str | None:
    """ARC's ``arkane.py::_normalize_name``: lowercase, hyphens and spaces removed."""
    if name is None:
        return None
    return str(name).replace("-", "").replace(" ", "").lower()


def _canonical_dispersion(dispersion: Any) -> str:
    """ARC's ``main.py::_canonical_dispersion``: ``gd3bj``, ``D3(BJ)`` and
    ``EmpiricalDispersion=GD3BJ`` all give ``d3bj``; ``''`` for none."""
    if not dispersion:
        return ""
    dispersion = str(dispersion).lower()
    for character in ("-", " ", "(", ")"):
        dispersion = dispersion.replace(character, "")
    dispersion = dispersion.removeprefix("empiricaldispersion=")
    if dispersion in ("gd2", "gd3", "gd3bj"):
        dispersion = dispersion[1:]  # Gaussian's spelling.
    return dispersion


def _level_identity(level: Any) -> tuple[str, str | None] | None:
    """ARC's ``main.py::_normalized_method_and_basis`` on an output.yml level dict.

    Returns the normalized ``(effective method, basis)``, or ``None`` for a
    level without a method.
    """
    if not isinstance(level, Mapping) or not level.get("method"):
        return None
    method = _normalize_level_name(level["method"])
    year_split = _METHOD_REFIT_YEAR_RE.match(method)  # arkane.py::_split_method_year
    if year_split is not None:
        method = year_split.group(1)
    suffix = _DISPERSION_SUFFIX_RE.search(method)
    if suffix is not None:
        method = method[:suffix.start()] + _canonical_dispersion(suffix.group())
    method += _canonical_dispersion(level.get("dispersion"))
    return method, _normalize_level_name(level.get("basis"))


def _species_element_symbols(record: Mapping[str, Any]) -> tuple[str, ...] | None:
    """A species record's composition: its xyz, else its ``formula``.

    ARC before c8240195 (output.yml 1.0) writes ``xyz: null`` for monoatomic
    species, which skip opt, while still recording ``formula``.
    """
    symbols = _xyz_element_symbols(record.get("xyz"))
    if symbols is None:
        symbols = _formula_element_symbols(record.get("formula"))
    return symbols


def _monatomic_sp_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """``sp_result`` of a single atom's one job: ``sp_energy_hartree``, else ``opt_final_energy_hartree``.

    An atom has no optimisation job; ARC runs only its single point, whose log
    output.yml 1.3 also exports as the atom's ``opt_log`` (so ``sp_log == opt_log``).
    Output written before that, and some records after it, state the atom's energy
    only as ``opt_final_energy_hartree``, which ARC parses from that same log. It is
    the energy of the atom's single job whichever key carries it, so it is used when
    ``sp_energy_hartree`` is not stated; nothing else is read.

    Not for a composite-run atom (``composite_log`` or a composite level stated): its
    ``opt_final_energy_hartree`` is parsed from the composite log and may be an
    intermediate SCF energy rather than the composite energy, so only
    ``sp_energy_hartree`` is accepted and the atom is otherwise refused.
    """
    result = _sp_result_payload(record)
    if result is not None:
        return result
    if _is_composite_atom_record(record):
        return None
    fallback = record.get("opt_final_energy_hartree")
    if isinstance(fallback, bool) or not isinstance(fallback, (int, float)) or not math.isfinite(fallback):
        return None
    return {"electronic_energy_hartree": float(fallback)}


def _is_composite_atom_record(record: Mapping[str, Any]) -> bool:
    """Whether the record is a composite-method run (composite log, or a composite level/method stated)."""
    if record.get("composite_log") or record.get("composite_method"):
        return True
    levels = record.get("levels")
    return isinstance(levels, Mapping) and bool(levels.get("composite"))


def _atom_without_sp_energy_error(record: Mapping[str, Any]) -> ValueError:
    if _is_composite_atom_record(record):
        return ValueError(
            f"{record.get('label')!r} is a single atom of a composite run, so its primary "
            "calculation is its single point, but ARC states no sp_energy_hartree for it "
            "(opt_final_energy_hartree is parsed from the composite log and may be an "
            "intermediate SCF energy, so it is not used); the adapter does "
            "not relabel it as an optimisation, so the species is not built.")
    return ValueError(
        f"{record.get('label')!r} is a single atom, so its primary calculation is its "
        "single point (an atom has no geometry to optimise and TCKDB accepts an sp "
        "primary for it), but ARC states neither sp_energy_hartree nor "
        "opt_final_energy_hartree for it; the adapter does "
        "not relabel it as an optimisation, so the species is not built.")


def _light_species_raw_energy_kj_mol(element_symbols: Any) -> float | None:
    """Estimate a light species' raw total energy magnitude (kJ/mol).

    ``None`` when the composition is unknown or holds any atom heavier than
    Li, whose raw energy alone passes ``_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL``.
    """
    from tckdb_arc._vendor import E_h_kJmol

    if not element_symbols:
        return None
    hartree = 0.0
    for symbol in element_symbols:
        atom_hartree = _LIGHT_ATOM_TOTAL_ENERGY_HARTREE.get(symbol)
        if atom_hartree is None:
            return None
        hartree += atom_hartree
    return hartree * E_h_kJmol


def _level_field_unverifiable_error(
    sides: tuple[tuple[str, Any], ...],
    matched: str,
) -> tuple[str, str] | None:
    """Refuse a level that sets a field ARC's atom-energy matching ignores.

    ``sides`` names each level checked; ``matched`` says which level the
    atom energies were matched to, for the message.
    """
    for side, level in sides:
        if not isinstance(level, Mapping):
            continue
        for field in _LEVEL_FIELDS_ARC_MATCHING_IGNORES:
            if level.get(field):
                return (
                    _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE,
                    f"The {side} {_describe_level(level)} sets "
                    f"{field}={level[field]!r}, which ARC's atom-energy matching "
                    f"ignores, so {matched} does not show that the "
                    f"subtracted atom energies carried it.",
                )
    return None


def _enthalpy_validity_error(
    block: Mapping[str, Any],
    thermo_record: Mapping[str, Any],
    energy_level: Mapping[str, Any] | None,
    header_corrections_level: Any = None,
    element_symbols: Any = None,
) -> tuple[str, str, dict[str, str]] | None:
    """Return why the block's enthalpies are not formation_298k, or ``None``.

    Returns ``(code, message, extra warning context)``. Reads ARC's recorded
    atom-correction switch and level first. A switch that was on still
    needs ``atom_corrections_level`` to be the energy level
    (``_level_identity``), and a match cannot be verified when either level
    sets a dispersion or solvation field ARC's matching ignores; the
    non-finite and magnitude checks follow.

    A null or absent switch runs, first failure wins: non-finite, magnitude,
    the header ``arkane_level_of_theory`` against the energy level
    (mismatch; skipped when the header is null or has no method),
    dispersion/solvation on either level (unverifiable), and a light
    species the magnitude guard cannot catch. Refusals from the last three
    carry ``atom_corrections_applied: not_recorded`` and whether the header
    level was checked in their context.
    """
    applied = thermo_record.get("atom_corrections_applied")
    if applied is False:
        return (
            _W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED,
            "ARC recorded atom_corrections_applied=false: Arkane subtracted no "
            "atom energies, so H298, the NASA fit and point H/G are raw absolute "
            "energies, not formation enthalpies.",
            {},
        )
    if applied is True:
        corrections_level = thermo_record.get("atom_corrections_level")
        corrections_key = _level_identity(corrections_level)
        if corrections_key is None or corrections_key != _level_identity(energy_level):
            return (
                _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH,
                f"Arkane subtracted the atom energies of "
                f"{_describe_level(corrections_level)} from energies computed at "
                f"{_describe_level(energy_level)}, so the enthalpies mix two levels "
                f"and are not formation enthalpies.",
                {},
            )
        unverifiable = _level_field_unverifiable_error(
            (("energy level", energy_level), ("atom_corrections_level", corrections_level)),
            matched="a matching level",
        )
        if unverifiable is not None:
            return (*unverifiable, {})
    values = [block.get("h298_kj_mol"), *block.get("nasa", {}).values()]
    values += [p.get(key) for p in block.get("points", ()) for key in ("h_kj_mol", "g_kj_mol")]
    if any(v is not None and not math.isfinite(v) for v in values):
        return (
            _W_ENTHALPY_NOT_FINITE,
            "An enthalpy value (H298, NASA coefficient or bound, point H or G) "
            "is not finite.",
            {},
        )
    magnitude = _enthalpy_magnitude_error(block)
    if magnitude is not None:
        return (*magnitude, {})
    if applied is True:
        return None
    return _unflagged_enthalpy_error(energy_level, header_corrections_level, element_symbols)


def _unflagged_enthalpy_error(
    energy_level: Mapping[str, Any] | None,
    header_corrections_level: Any,
    element_symbols: Any,
) -> tuple[str, str, dict[str, str]] | None:
    """The interim checks for a null or absent ``atom_corrections_applied``.

    See ``_LIGHT_ATOM_TOTAL_ENERGY_HARTREE`` for why each exists.
    """
    header_key = _level_identity(header_corrections_level)
    context = {
        "atom_corrections_applied": "not_recorded",
        "corrections_level_source": (
            _HEADER_CORRECTIONS_LEVEL_SOURCE if header_key is not None else "not_recorded"
        ),
    }
    if header_key is not None and header_key != _level_identity(energy_level):
        return (
            _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH,
            f"ARC ran Arkane's atom corrections at arkane_level_of_theory "
            f"{_describe_level(header_corrections_level)}, but the energies were "
            f"computed at {_describe_level(energy_level)}, so the enthalpies mix "
            f"two levels and are not formation enthalpies.",
            context,
        )
    unverifiable = _level_field_unverifiable_error(
        (("energy level", energy_level), ("arkane_level_of_theory", header_corrections_level)),
        matched="the recorded level",
    )
    if unverifiable is not None:
        return (*unverifiable, context)
    raw_kj_mol = _light_species_raw_energy_kj_mol(element_symbols)
    if raw_kj_mol is not None and raw_kj_mol < _FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:
        return (
            _W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES,
            f"ARC did not record whether Arkane applied atom-energy corrections, "
            f"and this {_formula(element_symbols)} species' raw total energy "
            f"(about {raw_kj_mol:.3g} kJ/mol) is within the "
            f"{_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:.6g} kJ/mol magnitude bound, so "
            f"an uncorrected enthalpy would pass as a formation enthalpy.",
            context,
        )
    return None


def _has_enthalpy_content(block: Mapping[str, Any]) -> bool:
    return "h298_kj_mol" in block or "nasa" in block or any(
        "h_kj_mol" in p or "g_kj_mol" in p for p in block.get("points", ())
    )


def _strip_enthalpy_content(block: dict[str, Any]) -> None:
    """Remove H298, the NASA fit, and point H/G; keep S298, point S and Cp.

    A point left with only its temperature is dropped (TCKDB requires a
    property on every point).
    """
    block.pop("h298_kj_mol", None)
    block.pop("nasa", None)
    points = [
        {k: v for k, v in p.items() if k not in ("h_kj_mol", "g_kj_mol")}
        for p in block.pop("points", ())
    ]
    points = [p for p in points if set(p) - {"temperature_k"}]
    if points:
        block["points"] = points


def _enthalpy_magnitude_error(block: Mapping[str, Any]) -> tuple[str, str] | None:
    """Refuse a block whose enthalpy is too large to be a formation enthalpy.

    Checks ``h298_kj_mol``, every point ``h_kj_mol``, and the NASA fit's
    H at 298.15 K (the only enthalpy a NASA-only block carries).
    """
    enthalpies = [("h298_kj_mol", block.get("h298_kj_mol"))]
    enthalpies += [
        (f"points[T={p['temperature_k']}].h_kj_mol", p.get("h_kj_mol"))
        for p in block.get("points", ())
    ]
    if "nasa" in block:
        enthalpies.append(("nasa H(298.15 K)", _nasa_h298_kj_mol(block["nasa"])))
    for name, value in enthalpies:
        if value is not None and abs(value) > _FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:
            return (
                _W_ENTHALPY_NOT_FORMATION_MAGNITUDE,
                f"{name}={value:.6g} kJ/mol exceeds the "
                f"{_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:.6g} kJ/mol bound on any "
                f"formation enthalpy, so it cannot be formation_298k. Arkane "
                f"most likely ran without atom-energy corrections for this "
                f"level of theory, leaving raw absolute energies.",
            )
    return None


def _build_thermo_block(
    thermo_record: Any,
    *,
    calc_keys_by_role: Mapping[str, str],
    target_model: Literal["ThermoInBundle", "BundleThermoIn"],
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "thermo",
    energy_level: Mapping[str, Any] | None = None,
    header_corrections_level: Any = None,
    element_symbols: Any = None,
    energy_level_unattributable: bool = False,
    energy_declaration: tuple[str, Mapping[str, Any]] | None = None,
    legacy_cp_data: bool = False,
) -> dict[str, Any] | None:
    """Build a ``target_model``-shaped thermo dict from ``output.yml`` thermo data.

    Returns ``None`` when no usable thermo content can be assembled. The
    server-side ``validate_has_scientific_content`` validator (present on
    both roots) rejects empty thermo blocks, so emitting one with
    nothing in it would just produce a 422; better to omit at the
    producer.

    Mapping (from ``arc/output.py::_thermo_to_dict``):
        h298_kj_mol  → h298_kj_mol
        s298_j_mol_k → s298_j_mol_k
        tmin_k       → tmin_k
        tmax_k       → tmax_k
        nasa_low.coeffs  → nasa.a1..a7
        nasa_high.coeffs → nasa.b1..b7
        nasa_low.tmin_k  → nasa.t_low
        nasa_low.tmax_k  → nasa.t_mid (cross-checked vs nasa_high.tmin_k)
        nasa_high.tmax_k → nasa.t_high
        thermo_points    → points (per-point validation; bad points dropped)

    Enthalpy content (h298, NASA, point H or G) is first checked by
    ``_enthalpy_validity_error`` against ARC's recorded atom-correction
    switch and level (``energy_level`` is the species' energy level, see
    ``_thermo_energy_level``); without that switch, against the document's
    ``arkane_level_of_theory`` (``header_corrections_level``) and the
    species' composition (``element_symbols``, see
    ``_species_element_symbols``). With ``energy_level_unattributable`` (an
    ``adaptive_levels`` run, ``_energy_level_unattributable``) the energy level
    is not known per species, so no enthalpy is verifiable and all of it is
    stripped (``enthalpy_adaptive_levels_unverifiable``). Enthalpy that is not a formation enthalpy is
    stripped, keeping S298, point S and Cp; the refusal is appended to
    ``warnings`` under ``warning_field`` with action
    ``thermo_enthalpy_omitted`` (``thermo_omitted`` when nothing is left).

    Remaining enthalpy content adds
    ``enthalpy_reference_kind="formation_298k"``; entropy content (s298,
    NASA, point S or G) adds ``reference_pressure_bar`` when ARC recorded
    a usable ``standard_state_pressure_pa`` (see
    ``_thermo_reference_pressure_bar``). Without one the field is omitted,
    never defaulted, and ``thermo_reference_pressure_not_stated`` is
    appended to ``warnings`` with action ``reference_pressure_omitted``;
    the block is still sent. Neither field is set on a Cp-only block. The
    finished block is checked with the shared ``enthalpy_reference_error``
    rule; a refused block is never emitted: ``None`` is returned and the
    refusal is appended to ``warnings`` with action ``thermo_omitted``.

    ``calc_keys_by_role`` maps roles to the actual bundle-local keys.
    Both roots accept source links; reaction participants use their own
    scoped keys (for example ``r0_opt``), never another participant's keys.

    ``energy_declaration`` is ``(role, level)`` from
    ``_energy_level_declaration``: the level of the linked energy calculation,
    sent as ``energy_level_of_theory`` only when that role is among this
    block's source links (TCKDB checks the declaration against those links).
    """
    if target_model not in _THERMO_FIELDS_BY_TARGET:
        raise ValueError(
            f"_build_thermo_block: unknown target_model={target_model!r}; "
            f"expected one of {sorted(_THERMO_FIELDS_BY_TARGET)}"
        )
    if not isinstance(thermo_record, Mapping):
        return None

    block: dict[str, Any] = {}

    h298 = thermo_record.get("h298_kj_mol")
    if h298 is not None:
        try:
            block["h298_kj_mol"] = float(h298)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB thermo: malformed h298_kj_mol=%r (%s)", h298, exc)

    s298 = thermo_record.get("s298_j_mol_k")
    if s298 is not None:
        try:
            block["s298_j_mol_k"] = float(s298)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB thermo: malformed s298_j_mol_k=%r (%s)", s298, exc)

    for tk in ("tmin_k", "tmax_k"):
        v = thermo_record.get(tk)
        if v is not None:
            try:
                block[tk] = float(v)
            except (TypeError, ValueError) as exc:
                logger.warning("TCKDB thermo: malformed %s=%r (%s)", tk, v, exc)

    nasa = _build_nasa_block(thermo_record.get("nasa_low"), thermo_record.get("nasa_high"))
    if nasa is not None:
        block["nasa"] = nasa

    points = _build_thermo_points(
        thermo_record.get("thermo_points")
        or (thermo_record.get("cp_data") if legacy_cp_data else None))
    if points:
        block["points"] = points

    # Thermo provenance: link every calculation that actually contributed
    # to the thermo result, not just the "physically produced" ones.
    # For ARC's standard opt/freq/sp pipeline that means:
    #   opt  — geometry that freq + sp were run on
    #   freq — vibrational modes / ZPE / thermal corrections
    #   sp   — electronic-energy reference for H298
    # The links should be self-sufficient; we don't want consumers to have
    # to traverse `calculation_dependency` to recover the opt calc.
    # Order is fixed (opt, freq, sp) so payloads are deterministic — same
    # inputs hash to the same idempotency key across runs.
    #
    sources: list[dict[str, str]] = []
    for role in (_CALC_KEY_OPT, _CALC_KEY_FREQ, _CALC_KEY_SP, _ROLE_COMPOSITE):
        key = calc_keys_by_role.get(role)
        if key:
            sources.append({"calculation_key": key, "role": role})
    if sources:
        block["source_calculations"] = sources

    def has_content() -> bool:
        return any(key in block for key in ("h298_kj_mol", "s298_j_mol_k", "nasa", "points"))

    if not has_content():
        # Server would 422 us; nothing usable here.
        return None

    # Refusals as (code, message, extra context), reported once the block's
    # fate is known.
    refusals: list[tuple[str, str, dict[str, str]]] = []
    if thermo_record.get(_THERMO_ENTHALPY_WITHHELD_KEY):
        # The record's E0 is one the adapter withheld (``_withhold_unreliable_composite_energies``,
        # which has warned): H298, NASA and point H/G derive from it; S298, Cp and point S do not.
        _strip_enthalpy_content(block)
    if _has_enthalpy_content(block):
        enthalpy_refusal = (
            (
                _W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE,
                "ARC ran with adaptive_levels, which chooses each species' "
                "energy level by heavy-atom count, while output.yml records one "
                "sp_level per run. The level this species' energy was computed "
                "at is not known, so it cannot be checked against the level "
                "Arkane's atom-energy corrections were applied at; H298, the "
                "NASA fit and point H/G may not be formation enthalpies and are "
                "omitted.",
                {"adaptive_levels": "detected"},
            )
            if energy_level_unattributable else
            _enthalpy_validity_error(
                block, thermo_record, energy_level, header_corrections_level, element_symbols)
        )
        if enthalpy_refusal is not None:
            refusals.append(enthalpy_refusal)
            _strip_enthalpy_content(block)
    keep = has_content()

    # Why the entropy's standard state is not stated, when it is not.
    pressure_unstated: str | None = None
    if keep:
        points_out = block.get("points", ())
        if _has_enthalpy_content(block):
            block["enthalpy_reference_kind"] = _THERMO_ENTHALPY_REFERENCE_KIND
        # G = H - T*S carries the entropy's standard state as well.
        if "s298_j_mol_k" in block or "nasa" in block or any(
            "s_j_mol_k" in p or "g_kj_mol" in p for p in points_out
        ):
            pressure_bar, pressure_unstated = _thermo_reference_pressure_bar(thermo_record)
            if pressure_bar is not None:
                block["reference_pressure_bar"] = pressure_bar
        if energy_declaration is not None and any(
                link["role"] == energy_declaration[0] for link in sources):
            block["energy_level_of_theory"] = dict(energy_declaration[1])

    # Belt-and-suspenders: this is the exact bug class that motivated
    # ``target_model`` in the first place (see
    # ``_THERMO_FIELDS_BY_TARGET``'s docstring) — a real raise here, not
    # just in the test suite, so a future field added to this builder
    # without updating the allow-list fails loudly the moment it's
    # exercised, rather than shipping a silent 422 to production. A bare
    # ``assert`` would vanish under ``python -O``, precisely when a
    # production guard is wanted most.
    disallowed = set(block) - _THERMO_FIELDS_BY_TARGET[target_model]
    if disallowed:
        raise ValueError(
            f"_build_thermo_block emitted field(s) not accepted by "
            f"{target_model}: {sorted(disallowed)}"
        )

    # TCKDB refuses the whole upload over an incoherent enthalpy
    # declaration. Never send a block the shared rule would refuse: drop
    # only the thermo block (the enclosing payload stays valid without
    # it). Record every refusal next to the server's own warnings.
    if keep:
        shared_refusal = enthalpy_reference_error(block)
        if shared_refusal is not None:
            refusals.append((*shared_refusal, {}))
            keep = False
    action = "thermo_enthalpy_omitted" if keep else "thermo_omitted"
    for code, message, extra_context in refusals:
        logger.warning(
            "TCKDB thermo %s: producer self-check refused %s (%s): %s",
            warning_field, "its enthalpy" if keep else "it", code, message,
        )
        if warnings is not None:
            warnings.append(_self_check(
                code, message, warning_field,
                {"action": action, **extra_context}))
    if keep and pressure_unstated is not None:
        message = (
            "ARC did not record the standard-state pressure its entropies were "
            "computed at (thermo.standard_state_pressure_pa is "
            + ("absent" if pressure_unstated == "not_recorded" else "not a usable pressure in Pa")
            + "), so reference_pressure_bar is omitted and TCKDB stores it as not "
            "stated rather than a guessed 1 atm."
        )
        logger.warning(
            "TCKDB thermo %s: %s: %s",
            warning_field, _W_THERMO_REFERENCE_PRESSURE_NOT_STATED, message,
        )
        if warnings is not None:
            warnings.append(_self_check(
                _W_THERMO_REFERENCE_PRESSURE_NOT_STATED, message, warning_field,
                {"action": "reference_pressure_omitted", "standard_state_pressure_pa": pressure_unstated}))
    return block if keep else None


def _index_species(output_doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Build a label → species record map from ``output_doc['species']``.

    Later occurrences win on collision — that matches the implicit
    contract of ARC's output.yml (one record per label) but lets the
    builder fail loudly when a record is genuinely missing rather than
    silently picking a stale duplicate.
    """
    out: dict[str, Mapping[str, Any]] = {}
    for record in output_doc.get("species") or []:
        if not isinstance(record, Mapping):
            continue
        label = record.get("label") or record.get("original_label")
        if label:
            out[str(label)] = record
    return out


def _index_transition_states(
    output_doc: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Build a ts-label → record map from ``output_doc['transition_states']``."""
    out: dict[str, Mapping[str, Any]] = {}
    for record in output_doc.get("transition_states") or []:
        if not isinstance(record, Mapping):
            continue
        label = record.get("label") or record.get("original_label")
        if label:
            out[str(label)] = record
    return out


def _arc_workflow_tool_release(
    output_doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the ARC ``WorkflowToolReleaseRef``-shaped dict, or ``None``.

    Single source of truth for the ARC release identity used in the
    bundle. Returns ``None`` when neither version nor git commit is
    available (rare — usually at least one is set).
    """
    arc_version = output_doc.get("arc_version")
    arc_git_commit = output_doc.get("arc_git_commit")
    if not (arc_version or arc_git_commit):
        return None
    wt: dict[str, Any] = {"name": "ARC"}
    if arc_version:
        wt["version"] = str(arc_version)
    if arc_git_commit:
        wt["git_commit"] = str(arc_git_commit)
    return wt


def _arkane_workflow_tool_release(
    output_doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Arkane as the ``WorkflowToolReleaseRef`` of a table-derived scheme.

    Contract: ``workflow_tool_release`` is the "workflow tool (e.g.
    ARC/Arkane) whose data file was the proximate source, when the scheme was
    looked up from a tool table". Arkane's tables are what ARC looked up, so
    a scheme's identity should include the Arkane build (a later database
    revision changing a parameter would otherwise collide with the stored
    value). Only what ARC recorded: ``arkane_version`` and ``arkane_git_commit``
    (``git_commit`` is 1-40 characters); ``None`` when neither is usable. This
    is not a calculation's software (``calculation_software_is_workflow_tool``)
    and never ARC.
    """
    version = output_doc.get("arkane_version")
    commit = output_doc.get("arkane_git_commit")
    release: dict[str, Any] = {"name": "Arkane"}
    if version and str(version).strip():
        release["version"] = str(version).strip()
    if commit and 1 <= len(str(commit).strip()) <= 40:
        release["git_commit"] = str(commit).strip()
    return release if len(release) > 1 else None


def _sha256_or_none(value: Any) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


# ``EnergyCorrectionSchemeRef.data_revision`` is at most 200 characters (0.62).
_DATA_REVISION_MAX_CHARS = 200


def _rmg_database_data_revision(output_doc: Mapping[str, Any]) -> str | None:
    """The revision of the RMG-database tables Arkane loaded, as a ``data_revision``, or ``None``.

    Output 1.3's header ``rmg_database`` identifies the ``quantum_corrections/
    data.py`` Arkane actually loaded (it need not be the file under ARC's
    ``RMG_DB_PATH``): its SHA-256, and the git ``HEAD`` of its checkout
    (``path_kind: git``) or the conda ``rmgdatabase`` package version
    (``path_kind: package``). tckdb-schemas 0.62's ``data_revision`` is "the
    revision of the *data* that holds the parameter tables, for example the
    RMG-database commit that holds Arkane's atom-energy and BAC tables" and joins
    the scheme's identity, so two Arkane builds that read one revision are one
    scheme. It is the git commit when the path is a git checkout, else the
    package version, else the ``data.py`` digest as a plain hex string (that is
    the exact table; TCKDB reads a 7-64 hex value as a git commit and lower-cases
    it, and keeps anything else as written). Never filled from anything else:
    ``None`` (the scheme then keeps its pre-0.62 identity, which includes the
    Arkane build) when the document is not 1.3, the block is absent, or it states
    no usable commit, version or digest.

    Not stated by ARC, so not checked here: whether the checkout is dirty or the
    tables were overridden (Arkane ``atomEnergies``/BAC overrides). The contract
    asks for a revision "only when the tables really came from that repository
    revision"; ARC's own ``data/AEC.yml`` override is handled separately
    (``_arc_aec_yml_digest``).
    """
    if not _is_output_schema_1_3_or_later(output_doc):
        return None
    identity = output_doc.get("rmg_database")
    if not isinstance(identity, Mapping):
        return None
    kind = identity.get("path_kind")
    commit = identity.get("git_commit")
    commit = commit.strip() if isinstance(commit, str) else ""
    version = identity.get("version")
    version = version.strip() if isinstance(version, str) else ""
    digest = _sha256_or_none(identity.get("quantum_corrections_sha256"))
    if kind == "git" and 1 <= len(commit) <= _DATA_REVISION_MAX_CHARS:
        return commit.lower() if re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) else commit
    if kind == "package" and 1 <= len(version) <= _DATA_REVISION_MAX_CHARS:
        return version
    return digest


def _arc_aec_yml_digest(output_doc: Mapping[str, Any]) -> str | None:
    """Output 1.3's ``arc_aec_yml_sha256`` when it is a SHA-256, else ``None``."""
    if not _is_output_schema_1_3_or_later(output_doc):
        return None
    return _sha256_or_none(output_doc.get("arc_aec_yml_sha256"))


# The scheme kinds whose parameters Arkane reads from the RMG-database tables.
_RMG_DATABASE_SCHEME_KINDS = ("atom_energy", "bac_petersson", "bac_melius")


def _scheme_data_revisions(output_doc: Mapping[str, Any]) -> dict[str, str]:
    """``scheme.data_revision`` per scheme kind from an output 1.3 header.

    Every record ARC exports (``atom_energy``, ``bac_petersson``, ``bac_melius``) is
    computed by Arkane's own correction functions from the RMG-database
    ``quantum_corrections/data.py`` it loaded, so the scheme's data revision is that
    table's (``_rmg_database_data_revision``). A revised table is then a new scheme
    identity (BRIDGE_ROADMAP B12), and an unrelated RMG-Py (Arkane) commit no longer
    splits one: the Arkane build stays on ``scheme.workflow_tool_release`` as
    provenance only (tckdb-schemas 0.62: "send the RMG-database commit here, keep
    stamping the tool release").

    The header's ``arc_aec_yml_sha256`` (ARC's own ``data/AEC.yml``, rendered
    as Arkane's ``atomEnergies``) is deliberately not a revision: ARC writes
    no ``atom_energy`` record for energies that came from that file (the record
    is keyed on a ``data.py`` entry), so no exported scheme has it as its source.

    Empty when the document states no database identity.
    """
    revision = _rmg_database_data_revision(output_doc)
    if revision is None:
        return {}
    return {kind: revision for kind in _RMG_DATABASE_SCHEME_KINDS}


def _arc_analysis_software_release(
    output_doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the Arkane ``SoftwareReleaseRef``-shaped dict, or ``None``.

    ARC runs Arkane for the thermo, statmech and kinetics it reports, so
    Arkane is the post-processing software of those products (their
    ``software_release`` / ``analysis_software_release``), never a
    calculation's ``software_release``, which TCKDB refuses for a
    workflow tool (``calculation_software_is_workflow_tool``). output.yml
    records ``arkane_version`` (e.g. ``4.0.0``) and ``arkane_git_commit``
    (the RMG-Py commit Arkane ran from). The version goes to ``version``;
    the commit stays in ``revision``, where earlier adapters put it. Each
    is sent only when ARC recorded it; ``None`` when neither is.
    """
    arkane_version = output_doc.get("arkane_version")
    arkane_git_commit = output_doc.get("arkane_git_commit")
    if not (arkane_version or arkane_git_commit):
        return None
    release: dict[str, Any] = {"name": "Arkane"}
    if arkane_version:
        release["version"] = str(arkane_version)
    if arkane_git_commit:
        release["revision"] = str(arkane_git_commit)
    return release


# StatmechCalculationRole values that ARC's three-stage opt/freq/sp
# workflow can declare. Order is fixed (opt → freq → sp) so the
# emitted source_calculations list is byte-stable across runs of the
# same content. ``opt_coarse`` is intentionally excluded — it's an
# intermediate optimization stage, not a statmech input (the schema's
# StatmechCalculationRole enum has no ``opt_coarse`` value, and the
# task spec is explicit that opt_coarse is not a statmech source).
_STATMECH_CALC_ROLES: tuple[tuple[str, str], ...] = (
    (_CALC_KEY_OPT, "opt"),
    (_CALC_KEY_FREQ, "freq"),
    (_CALC_KEY_SP, "sp"),
    (_ROLE_COMPOSITE, _ROLE_COMPOSITE),
)


_W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL = ArcWarning.FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL.value


def _build_freq_scale_factor_ref(
    output_doc: Mapping[str, Any],
    *,
    workflow_tool_release: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a ``FreqScaleFactorRef``-shaped dict, or ``None``.

    Maps ARC run-level frequency-scale metadata to the unified TCKDB
    reference shape. Returns ``None`` when ARC didn't apply a scale
    factor for this run, or when the level of theory's ``method``
    isn't available — ``LevelOfTheoryRef.method`` is required by the
    schema, and emitting a ref with a missing method would fail
    server-side validation.

    Source-attribution policy (from
    ``FreqScaleFactorRef`` docstring + ARC's audit):

    * ``value`` ← ``output_doc['freq_scale_factor']``
    * ``level_of_theory`` ← ``freq_level`` (preferred — actual freq LOT),
      falling back to ``arkane_level_of_theory`` when freq_level is
      absent (common single-LOT runs).
    * ``software`` ← ``freq_level.software`` (or fallback chain to opt).
    * ``scale_kind`` ← not sent (adapter 0.6.0, maintainer decision).
      ARC does not state what kind of factor it applied: its
      ``data/freq_scale_factors.yml`` mixes CCCBDB fundamental factors
      and ZPE x 1.014 harmonic ones. The field is optional, so TCKDB
      applies its own default instead of the adapter claiming one.
    * ``note`` ← ``freq_scale_factor_source`` when present (it's a bare
      citation/URL string, not structured literature).
    * ``source_literature`` ← always ``None``. The schema explicitly
      forbids synthesizing literature rows from raw citation strings.
    * ``workflow_tool_release`` ← ARC release **only when ARC's
      curated data file was the proximate source** (i.e., the source
      string is non-null). When the user supplied the factor directly,
      omit ``workflow_tool_release`` — claiming ARC's release would
      fork the dedupe identity tuple ``(level, software, scale_kind,
      value, source_literature, workflow_tool_release)`` and create
      duplicate registry rows.
    """
    value = output_doc.get("freq_scale_factor")
    if value is None:
        return None
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "TCKDB statmech: malformed freq_scale_factor=%r; omitting FSF ref.",
            value,
        )
        return None
    if value_f <= 0:
        logger.warning(
            "TCKDB statmech: freq_scale_factor=%r is non-positive; omitting "
            "FSF ref (server requires gt 0).", value,
        )
        return None

    # Prefer freq_level, fall back to arkane_level_of_theory. Don't
    # fall further back to opt_level — opt and freq levels are
    # genuinely independent in some workflows, and an FSF mislabeled
    # against opt_level would dedupe incorrectly.
    level_source = output_doc.get("freq_level") or output_doc.get("arkane_level_of_theory")
    level_of_theory = _arc_level_to_tckdb_lot(level_source)
    # Arkane applies the single run-wide ``frequencyScaleFactor`` to every species,
    # so the factor was applied to this record whatever its ``levels.freq``; its
    # ``FreqScaleFactorRef.level_of_theory`` is the level the factor belongs to
    # (the header ``freq_level``). Under ``adaptive_levels`` a record whose
    # frequencies are from another level is still sent with the header level, and
    # reported.
    recorded = arc13.recorded_level(record, "freq") if record is not None else None
    other_level = (
        recorded is not None and recorded.level is not None
        and isinstance(output_doc.get("freq_level"), Mapping)
        and _arc_level_to_tckdb_lot(recorded.level) != _arc_level_to_tckdb_lot(output_doc["freq_level"])
    )
    if other_level:
        message = (
            f"The run-wide frequency scale factor (fitted for header freq_level "
            f"{_describe_level(output_doc['freq_level'])}) was applied by Arkane to "
            f"{record.get('label')!r}, whose frequencies are from "
            f"{_describe_level(recorded.level)} (levels.freq). It is attached with its own level."
        )
        logger.warning("TCKDB %s: %s", _W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL, message, "statmech.freq_scale_factor",
                {"action": "freq_scale_factor_sent_with_header_level"}))
    if level_of_theory is None:
        logger.debug(
            "TCKDB statmech: freq/arkane level missing or has no 'method'; "
            "cannot build FreqScaleFactorRef.level_of_theory.",
        )
        return None

    ref: dict[str, Any] = {
        "level_of_theory": level_of_theory,
        "value": value_f,
    }

    # Software comes from freq_level when present; otherwise fall back
    # to opt_level's software (matches the existing ess_versions
    # fallback in _calculation_payload — opt and freq usually share an
    # ESS in practice).
    freq_software = (
        (level_source.get("software") if isinstance(level_source, Mapping) else None)
        or _opt_level_software(output_doc)
    )
    # Output.yml 1.3 states the program that ran the record's frequency job.
    if not other_level and recorded is not None and recorded.level is not None and recorded.job_key and isinstance(
            record.get("ess_software"), Mapping) and record["ess_software"].get(recorded.job_key):
        freq_software = record["ess_software"][recorded.job_key]
    if freq_software:
        ref["software"] = {"name": str(freq_software)}

    source_string = output_doc.get("freq_scale_factor_source")
    if source_string:
        # Bare citation string lands in note. Never synthesized into
        # source_literature.
        ref["note"] = str(source_string)
        # Only tag ARC as the proximate source when our data file was
        # actually the lookup origin (source_string non-null implies
        # _resolve_freq_scale_factor_source matched a row in
        # data/freq_scale_factors.yml). User-supplied factors leave
        # workflow_tool_release null to avoid forking the registry.
        if workflow_tool_release is not None:
            ref["workflow_tool_release"] = dict(workflow_tool_release)

    return ref


def _opt_level_software(output_doc: Mapping[str, Any]) -> str | None:
    opt_level = output_doc.get("opt_level")
    if isinstance(opt_level, Mapping):
        sw = opt_level.get("software")
        if sw:
            return str(sw)
    return None


# TCKDB ``RigidRotorKind`` enum values (live as of this writing — see
# ``app/db/models/common.py``). Output 1.3 classifies the kind from the principal
# moments of the exported geometry (``linear``, ``symmetric_top``,
# ``spherical_top``, ``asymmetric_top``; ``atom`` never reaches output.yml, and
# ``null`` where it cannot be classified) and the adapter sends it as stated.
# Before 1.3 ARC emitted only ``atom`` / ``linear`` / ``asymmetric_top``, which
# mislabelled every symmetric top.
_TCKDB_RIGID_ROTOR_KINDS = frozenset({
    "atom", "linear", "spherical_top", "symmetric_top", "asymmetric_top",
})

# TCKDB ``TorsionTreatmentKind`` values that ARC produces today. The
# remaining values (``rigid_top``, ``hindered_rotor_dos``) aren't in
# ARC's lexicon, so they're silently dropped per spec rather than
# guessed at.
_ARC_TO_TCKDB_TORSION_TREATMENTS = frozenset({"free_rotor", "hindered_rotor"})


# Which statmech-block fields this adapter can emit, keyed by the
# *root* model the block is destined for. Unlike thermo, both statmech
# roots currently accept the full set the builder below is capable of
# producing:
#   - "StatmechInBundle" is the computed-SPECIES bundle root.
#   - "BundleStatmechIn" is the computed-REACTION per-species root.
# ``StatmechInBundle`` additionally accepts species-only fields this
# builder never emits today (``literature``, ``rotational_constant_a
# /b/c_cm1``, ``software_release``, ``workflow_tool_release``) — see
# ``ComputedSpeciesUploadRequest``'s schema. That means today's parity
# is incidental, not structural: adding an emission for any of those
# fields without also updating this allow-list (and gating it on
# ``target_model``, the same way ``_build_thermo_block`` gates
# ``source_calculations``) would silently 422 every computed-reaction
# upload that hits it — exactly the bug class this file's thermo fix
# addresses. The final assert below turns that into a loud failure
# instead. Kept honest against schema drift by
# ``tests/test_shared_builder_field_sets.py``, which imports the real
# ``tckdb_schemas`` models and asserts these sets are subsets of
# ``<Model>.model_fields``.
_STATMECH_FIELDS_BY_TARGET: dict[str, frozenset[str]] = {
    "StatmechInBundle": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
        "energy_level_of_theory",
    }),
    "BundleStatmechIn": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
        "energy_level_of_theory",
    }),
    # The conformer upload's nested statmech (ConformerUploadRequest.statmech).
    # Its torsions (StatmechTorsionIn) carry ``invalidated_reason``, the home of
    # ARC's rejected rotors, as the bundle torsions have since tckdb-schemas 0.61.
    "ConformerUploadStatmechPayload": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
        "energy_level_of_theory",
    }),
}


_W_TORSION_SCAN_NOT_BUILT = ArcWarning.TORSION_SCAN_NOT_BUILT.value
_W_STATMECH_TREATMENT_NOT_STATED = ArcWarning.STATMECH_TREATMENT_NOT_STATED.value


def _build_statmech_block_for_species(
    *,
    output_doc: Mapping[str, Any],
    species_record: Mapping[str, Any] | None = None,
    calc_keys_by_role: Mapping[str, str],
    workflow_tool_release: Mapping[str, Any] | None,
    target_model: Literal[
        "StatmechInBundle", "BundleStatmechIn", "ConformerUploadStatmechPayload"],
    scan_key_renames: Mapping[str, str] | None = None,
    unbuilt_scans: Mapping[str, str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "statmech",
    freq_hessian_available: bool = False,
    energy_declaration: tuple[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a ``target_model``-shaped statmech dict, or ``None``.

    A rotor-aware ``statmech_treatment`` and each torsion's ``treatment_kind``
    are sent only when ``freq_hessian_available``; plain ``rrho`` (no rotors) is
    sent regardless. The reasoning:
    Arkane ignores every rotor when the frequency log has no force-constant
    matrix and runs the species as RRHO (RMG-Py ``arkane/statmech.py``
    647-667), so ARC's rotor list does not show which treatment was applied.
    Without Hessian evidence the field is omitted (TCKDB: "an absent field is
    honest where an invented one would not be") and
    ``statmech_treatment_not_stated`` is reported. The default is False, so a
    caller that has not looked never claims a treatment.

    ``unbuilt_scans`` maps each scan ARC exported (``rotor_scans``) that the
    caller could not build to the reason. A torsion naming one keeps its
    summary but loses ``source_scan_calculation_key``, which would dangle
    and make TCKDB refuse the whole upload; each dropped link is appended
    to ``warnings`` as ``torsion_scan_not_built``. Without the map (a
    direct call), scans are assumed unbuilt only when the run records no
    ``scan_level``. A reference to a scan ARC never exported is left as is,
    so it still reaches validation.

    Pulls per-species statmech metadata from
    ``species_record['statmech']`` (the dict that ``arc/output.py::
    _statmech_to_dict`` writes into ``output.yml``) and projects it onto
    the upload schema. The frequency-scale-factor handling is unchanged.

    ``energy_declaration`` is ``(role, level)`` from
    ``_energy_level_declaration``; it is sent as ``energy_level_of_theory``
    only when that role is among the block's source links, which TCKDB checks
    the declaration against.

    Every target adds ARC's rejected rotors (``statmech.rejected_torsions``) as
    torsions carrying ``invalidated_reason`` (the bundle torsion models have it
    since tckdb-schemas 0.61). See ``_build_rejected_torsions``.

    Both bundle endpoints (``StatmechInBundle`` for computed-species,
    ``BundleStatmechIn`` for computed-reaction per-species) currently
    accept the same field set this builder is capable of producing —
    see ``_STATMECH_FIELDS_BY_TARGET`` for why that's incidental rather
    than guaranteed, and why ``target_model`` is required here anyway.

    ``calc_keys_by_role`` is the role-to-bundle-local-key map for the
    *owning* species block. Computed-species passes the unscoped keys
    (``{"opt": "opt", ...}``); computed-reaction passes the species-
    scoped equivalents (``{"opt": "r0_opt", ...}``). The helper does
    not synthesize keys — it only writes a source_calculation entry per
    role that the caller declared.

    Returns ``None`` when no field survives filtering — emitting an
    empty statmech container would just create a useless server-side
    row. Per the project convention, no empty containers.
    """
    if target_model not in _STATMECH_FIELDS_BY_TARGET:
        raise ValueError(
            f"_build_statmech_block_for_species: unknown "
            f"target_model={target_model!r}; expected one of "
            f"{sorted(_STATMECH_FIELDS_BY_TARGET)}"
        )
    block: dict[str, Any] = {}

    fsf_ref = _build_freq_scale_factor_ref(
        output_doc, workflow_tool_release=workflow_tool_release,
        record=species_record, warnings=warnings,
    )
    if fsf_ref is not None:
        block["freq_scale_factor"] = fsf_ref

    statmech_input = (
        species_record.get("statmech") if isinstance(species_record, Mapping) else None
    )
    if isinstance(statmech_input, Mapping):
        external_symmetry = statmech_input.get("external_symmetry")
        if isinstance(external_symmetry, int) and external_symmetry >= 1:
            block["external_symmetry"] = external_symmetry

        optical_isomers = statmech_input.get("optical_isomers")
        if isinstance(optical_isomers, int) and optical_isomers >= 1:
            block["optical_isomers"] = optical_isomers

        is_linear = statmech_input.get("is_linear")
        if isinstance(is_linear, bool):
            block["is_linear"] = is_linear

        rotor_kind = statmech_input.get("rigid_rotor_kind")
        if isinstance(rotor_kind, str) and rotor_kind in _TCKDB_RIGID_ROTOR_KINDS:
            block["rigid_rotor_kind"] = rotor_kind

        torsions_input = statmech_input.get("torsions")
        # Build first, then classify from what survived. The treatment names
        # the rotors the record actually lists, and the builder drops rotors
        # this adapter cannot represent, so classifying from ARC's raw input
        # can claim a rotor-aware treatment the payload has no torsions to
        # support -- which TCKDB refuses as self-contradictory.
        # Only remove links to well-formed exported scans that were not
        # built (for any reason: no scan_level, a level without software,
        # ...). Unknown/malformed references still reach schema validation,
        # rather than being silently repaired.
        exported_scan_keys = {
            entry["key"] for entry in _scan_entries_from_record(species_record)
            if entry.get("type") == _CALC_KEY_SCAN
            and isinstance(entry.get("key"), str) and entry["key"]
            and isinstance(entry.get("scan_result"), Mapping)
        } if isinstance(species_record, Mapping) else set()
        if unbuilt_scans is None:
            unbuilt_scans = (
                {key: "no scan_level recorded" for key in exported_scan_keys}
                if _resolve_level(output_doc, "scan") is None else {}
            )
        omitted_scan_keys = {k: v for k, v in unbuilt_scans.items() if k in exported_scan_keys}
        dropped_links: list[tuple[int, str]] = []
        is_conformer_root = target_model == "ConformerUploadStatmechPayload"
        without_coordinates: list[int] = []
        schema_1_3 = _is_output_schema_1_3_or_later(output_doc)
        slim_torsions = _build_slim_torsions(
            torsions_input, scan_key_renames=scan_key_renames,
            omitted_scan_keys=set(omitted_scan_keys), dropped_links=dropped_links,
            require_coordinates=is_conformer_root,
            dropped_without_coordinates=without_coordinates,
            schema_1_3=schema_1_3,
        )
        if not is_conformer_root:
            # The bundle roots accept a torsion without coordinates, but it then
            # says nothing about which dihedral it is; report it.
            for torsion in slim_torsions:
                if "coordinates" in torsion:
                    continue
                message = (
                    f"Torsion #{torsion['torsion_index']} is sent without dihedral "
                    f"coordinates: ARC's atom_indices for it are missing or unusable."
                )
                logger.warning("TCKDB %s: %s: %s", warning_field,
                               _W_TORSION_WITHOUT_COORDINATES, message)
                if warnings is not None:
                    warnings.append(_self_check(
                        _W_TORSION_WITHOUT_COORDINATES, message, f"{warning_field}.torsions",
                        {"action": "torsion_sent_without_coordinates", "torsion_index": torsion["torsion_index"]}))
        for position in without_coordinates:
            message = (
                f"Torsion #{position} has no usable atom_indices, and the conformer "
                f"upload refuses a torsion without its dihedral coordinates, so it "
                f"is not sent."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_TORSION_NOT_SENT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_TORSION_NOT_SENT, message, f"{warning_field}.torsions",
                    {"action": "torsion_omitted", "torsion_position": position, "reason": "atom_indices_unusable"}))
        for position, scan_key in dropped_links:
            reason = omitted_scan_keys[scan_key]
            message = (
                f"Torsion #{position} names scan {scan_key!r}, which ARC exported "
                f"but the adapter could not build ({reason}); the torsion is kept "
                f"without source_scan_calculation_key, which would otherwise name "
                f"an undeclared calculation and refuse the whole upload."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_TORSION_SCAN_NOT_BUILT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_TORSION_SCAN_NOT_BUILT, message, warning_field,
                    {"action": "torsion_scan_link_omitted", "scan_key": scan_key, "reason": reason}))
        if schema_1_3:
            # Output 1.3 states the treatment Arkane applied, read from its own
            # ``output.py``, so neither ARC's rotor list nor the presence of a
            # force-constant matrix needs to stand in for it (the pre-1.3
            # inference below is retired for these documents).
            treatment = _stated_arkane_treatment(
                statmech_input.get("arkane_treatment"),
                torsions_sent=len(slim_torsions),
                warnings=warnings, warning_field=warning_field,
            )
            if treatment is not None:
                block["statmech_treatment"] = treatment
        else:
            treatment = _classify_statmech_treatment(
                torsions_input, emitted_torsions=slim_torsions,
            )
        # ``rrho`` (an empty rotor list) does not depend on the Hessian: Arkane
        # runs plain RRHO with no rotors whether or not it has a force-constant
        # matrix. Only rotor-aware treatments, and each torsion's own
        # ``treatment_kind`` ('hindered_rotor'), claim what Arkane did with the
        # rotors, and it discards them all without a Hessian.
        no_hessian = not freq_hessian_available and not schema_1_3
        withhold_treatment = (
            treatment is not None and treatment != "rrho" and no_hessian
        )
        strip_torsion_kinds = no_hessian and bool(slim_torsions)
        if treatment is not None and not withhold_treatment and not schema_1_3:
            block["statmech_treatment"] = treatment
        if strip_torsion_kinds:
            slim_torsions = [
                {k: v for k, v in t.items() if k != "treatment_kind"}
                for t in slim_torsions
            ]
        if withhold_treatment or strip_torsion_kinds:
            omitted = []
            if withhold_treatment:
                omitted.append("statmech_treatment")
            if strip_torsion_kinds:
                omitted.append("torsions[].treatment_kind")
            message = (
                f"No force-constant matrix (freq Hessian) was found for this "
                f"species, and Arkane ignores every rotor without one, so ARC's "
                f"rotor list does not show what treatment Arkane applied. Omitted: "
                f"{', '.join(omitted)}"
                + (f" (ARC's rotors would give {treatment!r})" if withhold_treatment else "")
                + f". The {len(slim_torsions)} torsion(s) ARC recorded are still "
                f"sent, without a treatment."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field,
                           _W_STATMECH_TREATMENT_NOT_STATED, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_STATMECH_TREATMENT_NOT_STATED, message, f"{warning_field}.statmech_treatment",
                    {"action": "statmech_treatment_omitted", "reason": "no_freq_hessian", "inferred_treatment": treatment, "omitted": omitted, "torsion_count": len(slim_torsions)}))

        # After the treatment is classified: a rejected rotor was not treated, so it
        # never counts toward ``statmech_treatment``. tckdb-schemas 0.61 gave the bundle
        # torsion models ``invalidated_reason`` too ("Same field, same meaning and same
        # storage as ``StatmechTorsionIn.invalidated_reason`` on the conformer route"),
        # so every route carries ARC's rejected rotors.
        slim_torsions = [*slim_torsions, *_build_rejected_torsions(
            statmech_input.get("rejected_torsions"),
            first_index=(len(torsions_input) if isinstance(torsions_input, list) else 0) + 1,
            warnings=warnings, warning_field=warning_field,
            # The bundle torsion models have no ``note`` (only ``StatmechTorsionIn`` does).
            with_note=is_conformer_root,
        )]

        if slim_torsions:
            block["torsions"] = slim_torsions

        point_group = statmech_input.get("point_group")
        if isinstance(point_group, str) and point_group.strip():
            block["point_group"] = point_group.strip()

    # Source calculations are provenance for actual statmech metadata,
    # not statmech metadata in themselves — emitting them on an
    # otherwise-empty block produces a "useless container" that the
    # project convention forbids. Only attach them when the block
    # already carries at least one substantive field (FSF or any
    # species-derived field), which preserves the prior behavior of
    # omitting the whole block on FSF-less runs that lack a statmech
    # subdict. The keys are taken straight from ``calc_keys_by_role``;
    # the caller is responsible for passing the correct namespace
    # (unscoped for computed-species, ``r0_*``/``p0_*`` for computed-
    # reaction species blocks) so the server-side ownership check sees
    # only calculations owned by the same species entry.
    if block:
        sources = _build_statmech_source_calculations(
            calc_keys_by_role=calc_keys_by_role,
        )
        if sources:
            block["source_calculations"] = sources
            if energy_declaration is not None and any(
                    link["role"] == energy_declaration[0] for link in sources):
                block["energy_level_of_theory"] = dict(energy_declaration[1])

    if not block:
        return None

    # Belt-and-suspenders (see ``_STATMECH_FIELDS_BY_TARGET``'s
    # docstring): fail loudly here, at build time, rather than shipping
    # a field the target root's extra="forbid" would reject as a 422. A
    # bare ``assert`` would vanish under ``python -O``, precisely when a
    # production guard is wanted most, so this is a real ``raise``.
    disallowed = set(block) - _STATMECH_FIELDS_BY_TARGET[target_model]
    if disallowed:
        raise ValueError(
            f"_build_statmech_block_for_species emitted field(s) not "
            f"accepted by {target_model}: {sorted(disallowed)}"
        )
    return block


_ARKANE_TREATMENTS = frozenset({"rrho", "rrho_1d", "rrho_nd", "rrho_1d_nd"})


def _stated_arkane_treatment(
    stated: Any,
    *,
    torsions_sent: int,
    warnings: list[dict[str, Any]] | None,
    warning_field: str,
) -> str | None:
    """The ``statmech_treatment`` for an output 1.3 record's ``arkane_treatment``.

    ARC derives ``arkane_treatment`` from the rotor modes in the ``output.py``
    Arkane wrote, so it is what Arkane applied (``rrho`` when it dropped every
    rotor), and ``null`` when that output was not parsed or holds a rotor mode of
    unknown kind. ``null`` is never filled in from ARC's own rotor list. A
    rotor-aware treatment is *defined* by the rotors it treats, so TCKDB refuses
    one that lists no torsion: it is withheld, with a warning, when none of the
    record's torsions is sent (for example, the conformer upload drops a torsion
    without usable ``atom_indices``).
    """
    reason: str | None = None
    if stated is None:
        reason = "arkane_treatment_not_recorded"
    elif stated not in _ARKANE_TREATMENTS:
        reason = "arkane_treatment_unrecognized"
    elif stated != "rrho" and not torsions_sent:
        reason = "no_torsion_sent_for_rotor_treatment"
    if reason is None:
        return str(stated)
    explanation = {
        "arkane_treatment_not_recorded": (
            "ARC did not record what treatment Arkane applied (its output was not "
            "parsed, or holds a rotor mode of unknown kind), so none is stated."),
        "arkane_treatment_unrecognized": (
            f"ARC states arkane_treatment={stated!r}, which is not a treatment "
            f"this adapter sends, so none is stated."),
        "no_torsion_sent_for_rotor_treatment": (
            f"ARC states arkane_treatment={stated!r}, but no torsion of the record "
            f"is sent, and TCKDB refuses a rotor-aware treatment that lists no "
            f"rotor, so none is stated."),
    }[reason]
    message = f"statmech_treatment is omitted: {explanation}"
    logger.warning("TCKDB %s: %s: %s", warning_field, _W_STATMECH_TREATMENT_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_STATMECH_TREATMENT_NOT_STATED, message, f"{warning_field}.statmech_treatment",
            {"action": "statmech_treatment_omitted", "reason": reason, "arkane_treatment": stated if isinstance(stated, str) else None, "torsion_count": torsions_sent}))
    return None


def _classify_statmech_treatment(
    torsions: Any,
    *,
    emitted_torsions: list[dict[str, Any]] | None = None,
) -> str | None:
    """Map ARC's torsion list to a TCKDB ``StatmechTreatmentKind`` value.

    ``emitted_torsions`` is the list :func:`_build_slim_torsions` actually
    produced for this record. Pass it whenever the payload will carry
    torsions: a rotor-aware treatment is *defined* by the rotors it treats,
    so TCKDB rejects one that lists none, and the builder drops rotors whose
    ARC ``treatment`` has no TCKDB equivalent. Classifying from ARC's raw
    input alone would therefore claim ``rrho_1d`` over an empty list.

    Rules:

    * ``None`` torsions input (no statmech subdict on the species
      record) → ``None``: ARC didn't emit a statmech evaluation, so the
      treatment is genuinely unknown. Don't fabricate one.
    * Empty list (statmech ran, no successful rotors) → ``"rrho"``.
    * Rotors present but none survived into the payload → ``None``.
      ``"rrho"`` would assert that the species was treated as a rigid
      rotor harmonic oscillator, and ARC's own record says otherwise --
      it treated rotors this adapter could not express. Omitting the
      field says "we are not naming a treatment", which is the only
      claim the evidence supports.
    * ≥1 1D rotor (each entry's ``atom_indices`` is a flat 4-int list)
      and no ND → ``"rrho_1d"``.
    * ≥1 ND rotor (entry's ``atom_indices`` is a list of 4-int lists)
      and no 1D → ``"rrho_nd"``.
    * Mix of 1D and ND → ``"rrho_1d_nd"``.
    * Anything we can't classify confidently → ``None`` (omitted).

    The ``rrho_ad``/``rrao`` enum values are reserved for treatments
    ARC doesn't currently produce; we never emit them.
    """
    if torsions is None:
        return None
    if not isinstance(torsions, list):
        return None
    if not torsions:
        return "rrho"
    if emitted_torsions is not None:
        if not emitted_torsions:
            return None
        # Classify from the emitted entries' ``dimension``, which the builder
        # sets only when it resolved real coordinate quartets. An entry
        # without one was emitted as a summary from unusable atom_indices;
        # its dimensionality is unknown, so no treatment can be named.
        dimensions = [t.get("dimension") for t in emitted_torsions]
        if any(not isinstance(d, int) or d < 1 for d in dimensions):
            return None
        has_1d = any(d == 1 for d in dimensions)
        has_nd = any(d > 1 for d in dimensions)
        if has_1d and has_nd:
            return "rrho_1d_nd"
        if has_nd:
            return "rrho_nd"
        return "rrho_1d"
    has_1d = False
    has_nd = False
    for t in torsions:
        if not isinstance(t, Mapping):
            return None
        atom_indices = t.get("atom_indices")
        if (
            isinstance(atom_indices, list)
            and len(atom_indices) == 4
            and all(isinstance(x, int) for x in atom_indices)
        ):
            has_1d = True
        elif (
            isinstance(atom_indices, list)
            and atom_indices
            and all(isinstance(x, list) for x in atom_indices)
        ):
            has_nd = True
        else:
            return None
    if has_1d and has_nd:
        return "rrho_1d_nd"
    if has_nd:
        return "rrho_nd"
    if has_1d:
        return "rrho_1d"
    return None


def _build_slim_torsions(
    torsions: Any,
    *,
    scan_key_renames: Mapping[str, str] | None = None,
    omitted_scan_keys: set[str] | None = None,
    dropped_links: list[tuple[int, str]] | None = None,
    require_coordinates: bool = False,
    dropped_without_coordinates: list[int] | None = None,
    schema_1_3: bool = False,
) -> list[dict[str, Any]]:
    """Build ``BundleStatmechTorsionIn`` entries, with coordinate quartets when available.

    ``schema_1_3`` (an output.yml 1.3 document): a torsion's ``treatment`` is
    nullable there (``null`` where Arkane's own output does not say what it did
    with the rotor, and never defaulted), so a torsion without one is kept and
    sent without ``treatment_kind`` rather than dropped. Before 1.3 a torsion
    without a recognised treatment is omitted.

    ``require_coordinates`` is for the conformer upload's ``StatmechTorsionIn``,
    which unlike the bundle models refuses a torsion whose coordinate count is
    not its dimension (a torsion with none included): a rotor without usable
    ``atom_indices`` is then left out, its position appended to
    ``dropped_without_coordinates``, rather than sent to be refused.

    ARC's per-rotor dict (built by ``_get_torsions`` in ``arc/output.py``)
    carries:

      * ``symmetry_number`` (int)
      * ``treatment`` (``"hindered_rotor"`` / ``"free_rotor"``)
      * ``atom_indices`` (1-based 4-int dihedral defining atom quartet,
        or for ND scans a list of 4-int lists)
      * ``pivot_atoms`` (1-based 2-int axis — bundle has no column)
      * ``barrier_kj_mol`` (fitted barrier — bundle has no column)

    The bundle ``StatmechTorsionInBundle`` schema accepts:

      * ``torsion_index`` (1-based, allocated by emission order)
      * ``symmetry_number``
      * ``treatment_kind``
      * ``dimension`` (default 1)
      * ``coordinates`` (list of ``StatmechTorsionCoordinateIn`` —
        each with ``coordinate_index`` plus ``atom1_index``..``atom4_index``,
        all 1-based; the four atoms must be distinct)
      * ``source_scan_calculation_key`` (optional, must resolve to a
        bundle-local calc of type ``scan`` — deferred until ARC emits
        scan calcs)

    Coordinate emission rules:

      * 1D rotors with a flat 4-int ``atom_indices`` → emit one
        coordinate, ``dimension=1``.
      * ND rotors with a list-of-lists ``atom_indices`` → emit one
        coordinate per inner list, ``dimension=N``. The bundle schema
        requires ``len(coordinates) == dimension`` and contiguous
        ``coordinate_index`` values 1..N.
      * Missing or malformed ``atom_indices`` → log a warning and emit
        the summary fields only (no ``coordinates``, no ``dimension``
        override). Producers must never fabricate atom quartets.

    Rotors whose treatment isn't a recognized TCKDB value (e.g. ARC
    might add new types in the future) are omitted entirely rather than
    emitted with a missing treatment_kind — the latter would produce a
    torsion entry that's effectively meaningless to consumers.

    ``pivot_atoms`` and ``barrier_kj_mol`` are deliberately not emitted:
    the bundle schema rejects ``pivot_atoms`` and has no destination
    column for the fitted barrier. Both stay out of the payload.

    ``torsion_index`` is the entry's 1-based *position in this record's
    own ``torsions`` argument* (i.e. ``statmech.torsions[]`` as it
    appears in ``output.yml``), not a counter of how many torsions this
    function has emitted so far. Positional index vs. emission-order
    counter is the whole difference this function makes: skipping an
    unrecognized-treatment entry leaves a gap in ``torsion_index``
    rather than shifting every later entry's index down by one. TCKDB
    only requires ``torsion_index`` to be unique within the record, not
    contiguous (``BundleStatmechIn.validate_unique_torsion_indices`` /
    ``StatmechTorsionInBundle.validate_unique_torsion_indices``, plus a
    DB ``UniqueConstraint(statmech_id, torsion_index)`` with no
    contiguity requirement), so the gap is valid, not a defect.

    This does **not** restore joinability to ARC's ``rotors_dict``
    ordinal numbering, and should not be described that way:
    ``statmech.torsions[]`` is itself already compacted relative to
    ``rotors_dict`` before it ever reaches the adapter —
    ``_get_torsions`` (``ARC:arc/output.py:1435-1438``) iterates
    ``spc.rotors_dict.items()`` and skips any rotor whose
    ``success is not True``, so a rotor dropped for that reason leaves
    no trace (no gap, no placeholder) in ``output.yml`` at all, and ARC
    exports no raw rotor ordinal alongside it. ``torsion_index`` is
    therefore only ever a stable, unique reference into *this record's
    own* torsions list as ARC wrote it — a real and sufficient property
    for TCKDB's uniqueness rule, but not a restored cross-reference to
    ARC's internal rotor numbering, which the adapter has no way to
    recover.
    """
    if not isinstance(torsions, list):
        return []
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(torsions, start=1):
        if not isinstance(entry, Mapping):
            continue
        treatment = entry.get("treatment")
        if schema_1_3 and treatment is None:
            slim: dict[str, Any] = {"torsion_index": position}
        elif treatment not in _ARC_TO_TCKDB_TORSION_TREATMENTS:
            continue
        else:
            slim = {"torsion_index": position, "treatment_kind": treatment}
        sym = entry.get("symmetry_number")
        if isinstance(sym, int) and sym >= 1:
            slim["symmetry_number"] = sym

        coordinates = _coerce_torsion_coordinates(entry.get("atom_indices"))
        if coordinates is None and require_coordinates:
            if dropped_without_coordinates is not None:
                dropped_without_coordinates.append(position)
            continue
        if coordinates is not None:
            slim["dimension"] = len(coordinates)
            slim["coordinates"] = coordinates
        else:
            atom_indices = entry.get("atom_indices")
            if atom_indices is not None:
                logger.warning(
                    "TCKDB statmech: torsion #%d has unusable atom_indices=%r; "
                    "emitting torsion summary without coordinates.",
                    position, atom_indices,
                )
        # Link the torsion to its underlying scan calc when ARC provided a
        # bundle-local key. Computed-reaction bundles namespace scan calcs
        # per-species (``r0_scan_rotor_0``, etc.) because the server
        # enforces global calc-key uniqueness across the whole bundle —
        # the caller passes a ``scan_key_renames`` map so the torsion's
        # reference matches the namespaced calc key. Computed-species
        # bundles have a single species and pass ``None``: the original
        # un-prefixed key is already unique.
        scan_key = entry.get("source_scan_key") or entry.get("source_scan_calculation_key")
        if isinstance(scan_key, str) and scan_key and scan_key in (omitted_scan_keys or set()):
            if dropped_links is not None:
                dropped_links.append((position, scan_key))
        elif isinstance(scan_key, str) and scan_key:
            if scan_key_renames is not None:
                scan_key = scan_key_renames.get(scan_key, scan_key)
            slim["source_scan_calculation_key"] = scan_key
        out.append(slim)
    return out


_W_TORSION_NOT_SENT = ArcWarning.TORSION_NOT_SENT.value
_W_TORSION_WITHOUT_COORDINATES = ArcWarning.TORSION_WITHOUT_COORDINATES.value
_W_REJECTED_TORSION_NOT_SENT = ArcWarning.REJECTED_TORSION_NOT_SENT.value
_REJECTED_TORSION_NO_REASON = "ARC rejected this rotor and recorded no reason"


def _build_rejected_torsions(
    rejected: Any,
    *,
    first_index: int,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "statmech",
    with_note: bool = True,
) -> list[dict[str, Any]]:
    """Build ``StatmechTorsionIn`` entries for ARC's ``statmech.rejected_torsions``.

    ``with_note`` is false for the bundle torsion models, which have no ``note``
    (ARC's ``rotor_index`` is then not sent).

    ARC's ``_get_rejected_torsions`` (``arc/output.py``) lists each rotor it
    decided against (``success is False``; pending rotors are not rejections)
    with ``rotor_index``, the free-text ``invalidation_reason`` (verbatim,
    possibly concatenated over troubleshooting rounds, and ``''`` when ARC
    recorded none), ``atom_indices``, ``pivot_atoms``, ``dimension`` and, when
    a scan log exists, ``source_log``. TCKDB's only home for a rejected rotor
    is ``torsions[].invalidated_reason``.

    Each entry carries the reason, the dihedral coordinates when ``atom_indices``
    is well-formed, ``dimension`` as their count, and ARC's ``rotor_index`` in
    ``note``. ARC states no ``treatment_kind`` or ``symmetry_number`` for a
    rejected rotor, so none is sent, and no scan calculation is linked (the
    conformer route accepts no scan calculations and ``source_log`` is a file
    path, not a calculation). An empty reason is sent as the plain statement
    that ARC recorded none, so the torsion still reads as invalidated; the
    reason is never invented. TCKDB refuses a torsion whose coordinate count
    differs from its dimension, so a rotor with unusable ``atom_indices`` is not
    sent, with a ``rejected_torsion_not_sent`` warning.

    ``first_index`` is the first ``torsion_index`` free after the accepted
    rotors' (their index is their position in ARC's ``torsions`` list).
    """
    if not isinstance(rejected, list):
        return []
    out: list[dict[str, Any]] = []
    next_index = first_index
    for entry in rejected:
        if not isinstance(entry, Mapping):
            continue
        rotor_index = entry.get("rotor_index")
        coordinates = _coerce_torsion_coordinates(entry.get("atom_indices"))
        if coordinates is None:
            message = (
                f"ARC rejected rotor {rotor_index!r} but recorded no usable "
                f"atom_indices ({entry.get('atom_indices')!r}); a torsion needs its "
                f"dihedral coordinates, so this rejected rotor is not sent."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_REJECTED_TORSION_NOT_SENT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_REJECTED_TORSION_NOT_SENT, message, f"{warning_field}.torsions",
                    {"action": "rejected_torsion_omitted", "rotor_index": rotor_index, "reason": "atom_indices_unusable"}))
            continue
        reason = entry.get("invalidation_reason")
        slim: dict[str, Any] = {
            "torsion_index": next_index,
            "dimension": len(coordinates),
            "coordinates": coordinates,
            "invalidated_reason": (
                reason if isinstance(reason, str) and reason.strip()
                else _REJECTED_TORSION_NO_REASON
            ),
        }
        if with_note and isinstance(rotor_index, int) and not isinstance(rotor_index, bool):
            slim["note"] = f"ARC rotor_index {rotor_index}"
        out.append(slim)
        next_index += 1
    return out


def _coerce_torsion_coordinates(
    atom_indices: Any,
) -> list[dict[str, int]] | None:
    """Validate and project ARC's atom_indices into TCKDB coordinate dicts.

    Returns the coordinate list when ``atom_indices`` is well-formed:

      * Flat list of 4 distinct positive ints → one coordinate.
      * List of N flat-4-int lists (each distinct, 4 atoms each) → N
        coordinates with ``coordinate_index`` running 1..N.

    Returns ``None`` when the input is missing, malformed, contains
    non-positive integers, or has duplicate atoms within a quartet —
    callers fall back to a coordinate-less torsion summary so a single
    bad rotor never blocks the whole upload. The 4-distinct-atoms check
    mirrors the server-side ``StatmechTorsionCoordinateIn`` validator;
    failing here gives a clearer producer-side message than letting the
    server 422.
    """
    if atom_indices is None:
        return None
    if not isinstance(atom_indices, list) or not atom_indices:
        return None
    # 1D shape: a flat list of exactly 4 ints.
    if all(isinstance(x, int) for x in atom_indices):
        if len(atom_indices) != 4:
            return None
        if not all(x >= 1 for x in atom_indices):
            return None
        if len(set(atom_indices)) != 4:
            return None
        a1, a2, a3, a4 = atom_indices
        return [{
            "coordinate_index": 1,
            "atom1_index": a1, "atom2_index": a2,
            "atom3_index": a3, "atom4_index": a4,
        }]
    # ND shape: a list of 4-int sub-lists.
    if all(isinstance(x, list) for x in atom_indices):
        coords: list[dict[str, int]] = []
        for i, quartet in enumerate(atom_indices, start=1):
            if not all(isinstance(x, int) for x in quartet):
                return None
            if len(quartet) != 4:
                return None
            if not all(x >= 1 for x in quartet):
                return None
            if len(set(quartet)) != 4:
                return None
            a1, a2, a3, a4 = quartet
            coords.append({
                "coordinate_index": i,
                "atom1_index": a1, "atom2_index": a2,
                "atom3_index": a3, "atom4_index": a4,
            })
        return coords or None
    return None


#: Marks a record whose energies the adapter withheld (see ``_withhold_unreliable_composite_energies``).
_ENERGY_WITHHELD_KEY = "_tckdb_energy_withheld"
_W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03 = ArcWarning.G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03.value
#: Marks a ``thermo`` record whose E0-derived content is withheld (``_build_thermo_block`` strips it).
_THERMO_ENTHALPY_WITHHELD_KEY = "_tckdb_thermo_enthalpy_withheld"
_GAUSSIAN16_A03_BANNER = re.compile(r"gaussian\s*16\b.*\brevision\s*a\.?03\b", re.IGNORECASE | re.DOTALL)
_G4_FAMILY_METHOD_KEYS = frozenset({"g4", "g4mp2"})


def _g4_loader_energy_unreliable(
    output_doc: Mapping[str, Any], record: Mapping[str, Any],
) -> tuple[str, str] | None:
    """``(method, banner)`` when the record's energy is one Arkane's loader mis-reads, else ``None``.

    Stopgap for the G4 / G4MP2 energy of a Gaussian 16 Revision A.03 composite run: ARC reads
    the energy from the number after the ``G4(0 K)`` / ``G4MP2(0 K)`` label (Arkane's
    ``load_energy``, and ARC's parser alike), but on that revision the summary table is shifted
    by one label, so the number there is the 298 K value and ARC's ``sp_energy_hartree`` is
    wrong by 9-11 kJ/mol. The true E0 is the archive ``\\G4=`` / ``\\G4MP2=`` value, which
    ARC does not export (ARC_TCKDB_EXPORT_BRIEF.md, Bug 8).

    The record must be a composite run (``composite_log``) whose composite method (the record's
    ``levels.composite``, else the header ``composite_method``) is G4 or G4MP2 and whose
    composite program banner (``ess_versions.composite``) is Gaussian 16 Revision A.03. Nothing
    is inferred: a missing banner or method is "not this case".
    """
    if not record.get("composite_log"):
        return None
    versions = record.get("ess_versions")
    banner = versions.get("composite") if isinstance(versions, Mapping) else None
    if not isinstance(banner, str) or _GAUSSIAN16_A03_BANNER.search(banner) is None:
        return None
    level = None
    levels = arc13.levels_of(record)
    if levels is not None and isinstance(levels.get("composite"), Mapping):
        level = levels["composite"]
    if level is None or not level.get("method"):
        header = output_doc.get("composite_method")
        level = header if isinstance(header, Mapping) else None
    if level is None or not isinstance(level.get("method"), str):
        return None
    key = method_identity_key(level["method"])
    if key not in _G4_FAMILY_METHOD_KEYS:
        return None
    return key, banner


def _withhold_unreliable_composite_energies(
    output_doc: Mapping[str, Any],
    record: Mapping[str, Any],
    warnings: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    """The record without the energies ARC read wrongly (G4 / G4MP2 on Gaussian 16 Rev A.03).

    Returns ``record`` itself when it is not that case. Otherwise a copy with
    ``sp_energy_hartree`` unset (so no sp energy and no sp link on the statmech energy), its
    ``thermo`` marked so the thermo block is built without its E0-derived content (H298, NASA,
    point H and G; ``_strip_enthalpy_content``, the path the enthalpy refusals use) and with S298,
    Cp and point S kept, and the record marked so the composite role (which would also link the
    composite job as the energy) is withheld too. Warns once per record.

    Only an output 1.3 record carries ``ess_versions.composite``, so an earlier document never
    matches ``_g4_loader_energy_unreliable`` and is not guarded.
    """
    hit = _g4_loader_energy_unreliable(output_doc, record) if isinstance(record, Mapping) else None
    if hit is None:
        return record
    method, banner = hit
    label = record.get("label") or record.get("original_label") or "<unlabeled>"
    message = (
        f"The energies of {label!r} were not sent: it is a {method.upper()} composite run on "
        f"{banner!r}, whose summary table shifts the {method.upper()}(0 K) label by one entry, so "
        "the energy ARC and Arkane read for it is the 298 K value, 9-11 kJ/mol from E0. What is derived "
        "from that energy is withheld until ARC exports E0 from the archive (ARC_TCKDB_EXPORT_BRIEF.md, Bug 8): "
        "the single-point energy, H298, the NASA polynomials, the point enthalpies and Gibbs energies, and the "
        "statmech energy links. S298, Cp and the point entropies, which come from statmech and not from E0, "
        "are kept."
    )
    if not any(w.get("code") == _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03
               and (w.get("context") or {}).get("label") == label for w in (warnings or ())):
        logger.warning("TCKDB %s: %s", _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message, f"{label}.energies",
                {"action": "record_energies_withheld", "label": label, "method": method, "ess_version": banner}))
    out = dict(record)
    out["sp_energy_hartree"] = None
    if isinstance(record.get("thermo"), Mapping):
        out["thermo"] = {**record["thermo"], _THERMO_ENTHALPY_WITHHELD_KEY: True}
    out[_ENERGY_WITHHELD_KEY] = _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03
    return out


def _withhold_unreliable_energies_in_doc(
    output_doc: Mapping[str, Any], warnings: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    """``output_doc`` with every affected species / TS record neutralised and the kinetics built on them dropped.

    Returns ``output_doc`` unchanged when no record is affected. See
    ``_withhold_reaction_kinetics`` for the kinetics.
    """
    changed = False

    def neutralise(records: Any) -> Any:
        nonlocal changed
        if not isinstance(records, list):
            return records
        out = []
        for record in records:
            fixed = _withhold_unreliable_composite_energies(output_doc, record, warnings)
            changed = changed or fixed is not record
            out.append(fixed)
        return out

    species = neutralise(output_doc.get("species"))
    transition_states = neutralise(output_doc.get("transition_states"))
    if not changed:
        return output_doc
    marked = dict(output_doc)
    if "species" in output_doc:
        marked["species"] = species
    if "transition_states" in output_doc:
        marked["transition_states"] = transition_states
    if isinstance(output_doc.get("reactions"), list):
        marked["reactions"] = [
            _withhold_reaction_kinetics(marked, reaction, warnings) for reaction in output_doc["reactions"]
        ]
    return marked


def _withhold_reaction_kinetics(
    output_doc: Mapping[str, Any], reaction: Mapping[str, Any], warnings: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    """The reaction without its kinetics when one of its participants' energies was withheld.

    A rate coefficient is fitted from the energies of the reactants, products and the TS, so one
    built on a withheld record is withheld too, with the same warning code. ``output_doc`` is the
    document after ``_withhold_unreliable_energies_in_doc`` (its records carry the marker).
    """
    if not isinstance(reaction, Mapping) or reaction.get("kinetics") is None:
        return reaction
    withheld = {
        str(record.get("label") or record.get("original_label"))
        for key in ("species", "transition_states") for record in output_doc.get(key) or ()
        if isinstance(record, Mapping) and record.get(_ENERGY_WITHHELD_KEY)
    }
    labels = {str(x) for x in (reaction.get("reactant_labels") or ())}
    labels |= {str(x) for x in (reaction.get("product_labels") or ())}
    labels.add(str(reaction.get("ts_label")))
    hit = sorted(labels & withheld)
    if not hit:
        return reaction
    if not any(w.get("code") == _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03 and w.get("field") == "kinetics"
               and (w.get("context") or {}).get("reaction") == reaction.get("label") for w in (warnings or ())):
        message = (
            f"The kinetics of reaction {reaction.get('label')!r} were not sent: they are fitted from the "
            f"energies of {hit}, which were withheld ({_W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03})."
        )
        logger.warning("TCKDB %s: %s", _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message, "kinetics",
                {"action": "kinetics_withheld", "reaction": reaction.get("label"), "withheld": hit}))
    return {**reaction, "kinetics": None}


def _with_composite_role(
    record: Mapping[str, Any], calc_keys_by_role: Mapping[str, str],
) -> dict[str, str]:
    """``calc_keys_by_role`` plus the ``composite`` role for a schema-1.3 composite run.

    On a composite run (``composite_log`` stated) the composite job is what
    produced the record's geometry, so the ``opt`` calculation, filed at the
    composite level with the composite program, is the composite-method
    calculation; thermo and statmech link it under the ``composite`` role as well
    as ``opt``. Unchanged for any other record.
    """
    out = dict(calc_keys_by_role)
    if record.get(_ENERGY_WITHHELD_KEY):
        return out      # the composite job is the energy source, and that energy is withheld
    if (arc13.software_job_key(record, "opt") == "composite"
            and arc13.is_composite_run(record) and out.get(_CALC_KEY_OPT)):
        out[_ROLE_COMPOSITE] = out[_CALC_KEY_OPT]
    return out


def _build_statmech_source_calculations(
    *,
    calc_keys_by_role: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Compose statmech ``source_calculations`` from the bundle's emitted calcs.

    Walks ``_STATMECH_CALC_ROLES`` in order (opt → freq → sp) and emits
    one entry per role whose key is present in the caller-supplied
    ``calc_keys_by_role`` map. ``opt_coarse`` is excluded by design —
    it's not a ``StatmechCalculationRole`` enum value.

    The role keys (``"opt"`` / ``"freq"`` / ``"sp"``) are the
    ``StatmechCalculationRole`` enum strings. Values are the bundle-local
    calculation keys the server should resolve. Both computed-species
    and computed-reaction call this with their own scoped values; this
    helper is mode-agnostic.
    """
    return [
        {"calculation_key": calc_keys_by_role[role], "role": role}
        for _, role in _STATMECH_CALC_ROLES
        if role in calc_keys_by_role and calc_keys_by_role[role]
    ]


def _ts_unmapped_smiles_handle(
    *,
    ts_record: Mapping[str, Any],
    reaction_record: Mapping[str, Any],
    species_index: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Derive a deterministic textual handle for the TS, or ``None``.

    Resolution order, lifted directly from the spec:

    1. ``ts_record['smiles']`` when ARC already attached a textual TS
       identifier (rare in production — TS records usually have SMILES
       null because there's no Lewis structure).
    2. A canonical reaction-SMILES handle ``"<r1>.<r2>>><p1>.<p2>"``
       built from the reactant/product species' SMILES looked up
       through ``species_index``. ``.`` joins species; ``>>`` separates
       reactant and product sides — the standard interchange format
       and unambiguous from ARC's data. This is *not* a claim that the
       TS itself is a single molecule; it's the same data the field
       intends to carry — a traceability handle.
    3. ``None`` when any reactant or product lacks a SMILES, or when
       the reaction has no reactant/product labels. The producer
       refuses to emit a misleading half-handle; TCKDB stores NULL.

    Determinism: the helper is a pure function of the input mappings.
    No paths, timestamps, or run identifiers leak in. Idempotency keys
    will move only if the upstream species SMILES change.
    """
    direct = ts_record.get("smiles")
    if direct:
        text = str(direct).strip()
        if text:
            return text

    def _smiles_for_labels(labels: list[str] | None) -> list[str] | None:
        out: list[str] = []
        for label in labels or []:
            record = species_index.get(label)
            if not isinstance(record, Mapping):
                return None
            smiles = record.get("smiles")
            if not smiles:
                return None
            out.append(str(smiles).strip())
        return out or None

    reactant_smiles = _smiles_for_labels(reaction_record.get("reactant_labels"))
    if reactant_smiles is None:
        return None
    product_smiles = _smiles_for_labels(reaction_record.get("product_labels"))
    if product_smiles is None:
        return None

    return f"{'.'.join(reactant_smiles)}>>{'.'.join(product_smiles)}"


# Roles defined by TCKDB's ``KineticsCalculationRole`` enum; mirrored
# here so the adapter stays loud about unknown roles instead of
# bouncing through a 422 at upload time.
_KINETICS_ROLE_REACTANT_ENERGY = "reactant_energy"
_KINETICS_ROLE_PRODUCT_ENERGY = "product_energy"
_KINETICS_ROLE_TS_ENERGY = "ts_energy"
_KINETICS_ROLE_TS_FREQ = "freq"
_KINETICS_ROLE_TS_IRC = "irc"


def _coerce_optional_float(value: object) -> float | None:
    """Return value as float when possible, otherwise None."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Preferred lookup order for extracting a canonical tunneling-model
# label out of a structured value. Mappings are checked first by key,
# then arbitrary objects by attribute. Keep the two lists aligned;
# ``class_name`` is the attribute analogue of the ``class`` mapping
# key (which would be a builtin shadow on attribute access).
_TUNNELING_MODEL_KEYS = ("method", "model", "type", "name", "class")
_TUNNELING_MODEL_ATTRS = ("method", "model", "type", "name", "class_name")


def _stringify_tunneling_model(value: object) -> str | None:
    """Return a concise, stable tunneling-model label for TCKDB.

    Tunneling metadata reaches the adapter from output.yml as a free-form
    value (string today, possibly a structured object tomorrow). TCKDB
    stores ``tunneling_model`` as a free-form ``str | None``, so the
    adapter's job is to turn whatever ARC supplies into a stable,
    human-meaningful label without ever crashing payload construction
    on a malformed entry.
    """
    if not value:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Mapping):
        for key in _TUNNELING_MODEL_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    else:
        for attr in _TUNNELING_MODEL_ATTRS:
            candidate = getattr(value, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(value).strip()
    return encoded or None


# ``BundleKineticsIn.t0_k``: 0 < t0_k <= 10000 K (tckdb-schemas 0.63).
_MAX_ARRHENIUS_T0_K = 10000.0


def _build_kinetics_block(
    *,
    kinetics_record: Mapping[str, Any],
    reactant_keys: list[str],
    product_keys: list[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    ts_calc_keys: Mapping[str, str],
    long_kinetic_description: str | None = None,
    schema_1_3: bool = False,
) -> dict[str, Any] | None:
    """Build a ``BundleKineticsIn``-shaped dict from ARC kinetics.

    ``schema_1_3`` (an output.yml 1.3 document) also reads the kinetics run's
    ``comment`` (Arkane's comment on the fitted expression, verbatim),
    ``ts_validation`` (ARC's summary for a rate computed from a TS that failed
    its IRC check) and ``atom_corrections_applied`` (the atom-energy switch of
    the Arkane kinetics run) into the kinetics ``note``: ``BundleKineticsIn`` has
    a free-text ``note`` and no structured field for any of the three.

    Mapping (ARC → TCKDB):
        A, T0_k, n  → a = A (unnormalised), t0_k = T0_k when it is not 1 K, n
                      (RMG uses (T/T0)**n; TCKDB's ``t0_k``, 0.63, carries the T0)
        A_units     → a_units (via :func:`arc_to_tckdb_a_units`)
        n           → n
        Ea          → reported_ea
        Ea_units    → reported_ea_units (via :func:`arc_to_tckdb_ea_units`)
        Tmin_k      → tmin_k
        Tmax_k      → tmax_k
        dn          → n_uncertainty
        dEa         → d_reported_ea (only when dEa_units == Ea_units;
                      the live schema has no d_reported_ea_units field,
                      so dEa is only safe to emit when its units agree
                      with Ea's)
        dA          → a_uncertainty (+ a_uncertainty_kind="multiplicative")
                      Arkane/ARC dA is a multiplicative factor f, with
                      the true value bracketed by [A/f, A*f]. TCKDB's
                      ``KineticsUncertaintyKind`` exposes that semantic
                      explicitly, so the producer preserves dA verbatim
                      rather than dropping or re-encoding it. dA < 1.0
                      is omitted (server rejects multiplicative factors
                      below 1).

    ``source_calculations`` is populated from each actor's emitted
    sp/ts_sp/ts_freq/ts_irc keys. Reactant/product freq calcs are
    deliberately *not* linked as ``role=freq`` — in v0 kinetics that
    role is reserved for the TS frequency.

    Returns ``None`` when neither A nor Ea is populated — TCKDB's
    ``BundleKineticsIn`` allows empty kinetics, but a totally empty
    record is just noise and easier to omit than to send.

    When ``A`` (or ``Ea``) is present but its unit string doesn't map to
    a TCKDB enum (missing or unrecognized), the magnitude is *never*
    deposited without its unit — but the failure is scoped to that one
    field, not to the whole bundle. ``a``/``a_units`` (or
    ``reported_ea``/``reported_ea_units``) are both omitted and a
    WARNING is logged; ``n``, the other of A/Ea, ``Tmin_k``/``Tmax_k``
    and the rest of the kinetics block are still built and returned.
    Raising here would abort ``_build_computed_reaction_payload``
    entirely (the call site is unguarded) and discard the reaction's
    species blocks, TS block, geometries, IRC and path-search data for
    the sake of one optional rate-coefficient field — a wildly
    disproportionate response to an unresolvable unit string. See
    PHASE_C_PLAN.md C-4 for the original defect (a unitless magnitude
    silently shipped) and its adversarial-review follow-up (this
    proportionality fix) for why bare failure was replaced.
    """
    has_substantive_field = any(
        kinetics_record.get(k) is not None
        for k in ("A", "n", "Ea", "Tmin_k", "Tmax_k")
    )
    if not has_substantive_field:
        return None

    block: dict[str, Any] = {
        "reactant_keys": list(reactant_keys),
        "product_keys": list(product_keys),
        "model_kind": "modified_arrhenius",
    }

    # A rate coefficient's pre-exponential factor is meaningless without
    # its unit. ``arc_to_tckdb_a_units`` returns None both when A_units is
    # absent and when it's a string the map doesn't recognize; either way,
    # depositing ``a`` alone would ship a unitless number that reads as
    # correct and silently isn't. Note this is a routine, *valid* ARC
    # shape, not just a hand-written/third-party edge case: ARC's own
    # ``output.yml`` schema requires ``A_units`` to be present but allows
    # it to be ``null`` (``arc/schemas/output_yml_schema.json``:
    # ``"A_units": {"type": ["string", "null"]}``, in ``required``), and
    # ``arc/output.py`` deliberately emits ``null`` whenever ``A`` isn't a
    # ``(value, unit)`` tuple. So omit ``a``/``a_units`` and log a WARNING
    # rather than raise — a single unresolvable unit on an optional field
    # must not abort the whole reaction bundle (species, TS, geometries,
    # IRC, path-search) via the unguarded call site in
    # ``_build_computed_reaction_payload``. ``a_units`` has no coupling
    # validator on ``BundleKineticsIn`` (unlike ``reported_ea``/
    # ``reported_ea_units`` — see the Ea handling below), so omitting both
    # is guaranteed to still validate.
    a = kinetics_record.get("A")
    if a is not None:
        try:
            a_value = float(a)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed A=%r (%s)", a, exc)
        else:
            a_units = arc_to_tckdb_a_units(kinetics_record.get("A_units"))
            if a_units is None:
                logger.warning(
                    "TCKDB kinetics: A=%r has no recognized TCKDB unit "
                    "(A_units=%r); omitting a/a_units rather than "
                    "depositing a pre-exponential factor with no unit. "
                    "Add the ARC string to _ARC_TO_TCKDB_A_UNITS if it is "
                    "a legitimate unit, or fix the producer. The rest of "
                    "the kinetics block is still built.",
                    a_value, kinetics_record.get("A_units"),
                )
            else:
                block["a"] = a_value
                block["a_units"] = a_units

    n = kinetics_record.get("n")
    if n is not None:
        try:
            block["n"] = float(n)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed n=%r (%s)", n, exc)

    # RMG/Arkane fits k = A * (T/T0)**n * exp(-Ea/RT). TCKDB (tckdb-schemas
    # 0.63) states the same law: ``t0_k`` is "the reference temperature T0 of the
    # Arrhenius expression, in K, meaning k = A * (T / T0)**n * exp(-Ea / (R * T))",
    # it defaults to 1 K ("the plain A T^n form and what every record deposited
    # before this release meant") and "the server stores ``a`` as sent (it is A at
    # T0, not A rescaled)". So ARC's A is sent unnormalised beside ARC's own T0_k
    # (the adapter used to send A / T0**n and lose the T0 it was fitted with).
    # Older exports omitted T0 entirely and used the 1 K convention: their rates
    # come from Arkane's ``Arrhenius().fit_to_data(...)`` (RMG-Py
    # arkane/kinetics.py), called without T0, whose default is T0=1 K
    # (rmgpy/kinetics/arrhenius.pyx ``fit_to_data(..., double T0=1)``, stored as
    # ``self.T0 = (T0, "K")``; ``Arrhenius.__init__`` also defaults T0=(1.0, "K")).
    # Current output always writes ``T0_k``. A record with no T0_k key at all is
    # kept as 1 K by maintainer decision (adapter 0.6.0) for pre-contract output
    # only (no ``t0_k`` sent: 1 K is TCKDB's default); an explicit null or invalid
    # T0 is unknown, not evidence for 1 K, and so is one the contract refuses
    # (``0 < t0_k <= 10000``): ``a`` is then not sent, since A at an unstated
    # reference temperature would be read as A at 1 K.
    if "a" in block and "T0_k" in kinetics_record:
        raw_t0 = kinetics_record.get("T0_k")
        try:
            if isinstance(raw_t0, bool):
                raise ValueError("T0_k must be a number")
            t0 = float(raw_t0)
            if not math.isfinite(t0) or not 0 < t0 <= _MAX_ARRHENIUS_T0_K:
                raise ValueError(
                    f"T0_k must satisfy 0 < t0_k <= {_MAX_ARRHENIUS_T0_K:g} K (tckdb-schemas 0.63)")
            if t0 != 1.0:
                block["t0_k"] = t0
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "TCKDB kinetics: cannot state T0_k=%r (%s); omitting a/a_units.",
                raw_t0, exc,
            )
            block.pop("a", None)
            block.pop("a_units", None)

    # Same policy as A/A_units above, and for the same reason: an
    # activation energy with no unit is not a smaller version of the
    # right answer, it's an unrecoverable one. But same fix as above too
    # — omit the pair and warn rather than raise. TCKDB's own
    # ``validate_ea_pair`` (``BundleKineticsIn`` model validator) already
    # requires ``reported_ea``/``reported_ea_units`` to be both provided
    # or both omitted, so omitting both here is exactly what the schema
    # wants when only one half is resolvable, and it costs nothing extra
    # server-side (no failed round trip) since the adapter never sends
    # the broken half. ``ea_units`` is computed unconditionally because
    # the dEa fallback below reuses it.
    ea = kinetics_record.get("Ea")
    ea_units = arc_to_tckdb_ea_units(kinetics_record.get("Ea_units"))
    if ea is not None:
        try:
            ea_value = float(ea)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed Ea=%r (%s)", ea, exc)
        else:
            if ea_units is None:
                logger.warning(
                    "TCKDB kinetics: Ea=%r has no recognized TCKDB unit "
                    "(Ea_units=%r); omitting reported_ea/reported_ea_units "
                    "rather than depositing an activation energy with no "
                    "unit. The rest of the kinetics block is still built.",
                    ea_value, kinetics_record.get("Ea_units"),
                )
            else:
                block["reported_ea"] = ea_value
                block["reported_ea_units"] = ea_units

    for arc_key, payload_key in (("Tmin_k", "tmin_k"), ("Tmax_k", "tmax_k")):
        v = kinetics_record.get(arc_key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed %s=%r (%s)", arc_key, v, exc)
            continue
        if f <= 0:
            logger.warning(
                "TCKDB kinetics: %s=%r is non-positive; field will be omitted "
                "(server requires gt 0).", arc_key, v,
            )
            continue
        block[payload_key] = f

    dn = kinetics_record.get("dn")
    if dn is not None:
        try:
            block["n_uncertainty"] = float(dn)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed dn=%r (%s)", dn, exc)

    # dEa policy: ARC's dEa_units may differ from Ea_units. TCKDB has
    # only one Ea unit per kinetics row (no separate d_reported_ea_units
    # column today), so we can only safely emit d_reported_ea when the
    # producer reported it in the same units as Ea *and* that unit is
    # actually on the wire as ``reported_ea_units`` — otherwise the
    # number would be silently misinterpreted as the wrong unit, or (the
    # "both unitless" case — same defect C-4 exists to prevent, applied
    # here too) shipped with no unit anywhere in the record at all.
    # ``ea_units is not None`` alone is not enough: it only proves the
    # string *resolved*, not that ``reported_ea``/``reported_ea_units``
    # actually made it into ``block`` (Ea itself may be absent or
    # malformed even when Ea_units resolves).
    dea = kinetics_record.get("dEa")
    if dea is not None:
        dea_units_raw = kinetics_record.get("dEa_units")
        dea_units = arc_to_tckdb_ea_units(dea_units_raw) if dea_units_raw else ea_units
        if (
            dea_units is not None
            and dea_units == ea_units
            and "reported_ea_units" in block
        ):
            try:
                block["d_reported_ea"] = float(dea)
            except (TypeError, ValueError) as exc:
                logger.warning("TCKDB kinetics: malformed dEa=%r (%s)", dea, exc)
        else:
            logger.warning(
                "TCKDB kinetics: dEa=%r has no unambiguous unit to ship "
                "with (dEa_units=%r, Ea_units=%r, reported_ea_units on "
                "wire=%s); omitting d_reported_ea rather than depositing "
                "an uncertainty with no unit or a mismatched one.",
                dea, dea_units_raw, kinetics_record.get("Ea_units"),
                "reported_ea_units" in block,
            )

    # dA policy: Arkane/ARC dA is a multiplicative uncertainty factor
    # (true A in [A/f, A*f]). TCKDB's ``KineticsUncertaintyKind.multiplicative``
    # encodes exactly that, so we forward dA verbatim — never re-derive
    # it as A*(dA-1) or fold it into an additive band. Pair the value
    # with ``a_uncertainty_kind="multiplicative"`` (the schema requires
    # both fields to appear together, and rejects multiplicative factors
    # below 1.0).
    da = kinetics_record.get("dA")
    if da is not None:
        try:
            da_f = float(da)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed dA=%r (%s)", da, exc)
        else:
            if da_f < 1.0:
                logger.debug(
                    "TCKDB kinetics: dA=%r is below 1.0; multiplicative factors "
                    "must be >= 1, so a_uncertainty will be omitted.",
                    da,
                )
            else:
                block["a_uncertainty"] = da_f
                block["a_uncertainty_kind"] = "multiplicative"

    # Tunneling method ARC applied to the fit (currently always Eckart;
    # surfaced through output.yml so the adapter doesn't have to hardcode
    # the constant alongside the producer template). Absent → omit the
    # field for backward compat with older output.yml versions. The
    # contract's ``TunnelingModel`` enum holds lowercase tokens; send the
    # token the server's own ``normalize_tunneling_model`` would store
    # (ARC's "Eckart" is ``eckart``, an unknown label ``other``), so the
    # payload matches the shipped JSON Schema, not just the lenient model.
    tunneling_label = normalize_tunneling_model(
        _stringify_tunneling_model(kinetics_record.get("tunneling"))
    )
    if tunneling_label:
        block["tunneling_model"] = tunneling_label

    # ``degeneracy`` is not sent: ARC's kinetics record has no degeneracy key
    # (arc/output.py::_rxn_to_dict), TCKDB reads a missing value as NULL, and
    # the producer must not default it to 1 or infer it from stoichiometry.
    #
    # Free-form note: the reaction-level ``long_kinetic_description`` is what
    # ARC writes (there is no ``kinetics.note``); it feeds TCKDB's
    # ``KineticsCreate.note`` slot.
    note_parts: list[str] = []
    if isinstance(long_kinetic_description, str) and long_kinetic_description.strip():
        note_parts.append(long_kinetic_description.strip())
    if schema_1_3:
        # The comment is Arkane's, verbatim, and for a rate whose TS failed the
        # IRC check it already ends in the ``ts_validation`` marker; the marker
        # is added only when the comment does not carry it. The switch states
        # what the run's E0s are (an absolute energy plus ZPE when off); a
        # barrier fitted within one run is unaffected either way.
        comment = kinetics_record.get("comment")
        comment = comment.strip() if isinstance(comment, str) else ""
        if comment:
            note_parts.append(comment)
        ts_validation = kinetics_record.get("ts_validation")
        ts_validation = ts_validation.strip() if isinstance(ts_validation, str) else ""
        if ts_validation and ts_validation not in comment:
            note_parts.append(ts_validation)
        switch = kinetics_record.get("atom_corrections_applied")
        if isinstance(switch, bool):
            note_parts.append(
                "Arkane kinetics run atom energy corrections: "
                + ("applied." if switch else "not applied (E0 values are absolute "
                   "electronic energy plus ZPE)."))
    if note_parts:
        block["note"] = "\n".join(note_parts)

    sources = _build_kinetics_source_calculations(
        reactant_keys=reactant_keys,
        product_keys=product_keys,
        actor_calc_keys=actor_calc_keys,
        ts_calc_keys=ts_calc_keys,
    )
    if sources:
        block["source_calculations"] = sources

    return block


def _build_kinetics_source_calculations(
    *,
    reactant_keys: list[str],
    product_keys: list[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    ts_calc_keys: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Compose the kinetics ``source_calculations`` list explicitly.

    Producer policy: declare exactly the links we have evidence for.

    - Each reactant/product contributes its sp calc (if present) under
      ``reactant_energy``/``product_energy``.
    - The TS contributes ts_sp under ``ts_energy``.
    - The TS contributes ts_freq under ``freq`` (kinetics ``freq`` role
      is the *TS* frequency in v0, not reactant freq).
    - The TS contributes ts_irc under ``irc`` only when ARC produced
      and persisted an IRC calc.

    Anything missing is omitted — the spec is explicit that a missing
    source link is preferable to a fabricated one.
    """
    links: list[dict[str, Any]] = []
    # A species may fill several slots (H + H, or one species on both
    # sides); its sp is linked once per role because the schema refuses a
    # repeated (calculation_key, role) pair. Reactant and product roles
    # differ, so a species on both sides keeps both links.
    seen_links: set[tuple[str, str]] = set()
    for role, keys in (
        (_KINETICS_ROLE_REACTANT_ENERGY, reactant_keys),
        (_KINETICS_ROLE_PRODUCT_ENERGY, product_keys),
    ):
        for actor_key in keys:
            sp_key = actor_calc_keys.get(actor_key, {}).get(_CALC_KEY_SP)
            if sp_key and (sp_key, role) not in seen_links:
                seen_links.add((sp_key, role))
                links.append({"calculation_key": sp_key, "role": role})
    ts_sp = ts_calc_keys.get(_CALC_KEY_SP)
    if ts_sp:
        links.append({"calculation_key": ts_sp, "role": _KINETICS_ROLE_TS_ENERGY})
    ts_freq = ts_calc_keys.get(_CALC_KEY_FREQ)
    if ts_freq:
        links.append({"calculation_key": ts_freq, "role": _KINETICS_ROLE_TS_FREQ})
    ts_irc = ts_calc_keys.get(_CALC_KEY_IRC)
    if ts_irc:
        links.append({"calculation_key": ts_irc, "role": _KINETICS_ROLE_TS_IRC})
    return links


# IRC direction labels mirror TCKDB's ``IRCDirection`` enum. They are
# ESS path-direction labels only — the producer does not infer
# reactant/product side from them.
_IRC_DIRECTION_FORWARD = "forward"
_IRC_DIRECTION_REVERSE = "reverse"
_IRC_DIRECTION_BOTH = "both"


def _detect_irc_direction(log_path: str) -> str | None:
    """Detect IRC direction (forward/reverse) from a log filename.

    ARC writes per-direction IRC logs whose filenames carry an explicit
    direction infix. The two patterns seen in production runs:

    - ``..._irc_f.log`` / ``..._irc_r.log``   (compact runtime convention)
    - ``..._forward.log`` / ``..._reverse.log`` (long-form / test fixtures)

    Returns ``None`` if the filename matches neither — caller still
    emits the trajectory but omits the per-point direction. ``None``
    is also the right answer when ARC has consolidated forward+reverse
    into a single log; current ARC adapters don't produce that shape,
    but the caller treats it correctly anyway.
    """
    name = Path(str(log_path)).name.lower()
    if "irc_f" in name or "forward" in name:
        return _IRC_DIRECTION_FORWARD
    if "irc_r" in name or "reverse" in name:
        return _IRC_DIRECTION_REVERSE
    return None


def _conformer_observed_lists(
    species_record: Mapping[str, Any],
) -> tuple[list[Any], list[Any]] | None:
    """``(conformer_ess_software, conformer_ess_version)`` when ARC states both, index-aligned with ``conformers``.

    Output.yml 1.3 at ARC #1059 ebc88ec8 emits them with ``conformers``; the earlier 1.3 draft had
    neither (the fallback is the header ``conformer_opt_level``'s program). A pair that is not aligned
    with ``conformers`` is not used.
    """
    conformers = species_record.get("conformers")
    software = species_record.get("conformer_ess_software")
    version = species_record.get("conformer_ess_version")
    if (isinstance(conformers, (list, tuple)) and isinstance(software, list)
            and isinstance(version, list)
            and len(software) == len(version) == len(conformers)):
        return software, version
    return None


def _conformer_observed_provenance(
    species_record: Mapping[str, Any], index: int,
) -> tuple[str | None, str | None]:
    """``(program, banner)`` of conformer ``index``'s optimization log, each ``None`` when not stated.

    The banner is only meaningful with its program: it is dropped when the program is ``None``.
    """
    lists = _conformer_observed_lists(species_record)
    if lists is None:
        return None, None
    program, banner = lists[0][index], lists[1][index]
    program = program.strip() if isinstance(program, str) and program.strip() else None
    banner = banner.strip() if program and isinstance(banner, str) and banner.strip() else None
    return program, banner


def _stated_conformer_level(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any], index: int,
) -> Mapping[str, Any] | None:
    """The level of conformer ``index`` (output.yml 1.3 ``conformer_levels``) with its program, or ``None``.

    ARC states the level of the optimization job behind each conformer geometry
    but, like every level inside a record, no ``software``. Where ARC also states
    the program of each conformer's own optimization log (``conformer_ess_software``,
    ARC #1059 ebc88ec8) that observed program is the one used and a ``null`` entry
    (no ESS optimization, no log recorded, or none identified) means not filed. Otherwise
    (the earlier 1.3 draft) the program is the header
    ``conformer_opt_level``'s, the level the conformer jobs were requested at
    (software included), accepted only when it names the same level as the
    conformer's own; a conformer re-run at a troubleshooting level, or an adaptive
    ``conf_opt`` (which leaves the header level null), has none and is not filed.
    """
    level = species_record["conformer_levels"][index]
    if not isinstance(level, Mapping):
        return None
    if _conformer_observed_lists(species_record) is not None:
        program = _conformer_observed_provenance(species_record, index)[0]
        return {**level, "software": program} if program else None
    header = output_doc.get("conformer_opt_level")
    if not isinstance(header, Mapping):
        return None
    software = header.get("software")
    if not software:
        return None
    if _arc_level_to_tckdb_lot(level) != _arc_level_to_tckdb_lot(header):
        return None
    return {**level, "software": software}


_E_H_KJ_MOL_KIND = "electronic_kj_mol"


def _stated_conformer_opt_result(
    species_record: Mapping[str, Any], index: int, level: Mapping[str, Any],
) -> dict[str, Any] | None:
    """``opt_result`` for a conformer whose energy ARC states as an electronic energy at its own level.

    ``conformer_energies`` is sent only when ``conformer_energy_kind`` says it is an
    electronic energy in kJ/mol and ``conformer_energy_level`` is the conformer's
    optimization level (a conformer single point at another level overwrites the
    optimization energy and would not be this opt's energy). A force-field energy
    (kcal/mol, no level) is never sent.
    """
    if species_record.get("conformer_energy_kind") != _E_H_KJ_MOL_KIND:
        return None
    energy_level = species_record.get("conformer_energy_level")
    if not isinstance(energy_level, Mapping):
        return None
    if _arc_level_to_tckdb_lot(energy_level) != _arc_level_to_tckdb_lot(level):
        return None
    energies = species_record.get("conformer_energies")
    if not isinstance(energies, (list, tuple)) or index >= len(energies):
        return None
    energy = energies[index]
    if isinstance(energy, bool) or not isinstance(energy, (int, float)) or not math.isfinite(energy):
        return None
    from tckdb_arc._vendor import E_h_kJmol
    return {"final_energy_hartree": float(energy) / E_h_kJmol}


def _stated_conformer_sp(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any], index: int,
    conf_level: Mapping[str, Any],
) -> tuple[Mapping[str, Any], float] | None:
    """``(level with program, hartree)`` of a conformer single point ARC states, else ``None``.

    When the screen ran conformer single points (``conformer_energy_level`` is not
    the conformer's own optimization level), the stated electronic energy is the
    single point's. Its program is the header ``conformer_sp_level``'s, accepted
    only when that names the same level as ``conformer_energy_level``.
    """
    if species_record.get("conformer_energy_kind") != _E_H_KJ_MOL_KIND:
        return None
    energy_level = species_record.get("conformer_energy_level")
    header = output_doc.get("conformer_sp_level")
    if not isinstance(energy_level, Mapping) or not isinstance(header, Mapping):
        return None
    if _arc_level_to_tckdb_lot(energy_level) == _arc_level_to_tckdb_lot(conf_level):
        return None  # the optimization's own energy, see _stated_conformer_opt_result
    software = header.get("software")
    if not software or _arc_level_to_tckdb_lot(energy_level) != _arc_level_to_tckdb_lot(header):
        return None
    energies = species_record.get("conformer_energies")
    if not isinstance(energies, (list, tuple)) or index >= len(energies):
        return None
    energy = energies[index]
    if isinstance(energy, bool) or not isinstance(energy, (int, float)) or not math.isfinite(energy):
        return None
    from tckdb_arc._vendor import E_h_kJmol
    return {**energy_level, "software": software}, float(energy) / E_h_kJmol


_W_CONFORMER_NOT_ESS_OPTIMIZED = ArcWarning.CONFORMER_NOT_ESS_OPTIMIZED.value
_W_CONFORMER_PROGRAM_NOT_STATED = ArcWarning.CONFORMER_PROGRAM_NOT_STATED.value


def _warn_conformer_not_esss_optimized(
    warnings: list[dict[str, Any]] | None, *, label: Any, omitted: int, force_field: Any,
) -> None:
    ff = f" (force field {force_field})" if isinstance(force_field, str) and force_field else ""
    message = (
        f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC states no "
        "ESS optimization level for them (conformer_levels is null: a force-field "
        f"geometry{ff}, a user-supplied conformer, or a restart that predates level "
        "recording), and TCKDB requires an ESS level of theory and program on every "
        "calculation, so a force-field geometry is not a calculation."
    )
    logger.warning("TCKDB %s: %s", _W_CONFORMER_NOT_ESS_OPTIMIZED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_CONFORMER_NOT_ESS_OPTIMIZED, message, "conformers",
            {"action": "screened_conformers_omitted", "omitted_count": str(omitted)}))


def _warn_conformer_program_not_stated(
    warnings: list[dict[str, Any]] | None, *, label: Any, omitted: int, observed: bool = False,
) -> None:
    if observed:
        message = (
            f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC states the "
            "level of their optimization but their conformer_ess_software entry is null (the "
            "optimization log was not recorded, is missing, or states no program), and the "
            "program of a calculation is never deduced from a requested level."
        )
    else:
        message = (
            f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC states the "
            "level of their optimization but not the program (no level inside a record states "
            "one), and the header conformer_opt_level, the only level that names a program, "
            "is null or names another level than theirs (an adaptive conf_opt, or a conformer "
            "re-run at a troubleshooting level)."
        )
    logger.warning("TCKDB %s: %s", _W_CONFORMER_PROGRAM_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_CONFORMER_PROGRAM_NOT_STATED, message, "conformers",
            {"action": "screened_conformers_omitted", "omitted_count": str(omitted)}))


_W_CONFORMER_LEVEL_NOT_STATED = ArcWarning.CONFORMER_LEVEL_NOT_STATED.value


def _warn_conformer_level_not_stated(
    warnings: list[dict[str, Any]] | None,
    *,
    label: Any,
    omitted: int,
) -> None:
    message = (
        f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC "
        "screens conformers at its conformer level "
        "(default wb97xd/def2svp, not the opt level), output.yml does not "
        "export that level, and the project's restart.yml does not give a "
        "usable one (no conformer_opt_level naming its program, conf_opt not "
        "run, or a species-specific adaptive level), so a calculation for "
        "them cannot state one. ARC should export the conformer level "
        "(BRIDGE_ROADMAP B3)."
    )
    logger.warning("TCKDB %s: %s", _W_CONFORMER_LEVEL_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_CONFORMER_LEVEL_NOT_STATED, message, "conformers",
            {"action": "screened_conformers_omitted", "omitted_count": str(omitted)}))


_W_TS_GUESS_LEVEL_NOT_STATED = ArcWarning.TS_GUESS_LEVEL_NOT_STATED.value
_W_TS_GUESS_SOFTWARE_NOT_STATED = ArcWarning.TS_GUESS_SOFTWARE_NOT_STATED.value


def _warn_ts_guess_level_not_stated(
    warnings: list[dict[str, Any]] | None,
    *,
    ts_label: Any,
    method: str,
    reason: str | None = None,
    software: bool = False,
    schema_states_level: bool | None = None,
) -> None:
    code = _W_TS_GUESS_SOFTWARE_NOT_STATED if software else _W_TS_GUESS_LEVEL_NOT_STATED
    if reason is not None:
        why = reason if software else (
            "could not build it from ARC's exported "
            f"{'GSM' if method == 'gsm' else 'NEB'} level ({reason})")
    elif method == "neb":
        why = "ARC did not export neb_level for this run"
    elif method == "gsm" and schema_states_level:
        why = (
            "ARC's gsm_level is null: the archived xtb outputs beside the GSM log "
            "do not all show GFN2-xTB with the TS record's charge and spin, or none "
            "were archived"
        )
    else:
        why = (
            f"ARC exports no level for the {method.upper()} path search "
            "(only the ORCA NEB level, neb_level, is exported)"
        )
    message = (
        f"The {method.upper()} path-search calculation of the TS guess for "
        f"{ts_label!r} was not uploaded: {why}. TCKDB requires a level of "
        "theory and a program on every calculation and the adapter does not "
        "file the path search at opt_level or under a deduced program."
        + ("" if schema_states_level else
           " ARC should export the TS-guess level (BRIDGE_ROADMAP B3).")
    )
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append(_self_check(
            code, message, "transition_state.path_search.level_of_theory",
            {"action": "path_search_calculation_omitted", "method": method}))


# ARC ``adaptive_levels`` and the other levels output.yml does not record (see
# ``tckdb_arc.adaptive``). ``TCKDBAdapter._with_adaptive_levels`` marks a copy of
# the output document with the project's ``restart.yml`` state
# (``_RESTART_KEY``) and, for an adaptive run, the job types the adaptive levels
# name (``_ADAPTIVE_LEVELS_KEY``). output.yml records one level per run, so for
# a named job type it states a level no particular species need have run at:
# ``_resolve_level`` replays ARC's own per-species choice from ``restart.yml``
# where that is possible, and otherwise states nothing.
_ADAPTIVE_LEVELS_KEY = "_tckdb_adaptive_levels"
_RESTART_KEY = "_tckdb_restart_levels"

# The ARC job types (``arc/scheduler.py`` ``run_job``) behind each level kind the
# adapter states; adaptive levels key on these exact strings.
_JOB_TYPES_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "opt": ("opt",),
    "freq": ("freq",),
    "sp": ("sp",),
    "composite": ("composite",),
    "irc": ("irc",),
    "scan": ("scan", "directed_scan"),
    "conf_opt": ("conf_opt",),
}

_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE: Mapping[str, str] = ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE
_W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE = ArcWarning.ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE.value


def _header_adaptive_job_types(output_doc: Mapping[str, Any]) -> set[str]:
    """The job types the schema-1.3 header ``adaptive_levels`` entries name (``"opt freq"`` is two)."""
    entries = output_doc.get("adaptive_levels")
    types: set[str] = set()
    if not isinstance(entries, list):
        return types
    for entry in entries:
        levels = entry.get("levels") if isinstance(entry, Mapping) else None
        if isinstance(levels, Mapping):
            for key in levels:
                types.update(str(key).split())
    return types


def _adaptive_marker(output_doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    marker = output_doc.get(_ADAPTIVE_LEVELS_KEY)
    return marker if isinstance(marker, Mapping) else None


def _restart_levels(output_doc: Mapping[str, Any]) -> RestartInfo | None:
    info = output_doc.get(_RESTART_KEY)
    return info if isinstance(info, RestartInfo) else None


def _adaptive_kind_named(output_doc: Mapping[str, Any], kind: str) -> bool:
    """Whether the run's adaptive levels name any job type behind ``kind``."""
    marker = _adaptive_marker(output_doc)
    return marker is not None and any(
        t in marker["named"] for t in _JOB_TYPES_BY_KIND.get(kind, (kind,)))


def _n_heavy_from_record(record: Mapping[str, Any] | None) -> int | None:
    """ARC's fallback heavy-atom count: the non-``H`` atoms of the geometry."""
    symbols = _species_element_symbols(record) if isinstance(record, Mapping) else None
    return None if symbols is None else sum(1 for symbol in symbols if symbol != "H")


def _adaptive_level_status(
    output_doc: Mapping[str, Any], record: Mapping[str, Any] | None, job_type: str,
) -> Mapping[str, Any] | str:
    """The level ARC ran ``job_type`` at for ``record``, from ``restart.yml``.

    A level dict (same shape as output.yml levels), ``RUN_LEVEL`` (the
    species' adaptive range does not name the job type), or ``UNDETERMINABLE``
    (no ``restart.yml`` adaptive spec or species entry, or no heavy-atom
    count).
    """
    restart = _restart_levels(output_doc)
    if restart is None or record is None:
        return UNDETERMINABLE
    return restart.attribute_level(
        record.get("label"), job_type, _n_heavy_from_record(record))


def _note_adaptive_omission(
    output_doc: Mapping[str, Any], kind: str, record: Mapping[str, Any] | None,
) -> None:
    marker = _adaptive_marker(output_doc)
    label = record.get("label") if isinstance(record, Mapping) else None
    if marker is not None and isinstance(label, str):
        marker["omitted"].setdefault(kind, set()).add(label)


def _energy_level_unattributable(
    output_doc: Mapping[str, Any], energy_level: Mapping[str, Any] | None,
) -> bool:
    """Whether an adaptive run left the species' energy level (sp, or composite) unknown."""
    return _adaptive_marker(output_doc) is not None and energy_level is None


def _irc_level(
    output_doc: Mapping[str, Any], ts_record: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, bool]:
    """``(exact IRC level or None, IRC level unattributable)`` for a TS.

    Exact when the adaptive levels name ``irc`` and ``restart.yml`` gives the
    TS's level, else when ``restart.yml`` records ``irc_level``. ``(None,
    False)`` means the level is the unreadable settings default (assumed to be
    ``opt_level``); ``(None, True)`` means the adaptive levels name ``irc``
    and the TS's level cannot be worked out.
    """
    if arc13.has_levels(ts_record):
        # Schema 1.3: ``levels.irc`` (or the one level every ``irc_log_levels``
        # entry shares) is the level the IRC jobs ran at, also under adaptive
        # levels; the header ``irc_level`` is the requested level of the run
        # and stands in only when the record states none. The program is
        # ``ess_software.irc`` (no level here states one). Nothing stated is
        # never assumed to be the opt level.
        stated, level = arc13.irc_recorded_level(ts_record)
        if not stated:
            header = output_doc.get("irc_level")
            level = header if isinstance(header, Mapping) and header.get("method") else None
            stated = level is not None
        if not stated or level is None:
            if _adaptive_kind_named(output_doc, "irc"):
                _note_adaptive_omission(output_doc, "irc", ts_record)
            return None, True
        return _with_observed_irc_program(level, ts_record), False
    if _adaptive_kind_named(output_doc, "irc"):
        status = _adaptive_level_status(output_doc, ts_record, "irc")
        if isinstance(status, Mapping):
            return _with_irc_program(status), False
        if status == UNDETERMINABLE:
            _note_adaptive_omission(output_doc, "irc", ts_record)
            return None, True
    restart = _restart_levels(output_doc)
    if restart is not None and restart.irc_level is not None:
        return _with_irc_program(restart.irc_level), False
    return None, False


# ARC's ``Level.deduce_software(job_type='irc')`` (arc/level.py:413-418, called
# by ``Scheduler.deduce_job_adapter``, arc/scheduler.py:1255) sets the software
# to Gaussian for an IRC whatever software the level dict names or DLPNO/other
# methods would pick: that is why a level's own ``software`` cannot be taken for
# an IRC (there is also no ``ess_software.irc``). It is not unconditional: UMA
# levels return early with ``ase`` (arc/level.py:386-388), and torchani / xtb /
# gfn methods are assigned afterwards (arc/level.py:421-426). Those give no
# stated program, so the IRC is not filed for them.
_IRC_NOT_GAUSSIAN_METHODS = ("uma", "uma-s-1", "uma-s-1p1", "uma-s-1p2", "uma-m-1p1")


def _with_observed_irc_program(
    level: Mapping[str, Any], ts_record: Mapping[str, Any],
) -> Mapping[str, Any]:
    """``level`` with the program ARC observed on the IRC logs (``ess_software.irc``), else none."""
    out = {k: v for k, v in level.items() if k != "software"}
    ess_software = ts_record.get("ess_software")
    observed = ess_software.get("irc") if isinstance(ess_software, Mapping) else None
    if observed:
        out["software"] = str(observed)
    return out


def _with_irc_program(level: Mapping[str, Any]) -> Mapping[str, Any]:
    """``level`` with ARC's IRC program: ``gaussian``, or none where ARC's rule gives another."""
    method = str(level.get("method") or "").lower()
    out = {k: v for k, v in level.items() if k != "software"}
    if not (method in _IRC_NOT_GAUSSIAN_METHODS
            or any(part in method for part in ("torchani", "xtb", "gfn"))):
        out["software"] = "gaussian"
    return out


def _conformer_screen_level(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The level ARC's conformer screen ran at for a species, or ``None``.

    ``conformer_opt_level`` from ``restart.yml``, only when ``conf_opt`` ran
    (``job_types``) and the level names its program; not when the adaptive
    levels name ``conf_opt`` for the species' range or the range cannot be
    worked out.
    """
    restart = _restart_levels(output_doc)
    if (restart is None or restart.conformer_opt_level is None
            or restart.job_types.get("conf_opt") is not True
            or not restart.conformer_opt_level.get("software")):
        return None
    if _adaptive_kind_named(output_doc, "conf_opt"):
        status = _adaptive_level_status(output_doc, species_record, "conf_opt")
        if status != RUN_LEVEL:
            return None
    return restart.conformer_opt_level


_SCAN_ROTOR_KEY_RE = re.compile(r"^scan_rotor_(\d+)$")


def _scan_job_type(
    output_doc: Mapping[str, Any], record: Mapping[str, Any], scan_key: Any,
) -> str | None:
    """``'scan'`` (ESS) or ``'directed_scan'`` for an exported rotor scan.

    output.yml does not say which kind ran; ``restart.yml`` records each
    rotor's ``directed_scan_type`` (``arc/species/species.py``), and the export
    key is ``scan_rotor_<rotor index>`` (``arc/output.py``). ``None`` when that
    cannot be looked up: the level then cannot tell the two apart.
    """
    restart = _restart_levels(output_doc)
    match = _SCAN_ROTOR_KEY_RE.match(scan_key) if isinstance(scan_key, str) else None
    if restart is None or match is None or not isinstance(record, Mapping):
        return None
    return restart.scan_job_type(record.get("label"), int(match.group(1)))


def _warn_adaptive_omissions(
    warnings: list[dict[str, Any]] | None, marker: Mapping[str, Any],
) -> None:
    """One warning per job kind whose level could not be attributed (and was left out)."""
    sources = ", ".join(marker["sources"])
    for kind, labels in sorted(marker["omitted"].items()):
        code = _W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.get(kind)
        if code is None or not labels:
            continue
        if "output.yml adaptive_levels" in marker["sources"]:
            # Output.yml 1.3 states the per-record level of the opt, freq, sp,
            # composite and IRC jobs (``levels``); the scan and conformer levels of
            # an adaptive run are header levels left null when an entry names them.
            message = (
                f"ARC ran with adaptive_levels (found in {sources}), which assigns the "
                f"{kind} level per species by heavy-atom count, and output.yml 1.3 "
                f"states no {kind} level for {', '.join(sorted(labels))} (the record's "
                f"levels entry is null, or the header {kind}_level is null because an "
                "adaptive entry names it). TCKDB requires a level on every "
                f"calculation, so their {kind} calculation(s) are omitted rather than "
                "labelled with a level they may not have run at."
            )
        else:
            message = (
                f"ARC ran with adaptive_levels (found in {sources}), which assigns the "
                f"{kind} level per species by heavy-atom count, but output.yml records "
                f"one {kind} level per run, and the project's restart.yml does not "
                f"allow the level of {', '.join(sorted(labels))} to be worked out "
                f"(it needs adaptive_levels and a species entry there). TCKDB requires "
                f"a level on every calculation, so their {kind} calculation(s) are "
                "omitted rather than labelled with a level they may not have run at. "
                "ARC should export per-species levels (BRIDGE_ROADMAP B2)."
            )
        logger.warning("TCKDB %s: %s", code, message)
        if warnings is not None:
            warnings.append(_self_check(
                code, message, f"{kind}.level_of_theory",
                {"action": f"{kind}_calculation_omitted", "adaptive_levels_sources": ",".join(marker["sources"]), "labels": ",".join(sorted(labels))}))


_W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION = ArcWarning.TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION.value


def _ts_irc_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    irc_calc_key: str | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
    reaction_record: Mapping[str, Any] | None = None,
    species_index: Mapping[str, Mapping[str, Any]] | None = None,
    ts_xyz_text: str | None = None,
) -> list[dict[str, Any]]:
    """Map ARC's ``ts_checks['IRC']`` verdict to TCKDB ``validation_evidence``.

    ``ts_checks['IRC']`` is ``True`` (the IRC connects the declared
    reactants and products), ``False`` (it does not) or ``None`` (not run, or
    no conclusion; arc/output.py ``_ts_checks_to_dict``). Only a bool is
    evidence; ``None`` or an absent ``ts_checks`` sends nothing, and the
    verdict is never derived from ``irc_converged``, which reports that the IRC
    jobs completed, not that the IRC validated anything.

    TCKDB binds evidence to an ``irc`` calculation of this transition state
    (``source_calculation_key``; the standalone route omits the key and binds
    to its single irc calculation). Without one there is nothing to bind to,
    so a verdict with no IRC calculation is reported and not sent. ``rationale``
    is required and non-empty: it states the source of ``passed``. ARC's
    ``ts_checks['warnings']`` are not used: they come only from the E0/e_elect
    and normal-mode-displacement checks (``arc/checks/ts.py``,
    ``arc/checks/nmd.py``), never from the IRC.

    ARC output schema 1.3 states the participant mappings
    (``irc_participant_mapping``); :func:`_irc_participant_mappings` turns them
    into ``reactant_participant_mapping`` / ``product_participant_mapping`` on
    a passed record, or sends neither side and says why.
    """
    checks = ts_record.get("ts_checks")
    verdict = checks.get("IRC") if isinstance(checks, Mapping) else None
    if not isinstance(verdict, bool):
        return []
    if irc_calc_key is None:
        message = (
            f"ARC recorded ts_checks['IRC'] = {verdict} for {ts_label!r} but the "
            "TS has no IRC calculation in this upload, so there is no irc "
            "calculation for validation evidence to bind to; the evidence was "
            "not sent."
        )
        logger.warning("TCKDB %s: %s", _W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION, message, "transition_state.validation_evidence",
                {"action": "validation_evidence_omitted", "ts_checks_irc": str(verdict).lower()}))
        return []
    mappings = None
    if verdict is True and reaction_record is not None and species_index is not None:
        mappings = _irc_participant_mappings(
            ts_record,
            reaction_record=reaction_record,
            species_index=species_index,
            ts_xyz_text=ts_xyz_text,
            ts_label=ts_label,
            warnings=warnings,
        )
    return [_ts_evidence.irc_record(
        verdict,
        source_calculation_key=irc_calc_key,
        rationale=f"ARC ts_checks['IRC'] = {verdict}",
        participant_mappings=mappings,
    )]


_W_TS_ENERGY_ORDERING_NOT_SENT = ArcWarning.TS_ENERGY_ORDERING_NOT_SENT.value
_W_TS_IMAGINARY_MODE_NOT_SENT = ArcWarning.TS_IMAGINARY_MODE_NOT_SENT.value
_W_TS_NMD_FORCED_CONTRADICTS_INDEX = ArcWarning.TS_NMD_FORCED_CONTRADICTS_INDEX.value
# The e_elect check's margin (``arc/checks/ts.py``), in kJ/mol; hartree convert with the vendored ``E_h_kJmol``.
_ENERGY_ORDERING_MARGIN_KJ_MOL = 1.0


def _warn_ts_evidence_not_sent(
    warnings: list[dict[str, Any]] | None, *, code: str, ts_label: Any, message: str,
    context: Mapping[str, Any] | None = None,
) -> None:
    """Report a TS validation verdict ARC states that is not sent (the adapter never guesses around it)."""
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append(_self_check(
            code, message, "transition_state.validation_evidence",
            {"action": "validation_evidence_omitted", "ts_label": str(ts_label), **(context or {})}))


def _ts_energy_ordering_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    ts_sp_key: str | None,
    reactant_labels: Sequence[str],
    product_labels: Sequence[str],
    reactant_keys: Sequence[str],
    product_keys: Sequence[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    species_index: Mapping[str, Mapping[str, Any]],
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Map ARC's ``ts_checks['e_elect']`` verdict to a TCKDB ``energy_ordering`` record (bundle route only).

    ARC's check (``arc/checks/ts.py::check_rxn_e_elect``) is ``ts_e_elect > sum(reactants) + 1 kJ/mol``
    and ``> sum(products) + 1 kJ/mol`` over the participants (each with its stoichiometric multiplier).
    It leaves ``e_elect`` unset (``None``) whenever the zero-point-corrected ``E0`` check passed, and a
    record's ``passed`` is required, so ``None`` sends nothing. Only the *electronic* energies are sent
    (each participant's ``sp_energy_hartree``, cited to its own ``sp`` calculation); ``e0`` is not:
    ARC's ``e0_kj_mol`` carries corrections and is not one calculation's absolute energy. ARC's ``E0``
    verdict is only mentioned in ``rationale``.

    Participants are numbered by their position in ``reactant_keys`` / ``product_keys`` (a repeated
    species repeats its entry, as the bundle repeats the key). TCKDB refuses a passing record whose
    stated numbers do not put the saddle point above each side, so a ``True`` verdict is first
    re-derived from the stated hartree values with ARC's margin; when they contradict it the record is
    not sent. A ``False`` verdict whose stated numbers do satisfy the ordering was computed on stale
    energies and is likewise not sent. A participant without a stated finite non-positive energy, or without an ``sp``
    calculation in the upload, also leaves the record out (a passing record needs every participant).
    """
    checks = ts_record.get("ts_checks")
    verdict = checks.get("e_elect") if isinstance(checks, Mapping) else None
    if not isinstance(verdict, bool):
        return []
    context = {"ts_checks_e_elect": str(verdict).lower()}

    def omit(reason: str, *, reason_code: str | None = None) -> list[dict[str, Any]]:
        _warn_ts_evidence_not_sent(
            warnings, code=_W_TS_ENERGY_ORDERING_NOT_SENT, ts_label=ts_label,
            context={**context, **({"reason": reason_code} if reason_code else {})},
            message=(f"ARC recorded ts_checks['e_elect'] = {verdict} for {ts_label!r}, but {reason}; "
                     "the energy_ordering evidence was not sent."))
        return []

    entries: list[tuple[str, str, Any, str | None]] = [
        ("ts", str(ts_label), ts_record.get("sp_energy_hartree"), ts_sp_key)]
    for side, labels, keys in (("reactant", reactant_labels, reactant_keys),
                               ("product", product_labels, product_keys)):
        if len(labels) != len(keys):
            return omit(f"the {side} labels and keys disagree")
        for position, (label, key) in enumerate(zip(labels, keys), start=1):
            entries.append((f"{side}:{position}", str(label),
                            (species_index.get(label) or {}).get("sp_energy_hartree"),
                            (actor_calc_keys.get(key) or {}).get("sp")))
    energies: list[dict[str, Any]] = []
    for participant, label, raw, key in entries:
        energy = _finite_float(raw)
        if energy is None:
            return omit(f"{participant} ({label!r}) has no stated finite sp_energy_hartree")
        if energy > 0:
            return omit(f"{participant} ({label!r}) has a positive sp_energy_hartree ({energy}); "
                        "TCKDB takes absolute (non-positive) energies")
        if not key:
            return omit(f"{participant} ({label!r}) has no sp calculation in this upload to cite")
        energies.append(_ts_evidence.energy_entry(participant, energy, key))
    # TCKDB refuses a pass its own numbers contradict; a failure they contradict is stale.
    contradiction = _ts_evidence.energy_ordering_contradiction(
        verdict, energies, margin_kj_mol=_ENERGY_ORDERING_MARGIN_KJ_MOL)
    if contradiction is not None:
        reason, reason_code = contradiction
        return omit(reason, reason_code=reason_code)
    rationale = f"ARC ts_checks['e_elect'] = {verdict} (electronic energies; sp_energy_hartree of each participant)"
    e0_verdict = checks.get("E0")
    if isinstance(e0_verdict, bool):
        rationale += f"; ARC ts_checks['E0'] = {e0_verdict} (not sent: not one calculation's energy)"
    return [_ts_evidence.energy_ordering_record(verdict, rationale, energies)]


def _ts_imaginary_mode_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    freq_calc_key: str | None,
    freq_result: Mapping[str, Any] | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Map ARC's ``ts_checks['freq']`` verdict to a TCKDB ``imaginary_mode`` record.

    ``passed`` is ``ts_checks['freq']`` when it is a bool, nothing otherwise. The record states what the
    TS frequency calculation found, read from the ``freq_result`` this upload sends for it, so the
    record cannot disagree with the result it cites (TCKDB refuses a count that differs, or a frequency
    more than 1 cm^-1 apart): ``imaginary_frequency_count`` is its ``n_imag``;
    ``imaginary_frequency_cm1`` is the designated reaction-coordinate mode's frequency (ARC's 1.3
    ``reaction_coordinate_mode_index`` into ``freq_frequencies_cm1_ess_order``, else tau / the window rule),
    or, with exactly one imaginary mode, ``imag_freq_cm1``; both are negated to the negative convention.
    ``mode_displacement_agrees`` is ``True`` only when ARC's own ``reaction_coordinate_mode_index`` is
    what designates the mode (ARC states it only for a genuine, non-forced normal-mode-displacement pass)
    and the TS's ``nmd_forced`` (output 1.3, ARC #1059 ebc88ec8) is not ``True``,
    ``False`` when ``ts_checks['NMD']`` is ``False`` with no index stated, or when ``nmd_forced`` is ``True``
    (ARC forces only a check that ran and failed, so the displacement disagreed), otherwise omitted (not
    assessed: an unrun or undecided check). ``nmd_forced: true`` never yields ``True``, and an index stated beside it is a contradiction that
    is reported (``ts_nmd_forced_contradicts_reaction_coordinate_index``) and not used. A passing record with more
    than one imaginary mode needs the cited result to designate the coordinate; otherwise it is not sent.
    On the standalone route the key is dropped by the caller (the record binds to the single freq).
    """
    checks = ts_record.get("ts_checks")
    verdict = checks.get("freq") if isinstance(checks, Mapping) else None
    if not isinstance(verdict, bool):
        return []
    context = {"ts_checks_freq": str(verdict).lower()}

    def omit(reason: str) -> list[dict[str, Any]]:
        _warn_ts_evidence_not_sent(
            warnings, code=_W_TS_IMAGINARY_MODE_NOT_SENT, ts_label=ts_label, context=context,
            message=(f"ARC recorded ts_checks['freq'] = {verdict} for {ts_label!r}, but {reason}; "
                     "the imaginary_mode evidence was not sent."))
        return []

    if freq_calc_key is None or freq_result is None:
        return omit("the TS has no frequency calculation in this upload, so there is no freq "
                    "calculation for the evidence to bind to")
    # What the cited frequency result states (a finite float or None for the frequency).
    n_imag, designated, value = _ts_evidence.freq_result_imaginary_facts(freq_result)
    if verdict and n_imag == 0:
        return omit("its frequency result reports no imaginary mode")
    if verdict and n_imag is not None and n_imag > 1 and designated is None:
        return omit(f"the frequency result has {n_imag} imaginary modes and designates no reaction "
                    "coordinate, which TCKDB requires of a passing record")
    mode_displacement_agrees: bool | None = None
    stated = ts_record.get("reaction_coordinate_mode_index")
    stated_index = stated if isinstance(stated, int) and not isinstance(stated, bool) else None
    nmd = checks.get("NMD")
    nmd_forced = ts_record.get("nmd_forced")
    rationale = f"ARC ts_checks['freq'] = {verdict}"
    if nmd_forced is True:
        # ARC sets ``nmd_forced`` only after ``check_normal_mode_displacement`` returned False and
        # ``skip_nmd`` overwrote ts_checks['NMD'] to True (arc/checks/ts.py); a None ("could not
        # decide") is never forced. So the displacement check ran and found the mode inconsistent
        # with the reaction: that is a ``False`` verdict, never ``True``. (TCKDB has no rule tying
        # ``mode_displacement_agrees`` to ``passed``.)
        mode_displacement_agrees = False
        rationale += ("; mode_displacement_agrees = False: ARC's normal mode displacement check failed "
                      "and the pass in ts_checks['NMD'] was forced (skip_nmd; nmd_forced = true)")
        if stated_index is not None:
            message = (
                f"ARC states reaction_coordinate_mode_index = {stated_index} for {ts_label!r} but also "
                "nmd_forced = true (the normal mode displacement check failed and the pass was forced); "
                "ARC states the index only for a genuine pass, so the index was not used as an NMD "
                "designation and mode_displacement_agrees was sent as False.")
            logger.warning("TCKDB %s: %s", _W_TS_NMD_FORCED_CONTRADICTS_INDEX, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_TS_NMD_FORCED_CONTRADICTS_INDEX, message, "transition_state.validation_evidence",
                    {"action": "reaction_coordinate_index_not_used", "ts_label": str(ts_label), "nmd_forced": "true", "reaction_coordinate_mode_index": str(stated_index)}))
    elif stated_index is not None and stated_index == designated and nmd is not False:
        mode_displacement_agrees = True
        rationale += (f"; mode_displacement_agrees from ARC's reaction_coordinate_mode_index = {stated_index} "
                      "(set only by a genuine normal mode displacement pass)")
    elif nmd is False and stated_index is None:
        mode_displacement_agrees = False
        rationale += "; mode_displacement_agrees from ARC ts_checks['NMD'] = False"
    return [_ts_evidence.imaginary_mode_record(
        verdict,
        source_calculation_key=freq_calc_key,
        imaginary_frequency_count=n_imag,
        frequency_cm1=value,
        mode_displacement_agrees=mode_displacement_agrees,
        rationale=rationale,
    )]


_W_IRC_PARTICIPANT_MAPPING_NOT_SENT = ArcWarning.IRC_PARTICIPANT_MAPPING_NOT_SENT.value
_W_ATOM_MAP_TS_ORDER_NOT_STATED = ArcWarning.ATOM_MAP_TS_ORDER_NOT_STATED.value
_W_REACTION_STOICHIOMETRY_NOT_STATED = ArcWarning.REACTION_STOICHIOMETRY_NOT_STATED.value
_W_REACTION_SPECIES_LABELS_CONTRADICTED = ArcWarning.REACTION_SPECIES_LABELS_CONTRADICTED.value


def _with_stated_participants(
    reaction_record: Mapping[str, Any],
    *,
    ts_record: Mapping[str, Any] | None,
    species_index: Mapping[str, Mapping[str, Any]],
    warnings: list[dict[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """The reaction record with one label per participant occurrence.

    ARC's ``reactant_labels`` / ``product_labels`` are ``list(rxn.reactants)``
    after ``remove_dup_species`` sorted and de-duplicated them, so ``HO2 + HO2
    <=> H2O2 + O2`` is exported as ``['HO2'] <=> ['H2O2', 'O2']`` and TCKDB
    refuses the unbalanced reaction. The occurrences are taken, in this
    order, from (0) ``reactant_species_labels`` / ``product_species_labels`` (output 1.3 at
    ARC #1059 ebc88ec8: one entry per occurrence, in ``get_reactants_and_products`` order, always
    stated for a reaction ARC ran; they are THE order of ``reactant_keys`` / ``product_keys``), else,
    for the earlier 1.3 draft that lacked them, from (a) ``atom_map_reactant_labels`` /
    ``atom_map_product_labels`` (one label per occurrence, present when ``atom_map`` is), then
    (b) the participants of the TS's ``irc_participant_mapping`` (repeats
    expanded, in ``position`` order); each must name exactly the species of
    the collapsed lists. Otherwise (c) the collapsed lists are used when their
    elements balance (from the species geometries; not checked when a geometry
    is missing), and the reaction is refused when they do not, with
    ``reaction_stoichiometry_not_stated``. The reaction label string is never
    parsed, and no repeat is guessed from a label.

    When (0) is stated it is refused (``reaction_species_labels_contradicted``) if it disagrees with
    ``atom_map_*_labels`` (which ARC says it equals when ``atom_map`` is not null), or names other
    species than the collapsed ``reactant_labels`` / ``product_labels``, or is not a list of labels.
    """
    collapsed = {side: [str(x) for x in reaction_record.get(key) or []]
                 for side, key in (("reactants", "reactant_labels"), ("products", "product_labels"))}

    species_labels = {side: reaction_record.get(f"{side[:-1]}_species_labels") for side in collapsed}
    if any(isinstance(v, list) and v for v in species_labels.values()):
        def contradicted(reason: str) -> ValueError:
            message = (
                f"reaction {reaction_record.get('label')!r}: ARC's reactant_species_labels / "
                f"product_species_labels {species_labels} are contradicted: {reason}; the "
                "reaction was not uploaded.")
            logger.warning("TCKDB %s: %s", _W_REACTION_SPECIES_LABELS_CONTRADICTED, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_REACTION_SPECIES_LABELS_CONTRADICTED, message, "reactant_keys",
                    {"action": "reaction_not_uploaded"}))
            return ValueError(message)

        if not all(isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)
                   for v in species_labels.values()):
            raise contradicted("one side is missing, empty, or not a list of labels")
        for side in collapsed:
            map_labels = reaction_record.get(f"atom_map_{side[:-1]}_labels")
            if map_labels is not None and list(map_labels) != species_labels[side]:
                raise contradicted(f"the {side} differ from atom_map_{side[:-1]}_labels {map_labels}, "
                                   "which ARC states they equal when atom_map is not null")
            if collapsed[side] and sorted(set(species_labels[side])) != sorted(set(collapsed[side])):
                raise contradicted(f"the {side} name other species than {side[:-1]}_labels "
                                   f"{collapsed[side]}")
        return {**reaction_record, "reactant_labels": list(species_labels["reactants"]),
                "product_labels": list(species_labels["products"])}

    def from_atom_map() -> dict[str, list[str]] | None:
        found = {side: reaction_record.get(f"atom_map_{side[:-1]}_labels")
                 for side in collapsed}
        return found if all(isinstance(v, list) and v for v in found.values()) else None

    def from_irc() -> dict[str, list[str]] | None:
        mapping = (ts_record or {}).get("irc_participant_mapping")
        if not isinstance(mapping, Mapping):
            return None
        found: dict[str, list[str]] = {}
        for side in collapsed:
            part = (mapping.get(side) or {}).get("participants") if isinstance(
                mapping.get(side), Mapping) else None
            if not isinstance(part, list) or not part or not all(
                    isinstance(p, Mapping) and isinstance(p.get("position"), int) for p in part):
                return None
            found[side] = [p.get("label") for p in sorted(part, key=lambda p: p["position"])]
        return found

    for source in (from_atom_map, from_irc):
        stated = source()
        if stated is None:
            continue
        stated = {side: [str(x) for x in labels] for side, labels in stated.items()}
        if all(sorted(set(stated[side])) == sorted(set(collapsed[side])) for side in collapsed):
            if all(Counter(stated[side]) == Counter(collapsed[side]) for side in collapsed):
                return reaction_record       # no repeat was lost; keep the exported order
            return {**reaction_record, "reactant_labels": stated["reactants"],
                    "product_labels": stated["products"]}

    counts = {}
    for side, labels in collapsed.items():
        symbols = [_xyz_element_symbols((species_index.get(label) or {}).get("xyz")) for label in labels]
        if not labels or any(sym is None for sym in symbols):
            return reaction_record           # nothing to check against
        counts[side] = Counter(sym for syms in symbols for sym in syms)
    if counts["reactants"] == counts["products"]:
        return reaction_record
    message = (
        f"reaction {reaction_record.get('label')!r}: ARC's reactant_labels {collapsed['reactants']} and "
        f"product_labels {collapsed['products']} are sorted and de-duplicated, are not atom-balanced, and "
        "ARC states no per-occurrence participants (atom_map_*_labels and irc_participant_mapping are "
        "absent), so a repeated species cannot be recovered; the reaction was not uploaded."
    )
    logger.warning("TCKDB %s: %s", _W_REACTION_STOICHIOMETRY_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_REACTION_STOICHIOMETRY_NOT_STATED, message, "reactant_keys",
            {"action": "reaction_not_uploaded"}))
    raise ValueError(message)


def _participant_slots(labels: Sequence[Any]) -> list[tuple[str, int]]:
    """``(label, occurrence)`` of each participant slot, in the order of ``labels``.

    TCKDB numbers a side's participants by their position in ``reactant_keys``
    / ``product_keys``, which the adapter builds from the reaction record's
    ``reactant_labels`` / ``product_labels``. ARC numbers the participants of
    ``irc_participant_mapping`` in the order of ``atom_map_reactant_labels`` /
    ``atom_map_product_labels`` (``r_species`` order), and ``reactant_labels``
    is sorted instead, so the two orders differ and ARC's ``position`` is not
    TCKDB's ``participant_index``. The label and its occurrence (the 1-based
    count of that label up to the slot, as ARC states it) identify the same
    participant in both.
    """
    seen: dict[str, int] = {}
    slots: list[tuple[str, int]] = []
    for label in labels:
        seen[str(label)] = seen.get(str(label), 0) + 1
        slots.append((str(label), seen[str(label)]))
    return slots


def _irc_participant_mappings(
    ts_record: Mapping[str, Any],
    *,
    reaction_record: Mapping[str, Any],
    species_index: Mapping[str, Mapping[str, Any]],
    ts_xyz_text: str | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[int]], dict[str, list[int]]] | None:
    """Translate ARC's ``irc_participant_mapping`` to TCKDB's two participant mappings.

    TCKDB's ``reactant_participant_mapping`` / ``product_participant_mapping``
    are ``{"reactant:N": [1-based transition-state atom indices]}`` and cover
    every transition-state atom exactly once on each side. ARC's mapping
    (output schema 1.3, ``irc_participant_mapping``) gives 0-based indices into
    each optimized IRC endpoint geometry, and those are transition-state atom
    indices only when ``atom_order_matches_ts`` is ``True``. Nothing is sent
    unless every claim TCKDB checks can be stated from what ARC says:

    * ``atom_order_matches_ts`` is ``True`` (otherwise the indices are not
      transition-state atoms);
    * ``sides_distinguishable`` is ``True`` (otherwise which endpoint is the
      reactants is only a convention ARC states it made);
    * each side's participants are exactly the participant slots of the
      reaction the upload declares, matched by label and occurrence, never by
      ARC's ``position``;
    * each side's indices partition the transition-state atoms, and each
      participant's atoms have the element counts of its species.

    Both sides are sent or neither (TCKDB refuses one side). ARC states the
    mapping only when the IRC verdict was established by graph isomorphism, so
    an absent mapping (``null``, or a pre-1.3 document) sends nothing and is not
    reported.
    """
    mapping = ts_record.get("irc_participant_mapping")
    if not isinstance(mapping, Mapping):
        return None

    def refuse(reason: str, **context: Any) -> None:
        message = (
            f"ARC states an IRC participant mapping for {ts_label!r} but it cannot be "
            f"sent as TCKDB participant mappings: {reason}. Neither side was sent."
        )
        logger.warning("TCKDB %s: %s", _W_IRC_PARTICIPANT_MAPPING_NOT_SENT, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_IRC_PARTICIPANT_MAPPING_NOT_SENT, message, "transition_state.validation_evidence",
                {"action": "irc_participant_mapping_omitted", **({key: str(value) for key, value in context.items()})}))
        return None

    if mapping.get("atom_order_matches_ts") is not True:
        return refuse(
            "atom_order_matches_ts is not true, so the endpoint atom indices are not "
            "transition-state atom indices",
            atom_order_matches_ts=mapping.get("atom_order_matches_ts"))
    if mapping.get("sides_distinguishable") is not True:
        return refuse(
            "the reactants and products are graph-isomorphic, so which IRC endpoint is the "
            "reactants is a convention, not something ARC established",
            sides_distinguishable=mapping.get("sides_distinguishable"))
    ts_symbols = _xyz_element_symbols(ts_xyz_text)
    if ts_symbols is None:
        return refuse("the transition-state geometry has no readable atoms")

    sides: list[dict[str, list[int]]] = []
    for side_name, prefix, labels_key in (
            ("reactants", "reactant", "reactant_labels"),
            ("products", "product", "product_labels")):
        side = mapping.get(side_name)
        participants = side.get("participants") if isinstance(side, Mapping) else None
        if not isinstance(participants, list) or not participants:
            return refuse(f"the {side_name} side states no participants")
        slots = _participant_slots(reaction_record.get(labels_key) or [])
        by_slot: dict[tuple[str, int], Mapping[str, Any]] = {}
        for participant in participants:
            if not isinstance(participant, Mapping):
                return refuse(f"a {side_name} participant is not a record")
            key = (str(participant.get("label")), participant.get("occurrence"))
            if key in by_slot:
                return refuse(f"{side_name} participant {key[0]!r} occurrence {key[1]} is listed twice")
            by_slot[key] = participant
        if set(by_slot) != set(slots):
            return refuse(
                f"the {side_name} participants (label, occurrence) "
                f"{sorted(map(str, by_slot))} are not the participants of the uploaded reaction "
                f"{sorted(map(str, slots))}")
        result: dict[str, list[int]] = {}
        claimed: list[int] = []
        for index, slot in enumerate(slots, start=1):
            atoms = by_slot[slot].get("atom_indices")
            if (not isinstance(atoms, list) or not atoms
                    or not all(isinstance(i, int) and not isinstance(i, bool)
                               and 0 <= i < len(ts_symbols) for i in atoms)):
                return refuse(f"{side_name} participant {slot[0]!r} has atom indices that are not "
                              f"0-based indices into the {len(ts_symbols)}-atom transition state")
            species_symbols = _xyz_element_symbols(
                (species_index.get(slot[0]) or {}).get("xyz"))
            if species_symbols is None or Counter(species_symbols) != Counter(
                    ts_symbols[i] for i in atoms):
                return refuse(f"the atoms assigned to {side_name} participant {slot[0]!r} are not "
                              "the elements of that species")
            result[f"{prefix}:{index}"] = sorted(i + 1 for i in atoms)
            claimed.extend(atoms)
        if sorted(claimed) != list(range(len(ts_symbols))):
            return refuse(f"the {side_name} participants do not cover every transition-state "
                          "atom exactly once")
        sides.append(result)
    return sides[0], sides[1]


def _warn_atom_map_not_sent(
    reaction_record: Mapping[str, Any],
    *,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None,
) -> None:
    """Report ARC's reaction ``atom_map`` (schema 1.3) that cannot be sent as TCKDB's ``atom_map``.

    TCKDB's ``ReactionAtomMapIn`` is ``{participant atom: transition-state atom}``
    for every participant, both sides 1-based, against the transition state
    geometry. ARC's ``atom_map`` is reactant-atom to product-atom, and its
    schema states that it "says nothing about the atom order of the transition
    state". No ARC key relates any participant atom to a transition-state atom
    one by one: ``irc_participant_mapping`` records only which atoms belong to
    which participant ("only atom-set membership is recorded, not the
    atom-to-atom correspondence inside a participant"). Writing TS atom ``i`` =
    reactant atom ``i`` would claim a TS order ARC does not state, and TCKDB
    never derives a map (ADR 0011). So no ``atom_map`` is built, and TCKDB
    reports ``reaction_atom_map_absent`` for the reaction.
    """
    atom_map = reaction_record.get("atom_map")
    if not isinstance(atom_map, list) or not atom_map:
        return
    if "ts_atom_map" in reaction_record:
        # ARC #1059 ebc88ec8 states ``ts_atom_map`` and, when it cannot, why.
        unavailable = reaction_record.get("ts_atom_map_unavailable_reason")
        _warn_ts_atom_map_not_sent(
            reaction_record, ts_label=ts_label, warnings=warnings,
            why=("ARC states ts_atom_map = null with ts_atom_map_unavailable_reason "
                 f"{unavailable!r}"),
            action="atom_map_omitted")
        return
    reason = (
        f"TCKDB's atom_map maps each participant atom to an atom of the transition-state "
        f"geometry {ts_label!r} and ARC states no relation between its map and the "
        "transition-state atom order (the output document has no ts_atom_map; it predates ARC "
        "#1059 ebc88ec8)"
    )
    message = (
        f"ARC states a reactant-to-product atom_map for {reaction_record.get('label')!r} "
        f"(source {reaction_record.get('atom_map_source')!r}), but {reason}, so no atom_map "
        "was sent. TCKDB will report reaction_atom_map_absent."
    )
    logger.warning("TCKDB %s: %s", _W_ATOM_MAP_TS_ORDER_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_ATOM_MAP_TS_ORDER_NOT_STATED, message, "atom_map",
            {"action": "atom_map_omitted", "atom_map_source": str(reaction_record.get("atom_map_source")), "atom_map_method": str(reaction_record.get("atom_map_method"))}))


_W_REACTION_TS_ATOM_MAP_NOT_SENT = ArcWarning.REACTION_TS_ATOM_MAP_NOT_SENT.value
_TS_ATOM_MAP_METHOD = "irc_endpoint_cgr_isomorphism"
_TS_ATOM_MAP_NOTE = (
    "ARC ts_atom_map (output.yml 1.3, method irc_endpoint_cgr_isomorphism, IRC endpoint {endpoint} "
    "served as the reactants): the TS atom of every reactant and product atom, from a label-preserving "
    "isomorphism of the condensed graph of reaction of the reaction (its atom_map) and that of the TS "
    "and its two IRC endpoint geometries. It is a constitutional (2D) correspondence: symmetry-equivalent "
    "atoms, including diastereotopic ones, are assigned by a deterministic convention that no geometry "
    "decides, and a different but equally valid assignment exists."
)


def _warn_ts_atom_map_not_sent(
    reaction_record: Mapping[str, Any],
    *,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None,
    why: str,
    action: str = "atom_map_omitted",
) -> None:
    """Report that no TCKDB ``atom_map`` was built from ARC's ``ts_atom_map`` (or its absence), and why."""
    message = (
        f"No atom_map was sent for {reaction_record.get('label')!r} (transition state {ts_label!r}): "
        f"{why}. TCKDB will report reaction_atom_map_absent."
    )
    logger.warning("TCKDB %s: %s", _W_REACTION_TS_ATOM_MAP_NOT_SENT, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_REACTION_TS_ATOM_MAP_NOT_SENT, message, "atom_map",
            {"action": action, "ts_atom_map_unavailable_reason": str(
                            reaction_record.get("ts_atom_map_unavailable_reason")), "atom_map_source": str(reaction_record.get("atom_map_source")), "atom_map_method": str(reaction_record.get("atom_map_method"))}))


_TS_ROUTE_TS_GEOMETRY_KEY = "ts_geom"


def _ts_route_atom_map_slots(
    reaction_record: Mapping[str, Any], species_index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The participant slots of a standalone TS upload, with the key and geometry each would carry.

    Keys (``reactant_1``) and geometry keys (``reactant_1_geom``) are unique per slot, as the route
    requires, even when one species fills several slots. A slot whose species has no usable geometry
    has ``geometry: None``.
    """
    slots: list[dict[str, Any]] = []
    for side, labels_key in (("reactant", "reactant_labels"), ("product", "product_labels")):
        for position, label in enumerate(reaction_record.get(labels_key) or [], start=1):
            record = species_index.get(label) or {}
            key = f"{side}_{position}"
            text = _normalize_xyz_text(record.get("xyz"), label) if record.get("xyz") else None
            geometry = _geometry_payload(
                record, text, record.get("xyz_isotopes"), key=f"{key}_geom") if text else None
            slots.append({"side": side, "index": position, "species_key": key,
                          "label": label, "geometry": geometry})
    return slots


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _tckdb_reaction_atom_map(
    reaction_record: Mapping[str, Any],
    *,
    ts_label: Any,
    ts_xyz_text: str | None,
    ts_geometry_key: str,
    participants: Sequence[Mapping[str, Any]],
    irc_evidence: Sequence[Mapping[str, Any]] = (),
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """TCKDB's ``ReactionAtomMapIn`` from ARC's ``ts_atom_map`` (output 1.3 at ARC #1059 ebc88ec8), or ``None``.

    ARC states, for every concatenated reactant atom and every concatenated product atom, the 0-based TS
    atom it is (``ts_atom_map.reactants`` / ``.products``), counting the participants in the order of
    ``atom_map_reactant_labels`` / ``atom_map_product_labels`` and each participant's atoms in the atom
    order of its exported geometry. ``participants`` are the uploaded slots, in upload order, each
    ``{side, index (1-based), species_key, label, geometry: {key, xyz_text} | None}``; for each, its block
    of the map becomes ``atom_to_ts`` ({participant geometry atom: TS atom}, both 1-based) and the map is
    ``source: inferred`` naming ARC's method and its symmetry-equivalent-atom convention (the note).

    Nothing is sent unless every check passes; a failure reports ``reaction_ts_atom_map_not_sent`` and
    sends no map (never a partial one): ``ts_atom_map`` is stated and well formed; the uploaded slot labels
    are exactly ``atom_map_*_labels``; each block length is the participant's uploaded geometry atom count;
    TS indices are in range and cover every TS atom once per side; each participant atom has the element of
    its TS atom; ``products[atom_map[i]] == reactants[i]``; and, when the IRC evidence sent a participant
    mapping, each participant's TS atoms are the ones that mapping gives it. When ARC states
    ``ts_atom_map = null`` (or the document predates the key) the omission is reported with ARC's reason.
    """
    atom_map = reaction_record.get("atom_map")
    if not isinstance(atom_map, list) or not atom_map:
        return None
    ts_map = reaction_record.get("ts_atom_map")
    if "ts_atom_map" not in reaction_record or ts_map is None:
        _warn_atom_map_not_sent(reaction_record, ts_label=ts_label, warnings=warnings)
        return None

    def refuse(why: str) -> None:
        _warn_ts_atom_map_not_sent(
            reaction_record, ts_label=ts_label, warnings=warnings,
            why=f"ARC's ts_atom_map cannot be sent as TCKDB's atom_map: {why}")
        return None

    if not isinstance(ts_map, Mapping):
        return refuse("it is not an object")
    reactants, products = ts_map.get("reactants"), ts_map.get("products")
    endpoint = ts_map.get("reactant_endpoint")
    if (ts_map.get("method") != _TS_ATOM_MAP_METHOD or not _is_plain_int(endpoint)
            or endpoint not in (1, 2) or not isinstance(ts_map.get("ts_atom_order_follows_reactants"), bool)
            or not all(isinstance(side, list) and all(_is_plain_int(i) for i in side)
                       for side in (reactants, products))):
        return refuse("it does not have the documented shape (method "
                      f"{ts_map.get('method')!r}, reactant_endpoint {endpoint!r})")
    if ts_map.get("ts_label") != ts_label:
        return refuse(f"it is for transition state {ts_map.get('ts_label')!r}, not {ts_label!r}")
    ts_symbols = _xyz_element_symbols(ts_xyz_text)
    if ts_symbols is None:
        return refuse("the transition-state geometry has no readable atoms")
    n_ts = len(ts_symbols)
    if not all(_is_plain_int(i) for i in atom_map):
        return refuse("ARC's atom_map is not a list of integers")
    sides: dict[str, list[Mapping[str, Any]]] = {"reactant": [], "product": []}
    for participant in participants:
        sides[participant["side"]].append(participant)
    for side, key in (("reactant", "atom_map_reactant_labels"), ("product", "atom_map_product_labels")):
        if [p["label"] for p in sides[side]] != list(reaction_record.get(key) or []):
            return refuse(f"the uploaded {side} slots {[p['label'] for p in sides[side]]} are not "
                          f"ARC's {key} {reaction_record.get(key)}, the order its map counts in")
    mapped = {"reactant": reactants, "product": products}
    symbols: dict[str, list[list[str]]] = {"reactant": [], "product": []}
    for side in sides:
        for participant in sides[side]:
            geometry = participant.get("geometry")
            found = _xyz_element_symbols(geometry.get("xyz_text") if isinstance(geometry, Mapping) else None)
            if found is None:
                return refuse(f"{side} {participant['index']} ({participant['label']!r}) has no readable "
                              "uploaded geometry")
            symbols[side].append(list(found))
        total = sum(len(block) for block in symbols[side])
        if len(mapped[side]) != total or len(atom_map) != total:
            return refuse(f"the {side} block lengths do not agree: ts_atom_map.{side}s has "
                          f"{len(mapped[side])} entries, atom_map has {len(atom_map)}, and the uploaded "
                          f"{side} geometries have {total} atoms")
        if sorted(mapped[side]) != list(range(n_ts)):
            return refuse(f"ts_atom_map.{side}s is not a one-to-one map onto the {n_ts} transition-state "
                          "atoms (an index is out of range, repeated, or a TS atom is not covered)")
    n = len(atom_map)
    if sorted(atom_map) != list(range(n)) or any(products[atom_map[i]] != reactants[i] for i in range(n)):
        return refuse("it is not consistent with ARC's atom_map (products[atom_map[i]] != reactants[i] "
                      "for some reactant atom i)")
    if ts_map["ts_atom_order_follows_reactants"] != (list(reactants) == list(range(n))):
        return refuse("ts_atom_order_follows_reactants disagrees with its own reactants list")

    entries: list[dict[str, Any]] = []
    claimed: dict[tuple[str, int], set[int]] = {}
    for side in ("reactant", "product"):
        offset = 0
        for participant, block in zip(sides[side], symbols[side]):
            atom_to_ts: dict[str, int] = {}
            for atom_index, element in enumerate(block):
                ts_atom = mapped[side][offset + atom_index]
                if ts_symbols[ts_atom].capitalize() != element.capitalize():
                    return refuse(
                        f"{side} {participant['index']} ({participant['label']!r}) atom {atom_index + 1} is "
                        f"{element} but the transition-state atom {ts_atom + 1} it maps to is "
                        f"{ts_symbols[ts_atom]}")
                atom_to_ts[str(atom_index + 1)] = ts_atom + 1
            offset += len(block)
            claimed[(side, participant["index"])] = set(atom_to_ts.values())
            entries.append({
                "side": side,
                "species_key": participant["species_key"],
                "participant_index": participant["index"],
                "geometry_key": participant["geometry"]["key"],
                "atom_to_ts": atom_to_ts,
            })
    for evidence in irc_evidence:
        if evidence.get("kind") != "irc":
            continue
        for side in ("reactant", "product"):
            given = evidence.get(f"{side}_participant_mapping")
            if not isinstance(given, Mapping):
                continue
            for slot, atoms in given.items():
                index = int(str(slot).split(":")[1])
                if claimed.get((side, index)) != set(atoms):
                    return refuse(
                        f"it contradicts the IRC participant mapping that is sent: {slot} has transition-state "
                        f"atoms {sorted(claimed.get((side, index), ()))} here and {sorted(atoms)} there")
    return {
        "source": "inferred",
        "ts_geometry_key": ts_geometry_key,
        "participants": entries,
        "note": _TS_ATOM_MAP_NOTE.format(endpoint=endpoint),
    }


_W_IRC_DIRECTION_NOT_STATED = ArcWarning.IRC_DIRECTION_NOT_STATED.value
# ARC runs every IRC job at ``irc_level`` (arc/scheduler.py ``run_irc_job``).
# restart.yml records it whenever it differs from the settings default, and
# the adapter then uses it exactly. When it is absent the level is that
# default, ``default_levels_of_theory['irc']`` (arc/settings/settings.py), which
# the adapter cannot read from the project files, and the adapter *assumes* it
# equals ``opt_level`` (true when ``opt_level`` is that same default, the
# maintainer's adapter-0.6.0 decision). That assumption is reported on the IRC
# calculation, until ARC exports the IRC level (BRIDGE_ROADMAP B3).
_W_IRC_LEVEL_NOT_STATED = ArcWarning.IRC_LEVEL_NOT_STATED.value


def _warn_irc_level_not_stated(
    warnings: list[dict[str, Any]] | None, *, ts_label: Any, ts_record: Mapping[str, Any],
) -> None:
    """Schema 1.3 states no usable IRC level for this TS, so its IRC calculation is not filed."""
    stated, _ = arc13.irc_recorded_level(ts_record)
    why = (
        "its forward and reverse IRC jobs ran at different levels (levels.irc is null "
        "and irc_log_levels differ), and one calculation cannot carry two levels"
        if stated else
        "ARC recorded no level for its IRC jobs (levels.irc and irc_log_levels are null) "
        "and states no header irc_level"
    )
    message = (
        f"The IRC calculation of {ts_label!r} was not uploaded: {why}. The adapter "
        "does not file an IRC at the opt level."
    )
    logger.warning("TCKDB %s: %s", _W_IRC_LEVEL_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_IRC_LEVEL_NOT_STATED, message, "transition_state.irc.level_of_theory",
            {"action": "irc_calculation_omitted"}))


_W_IRC_LEVEL_ASSUMED_OPT_LEVEL = ArcWarning.IRC_LEVEL_ASSUMED_OPT_LEVEL.value


def _warn_irc_level_assumed(
    warnings: list[dict[str, Any]] | None,
    level_of_theory: Mapping[str, Any],
) -> None:
    message = (
        "ARC does not export irc_level and the project's restart.yml does not "
        "record one, so the IRC ran at the run's settings default "
        "(default_levels_of_theory['irc']), which the adapter cannot read. "
        "The IRC calculation is labelled with opt_level "
        f"({level_of_theory.get('method')}"
        f"{'/' + str(level_of_theory['basis']) if level_of_theory.get('basis') else ''}), "
        "assuming the two are equal; that is wrong when opt_level is not the "
        "settings default."
    )
    logger.warning("TCKDB %s: %s", _W_IRC_LEVEL_ASSUMED_OPT_LEVEL, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_IRC_LEVEL_ASSUMED_OPT_LEVEL, message, "transition_state.irc.level_of_theory",
            {"action": "level_of_theory_assumed"}))


def _build_irc_result_payload(
    trajectories: list[dict[str, Any]],
    zero_energy_reference_hartree: float | None = None,
    ts_marker: Mapping[str, Any] | None = None,
    *,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "transition_state.irc_result",
) -> dict[str, Any] | None:
    """Build a TCKDB ``IRCResultPayload``-shaped dict from parsed trajectories.

    Each trajectory is a
    ``{"direction": str|None,
       "rich_points": [<rich-point dict>, ...] | None,
       "geom_points": [<xyz_dict>, ...] | None}``
    as produced by :meth:`TCKDBAdapter._parse_irc_trajectories`. When
    ``rich_points`` is populated, per-point ``electronic_energy_hartree``,
    ``reaction_coordinate``, ``max_gradient``, ``rms_gradient``, and (when
    Gaussian provides them) per-point ``direction`` flow through. When
    only ``geom_points`` is available the result reduces to the original
    geometry-only IRC payload behavior — energies/gradients are simply
    omitted, the schema makes them optional.

    ``point_index`` is allocated globally across both branches so the
    server-side uniqueness invariant holds. Direction labels are passed
    straight through — forward/reverse are ESS path labels, not
    reactant/product designators.

    ``zero_energy_reference_hartree`` is the TS reference energy at the
    IRC level of theory, resolved by
    :func:`_resolve_irc_zero_energy_reference`. When non-null, per-point
    ``relative_energy_kj_mol`` is computed against it and the result-
    level field is stamped on the payload. When null, both stay absent
    rather than fabricated.

    ``ts_marker`` is an optional dict
    ``{"xyz_text": <normalized xyz>, "electronic_energy_hartree": float|None}``
    representing the *seed* TS of the IRC path — the optimized saddle
    that ARC handed to the IRC job. When provided, a single synthesized
    point is appended after all trajectory points with ``is_ts=True``,
    ``reaction_coordinate=0.0`` (the TS is the origin by definition),
    ``relative_energy_kj_mol=0.0`` (when ``zero_energy_reference_hartree``
    is set), and the supplied geometry/energy. The result's
    ``ts_point_index`` is then set to this synthesized point's index.

    This is distinct from inferring the TS from per-point trajectory
    data (which ARC's parsers don't reliably do): the TS marker is
    ARC's own seed for the IRC, not a derivative of the parsed log.
    Ordering: the marker is *appended* rather than inserted between
    branches, so existing trajectory indices stay stable and ARC's
    ESS-emitted trajectory order is not reshuffled.

    ``direction`` is required by ``IRCResultPayload``. When any point's
    direction is unknown (no ``irc_log_directions`` entry, no per-point
    direction from the parser, no filename naming one), ARC's output does
    not state the run mode, so no result is built: ``None`` is returned and
    ``irc_direction_not_stated`` is appended to ``warnings`` under
    ``warning_field`` (the caller keeps the ``irc`` calculation itself).
    """
    if not trajectories:
        return None
    # Vendored xyz formatter + Hartree->kJ/mol constant (no ARC
    # dependency). Same constant ``path_search_result`` uses below, so
    # the two ``relative_energy_kj_mol`` fields agree bit-for-bit rather
    # than differing by the ~7e-8 relative amount a second, independently
    # rounded inline literal introduced (see PHASE_C_PLAN.md C-3).
    from tckdb_arc._vendor import E_h_kJmol, xyz_to_str

    points: list[dict[str, Any]] = []
    has_forward = False
    has_reverse = False
    # Trajectory points whose branch no source states.
    unstated_points = 0
    for traj in trajectories:
        traj_direction = traj.get("direction")
        rich_points = traj.get("rich_points") or []
        geom_points = traj.get("geom_points") or []
        # Rich points carry per-point direction (Gaussian's FORWARD/
        # REVERSE announcement). Geometry-only points inherit the
        # trajectory-level direction resolved upstream from the
        # scheduler list / filename heuristic.
        if rich_points:
            iter_records = (
                {
                    "xyz": rp.get("xyz"),
                    "geometry_xyz_text": rp.get("geometry_xyz_text"),
                    "direction": rp.get("direction") or traj_direction,
                    "electronic_energy_hartree": rp.get("electronic_energy_hartree"),
                    "reaction_coordinate": rp.get("reaction_coordinate"),
                    "max_gradient": rp.get("max_gradient"),
                    "rms_gradient": rp.get("rms_gradient"),
                }
                for rp in rich_points
            )
        else:
            iter_records = (
                {
                    "xyz": xyz,
                    "geometry_xyz_text": None,
                    "direction": traj_direction,
                    "electronic_energy_hartree": None,
                    "reaction_coordinate": None,
                    "max_gradient": None,
                    "rms_gradient": None,
                }
                for xyz in geom_points
            )

        for record in iter_records:
            direction = record["direction"]
            if direction == _IRC_DIRECTION_FORWARD:
                has_forward = True
            elif direction == _IRC_DIRECTION_REVERSE:
                has_reverse = True
            else:
                unstated_points += 1
            point: dict[str, Any] = {"point_index": len(points)}
            if direction in (_IRC_DIRECTION_FORWARD, _IRC_DIRECTION_REVERSE):
                point["direction"] = direction
            xyz_dict = record["xyz"]
            normalized = record.get("geometry_xyz_text")
            if normalized is None and xyz_dict is not None:
                try:
                    xyz_str = xyz_to_str(xyz_dict=xyz_dict)
                except Exception as exc:
                    logger.debug(
                        "TCKDB computed-reaction: IRC point xyz_to_str failed: %s",
                        exc,
                    )
                    xyz_str = None
                normalized = _normalize_xyz_text(xyz_str, None)
            if normalized:
                point["geometry"] = {"xyz_text": normalized}
            energy = record["electronic_energy_hartree"]
            if energy is not None:
                point["electronic_energy_hartree"] = float(energy)
                if zero_energy_reference_hartree is not None:
                    point["relative_energy_kj_mol"] = (
                        (float(energy) - zero_energy_reference_hartree)
                        * E_h_kJmol
                    )
            rc = record["reaction_coordinate"]
            if rc is not None:
                point["reaction_coordinate"] = float(rc)
            max_grad = record["max_gradient"]
            if max_grad is not None:
                point["max_gradient"] = float(max_grad)
            rms_grad = record["rms_gradient"]
            if rms_grad is not None:
                point["rms_gradient"] = float(rms_grad)
            points.append(point)
    if not points:
        return None
    if unstated_points:
        # A branch whose direction no source states. ARC runs every IRC job
        # in one direction (arc/job/adapters/common.py refuses an irc job
        # without 'forward' or 'reverse') and current output records it as
        # ``irc_log_directions``; without that (older runs pad the list
        # with None), a parsed per-point direction or a filename naming
        # one, which branch those points lie on is unknown. The run mode
        # (``direction``, required) and ``has_forward``/``has_reverse``
        # would both be guesses, so refuse the block rather than claim
        # ``both`` or file the unlabelled points under the other branch.
        message = (
            f"{unstated_points} of {len(points)} IRC point(s) from the "
            f"{len(trajectories)} IRC log(s) have no stated direction (no "
            f"irc_log_directions entry, no per-point direction, no forward/"
            f"reverse filename), and IRCResultPayload.direction is required, "
            f"so the IRC result is omitted; the irc calculation is still sent."
        )
        logger.warning("TCKDB %s: %s: %s", warning_field, _W_IRC_DIRECTION_NOT_STATED, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_IRC_DIRECTION_NOT_STATED, message, warning_field,
                {"action": "irc_result_omitted"}))
        return None
    if has_forward and has_reverse:
        overall = _IRC_DIRECTION_BOTH
    elif has_forward:
        overall = _IRC_DIRECTION_FORWARD
    else:
        overall = _IRC_DIRECTION_REVERSE

    # TS marker: synthesized point identifying the optimized saddle the
    # IRC was seeded from. ``direction`` is intentionally omitted (the
    # schema treats null direction as the TS marker); ``is_ts=True``
    # and a matching ``ts_point_index`` on the result let downstream
    # consumers locate the TS without scanning energies. Only emit
    # when we actually have a TS xyz to attach — the marker is
    # geometry-anchored, not a pure flag.
    ts_point_index: int | None = None
    if ts_marker and ts_marker.get("xyz_text"):
        ts_index = len(points)
        ts_point: dict[str, Any] = {
            "point_index": ts_index,
            "is_ts": True,
            "reaction_coordinate": 0.0,
            "geometry": {"xyz_text": str(ts_marker["xyz_text"])},
        }
        ts_energy = ts_marker.get("electronic_energy_hartree")
        if ts_energy is not None:
            try:
                ts_energy_f = float(ts_energy)
            except (TypeError, ValueError):
                ts_energy_f = None
            if ts_energy_f is not None:
                ts_point["electronic_energy_hartree"] = ts_energy_f
                if zero_energy_reference_hartree is not None:
                    ts_point["relative_energy_kj_mol"] = (
                        (ts_energy_f - zero_energy_reference_hartree)
                        * E_h_kJmol
                    )
        points.append(ts_point)
        ts_point_index = ts_index

    payload: dict[str, Any] = {
        "direction": overall,
        "has_forward": has_forward,
        "has_reverse": has_reverse,
        "point_count": len(points),
        "points": points,
    }
    if ts_point_index is not None:
        payload["ts_point_index"] = ts_point_index
    if zero_energy_reference_hartree is not None:
        payload["zero_energy_reference_hartree"] = float(
            zero_energy_reference_hartree
        )
    return payload


def _level_keys_match(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    """Conservative level-of-theory equality check.

    Compares the fields TCKDB treats as primary keys for
    ``LevelOfTheoryRef`` (method/basis/aux_basis/cabs_basis). A difference
    in software is intentionally not enough to declare inequality,
    since ARC frequently leaves it null on one side
    (the opt level) and populated on the other (the sp level) without
    that meaning the *energies* are at different levels.

    The one exception is ``dispersion`` and ``solvation_method``: a
    difference there (present on one side only, or different) *is* a
    mismatch, so an SMD single point is never taken as the reference of a
    gas-phase IRC (its only use is the IRC reference energy).
    """
    if a is None or b is None:
        return False
    for field in ("dispersion", "solvation_method"):
        av, bv = a.get(field), b.get(field)
        if (av is None) != (bv is None):
            return False
        if av is not None and str(av).strip().lower() != str(bv).strip().lower():
            return False
    for field in _TCKDB_LOT_REF_FIELDS:
        av = a.get(field)
        bv = b.get(field)
        if av is None and bv is None:
            continue
        if av is None or bv is None:
            return False
        if str(av).strip().lower() != str(bv).strip().lower():
            return False
    return True


def _parse_xtb_turbomole_energy_file(path: str | Path) -> float | None:
    """Parse the Turbomole-format ``energy`` file written by ``xtb --grad``.

    Provenance is xTB; ``Turbomole`` here names only the on-disk text
    format xTB emits when invoked with ``--grad`` (the same shape
    Turbomole's gradient driver writes — xTB borrows the layout to
    plug into Turbomole-compatible tooling like ``tm2orca.py``).

    File shape (one cycle per line, terminated by ``$end``)::

        $energy      SCF              SCFKIN            SCFPOT
             1     -28.12345678901   0.0   0.0
        $end

    The relevant value is on the line before ``$end``, second
    whitespace-separated field. Mirrors ``tm2orca.py``'s parse
    convention so the two stay in lockstep. Returns ``None`` for
    missing/malformed files (the caller treats that as
    "no energy known for this node").
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
        return float(lines[-2].strip().split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _parse_xtb_turbomole_gradient_file(
    path: str | Path,
) -> tuple[float | None, float | None]:
    """Parse the Turbomole-format ``gradient`` file written by ``xtb --grad``
    → ``(max_grad, rms_grad)`` in Hartree/Bohr. Returns ``(None, None)``
    for missing/malformed input.

    Provenance is xTB; ``Turbomole`` names only the on-disk text
    format xTB emits with ``--grad``.

    File shape::

        $grad   cycle = 1  SCF energy = ...
           x  y  z  symbol      ← N coord lines
           ...
           gx  gy  gz           ← N gradient-component lines (D-exponent)
           ...
        $end

    ``max_grad`` is the largest absolute gradient component; ``rms_grad``
    is ``sqrt(mean(g_i^2))`` over all ``3N`` components. Both are
    derived from the *gradient* (``dE/dx``), not the force — schema
    distinguishes the two and the source data is unambiguously the
    gradient (xTB writes its output in Turbomole's gradient layout
    when called with ``--grad``).
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
        natoms = int((len(lines) - 3) / 2)
        if natoms < 1:
            return None, None
        grad_lines = lines[2 + natoms : 2 + 2 * natoms]
        components: list[float] = []
        for line in grad_lines:
            parts = line.strip().replace("D", "E").split()
            if len(parts) < 3:
                continue
            components.extend(float(p) for p in parts[:3])
        if not components:
            return None, None
        import math
        max_g = max(abs(c) for c in components)
        rms_g = math.sqrt(sum(c * c for c in components) / len(components))
        return max_g, rms_g
    except (OSError, ValueError, IndexError):
        return None, None


# Matches xTB's final total-energy print. xTB emits it twice near the end
# of a ``--grad`` run, in two visually different frames but with the same
# value::
#
#     :: total energy              -9.441927748544 Eh    ::
#     ...
#     | TOTAL ENERGY               -9.441927748544 Eh   |
#
# We accept either (case-insensitive) and take the *last* match so a
# partially written/streamed file still yields the converged value. The
# unit is always Hartree (``Eh``); we assert the ``Eh`` tag is present so a
# stray ``total energy`` line in some other unit can't be misread.
_XTBOUT_TOTAL_ENERGY_RE = re.compile(
    r"total\s+energy\s+(-?\d+\.\d+(?:[eEdD][+-]?\d+)?)\s*Eh",
    re.IGNORECASE,
)


def _parse_xtb_xtbout_energy(path: str | Path) -> float | None:
    """Parse the total electronic energy (Hartree) from an xTB stdout
    (``.xtbout``) file.

    This is the load-bearing per-node energy source for xTB-GSM runs: the
    patched ``ograd`` wrapper copies ``<node>.xtbout`` into
    ``gsm_node_outputs/`` unconditionally, whereas the cleaner Turbomole
    ``.energy`` file depends on a ``tm2orca.py`` rename that frequently
    does not land, so ``.xtbout`` is often the only surviving record of
    the node's energy. The GSM stringfile itself carries no usable
    per-node energy (ARC's molecularGSM build writes ``0.000000`` for
    every node), so this is the sole absolute-energy source.

    Returns ``None`` for a missing/garbled file (no ``total energy ... Eh``
    line) — the caller treats that as "no energy known for this node".
    """
    try:
        # xTB banners carry non-ASCII glyphs (e.g. ``Eh/α``), so decode
        # explicitly as UTF-8 with replacement rather than relying on the
        # locale encoding — on a non-UTF-8 locale a bare ``open()`` would
        # raise ``UnicodeDecodeError`` and, uncaught, abort the whole
        # bundle build. ``ValueError`` (the ``UnicodeDecodeError`` base) is
        # also caught defensively, mirroring the sibling ``.energy`` parser.
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return None
    matches = _XTBOUT_TOTAL_ENERGY_RE.findall(text)
    if not matches:
        return None
    try:
        # xTB writes fixed-decimal Hartrees; the ``D`` FORTRAN exponent
        # marker is tolerated defensively for atypical builds.
        return float(matches[-1].replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


def _read_gsm_node_outputs(
    outputs_dir: str | Path,
) -> dict[int, dict[str, float]]:
    """Scan a ``gsm_node_outputs/`` directory for preserved per-node data
    written by ``xtb --grad`` (in Turbomole's on-disk format).

    The patched ``ograd`` wrapper writes files named like::

        gsm_node_outputs/
          0000.01.energy        ← xTB-generated, Turbomole-format (optional)
          0000.01.gradient      ← xTB-generated, Turbomole-format (optional)
          0000.01.xtbout        ← xTB stdout (always preserved)
          0000.02.xtbout
          ...

    where the ``0000`` prefix is GSM's iteration-round id and the
    ``.NN`` suffix is the per-round node label (1-based). This helper
    returns ``{label_int: {electronic_energy_hartree[, max_gradient,
    rms_gradient]}}`` for every label whose energy could be recovered.

    Two on-disk sources are consulted, in priority order:

    1. The Turbomole-format ``.energy`` / ``.gradient`` pair (cleaner,
       carries gradient metrics) — *preferred* when present.
    2. The xTB stdout ``.xtbout`` (energy only) — the *reliable
       fallback*. ``ograd`` copies ``.xtbout`` unconditionally, but the
       ``.energy``/``.gradient`` copies depend on a ``tm2orca.py`` rename
       that frequently does not land, so on most real runs ``.xtbout`` is
       the only surviving per-node record. Without this fallback the
       energies exist on disk but never reach the TCKDB payload.

    Nodes whose energy cannot be recovered from either source are omitted;
    the caller decides whether to leave the corresponding point's metadata
    null.

    The ESS provenance for the resulting calculation remains xTB /
    xTB-GSM; ``Turbomole`` names only the on-disk text shape the
    ``.energy``/``.gradient`` files use.
    """
    outputs_dir = Path(outputs_dir)
    if not outputs_dir.is_dir():
        return {}

    def _label_of(path: Path) -> int | None:
        # Filename is ``<round>.<NN>.<ext>`` — pull the trailing ``NN``
        # integer (the per-round node label). ``Path.stem`` drops only the
        # final extension, so ``0000.01.xtbout`` -> stem ``0000.01``.
        try:
            return int(path.stem.split(".")[-1])
        except (ValueError, IndexError):
            return None

    by_label: dict[int, dict[str, float]] = {}
    # Source 1 (preferred): Turbomole-format .energy/.gradient pair.
    for energy_file in sorted(outputs_dir.glob("*.energy")):
        label_int = _label_of(energy_file)
        if label_int is None:
            continue
        e_h = _parse_xtb_turbomole_energy_file(energy_file)
        if e_h is None:
            continue
        node_data: dict[str, float] = {"electronic_energy_hartree": e_h}
        gradient_file = energy_file.with_suffix(".gradient")
        if gradient_file.is_file():
            max_g, rms_g = _parse_xtb_turbomole_gradient_file(gradient_file)
            if max_g is not None:
                node_data["max_gradient"] = max_g
            if rms_g is not None:
                node_data["rms_gradient"] = rms_g
        by_label[label_int] = node_data
    # Source 2 (fallback): xTB stdout .xtbout — energy only, and only for
    # labels not already recovered from a cleaner .energy file above.
    for xtb_file in sorted(outputs_dir.glob("*.xtbout")):
        label_int = _label_of(xtb_file)
        if label_int is None or label_int in by_label:
            continue
        e_h = _parse_xtb_xtbout_energy(xtb_file)
        if e_h is None:
            continue
        by_label[label_int] = {"electronic_energy_hartree": e_h}
    return by_label


def _build_path_search_result_payload(
    *,
    method: str,
    log_path: str | Path | None,
    fallback_xyz_text: str | None,
    node_outputs_dir: str | Path | None = None,
    gsm_evidence: Mapping[str, Any] | None = None,
    evidence_unavailable: bool = False,
) -> dict[str, Any] | None:
    """Build a TCKDB ``PathSearchResultPayload``-shaped dict for the
    chosen TS-guess parent calc.

    The backend requires ``points`` with ``min_length=1`` (see
    ``backend/app/schemas/fragments/calculation.py``), so a bare
    ``{method: ...}`` payload won't validate.

    Strategy by method:

    * ``method == 'gsm'``: parse the GSM stringfile via
      :func:`arc.parser.parser.parse_trajectory` (handles the
      ``stringfile.xyz0000`` multi-frame XYZ format). Each frame
      becomes one point with ``geometry``. ``selected_ts_point_index``
      and the corresponding point's ``is_ts_guess`` flag track the
      middle-ish frame xtb_gsm picks as the TS guess (mirroring
      ``xTBGSMAdapter.process_run``).

    * ``method == 'neb'``: NEB log image extraction is a future parser
      lift; for now emit a single TS-guess point from
      ``fallback_xyz_text`` (the chosen guess's geometry, also the
      ts_opt's input XYZ). This is provenance the producer already
      committed to — the same geometry that became ``ts_opt`` input —
      not invented data. Marked with ``is_ts_guess=True``.

    * Other methods or unparseable logs: fall back to the single-point
      shape, or return ``None`` if no fallback geometry is available.

    Returns ``None`` (caller skips the calc) when even the fallback
    can't be built — the conservative gate then leaves ``ts_opt``
    edge-less rather than emitting a path_search calc with no points.
    """
    # Late-imported to keep the adapter's import surface lean when
    # path-search emission isn't exercised.
    from tckdb_arc._vendor import E_h_kJmol, xyz_to_str
    # ARC-only trajectory parsers + Kabsch alignment via the optional boundary.
    # These import cleanly and raise OptionalArcUnavailable only at call time;
    # each call below degrades (GSM sub-payload omitted) when ARC is absent.
    from tckdb_arc._arc_optional import (
        kabsch,
        parse_gsm_stringfile_energies,
        parse_trajectory,
    )

    # Archived ograd ids identify invocations, not final stringfile frames.
    # Only portable evidence supplies verified geometry matches. Raw fallback
    # keeps geometries and stringfile relative energies, never index-attached
    # absolute energies or gradients from node_outputs_dir.
    if gsm_evidence is None and not evidence_unavailable and node_outputs_dir is not None and method == "gsm":
        logger.warning(
            "TCKDB path_search: archived GSM energies/gradients omitted without geometry-matched parser evidence"
        )

    points: list[dict[str, Any]] = []
    selected_index: int | None = None
    evidence_relative_kcal: dict[int, float] = {}

    if method == "gsm" and gsm_evidence is not None:
        selected_index = int(gsm_evidence["selected_source_point_index"])
        for source_point in gsm_evidence["points"]:
            index = int(source_point["source_point_index"])
            point: dict[str, Any] = {
                "point_index": index,
                "geometry": {"xyz_text": source_point["geometry_xyz_text"]},
            }
            if "path_coordinate_angstrom" in source_point:
                point["path_coordinate"] = source_point["path_coordinate_angstrom"]
            if "cumulative_com_superposed_displacement_angstrom" in source_point:
                point["path_coordinate"] = source_point["cumulative_com_superposed_displacement_angstrom"]
            if index == selected_index:
                point["is_ts_guess"] = True
            for evidence_key, payload_key in (
                ("electronic_energy_hartree", "electronic_energy_hartree"),
                ("max_gradient_hartree_per_bohr", "max_gradient"),
                ("rms_gradient_hartree_per_bohr", "rms_gradient"),
            ):
                if evidence_key in source_point:
                    point[payload_key] = source_point[evidence_key]
            if "stringfile_relative_energy_kcal_mol" in source_point:
                evidence_relative_kcal[index] = source_point["stringfile_relative_energy_kcal_mol"]
            points.append(point)

    if method == "gsm" and gsm_evidence is None and not evidence_unavailable and log_path is not None:
        path_str = str(log_path)
        try:
            traj = parse_trajectory(path_str)
        except Exception as exc:
            logger.warning(
                "TCKDB path_search: GSM trajectory parse failed for %s: %s",
                path_str, exc,
            )
            traj = None
        if traj:
            # xtb_gsm.process_run picks the TS guess at the middle
            # frame: ``int((len(traj) - 1) / 2) + 1``. Mirror that
            # here so the selected point matches the geometry that
            # actually became ``ts_opt``'s input.
            selected_index = int((len(traj) - 1) / 2) + 1
            # path_coordinate holds the physical cumulative arc length in
            # Angstrom along the string — the analog of ORCA NEB's "Dist.(Ang.)"
            # the backend's ``path_coordinate`` column stores (a monotonically
            # increasing distance, not a normalized 0-1 fraction). Each node is
            # Kabsch-aligned onto its predecessor before the displacement is
            # summed: GSM does NOT keep every node in a common frame (the
            # reactant endpoint is frequently translated/rotated relative to the
            # interior nodes), so the raw inter-node displacement would be
            # inflated by that pure frame shift. ``kabsch`` returns the aligned
            # root-sum-square displacement; node 0 = 0.0. If alignment fails for
            # any pair, path_coordinate is left null on every point (an honest
            # null beats a mislabeled proxy that would clash with ORCA's Angstrom
            # rows).
            arc_lengths: list[float] | None = None
            if len(traj) >= 2:
                try:
                    arc_lengths = [0.0]
                    for idx in range(1, len(traj)):
                        arc_lengths.append(float(
                            arc_lengths[-1] + kabsch(traj[idx], traj[idx - 1])
                        ))
                except (ValueError, KeyError, TypeError, IndexError):
                    arc_lengths = None
            for i, frame in enumerate(traj):
                try:
                    atom_only = xyz_to_str(frame)
                except Exception:
                    continue
                # TCKDB's ``GeometryPayload.xyz_text`` requires the
                # canonical XYZ-file shape (atom-count header + comment
                # + atom lines). ``xyz_to_str`` emits atom-lines only,
                # so route through ``_normalize_xyz_text`` to add the
                # header — same translation the species-side calcs use.
                xyz_text = _normalize_xyz_text(
                    atom_only, label=f"gsm_point_{i}",
                )
                if not xyz_text:
                    continue
                point: dict[str, Any] = {
                    "point_index": i,
                    "geometry": {"xyz_text": xyz_text},
                }
                if arc_lengths is not None:
                    point["path_coordinate"] = arc_lengths[i]
                if i == selected_index:
                    point["is_ts_guess"] = True
                points.append(point)

    if not points and evidence_unavailable:
        return None
    if not points:
        # Single-point fallback: emit the chosen guess's geometry as
        # one TS-guess point. Honest about scope (lossy: we know one
        # point, not the full path) without faking other points.
        normalized_fallback = _normalize_xyz_text(
            fallback_xyz_text, label="ts_guess_point",
        )
        if not normalized_fallback:
            return None
        points = [{
            "point_index": 0,
            "geometry": {"xyz_text": normalized_fallback},
            "is_ts_guess": True,
        }]
        selected_index = 0

    # Relative-energy convention. Reference every point that carries an
    # absolute electronic energy to the minimum absolute energy present,
    # and leave points without one with no ``relative_energy_kj_mol`` (an
    # explicit gap — never a fabricated value; a null is not a "misleading
    # dip" the way a made-up number would be).
    #
    # Only geometrically attached evidence points have absolute energies.
    # Unmatched points retain missing values; the minimum known energy is a
    # numerical reference, not an inferred reactant assignment.
    energies = [
        p.get("electronic_energy_hartree") for p in points
    ]
    known = [e for e in energies if e is not None]
    zero_e_h: float | None = None
    if known:
        zero_e_h = min(known)
        for p in points:
            e_h = p.get("electronic_energy_hartree")
            if e_h is not None:
                p["relative_energy_kj_mol"] = (e_h - zero_e_h) * E_h_kJmol

    # Fallback relative-energy source: the GSM stringfile's per-node
    # comment column (relative energies in kcal/mol, first node = 0).
    # Only used when absolute per-node Hartrees weren't preserved (no
    # ``zero_e_h`` above) — the stringfile carries *relative* energies
    # only, so ``electronic_energy_hartree`` and
    # ``zero_energy_reference_hartree`` legitimately stay null here.
    #
    # Guard: ARC's current molecularGSM build writes ``0.000000`` for
    # every comment line (verified benchmark-wide), so a column that
    # spans < ``_GSM_STRINGFILE_ENERGY_EPS`` is the "no energy emitted"
    # sentinel — we leave relative energies null rather than upload a
    # fabricated flat-zero profile. When a build *does* emit energies,
    # this populates ``relative_energy_kj_mol`` for every node. The peak
    # node is not marked ``is_climbing_image``: the contract defines that
    # flag for NEB-CI images (string methods ignore it), and the highest
    # node of a string is not evidence that a climbing image was run.
    if zero_e_h is None and not any(e is not None for e in energies) and method == "gsm":
        if gsm_evidence is not None:
            rel_kcal = [evidence_relative_kcal.get(p["point_index"]) for p in points]
            if not rel_kcal or any(value is None for value in rel_kcal):
                rel_kcal = None
        elif log_path is not None:
            try:
                rel_kcal = parse_gsm_stringfile_energies(str(log_path))
            except Exception as exc:  # OptionalArcUnavailable (no [arc]) or parse error
                logger.debug("TCKDB path_search: GSM string-file energies unavailable (%s)", exc)
                rel_kcal = None
        else:
            rel_kcal = None
        if rel_kcal is not None:
            point_indices = [p["point_index"] for p in points]
            # Evidence values were collected in payload point order above;
            # source indices need not be contiguous offsets into that list.
            if point_indices and (gsm_evidence is not None or all(
                0 <= ix < len(rel_kcal) for ix in point_indices
            )):
                vals = rel_kcal if gsm_evidence is not None else [rel_kcal[ix] for ix in point_indices]
                if max(vals) - min(vals) > _GSM_STRINGFILE_ENERGY_EPS:
                    base_kcal = min(vals)
                    for p, v in zip(points, vals):
                        p["relative_energy_kj_mol"] = (
                            (v - base_kcal) * _KCAL_MOL_TO_KJ_MOL
                        )

    # ``converged`` (optional) is omitted: ARC records no convergence
    # verdict for a path search. ``TSGuess.success`` and ``log_path`` mean
    # only that the output file exists (arc/job/adapters/ts/xtb_gsm.py and
    # orca_neb.py ``process_run``), not that the string or band converged,
    # and the contract asks a producer to leave out what its source does
    # not state.
    payload: dict[str, Any] = {
        "method": method,
        "n_points": len(points),
        "points": points,
    }
    if zero_e_h is not None:
        payload["zero_energy_reference_hartree"] = zero_e_h
    # Static method properties (is_double_ended, source_endpoint_count)
    # — intrinsic to the algorithm, not the run. See
    # ``_PATH_SEARCH_METHOD_PROPERTIES``.
    payload.update(_PATH_SEARCH_METHOD_PROPERTIES.get(method, {}))
    if selected_index is not None and any(
        p["point_index"] == selected_index for p in points
    ):
        payload["selected_ts_point_index"] = selected_index
    return payload


def _resolve_irc_zero_energy_reference(
    *,
    output_doc: Mapping[str, Any],
    ts_record: Mapping[str, Any],
    irc_level: Mapping[str, Any] | None = None,
) -> float | None:
    """Pick the TS reference electronic energy for an IRC path.

    The reference must live at the IRC calculation's level, or the relative
    energies it produces would mix two levels of theory. That level is
    ``irc_level`` when known exactly (restart.yml), else the assumed
    ``opt_level`` (see the IRC calculation construction site).

    Resolution order, conservative by design:

    1. ``ts_sp_result.electronic_energy_hartree`` *only if* the project
       SP level matches the IRC level (LoT keys equal).
    2. ``ts_record['opt_final_energy_hartree']`` -- the TS opt's converged
       SCF, at the opt level -- *only if* that matches the IRC level (always,
       when the IRC level is the assumed opt level).
    3. ``None`` -- never fabricate a reference. Downstream consumers
       interpret a null ``zero_energy_reference_hartree`` as "relative
       energies unavailable" and skip the ``relative_energy_kj_mol``
       per-point field accordingly.
    """
    opt_level = _resolve_level(output_doc, "opt", ts_record)
    ref_level = irc_level if irc_level is not None else opt_level
    sp_level = output_doc.get("sp_level")
    sp_level = (
        _resolve_level(output_doc, "sp", ts_record)
        if isinstance(sp_level, Mapping) or arc13.has_levels(ts_record) else None
    )
    sp_energy = ts_record.get("sp_energy_hartree")
    if sp_energy is None:
        sp_energy = ts_record.get("electronic_energy_hartree")
    if sp_energy is not None and _level_keys_match(ref_level, sp_level):
        try:
            return float(sp_energy)
        except (TypeError, ValueError):
            pass
    opt_energy = ts_record.get("opt_final_energy_hartree")
    if opt_energy is not None and (
            irc_level is None or _level_keys_match(ref_level, opt_level)):
        try:
            return float(opt_energy)
        except (TypeError, ValueError):
            pass
    return None


def _normalize_xyz_text(xyz: str | None, label: str | None) -> str | None:
    """Convert an ARC atom-only xyz string into TCKDB's standard XYZ format.

    Input shape (what ``xyz_to_str`` emits):
        ``"C 0.0 0.0 0.0\\nH 1.0 0.0 0.0"``
    Output shape (what TCKDB ``GeometryPayload.xyz_text`` expects):
        ``"<n_atoms>\\n<comment>\\n<atom lines>"``

    If the input already has a valid integer atom-count header, it's
    returned untouched. Returns ``None`` for null/empty input — the
    format-translation boundary between ARC's internal convention and
    the TCKDB schema, with no requirement that input be present.
    """
    if not xyz:
        return None
    text = str(xyz).strip()
    if not text:
        return None
    lines = text.splitlines()
    try:
        int(lines[0].strip())
        return text
    except (ValueError, IndexError):
        pass
    return f"{len(lines)}\n{label or ''}\n{text}"


def _require_xyz_text(record: Mapping[str, Any]) -> str:
    """Pull and normalize the species record's reference xyz, raising if absent.

    Wraps :func:`_normalize_xyz_text` for the conformer-level geometry
    case where missing xyz is a fatal error (the bundle requires a
    ``ConformerInBundle.geometry``). For optional per-calc input
    geometries, call ``_normalize_xyz_text`` directly so a missing xyz
    just yields ``None`` and the caller omits the field.
    """
    xyz = record.get("xyz")
    if not xyz:
        raise ValueError(
            f"output.yml record for label={record.get('label')!r} has no xyz; "
            "cannot build geometry payload."
        )
    text = _normalize_xyz_text(xyz, record.get("label"))
    if text is None:
        raise ValueError(
            f"output.yml record for label={record.get('label')!r} has empty xyz."
        )
    _assert_isotopes_reconcile(record)
    return text


_W_GEOMETRY_ISOTOPES_NOT_STATED = ArcWarning.GEOMETRY_ISOTOPES_NOT_STATED.value


def _isotopic_substitutions(record: Mapping[str, Any]) -> dict[int, int] | None:
    """The record's final geometry's isotope substitutions as ARC states them (1.3), else ``None``."""
    if "xyz_isotopes" not in record:
        return None
    text = _normalize_xyz_text(record.get("xyz"), record.get("label"))
    return arc13.geometry_isotope_substitutions(text, record.get("xyz_isotopes"))


def _assert_isotopes_reconcile(record: Mapping[str, Any]) -> None:
    """Refuse a species whose stated geometry isotopes contradict its SMILES (output.yml 1.3).

    TCKDB's ``species_geometry_isotope_mismatch`` refuses a geometry whose isotope
    substitutions differ from those the species entry's SMILES declares (``[2H]``).
    ARC states the geometry's isotope list (``xyz_isotopes``); the adapter sends it
    as stated, and it never rewrites the SMILES to fit nor drops a stated
    substitution to get past the check. When the two disagree, or the SMILES
    declares a substitution the geometry's isotopes cannot confirm, no request is
    built. The rule is :func:`tckdb_core.isotopes.isotope_reconciliation_error`;
    reading ``xyz_isotopes`` and ``smiles`` from the record is the ARC part.
    """
    if "xyz_isotopes" not in record:
        return
    text = _normalize_xyz_text(record.get("xyz"), record.get("label"))
    error = _isotopes.isotope_reconciliation_error(
        record.get("label"),
        declared=arc13.smiles_isotope_multiset(record.get("smiles")),
        substitutions=arc13.geometry_isotope_substitutions(text, record.get("xyz_isotopes")),
        xyz_text=text,
        producer_name="ARC",
        isotopes_field="xyz_isotopes",
    )
    if error is not None:
        raise error


def _geometry_payload(
    record: Mapping[str, Any], xyz_text: str, isotopes: Any, *, key: str | None = None,
) -> dict[str, Any] | None:
    """``{xyz_text[, key][, isotopes]}`` for a geometry of ``record`` whose ARC isotope list is ``isotopes``.

    TCKDB's ``isotopes`` maps a 1-based atom index to a mass number for the
    substituted atoms only (an unlisted atom is at its most abundant isotope), so
    an all-standard list sends none. Nothing is sent for a record without
    output.yml 1.3's ``xyz_isotopes``. For an isotopically substituted record the
    geometry must state the same substitutions as the record's own geometry (the
    species identity carries them); one that states none, or others, is
    ``None``: the caller leaves that geometry out rather than deposit it as an
    unsubstituted one. The rule is :func:`tckdb_core.isotopes.geometry_payload`.
    """
    record_subs = _isotopic_substitutions(record) if "xyz_isotopes" in record else None
    return _isotopes.geometry_payload(
        xyz_text, isotopes,
        isotopes_stated="xyz_isotopes" in record,
        record_xyz_text=_normalize_xyz_text(record.get("xyz"), record.get("label")),
        record_substitutions=record_subs,
        key=key,
    )


def _conformer_isotopes(record: Mapping[str, Any], index: int) -> Any:
    lists = record.get("conformers_isotopes")
    return lists[index] if isinstance(lists, list) and index < len(lists) else None


def _warn_isotopes_not_stated(
    warnings: list[dict[str, Any]] | None, *, label: Any, what: str,
) -> None:
    message = (
        f"{what} of the isotopically substituted {label!r} were left out: ARC states no "
        "isotope list that matches the species' own geometry for them, and TCKDB would "
        "read an unlabelled geometry as the unsubstituted species."
    )
    logger.warning("TCKDB %s: %s", _W_GEOMETRY_ISOTOPES_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_GEOMETRY_ISOTOPES_NOT_STATED, message, "geometry.isotopes",
            {"action": "geometry_omitted"}))


def _artifact_batch_idempotency_prefix(project_label: Any, species_label: Any) -> str:
    """ARC's artifact-batch key prefix, ``arc:<project>:<species>:artifact`` (see tckdb_core)."""
    return _core_artifact_batch_idempotency_prefix(
        project_label, species_label, namespace=TCKDBAdapter.IDEMPOTENCY_NAMESPACE,
    )


def _calculation_ref_not_returned_warning(calculation_id: int | None) -> dict[str, Any]:
    """Self-check finding: an artifact target was named by integer id, not ``calc_`` ref."""
    return _core_calculation_ref_not_returned_warning(
        calculation_id, producer=TCKDBAdapter.PRODUCER_TAG,
    )


__all__ = [
    "ARTIFACTS_ENDPOINT_TEMPLATE",
    "ArtifactUploadOutcome",
    "COMPUTED_REACTION_ENDPOINT",
    "COMPUTED_REACTION_KIND",
    "COMPUTED_SPECIES_ENDPOINT",
    "COMPUTED_SPECIES_KIND",
    "CONFORMER_UPLOAD_ENDPOINT",
    "PAYLOAD_KIND",
    "TCKDBAdapter",
    "TRANSITION_STATE_ENDPOINT",
    "TRANSITION_STATE_KIND",
    "UploadOutcome",
    "arc_to_tckdb_a_units",
    "arc_to_tckdb_ea_units",
]
