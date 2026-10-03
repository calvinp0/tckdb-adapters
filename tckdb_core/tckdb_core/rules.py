"""TCKDB rules a producer must satisfy, replicated as pre-checks.

Each function here mirrors a rule the TCKDB server applies to an upload (named in its
docstring), so a producer can decide *before* posting what the server would accept,
refuse or flag. They take the producer's facts as arguments (an already-converted
level, a list of imaginary frequencies, the calculations it built); reading those facts
out of the producer's own output is the producer's job.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tckdb_schemas.stationary_point import TAU_PROTOCOL_NOT_RECORDED_CM1

from tckdb_core._logging import resolve_log

# ---------------------------------------------------------------------------
# Energy-level declaration (assert_role_consistency)
# ---------------------------------------------------------------------------

def energy_level_declaration(
    stated: Mapping[str, Any] | None,
    *,
    energy_level_description: str,
    calc_keys_by_role: Mapping[str, str],
    calculations: Mapping[str, Mapping[str, Any]],
    producer_name: str,
    role_order: Sequence[str] = ("sp", "opt"),
    log=None,
) -> tuple[str, dict[str, Any]] | None:
    """Return ``(role, level)`` to declare as ``energy_level_of_theory``, or ``None``.

    TCKDB checks a declared energy level against the calculations the record
    links (``app.services.calculation_levels.assert_role_consistency``): the
    linked sp's level when there is one, else every linked opt's. Levels are
    compared as resolved rows, and the row identity hashes every field of
    ``LevelOfTheoryRef`` including ``spin_treatment``, where NULL folds to
    ``"unknown"`` (``app.services.calculation_resolution._level_of_theory_hash``).
    A producer stamps ``spin_treatment`` on the sp's level from its SCF reference, so the
    bare level it ran at is a different row and a declaration built from it is refused
    (``thermo_energy_level_contradiction`` / ``statmech_energy_level_contradiction``).

    The declaration is therefore the energy calculation's own ``level_of_theory``
    as sent, ``spin_treatment`` included, and only when the producer's stated energy level
    (``stated``, already converted to the ``LevelOfTheoryRef`` shape) is that calculation's
    level apart from ``spin_treatment``. Nothing is declared when ``stated`` is ``None``
    (the energy level is not stated, e.g. adaptive runs it cannot attribute), when no sp
    (or, without one, no opt) is linked (``role_order`` names the roles tried, first
    match wins), or when ``stated`` is another level than the linked calculation's (a
    composite method, which is not sent as a calculation, or an ``sp_level`` whose sp
    was not built): TCKDB would refuse it or have nothing to check it against.

    ``energy_level_description`` and ``producer_name`` only word the log line.
    """
    if stated is None:
        return None
    role = next((r for r in role_order if r in calc_keys_by_role), None)
    if role is None:
        return None
    calc = calculations.get(calc_keys_by_role[role])
    level = calc.get("level_of_theory") if isinstance(calc, Mapping) else None
    if not isinstance(level, Mapping):
        return None
    if {k: v for k, v in level.items() if k != "spin_treatment"} != stated:
        resolve_log(log).info(
            "TCKDB energy level not declared: %s's energy level %s is not the "
            "level of the linked %s calculation.", producer_name, energy_level_description, role)
        return None
    return role, dict(level)


def calculations_by_key(*groups: Any) -> dict[str, Mapping[str, Any]]:
    """Map each built calculation dict's bundle-local ``key`` to the dict."""
    out: dict[str, Mapping[str, Any]] = {}
    for group in groups:
        for calc in ([group] if isinstance(group, Mapping) else group or ()):
            if isinstance(calc, Mapping) and isinstance(calc.get("key"), str):
                out[calc["key"]] = calc
    return out


def describe_level(level: Any) -> str:
    if not isinstance(level, Mapping) or not level.get("method"):
        return "an unrecorded level"
    method = str(level["method"])
    if level.get("dispersion"):
        method += f" + {level['dispersion']}"
    return "/".join([method, *([str(level["basis"])] if level.get("basis") else [])])




# ---------------------------------------------------------------------------
# Transition-state reaction coordinate (ADR 0012, the tau rule)
# ---------------------------------------------------------------------------

def designate_reaction_coordinate_index(
    imaginary_values: list[float],
    *,
    window_cm1: tuple[float, float],
) -> int | None:
    """1-based position in ``imaginary_values`` of the unique major TS mode.

    Applies the producer's own window criterion (``window_cm1``, a ``(low, high)``
    pair; ARC's is (75, 10000), see ``arc/checks/ts.py::check_imaginary_frequencies``)
    instead of "most negative wins" over ALL modes: a candidate is any imaginary mode
    whose magnitude sits strictly inside the window. Restricting candidacy to the
    window is what keeps a parse/SCF artifact (e.g. -12000 cm-1, outside the window)
    from ever being asserted as the barrier over the real reaction coordinate (e.g.
    -1320.5 cm-1) sitting right where the producer itself would call it.

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
        if window_cm1[0] < abs(v) < window_cm1[1]
    ]
    if not candidates:
        return None
    max_magnitude = max(magnitude for _, magnitude in candidates)
    top = [i for i, magnitude in candidates if magnitude == max_magnitude]
    return top[0] if len(top) == 1 else None


# ``TAU_PROTOCOL_NOT_RECORDED_CM1`` (imported above, re-exported here) is TCKDB's noise
# floor for an imaginary mode when no protocol is recorded: deliberately the
# finite-difference-from-gradients value, since assuming the better case would flag
# modes that are only noise.


def designate_by_tau(
    imaginary_modes: Sequence[tuple[int, float]],
    n_imag: int,
    *,
    tau_cm1: float = TAU_PROTOCOL_NOT_RECORDED_CM1,
) -> tuple[int | None, int]:
    """Designate the reaction coordinate by TCKDB's noise floor.

    ``imaginary_modes`` is ``[(mode position, frequency_cm1), ...]`` for the imaginary
    modes found; ``n_imag`` is the count the producer states for the calculation.
    With exactly one imaginary mode at or above ``tau_cm1`` in magnitude (and every
    other below it), the others are numerical noise and the one real mode is the
    reaction coordinate. Two or more at or above tau is a genuine higher-order saddle
    TCKDB blocks without a designation, and a count that disagrees with ``n_imag`` is
    malformed data: both are undecidable.

    Returns ``(position or None, number of modes at or above tau)``; callers must not
    guess when the position is ``None``.
    """
    above = [i for i, frequency in imaginary_modes if abs(frequency) >= tau_cm1]
    if len(imaginary_modes) == n_imag and len(above) == 1:
        return above[0], 1
    return None, len(above)
