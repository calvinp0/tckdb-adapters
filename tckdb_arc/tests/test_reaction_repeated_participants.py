"""A repeated species is not lost from the reaction (HO2 + HO2 <=> H2O2 + O2).

ARC's ``reactant_labels`` / ``product_labels`` are sorted and de-duplicated, so
the reaction would be uploaded as ``HO2 <=> H2O2 + O2``, which TCKDB refuses as
unbalanced. The occurrences come from ``atom_map_*_labels`` (output 1.3), else
from the TS's ``irc_participant_mapping``; with neither, the collapsed lists are
used when they balance and the reaction is refused
(``reaction_stoichiometry_not_stated``) when they do not.
"""

import copy

import pytest

from _contract import contract_validate
from test_adapter import _reaction_output_doc, _reaction_record
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig

XYZ = {
    "HO2": "H 0.0 0.0 0.0\nO 1.0 0.0 0.0\nO 2.0 0.0 0.0",
    "H2O2": "H 0.0 0.0 0.0\nH 0.0 1.0 0.0\nO 1.0 0.0 0.0\nO 2.0 0.0 0.0",
    "O2": "O 0.0 0.0 0.0\nO 1.2 0.0 0.0",
}
TS_XYZ = "H 0 0 0\nH 0 1 0\nO 1 0 0\nO 2 0 0\nO 3 0 0\nO 4 0 0"
SMILES = {"HO2": "[O]O", "H2O2": "OO", "O2": "[O][O]"}


def _spc(label):
    mult = 1 if label == "H2O2" else (2 if label == "HO2" else 3)
    return {
        "label": label, "smiles": SMILES[label], "charge": 0, "multiplicity": mult,
        "is_ts": False, "xyz": XYZ[label], "opt_n_steps": 3, "opt_final_energy_hartree": -1.0,
        "opt_converged": True, "freq_n_imag": 0, "zpe_hartree": 0.01, "sp_energy_hartree": -1.0,
        "ess_versions": {"opt": "Gaussian 16, Revision A.03"},
    }


def _side(participants):
    return {"endpoint": 1, "endpoint_label": None, "participants": [
        {"label": label, "position": i, "occurrence": occ, "atom_indices": atoms}
        for i, (label, occ, atoms) in enumerate(participants, start=1)]}


IRC = {
    "reactants": _side([("HO2", 1, [0, 2, 3]), ("HO2", 2, [1, 4, 5])]),
    "products": _side([("H2O2", 1, [0, 1, 2, 4]), ("O2", 1, [3, 5])]),
    "sides_distinguishable": True, "atom_order_matches_ts": True,
}


def _doc(*, atom_map_labels=False, irc=False):
    doc = _reaction_output_doc()
    doc["species"] = [_spc("HO2"), _spc("H2O2"), _spc("O2")]
    doc["transition_states"][0]["xyz"] = TS_XYZ
    doc["transition_states"][0]["multiplicity"] = 2
    if irc:
        doc["transition_states"][0]["irc_participant_mapping"] = copy.deepcopy(IRC)
    record = _reaction_record()
    record.update(label="HO2 + HO2 <=> H2O2 + O2", reactant_labels=["HO2"],
                  product_labels=["H2O2", "O2"], multiplicity=2)
    if atom_map_labels:
        record.update(atom_map_reactant_labels=["HO2", "HO2"], atom_map_product_labels=["H2O2", "O2"])
    doc["reactions"] = [record]
    return doc, record


def _reaction(doc, record, warnings):
    cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
    return TCKDBAdapter(cfg)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=record, warnings=warnings)


def _standalone(doc, record, warnings):
    cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
    return TCKDBAdapter(cfg)._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=record,
        warnings=warnings)


@pytest.mark.parametrize("source", ["atom_map_labels", "irc"])
def test_a_stated_repeat_gives_two_slots_and_one_species_block(source):
    doc, record = _doc(**{"atom_map_labels": source == "atom_map_labels", "irc": source == "irc"})
    payload = _reaction(doc, record, [])
    assert payload["reactant_keys"] == ["r0_HO2", "r0_HO2"]
    assert len(payload["reactant_keys"]) == 2 and payload["product_keys"] == ["p0_H2O2", "p1_O2"]
    assert [s["key"] for s in payload["species"]] == ["r0_HO2", "p0_H2O2", "p1_O2"]
    contract_validate(ComputedReactionUploadRequest, payload)
    # balanced: 2 HO2 = H2O4 = H2O2 + O2
    xyz = {s["key"]: s["conformers"][0]["geometry"]["xyz_text"] for s in payload["species"]}
    count = lambda keys: sorted(l.split()[0] for k in keys for l in xyz[k].splitlines()[2:])
    assert count(payload["reactant_keys"]) == count(payload["product_keys"])
    standalone = _standalone(doc, record, [])
    assert len(standalone["reaction"]["reactants"]) == 2


@pytest.mark.parametrize("build", [_reaction, _standalone])
def test_irc_mapping_numbers_each_occurrence_of_a_repeated_species(build):
    doc, record = _doc(atom_map_labels=True, irc=True)
    doc["transition_states"][0]["ts_checks"] = {"IRC": True, "warnings": ""}
    doc["transition_states"][0]["irc_logs"] = ["irc_f.log", "irc_r.log"]
    payload = build(doc, record, [])
    block = payload.get("transition_state", payload)
    (evidence,) = block["validation_evidence"]
    assert evidence["reactant_participant_mapping"] == {"reactant:1": [1, 3, 4], "reactant:2": [2, 5, 6]}
    assert evidence["product_participant_mapping"] == {"product:1": [1, 2, 3, 5], "product:2": [4, 6]}


@pytest.mark.parametrize("builder", [_reaction, _standalone])
def test_unbalanced_collapsed_lists_with_nothing_stated_are_refused(builder):
    doc, record = _doc()                              # <= 1.2: no atom_map labels, no IRC mapping
    warnings = []
    with pytest.raises(ValueError, match="not atom-balanced"):
        builder(doc, record, warnings)
    assert [w["code"] for w in warnings] == ["reaction_stoichiometry_not_stated"]


def test_a_balanced_reaction_without_repeats_is_unchanged():
    doc, record = _doc()
    record.update(reactant_labels=["H2O2"], product_labels=["H2O2"],
                  label="H2O2 <=> H2O2")
    doc["transition_states"][0]["xyz"] = XYZ["H2O2"]
    warnings = []
    payload = _reaction(doc, record, warnings)
    assert payload["reactant_keys"] == ["r0_H2O2"]
    assert "reaction_stoichiometry_not_stated" not in [w["code"] for w in warnings]


def test_the_reaction_label_string_is_never_parsed():
    doc, record = _doc()
    record["label"] = "HO2 + HO2 <=> H2O2 + O2"       # states the repeat, but is only a label
    with pytest.raises(ValueError, match="not atom-balanced"):
        _reaction(doc, record, [])
