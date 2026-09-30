"""A single-atom species is uploaded with a placeholder opt and a warning (A6).

ARC never optimises a single atom, but the computed-species and
computed-reaction routes require an opt primary calculation, so the atom is
deposited with that opt (``converged`` as ARC reports it) and the adapter warns
(TCKDB issue #600 asks for an sp primary). The contract's calculation model has
no ``note`` to carry the statement, so the warning is where it lives.
"""

import copy
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import yaml

from test_adapter import _fake_output_doc, _full_record, _reaction_output_doc, _reaction_record
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
CODE = "monatomic_species_primary_opt_placeholder"


def _adapter(tmp_path):
    return TCKDBAdapter(TCKDBConfig(
        enabled=True, base_url="http://x", upload=False, payload_dir=tmp_path,
        project_label="p", api_key_env="X_TCKDB_API_KEY"))


def _atom_record():
    record = copy.deepcopy(_full_record())
    record.update(label="H", smiles="[H]", multiplicity=2, xyz="H 0.0 0.0 0.0")
    return record


def _codes(outcome):
    return [w["code"] for w in outcome.warnings]


def test_species_route_uploads_an_atom_with_placeholder_opt_and_warning(tmp_path):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=_fake_output_doc(), species_record=_atom_record())
    payload = json.loads(outcome.payload_path.read_text())
    assert payload["conformers"][0]["primary_calculation"]["type"] == "opt"
    assert _codes(outcome).count(CODE) == 1
    warning = next(w for w in outcome.warnings if w["code"] == CODE)
    assert "issues/600" in warning["message"] and "'H'" in warning["message"]
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings


def test_old_arc_atom_without_xyz_is_recognised_by_its_formula(tmp_path):
    record = _atom_record()
    record["formula"] = "H"
    outcome_warnings = []
    _adapter(tmp_path)._build_computed_species_payload(
        output_doc=_fake_output_doc(), species_record=record, conformer_key="conf0",
        warnings=outcome_warnings)
    assert [w["code"] for w in outcome_warnings].count(CODE) == 1


def test_a_molecule_gets_no_warning(tmp_path):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=_fake_output_doc(), species_record=_full_record())
    assert CODE not in _codes(outcome)


def test_reaction_route_uploads_the_atom_and_warns_once_per_atom_species(tmp_path):
    doc = yaml.safe_load((GOLDEN / "phase3_output.yml").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(GOLDEN / "tckdb_evidence.json", tmp_path / "output" / "tckdb_evidence.json")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=doc["reactions"][0])
    payload = json.loads(outcome.payload_path.read_text())
    h_block = next(s for s in payload["species"] if s["key"].endswith("_H"))
    assert h_block["conformers"][0]["calculation"]["type"] == "opt"
    warnings = [w for w in outcome.warnings if w["code"] == CODE]
    assert len(warnings) == 1  # H is declared once (A16)
    assert warnings[0]["field"] == f"species[{h_block['key']}].conformers[0].calculation"


def test_ts_route_names_atoms_as_participants_without_a_warning(tmp_path):
    doc = yaml.safe_load((GOLDEN / "phase3_output.yml").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(GOLDEN / "tckdb_evidence.json", tmp_path / "output" / "tckdb_evidence.json")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_ts_from_output(
            output_doc=doc, ts_record=doc["transition_states"][0],
            reaction_record=doc["reactions"][0])
    payload = json.loads(outcome.payload_path.read_text())
    smiles = [p["species_entry"]["smiles"]
              for p in payload["reaction"]["reactants"] + payload["reaction"]["products"]]
    assert smiles.count("[H]") == 2
    assert CODE not in _codes(outcome)  # no per-species conformer, so no placeholder opt
