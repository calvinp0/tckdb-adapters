"""A single atom is uploaded with its own ``sp`` as the primary calculation (A6, tckdb-schemas 0.59).

ARC never optimises an atom. Until 0.59 TCKDB's computed-species and computed-reaction
routes demanded an ``opt`` primary, so the adapter filed the atom's single point as a
placeholder ``opt`` and warned (TCKDB#600). TCKDB#610 (tckdb-schemas 0.59) accepts
``type: "sp"`` as the primary calculation of a conformer whose own XYZ has exactly one
atom, on both bundle routes (and ``/uploads/conformers``). The adapter therefore sends the
atom's real single point (its own log, level, program and energy), no ``opt``, no
placeholder and no warning. A molecule with no opt job keeps its placeholder opt
(``primary_opt_placeholder_no_opt_job``): 0.59 does not cover it.

The contract's offline-checkable rules are replicated here:

* ``thermo_role_duplicate`` / ``statmech_role_duplicate``: two ``sp`` links on one geometry are
  refused when no ``opt`` is linked, so the atom's thermo and statmech each link its one ``sp`` once;
* the ownership rule (links name the atom's own calculations) is in ``test_reaction_bundle_source_links``.
"""

import copy
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest
import yaml

from _contract import contract_validate
from test_adapter import _fake_output_doc, _full_record, _reaction_output_doc, _reaction_record
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.conformer_upload import ConformerUploadRequest

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
FIXTURES = Path(__file__).parent / "fixtures"
PLACEHOLDER_CODES = {"monatomic_species_primary_opt_placeholder", "primary_opt_placeholder_no_opt_job",
                     "composite_geometry_level_not_stated"}


def _adapter(tmp_path):
    return TCKDBAdapter(TCKDBConfig(
        enabled=True, base_url="http://x", upload=False, payload_dir=tmp_path,
        project_label="p", api_key_env="X_TCKDB_API_KEY"))


def _atom_record():
    record = copy.deepcopy(_full_record())
    record.update(label="H", smiles="[H]", multiplicity=2, xyz="H 0.0 0.0 0.0", sp_energy_hartree=-0.5)
    return record


def _atom_record_1_3():
    """A schema-1.3 atom: no opt job (``levels.opt`` null), its sp log is also its ``opt_log``."""
    doc = yaml.safe_load((FIXTURES / "arc_1_3_levels" / "output.yml").read_text())
    record = copy.deepcopy(next(s for s in doc["species"] if s["label"] == "iC3H7"))
    sp_level = {"basis": "cc-pvtz", "method": "wb97xd"}
    record.update(
        label="H", smiles="[H]", formula="H", multiplicity=2, xyz="H 0.0 0.0 0.0",
        xyz_isotopes=[1], opt_input_xyz=None, opt_input_xyz_isotopes=None, conformers_isotopes=None,
        conformers=None, conformer_energies=None, conformer_levels=None, thermo=None, statmech=None,
        energy_corrections=[], freq_log=None, freq_route=None, freq_n_imag=None, zpe_hartree=None,
        opt_n_steps=None, opt_final_energy_hartree=None, opt_converged=None, imag_freq_cm1=None,
        opt_route=None, opt_dipole_moment_debye=None, opt_dipole_moment_density=None,
        sp_spin_diagnostic=None, sp_energy_hartree=-0.5, sp_log="calcs/Species/H/sp/output.out",
        opt_log="calcs/Species/H/sp/output.out", sp_route="#P wb97xd/cc-pvtz",
        ess_software={"sp": "gaussian", "opt": None, "freq": None},
        ess_versions={"sp": "Gaussian 16, Revision C.01", "opt": None, "freq": None},
        levels={"composite": None, "freq": None, "irc": None, "opt": None, "sp": sp_level})
    return doc, record


def _submit_species(tmp_path, doc, record):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
    return payload, outcome


def _codes(outcome):
    return [w["code"] for w in outcome.warnings]


def _calcs(species_block):
    primary = [c["calculation"] for c in species_block["conformers"]]
    return [*primary, *species_block.get("calculations", [])]


def _reaction_outcome(tmp_path):
    doc = yaml.safe_load((GOLDEN / "phase3_output.yml").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(GOLDEN / "tckdb_evidence.json", tmp_path / "output" / "tckdb_evidence.json")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=doc["reactions"][0])
    return json.loads(outcome.payload_path.read_text()), outcome


# ---------------------------------------------------------------------------
# computed-species route
# ---------------------------------------------------------------------------

def test_species_route_uploads_the_atoms_sp_as_its_primary_calculation(tmp_path):
    payload, outcome = _submit_species(tmp_path, _fake_output_doc(), _atom_record())
    (conformer,) = payload["conformers"]
    primary = conformer["primary_calculation"]
    assert primary["type"] == "sp" and primary["key"] == "sp"
    assert primary["sp_result"] == {"electronic_energy_hartree": -0.5}
    # No relabelled optimisation: no opt result, no converged flag, no placeholder origin, no
    # dependency on an opt that does not exist, and no second copy of the same job.
    assert "opt_result" not in primary and "depends_on" not in primary
    assert "parameters_json" not in primary or "tckdb_origin" not in primary["parameters_json"]
    assert conformer["additional_calculations"] == []
    assert conformer["geometry"]["xyz_text"].splitlines()[0] == "1"
    assert not PLACEHOLDER_CODES & set(_codes(outcome))
    contract_validate(ComputedSpeciesUploadRequest, payload)


