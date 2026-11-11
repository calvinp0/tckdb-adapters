"""A one-electron participant's Hartree-Fock energy under a correlated kinetics level.

Mirror of TCKDB's one-electron HF exemption in ``kinetics_energy_level_violation``: a link (and its
participant's stated level) may differ from the others in ``method`` alone when the participant has
exactly one electron, its method identifies as Hartree-Fock (``method_identity_key`` is ``hf``) and
every other identity field except ``spin_treatment`` equals the declared level's. Shape:
``rotor_scan_rxn_210``, ``CC + [H] <=> C[CH2] + [H][H]``, ORCA DLPNO-CCSD(T)-F12 for all but the H atom,
which ORCA can only run at HF.
"""

import pytest

from tckdb_core import composition, rules

F12 = {"method": "dlpno-ccsd(t)-f12", "basis": "cc-pvtz-f12", "aux_basis": "aug-cc-pvtz/c",
       "cabs_basis": "cc-pvtz-f12-cabs"}
HF = {**F12, "method": "hf"}
U = {**F12, "spin_treatment": "unrestricted"}
R = {**F12, "spin_treatment": "restricted"}
H_U = {**HF, "spin_treatment": "unrestricted"}

LINKS = [{"calculation_key": k, "role": r} for k, r in (
    ("r0_sp", "reactant_energy"), ("r1_sp", "reactant_energy"),
    ("p0_sp", "product_energy"), ("p1_sp", "product_energy"), ("ts_sp", "ts_energy"))]
MULT = {"r0_sp": 1, "r1_sp": 2, "p0_sp": 2, "p1_sp": 1, "ts_sp": 2}
ELECTRONS = {"r0_sp": 18, "r1_sp": 1, "p0_sp": 17, "p1_sp": 2, "ts_sp": 19}   # CC, H, C2H5, H2, TS
STATED_ELECTRONS = [18, 1, 17, 2, 19]


def _levels(r1=H_U, **override):
    levels = {"r0_sp": R, "r1_sp": r1, "p0_sp": U, "p1_sp": R, "ts_sp": U}
    levels.update(override)
    return levels


def _calcs(levels):
    return {k: {"key": k, "level_of_theory": v} for k, v in levels.items()}


def _stated(r1=HF):
    return [F12, r1, F12, F12, F12]


def _assess(levels=None, *, stated=None, electrons=ELECTRONS, stated_electrons=STATED_ELECTRONS, mult=MULT):
    return rules.kinetics_energy_level_assessment(
        LINKS, calculations=_calcs(levels or _levels()), stated_levels=stated or _stated(),
        multiplicities=mult, electron_counts=electrons, stated_electron_counts=stated_electrons)


def test_rxn_210_declares_the_correlated_level_and_reports_the_substituted_link():
    assessment = _assess()
    assert assessment.level == U            # never HF; the shared open-shell reference
    assert assessment.one_electron_hf_calculations == ("r1_sp",)
    assert assessment.mixed_spin_calculations == ()
    assert rules.kinetics_energy_level_declaration(
        LINKS, calculations=_calcs(_levels()), stated_levels=_stated(), multiplicities=MULT,
        electron_counts=ELECTRONS, stated_electron_counts=STATED_ELECTRONS) == U


def test_the_h_atom_link_comes_first_and_the_others_still_set_the_declared_level():
    order = [LINKS[1], *LINKS[:1], *LINKS[2:]]
    assessment = rules.kinetics_energy_level_assessment(
        order, calculations=_calcs(_levels()), stated_levels=_stated(), multiplicities=MULT,
        electron_counts=ELECTRONS, stated_electron_counts=STATED_ELECTRONS)
    assert assessment.level == U and assessment.one_electron_hf_calculations == ("r1_sp",)


def test_a_hydride_two_electrons_at_hf_declares_nothing():
    assert _assess(electrons={**ELECTRONS, "r1_sp": 2}, stated_electrons=[18, 2, 17, 2, 19]).level is None


def test_a_one_electron_cation_such_as_he_plus_is_exempt():
    assert _assess(electrons={**ELECTRONS, "r1_sp": 1}).one_electron_hf_calculations == ("r1_sp",)


def test_the_h_atom_at_hf_in_another_basis_declares_nothing():
    other = {**H_U, "basis": "cc-pvdz-f12"}
    assert _assess(_levels(r1=other), stated=_stated({**HF, "basis": "cc-pvdz-f12"})).level is None
    # the stated level alone in another basis
    assert _assess(stated=_stated({**HF, "basis": "cc-pvdz-f12"})).level is None


