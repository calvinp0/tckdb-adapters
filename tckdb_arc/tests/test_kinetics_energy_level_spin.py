"""A mixed-spin reaction declares its kinetics energy level with the open-shell reference (tckdb-schemas 0.161).

The shape is ``r3_04_h_abstraction`` (radicals computed unrestricted, a closed-shell species
computed restricted), built from the ``arc_1_3_reactions`` document (OH + CH4 <=> H2O + CH3, TS0):
OH, CH3 and TS0 are doublets with a stated ``<S**2>``, CH4 and H2O are singlets.
TCKDB judges spin per participant (``kinetics_energy_level_violation``); the adapter mirrors it in
``tckdb_core.rules.kinetics_energy_level_assessment`` and never invents a spin treatment or an
``<S**2>``.
"""

import copy

import pytest
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest

from _contract import contract_validate
from test_arc_1_3_reactions import _fixture, _reaction_payload

LEVEL = {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}
CODE = "kinetics_energy_level_spin_mixed"
RADICALS = ("OH", "CH3")
SINGLETS = ("CH4", "H2O")


def _doc(references, s_squared=None):
    """``references`` maps a label (TS0 included) to the sp reference ARC recorded for it."""
    doc = _fixture()
    for record in (*doc["species"], *doc["transition_states"]):
        label = record["label"]
        if label in references:
            record["scf_reference"] = {**(record.get("scf_reference") or {}), "sp_reference": references[label]}
        if s_squared is not None and label in s_squared:
            record["sp_spin_diagnostic"] = ({"s_squared": s_squared[label]}
                                            if s_squared[label] is not None else None)
    return doc


def _unrestricted_radicals(**extra):
    return {"OH": "unrestricted", "CH3": "unrestricted", "TS0": "unrestricted",
            "CH4": "restricted", "H2O": "restricted", **extra}


def _kinetics(payload):
    (block,) = payload["kinetics"]
    return block


def _sp_calc(payload, key):
    for species in payload["species"]:
        for calc in [species["conformers"][0]["calculation"], *species.get("calculations", ())]:
            if calc["key"] == key:
                return calc
    raise KeyError(key)


def test_unrestricted_radicals_and_restricted_singlets_declare_unrestricted():
    payload, warnings = _reaction_payload(_doc(_unrestricted_radicals()))
    assert _kinetics(payload)["energy_level_of_theory"] == {**LEVEL, "spin_treatment": "unrestricted"}
    assert CODE not in [w["code"] for w in warnings]
    contract_validate(ComputedReactionUploadRequest, payload)


def test_the_linked_energies_really_are_at_two_spin_treatments():
    payload, _ = _reaction_payload(_doc(_unrestricted_radicals()))
    spins = {link["calculation_key"]: None for link in _kinetics(payload)["source_calculations"]
             if link["role"] in ("reactant_energy", "product_energy")}
    for key in spins:
        spins[key] = _sp_calc(payload, key)["level_of_theory"].get("spin_treatment")
    assert set(spins.values()) == {"restricted", "unrestricted"}


def test_radicals_computed_restricted_throughout_declare_restricted():
    doc = _doc({label: "restricted" for label in ("OH", "CH3", "TS0", "CH4", "H2O")})
    payload, _ = _reaction_payload(doc)
    # Radicals computed restricted (an open-shell restricted run): the shared reference is still named.
    assert _kinetics(payload)["energy_level_of_theory"] == {**LEVEL, "spin_treatment": "restricted"}


def test_no_stated_references_declare_the_level_without_spin():
    payload, _ = _reaction_payload(_doc({}))
    assert _kinetics(payload)["energy_level_of_theory"] == LEVEL


def test_open_shell_references_that_differ_declare_nothing_and_say_so():
    payload, warnings = _reaction_payload(_doc(_unrestricted_radicals(OH="restricted_open")))
    assert "energy_level_of_theory" not in _kinetics(payload)
    (warning,) = [w for w in warnings if w["code"] == CODE]
    assert warning["field"] == "kinetics[0].energy_level_of_theory"
    assert set(warning["context"]["calculation_keys"]) == {"r1_sp", "p0_sp", "ts_sp"}
    contract_validate(ComputedReactionUploadRequest, payload)


@pytest.mark.parametrize("s_squared,declared_spin", [
    (0.0, "restricted_open"),   # collapsed to the restricted solution: closed-shell, exempt
    (0.76, None),               # contaminated: open-shell, so it mixes with the restricted-open radicals
    (None, None),               # ARC states none: TCKDB counts it open-shell, so does the adapter
])
def test_an_unrestricted_singlet_is_judged_by_its_stated_s_squared(s_squared, declared_spin):
    refs = {"OH": "restricted_open", "CH3": "restricted_open", "TS0": "restricted_open",
            "CH4": "unrestricted", "H2O": "restricted"}
    payload, warnings = _reaction_payload(_doc(refs, s_squared={"CH4": s_squared}))
    block = _kinetics(payload)
    if declared_spin:
        assert block["energy_level_of_theory"] == {**LEVEL, "spin_treatment": declared_spin}
        assert CODE not in [w["code"] for w in warnings]
    else:
        assert "energy_level_of_theory" not in block
        assert CODE in [w["code"] for w in warnings]


def test_the_stated_s_squared_of_an_unrestricted_singlet_reaches_its_sp_and_none_is_invented():
    refs = _unrestricted_radicals(CH4="unrestricted")
    payload, _ = _reaction_payload(_doc(refs, s_squared={"CH4": 0.0, "H2O": None}))
    methane = _sp_calc(payload, "r0_sp")
    assert methane["level_of_theory"]["spin_treatment"] == "unrestricted"
    assert methane["spin_diagnostic"]["s_squared"] == 0.0
    assert "spin_diagnostic" not in _sp_calc(payload, "p1_sp")      # H2O: ARC stated none
    contract_validate(ComputedReactionUploadRequest, payload)


def test_input_document_is_not_mutated_by_building():
    doc = _doc(_unrestricted_radicals())
    before = copy.deepcopy(doc)
    _reaction_payload(doc)
    assert doc == before
