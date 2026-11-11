"""A free H atom run at HF under a DLPNO-CCSD(T)-F12 kinetics level (``rotor_scan_rxn_210`` shape).

ORCA cannot run a correlated method on one electron, so ARC runs the H atom of
``CC + [H] <=> C[CH2] + [H][H]`` at HF in the same basis, auxiliary basis and CABS basis as the rest
(``levels.sp.method: hf``, ``sp_route: uhf hf cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno``).
The adapter declares the correlated level the other links share, as TCKDB's one-electron HF
exemption accepts, and says so (``one_electron_hf_substitution``); see
``tckdb_core.rules.kinetics_energy_level_assessment``.

Built from the ``arc_1_3_levels`` reaction document with ORCA DLPNO-CCSD(T)-F12 sp levels
(``test_orca_route_aux_basis._orca_doc``): the reaction's second species stands in for the H atom
(relabelled ``H``), its first and last for the radicals.

NOTE: the installed tckdb-schemas 0.170 model does not know the server rule, but the payload shape is
unchanged, so the published model and JSON Schema validate it. A live TCKDB at the 0.170 pin WOULD
refuse this payload (``kinetics_energy_level_contradiction``: the H link's method differs); the rule lands
server-side in a later TCKDB revision, and the live gate corpus follows when the pin moves.
"""

import copy
from pathlib import Path

import pytest
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest

from _contract import contract_validate
from test_orca_route_aux_basis import ORCA_SP, _adapter, _atom_participant_doc, _reaction_payload

CODE = "one_electron_hf_substitution"
DLPNO = {"method": "dlpno-ccsd(t)-f12", "basis": "cc-pvtz-f12", "aux_basis": "aug-cc-pvtz/c",
         "cabs_basis": "cc-pvtz-f12-cabs"}
HF_SP = {**ORCA_SP, "method": "hf"}
HF_ROUTE = "uhf hf cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno"


