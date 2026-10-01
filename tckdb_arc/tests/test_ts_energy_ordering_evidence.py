"""``energy_ordering`` validation evidence from ARC's ``ts_checks['e_elect']`` (tckdb-schemas 0.64).

ARC's check (``arc/checks/ts.py::check_rxn_e_elect``): the TS electronic energy is above both wells by more
than 1 kJ/mol, wells summed over their participants with the stoichiometric multipliers. It leaves
``ts_checks['e_elect']`` unset whenever the ``E0`` check passed. What the adapter sends, and what it refuses
to send:

* the electronic energies only (each participant's ``sp_energy_hartree``, cited to that participant's own
  ``sp`` calculation, ``reactant:N`` / ``product:N`` in the declared order, a repeated species repeated);
  never ``e0`` (ARC's ``e0_kj_mol`` carries corrections and is not one calculation's energy);
* ``passed`` is ARC's ``e_elect`` verdict when it is a bool, otherwise nothing is sent (``passed`` is
  required);
* a ``True`` verdict the stated hartree numbers contradict (ARC's 1 kJ/mol margin) is not sent and is
  reported: TCKDB refuses such a pass and the whole upload would 422;
* a participant without a stated, finite, non-positive ``sp_energy_hartree`` or without an ``sp`` calculation
  leaves the record out (a passing record needs every participant);
* the bundle only: the standalone TS upload refuses the kind.
"""

import copy
from pathlib import Path

import pytest
import yaml

from _contract import assert_fragment_matches_contract, contract_validate
from test_adapter import _reaction_output_doc
from test_reaction_repeated_participants import _doc as _repeated_doc
from test_reaction_repeated_participants import _reaction as _repeated_reaction
from test_reaction_repeated_participants import _standalone as _repeated_standalone
from tckdb_schemas.fragments.ts_validation_evidence import TransitionStateValidationEvidenceIn
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

from test_arc_1_3_reactions import _adapter
from tckdb_arc.adapter import TCKDBAdapter, _ts_energy_ordering_validation_evidence
from tckdb_arc.config import TCKDBConfig

GOLDEN = Path(__file__).parent / "fixtures" / "golden"

CODE = "ts_energy_ordering_evidence_not_sent"
HARTREE_TO_KJ = 2625.4996394799

# test_adapter's CHO + CH4 <=> CH2O + CH3: reactants -113.7 + -40.5 = -154.2, products -114.5 + -39.8 = -154.3.
REACTANTS, PRODUCTS = -154.2, -154.3


def _doc(*, ts_energy=-154.0, e_elect=True, e0=None):
    doc = copy.deepcopy(_reaction_output_doc())
    ts = doc["transition_states"][0]
    ts["sp_energy_hartree"] = ts_energy
    ts["ts_checks"] = {"E0": e0, "e_elect": e_elect, "IRC": None, "freq": None, "NMD": None, "warnings": ""}
    return doc


def _reaction(doc):
    warnings = []
    payload = _adapter()._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0], warnings=warnings)
    return payload, warnings


def _standalone(doc):
    warnings = []
    payload = _adapter()._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0],
        warnings=warnings)
    return payload, warnings


def _ordering(payload):
    return [r for r in payload["transition_state"].get("validation_evidence", [])
            if r["kind"] == "energy_ordering"]


def _codes(warnings):
    return [w["code"] for w in warnings]


def _sp_keys(payload):
    """species key -> its sp calculation key"""
    return {s["key"]: next(c["key"] for c in s["calculations"] if c["type"] == "sp") for s in payload["species"]}


