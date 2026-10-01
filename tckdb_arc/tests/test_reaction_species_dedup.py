"""One species block per ARC species label in a computed-reaction bundle (A16).

A species on both sides (degenerate ``H2 + H <=> H + H2``) or twice on one side
(``H + H <=> H2``) is declared once and referenced from every slot: the schema
allows a repeated key in ``reactant_keys``/``product_keys`` and the server
resolves participants by position.
"""

import copy

from _contract import contract_validate
from test_adapter import _reaction_output_doc, _reaction_record
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig


def _species(label, smiles, mult, sp_e):
    return {
        "label": label, "smiles": smiles, "charge": 0, "multiplicity": mult,
        "is_ts": False,
        "xyz": "H 0.0 0.0 0.0" if label == "H" else "H 0.0 0.0 0.0\nH 0.74 0.0 0.0",
        "opt_n_steps": 3, "opt_final_energy_hartree": sp_e, "opt_converged": True,
        "freq_n_imag": 0, "zpe_hartree": 0.01, "sp_energy_hartree": sp_e,
        "ess_versions": {"opt": "Gaussian 16, Revision A.03"},
    }


def _build(reactants, products):
    doc = _reaction_output_doc()
    doc["species"] = [_species("H2", "[H][H]", 1, -1.17), _species("H", "[H]", 2, -0.5)]
    record = _reaction_record()
    record["reactant_labels"] = reactants
    record["product_labels"] = products
    doc["reactions"] = [record]
    cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
    return TCKDBAdapter(cfg)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=copy.deepcopy(record))


def _calc_keys(species):
    keys = [c["key"] for c in species["calculations"]]
    return keys + [species["conformers"][0]["calculation"]["key"]]


def _assert_kinetics_links_resolve(payload):
    all_calc = {k for sp in payload["species"] for k in _calc_keys(sp)}
    all_calc |= {c["key"] for c in payload["transition_state"]["calculations"]}
    all_calc.add(payload["transition_state"]["calculation"]["key"])
    links = payload["kinetics"][0]["source_calculations"]
    assert len({(l["calculation_key"], l["role"]) for l in links}) == len(links)
    assert all(l["calculation_key"] in all_calc for l in links)
    return links


def test_degenerate_reaction_declares_each_species_once():
    payload = _build(["H2", "H"], ["H", "H2"])
    assert [s["key"] for s in payload["species"]] == ["r0_H2", "r1_H"]
    assert payload["reactant_keys"] == ["r0_H2", "r1_H"]
    assert payload["product_keys"] == ["r1_H", "r0_H2"]
    assert payload["kinetics"][0]["reactant_keys"] == payload["reactant_keys"]
    assert payload["kinetics"][0]["product_keys"] == payload["product_keys"]
    # One sp calculation per species, however many slots reference it.
    sp_calcs = [c for s in payload["species"] for c in s["calculations"] if c["type"] == "sp"]
    assert len(sp_calcs) == 2
    links = _assert_kinetics_links_resolve(payload)
    by_role = {}
    for link in links:
        by_role.setdefault(link["role"], []).append(link["calculation_key"])
    # Reactant and product energy still point at each slot's own species' sp.
    assert by_role["reactant_energy"] == ["r0_sp", "r1_sp"]
    assert by_role["product_energy"] == ["r1_sp", "r0_sp"]
    contract_validate(ComputedReactionUploadRequest, payload)


def test_species_repeated_on_one_side_is_declared_once_with_one_link():
    payload = _build(["H", "H"], ["H2"])
    assert [s["key"] for s in payload["species"]] == ["r0_H", "p0_H2"]
    assert payload["reactant_keys"] == ["r0_H", "r0_H"]
    assert payload["product_keys"] == ["p0_H2"]
    links = _assert_kinetics_links_resolve(payload)
    # The schema refuses a repeated (calculation_key, role) pair.
    assert [l for l in links if l["role"] == "reactant_energy"] == [
        {"calculation_key": "r0_sp", "role": "reactant_energy"}]
    contract_validate(ComputedReactionUploadRequest, payload)


def test_non_degenerate_reaction_is_unchanged():
    doc = _reaction_output_doc()
    cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
    payload = TCKDBAdapter(cfg)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])
    assert [s["key"] for s in payload["species"]] == [
        "r0_CHO", "r1_CH4", "p0_CH2O", "p1_CH3"]
    assert payload["reactant_keys"] == ["r0_CHO", "r1_CH4"]
    assert payload["product_keys"] == ["p0_CH2O", "p1_CH3"]
    links = _assert_kinetics_links_resolve(payload)
    assert [(l["calculation_key"], l["role"]) for l in links[:4]] == [
        ("r0_sp", "reactant_energy"), ("r1_sp", "reactant_energy"),
        ("p0_sp", "product_energy"), ("p1_sp", "product_energy")]
