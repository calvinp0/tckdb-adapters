"""The kinetics energy-level declaration mirrors TCKDB's per-participant spin rule (tckdb-schemas 0.161).

TCKDB: ``backend/app/services/calculation_levels.py::kinetics_energy_level_violation`` (TCKDB
``cd84fb6a``, #870). Closed-shell links never constrain spin; the open-shell links share one
reference, which a declaration names; a mixed open-shell reference is refused by TCKDB, so nothing
is declared here and the caller is told.
"""

import pytest

from tckdb_core import rules

B3 = {"method": "b3lyp", "basis": "def2tzvp"}
U = {**B3, "spin_treatment": "unrestricted"}
R = {**B3, "spin_treatment": "restricted"}
RO = {**B3, "spin_treatment": "restricted_open"}

# r3_04: R1 + R2 -> P1 + P2 through TS0, multiplicities 2, 2, 1, 3 and TS 3.
LINKS = [{"calculation_key": "r0_sp", "role": "reactant_energy"},
         {"calculation_key": "r1_sp", "role": "reactant_energy"},
         {"calculation_key": "p0_sp", "role": "product_energy"},
         {"calculation_key": "p1_sp", "role": "product_energy"},
         {"calculation_key": "ts_sp", "role": "ts_energy"},
         {"calculation_key": "ts_freq", "role": "freq"}]
MULT = {"r0_sp": 2, "r1_sp": 2, "p0_sp": 1, "p1_sp": 3, "ts_sp": 3, "ts_freq": 3}
STATED = [B3] * 5


def _calcs(levels, s_squared=None):
    out = {}
    for key, level in levels.items():
        out[key] = {"key": key, "level_of_theory": level}
        if s_squared and key in s_squared:
            out[key]["spin_diagnostic"] = {"s_squared": s_squared[key]}
    return out


def _r3_04(p0=R, **override):
    levels = {"r0_sp": U, "r1_sp": U, "p0_sp": p0, "p1_sp": U, "ts_sp": U, "ts_freq": B3}
    levels.update(override)
    return levels


def _assess(levels, *, mult=MULT, stated=STATED, s_squared=None, links=LINKS):
    return rules.kinetics_energy_level_assessment(
        links, calculations=_calcs(levels, s_squared), stated_levels=stated, multiplicities=mult)


def test_r3_04_declares_the_open_shell_reference_with_a_restricted_singlet_exempt():
    assert _assess(_r3_04()).level == U
    assert rules.kinetics_energy_level_declaration(
        LINKS, calculations=_calcs(_r3_04()), stated_levels=STATED, multiplicities=MULT) == U


def test_all_closed_shell_restricted_declares_restricted():
    levels = {k: R for k in ("r0_sp", "r1_sp", "p0_sp", "p1_sp", "ts_sp")}
    assert _assess(levels, mult={k: 1 for k in levels}).level == R


def test_all_closed_shell_with_disagreeing_spin_declares_without_spin():
    levels = {k: R for k in ("r0_sp", "r1_sp", "p0_sp", "p1_sp")} | {"ts_sp": RO}
    assessment = _assess(levels, mult={k: 1 for k in levels})
    assert assessment.level == B3 and assessment.mixed_spin_calculations == ()


def test_open_shell_restricted_open_is_declared_as_the_reference():
    levels = _r3_04(r0_sp=RO, r1_sp=RO, p1_sp=RO, ts_sp=RO)
    assert _assess(levels).level == RO


def test_open_shell_unrestricted_against_restricted_open_declares_nothing_and_reports():
    assessment = _assess(_r3_04(r0_sp=RO))
    assert assessment.level is None
    assert assessment.mixed_spin_calculations == ("p1_sp", "r0_sp", "r1_sp", "ts_sp")


def test_the_mixed_report_needs_the_rest_of_the_declaration_to_hold():
    assert _assess(_r3_04(r0_sp=RO), stated=[B3, B3, B3, B3, None]).mixed_spin_calculations == ()


def test_s_squared_bound_decides_whether_an_unrestricted_singlet_is_open_shell():
    # Every radical restricted-open; the unrestricted singlet is the only thing that can mix them.
    base = _r3_04(p0_sp=U, r0_sp=RO, r1_sp=RO, p1_sp=RO, ts_sp=RO)
    collapsed = _assess(base, s_squared={"p0_sp": 0.0})
    assert collapsed.level == RO and collapsed.mixed_spin_calculations == ()
    at_bound = _assess(base, s_squared={"p0_sp": 0.1})
    assert at_bound.level == RO
    contaminated = _assess(base, s_squared={"p0_sp": 0.76})
    assert contaminated.level is None and "p0_sp" in contaminated.mixed_spin_calculations
    # No <S**2> stated: TCKDB counts it open-shell, and so does the rule (nothing is invented).
    unstated = _assess(base)
    assert unstated.level is None and "p0_sp" in unstated.mixed_spin_calculations


def test_a_restricted_singlet_never_mixes_whatever_its_s_squared():
    assert _assess(_r3_04(), s_squared={"p0_sp": 0.76}).level == U


def test_open_shell_links_of_unknown_spin_declare_without_spin():
    unknown = {k: B3 for k in ("r0_sp", "r1_sp", "p1_sp", "ts_sp")}
    assert _assess(_r3_04(**unknown)).level == B3


def test_an_unknown_spin_open_shell_link_is_skipped_not_mixed():
    assert _assess(_r3_04(r0_sp=B3)).level == U


@pytest.mark.parametrize("other", [{"method": "b3lyp", "basis": "def2svp"}, {"method": "m06-2x", "basis": "def2tzvp"},
                                   {**B3, "dispersion": "d3bj"}, {**B3, "core_treatment": "frozen_core"}])
def test_another_identity_field_among_the_links_declares_nothing(other):
    assessment = _assess(_r3_04(ts_sp={**other, "spin_treatment": "unrestricted"}))
    assert assessment.level is None and assessment.mixed_spin_calculations == ()


def test_an_unstated_participant_level_declares_nothing():
    assert _assess(_r3_04(), stated=[B3, B3, None, B3, B3]).level is None
    assert _assess(_r3_04(), stated=[]).level is None
    assert _assess(_r3_04(), stated=[B3, B3, {"method": "b3lyp", "basis": "def2svp"}, B3, B3]).level is None


def test_a_link_without_a_stated_multiplicity_declares_nothing():
    assert _assess(_r3_04(), mult={k: v for k, v in MULT.items() if k != "p0_sp"}).level is None


def test_a_link_without_a_calculation_or_level_declares_nothing():
    assert _assess({k: v for k, v in _r3_04().items() if k != "ts_sp"}).level is None
    assert _assess(_r3_04(ts_sp=None)).level is None


def test_the_returned_level_is_a_copy():
    levels = _r3_04()
    declared = _assess(levels).level
    declared["basis"] = "x"
    assert levels["r0_sp"]["basis"] == "def2tzvp"