def test_a_passing_ordering_is_sent_with_every_participants_own_electronic_energy():
    payload, warnings = _reaction(_doc())
    [record] = _ordering(payload)
    sp = _sp_keys(payload)
    assert payload["reactant_keys"] == ["r0_CHO", "r1_CH4"] and payload["product_keys"] == ["p0_CH2O", "p1_CH3"]
    assert record["kind"] == "energy_ordering" and record["passed"] is True
    assert "source_calculation_key" not in record
    assert record["energies"] == [
        {"participant": "ts", "energy_kind": "electronic", "energy_hartree": -154.0, "source_calculation_key": "ts_sp"},
        {"participant": "reactant:1", "energy_kind": "electronic", "energy_hartree": -113.7,
         "source_calculation_key": sp["r0_CHO"]},
        {"participant": "reactant:2", "energy_kind": "electronic", "energy_hartree": -40.5,
         "source_calculation_key": sp["r1_CH4"]},
        {"participant": "product:1", "energy_kind": "electronic", "energy_hartree": -114.5,
         "source_calculation_key": sp["p0_CH2O"]},
        {"participant": "product:2", "energy_kind": "electronic", "energy_hartree": -39.8,
         "source_calculation_key": sp["p1_CH3"]},
    ]
    assert not any(e["energy_kind"] == "e0" for e in record["energies"])
    assert CODE not in _codes(warnings)
    TransitionStateValidationEvidenceIn.model_validate(record)
    assert_fragment_matches_contract(record, "TransitionStateValidationEvidenceIn")
    contract_validate(ComputedReactionUploadRequest, payload)


def test_arcs_e0_verdict_is_only_mentioned_in_the_rationale():
    payload, _ = _reaction(_doc(e0=False))
    [record] = _ordering(payload)
    assert "ts_checks['e_elect'] = True" in record["rationale"] and "ts_checks['E0'] = False" in record["rationale"]
    assert "e0" not in {e["energy_kind"] for e in record["energies"]} and "e0_kj_mol" not in str(record)


def test_the_standalone_route_never_carries_it_and_is_not_told_it_was_omitted():
    payload, warnings = _standalone(_doc())
    assert "validation_evidence" not in payload
    assert CODE not in _codes(warnings)
    contract_validate(TransitionStateUploadRequest, payload)


@pytest.mark.parametrize("checks", [
    pytest.param({"e_elect": None, "E0": True}, id="E0_passed_so_ARC_left_e_elect_unset"),
    pytest.param(None, id="no_ts_checks"),
])
def test_without_a_verdict_nothing_is_sent_and_nothing_is_reported(checks):
    doc = _doc()
    ts = doc["transition_states"][0]
    if checks is None:
        del ts["ts_checks"]
    else:
        ts["ts_checks"].update(checks)
    payload, warnings = _reaction(doc)
    assert _ordering(payload) == []
    assert CODE not in _codes(warnings)


def test_a_failed_verdict_is_sent_as_a_failure():
    """ARC's verdict is stated as stated; TCKDB does not refuse a failure whose numbers look fine."""
    payload, warnings = _reaction(_doc(ts_energy=-154.5, e_elect=False))     # TS below both wells
    [record] = _ordering(payload)
    assert record["passed"] is False
    assert CODE not in _codes(warnings)
    contract_validate(ComputedReactionUploadRequest, payload)


def test_a_false_verdict_the_stated_numbers_satisfy_is_omitted_as_stale():
    """ARC's verdict was computed on stale energies: the stated sp energies put the TS above both wells."""
    payload, warnings = _reaction(_doc(ts_energy=-154.0, e_elect=False))
    assert _ordering(payload) == []
    [warning] = [w for w in warnings if w["code"] == CODE]
    assert "failure is contradicted by its own numbers" in warning["message"]
    assert warning["context"]["ts_checks_e_elect"] == "false"
    assert warning["context"]["reason"] == "verdict_contradicted_by_stated_energies"
    assert warning["context"]["action"] == "validation_evidence_omitted"
    contract_validate(ComputedReactionUploadRequest, payload)


def test_a_false_verdict_just_inside_arcs_margin_is_sent_as_stated():
    payload, warnings = _reaction(_doc(ts_energy=REACTANTS + 0.5 / HARTREE_TO_KJ, e_elect=False))
    [record] = _ordering(payload)
    assert record["passed"] is False
    assert CODE not in _codes(warnings)