def test_a_1_3_atom_is_filed_at_the_level_and_program_of_its_sp_log(tmp_path):
    doc, record = _atom_record_1_3()
    payload, outcome = _submit_species(tmp_path, doc, record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert primary["type"] == "sp"
    assert (primary["level_of_theory"]["method"], primary["level_of_theory"]["basis"]) == ("wb97xd", "cc-pvtz")
    assert primary["software_release"]["name"] == "gaussian"
    assert primary["software_release"]["version"].startswith("16") or "C.01" in json.dumps(
        primary["software_release"])
    # Its sp log is its own job, not an energy reused from an opt that never ran.
    assert "tckdb_origin" not in (primary.get("parameters_json") or {})
    assert not PLACEHOLDER_CODES & set(_codes(outcome))


def test_an_atom_of_a_composite_run_is_filed_as_its_sp_at_the_composite_level_without_a_placeholder(tmp_path):
    """A composite atom has no opt either; it must not draw the composite placeholder-opt warning."""
    doc, record = _atom_record_1_3()
    log = record["sp_log"]
    record.update(composite_log=log, levels={"composite": {"method": "cbs-qb3"}, "freq": None, "irc": None,
                                              "opt": None, "sp": None},
                  ess_software={"composite": "gaussian", "sp": "gaussian", "opt": None, "freq": None},
                  ess_versions={"composite": "Gaussian 16, Revision C.01", "sp": "Gaussian 16, Revision C.01",
                                "opt": None, "freq": None})
    payload, outcome = _submit_species(tmp_path, doc, record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert primary["type"] == "sp" and primary["level_of_theory"]["method"] == "cbs-qb3"
    assert not PLACEHOLDER_CODES & set(_codes(outcome))
    assert "parameters_json" not in primary or "placeholder" not in json.dumps(primary["parameters_json"])


def test_an_atom_whose_energy_is_stated_only_as_the_opt_energy_still_files_its_sp(tmp_path):
    """Older ARC output (and some records) state the atom's one job's energy as ``opt_final_energy_hartree``."""
    record = _atom_record()
    record.update(sp_energy_hartree=None, opt_final_energy_hartree=-0.4999)
    payload, _ = _submit_species(tmp_path, _fake_output_doc(), record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert primary["type"] == "sp" and primary["sp_result"] == {"electronic_energy_hartree": -0.4999}


def _composite_atom(**energies):
    doc, record = _atom_record_1_3()
    record.update(composite_log=record["sp_log"],
                  levels={"composite": {"method": "cbs-qb3"}, "freq": None, "irc": None, "opt": None, "sp": None},
                  ess_software={"composite": "gaussian", "sp": "gaussian", "opt": None, "freq": None},
                  ess_versions={"composite": "Gaussian 16, Revision C.01", "sp": "Gaussian 16, Revision C.01",
                                "opt": None, "freq": None})
    record.update(energies)
    return doc, record


def test_a_composite_atom_with_sp_energy_files_its_sp_with_that_energy(tmp_path):
    doc, record = _composite_atom(sp_energy_hartree=-0.5, opt_final_energy_hartree=-0.49)
    payload, _ = _submit_species(tmp_path, doc, record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert primary["type"] == "sp" and primary["sp_result"] == {"electronic_energy_hartree": -0.5}


def test_a_composite_atom_without_sp_energy_is_refused_not_filed_from_the_opt_energy(tmp_path):
    """The composite log's opt_final_energy may be an intermediate SCF energy, not the composite energy."""
    doc, record = _composite_atom(sp_energy_hartree=None, opt_final_energy_hartree=-0.49)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        with pytest.raises(ValueError, match="single atom.*composite run.*no sp_energy_hartree"):
            _adapter(tmp_path).submit_computed_species_from_output(output_doc=doc, species_record=record)


def test_an_atom_with_no_stated_energy_is_not_relabelled_as_an_opt(tmp_path):
    record = _atom_record()
    record.update(sp_energy_hartree=None, opt_final_energy_hartree=None)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        with pytest.raises(ValueError, match="single atom.*neither sp_energy_hartree"):
            _adapter(tmp_path).submit_computed_species_from_output(
                output_doc=_fake_output_doc(), species_record=record)


def test_a_molecule_keeps_its_opt_primary_and_gets_no_placeholder_warning(tmp_path):
    payload, outcome = _submit_species(tmp_path, _fake_output_doc(), _full_record())
    assert payload["conformers"][0]["primary_calculation"]["type"] == "opt"
    assert not PLACEHOLDER_CODES & set(_codes(outcome))


def test_a_molecule_with_no_opt_job_keeps_its_placeholder_opt(tmp_path):
    """0.59 covers a one-atom XYZ only; a molecule whose opt ARC did not export is still a placeholder."""
    doc = yaml.safe_load((FIXTURES / "arc_1_3_levels" / "output.yml").read_text())
    record = copy.deepcopy(next(s for s in doc["species"] if s["label"] == "iC3H7"))
    record.update(opt_log=None, composite_log=None, opt_n_steps=None, opt_final_energy_hartree=None)
    record["levels"] = {**record["levels"], "opt": None}
    record["ess_software"] = {**record["ess_software"], "opt": None}
    payload, outcome = _submit_species(tmp_path, doc, record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert primary["type"] == "opt"
    assert primary["parameters_json"]["tckdb_origin"]["origin_detail"] == "placeholder_primary_opt_no_opt_job"
    assert "primary_opt_placeholder_no_opt_job" in _codes(outcome)


# ---------------------------------------------------------------------------
# thermo / statmech links and corrections: one sp link, the atom's own
# ---------------------------------------------------------------------------

def _linked_blocks(payload):
    return [b for b in (payload.get("thermo"), payload.get("statmech")) if b]


def test_the_atoms_thermo_and_statmech_link_its_one_sp_once(tmp_path):
    record = _atom_record()
    record["thermo"] = {
        "h298_kj_mol": 218.0, "s298_j_mol_k": 114.7, "tmin_k": 300.0, "tmax_k": 2000.0,
        "standard_state_pressure_pa": 101325.0,
        "thermo_points": [{"temperature_k": 300.0, "cp_j_mol_k": 20.8, "s_j_mol_k": 114.7, "h_kj_mol": 218.0}],
        "atom_corrections_applied": True,
    }
    record["statmech"] = {"external_symmetry": 1, "rigid_rotor_kind": "atom", "is_linear": False}
    doc = _fake_output_doc()
    payload, _ = _submit_species(tmp_path, doc, record)
    blocks = _linked_blocks(payload)
    assert blocks, "the atom's thermo or statmech block must be built for this check to mean anything"
    for block in blocks:
        links = [(l["calculation_key"], l["role"]) for l in block.get("source_calculations", [])]
        # thermo_role_duplicate / statmech_role_duplicate: no repeated role on the one sp.
        assert len({role for _, role in links}) == len(links)
        assert set(links) <= {("sp", "sp")}
    assert any(block.get("source_calculations") for block in blocks)


def test_an_atoms_statmech_states_no_electronic_levels(tmp_path):
    """tckdb-schemas 0.60 accepts ``electronic_levels`` on the bundle statmech blocks, and TCKDB then warns
    ``missing_atomic_electronic_levels`` for an atom whose ground term is not S. ARC 1.3 exports no electronic
    level, term symbol or spin-orbit data (its statmech record has none), so none is sent and none is derived."""
    from tckdb_arc.adapter import _STATMECH_FIELDS_BY_TARGET
    assert all("electronic_levels" not in fields for fields in _STATMECH_FIELDS_BY_TARGET.values())
    record = _atom_record()
    record["statmech"] = {"external_symmetry": 1, "rigid_rotor_kind": "atom", "is_linear": False,
                          "electronic_levels": [{"level_index": 0, "energy_cm1": 0.0, "degeneracy": 4}]}
    payload, _ = _submit_species(tmp_path, _fake_output_doc(), record)
    assert "electronic_levels" not in json.dumps(payload)


def test_the_atoms_applied_corrections_point_at_its_sp(tmp_path):
    doc, record = _atom_record_1_3()
    record["energy_corrections"] = [{
        "kind": "atom_energy", "model": "atom_energy", "total": {"value": -0.1, "unit": "hartree"},
        "level_of_theory": {"basis": "cc-pvtz", "method": "wb97xd"},
    }]
    payload, _ = _submit_species(tmp_path, doc, record)
    for correction in payload.get("applied_energy_corrections", []):
        assert correction["source_calculation_key"] == "sp"


# ---------------------------------------------------------------------------
# computed-reaction route
# ---------------------------------------------------------------------------

def test_reaction_route_uploads_each_atom_with_an_sp_primary_and_no_warning(tmp_path):
    payload, outcome = _reaction_outcome(tmp_path)
    h_block = next(s for s in payload["species"] if s["key"].endswith("_H"))
    (conformer,) = h_block["conformers"]
    assert conformer["calculation"]["type"] == "sp"
    assert conformer["calculation"]["key"].endswith("_sp")
    assert h_block["calculations"] == []        # H is declared once (A16); no second sp, no opt, no freq
    assert not PLACEHOLDER_CODES & set(_codes(outcome))
    contract_validate(ComputedReactionUploadRequest, payload)


# ---------------------------------------------------------------------------
# conformer route
# ---------------------------------------------------------------------------

def test_conformer_route_uploads_the_atom_with_an_sp_primary(tmp_path):
    adapter = _adapter(tmp_path)
    payload = adapter._build_payload(output_doc=_fake_output_doc(), species_record=_atom_record(), warnings=[])
    assert payload["calculation"]["type"] == "sp" and payload["calculation"]["key"] == "sp"
    assert "additional_calculations" not in payload
    assert "opt_result" not in payload["calculation"]
    contract_validate(ConformerUploadRequest, payload)
