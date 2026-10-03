"""Thermo numerics a producer would compute identically: NASA-7 and Cp/H/S/G points.

The functions read already-extracted mappings (``tmin_k``/``tmax_k``/``coeffs`` per
NASA block, ``temperature_k`` and the optional ``cp_j_mol_k``/``h_kj_mol``/
``s_j_mol_k``/``g_kj_mol`` per point), so they hold no producer key lookup beyond
those neutral names. A malformed NASA block is skipped as a block, and a malformed
point is dropped as a point, with a log line; the caller keeps the scalar thermo.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tckdb_core._logging import resolve_log

#: Molar gas constant (J mol^-1 K^-1) the NASA enthalpy evaluation uses.
GAS_CONSTANT_J_MOL_K = 8.314462618
#: The reference temperature (K) of a standard enthalpy of formation.
T298_K = 298.15


def nasa_h298_kj_mol(nasa: Mapping[str, float]) -> float:
    """Evaluate a NASA-7 block's H at 298.15 K (kJ/mol)."""
    prefix = "a" if T298_K <= nasa["t_mid"] else "b"
    c = [nasa[f"{prefix}{i}"] for i in range(1, 8)]
    t = T298_K
    h_rt = c[0] + c[1] * t / 2 + c[2] * t**2 / 3 + c[3] * t**3 / 4 + c[4] * t**4 / 5 + c[5] / t
    return h_rt * GAS_CONSTANT_J_MOL_K * t / 1000.0


def build_nasa_block(
    nasa_low: Any, nasa_high: Any, *, log=None
) -> dict[str, Any] | None:
    """Map a producer's two NASA blocks to ``ThermoNASACreate``.

    Returns ``None`` if either block is missing or fails any of the
    structural checks. Per spec, malformed NASA must skip the NASA block
    only — scalar thermo and Cp points are kept by the caller. Skips are logged to
    ``log``, else the package logger.
    """
    log = resolve_log(log)
    if not isinstance(nasa_low, Mapping) or not isinstance(nasa_high, Mapping):
        return None
    low_coeffs = nasa_low.get("coeffs")
    high_coeffs = nasa_high.get("coeffs")
    if not isinstance(low_coeffs, list) or len(low_coeffs) != 7:
        log.warning(
            "TCKDB thermo: NASA block skipped — nasa_low.coeffs must be a list of 7 floats."
        )
        return None
    if not isinstance(high_coeffs, list) or len(high_coeffs) != 7:
        log.warning(
            "TCKDB thermo: NASA block skipped — nasa_high.coeffs must be a list of 7 floats."
        )
        return None
    t_low = nasa_low.get("tmin_k")
    t_mid_low = nasa_low.get("tmax_k")
    t_mid_high = nasa_high.get("tmin_k")
    t_high = nasa_high.get("tmax_k")
    if None in (t_low, t_mid_low, t_mid_high, t_high):
        log.warning(
            "TCKDB thermo: NASA block skipped — temperature bounds incomplete."
        )
        return None
    try:
        t_low_f, t_mid_low_f, t_mid_high_f, t_high_f = (
            float(t_low), float(t_mid_low), float(t_mid_high), float(t_high)
        )
    except (TypeError, ValueError) as exc:
        log.warning("TCKDB thermo: NASA block skipped — non-numeric bounds (%s).", exc)
        return None
    if t_mid_low_f != t_mid_high_f:
        log.warning(
            "TCKDB thermo: NASA block skipped — nasa_low.tmax_k=%s != nasa_high.tmin_k=%s.",
            t_mid_low_f, t_mid_high_f,
        )
        return None
    try:
        low_floats = [float(c) for c in low_coeffs]
        high_floats = [float(c) for c in high_coeffs]
    except (TypeError, ValueError) as exc:
        log.warning("TCKDB thermo: NASA block skipped — non-numeric coefficient (%s).", exc)
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


def build_thermo_points(thermo_points: Any, *, log=None) -> list[dict[str, Any]]:
    """Map a producer's ``thermo_points`` list to ``ThermoPointCreate`` dicts.

    Each entry must carry ``temperature_k``; ``cp_j_mol_k``, ``h_kj_mol``,
    ``s_j_mol_k``, and ``g_kj_mol`` are optional and forwarded when present
    and numeric. Malformed individual points (missing/non-numeric
    temperature, or a per-quantity value that won't coerce) are dropped
    with a warning so a single bad row doesn't take out the whole
    thermo upload. Drops are logged to ``log``, else the package logger.
    """
    log = resolve_log(log)
    if not isinstance(thermo_points, list):
        return []
    seen_temps: set[float] = set()
    points: list[dict[str, Any]] = []
    for i, raw in enumerate(thermo_points):
        if not isinstance(raw, Mapping):
            log.warning("TCKDB thermo: thermo_points[%d] skipped — not a mapping.", i)
            continue
        t = raw.get("temperature_k")
        if t is None:
            log.warning("TCKDB thermo: thermo_points[%d] skipped — missing temperature_k.", i)
            continue
        try:
            t_f = float(t)
        except (TypeError, ValueError) as exc:
            log.warning(
                "TCKDB thermo: thermo_points[%d] skipped — non-numeric temperature_k=%r (%s).",
                i, t, exc,
            )
            continue
        if t_f <= 0:
            log.warning(
                "TCKDB thermo: thermo_points[%d] skipped — temperature_k must be > 0 (got %s).",
                i, t_f,
            )
            continue
        if t_f in seen_temps:
            # Server enforces uniqueness by temperature_k; skip duplicates here.
            log.warning(
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
                log.warning(
                    "TCKDB thermo: thermo_points[%d].%s dropped — non-numeric %r (%s).",
                    i, src_key, v, exc,
                )
        points.append(point)
    return points