@pytest.mark.parametrize("ts_energy,reason", [
    pytest.param(-154.5, "reactant", id="below_both_wells"),
    pytest.param(-154.25, "reactant", id="between_the_wells_above_the_products_only"),
    pytest.param(REACTANTS + 0.5 / HARTREE_TO_KJ, "reactant", id="above_a_well_by_less_than_ARCs_margin"),
])
def test_a_true_verdict_the_stated_numbers_contradict_is_omitted_with_a_warning(ts_energy, reason):
    payload, warnings = _reaction(_doc(ts_energy=ts_energy))
    assert _ordering(payload) == []
    [warning] = [w for w in warnings if w["code"] == CODE]
    assert reason in warning["message"] and "contradicted by its own numbers" in warning["message"]
    assert warning["field"] == "transition_state.validation_evidence"
    assert warning["context"]["action"] == "validation_evidence_omitted"
    assert warning["context"]["ts_checks_e_elect"] == "true"
    contract_validate(ComputedReactionUploadRequest, payload)


def test_a_pass_just_above_arcs_margin_is_sent():
    payload, _ = _reaction(_doc(ts_energy=REACTANTS + 1.5 / HARTREE_TO_KJ))
    assert [r["passed"] for r in _ordering(payload)] == [True]


@pytest.mark.parametrize("species_or_ts,field,value,expected", [
    pytest.param("CH4", "sp_energy_hartree", None, "reactant:2", id="reactant_without_energy"),
    pytest.param("CH3", "sp_energy_hartree", None, "product:2", id="product_without_energy"),
    pytest.param("TS0", "sp_energy_hartree", None, "ts", id="ts_without_energy"),
    pytest.param("CH4", "sp_energy_hartree", 3.0, "reactant:2", id="positive_energy_is_not_absolute"),
])
def test_a_participant_without_a_usable_energy_leaves_the_record_out(species_or_ts, field, value, expected):
    doc = _doc()
    record = next(r for r in (*doc["species"], *doc["transition_states"]) if r["label"] == species_or_ts)
    record[field] = value
    payload, warnings = _reaction(doc)
    assert _ordering(payload) == []
    [warning] = [w for w in warnings if w["code"] == CODE]
    assert expected in warning["message"]
    # a failed verdict needs the energies too: they are required
    doc["transition_states"][0]["ts_checks"]["e_elect"] = False
    payload, warnings = _reaction(doc)
    assert _ordering(payload) == [] and CODE in _codes(warnings)


def test_a_participant_without_an_sp_calculation_leaves_the_record_out():
    warnings = []
    ts = {"ts_checks": {"e_elect": True}, "sp_energy_hartree": -1.0}
    species = {"A": {"sp_energy_hartree": -2.0}, "B": {"sp_energy_hartree": -2.0}}
    kwargs = dict(ts_sp_key="ts_sp", reactant_labels=["A"], product_labels=["B"], reactant_keys=["r0_A"],
                  product_keys=["p0_B"], species_index=species, ts_label="TS0", warnings=warnings)
    sent = _ts_energy_ordering_validation_evidence(
        ts, actor_calc_keys={"r0_A": {"sp": "r0_sp"}, "p0_B": {"sp": "p0_sp"}}, **kwargs)
    assert [e["source_calculation_key"] for e in sent[0]["energies"]] == ["ts_sp", "r0_sp", "p0_sp"] and warnings == []
    assert _ts_energy_ordering_validation_evidence(
        ts, actor_calc_keys={"r0_A": {"opt": "r0_opt"}, "p0_B": {"sp": "p0_sp"}}, **kwargs) == []
    assert _ts_energy_ordering_validation_evidence(
        ts, actor_calc_keys={"r0_A": {"sp": "r0_sp"}, "p0_B": {"sp": "p0_sp"}},
        **{**kwargs, "ts_sp_key": None}) == []
    assert [w["code"] for w in warnings] == [CODE, CODE]
    assert "no sp calculation" in warnings[0]["message"]