@pytest.mark.parametrize("field,value", [("aux_basis", "def2/c"), ("cabs_basis", "other-cabs"),
                                         ("dispersion", "d3bj"), ("solvent", "water"),
                                         ("core_treatment", "frozen_core")])
def test_any_other_identity_field_differing_declares_nothing(field, value):
    assert _assess(_levels(r1={**H_U, field: value})).level is None


def test_a_two_electron_participant_at_hf_declares_nothing():
    levels = _levels(p1_sp={**HF, "spin_treatment": "restricted"})      # H2 at HF
    assert _assess(levels).level is None


def test_the_stated_level_of_a_non_one_electron_participant_at_hf_declares_nothing():
    assert _assess(stated=[F12, HF, F12, HF, F12]).level is None        # H2's stated level at HF


def test_every_link_a_one_electron_hf_link_has_nothing_to_exempt():
    links = LINKS[1:2]
    assessment = rules.kinetics_energy_level_assessment(
        links, calculations=_calcs({"r1_sp": H_U}), stated_levels=[HF], multiplicities={"r1_sp": 2},
        electron_counts={"r1_sp": 1}, stated_electron_counts=[1])
    assert assessment.one_electron_hf_calculations == ()
    assert assessment.level == H_U       # today's behaviour: the links agree, so that level is the declaration


@pytest.mark.parametrize("electrons,stated_electrons", [
    (None, STATED_ELECTRONS),
    ({k: v for k, v in ELECTRONS.items() if k != "r1_sp"}, STATED_ELECTRONS),
    ({**ELECTRONS, "r1_sp": None}, STATED_ELECTRONS),
    (ELECTRONS, None),
    (ELECTRONS, [18, None, 17, 2, 19]),
])
def test_an_unknown_electron_count_grants_no_exemption(electrons, stated_electrons):
    assert _assess(electrons=electrons, stated_electrons=stated_electrons).level is None


@pytest.mark.parametrize("method,exempt", [("hf", True), ("HF", True), ("uhf", False), ("rhf", False),
                                           ("rohf", False), ("hartree-fock", False)])
def test_only_what_the_level_identity_folds_to_hf_is_exempt(method, exempt):
    """``method_identity_key`` folds case only: ``uhf`` / ``rhf`` / ``rohf`` / ``hartree-fock`` are other keys.

    ARC states the spin form in ``spin_treatment`` (``method: hf``), so the exemption keys on ``hf`` alone
    and does not invent spellings."""
    spelled = {**H_U, "method": method}
    assessment = _assess(_levels(r1=spelled), stated=_stated({**HF, "method": method}))
    assert (assessment.level == U) is exempt
    assert (assessment.level is None) is (not exempt)


def test_a_correlated_one_electron_link_is_not_reported_as_a_substitution():
    levels = _levels(r1={**F12, "spin_treatment": "unrestricted"})
    assessment = _assess(levels, stated=_stated(F12))
    assert assessment.level == U and assessment.one_electron_hf_calculations == ()


def test_spin_is_still_judged_per_participant_for_the_h_atom():
    # H (doublet) restricted-open against the unrestricted radicals: open-shell references that differ.
    assessment = _assess(_levels(r1={**HF, "spin_treatment": "restricted_open"}))
    assert assessment.level is None and "r1_sp" in assessment.mixed_spin_calculations


def test_a_composite_scheme_link_is_not_exempted():
    scheme = {"name": "x"}
    levels = _levels(r1={**H_U, "composite_scheme": scheme})
    assert _assess(levels).level is None


def test_electron_count_from_composition_and_charge():
    assert composition.electron_count(("H",), 0) == 1
    assert composition.electron_count(("H",), -1) == 2
    assert composition.electron_count(("He",), 1) == 1
    assert composition.electron_count(("H", "H"), 0) == 2
    assert composition.electron_count(("C", "H", "H", "H"), 0) == 9
    assert composition.electron_count(("H",), 0.0) == 1
    for charge in (None, True, "0", 0.5):
        assert composition.electron_count(("H",), charge) is None
    assert composition.electron_count(None, 0) is None
    assert composition.electron_count((), 0) is None
    assert composition.electron_count(("Xx",), 0) is None
