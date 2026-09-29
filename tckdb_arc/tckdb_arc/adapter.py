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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tckdb_client import TCKDBClient
from tckdb_client.errors import TCKDBError
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.fragments.refs import (
    W_SOFTWARE_RELEASE_VERSION_IS_COMPOSITE,
    SoftwareReleaseRef,
)
from tckdb_schemas.utils import normalize_tunneling_model

from tckdb_arc._logging import get_logger
from tckdb_arc.adaptive import (
    RUN_LEVEL,
    UNDETERMINABLE,
    AdaptiveLevels,
    RestartInfo,
    detect_adaptive_levels,
    read_restart_info,
)
from tckdb_arc.config import (
    IMPLEMENTED_ARTIFACT_KINDS,
    TCKDBConfig,
    UPLOAD_MODE_COMPUTED_REACTION,
    UPLOAD_MODE_COMPUTED_SPECIES,
)

# Upload modes whose bundle payload already carries input/output_log
# artifacts inline under each calculation. Standalone artifact sidecars
# in these modes would (a) duplicate uploads and (b) lock in
# server-assigned calculation IDs that go stale on DB resets — see
# `submit_artifacts_for_calculation` for the gating logic.
_BUNDLE_MODES_WITH_INLINE_ARTIFACTS = frozenset({
    UPLOAD_MODE_COMPUTED_SPECIES,
    UPLOAD_MODE_COMPUTED_REACTION,
})
from tckdb_arc.idempotency import (
    ArtifactIdempotencyInputs,
    IdempotencyInputs,
    build_artifact_idempotency_key,
    build_idempotency_key,
)
from tckdb_arc.constraints import serialize_constraints
from tckdb_arc.evidence import EvidenceStore
from tckdb_arc.payload_writer import (
    ArtifactSidecarMetadata,
    PayloadWriter,
    SidecarMetadata,
    WrittenArtifact,
    WrittenPayload,
    _utcnow_iso,
)


def _serialize_calc_constraints(source) -> list[dict]:
    """Translate a parser-shaped constraint list into the TCKDB payload shape.

    ``source`` is whatever ``arc/output.py`` attached to the record (a
    list of parser dicts) or what the caller passed explicitly. Empty /
    None / unrecognised input produces ``[]``. Wraps
    ``tckdb_arc.constraints.serialize_constraints`` so per-calc parse
    failures never bubble up into payload generation.
    """
    if not source:
        return []
    if not isinstance(source, (list, tuple)):
        logger.warning("TCKDB constraints: expected list, got %s; emitting [].",
                       type(source).__name__)
        return []
    try:
        return serialize_constraints(source)
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
        scan_result = _neutral_scan_result_to_tckdb(result)
        if scan_result is None:
            continue
        translated.append({
            "key": key,
            "type": _CALC_KEY_SCAN,
            "scan_result": scan_result,
            "constraints": scan.get("constraints") or [],
        })
    return translated


def _neutral_scan_result_to_tckdb(result: Mapping[str, Any]) -> dict[str, Any] | None:
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
            point["geometry"] = {"xyz_text": geometry}
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