@pytest.mark.parametrize("source", ["atom_map_labels", "irc"])
def test_a_repeated_species_repeats_its_entry_and_its_energy_counts_twice(source):
    """HO2 + HO2 <=> H2O2 + O2, every species at -1.0: the reactants sum to -2.0.

    A TS at -1.5 is above both wells only if the repeat is summed twice (counted once, the reactant side
    would be -1.0 and the pass contradicted).
    """
    doc, record = _repeated_doc(**{"atom_map_labels": source == "atom_map_labels", "irc": source == "irc"})
    ts = doc["transition_states"][0]
    ts["sp_energy_hartree"] = -1.5
    ts["ts_checks"] = {"E0": None, "e_elect": True, "IRC": None, "freq": None, "NMD": None, "warnings": ""}
    warnings = []
    payload = _repeated_reaction(doc, record, warnings)
    [ordering] = _ordering(payload)
    assert payload["reactant_keys"] == ["r0_HO2", "r0_HO2"]
    assert [(e["participant"], e["energy_hartree"], e["source_calculation_key"]) for e in ordering["energies"]] == [
        ("ts", -1.5, "ts_sp"), ("reactant:1", -1.0, "r0_sp"), ("reactant:2", -1.0, "r0_sp"),
        ("product:1", -1.0, "p0_sp"), ("product:2", -1.0, "p1_sp")]
    contract_validate(ComputedReactionUploadRequest, payload)
    # the standalone route has no energy_ordering slot
    assert "validation_evidence" not in _repeated_standalone(doc, record, [])
    # below the doubled reactant side (-2.0): the verdict is contradicted, so nothing is sent
    doc["transition_states"][0]["sp_energy_hartree"] = -2.5
    warnings = []
    assert _ordering(_repeated_reaction(doc, record, warnings)) == []
    assert CODE in _codes(warnings)


def test_atoms_cite_their_sp_primary_and_a_species_on_both_sides_is_cited_from_both():
    """The golden H2 + H <=> H + H2: an atom's primary is its own single point (batch H), and the species
    blocks are shared between the sides, so reactant:2 and product:1 (H) name the one ``r1_sp``."""
    doc = yaml.safe_load((GOLDEN / "phase3_output.yml").read_text())
    ts = doc["transition_states"][0]
    ts["sp_energy_hartree"] = -1.5
    ts["ts_checks"] = {"E0": None, "e_elect": True, "IRC": None, "freq": None, "NMD": None, "warnings": ""}
    warnings = []
    payload = TCKDBAdapter(TCKDBConfig(enabled=True, base_url="http://x", upload=False)
                           )._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0], warnings=warnings)
    atom = next(s for s in payload["species"] if s["key"].endswith("_H"))
    assert atom["conformers"][0]["calculation"]["type"] == "sp"
    atom_sp = atom["conformers"][0]["calculation"]["key"]
    [record] = _ordering(payload)
    cited = {e["participant"]: e["source_calculation_key"] for e in record["energies"]}
    assert payload["reactant_keys"] == ["r0_H2", "r1_H"] and payload["product_keys"] == ["r1_H", "r0_H2"]
    assert cited["reactant:2"] == cited["product:1"] == atom_sp
    assert cited["reactant:1"] == cited["product:2"] and cited["reactant:1"] != atom_sp
    assert [e["energy_hartree"] for e in record["energies"]] == [-1.5, -1.17, -0.5, -0.5, -1.17]
    contract_validate(ComputedReactionUploadRequest, payload)


def test_a_non_finite_energy_is_not_an_energy():
    warnings = []
    kwargs = dict(ts_sp_key="ts_sp", reactant_labels=["A"], product_labels=["B"], reactant_keys=["r0_A"],
                  product_keys=["p0_B"], ts_label="TS0", warnings=warnings,
                  actor_calc_keys={"r0_A": {"sp": "r0_sp"}, "p0_B": {"sp": "p0_sp"}})
    for bad in (float("nan"), float("inf"), True, "n/a"):
        assert _ts_energy_ordering_validation_evidence(
            {"ts_checks": {"e_elect": True}, "sp_energy_hartree": -1.0},
            species_index={"A": {"sp_energy_hartree": bad}, "B": {"sp_energy_hartree": -2.0}}, **kwargs) == []
    assert [w["code"] for w in warnings] == [CODE] * 4