def _rename(node, old, new):
    if isinstance(node, dict):
        return {k: _rename(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_rename(v, old, new) for v in node]
    return new if node == old else node


def _doc(**atom_changes):
    """The rxn_210 shape: the H atom at HF (unrestricted doublet), the radicals unrestricted doublets."""
    doc, atom = _atom_participant_doc()
    old = atom["label"]
    doc = _rename(doc, old, "H")
    atom = next(r for r in doc["species"] if r["label"] == "H")
    atom["levels"]["sp"] = dict(HF_SP)
    atom["sp_route"] = HF_ROUTE
    for record in (*doc["species"], *doc["transition_states"]):
        mult = record.get("multiplicity")
        record["scf_reference"] = {**(record.get("scf_reference") or {}),
                                   "sp_reference": "restricted" if mult == 1 else "unrestricted"}
    atom.update(atom_changes)
    return doc


def _build(doc, tmp_path):
    warnings = []
    return _reaction_payload(_adapter(tmp_path), doc, warnings), warnings


def _kinetics(payload):
    (block,) = payload["kinetics"]
    return block


def _substitutions(warnings):
    return [w for w in warnings if w["code"] == CODE]


def _h_sp_key(payload):
    (key,) = [c["key"] for s in payload["species"]
              for c in [s["conformers"][0]["calculation"], *s.get("calculations", ())]
              if c["type"] == "sp" and c["level_of_theory"]["method"] == "hf"]
    return key


def test_the_h_atom_really_is_computed_at_hf_in_the_payload(tmp_path):
    payload, _ = _build(_doc(), tmp_path)
    levels = {c["key"]: c["level_of_theory"] for s in payload["species"]
              for c in [s["conformers"][0]["calculation"], *s.get("calculations", ())] if c["type"] == "sp"}
    h_level = next(v for k, v in levels.items() if v["method"] == "hf")
    assert {k: h_level[k] for k in ("basis", "aux_basis", "cabs_basis")} == {
        k: DLPNO[k] for k in ("basis", "aux_basis", "cabs_basis")}
    assert sum(v["method"] == "hf" for v in levels.values()) == 1
    assert all(v["method"] == "dlpno-ccsd(t)-f12" for v in levels.values() if v["method"] != "hf")


def test_the_kinetics_block_declares_the_correlated_level_with_unrestricted(tmp_path):
    payload, warnings = _build(_doc(), tmp_path)
    assert _kinetics(payload)["energy_level_of_theory"] == {**DLPNO, "spin_treatment": "unrestricted"}
    contract_validate(ComputedReactionUploadRequest, payload)


def test_the_substitution_is_reported_with_the_h_sp_key_and_the_cabs_caveat(tmp_path):
    payload, warnings = _build(_doc(), tmp_path)
    (warning,) = _substitutions(warnings)
    assert warning["field"] == "kinetics[0].energy_level_of_theory"
    assert warning["context"]["calculation_keys"] == [_h_sp_key(payload)]
    assert warning["context"]["cabs_singles_correction"] == "absent"
    assert "CABS singles" in warning["message"]
    assert warning["context"]["source"] == "tckdb_arc_self_check"


def test_no_substitution_when_the_h_atom_is_at_the_shared_level(tmp_path):
    doc = _doc()
    atom = next(r for r in doc["species"] if r["label"] == "H")
    atom["levels"]["sp"] = dict(ORCA_SP)
    atom["sp_route"] = "uhf dlpno-ccsd(t)-f12 cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno"
    payload, warnings = _build(doc, tmp_path)
    assert _kinetics(payload)["energy_level_of_theory"]["method"] == "dlpno-ccsd(t)-f12"
    assert not _substitutions(warnings)


def test_a_hydride_at_hf_declares_nothing(tmp_path):
    """H- has two electrons (charge -1): the HF energy is not the correlated one."""
    payload, warnings = _build(_doc(charge=-1, multiplicity=1), tmp_path)
    assert "energy_level_of_theory" not in _kinetics(payload)
    assert not _substitutions(warnings)
    contract_validate(ComputedReactionUploadRequest, payload)


def test_a_dft_declared_method_grants_no_exemption(tmp_path):
    """B3LYP everywhere but H at HF: the exemption is for wave-function declared methods only."""
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        sp = (record.get("levels") or {}).get("sp")
        if sp and sp.get("method") == "dlpno-ccsd(t)-f12":
            sp["method"] = "b3lyp"
            if record.get("sp_route"):
                record["sp_route"] = record["sp_route"].replace("dlpno-ccsd(t)-f12", "b3lyp")
    payload, warnings = _build(doc, tmp_path)
    assert "energy_level_of_theory" not in _kinetics(payload)
    assert not _substitutions(warnings)
    assert any(c["level_of_theory"]["method"] == "b3lyp" for s in payload["species"]
               for c in [s["conformers"][0]["calculation"], *s.get("calculations", ())] if c["type"] == "sp")
    contract_validate(ComputedReactionUploadRequest, payload)


def test_the_h_atom_at_hf_in_another_basis_declares_nothing(tmp_path):
    doc = _doc()
    atom = next(r for r in doc["species"] if r["label"] == "H")
    atom["levels"]["sp"] = {**HF_SP, "basis": "cc-pvdz-f12"}
    atom["sp_route"] = HF_ROUTE.replace("cc-pvtz-f12 ", "cc-pvdz-f12 ", 1)
    payload, warnings = _build(doc, tmp_path)
    assert "energy_level_of_theory" not in _kinetics(payload)
    assert not _substitutions(warnings)


def test_an_unstated_composition_grants_no_exemption(tmp_path):
    """No xyz and no formula: the electron count is unknown, so the HF link stays a contradiction."""
    doc = _doc()
    atom = next(r for r in doc["species"] if r["label"] == "H")
    from tckdb_arc.adapter import _participant_electron_count
    assert _participant_electron_count(atom) == 1
    assert _participant_electron_count({**atom, "xyz": None, "formula": None}) is None
    assert _participant_electron_count({k: v for k, v in atom.items() if k != "charge"}) is None
    assert _participant_electron_count({**atom, "charge": -1}) == 2
    assert _participant_electron_count(None) is None


def test_the_input_document_is_not_mutated(tmp_path):
    doc = _doc()
    before = copy.deepcopy(doc)
    _build(doc, tmp_path)
    assert doc == before