def _correction_records_from_record(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate neutral ARC correction facts to the legacy adapter boundary."""
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
            if atom_params:
                scheme["atom_params"] = atom_params
                if reference.get("applied_as") == "subtracted":
                    # Sign convention ARC records; the atom_params themselves
                    # are bare atomic energies.
                    scheme["note"] = (
                        "Atom energies are subtracted from the molecular "
                        "electronic energy (Arkane)."
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
        out.append({
            "application_role": application_role,
            "value": float(total["value"]),
            "value_unit": unit,
            "scheme": scheme,
            "components": correction.get("components") or [],
            # Arkane's database key the table came from; read by
            # ``_build_applied_energy_corrections`` for ``scheme.software``,
            # never sent itself.
            "matched_arkane_key": correction.get("matched_arkane_key"),
        })
    return out


logger = get_logger()

CONFORMER_UPLOAD_ENDPOINT = "/uploads/conformers"
PAYLOAD_KIND = "conformer_calculation"
ARTIFACTS_ENDPOINT_TEMPLATE = "/calculations/{calculation_id}/artifacts"

# Computed-species bundle endpoint. One self-contained payload that
# carries species_entry + conformers + calcs + artifacts + thermo, with
# all cross-references expressed as local string keys (no DB ids).
COMPUTED_SPECIES_ENDPOINT = "/uploads/computed-species"
COMPUTED_SPECIES_KIND = "computed_species"

# Computed-reaction bundle endpoint. Like computed-species but adds
# reactant/product species blocks, an inline transition_state, and a
# modified-Arrhenius kinetics block with producer-declared
# source_calculations.
COMPUTED_REACTION_ENDPOINT = "/uploads/computed-reaction"
COMPUTED_REACTION_KIND = "computed_reaction"

# Standalone transition-state endpoint. One payload per converged TS,
# carrying the embedded reaction (reactants/products by identity, family,
# reversibility), the saddle-point geometry, a required primary opt, and
# optional freq/sp/irc/path_search calculations. Unlike the computed-
# reaction bundle, no species minima or kinetics travel with it — the TS
# is the record. See ``submit_computed_ts_from_output``.
TRANSITION_STATE_ENDPOINT = "/uploads/transition-states"
TRANSITION_STATE_KIND = "transition_state"

# Bundle-only calculation keys that ``_build_ts_block`` layers onto each
# calc dict for the computed-reaction wire shape (local cross-reference
# identity + dependency edges). The standalone TS endpoint consumes a
# plain ``CalculationWithResultsPayload`` (``extra="forbid"``) for
# primary_opt / additional_calculations, so these must be stripped before
# upload. Dependency wiring (e.g. path_search -> primary_opt) is
# re-derived server-side for the standalone endpoint.
_TS_STANDALONE_STRIP_CALC_KEYS = ("key", "depends_on", "geometry_key", "artifacts")

# Readiness-probe (/readyz) retry policy. During a long ARC run the TCKDB
# server can be briefly not-ready (restart, rolling deploy, momentary
# load). Rather than fail the in-run upload on the first blip, the
# preflight probe retries with exponential backoff before giving up.
#
# PREFLIGHT_MAX_ATTEMPTS counts the initial probe plus retries (5 → 1
# initial + 4 retries). Delays follow PREFLIGHT_BASE_DELAY_SECONDS * 2**i
# (1s, 2s, 4s, 8s) capped at PREFLIGHT_MAX_DELAY_SECONDS, so the worst
# case waits ~1+2+4+8 = 15s across the four gaps — well under a minute so
# a genuinely-down server doesn't stall the whole run. Tunable here.
PREFLIGHT_MAX_ATTEMPTS = 5
PREFLIGHT_BASE_DELAY_SECONDS = 1.0
PREFLIGHT_MAX_DELAY_SECONDS = 8.0

# Local calculation-key namespace within a computed-species bundle.
# These keys are referenced from `depends_on.parent_calculation_key` and
# `thermo.source_calculations[].calculation_key`. They have no relation
# to TCKDB-assigned calculation_ids — the bundle endpoint mints those
# server-side and returns them in the response.
_CALC_KEY_OPT = "opt"
_CALC_KEY_OPT_COARSE = "opt_coarse"
_CALC_KEY_FREQ = "freq"
_CALC_KEY_SP = "sp"

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


# Backend ``KIND_ALLOWED_EXTENSIONS`` for ``output_log`` is
# ``{.log, .out, .orca}`` (see TCKDB_v2 fragments/artifact.py).
# Non-traditional log artifacts (e.g. the GSM ``stringfile.xyz0000``)
# need their declared filename adapted to satisfy the allowlist while
# preserving the original basename for human inspection. The backend
# treats ``filename`` as provenance metadata only — storage is content-
# addressed and the filename does not influence the URI.
_OUTPUT_LOG_ALLOWED_EXTS: frozenset[str] = frozenset({".log", ".out", ".orca"})


def _coerce_artifact_filename(name: str, kind: str) -> str:
    """Adapt an artifact ``filename`` to satisfy the backend's per-kind
    extension allowlist. Currently only ``output_log`` is coerced —
    other kinds either match upstream (``input``, ``checkpoint``) or
    aren't emitted by ARC.

    For ``output_log`` files whose extension is already permitted
    (``.log`` / ``.out`` / ``.orca``), the name is returned verbatim.
    For non-conforming names (e.g. GSM ``stringfile.xyz0000``), a
    ``.log`` suffix is appended so the original basename is preserved
    as the prefix (``stringfile.xyz0000.log``). The coercion is a
    no-op for any other kind.
    """
    if kind != "output_log":
        return name
    ext = os.path.splitext(name)[1].lower()
    if ext in _OUTPUT_LOG_ALLOWED_EXTS:
        return name
    return f"{name}.log"


# Bundle-local calc role for rotor scans. Per-rotor keys are minted as
# ``f"{_CALC_KEY_SCAN}_rotor_{i}"`` by ``arc/output.py``; the adapter
# accepts whatever key the species record provides without re-minting,
# so the source-of-truth for naming stays on the producer side.
_CALC_KEY_SCAN = "scan"

# ARC kinetics-unit string → TCKDB enum string. Source of truth for the
# enum values is TCKDB's ``ArrheniusAUnits`` and ``ActivationEnergyUnits``
# (backend/app/db/models/common.py). The ARC side normalizes whitespace
# and quotes before lookup so cosmetic variations don't miss.
_ARC_TO_TCKDB_A_UNITS: dict[str, str] = {
    "s^-1": "per_s",
    "1/s": "per_s",
    "cm^3/(mol*s)": "cm3_mol_s",
    "cm^3/(molecule*s)": "cm3_molecule_s",
    "m^3/(mol*s)": "m3_mol_s",
    "cm^6/(mol^2*s)": "cm6_mol2_s",
    "cm^6/(molecule^2*s)": "cm6_molecule2_s",
    "m^6/(mol^2*s)": "m6_mol2_s",
}

_ARC_TO_TCKDB_EA_UNITS: dict[str, str] = {
    "j/mol": "j_mol",
    "kj/mol": "kj_mol",
    "cal/mol": "cal_mol",
    "kcal/mol": "kcal_mol",
}


def _normalize_unit_key(text: str | None) -> str | None:
    """Lowercase + strip whitespace so ``" kJ/mol "`` matches ``"kj/mol"``.

    The ARC unit strings come from RMG and aren't perfectly consistent in
    case/whitespace; the TCKDB enums are. This is the boundary helper.
    """
    if text is None:
        return None
    s = str(text).strip().lower()
    return s or None


def arc_to_tckdb_a_units(arc_units: str | None) -> str | None:
    """Map an ARC ``A_units`` string to a TCKDB ``ArrheniusAUnits`` enum value.

    Returns ``None`` for null/empty input or unrecognized strings — the
    caller decides whether to omit the field or raise. Unrecognized
    units log a debug line so a producer with a typo can spot it
    without spamming warnings on every reaction.
    """
    key = _normalize_unit_key(arc_units)
    if key is None:
        return None
    enum_value = _ARC_TO_TCKDB_A_UNITS.get(key)
    if enum_value is None:
        logger.debug(
            "TCKDB kinetics: unrecognized A_units %r; field will be omitted.",
            arc_units,
        )
    return enum_value


def arc_to_tckdb_ea_units(arc_units: str | None) -> str | None:
    """Map an ARC ``Ea_units`` (or ``dEa_units``) string to a TCKDB ``ActivationEnergyUnits`` enum value.

    Same null-or-unknown → ``None`` policy as :func:`arc_to_tckdb_a_units`.
    """
    key = _normalize_unit_key(arc_units)
    if key is None:
        return None
    enum_value = _ARC_TO_TCKDB_EA_UNITS.get(key)
    if enum_value is None:
        logger.debug(
            "TCKDB kinetics: unrecognized Ea_units %r; field will be omitted.",
            arc_units,
        )
    return enum_value


@dataclass(frozen=True)
class UploadOutcome:
    """Result of one adapter invocation; mirrors the sidecar status.

    ``primary_calculation`` and ``additional_calculations`` are populated
    on successful conformer uploads (status == ``uploaded``) and carry
    the server's :class:`CalculationUploadRef` shape — i.e. dicts with
    ``calculation_id``, ``type``, and (for additional calcs) ``request_index``.
    They are ``None`` / empty otherwise.
    """

    status: str  # pending | uploaded | failed | skipped
    payload_path: Path
    sidecar_path: Path
    idempotency_key: str
    error: str | None = None
    response: Any = None
    primary_calculation: dict[str, Any] | None = None
    additional_calculations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ArtifactUploadOutcome:
    """Result of one artifact upload attempt."""

    status: str  # uploaded | failed | skipped
    sidecar_path: Path | None
    idempotency_key: str | None
    calculation_id: int
    kind: str
    error: str | None = None
    response: Any = None
    skip_reason: str | None = None


@dataclass(frozen=True)
class _PreparedArtifactUpload:
    """ARC-local artifact upload plan item plus its sidecar handle."""

    written: WrittenArtifact
    calculation_key: str
    calculation_id: int
    path: Path
    kind: str
    label: str | None
    sha256: str
    bytes: int
    filename: str


class TCKDBReadinessError(RuntimeError):
    """Raised when the TCKDB readyz preflight fails before upload."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_json: Any = None,
        response_text: str | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_json = response_json
        self.response_text = response_text
        self.headers = dict(headers) if headers is not None else None


class TCKDBAdapter:
    """Build, write, and optionally upload one conformer/calculation payload.

    The adapter holds the config and a payload writer. ``client_factory``
    is overridable so tests can inject a mocked ``TCKDBClient`` without
    touching the network.
    """

    def __init__(
        self,
        config: TCKDBConfig,
        *,
        project_directory: str | Path | None = None,
        client_factory=None,
        input_dict: Mapping[str, Any] | None = None,
    ):
        self._config = config
        # ARC's parsed ``input.yml`` when the caller has it (the CLI), used with
        # the project's ``restart.yml`` / ``input.yml`` to detect
        # ``adaptive_levels`` runs (see ``tckdb_arc.adaptive``).
        self._input_dict = input_dict
        self._adaptive_levels_checked = False
        self._adaptive_levels: AdaptiveLevels | None = None
        self._restart_info: RestartInfo | None = None
        self._project_directory = (
            Path(project_directory) if project_directory is not None else None
        )
        # Resolve payload_dir against the project directory if it's relative,
        # so payloads land under the active ARC project rather than CWD.
        payload_root = Path(config.payload_dir)
        if not payload_root.is_absolute() and project_directory is not None:
            payload_root = Path(project_directory) / payload_root
        self._writer = PayloadWriter(payload_root)
        self._client_factory = client_factory
        self._preflight_checked = False
        self._preflight_metadata: dict[str, Any] | None = None
        self._preflight_error: TCKDBReadinessError | None = None
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
        if detection is None and restart is None:
            return output_doc
        marked = dict(output_doc)
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

        build_warnings: list[dict[str, Any]] = []
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        payload = self._build_payload(
            output_doc=output_doc,
            species_record=species_record,
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
        calculation_id: int,
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

        ``calculation_type`` is recorded in the sidecar but does not feed
        the URL — the endpoint takes the calc id directly.
        """
        outcomes = self.submit_artifact_batch_for_calculation(
            output_doc=output_doc,
            species_record=species_record,
            calculation_id=calculation_id,
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
        calculation_id: int,
        calculation_type: str,
        artifacts: list[tuple[str, str | Path]],
    ) -> list[ArtifactUploadOutcome] | None:
        """Upload artifacts for one calculation with client-side batch grouping."""
        if not self._config.enabled:
            return None

        species_label = species_record.get("label") or "unlabeled"
        artifact_cfg = self._config.artifacts
        outcomes: list[ArtifactUploadOutcome] = []
        prepared: list[_PreparedArtifactUpload] = []

        for kind, file_path in artifacts:
            prepared_item = self._prepare_artifact_upload(
                output_doc=output_doc,
                species_label=species_label,
                calculation_id=calculation_id,
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
            calculation_key=f"calc{calculation_id}-{batch_digest}",
            calculation_id=first.calculation_id,
            path=first.path,
            kind=first.kind,
            label=first.label,
            sha256=first.sha256,
            bytes=first.bytes,
            filename=first.filename,
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

        build_warnings: list[dict[str, Any]] = []
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
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
            _correction_records_from_record(species_record),
            source_calculation_key=(
                _CALC_KEY_SP if _CALC_KEY_SP in included_keys else None
            ),
            warnings=warnings,
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            omitted_bac_reasons=omitted_bacs,
        )
        if applied_corrections:
            bundle["applied_energy_corrections"] = applied_corrections

        energy_level = (
            _thermo_energy_level(output_doc, species_record)
            if isinstance(species_record.get("thermo"), Mapping) else None
        )
        thermo_block = _build_thermo_block(
            species_record.get("thermo"),
            # Computed-species has a single, unscoped calc namespace, so
            # each included role's own literal ("opt"/"freq"/"sp") is
            # also its bundle-local key — an identity map.
            calc_keys_by_role={key: key for key in included_keys},
            # This bundle's root is ComputedSpeciesUploadRequest, whose
            # thermo field is ``ThermoInBundle`` — the shape that accepts
            # ``source_calculations``.
            target_model="ThermoInBundle",
            warnings=warnings,
            energy_level=energy_level,
            header_corrections_level=output_doc.get("arkane_level_of_theory"),
            element_symbols=_species_element_symbols(species_record),
            energy_level_unattributable=_energy_level_unattributable(output_doc, energy_level),
        )
        # This route has no bundle-level analysis_software_release (the
        # reaction bundle does, and its thermo/statmech inherit it), so the
        # Arkane release goes on the thermo and statmech blocks themselves.
        arkane_release = _arc_analysis_software_release(output_doc)
        _note_omitted_bac_on_thermo(
            thermo_block, omitted_bacs, _bond_corrections_flag(species_record))
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
            calc_keys_by_role=species_calc_keys_by_role,
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

        freq_result = _freq_result_payload(species_record)
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
                        _reused_origin("opt") if _sp_is_reused_from_opt(output_doc) else None
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
            "geometry": {"xyz_text": conformer_xyz_text},
            "primary_calculation": primary_calc,
            "additional_calculations": additional,
        }
        label = species_record.get("label")
        if label:
            block["label"] = str(label)[:64]
        return included, block

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

        label = species_record.get("label")
        candidates: list[str] = []
        unoptimized: list[str] = []
        seen_xyz: set[str] = {selected_xyz_text}
        energies = species_record.get("conformer_energies")
        lockstep = isinstance(energies, (list, tuple)) and len(energies) == len(raw_conformers)
        for index, raw_xyz in enumerate(raw_conformers):
            normalized = _normalize_xyz_text(raw_xyz, label)
            if normalized is None or normalized in seen_xyz:
                continue
            seen_xyz.add(normalized)
            candidates.append(normalized)
            # ARC replaces conformers[i] with the optimized geometry only when
            # its conf_opt finished (arc/scheduler.py:3133-3134, else it just
            # warns), and fills conformer_energies[i] at the same time; a null
            # energy means the geometry is still the force-field one.
            if not lockstep or energies[index] is None:
                unoptimized.append(normalized)
        if not candidates:
            return []

        level = _conformer_screen_level(output_doc, species_record)
        blocks: list[dict[str, Any]] = []
        if level is not None:
            for normalized in candidates:
                if normalized in unoptimized:
                    continue
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
                        result_payload=None,
                        # ``tckdb_origin`` tags the row as a screened-conformer
                        # anchor, NOT a parsed opt job of the selected conformer.
                        tckdb_origin=_screened_conformer_origin(),
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
                opt_calc["output_geometries"] = [
                    {"geometry": {"xyz_text": normalized}, "role": "final"},
                ]
                block: dict[str, Any] = {
                    "key": alt_key,
                    "geometry": {"xyz_text": normalized},
                    "primary_calculation": opt_calc,
                    "additional_calculations": [],
                }
                if label:
                    block["label"] = str(label)[:64]
                blocks.append(block)
        omitted = len(candidates) - len(blocks)
        if omitted:
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
    ) -> dict[str, Any]:
        """Build one CalculationInBundle dict.

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
            artifacts = self._inline_artifacts_for_calc(species_record, calc_role=role)
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
            return [{"geometry": {"xyz_text": conformer_xyz_text}, "role": "final"}]
        if calc_role == _CALC_KEY_OPT_COARSE:
            coarse_out = species_record.get("coarse_opt_output_xyz")
            if not coarse_out:
                return []
            normalized = _normalize_xyz_text(coarse_out, species_record.get("label"))
            if not normalized:
                return []
            return [{"geometry": {"xyz_text": normalized}, "role": "final"}]
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
            return [{"xyz_text": normalized}]
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
            return [{"xyz_text": normalized}] if normalized else []
        if calc_role in (_CALC_KEY_FREQ, _CALC_KEY_SP, _CALC_KEY_IRC):
            # ARC invariant: freq, sp, and (TS) irc all run on the
            # conformer's optimized xyz. Surface it explicitly rather
            # than relying on backend auto-fill — keeps the bundle
            # self-describing.
            if not conformer_xyz_text:
                return []
            return [{"xyz_text": conformer_xyz_text}]
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
            return self._build_calc_in_bundle(
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
            if kind == "output_log":
                record_field = _resolve_log_field(calc_role, species_record)
            else:
                record_field = field_map.get(calc_role)
            artifact = self._read_inline_artifact(
                species_record,
                calc_role=calc_role,
                kind=kind,
                record_field=record_field,
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
    ) -> dict[str, Any] | None:
        """Resolve, read, hash, and base64-encode one artifact for the bundle.

        Returns ``None`` (with a debug or warning log) on any of:
        unknown calc_key, missing/null record path, file not on disk, or
        file exceeding ``max_size_mb``. Otherwise returns the
        ``ArtifactIn``-shaped dict ready to drop into ``calc.artifacts``.
        """
        if record_field is None:
            return None
        path_value = species_record.get(record_field)
        if not path_value:
            return None
        resolved = self._resolve_local_path(path_value)
        if resolved is None or not resolved.is_file():
            logger.debug(
                "TCKDB bundle: %s %s artifact missing on disk for %s (path=%s)",
                calc_role, kind, species_record.get("label"), path_value,
            )
            return None
        size_bytes = resolved.stat().st_size
        max_bytes = self._config.artifacts.max_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            logger.warning(
                "TCKDB bundle: %s %s %s skipped (%s bytes > %s MB cap)",
                calc_role, kind, resolved.name, size_bytes,
                self._config.artifacts.max_size_mb,
            )
            return None
        with resolved.open("rb") as fh:
            content = fh.read()
        return {
            "kind": kind,
            "filename": _coerce_artifact_filename(resolved.name, kind),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": size_bytes,
        }

    def _resolve_local_path(self, file_path: str | Path) -> Path | None:
        """Resolve a local file path against project_directory if it's relative."""
        if file_path is None:
            return None
        path = Path(file_path)
        if path.is_absolute():
            return path
        if self._project_directory is not None:
            return Path(self._project_directory) / path
        return path.resolve()

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

        build_warnings: list[dict[str, Any]] = []
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
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

        for i, label in enumerate(reactant_labels):
            actor_key = _local_key_for_actor("r", i, label)
            calc_prefix = _calc_prefix_for_actor("r", i)
            record = species_index.get(label)
            if record is None:
                raise ValueError(
                    f"reaction {reaction_record.get('label')!r}: reactant "
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
            reactant_keys.append(actor_key)
            actor_calc_keys[actor_key] = calc_keys

        for j, label in enumerate(product_labels):
            actor_key = _local_key_for_actor("p", j, label)
            calc_prefix = _calc_prefix_for_actor("p", j)
            record = species_index.get(label)
            if record is None:
                raise ValueError(
                    f"reaction {reaction_record.get('label')!r}: product "
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
            product_keys.append(actor_key)
            actor_calc_keys[actor_key] = calc_keys

        # TS block (inline). Optional — a reaction with no TS still
        # carries kinetics but server-side it's a thinner record.
        ts_label = reaction_record.get("ts_label")
        ts_block: dict[str, Any] | None = None
        ts_calc_keys: dict[str, str] = {}
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
            )

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
            )
            if kinetics_block is not None:
                kinetics_blocks.append(kinetics_block)

        bundle: dict[str, Any] = {
            "species": species_blocks,
            "reactant_keys": reactant_keys,
            "product_keys": product_keys,
        }
        # Emit ``reversible`` only when the producer set it explicitly.
        # ARCReaction has no first-class ``reversible`` attribute yet,
        # so this is normally ``None`` and we fall back to the schema's
        # default of True. The pass-through is here so a future ARC
        # change that sets ``reversible`` on the reaction (e.g., from an
        # RMG import) lands on the wire without a second adapter edit.
        reversible = reaction_record.get("reversible")
        if reversible is not None:
            bundle["reversible"] = bool(reversible)
        if ts_block is not None:
            bundle["transition_state"] = ts_block
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
        conformer_xyz_text = _require_xyz_text(species_record)
        opt_key = f"{calc_prefix}_{_CALC_KEY_OPT}"
        opt_coarse_key = f"{calc_prefix}_{_CALC_KEY_OPT_COARSE}"
        freq_key = f"{calc_prefix}_{_CALC_KEY_FREQ}"
        sp_key = f"{calc_prefix}_{_CALC_KEY_SP}"
        geom_key = f"{actor_key}_geom"
        conf_key = f"{actor_key}_conf0"

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

        calc_keys: dict[str, str] = {_CALC_KEY_OPT: opt_key}
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

        freq_result = _freq_result_payload(species_record)
        if freq_result is not None:
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
        if sp_result is not None:
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
                        _reused_origin("opt") if _sp_is_reused_from_opt(output_doc) else None
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
        for scan_entry in _scan_entries_from_record(species_record):
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
                    "geometry": {"key": geom_key, "xyz_text": conformer_xyz_text},
                    "calculation": primary_calc,
                }
            ],
            "calculations": additional,
        }
        energy_level = (
            _thermo_energy_level(output_doc, species_record)
            if isinstance(species_record.get("thermo"), Mapping) else None
        )
        thermo_block = _build_thermo_block(
            species_record.get("thermo"),
            # The current reaction root accepts species-scoped thermo provenance.
            calc_keys_by_role=calc_keys,
            target_model="BundleThermoIn",
            warnings=warnings,
            warning_field=f"species[{actor_key}].thermo",
            energy_level=energy_level,
            header_corrections_level=output_doc.get("arkane_level_of_theory"),
            element_symbols=_species_element_symbols(species_record),
            energy_level_unattributable=_energy_level_unattributable(output_doc, energy_level),
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
            _correction_records_from_record(species_record),
            source_calculation_key=calc_keys.get(_CALC_KEY_SP),
            warnings=warnings,
            warning_field=f"species[{actor_key}].applied_energy_corrections",
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            omitted_bac_reasons=omitted_bacs,
        )
        _note_omitted_bac_on_thermo(
            species_block.get("thermo"), omitted_bacs,
            _bond_corrections_flag(species_record))
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
            calc_keys_by_role=calc_keys,
            workflow_tool_release=_arc_workflow_tool_release(output_doc),
            target_model="BundleStatmechIn",
            scan_key_renames=scan_key_renames or None,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            unbuilt_scans=unbuilt_scans,
            warnings=warnings,
            warning_field=f"species[{actor_key}].statmech",
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
            neb_level = (
                _resolve_level(output_doc, "neb") if ts_guess_method == "neb" else None)
            # ``neb_level`` is ORCA's (orca_neb_settings) but ARC's Level
            # deduces a software from the method alone (wb97xd/def2tzvp gives
            # gaussian), so ``neb_level.software`` says nothing about what
            # ran. The program is the one observed on the NEB log
            # (``ess_software.neb``), or none: then nothing is filed.
            observed = ts_record.get("ess_software")
            if neb_level is not None and not (
                    isinstance(observed, Mapping) and observed.get("neb")):
                _warn_ts_guess_level_not_stated(
                    warnings, ts_label=ts_label, method=ts_guess_method,
                    reason="ARC recorded no ess_software.neb for the NEB log",
                    software=True)
            elif neb_level is None:
                _warn_ts_guess_level_not_stated(
                    warnings, ts_label=ts_label, method=ts_guess_method)
            else:
                ts_guess_level = neb_level
                ts_guess_level_kind = "neb"
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

        freq_result = _freq_result_payload(ts_record)
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
                        _reused_origin("opt") if _sp_is_reused_from_opt(output_doc) else None
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
        if irc_level is not None and not irc_level.get("software"):
            # ARC's IRC program rule gives no ESS this adapter can name.
            logger.warning(
                "TCKDB irc_software_not_stated: TS %r IRC calculation not filed: "
                "its level's method (%s) is not run in Gaussian, and ARC records no "
                "IRC program.", ts_label, irc_level.get("method"))
            if warnings is not None:
                warnings.append({
                    "code": "irc_software_not_stated",
                    "message": (
                        f"The IRC calculation of {ts_label!r} was not uploaded: ARC runs "
                        "an IRC in Gaussian (arc/level.py deduce_software) except for "
                        f"UMA/torchani/xtb-type methods ({irc_level.get('method')!r}), "
                        "and records no program for it."),
                    "field": "transition_state.irc.software_release",
                    "context": {"source": "tckdb_arc_self_check",
                                "action": "irc_calculation_omitted"},
                })
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
            "geometry": {"key": ts_geom_key, "xyz_text": conformer_xyz_text},
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
        )
        if validation_evidence:
            ts_block["validation_evidence"] = validation_evidence

        # TS-side AEC/BAC corrections target the TS entry directly via
        # ``target_transition_state_entry_id`` server-side — never via the
        # reaction entry. Anchor to ts_sp specifically; species-side SP
        # keys (r0_sp / p1_sp) belong to other species and a cross-owner
        # reference would 422.
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(ts_record),
            source_calculation_key=calc_keys.get(_CALC_KEY_SP),
            warnings=warnings,
            warning_field="transition_state.applied_energy_corrections",
            target_kind="transition_state",
            target_label=str(ts_label) or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
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

        build_warnings: list[dict[str, Any]] = []
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
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

        Two pieces of computed-reaction provenance are intentionally NOT
        carried here because the ``/uploads/transition-states`` schema has
        no slot for them:

        * **Inline artifacts** (ESS logs / input decks). The bundle path
          base64-inlines these under each calc when
          ``config.artifacts.upload`` is set; the standalone endpoint has
          no artifact field, so ``_build_ts_block`` is called with
          ``include_artifacts=False`` to skip the read+encode entirely
          (rather than build-then-drop), and a one-time WARNING tells the
          user their request can't be honored on this path.
        * **Applied energy corrections** (AEC/BAC). ``_build_ts_block``
          attaches ``applied_energy_corrections`` to the TS block for the
          reaction bundle, but the standalone request carries no such
          field. We drop them deliberately (a debug log records when a TS
          actually had them) — the raw SP energy still ships on the ``sp``
          calc, so no scientific data the endpoint can store is lost.
        """
        species_index = _index_species(output_doc)

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

        ts_warnings: list[dict[str, Any]] = []
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
        )
        # The standalone request drops the TS's applied energy corrections
        # (below), so findings about them do not describe this upload.
        if warnings is not None:
            warnings.extend(
                w for w in ts_warnings
                if not str(w.get("field", "")).startswith(
                    "transition_state.applied_energy_corrections")
            )

        # Applied energy corrections travel on the TS block for the
        # reaction bundle but have no home in the standalone request. Log
        # (debug) when we drop a non-empty set so the omission is visible
        # and clearly intentional, not a silent data loss.
        if ts_block.get("applied_energy_corrections"):
            logger.debug(
                "TCKDB transition-state %r: %d applied energy correction(s) "
                "dropped — /uploads/transition-states has no slot for them.",
                str(ts_label),
                len(ts_block["applied_energy_corrections"]),
            )

        # primary_opt is required and must be type=opt; _build_ts_block
        # always emits ts_block["calculation"] as the type=opt primary.
        primary_opt = self._ts_calc_to_standalone(ts_block["calculation"])
        additional_calculations = []
        for calc in ts_block.get("calculations", []):
            if calc.get("type") == "scan":
                logger.warning(
                    "TCKDB transition-state %r: standalone uploads cannot carry "
                    "rotor scan results; use computed_reaction mode to retain them.",
                    str(ts_label),
                )
                continue
            additional_calculations.append(self._ts_calc_to_standalone(calc))

        request: dict[str, Any] = {
            "reaction": self._build_ts_reaction_upload(
                reaction_record=reaction_record,
                species_index=species_index,
            ),
            "charge": ts_block["charge"],
            "multiplicity": ts_block["multiplicity"],
            "geometry": {"xyz_text": ts_block["geometry"]["xyz_text"]},
            "primary_opt": primary_opt,
        }
        if additional_calculations:
            request["additional_calculations"] = additional_calculations
        # The standalone route has no calculation-key namespace: evidence binds
        # to the upload's single irc additional calculation and must omit
        # ``source_calculation_key``.
        if ts_block.get("validation_evidence"):
            request["validation_evidence"] = [
                {k: v for k, v in evidence.items() if k != "source_calculation_key"}
                for evidence in ts_block["validation_evidence"]
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
    ) -> dict[str, Any]:
        """Build the embedded ``TSReactionUpload`` for a standalone TS upload.

        Ordered reactant/product participants resolve each label through
        ``species_index`` into a ``SpeciesEntryIdentityPayload`` via
        :meth:`_species_entry_payload`. A missing label raises — the same
        loud failure the computed-reaction path uses — so a TS is never
        uploaded with an incomplete reaction description.

        ``reversible`` is required by the schema (no default): ARC has no
        first-class per-reaction ``reversible`` attribute yet, so this
        falls back to True (the same default the computed-reaction schema
        applies) unless the record carries an explicit value. Kept by
        maintainer decision (adapter 0.6.0): refusing would block every
        standalone TS upload, since ARC never states it. TCKDB issue
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

        reversible = reaction_record.get("reversible")
        reaction: dict[str, Any] = {
            "reversible": bool(reversible) if reversible is not None else True,
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
        # Optional ``unmapped_smiles``: TCKDB carries a free-form
        # alternate identity string for cases where the primary
        # ``smiles`` is something special (e.g. an atom-mapped form
        # from a future producer path). The adapter only forwards an
        # explicit value the producer surfaces — it never derives by
        # string manipulation, never strips atom maps from arbitrary
        # strings. Omitted when missing/empty/whitespace, and also
        # omitted when identical to the main ``smiles`` (the schema
        # would accept the duplicate but storing the same string twice
        # adds no identity information).
        unmapped_raw = record.get("unmapped_smiles")
        if isinstance(unmapped_raw, str):
            unmapped_text = unmapped_raw.strip()
            if unmapped_text and unmapped_text != smiles_text:
                entry["unmapped_smiles"] = unmapped_text
        return entry

    def _build_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
    ) -> dict[str, Any]:
        species_entry = self._species_entry_payload(species_record)
        geometry_payload = {"xyz_text": _require_xyz_text(species_record)}
        primary, additional = self._build_calculations(output_doc, species_record)

        payload: dict[str, Any] = {
            "species_entry": species_entry,
            "geometry": geometry_payload,
            "calculation": primary,
            "scientific_origin": "computed",
        }
        if additional:
            payload["additional_calculations"] = additional
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
        """
        primary = cls._calculation_payload(
            output_doc,
            record,
            calc_type="opt",
            level=_resolve_level(output_doc, "opt", record),
            ess_job_key="opt",
            result_field="opt_result",
            result_payload=_opt_result_payload(record),
        )

        additional: list[dict[str, Any]] = []
        freq_result = _freq_result_payload(record)
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
            sp_origin = _reused_origin("opt") if _sp_is_reused_from_opt(output_doc) else None
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
    ) -> dict[str, Any]:
        if not isinstance(level, Mapping):
            adaptive = (
                f" ARC ran with adaptive_levels naming {calc_type}, and the level "
                f"this species ran at cannot be attributed "
                f"({_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.get(calc_type, calc_type + '_level_adaptive_not_attributable')})."
                if _adaptive_kind_named(output_doc, calc_type) else ""
            )
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
        # ARC's per-job banner identification names the program that actually
        # ran; the requested level may name a different troubleshooting ESS.
        ess_software = record.get("ess_software")
        observed_software = (
            ess_software.get(ess_job_key)
            if isinstance(ess_software, Mapping) else None
        )
        software_name = observed_software or level.get("software")
        if not software_name:
            raise ValueError(
                f"level of theory for {calc_type} is missing software; "
                "cannot identify the ESS for TCKDB."
            )

        level_of_theory = _arc_level_to_tckdb_lot(level)
        if level_of_theory is None:
            # method was already validated above; this is defensive against
            # a future change to _arc_level_to_tckdb_lot that drops the row.
            raise ValueError(
                f"could not project level of theory for {calc_type} onto "
                "TCKDB LevelOfTheoryRef shape."
            )

        # These are the references recorded from the actual freq/SP inputs.
        # A species-level stability verdict does not prove the reference of
        # its other jobs, so do not propagate it to opt, scans, or IRC.
        scf_reference = record.get("scf_reference")
        if calc_type in {_CALC_KEY_FREQ, _CALC_KEY_SP} and isinstance(scf_reference, Mapping):
            reference = scf_reference.get(f"{calc_type}_reference")
            if reference in {"restricted", "unrestricted", "restricted_open"}:
                level_of_theory["spin_treatment"] = reference

        software_release: dict[str, Any] = {"name": str(software_name)}
        ess_versions = record.get("ess_versions")
        if isinstance(ess_versions, Mapping):
            # ess_versions is keyed by job type ('opt', 'freq', 'sp', 'neb'),
            # not by software name. Fall back to opt's version if the
            # job-specific entry is missing (often the case for combined
            # opt+freq runs or shared sp/freq logs).
            ess_version = ess_versions.get(ess_job_key)
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

        hessian_method = record.get("freq_hessian_method")
        if calc_type == _CALC_KEY_FREQ and hessian_method in {
            "analytic", "finite_difference_gradient", "finite_difference_energy",
        }:
            calc["parameters"] = [{
                "raw_key": "freq_hessian_method",
                "raw_value": hessian_method,
                "canonical_key": "freq.hessian_method",
                "canonical_value": hessian_method,
                "section": "freq",
                "value_type": "string",
            }]

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

    def _finalize_skipped(self, written: WrittenPayload) -> UploadOutcome:
        sc = written.sidecar
        sc.status = "skipped"
        self._writer.update_sidecar(written.sidecar_path, sc)
        # Two distinct reasons land here: (a) ``upload=false`` for a
        # complete bundle, and (b) phase-1 partial sidecars that are
        # intentionally never live-POSTed. Pick the right log line so
        # operators reading logs don't have to grep ``is_partial`` to
        # reverse-engineer the cause.
        if sc.is_partial:
            logger.info(
                "TCKDB partial sidecar finalized (status=skipped, is_partial=true; "
                "phase-1 policy: partial bundles are sidecar-only, never POSTed): %s",
                written.payload_path,
            )
        else:
            logger.info(
                "TCKDB upload skipped (upload=false): %s", written.payload_path,
            )
        return UploadOutcome(
            status="skipped",
            payload_path=written.payload_path,
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            warnings=list(sc.warnings),
        )

    def _upload(
        self,
        written: WrittenPayload,
        payload: dict[str, Any],
        *,
        endpoint: str = CONFORMER_UPLOAD_ENDPOINT,
    ) -> UploadOutcome:
        sc = written.sidecar
        api_key = self._config.resolve_api_key()
        if not api_key:
            msg = (
                f"TCKDB API key not configured (tried: "
                f"{self._config.describe_api_key_sources()}); "
                "skipping network call and recording sidecar as failed."
            )
            return self._record_failure(written, msg, raised=ValueError(msg))

        try:
            client = self._make_client(api_key)
        except Exception as exc:  # pragma: no cover - defensive
            return self._record_failure(written, f"client init failed: {exc}", exc)

        try:
            self._ensure_ready(client)
            _attach_preflight(sc, self._preflight_metadata)
            response = client.request_json(
                "POST",
                endpoint,
                json=payload,
                idempotency_key=sc.idempotency_key,
            )
        except Exception as exc:
            _close_quietly(client, "after upload failure")
            return self._record_failure(written, str(exc), exc)
        else:
            _close_quietly(client, "after upload success")

        sc.status = "uploaded"
        sc.uploaded_at = _utcnow_iso()
        sc.response_status_code = getattr(response, "status_code", None)
        response_data = getattr(response, "data", None)
        sc.response_body = _summarize_response_body(response_data)
        # Keep the server's structured scientific findings independently of
        # the response summary so callers need not inspect an HTTP envelope.
        # They follow any producer-side warnings recorded at write time.
        response_warnings = (
            response_data.get("warnings", []) if isinstance(response_data, dict) else []
        )
        server_warnings = (
            [dict(item) for item in response_warnings if isinstance(item, dict)]
            if isinstance(response_warnings, list) else []
        )
        for warning in server_warnings:
            logger.warning("TCKDB upload warning: %s", warning)
        sc.warnings = [*sc.warnings, *server_warnings]
        sc.public_refs = _extract_tckdb_public_refs(response_data)
        _append_request_id(sc, "upload", response)
        sc.idempotency_replayed = bool(getattr(response, "idempotency_replayed", False))
        sc.last_error = None
        self._writer.update_sidecar(written.sidecar_path, sc)
        if sc.idempotency_replayed:
            logger.info(
                "TCKDB upload replayed (idempotent): %s key=%s",
                written.payload_path,
                sc.idempotency_key,
            )
        else:
            logger.info(
                "TCKDB upload succeeded: %s key=%s",
                written.payload_path,
                sc.idempotency_key,
            )
        primary, additional = _extract_calc_refs(response_data)
        return UploadOutcome(
            status="uploaded",
            payload_path=written.payload_path,
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            response=sc.response_body,
            primary_calculation=primary,
            additional_calculations=additional,
            warnings=sc.warnings,
        )

    def _record_failure(
        self, written: WrittenPayload, message: str, raised: BaseException
    ) -> UploadOutcome:
        sc = written.sidecar
        sc.status = "failed"
        # Pull structured HTTP details off the exception when present
        # (TCKDBClient attaches ``status_code``, ``response_json``, and
        # ``response_text`` on its HTTP-error subclasses). FastAPI 422
        # bodies otherwise collapse to "HTTP 422" via the exception's
        # default message, hiding the field-level rejection reason.
        # Non-HTTP failures (timeouts, connection errors, etc.) lack
        # these attrs and fall back to the original ``message`` —
        # preserving the prior log shape for those paths.
        status_code = getattr(raised, "status_code", None)
        response_json = getattr(raised, "response_json", None)
        response_text = getattr(raised, "response_text", None)
        if response_json is not None:
            sc.response_body = _summarize_response_body(response_json)
        elif response_text:
            sc.response_body = _summarize_response_body(response_text)
        if status_code is not None:
            sc.response_status_code = status_code
        if isinstance(raised, TCKDBReadinessError):
            sc.preflight = {
                "ready": False,
                "status_code": status_code,
                "request_id": _request_id_from(raised),
            }
            _append_request_id(sc, "readyz", raised)
        else:
            _append_request_id(sc, "upload", raised)
        detail_for_message: Any = (
            response_json
            if response_json is not None
            else (response_text or message)
        )
        sc.last_error = (
            f"HTTP {status_code}: {detail_for_message}"
            if status_code is not None
            else message
        )
        self._writer.update_sidecar(written.sidecar_path, sc)
        logger.warning(
            "TCKDB upload failed (strict=%s): %s key=%s err=%s",
            self._config.strict,
            written.payload_path,
            sc.idempotency_key,
            sc.last_error,
        )
        if isinstance(raised, TCKDBReadinessError):
            self._log_readiness_recovery()
        if self._config.strict:
            raise raised
        return UploadOutcome(
            status="failed",
            payload_path=written.payload_path,
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            error=sc.last_error,
            warnings=list(sc.warnings),
        )

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

    def _make_client(self, api_key: str):
        if self._client_factory is not None:
            return self._client_factory(self._config, api_key)
        return TCKDBClient(
            self._config.base_url,
            api_key=api_key,
            timeout=self._config.timeout_seconds,
        )

    def _ensure_ready(self, client: Any) -> None:
        """Run the optional TCKDB readyz preflight once per adapter instance.

        The probe is retried up to :data:`PREFLIGHT_MAX_ATTEMPTS` times
        with exponential backoff (see the module constants) so a transient
        not-ready blip mid-run doesn't drop an upload. Both failure modes
        are retried: a request exception (server unreachable / 5xx) and a
        200 body that reports not-ready. The first attempt that reports
        ready wins; only after all attempts are exhausted is
        ``self._preflight_error`` set and raised. The single-check-per-
        instance semantics are preserved — the whole retry loop runs once,
        and the final error is cached so later calls re-raise it cheaply.
        """
        if not self._config.preflight:
            return
        if self._preflight_error is not None:
            raise self._preflight_error
        if self._preflight_checked:
            return
        self._preflight_checked = True

        last_error: TCKDBReadinessError | None = None
        for attempt in range(PREFLIGHT_MAX_ATTEMPTS):
            try:
                response = client.request_json("GET", "/readyz", authenticated=False)
            except TCKDBError as exc:
                # Server unreachable / timeout / 5xx during a blip. Build
                # the readiness error now so, if this is the last attempt,
                # we raise a message consistent with the not-ready path.
                last_error = _build_readiness_error(exc)
            else:
                data = getattr(response, "data", None)
                ready = _readyz_body_is_ready(data)
                metadata = {
                    "ready": ready,
                    "status_code": getattr(response, "status_code", None),
                    "request_id": _request_id_from(response),
                }
                if isinstance(data, Mapping):
                    for key in ("status", "code", "alembic_revision"):
                        if data.get(key) is not None:
                            metadata[key] = data.get(key)
                self._preflight_metadata = metadata
                if ready:
                    return
                last_error = TCKDBReadinessError(
                    _format_readiness_message(
                        status_code=metadata.get("status_code"),
                        body=data,
                        request_id=metadata.get("request_id"),
                    ),
                    status_code=metadata.get("status_code"),
                    response_json=data if isinstance(data, Mapping) else None,
                    response_text=None if isinstance(data, Mapping) else str(data),
                    headers=getattr(response, "headers", None),
                )

            # Not ready (error or not-ready body). Back off before the
            # next attempt unless this was the final one.
            if attempt < PREFLIGHT_MAX_ATTEMPTS - 1:
                delay = min(
                    PREFLIGHT_BASE_DELAY_SECONDS * (2 ** attempt),
                    PREFLIGHT_MAX_DELAY_SECONDS,
                )
                logger.info(
                    "TCKDB /readyz not ready (attempt %d/%d); retrying in %.1fs.",
                    attempt + 1, PREFLIGHT_MAX_ATTEMPTS, delay,
                )
                _preflight_sleep(delay)

        # All attempts exhausted. ``last_error`` is always set here because
        # every loop iteration that reaches this point set it.
        self._preflight_error = last_error
        raise self._preflight_error

    def _prepare_artifact_upload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_label: str,
        calculation_id: int,
        kind: str,
        file_path: str | Path,
        artifact_cfg: Any,
    ) -> _PreparedArtifactUpload | ArtifactUploadOutcome:
        # Defense-in-depth: in bundle modes the bundle payload already
        # carries input/output_log artifacts inline under each calc.
        if (
            self._config.upload_mode in _BUNDLE_MODES_WITH_INLINE_ARTIFACTS
            and kind in IMPLEMENTED_ARTIFACT_KINDS
        ):
            return _skip(
                calculation_id, kind,
                f"upload_mode={self._config.upload_mode!r} carries kind={kind!r} "
                "inline in the bundle; standalone artifact upload suppressed",
            )

        if not artifact_cfg.upload:
            return _skip(calculation_id, kind, "artifacts.upload is False")
        if kind not in artifact_cfg.kinds:
            return _skip(calculation_id, kind, f"kind {kind!r} not in config.kinds")
        if kind not in IMPLEMENTED_ARTIFACT_KINDS:
            return _skip(
                calculation_id, kind,
                f"kind {kind!r} is server-accepted but ARC has no upload path yet",
            )

        resolved = self._resolve_local_path(file_path)
        if resolved is None or not resolved.is_file():
            return _skip(calculation_id, kind, f"file missing: {file_path!r}")

        size_bytes = resolved.stat().st_size
        max_bytes = artifact_cfg.max_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            return _skip(
                calculation_id,
                kind,
                f"file {resolved.name} is {size_bytes} bytes "
                f"(>{artifact_cfg.max_size_mb} MB cap)",
            )

        with resolved.open("rb") as fh:
            content = fh.read()
        sha256 = hashlib.sha256(content).hexdigest()

        project_label = self._config.project_label or output_doc.get("project")
        idempotency_key = build_artifact_idempotency_key(ArtifactIdempotencyInputs(
            project_label=project_label,
            species_label=species_label,
            calculation_id=calculation_id,
            artifact_kind=kind,
            artifact_sha256=sha256,
        ))
        endpoint = ARTIFACTS_ENDPOINT_TEMPLATE.format(calculation_id=calculation_id)
        written_artifact = self._writer.write_artifact_sidecar(
            species_label=species_label,
            calculation_id=calculation_id,
            kind=kind,
            filename=resolved.name,
            sha256=sha256,
            bytes_=size_bytes,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            source_path=str(resolved),
            base_url=self._config.base_url,
        )
        return _PreparedArtifactUpload(
            written=written_artifact,
            calculation_key=f"{kind}-{sha256[:16]}",
            calculation_id=calculation_id,
            path=resolved,
            kind=kind,
            label=species_label,
            sha256=sha256,
            bytes=size_bytes,
            filename=resolved.name,
        )

    def _upload_artifact_batch(
        self,
        *,
        prepared: list[_PreparedArtifactUpload],
        idempotency_key_prefix: str,
    ) -> list[ArtifactUploadOutcome]:
        api_key = self._config.resolve_api_key()
        if not api_key:
            msg = (
                f"TCKDB API key not configured (tried: "
                f"{self._config.describe_api_key_sources()}); "
                "skipping artifact network call."
            )
            return self._record_artifact_batch_failure(prepared, msg, ValueError(msg))

        try:
            client = self._make_client(api_key)
        except Exception as exc:  # pragma: no cover - defensive
            return self._record_artifact_batch_failure(
                prepared, f"client init failed: {exc}", exc
            )

        try:
            self._ensure_ready(client)
            for item in prepared:
                _attach_preflight(item.written.sidecar, self._preflight_metadata)
            batch_results = client.upload_artifacts(
                prepared,
                idempotency_key_prefix=idempotency_key_prefix,
                batch_by_calculation=True,
            )
        except (AttributeError, TypeError) as exc:
            _close_quietly(client, "after artifact upload failure")
            msg = (
                "Installed tckdb-client does not support "
                "batch_by_calculation artifact uploads. Upgrade tckdb-client "
                "to the version expected by this ARC branch."
            )
            return self._record_artifact_batch_failure(prepared, msg, exc)
        except Exception as exc:
            _close_quietly(client, "after artifact upload failure")
            return self._record_artifact_batch_failure(prepared, str(exc), exc)
        else:
            _close_quietly(client, "after artifact upload success")

        batch_summary = _summarize_artifact_batch_results(batch_results)
        outcomes: list[ArtifactUploadOutcome] = []
        for item in prepared:
            sc = item.written.sidecar
            sc.status = "uploaded"
            sc.uploaded_at = _utcnow_iso()
            sc.response_status_code = _artifact_batch_status_code(batch_results)
            sc.response_body = _summarize_response_body(batch_summary)
            sc.public_refs = _extract_tckdb_public_refs(batch_summary)
            for result in _artifact_batch_result_items(batch_results):
                _append_request_id(sc, "artifact_upload", result)
            sc.idempotency_replayed = _artifact_batch_replayed(batch_results)
            sc.last_error = None
            self._writer.update_artifact_sidecar(item.written.sidecar_path, sc)
            logger.info(
                "TCKDB artifact batch upload succeeded: calc=%s kind=%s key=%s",
                sc.calculation_id, sc.kind, sc.idempotency_key,
            )
            outcomes.append(ArtifactUploadOutcome(
                status="uploaded",
                sidecar_path=item.written.sidecar_path,
                idempotency_key=sc.idempotency_key,
                calculation_id=sc.calculation_id,
                kind=sc.kind,
                response=sc.response_body,
            ))
        return outcomes

    def _record_artifact_batch_failure(
        self,
        prepared: list[_PreparedArtifactUpload],
        message: str,
        raised: BaseException,
    ) -> list[ArtifactUploadOutcome]:
        outcomes = [
            self._record_artifact_failure(item.written, message, raised, raise_on_strict=False)
            for item in prepared
        ]
        if self._config.strict:
            raise raised
        return outcomes

    def _record_artifact_failure(
        self,
        written: WrittenArtifact,
        message: str,
        raised: BaseException,
        *,
        raise_on_strict: bool = True,
    ) -> ArtifactUploadOutcome:
        sc = written.sidecar
        sc.status = "failed"
        sc.last_error = message
        status_code = getattr(raised, "status_code", None)
        response_json = getattr(raised, "response_json", None)
        response_text = getattr(raised, "response_text", None)
        if status_code is not None:
            sc.response_status_code = status_code
        if response_json is not None:
            sc.response_body = _summarize_response_body(response_json)
        elif response_text:
            sc.response_body = _summarize_response_body(response_text)
        if isinstance(raised, TCKDBReadinessError):
            sc.preflight = {
                "ready": False,
                "status_code": getattr(raised, "status_code", None),
                "request_id": _request_id_from(raised),
            }
            _append_request_id(sc, "readyz", raised)
        else:
            _append_request_id(sc, "artifact_upload", raised)
        self._writer.update_artifact_sidecar(written.sidecar_path, sc)
        logger.warning(
            "TCKDB artifact upload failed (strict=%s): calc=%s kind=%s err=%s",
            self._config.strict, sc.calculation_id, sc.kind, message,
        )
        if self._config.strict and raise_on_strict:
            raise raised
        return ArtifactUploadOutcome(
            status="failed",
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            calculation_id=sc.calculation_id,
            kind=sc.kind,
            error=message,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _artifact_batch_digest(items: list[_PreparedArtifactUpload]) -> str:
    """Content digest for a calculation-scoped artifact batch idempotency key."""
    h = hashlib.sha256()
    for item in items:
        h.update(str(item.calculation_id).encode("utf-8"))
        h.update(b"\0")
        h.update(item.kind.encode("utf-8"))
        h.update(b"\0")
        h.update(item.filename.encode("utf-8"))
        h.update(b"\0")
        h.update(item.sha256.encode("ascii"))
        h.update(b"\0")
        h.update(str(item.bytes).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _artifact_batch_idempotency_prefix(
    project_label: Any,
    species_label: Any,
) -> str:
    """Stable prefix consumed by ``client.upload_artifacts`` batch mode."""
    def clean(value: Any, *, limit: int) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "unknown"))
        cleaned = cleaned.strip(":-.") or "unknown"
        return cleaned[:limit]

    return f"arc:{clean(project_label, limit=48)}:{clean(species_label, limit=64)}:artifact"


def _summarize_artifact_batch_results(batch_results: Any) -> Any:
    """Make tckdb-client ``ArtifactUploadBatchResult`` objects JSON-shaped."""
    if isinstance(batch_results, list):
        summarized = []
        for result in batch_results:
            response = getattr(result, "response", result)
            response_data = getattr(response, "data", response)
            summarized.append({
                "calculation_id": getattr(result, "calculation_id", None),
                "calculation_keys": list(getattr(result, "calculation_keys", ()) or ()),
                "artifact_count": getattr(result, "artifact_count", None),
                "response": response_data,
            })
        return summarized
    return batch_results


def _artifact_batch_status_code(batch_results: Any) -> int | None:
    if not isinstance(batch_results, list):
        return getattr(batch_results, "status_code", None)
    for result in batch_results:
        response = getattr(result, "response", None)
        status_code = getattr(result, "status_code", None)
        if status_code is None:
            status_code = getattr(response, "status_code", None)
        if status_code is not None:
            return status_code
    return None


def _artifact_batch_replayed(batch_results: Any) -> bool | None:
    if not isinstance(batch_results, list):
        replayed = getattr(batch_results, "idempotency_replayed", None)
        return bool(replayed) if replayed is not None else None
    replay_values: list[bool] = []
    for result in batch_results:
        response = getattr(result, "response", None)
        replayed = getattr(result, "idempotency_replayed", None)
        if replayed is None:
            replayed = getattr(response, "idempotency_replayed", None)
        if replayed is not None:
            replay_values.append(bool(replayed))
    return any(replay_values) if replay_values else None


def _artifact_batch_result_items(batch_results: Any) -> list[Any]:
    return batch_results if isinstance(batch_results, list) else [batch_results]


def _headers_from(obj: Any) -> Mapping[str, str] | None:
    headers = getattr(obj, "headers", None)
    if headers is None:
        response = getattr(obj, "response", None)
        headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else None


def _request_id_from(obj: Any) -> str | None:
    headers = _headers_from(obj)
    if not headers:
        return None
    for name, value in headers.items():
        if str(name).lower() == "x-request-id" and value:
            return str(value)
    return None


def _append_request_id(sidecar: Any, operation: str, obj: Any) -> None:
    request_id = _request_id_from(obj)
    if not request_id:
        return
    entry = {
        "operation": operation,
        "request_id": request_id,
        "status_code": getattr(obj, "status_code", None),
    }
    if entry["status_code"] is None:
        response = getattr(obj, "response", None)
        entry["status_code"] = getattr(response, "status_code", None)
    sidecar.request_ids.append(entry)


def _attach_preflight(sidecar: Any, metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    sidecar.preflight = dict(metadata)
    request_id = metadata.get("request_id")
    if request_id:
        sidecar.request_ids.append({
            "operation": "readyz",
            "request_id": request_id,
            "status_code": metadata.get("status_code"),
        })


def _preflight_sleep(seconds: float) -> None:
    """Sleep between readiness-probe retries.

    Thin indirection over :func:`time.sleep` so tests can patch out the
    real backoff wait (``mock.patch('tckdb_arc.adapter._preflight_sleep')``)
    and run instantly without changing the retry logic.
    """
    time.sleep(seconds)


def _readyz_body_is_ready(body: Any) -> bool:
    if not isinstance(body, Mapping):
        return False
    if body.get("ready") is True:
        return True
    status = body.get("status")
    return isinstance(status, str) and status.lower() in {"ready", "ok"}


def _build_readiness_error(exc: BaseException) -> TCKDBReadinessError:
    response_json = getattr(exc, "response_json", None)
    response_text = getattr(exc, "response_text", None)
    status_code = getattr(exc, "status_code", None)
    headers = getattr(exc, "headers", None)
    return TCKDBReadinessError(
        _format_readiness_message(
            status_code=status_code,
            body=response_json if response_json is not None else response_text,
            request_id=_request_id_from(exc),
        ),
        status_code=status_code,
        response_json=response_json,
        response_text=response_text,
        headers=headers,
    )


def _format_readiness_message(
    *,
    status_code: Any,
    body: Any,
    request_id: Any,
) -> str:
    status = None
    code = None
    if isinstance(body, Mapping):
        status = body.get("status")
        code = body.get("code")
    parts = ["TCKDB readiness check failed before upload:"]
    if status_code is not None:
        parts.append(f"status_code={status_code}")
    if status is not None:
        parts.append(f"status={status}")
    if code is not None:
        parts.append(f"code={code}")
    if request_id is not None:
        parts.append(f"request_id={request_id}")
    if len(parts) == 1 and body:
        parts.append(str(body))
    return " ".join(parts)


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


# TCKDB's ``CalculationWithResultsPayload.origin_kind`` is a *validated*
# enum. The backend hoists ``parameters_json.tckdb_origin.origin_kind``
# into that top-level field (that is why the ``reused_result`` marker,
# which happens to be a valid member, round-trips cleanly), so any value
# ARC emits for ``origin_kind`` — even nested under the "free-form"
# parameters_json — MUST be one of these. ARC-specific provenance detail
# that is NOT an enum member (e.g. "screened_conformer") is carried on a
# separate ``origin_detail`` key, which the backend leaves opaque.
VALID_TCKDB_ORIGIN_KINDS: frozenset[str] = frozenset(
    {"executed", "reused_result", "imported", "derived"}
)


def _reused_origin(reused_from_calc_type: str) -> dict[str, Any]:
    """Build the ``tckdb_origin`` payload for a reused-result calculation.

    Lives under ``parameters_json.tckdb_origin`` on the calculation row.
    The DAG edge between calculations carries the relational link
    (e.g. ``opt -> sp`` with role ``single_point_on``); this dict
    carries the qualifier — *this* row's energy is reused, not freshly
    computed — so downstream consumers can tell aggregate-from-opt SP
    rows apart from independently executed SP jobs.
    """
    return {
        "origin_kind": "reused_result",
        "reused_from": {"calculation_type": reused_from_calc_type},
        "reason": (
            f"sp_level equals {reused_from_calc_type}_level; "
            f"{reused_from_calc_type} electronic energy reused as SP energy"
        ),
        "independent_ess_job": False,
        "producer": "ARC",
    }


# Per-calc-role mapping to the ``<role>_final_settings`` field name on the
# species record. Roles not in the map have no current producer-side
# source of final-settings data; the helper returns ``None`` for them
# and the adapter omits ``parameters_json.final_settings`` from the calc.
_FINAL_SETTINGS_FIELD_BY_CALC_ROLE: Mapping[str, str] = {
    "opt": "opt_final_settings",
    "opt_coarse": "coarse_opt_final_settings",
    "freq": "freq_final_settings",
    "sp": "sp_final_settings",
    "irc": "irc_final_settings",
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


def _screened_conformer_origin() -> dict[str, Any]:
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
        ),
        "independent_ess_job": False,
        "producer": "ARC",
    }


def _split_ess_version_banner(name: str, banner: str) -> dict[str, str]:
    """Split an ESS banner into ``version`` (and ``revision``) as TCKDB would.

    ARC records the banner as printed (``ess_versions``: ``'Gaussian 16,
    Revision C.02'``). TCKDB keeps version and revision in separate columns
    and, given the banner as ``version``, normalises it itself with a
    ``software_release_version_is_composite`` warning. The rule is the shared
    ``tckdb_schemas.fragments.refs.SoftwareReleaseRef.normalize_composite_version``
    (the model the server validates with), so it is reused here rather than
    re-implemented and the adapter sends exactly what the server would store:

    * no internal whitespace (``'16'``, ``'5.0.4'``): unchanged;
    * leading token equal to ``name`` (case-insensitive): stripped, and a
      trailing ``', Revision <label>'`` split into ``revision``
      (``'Gaussian 16, Revision C.02'`` -> ``16`` / ``C.02``, ``'ORCA 5.0.4'``
      -> ``5.0.4``, ``'Molpro 2022.3'`` -> ``2022.3``);
    * leading token naming another program (``name='gaussian'``,
      ``'ORCA 6.0.0'``; ``name='qchem'``, ``'Q-Chem 5.4'``): unchanged, as
      the server leaves it (it warns ``software_release_name_looks_wrong``);
      the adapter never guesses which of the two is right.

    The server's ``[auto] ...`` provenance note is not sent: the split is
    a deterministic reformat, and the banner is reconstructible from
    ``name``/``version``/``revision``.
    """
    unchanged = {"version": banner}
    try:
        ref = SoftwareReleaseRef(name=name, version=banner)
    except ValueError:
        return unchanged
    warning = ref.version_warning()
    if warning is None or warning.code != W_SOFTWARE_RELEASE_VERSION_IS_COMPOSITE:
        return unchanged
    split = {"version": ref.version}
    if ref.revision is not None:
        split["revision"] = ref.revision
    return split


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
    if job_kind in {"opt", "scan", "neb"}:
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

    Applies ARC's own window criterion instead of "most negative wins"
    over ALL modes: a candidate is any imaginary mode whose magnitude
    sits strictly inside (75, 10000) cm-1 — mirroring
    ``arc/checks/ts.py::check_imaginary_frequencies``, the same rule ARC
    itself uses to accept a TS. Restricting candidacy to the window is
    what keeps a parse/SCF artifact (e.g. -12000 cm-1, outside the
    window) from ever being asserted as the barrier over the real
    reaction coordinate (e.g. -1320.5 cm-1) sitting right where ARC
    itself would call it — see
    ``test_ts_designates_window_mode_over_a_stiffer_artifact``.

    When more than one candidate qualifies, the largest-magnitude
    candidate is designated. This is safe against TCKDB's own ambiguity
    check (``W_TS_REACTION_COORDINATE_AMBIGUOUS`` in
    ``stationary_point.py``), which blocks only when an undeclared extra
    mode is *at least as stiff* as the designated one: a smaller
    in-window sibling can never trip it once the larger one is chosen and
    the others are marked ``unassigned``. See
    ``test_ts_two_in_window_candidates_designates_larger_magnitude``.

    Returns ``None`` only when the designation is genuinely undecidable:
    zero candidates qualify, or more than one candidate ties for the
    largest magnitude — including the classic degenerate-pair case,
    where two modes share one magnitude and both sit inside the window.
    Callers must not guess in that case — see
    ``stationary_point.py``'s ``W_TS_REACTION_COORDINATE_AMBIGUOUS``,
    which an undesignated same-magnitude "extra" mode would trip anyway.
    """
    candidates = [
        (i, abs(v)) for i, v in enumerate(imaginary_values, start=1)
        if _TS_MAJOR_MODE_MIN_CM1 < abs(v) < _TS_MAJOR_MODE_MAX_CM1
    ]
    if not candidates:
        return None
    max_magnitude = max(magnitude for _, magnitude in candidates)
    top = [i for i, magnitude in candidates if magnitude == max_magnitude]
    return top[0] if len(top) == 1 else None


def _freq_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build a FreqResultPayload-shaped dict from an output.yml record.

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
    if n_imag and not any(m["is_imaginary"] for m in modes):
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
    if n_imag and n_imag > 1:
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
    # ``sp_energy_hartree`` is ARC's record key; ``electronic_energy_hartree``
    # is the TCKDB-side field name (some records may carry it directly).
    record_key = "sp_energy_hartree" if record.get("sp_energy_hartree") is not None \
        else "electronic_energy_hartree"
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
    note = block.get("note")
    if isinstance(note, str) and note:
        payload["note"] = note
    return payload


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


def _arc_level_to_tckdb_lot(level: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Project ARC's per-job level dict (output.yml shape) onto TCKDB's
    ``LevelOfTheoryRef`` shape, applying field-name translation and
    flattening ``args`` into ``keywords``.

    Returns ``None`` if ``level`` is missing or has no ``method`` —
    callers decide whether to error or skip.
    """
    if not isinstance(level, Mapping):
        return None
    if not level.get("method"):
        return None
    out: dict[str, Any] = {}
    for src, dst in _ARC_TO_TCKDB_LOT_FIELDS.items():
        v = level.get(src)
        if v:
            out[dst] = str(v)
    keywords = _arc_args_to_keywords(level.get("args"))
    if keywords:
        out["keywords"] = keywords
    return out


def _scheme_level_of_theory(scheme: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project ARC's per-species scheme.level_of_theory dict onto TCKDB's
    ``LevelOfTheoryRef`` shape. The scheme dict comes from the same
    ``_level_to_dict(arkane_level_of_theory)`` producer as opt/freq/sp
    levels, so the same field-name translation applies."""
    return _arc_level_to_tckdb_lot(scheme.get("level_of_theory"))


_ARKANE_KEY_RE = re.compile(r"^\s*LevelOfTheory\((?P<body>.*)\)\s*$", re.DOTALL)
_ARKANE_KEY_SOFTWARE_RE = re.compile(r"(?:^|,)\s*software\s*=\s*'(?P<name>[A-Za-z0-9_.+-]+)'\s*(?=,|$)")
_W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT = "energy_correction_scheme_software_conflict"


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


_W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE = (
    "bac_correction_omitted_components_incomplete"
)


def _bond_corrections_flag(record: Mapping[str, Any]) -> bool | None:
    """``thermo.bond_corrections_applied`` (output.yml 1.2) when a bool, else None."""
    thermo = record.get("thermo")
    flag = thermo.get("bond_corrections_applied") if isinstance(thermo, Mapping) else None
    return flag if isinstance(flag, bool) else None


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
    if any(not _component_is_usable(c) for c in items):
        return ("component_unusable",
                "a bond component lacks a parameter or contribution value, so the "
                "decomposition would not sum to the total.")
    has_bond = any(c.get("component_kind") == "bond" for c in items)
    if has_bond:
        return None
    monatomic = (
        target_kind == "species"
        and isinstance(element_symbols, (list, tuple))
        and len(element_symbols) == 1
    )
    if monatomic:
        return None
    if not items:
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

    ``arkane_release`` (see ``_arkane_workflow_tool_release``) is stamped as
    ``scheme.workflow_tool_release`` on ``atom_energy``, ``bac_petersson`` and
    ``bac_melius`` schemes, the kinds built from Arkane's tables.
    """
    if not isinstance(applied_records, list):
        return []

    out: list[dict[str, Any]] = []
    for rec in applied_records:
        if not isinstance(rec, Mapping):
            continue
        if rec.get("value") is None or rec.get("scheme") is None:
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
                warnings.append({
                    "code": _W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE,
                    "message": message,
                    "field": warning_field,
                    "context": {"source": "tckdb_arc_self_check",
                                "action": "bac_correction_omitted",
                                "reason": reason,
                                "target_kind": target_kind,
                                "species": target_label},
                })
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
            scheme_out["level_of_theory"] = lot_ref
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
                    warnings.append({
                        "code": _W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT,
                        "message": message,
                        "field": f"{warning_field}.scheme.software",
                        "context": {"source": "tckdb_arc_self_check",
                                    "action": "scheme_software_omitted",
                                    "scheme_kind": str(scheme_out.get("kind")),
                                    "arkane_key_software": key_software,
                                    "level_software": level_software},
                    })
            else:
                scheme_out["software"] = {"name": key_software}

        if (
            arkane_release is not None
            and "workflow_tool_release" not in scheme_out
            and scheme_out.get("kind") in ("atom_energy", "bac_petersson", "bac_melius")
        ):
            scheme_out["workflow_tool_release"] = dict(arkane_release)

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
    }),
    "BundleThermoIn": frozenset({
        "h298_kj_mol", "s298_j_mol_k", "tmin_k", "tmax_k",
        "nasa", "points", "source_calculations",
        "enthalpy_reference_kind", "reference_pressure_bar",
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
_W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED = "enthalpy_atom_corrections_not_applied"
_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH = "enthalpy_atom_corrections_level_mismatch"
_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE = "enthalpy_atom_corrections_level_unverifiable"
_W_ENTHALPY_NOT_FINITE = "enthalpy_not_finite"
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
_W_ENTHALPY_NOT_FORMATION_MAGNITUDE = "enthalpy_not_formation_magnitude"

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
_W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES = "enthalpy_formation_unverifiable_light_species"
_HEADER_CORRECTIONS_LEVEL_SOURCE = "output_header.arkane_level_of_theory"
_GAS_CONSTANT_J_MOL_K = 8.314462618
_T298_K = 298.15

# TCKDB #529 and the producer contract: ``reference_pressure_bar`` is never
# defaulted, and "if your source does not say which convention a number
# follows, leave the field out". ARC states the standard-state pressure only
# as ``thermo.standard_state_pressure_pa`` (arc/output.py
# ``_thermo_to_dict``; ``None`` when the thermo did not come from a statmech
# run that recorded one). When it is absent or unusable the adapter omits
# ``reference_pressure_bar`` (TCKDB stores "not stated") and reports
# ``thermo_reference_pressure_not_stated``; it never fills RMG's 1 atm in.
_W_THERMO_REFERENCE_PRESSURE_NOT_STATED = "thermo_reference_pressure_not_stated"

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


def _nasa_h298_kj_mol(nasa: Mapping[str, float]) -> float:
    """Evaluate a NASA-7 block's H at 298.15 K (kJ/mol)."""
    prefix = "a" if _T298_K <= nasa["t_mid"] else "b"
    c = [nasa[f"{prefix}{i}"] for i in range(1, 8)]
    t = _T298_K
    h_rt = c[0] + c[1] * t / 2 + c[2] * t**2 / 3 + c[3] * t**3 / 4 + c[4] * t**4 / 5 + c[5] / t
    return h_rt * _GAS_CONSTANT_J_MOL_K * t / 1000.0


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


def _describe_level(level: Any) -> str:
    if not isinstance(level, Mapping) or not level.get("method"):
        return "an unrecorded level"
    method = str(level["method"])
    if level.get("dispersion"):
        method += f" + {level['dispersion']}"
    return "/".join([method, *([str(level["basis"])] if level.get("basis") else [])])


def _xyz_element_symbols(xyz: Any) -> tuple[str, ...] | None:
    """Return the element symbol of every atom in an output.yml ``xyz`` string.

    Accepts ARC's atom-only lines (``xyz_to_str``) with or without an XYZ
    count/comment header. ``None`` when the geometry is missing or a line
    does not start with an element symbol.
    """
    if not isinstance(xyz, str):
        return None
    lines = [line for line in xyz.strip().splitlines() if line.strip()]
    if lines and lines[0].strip().isdigit():
        lines = [line for line in xyz.strip().splitlines()[2:] if line.strip()]
    symbols = tuple(line.split()[0].capitalize() for line in lines)
    if not symbols or not all(symbol.isalpha() for symbol in symbols):
        return None
    return symbols


_FORMULA_TERM_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


def _formula_element_symbols(formula: Any) -> tuple[str, ...] | None:
    """Expand a plain ``formula`` (``H``, ``H2``, ``C2H5O``) to one symbol per atom.

    ``None`` for anything else (charges, parentheses, isotopes), like
    ``_xyz_element_symbols``.
    """
    if not isinstance(formula, str) or not formula:
        return None
    terms = _FORMULA_TERM_RE.findall(formula)
    if "".join(symbol + count for symbol, count in terms) != formula:
        return None
    symbols: list[str] = []
    for symbol, count in terms:
        if count.startswith("0"):
            return None
        symbols.extend([symbol] * int(count or 1))
    return tuple(symbols)


def _species_element_symbols(record: Mapping[str, Any]) -> tuple[str, ...] | None:
    """A species record's composition: its xyz, else its ``formula``.

    ARC before c8240195 (output.yml 1.0) writes ``xyz: null`` for monoatomic
    species, which skip opt, while still recording ``formula``.
    """
    symbols = _xyz_element_symbols(record.get("xyz"))
    if symbols is None:
        symbols = _formula_element_symbols(record.get("formula"))
    return symbols


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


def _formula(element_symbols: Any) -> str:
    counts: dict[str, int] = {}
    for symbol in element_symbols:
        counts[symbol] = counts.get(symbol, 0) + 1
    return "".join(f"{s}{n if n > 1 else ''}" for s, n in sorted(counts.items()))


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

    points = _build_thermo_points(thermo_record.get("thermo_points") or thermo_record.get("cp_data"))
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
    for role in (_CALC_KEY_OPT, _CALC_KEY_FREQ, _CALC_KEY_SP):
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
            warnings.append({
                "code": code,
                "message": message,
                "field": warning_field,
                "context": {"source": "tckdb_arc_self_check", "action": action,
                            **extra_context},
            })
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
            warnings.append({
                "code": _W_THERMO_REFERENCE_PRESSURE_NOT_STATED,
                "message": message,
                "field": warning_field,
                "context": {"source": "tckdb_arc_self_check",
                            "action": "reference_pressure_omitted",
                            "standard_state_pressure_pa": pressure_unstated},
            })
    return block if keep else None


def _build_nasa_block(
    nasa_low: Any, nasa_high: Any
) -> dict[str, Any] | None:
    """Map ARC's two NASA blocks to ``ThermoNASACreate``.

    Returns ``None`` if either block is missing or fails any of the
    structural checks. Per spec, malformed NASA must skip the NASA block
    only — scalar thermo and Cp points are kept by the caller.
    """
    if not isinstance(nasa_low, Mapping) or not isinstance(nasa_high, Mapping):
        return None
    low_coeffs = nasa_low.get("coeffs")
    high_coeffs = nasa_high.get("coeffs")
    if not isinstance(low_coeffs, list) or len(low_coeffs) != 7:
        logger.warning(
            "TCKDB thermo: NASA block skipped — nasa_low.coeffs must be a list of 7 floats."
        )
        return None
    if not isinstance(high_coeffs, list) or len(high_coeffs) != 7:
        logger.warning(
            "TCKDB thermo: NASA block skipped — nasa_high.coeffs must be a list of 7 floats."
        )
        return None
    t_low = nasa_low.get("tmin_k")
    t_mid_low = nasa_low.get("tmax_k")
    t_mid_high = nasa_high.get("tmin_k")
    t_high = nasa_high.get("tmax_k")
    if None in (t_low, t_mid_low, t_mid_high, t_high):
        logger.warning(
            "TCKDB thermo: NASA block skipped — temperature bounds incomplete."
        )
        return None
    try:
        t_low_f, t_mid_low_f, t_mid_high_f, t_high_f = (
            float(t_low), float(t_mid_low), float(t_mid_high), float(t_high)
        )
    except (TypeError, ValueError) as exc:
        logger.warning("TCKDB thermo: NASA block skipped — non-numeric bounds (%s).", exc)
        return None
    if t_mid_low_f != t_mid_high_f:
        logger.warning(
            "TCKDB thermo: NASA block skipped — nasa_low.tmax_k=%s != nasa_high.tmin_k=%s.",
            t_mid_low_f, t_mid_high_f,
        )
        return None
    try:
        low_floats = [float(c) for c in low_coeffs]
        high_floats = [float(c) for c in high_coeffs]
    except (TypeError, ValueError) as exc:
        logger.warning("TCKDB thermo: NASA block skipped — non-numeric coefficient (%s).", exc)
        return None
    block: dict[str, Any] = {
        "t_low": t_low_f,
        "t_mid": t_mid_low_f,
        "t_high": t_high_f,
    }
    for i, c in enumerate(low_floats, start=1):
        block[f"a{i}"] = c
    for i, c in enumerate(high_floats, start=1):
        block[f"b{i}"] = c
    return block


def _build_thermo_points(thermo_points: Any) -> list[dict[str, Any]]:
    """Map ARC's ``thermo_points`` list to ``ThermoPointCreate`` dicts.

    Each entry must carry ``temperature_k``; ``cp_j_mol_k``, ``h_kj_mol``,
    ``s_j_mol_k``, and ``g_kj_mol`` are optional and forwarded when present
    and numeric. Malformed individual points (missing/non-numeric
    temperature, or a per-quantity value that won't coerce) are dropped
    with a warning so a single bad row doesn't take out the whole
    thermo upload.
    """
    if not isinstance(thermo_points, list):
        return []
    seen_temps: set[float] = set()
    points: list[dict[str, Any]] = []
    for i, raw in enumerate(thermo_points):
        if not isinstance(raw, Mapping):
            logger.warning("TCKDB thermo: thermo_points[%d] skipped — not a mapping.", i)
            continue
        t = raw.get("temperature_k")
        if t is None:
            logger.warning("TCKDB thermo: thermo_points[%d] skipped — missing temperature_k.", i)
            continue
        try:
            t_f = float(t)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "TCKDB thermo: thermo_points[%d] skipped — non-numeric temperature_k=%r (%s).",
                i, t, exc,
            )
            continue
        if t_f <= 0:
            logger.warning(
                "TCKDB thermo: thermo_points[%d] skipped — temperature_k must be > 0 (got %s).",
                i, t_f,
            )
            continue
        if t_f in seen_temps:
            # Server enforces uniqueness by temperature_k; skip duplicates here.
            logger.warning(
                "TCKDB thermo: thermo_points[%d] skipped — duplicate temperature_k=%s.",
                i, t_f,
            )
            continue
        seen_temps.add(t_f)
        point: dict[str, Any] = {"temperature_k": t_f}
        for src_key, dst_key in (
            ("cp_j_mol_k", "cp_j_mol_k"),
            ("h_kj_mol", "h_kj_mol"),
            ("s_j_mol_k", "s_j_mol_k"),
            ("g_kj_mol", "g_kj_mol"),
        ):
            v = raw.get(src_key)
            if v is None:
                continue
            try:
                point[dst_key] = float(v)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "TCKDB thermo: thermo_points[%d].%s dropped — non-numeric %r (%s).",
                    i, src_key, v, exc,
                )
        points.append(point)
    return points


