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


# ---------------------------------------------------------------------------
# Kinetics energy-level declaration (assert_kinetics_energy_level_consistency)
# ---------------------------------------------------------------------------

_KINETICS_ENERGY_ROLES = frozenset({"reactant_energy", "product_energy", "ts_energy"})


def kinetics_energy_level_declaration(
    source_calculations: Sequence[Mapping[str, Any]],
    *,
    calculations: Mapping[str, Mapping[str, Any]],
    stated_levels: Sequence[Mapping[str, Any] | None],
) -> dict[str, Any] | None:
    """Return the level to declare as a kinetics block's ``energy_level_of_theory``, or ``None``.

    TCKDB (tckdb-schemas 0.99, ``assert_kinetics_energy_level_consistency``) holds a
    declared kinetics energy level against every ``reactant_energy`` / ``product_energy`` /
    ``ts_energy`` calculation the record links, as resolved level rows: one linked energy
    at another level, ``spin_treatment`` included, is ``kinetics_energy_level_contradiction``.
    A record that declares none draws the ``missing_level_of_theory_provenance`` warning.

    One declaration has to hold for every linked energy at once, so a level is declared only
    when (a) the record links at least one energy calculation and each has a level, (b) they
    all carry the same level exactly as sent, and (c) the producer states the energy level of
    every participant behind them (``stated_levels``, each already in the ``LevelOfTheoryRef``
    shape, one per participant the links name) and that level is the linked calculation's
    apart from ``spin_treatment``. Otherwise nothing is declared: a level is never taken from a
    run-wide default, and a reaction whose participants ran at different levels (or at one level
    with different spin treatments) has no single level to state.
    """
    levels: list[Mapping[str, Any]] = []
    for link in source_calculations:
        if link.get("role") not in _KINETICS_ENERGY_ROLES:
            continue
        calc = calculations.get(link.get("calculation_key"))
        level = calc.get("level_of_theory") if isinstance(calc, Mapping) else None
        if not isinstance(level, Mapping):
            return None
        levels.append(level)
    if not levels or any(level != levels[0] for level in levels):
        return None
    bare = {k: v for k, v in levels[0].items() if k != "spin_treatment"}
    if not stated_levels or any(stated != bare for stated in stated_levels):
        return None
    return dict(levels[0])


# ---------------------------------------------------------------------------
# Geometry / conformer agreement (tckdb-schemas 0.121, #635)
# ---------------------------------------------------------------------------

_INPUT_DECLARING_TYPES = frozenset({"freq", "sp"})


def _xyz_tokens(xyz_text: Any) -> list[str] | None:
    return str(xyz_text).split() if isinstance(xyz_text, str) and xyz_text.strip() else None


def _calc_view(calc: Mapping[str, Any]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        key=calc.get("key"), geometry_key=calc.get("geometry_key"), conformer_key=calc.get("conformer_key"))


def geometry_anchor_refusals(payload: Mapping[str, Any], route: str) -> list[tuple[str, str]]:
    """``(code, detail)`` for each geometry/conformer disagreement TCKDB would refuse in ``payload``.

    Runs the request-level rules tckdb-schemas 0.121 ships in ``tckdb_schemas.bundle_source_rules``
    on a built ``computed_reaction`` payload, so the producer learns of a refusal before posting:

    * a species calculation whose ``conformer_key`` and ``geometry_key`` name different
      conformers, and a conformer's own calculation whose keys point at another conformer
      (``calculation_geometry_conformer_mismatch``, ``conformer_key_undeclared``,
      ``geometry_key_unresolved``);
    * a transition-state calculation with a ``conformer_key``, or whose ``geometry_key`` is a
      species conformer's geometry (``transition_state_calculation_conformer_key``,
      ``transition_state_calculation_species_geometry``).

    ``route`` is ``"computed_reaction"``; the other routes have no keys to compare (a computed-species
    conformer owns its calculations by nesting) and return ``[]``.
    """
    if route != "computed_reaction":
        return []
    from tckdb_schemas.bundle_source_rules import (
        assert_calculation_geometry_matches_conformer,
        assert_conformer_primaries_agree,
        assert_transition_state_calculations_unanchored,
    )
    from tckdb_schemas.coded_error import CodedValidationError
    from types import SimpleNamespace

    problems: list[tuple[str, str]] = []

    def run(check) -> None:
        try:
            check()
        except CodedValidationError as exc:  # ``code`` and the message are the refusal; any other error is a crash
            problems.append((str(exc.code), str(exc)))

    species_geometry_keys: list[str] = []
    for species in payload.get("species") or ():
        conformers = [
            SimpleNamespace(
                key=conf.get("key"),
                geometry=SimpleNamespace(key=(conf.get("geometry") or {}).get("key")),
                calculation=_calc_view(conf.get("calculation") or {}),
            )
            for conf in species.get("conformers") or ()
        ]
        species_geometry_keys.extend(c.geometry.key for c in conformers if c.geometry.key)
        run(lambda conformers=conformers: assert_conformer_primaries_agree(conformers))
        owners = {c.geometry.key: c.key for c in conformers if c.geometry.key}
        run(lambda species=species, owners=owners: assert_calculation_geometry_matches_conformer(
            [_calc_view(c) for c in species.get("calculations") or ()], owners))
    ts = payload.get("transition_state")
    if isinstance(ts, Mapping):
        run(lambda: assert_transition_state_calculations_unanchored(
            [_calc_view(c) for c in ts.get("calculations") or ()], species_geometry_keys))
    return problems