_KEY_PART_RE = re.compile(r"[^A-Za-z0-9]+")


def _safe_key_part(label: str | None) -> str:
    """Sanitize a label for use as part of a bundle local key.

    Bundle keys ride into ``GeometryIn.key`` / ``CalculationIn.key``
    where the schema only requires ``min_length=1``, but downstream
    consumers (and the idempotency-key sanitizer) prefer
    ``[A-Za-z0-9._:-]``. We keep alphanumerics, drop everything else,
    cap to 32 chars, and fall back to ``"x"`` for an all-junk label so
    we never produce an empty key segment.
    """
    if not label:
        return "x"
    cleaned = _KEY_PART_RE.sub("", str(label))[:32]
    return cleaned or "x"


def _local_key_for_actor(prefix: str, index: int, label: str | None) -> str:
    """Build a deterministic per-actor *species* key like ``"r0_CHO"`` / ``"p1_CH3"``.

    The numeric ``index`` is what guarantees uniqueness across actors
    that happen to share a chemical label (e.g. H + H ⇌ H2 has two
    ``r*_H`` slots). The sanitized label tail is purely informational —
    a human reading the JSON should be able to tell ``r0_CHO`` from
    ``r1_CH4`` without cross-referencing. Calc keys derive from
    :func:`_calc_prefix_for_actor` instead, which omits the label so
    calc keys stay short (e.g. ``r0_opt`` not ``r0_CHO_opt``).
    """
    return f"{prefix}{index}_{_safe_key_part(label)}"


def _calc_prefix_for_actor(prefix: str, index: int) -> str:
    """Build the per-actor calc-key prefix (e.g. ``"r0"`` / ``"p1"``).

    The chemical label is intentionally omitted: calculation keys ride
    into ``kinetics.source_calculations`` and are referenced verbatim,
    so shorter is better as long as uniqueness is preserved (the
    role-letter + index combo guarantees it).
    """
    return f"{prefix}{index}"


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
)


def _build_freq_scale_factor_ref(
    output_doc: Mapping[str, Any],
    *,
    workflow_tool_release: Mapping[str, Any] | None,
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
# ``app/db/models/common.py``). ARC's ``_statmech_to_dict`` currently
# only ever emits ``atom`` / ``linear`` / ``asymmetric_top``; the other
# two are listed for forward compatibility if ARC adds the analysis.
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
    }),
    "BundleStatmechIn": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
    }),
}


_W_TORSION_SCAN_NOT_BUILT = "torsion_scan_not_built"


_W_STATMECH_TREATMENT_NOT_STATED = "statmech_treatment_not_stated"


def _build_statmech_block_for_species(
    *,
    output_doc: Mapping[str, Any],
    species_record: Mapping[str, Any] | None = None,
    calc_keys_by_role: Mapping[str, str],
    workflow_tool_release: Mapping[str, Any] | None,
    target_model: Literal["StatmechInBundle", "BundleStatmechIn"],
    scan_key_renames: Mapping[str, str] | None = None,
    unbuilt_scans: Mapping[str, str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "statmech",
    freq_hessian_available: bool = False,
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
        slim_torsions = _build_slim_torsions(
            torsions_input, scan_key_renames=scan_key_renames,
            omitted_scan_keys=set(omitted_scan_keys), dropped_links=dropped_links,
        )
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
                warnings.append({
                    "code": _W_TORSION_SCAN_NOT_BUILT,
                    "message": message,
                    "field": warning_field,
                    "context": {"source": "tckdb_arc_self_check",
                                "action": "torsion_scan_link_omitted",
                                "scan_key": scan_key, "reason": reason},
                })
        treatment = _classify_statmech_treatment(
            torsions_input, emitted_torsions=slim_torsions,
        )
        # ``rrho`` (an empty rotor list) does not depend on the Hessian: Arkane
        # runs plain RRHO with no rotors whether or not it has a force-constant
        # matrix. Only rotor-aware treatments, and each torsion's own
        # ``treatment_kind`` ('hindered_rotor'), claim what Arkane did with the
        # rotors, and it discards them all without a Hessian.
        no_hessian = not freq_hessian_available
        withhold_treatment = (
            treatment is not None and treatment != "rrho" and no_hessian
        )
        strip_torsion_kinds = no_hessian and bool(slim_torsions)
        if treatment is not None and not withhold_treatment:
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
                warnings.append({
                    "code": _W_STATMECH_TREATMENT_NOT_STATED,
                    "message": message,
                    "field": f"{warning_field}.statmech_treatment",
                    "context": {"source": "tckdb_arc_self_check",
                                "action": "statmech_treatment_omitted",
                                "reason": "no_freq_hessian",
                                "inferred_treatment": treatment,
                                "omitted": omitted,
                                "torsion_count": len(slim_torsions)},
                })

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
) -> list[dict[str, Any]]:
    """Build ``BundleStatmechTorsionIn`` entries, with coordinate quartets when available.

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
        if treatment not in _ARC_TO_TCKDB_TORSION_TREATMENTS:
            continue
        slim: dict[str, Any] = {
            "torsion_index": position,
            "treatment_kind": treatment,
        }
        sym = entry.get("symmetry_number")
        if isinstance(sym, int) and sym >= 1:
            slim["symmetry_number"] = sym

        coordinates = _coerce_torsion_coordinates(entry.get("atom_indices"))
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


# Result-shape adapter: ``CalculationInBundle`` (computed-species)
# wraps results in nested ``opt_result``/``freq_result``/``sp_result``
# dicts; the network_pdep ``CalculationIn`` (which the computed-reaction
# endpoint extends) carries the same data as flat fields. The mapping
# below is the full v0 translation; ``_calculation_payload`` produces
# the wrapped shape, and :func:`_flatten_result_fields` rewrites it to
# the flat shape in place when building reaction bundles.
_REACTION_FLAT_RESULT_FIELDS: dict[str, dict[str, str]] = {
    "opt_result": {
        "converged": "opt_converged",
        "n_steps": "opt_n_steps",
        "final_energy_hartree": "opt_final_energy_hartree",
    },
    "freq_result": {
        "n_imag": "freq_n_imag",
        "imag_freq_cm1": "freq_imag_freq_cm1",
        "zpe_hartree": "freq_zpe_hartree",
        "reaction_coordinate_mode_index": "freq_reaction_coordinate_mode_index",
    },
    "sp_result": {
        "electronic_energy_hartree": "sp_electronic_energy_hartree",
    },
}

# Wrapped-result top-level fields with no 1:1 entry in
# ``_REACTION_FLAT_RESULT_FIELDS`` above but that ARE carried onto the
# reaction route by dedicated code further down in
# :func:`_flatten_result_fields`, rather than the generic src->dst copy
# loop. ``freq_result.modes`` is the only one today: it fans out into
# both ``freq_frequencies_cm1`` and ``freq_imaginary_dispositions``.
_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE: dict[str, frozenset[str]] = {
    "freq_result": frozenset({"modes"}),
}

# Wrapped-result top-level fields that are deliberately NOT carried onto
# the reaction route's flat shape, keyed by wrapped_key -> {field:
# reason}. Empty today: every top-level field ``OptResultPayload`` /
# ``FreqResultPayload`` / ``SPResultPayload`` can carry already has a
# mapping above or a dedicated handler. Kept as a real (checked) table
# rather than omitted so a future field that genuinely can't be carried
# has an explicit place to land, with a reason, instead of the
# completeness guard below being silenced by deleting its check.
_REACTION_RESULT_FIELDS_NOT_CARRIED: dict[str, dict[str, str]] = {}

# Per-mode (``FrequencyModePayload``) fields consumed while building
# ``freq_frequencies_cm1`` / ``freq_imaginary_dispositions`` above.
# ``mode_index`` and ``is_imaginary`` are read as indexing/control data
# and not re-emitted under their own name (``mode_index`` becomes the
# mode's position in ``freq_frequencies_cm1`` and its key in
# ``freq_imaginary_dispositions``; ``is_imaginary`` is implied on the
# flat side by the sign of ``frequency_cm1`` — see
# ``shared/calculation_in.py::freq_result_of``).
_REACTION_MODE_FIELDS_HANDLED = frozenset({
    "mode_index", "frequency_cm1", "is_imaginary", "imaginary_disposition",
})

# ``FrequencyModePayload`` fields with no home at all on the reaction
# route: ``CalculationIn.freq_frequencies_cm1`` is a bare ``list[float]``
# and ``freq_imaginary_dispositions`` is a bare ``{mode_index:
# disposition}`` map (``shared/calculation_in.py``) — neither has a slot
# for per-mode metadata beyond frequency and imaginary disposition. This
# is a structural limit of the flat reaction shape itself, not an
# adapter oversight: ``shared/calculation_in.py::freq_result_of``
# reconstructs ``FrequencyModePayload`` from the flat fields using only
# ``mode_index``/``frequency_cm1``/``is_imaginary``/
# ``imaginary_disposition``, so nothing downstream could receive these
# even if this adapter tried to carry them. ``_freq_result_payload``
# does not currently populate any of them either, so nothing is lost in
# practice today; they are listed here (with the schema-level reason)
# so the completeness guard has a real, checked table to point at
# instead of an unexamined "everything else is fine".
_REACTION_MODE_FIELDS_NOT_CARRIED: dict[str, str] = {
    "reduced_mass_amu": "no flat CalculationIn slot for per-mode reduced mass",
    "force_constant_mdyne_angstrom": "no flat CalculationIn slot for per-mode force constant",
    "ir_intensity_km_mol": "no flat CalculationIn slot for per-mode IR intensity",
    "raman_activity": "no flat CalculationIn slot for per-mode Raman activity",
    "symmetry_label": "no flat CalculationIn slot for per-mode symmetry label",
    "note": "no flat CalculationIn slot for per-mode free-text notes",
}


def _flatten_result_fields(calc: dict[str, Any]) -> None:
    """Convert wrapped ``opt_result``/``freq_result``/``sp_result`` into flat fields.

    Mutates ``calc`` in place: each wrapped result dict is removed and
    its values are promoted to the network_pdep-style flat field names.
    A no-op for IRC and any other calc type without a wrapped result —
    those carry their data through ``parameters_json`` instead.

    Completeness guard: any key inside a wrapped result (or inside one
    of ``freq_result``'s ``modes`` entries) that is not accounted for by
    ``_REACTION_FLAT_RESULT_FIELDS``, ``_REACTION_RESULT_FIELDS_
    HANDLED_ELSEWHERE``/``_REACTION_MODE_FIELDS_HANDLED``, or the
    corresponding "not carried" table raises ``ValueError`` instead of
    being silently dropped. Before this guard existed, any such key
    (e.g. a new field a species-shape builder starts emitting without
    updating this module) would vanish here with no error and no test
    failure — the reaction upload would simply lack data the species
    upload has, discovered only by a human diffing two uploads of the
    "same" calculation.
    """
    for wrapped_key, field_map in _REACTION_FLAT_RESULT_FIELDS.items():
        result = calc.pop(wrapped_key, None)
        if not isinstance(result, Mapping):
            continue
        handled_elsewhere = _REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE.get(
            wrapped_key, frozenset()
        )
        not_carried = _REACTION_RESULT_FIELDS_NOT_CARRIED.get(wrapped_key, {})
        unknown = set(result) - set(field_map) - handled_elsewhere - set(not_carried)
        if unknown:
            raise ValueError(
                f"{wrapped_key} carries field(s) {sorted(unknown)!r} with no "
                f"reaction-route flat-field mapping, explicit handler, or "
                f"documented 'not carried' justification. Update "
                f"_REACTION_FLAT_RESULT_FIELDS, "
                f"_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE, or "
                f"_REACTION_RESULT_FIELDS_NOT_CARRIED in adapter.py before "
                f"this field can silently vanish on the computed-reaction "
                f"route."
            )
        for src, dst in field_map.items():
            if src in result:
                calc[dst] = result[src]
        if wrapped_key == "freq_result":
            modes = result.get("modes")
            if modes:
                for m in modes:
                    mode_unknown = (
                        set(m)
                        - _REACTION_MODE_FIELDS_HANDLED
                        - set(_REACTION_MODE_FIELDS_NOT_CARRIED)
                    )
                    if mode_unknown:
                        raise ValueError(
                            f"freq_result.modes carries field(s) "
                            f"{sorted(mode_unknown)!r} with no reaction-route "
                            f"handling or documented 'not carried' "
                            f"justification. Update "
                            f"_REACTION_MODE_FIELDS_HANDLED or "
                            f"_REACTION_MODE_FIELDS_NOT_CARRIED in "
                            f"adapter.py before this field can silently "
                            f"vanish on the computed-reaction route."
                        )
                calc["freq_frequencies_cm1"] = [m["frequency_cm1"] for m in modes]
                # ``modes``' per-entry ``imaginary_disposition`` (set by
                # ``_freq_result_payload`` on every non-designated
                # imaginary mode once a reaction coordinate is
                # designated) has no home on the flat per-mode list —
                # the network_pdep/computed-reaction shape carries it
                # separately, keyed by mode_index, via
                # ``CalculationIn.freq_imaginary_dispositions``
                # (shared/calculation_in.py). Drop it and it silently
                # reverts to "undeclared", which is exactly the state
                # stationary_point.py's ambiguity check blocks on.
                dispositions = {
                    m["mode_index"]: m["imaginary_disposition"]
                    for m in modes
                    if m.get("imaginary_disposition") is not None
                }
                if dispositions:
                    calc["freq_imaginary_dispositions"] = dispositions


def _flatten_all_reaction_calcs(bundle: dict[str, Any]) -> None:
    """Walk a computed-reaction bundle and flatten every calc in place.

    Hits each species block's primary opt (under ``conformers[*].calculation``)
    and additionals (under ``calculations``), then the TS's primary
    (``transition_state.calculation``) and additionals
    (``transition_state.calculations``). One pass at the bundle root
    keeps the per-builder code free of shape concerns.
    """
    for species in bundle.get("species") or []:
        for conf in species.get("conformers") or []:
            calc = conf.get("calculation")
            if isinstance(calc, dict):
                _flatten_result_fields(calc)
        for calc in species.get("calculations") or []:
            if isinstance(calc, dict):
                _flatten_result_fields(calc)
    ts = bundle.get("transition_state")
    if isinstance(ts, dict):
        primary = ts.get("calculation")
        if isinstance(primary, dict):
            _flatten_result_fields(primary)
        for calc in ts.get("calculations") or []:
            if isinstance(calc, dict):
                _flatten_result_fields(calc)


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


def _build_kinetics_block(
    *,
    kinetics_record: Mapping[str, Any],
    reactant_keys: list[str],
    product_keys: list[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    ts_calc_keys: Mapping[str, str],
    long_kinetic_description: str | None = None,
) -> dict[str, Any] | None:
    """Build a ``BundleKineticsIn``-shaped dict from ARC kinetics.

    Mapping (ARC → TCKDB):
        A, T0_k, n  → a = A / T0_k**n (RMG uses (T/T0)**n)
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

    # RMG/Arkane fits A * (T/T0)**n; TCKDB evaluates a * T**n.
    # Older exports omitted T0 entirely and used the 1 K convention: their
    # rates come from Arkane's ``Arrhenius().fit_to_data(...)``
    # (RMG-Py arkane/kinetics.py), called without T0, whose default is
    # T0=1 K (rmgpy/kinetics/arrhenius.pyx ``fit_to_data(..., double T0=1)``,
    # stored as ``self.T0 = (T0, "K")``; ``Arrhenius.__init__`` also defaults
    # T0=(1.0, "K")). Current output always writes ``T0_k``. Kept by
    # maintainer decision (adapter 0.6.0) for pre-contract output only.
    # An explicit null/invalid T0 is unknown, not evidence for 1 K.
    if "a" in block:
        try:
            t0 = float(kinetics_record.get("T0_k", 1.0))
            if not math.isfinite(t0) or t0 <= 0:
                raise ValueError("T0_k must be finite and positive")
            if t0 != 1.0:
                exponent = block.get("n")
                if exponent is None or not math.isfinite(exponent):
                    raise ValueError("non-unit T0_k requires a finite n")
                normalized_a = block["a"] / t0**exponent
                if not math.isfinite(normalized_a) or (normalized_a == 0 and block["a"] != 0):
                    raise ValueError("normalized A is not representable")
                block["a"] = normalized_a
        except (TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
            logger.warning(
                "TCKDB kinetics: cannot normalize A with T0_k=%r, n=%r (%s); "
                "omitting a/a_units.", kinetics_record.get("T0_k"), n, exc,
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

    # Reaction-path degeneracy. TCKDB's ``BundleKineticsIn.degeneracy``
    # is ``float | None`` with ``gt=0``; the server rejects zero and
    # negative values. We mirror that constraint locally so the
    # producer never ships a value the server would 422 — and so the
    # absence of degeneracy on the wire stays distinct from a "zero"
    # value (the schema treats missing as NULL, *not* 1.0, and the
    # producer must too: don't default to 1, don't infer from
    # stoichiometry). Non-numeric / None / non-positive → omit.
    #
    # This field is guarded, not unreachable: ``output.yml``'s JSON
    # schema (output_yml_schema.json) sets ``additionalProperties: false``
    # on ``kinetics``, but that schema is never enforced at runtime — the
    # adapter ``yaml.load``s the file directly (``_vendor.py``) and only
    # checks ``schema_version`` (``evidence.py::validate_output_schema``),
    # and ARC itself doesn't validate against it on write either (it's
    # referenced only from ``arc/output_schema_test.py``). A hand-written,
    # older, or third-party ``output.yml`` can carry a ``degeneracy`` key
    # this branch reads. ``degeneracy > 0`` rejects NaN and -inf (both
    # compare False against 0), but ``float('inf') > 0`` is True, so
    # ``math.isfinite`` closes that one remaining hole.
    degeneracy = _coerce_optional_float(kinetics_record.get("degeneracy"))
    if degeneracy is not None and math.isfinite(degeneracy) and degeneracy > 0:
        block["degeneracy"] = degeneracy

    # Free-form note. Prefer an explicit ``note`` on the kinetics record
    # (future-proofing) over the reaction-level
    # ``long_kinetic_description`` ARC carries today. Both routes feed
    # the same TCKDB ``KineticsCreate.note`` slot.
    note_candidates = (
        kinetics_record.get("note"),
        long_kinetic_description,
    )
    for candidate in note_candidates:
        if isinstance(candidate, str) and candidate.strip():
            block["note"] = candidate.strip()
            break

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
    for actor_key in reactant_keys:
        sp_key = actor_calc_keys.get(actor_key, {}).get(_CALC_KEY_SP)
        if sp_key:
            links.append({"calculation_key": sp_key, "role": _KINETICS_ROLE_REACTANT_ENERGY})
    for actor_key in product_keys:
        sp_key = actor_calc_keys.get(actor_key, {}).get(_CALC_KEY_SP)
        if sp_key:
            links.append({"calculation_key": sp_key, "role": _KINETICS_ROLE_PRODUCT_ENERGY})
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


_W_CONFORMER_LEVEL_NOT_STATED = "conformer_level_not_stated"


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
        warnings.append({
            "code": _W_CONFORMER_LEVEL_NOT_STATED,
            "message": message,
            "field": "conformers",
            "context": {"source": "tckdb_arc_self_check",
                        "action": "screened_conformers_omitted",
                        "omitted_count": str(omitted)},
        })


_W_TS_GUESS_LEVEL_NOT_STATED = "ts_guess_level_not_stated"


_W_TS_GUESS_SOFTWARE_NOT_STATED = "ts_guess_software_not_stated"


def _warn_ts_guess_level_not_stated(
    warnings: list[dict[str, Any]] | None,
    *,
    ts_label: Any,
    method: str,
    reason: str | None = None,
    software: bool = False,
) -> None:
    code = _W_TS_GUESS_SOFTWARE_NOT_STATED if software else _W_TS_GUESS_LEVEL_NOT_STATED
    if reason is not None:
        why = reason if software else f"could not build it from ARC's exported NEB level ({reason})"
    elif method == "neb":
        why = "ARC did not export neb_level for this run"
    else:
        why = (
            f"ARC exports no level for the {method.upper()} path search "
            "(only the ORCA NEB level, neb_level, is exported)"
        )
    message = (
        f"The {method.upper()} path-search calculation of the TS guess for "
        f"{ts_label!r} was not uploaded: {why}. TCKDB requires a level of "
        "theory and a program on every calculation and the adapter does not "
        "file the path search at opt_level or under a deduced program. ARC "
        "should export the TS-guess level (BRIDGE_ROADMAP B3)."
    )
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append({
            "code": code,
            "message": message,
            "field": "transition_state.path_search.level_of_theory",
            "context": {"source": "tckdb_arc_self_check",
                        "action": "path_search_calculation_omitted",
                        "method": method},
        })


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

_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE: Mapping[str, str] = {
    "opt": "opt_level_adaptive_not_attributable",
    "freq": "freq_level_adaptive_not_attributable",
    "sp": "sp_level_adaptive_not_attributable",
    "scan": "scan_level_adaptive_not_attributable",
    "irc": "irc_level_adaptive_not_attributable",
}
_W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE = "enthalpy_adaptive_levels_unverifiable"


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
            warnings.append({
                "code": code,
                "message": message,
                "field": f"{kind}.level_of_theory",
                "context": {"source": "tckdb_arc_self_check",
                            "action": f"{kind}_calculation_omitted",
                            "adaptive_levels_sources": ",".join(marker["sources"]),
                            "labels": ",".join(sorted(labels))},
            })


_W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION = "ts_irc_evidence_without_irc_calculation"


def _ts_irc_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    irc_calc_key: str | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
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
    ``arc/checks/nmd.py``), never from the IRC. The participant mappings are
    not exported by ARC and are omitted.
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
            warnings.append({
                "code": _W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION,
                "message": message,
                "field": "transition_state.validation_evidence",
                "context": {"source": "tckdb_arc_self_check",
                            "action": "validation_evidence_omitted",
                            "ts_checks_irc": str(verdict).lower()},
            })
        return []
    rationale = f"ARC ts_checks['IRC'] = {verdict}"
    return [{
        "kind": "irc",
        "passed": verdict,
        "rationale": rationale,
        "source_calculation_key": irc_calc_key,
    }]


_W_IRC_DIRECTION_NOT_STATED = "irc_direction_not_stated"

# ARC runs every IRC job at ``irc_level`` (arc/scheduler.py ``run_irc_job``).
# restart.yml records it whenever it differs from the settings default, and
# the adapter then uses it exactly. When it is absent the level is that
# default, ``default_levels_of_theory['irc']`` (arc/settings/settings.py), which
# the adapter cannot read from the project files, and the adapter *assumes* it
# equals ``opt_level`` (true when ``opt_level`` is that same default, the
# maintainer's adapter-0.6.0 decision). That assumption is reported on the IRC
# calculation, until ARC exports the IRC level (BRIDGE_ROADMAP B3).
_W_IRC_LEVEL_ASSUMED_OPT_LEVEL = "irc_level_assumed_opt_level"


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
        warnings.append({
            "code": _W_IRC_LEVEL_ASSUMED_OPT_LEVEL,
            "message": message,
            "field": "transition_state.irc.level_of_theory",
            "context": {"source": "tckdb_arc_self_check",
                        "action": "level_of_theory_assumed"},
        })


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
            warnings.append({
                "code": _W_IRC_DIRECTION_NOT_STATED,
                "message": message,
                "field": warning_field,
                "context": {"source": "tckdb_arc_self_check",
                            "action": "irc_result_omitted"},
            })
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
        if isinstance(sp_level, Mapping) else None
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
    return text


def _summarize_response_body(body: Any, *, max_chars: int = 2000) -> Any:
    """Truncate huge bodies so the sidecar stays small and grep-friendly."""
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    text = str(body)
    if len(text) > max_chars:
        return text[:max_chars] + "...<truncated>"
    return text


_PUBLIC_REF_ECHO_KEYS = frozenset({"request", "requests", "payload", "payloads"})


def _extract_tckdb_public_refs(obj: Any) -> dict[str, list[str]]:
    """Collect returned TCKDB ``*_ref`` strings from a nested response body.

    The helper stores all returned refs it sees outside obvious echoed
    request/payload blocks. Values are grouped by pluralized key name and
    deduped in first-seen order, e.g. ``species_entry_ref`` becomes
    ``species_entry_refs``.
    """
    refs: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    def bucket_for(key: str) -> str:
        return key[:-4] + "_refs"

    def add(key: str, value: str) -> None:
        bucket = bucket_for(key)
        bucket_seen = seen.setdefault(bucket, set())
        if value in bucket_seen:
            return
        bucket_seen.add(value)
        refs.setdefault(bucket, []).append(value)

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_str = str(key)
                if key_str in _PUBLIC_REF_ECHO_KEYS:
                    continue
                if key_str.endswith("_ref") and isinstance(child, str):
                    add(key_str, child)
                    continue
                if child is None:
                    continue
                walk(child)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return refs


def _extract_calc_refs(
    response_data: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pull primary/additional CalculationUploadRef dicts from a server response.

    Two response shapes are recognized:

    1. **Conformer upload** (``/uploads/conformers``): ``primary_calculation``
       and ``additional_calculations`` sit at the top level::

           {"primary_calculation": {"calculation_id": ..., "type": ...},
            "additional_calculations": [...]}

    2. **Computed-species bundle** (``/uploads/computed-species``): the
       same fields are nested under ``conformers[0]``::

           {"species_entry_id": ..., "conformers": [
              {"key": "...", "primary_calculation": {...},
               "additional_calculations": [...]}, ...]}

       Only the first conformer's refs are surfaced — today's ARC bundles
       carry exactly one conformer per species, so multi-conformer
       responses would need a richer outcome shape, which we'd add when
       the producer side starts emitting them.

    Older server builds omit these fields entirely; the caller treats
    their absence as "no artifact targets known".
    """
    if not isinstance(response_data, Mapping):
        return None, []
    if "conformers" in response_data and isinstance(response_data["conformers"], list):
        confs = response_data["conformers"]
        if confs and isinstance(confs[0], Mapping):
            return _extract_calc_refs(confs[0])
        return None, []
    primary = response_data.get("primary_calculation")
    if not isinstance(primary, Mapping):
        primary = None
    else:
        primary = dict(primary)
    additional_raw = response_data.get("additional_calculations") or []
    additional = [dict(ref) for ref in additional_raw if isinstance(ref, Mapping)]
    return primary, additional


def _skip(
    calculation_id: int, kind: str, reason: str
) -> "ArtifactUploadOutcome":
    """Build a skipped outcome and log the reason once."""
    logger.info(
        "TCKDB artifact upload skipped: calc=%s kind=%s reason=%s",
        calculation_id, kind, reason,
    )
    return ArtifactUploadOutcome(
        status="skipped",
        sidecar_path=None,
        idempotency_key=None,
        calculation_id=calculation_id,
        kind=kind,
        skip_reason=reason,
    )


def _close_quietly(client: Any, context: str) -> None:
    """Close a TCKDB client and swallow close errors with a debug log."""
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # pragma: no cover - close errors swallowed
        logger.debug("TCKDB client close errored %s", context, exc_info=True)


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