def undeclarable_input_geometries(payload: Mapping[str, Any], route: str) -> list[tuple[Mapping[str, Any], str]]:
    """The ``freq``/``sp`` calculations whose declared input geometry is not their own owner's geometry.

    TCKDB (0.121, ``calculation_input_geometry_mismatch``, raised by the workflow where geometry
    rows are known, so no shipped schema model can check it) refuses a ``freq`` or ``sp`` that
    states its geometry and also declares ``input_geometries`` that do not include that geometry,
    compared as the same structure (rounding, a rigid move and atom reordering forgiven;
    coordinates a program re-printed or symmetrised are another structure). The owner is the
    conformer a computed-species calculation sits in, the conformer a computed-reaction species
    calculation's ``geometry_key`` names, the transition state's own geometry for a transition
    state's calculations, and the saddle point for a standalone transition-state upload.

    This replica accepts only the textual identity (the same atoms and coordinates, whitespace
    aside), which is what a producer feeding a job the conformer's own geometry sends. It is
    stricter than the server, which compares by ``geometries_are_one_structure`` and so forgives
    rounding, a rigid move and atom reordering that this replica would drop, and which on the
    computed-reaction route checks only the calculations that state a ``geometry_key`` (this
    replica also checks a conformer's own calculation and the transition state's). A declaration
    dropped here that the server would have accepted costs only the explicit statement: TCKDB then
    takes the owner's geometry. Returns ``[(calculation, field path), ...]`` for every
    calculation whose declared inputs fail it; the caller omits their ``input_geometries`` (TCKDB
    then takes the owner's geometry) and says so. An ``opt`` is exempt, as it is on the server.
    """
    found: list[tuple[Mapping[str, Any], str]] = []

    def check(calc: Any, owner_xyz: Any, field: str) -> None:
        if not isinstance(calc, Mapping) or calc.get("type") not in _INPUT_DECLARING_TYPES:
            return
        declared = calc.get("input_geometries")
        owner = _xyz_tokens(owner_xyz)
        if not declared or owner is None:
            return
        if not any(_xyz_tokens((entry or {}).get("xyz_text")) == owner for entry in declared):
            found.append((calc, field))

    if route == "computed_species":
        for ci, conf in enumerate(payload.get("conformers") or ()):
            xyz = (conf.get("geometry") or {}).get("xyz_text")
            check(conf.get("primary_calculation"), xyz, f"conformers[{ci}].primary_calculation")
            for ai, calc in enumerate(conf.get("additional_calculations") or ()):
                check(calc, xyz, f"conformers[{ci}].additional_calculations[{ai}]")
    elif route == "computed_reaction":
        for si, species in enumerate(payload.get("species") or ()):
            xyz_by_geometry_key: dict[str, Any] = {}
            for ci, conf in enumerate(species.get("conformers") or ()):
                geometry = conf.get("geometry") or {}
                if geometry.get("key"):
                    xyz_by_geometry_key[geometry["key"]] = geometry.get("xyz_text")
                check(conf.get("calculation"), geometry.get("xyz_text"),
                      f"species[{si}].conformers[{ci}].calculation")
            for ai, calc in enumerate(species.get("calculations") or ()):
                if isinstance(calc, Mapping) and calc.get("geometry_key") in xyz_by_geometry_key:
                    check(calc, xyz_by_geometry_key[calc["geometry_key"]], f"species[{si}].calculations[{ai}]")
        ts = payload.get("transition_state")
        if isinstance(ts, Mapping):
            xyz = (ts.get("geometry") or {}).get("xyz_text")
            for ai, calc in enumerate(ts.get("calculations") or ()):
                check(calc, xyz, f"transition_state.calculations[{ai}]")
    elif route == "transition_state":
        xyz = (payload.get("geometry") or {}).get("xyz_text")
        check(payload.get("primary_opt"), xyz, "primary_opt")
        for ai, calc in enumerate(payload.get("additional_calculations") or ()):
            check(calc, xyz, f"additional_calculations[{ai}]")
    return found
