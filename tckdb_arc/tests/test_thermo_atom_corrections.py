"""ARC output.yml 1.2 atom-correction flags decide whether thermo enthalpy is formation_298k.

``thermo.atom_corrections_applied`` false means Arkane subtracted no atom
energies; true still needs ``thermo.atom_corrections_level`` to be the level
the species' energies were computed at. Enthalpy that fails either check (or
the magnitude backstop, the only check for a null flag) is stripped from the
thermo block; entropy and Cp still go up.
"""

import copy
import itertools
import json
import math
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from tckdb_arc.adapter import (
    _build_thermo_block,
    _level_identity,
    _thermo_energy_level,
)
from tckdb_arc.evidence import EvidenceStore, validate_output_schema
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.workflows.computed_reaction_upload import BundleThermoIn
from tckdb_schemas.workflows.computed_species_upload import (
    ComputedSpeciesUploadRequest,
    ThermoInBundle,
)

from test_thermo_enthalpy_declaration import _adapter

FIXTURE = Path(__file__).parent / "fixtures" / "arc_1_2" / "output.yml"
TARGETS = (("ThermoInBundle", ThermoInBundle), ("BundleThermoIn", BundleThermoIn))
BOUND_KJ_MOL = 2.0e4


def _doc(**overrides):
    doc = yaml.safe_load(FIXTURE.read_text())
    doc.update(overrides)
    return doc


def _species(doc, label):
    return copy.deepcopy(next(s for s in doc["species"] if s["label"] == label))


def _submit(tmp_path, doc, record):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    ComputedSpeciesUploadRequest.model_validate(payload)
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
    return payload.get("thermo"), outcome.warnings


def _expected_stripped(thermo):
    """The block a stripped ARC thermo record must become: S298, point S/Cp, P, no enthalpy."""
    return {
        "s298_j_mol_k": thermo["s298_j_mol_k"],
        "tmin_k": thermo["tmin_k"],
        "tmax_k": thermo["tmax_k"],
        "points": [
            {key: p[key] for key in ("temperature_k", "cp_j_mol_k", "s_j_mol_k")}
            for p in thermo["thermo_points"]
        ],
        "reference_pressure_bar": 1.01325,
    }


def _assert_stripped_block_is_valid(block):
    assert enthalpy_reference_error(block) is None
    for _target, model in TARGETS:
        model.model_validate(block)


def _warning(code, action="thermo_enthalpy_omitted", field="thermo"):
    return {"code": code, "field": field,
            "context": {"source": "tckdb_arc_self_check", "action": action}}


def _shape(warnings):
    return [{k: w[k] for k in ("code", "field", "context")} for w in warnings]


def test_schema_1_2_is_supported():
    doc = _doc()
    assert validate_output_schema(doc) == "1.2"
    # No evidence descriptor: the store falls back instead of refusing the document.
    assert EvidenceStore(None).lookup(doc, "species", "CH4", "freq_hessian").state == "fallback"


def test_corrected_at_the_energy_level_declares_formation_298k(tmp_path):
    # atom_corrections_level wb97xd/def2tzvp vs sp_level wb97x-d/def2-tzvp: one level.
    doc = _doc()
    thermo, warnings = _submit(tmp_path, doc, _species(doc, "CH4"))
    assert warnings == []
    assert thermo["h298_kj_mol"] == -74.6
    assert thermo["enthalpy_reference_kind"] == "formation_298k"
    assert "nasa" in thermo
    assert [p["h_kj_mol"] for p in thermo["points"]] == [-74.5, -36.1]


def test_stand_in_correction_level_strips_enthalpy(tmp_path):
    # ARC's BDE example: bmk/cbsb7 atom energies applied to apfd/def2svp energies.
    doc = _doc(sp_level={"method": "apfd", "basis": "def2svp", "software": "gaussian"})
    record = _species(doc, "CH4_standin")
    thermo, warnings = _submit(tmp_path, doc, record)
    assert thermo == _expected_stripped(record["thermo"]) | {"source_calculations": thermo["source_calculations"]}
    _assert_stripped_block_is_valid(thermo)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch")]
    assert "bmk/cbsb7" in warnings[0]["message"]
    assert "apfd/def2svp" in warnings[0]["message"]


@pytest.mark.parametrize("label", ["CH4_uncorrected", "H", "H2"])
def test_corrections_not_applied_strips_enthalpy(tmp_path, label):
    doc = _doc()
    record = _species(doc, label)
    assert "energy_corrections" not in record
    thermo, warnings = _submit(tmp_path, doc, record)
    assert thermo == _expected_stripped(record["thermo"]) | {"source_calculations": thermo["source_calculations"]}
    _assert_stripped_block_is_valid(thermo)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_not_applied")]


@pytest.mark.parametrize("label", ["H", "H2"])
def test_flag_catches_what_the_magnitude_guard_misses(label):
    thermo = _species(_doc(), label)["thermo"]
    assert abs(thermo["h298_kj_mol"]) < BOUND_KJ_MOL
    thermo["atom_corrections_applied"] = None
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle")
    assert block["enthalpy_reference_kind"] == "formation_298k"  # undetectable without the flag


def test_unknown_flag_falls_back_to_the_magnitude_guard(tmp_path):
    doc = _doc()
    record = _species(doc, "CH4_yml")
    assert record["thermo"]["atom_corrections_applied"] is None
    thermo, warnings = _submit(tmp_path, doc, record)
    assert thermo == _expected_stripped(record["thermo"]) | {"source_calculations": thermo["source_calculations"]}
    assert _shape(warnings) == [_warning("enthalpy_not_formation_magnitude")]


def test_unknown_flag_with_formation_magnitude_is_declared():
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo.update(atom_corrections_applied=None, bond_corrections_applied=None,
                  atom_corrections_level=None)
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle")
    assert block["enthalpy_reference_kind"] == "formation_298k"
    assert block["h298_kj_mol"] == -74.6


def test_legacy_record_without_flags_uses_the_magnitude_guard():
    thermo = _species(_doc(), "CH4_uncorrected")["thermo"]
    for key in ("atom_corrections_applied", "bond_corrections_applied", "atom_corrections_level"):
        del thermo[key]
    warnings = []
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                warnings=warnings)
    assert block == _expected_stripped(thermo)
    assert _shape(warnings) == [_warning("enthalpy_not_formation_magnitude")]


@pytest.mark.parametrize("energy_level,corrections_level", [
    ({"method": "wB97X-D", "basis": "def2-TZVP"}, {"method": "wb97xd", "basis": "def2tzvp"}),
    ({"method": "B3LYP", "basis": "6-311+G(d,p)", "software": "gaussian", "year": 2019},
     {"method": "b3lyp", "basis": "6-311+g(d,p)", "software": "qchem", "method_type": "dft"}),
    ({"method": "CCSD(T)-F12", "basis": "cc-pVTZ-F12"}, {"method": "ccsd(t)f12", "basis": "ccpvtzf12"}),
    ({"method": "b3lyp-d3bj", "basis": "def2tzvp"}, {"method": "b3lyp-d3bj", "basis": "def2tzvp"}),
    ({"method": "B3LYP-D3(BJ)", "basis": "def2-TZVP"}, {"method": "b3lyp-d3bj", "basis": "def2tzvp"}),
    ({"method": "b3lyp-gd3bj", "basis": "def2tzvp"}, {"method": "b3lyp-d3bj", "basis": "def2tzvp"}),
    ({"method": "b3lyp", "basis": "def2tzvp"}, {"method": "b3lyp2023", "basis": "def2tzvp"}),
    ({"method": "dsd-pbep86", "basis": "def2tzvp"}, {"method": "dsd-pbep86-2013", "basis": "def2tzvp"}),
    ({"method": "b3lyp", "basis": "def2tzvp", "auxiliary_basis": "def2/j", "cabs": "x",
      "software_version": "g16", "args": {"keyword": {"opt": "tight"}}},
     {"method": "b3lyp", "basis": "def2tzvp"}),
], ids=["dispersion_hyphens", "software_year_type_ignored", "f12_parentheses_kept_both_sides",
        "dispersion_in_method", "dispersion_parentheses", "gaussian_dispersion_suffix",
        "refit_year_in_method", "refit_year_after_hyphen", "aux_cabs_version_args_ignored"])
def test_level_spelling_variants_match(energy_level, corrections_level):
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo["atom_corrections_level"] = corrections_level
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                energy_level=energy_level)
    assert block["enthalpy_reference_kind"] == "formation_298k"


@pytest.mark.parametrize("corrections_level", [
    {"method": "wb97xd", "basis": "def2svp"},     # basis differs
    {"method": "b97d3", "basis": "def2tzvp"},     # method differs
    {"method": "wb97xd"},                        # basis missing on one side only
    None,                                        # true with no level recorded
    {"basis": "def2tzvp"},                       # no method
    {"method": "wb97xd3", "basis": "def2tzvp"},  # a different dispersion variant
    {"method": "wb97x", "basis": "def2tzvp"},    # dispersion missing
])
def test_level_mismatch_or_missing_level_strips(corrections_level):
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo["atom_corrections_level"] = corrections_level
    warnings = []
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                warnings=warnings,
                                energy_level={"method": "wB97X-D", "basis": "def2-TZVP"})
    assert block == _expected_stripped(thermo)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch")]


@pytest.mark.parametrize("energy_level,corrections_level", [
    ({"method": "ccsd(t)", "basis": "cc-pvtz"}, {"method": "ccsdt", "basis": "cc-pvtz"}),
    ({"method": "dlpno-ccsd(t)-f12", "basis": "cc-pvtz-f12"},
     {"method": "dlpno-ccsdt-f12", "basis": "cc-pvtz-f12"}),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd4"},   # 'g' is stripped only for gd2/gd3/gd3bj
     {"method": "b3lyp-d4", "basis": "def2tzvp"}),
    ({"method": "b3lyp", "basis": "def2tzvp"}, {"method": "b3lyp", "basis": "def2_tzvp"}),
], ids=["ccsd_t_vs_ccsdt", "dlpno_f12_parentheses", "gd4_field", "underscore_is_significant"])
def test_levels_arc_keeps_distinct_mismatch(energy_level, corrections_level):
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo["atom_corrections_level"] = corrections_level
    warnings = []
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                warnings=warnings, energy_level=energy_level)
    assert block == _expected_stripped(thermo)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch")]


def test_corrections_applied_with_unknown_energy_level_strips():
    thermo = _species(_doc(), "CH4")["thermo"]
    warnings = []
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                warnings=warnings, energy_level=None)
    assert block == _expected_stripped(thermo)
    assert "an unrecorded level" in warnings[0]["message"]


def test_composite_method_is_the_energy_level(tmp_path):
    composite = {"method": "CBS-QB3", "method_type": "composite", "software": "gaussian"}
    doc = _doc(composite_method=composite)
    assert _thermo_energy_level(doc) is doc["composite_method"]
    record = _species(doc, "CH4")
    record["thermo"]["atom_corrections_level"] = {
        "method": "cbs-qb3", "basis": None, "method_type": "composite", "software": "gaussian"}
    thermo, warnings = _submit(tmp_path, doc, record)
    assert warnings == []
    assert thermo["enthalpy_reference_kind"] == "formation_298k"
    # The same record against the doc's sp_level would mismatch.
    doc["composite_method"] = None
    _thermo, warnings = _submit(tmp_path / "sp", doc, record)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch")]


def test_energy_level_falls_back_to_opt_when_sp_level_is_null():
    doc = _doc(sp_level=None)
    assert _thermo_energy_level(doc) == doc["opt_level"]
    assert _level_identity(_thermo_energy_level(_doc())) == ("wb97xd", "def2tzvp")


@pytest.mark.parametrize("target,model", TARGETS)
@pytest.mark.parametrize("label", ["CH4_standin", "CH4_uncorrected", "CH4_yml", "H", "H2"])
def test_stripped_blocks_validate_for_both_roots(label, target, model):
    doc = _doc(sp_level={"method": "apfd", "basis": "def2svp"})
    thermo = _species(doc, label)["thermo"]
    block = _build_thermo_block(thermo, calc_keys_by_role={"opt": "r0_opt"}, target_model=target,
                                energy_level=_thermo_energy_level(doc))
    assert block == _expected_stripped(thermo) | {
        "source_calculations": [{"calculation_key": "r0_opt", "role": "opt"}]}
    assert enthalpy_reference_error(block) is None
    model.model_validate(block)


def test_reaction_participant_warning_carries_its_field(tmp_path):
    doc = _doc()
    record = _species(doc, "CH4_uncorrected")
    warnings = []
    block = _build_thermo_block(record["thermo"], calc_keys_by_role={}, target_model="BundleThermoIn",
                                warnings=warnings, warning_field="species[r0_CH4].thermo",
                                energy_level=_thermo_energy_level(doc))
    assert "h298_kj_mol" not in block
    assert _shape(warnings) == [
        _warning("enthalpy_atom_corrections_not_applied", field="species[r0_CH4].thermo")]


def test_nothing_left_after_stripping_omits_the_block():
    warnings = []
    block = _build_thermo_block(
        {"h298_kj_mol": -1306.5, "atom_corrections_applied": False,
         "thermo_points": [{"temperature_k": 300.0, "h_kj_mol": -1306.5, "g_kj_mol": -1340.9}]},
        calc_keys_by_role={}, target_model="ThermoInBundle", warnings=warnings)
    assert block is None
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_not_applied", action="thermo_omitted")]


def test_entropy_free_stripped_block_keeps_cp_without_pressure():
    block = _build_thermo_block(
        {"h298_kj_mol": -1306.5, "atom_corrections_applied": False,
         "thermo_points": [{"temperature_k": 300.0, "cp_j_mol_k": 20.8, "h_kj_mol": -1306.5}]},
        calc_keys_by_role={}, target_model="ThermoInBundle")
    assert block == {"points": [{"temperature_k": 300.0, "cp_j_mol_k": 20.8}]}
    ThermoInBundle.model_validate(block)


def test_bond_correction_flag_does_not_gate_enthalpy():
    # bond_corrections_applied only says whether BAC ran; it never makes H non-formation.
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo["bond_corrections_applied"] = True
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                energy_level=_thermo_energy_level(_doc()))
    assert block["enthalpy_reference_kind"] == "formation_298k"


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(h298_kj_mol=math.nan),
    lambda t: t["thermo_points"][0].update(h_kj_mol=math.inf),
    lambda t: t["thermo_points"][1].update(g_kj_mol=-math.inf),
    lambda t: t["nasa_low"]["coeffs"].__setitem__(5, math.nan),
], ids=["h298_nan", "point_h_inf", "point_g_inf", "nasa_nan"])
def test_non_finite_enthalpy_is_stripped(mutate):
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo.update(atom_corrections_applied=None, atom_corrections_level=None)
    mutate(thermo)
    warnings = []
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                warnings=warnings)
    assert block == _expected_stripped(thermo)
    _assert_stripped_block_is_valid(block)
    assert _shape(warnings) == [_warning("enthalpy_not_finite")]


@pytest.mark.parametrize("energy_level,corrections_level,side,field", [
    # ARC matched plain B3LYP atom energies for B3LYP + GD3BJ and says the levels agree.
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"},
     {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"}, "energy level", "dispersion"),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "GD3BJ"},
     {"method": "b3lyp-d3bj", "basis": "def2tzvp"}, "energy level", "dispersion"),
    # A user-set arkane_level_of_theory carrying its dispersion in the field:
    # ARC applied plain b3lyp atom energies to b3lyp-d3bj energies.
    ({"method": "b3lyp-d3bj", "basis": "def2tzvp"},
     {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"}, "atom_corrections_level", "dispersion"),
    ({"method": "b3lyp-d3bj", "basis": "def2tzvp"},
     {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "EmpiricalDispersion=GD3BJ"},
     "atom_corrections_level", "dispersion"),
    # Gas-phase atom energies applied to SMD energies.
    ({"method": "b3lyp", "basis": "def2tzvp", "solvation_method": "smd", "solvent": "water"},
     {"method": "b3lyp", "basis": "def2tzvp", "solvation_method": "smd", "solvent": "water"},
     "energy level", "solvation_method"),
    ({"method": "b3lyp", "basis": "def2tzvp"},
     {"method": "b3lyp", "basis": "def2tzvp", "solvation_method": "smd", "solvent": "water"},
     "atom_corrections_level", "solvation_method"),
], ids=["dispersion_field", "dispersion_field_vs_method", "corrections_dispersion_field",
        "corrections_empiricaldispersion_field", "solvation", "corrections_solvation"])
def test_matching_level_with_dispersion_or_solvation_field_is_unverifiable(
        energy_level, corrections_level, side, field):
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo["atom_corrections_level"] = corrections_level
    warnings = []
    block = _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle",
                                warnings=warnings, energy_level=energy_level)
    assert block == _expected_stripped(thermo)
    _assert_stripped_block_is_valid(block)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_unverifiable")]
    assert warnings[0]["message"].startswith(f"The {side} ")
    assert f"sets {field}=" in warnings[0]["message"]


def test_dispersion_field_is_part_of_the_effective_method():
    # B3LYP + GD3BJ energies corrected with plain B3LYP atom energies: a mismatch,
    # reported as such rather than as merely unverifiable.
    thermo = _species(_doc(), "CH4")["thermo"]
    thermo["atom_corrections_level"] = {"method": "b3lyp", "basis": "def2tzvp"}
    warnings = []
    _build_thermo_block(thermo, calc_keys_by_role={}, target_model="ThermoInBundle", warnings=warnings,
                        energy_level={"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"})
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch")]
    assert "b3lyp + gd3bj/def2tzvp" in warnings[0]["message"]


def test_dispersion_and_solvation_fields_reach_the_species_upload(tmp_path):
    doc = _doc(sp_level={"method": "wB97X-D", "basis": "def2-TZVP", "solvation_method": "smd",
                         "solvent": "water"})
    record = _species(doc, "CH4")
    record["thermo"]["atom_corrections_level"] = dict(doc["sp_level"])
    thermo, warnings = _submit(tmp_path, doc, record)
    assert "h298_kj_mol" not in thermo
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_unverifiable")]


# ``arc.main._normalized_method_and_basis`` outputs captured from ARC 1977e53b
# (feature_export_atom_corrections_applied worktree), hard-coded so drift in the
# ported normalizer fails even where the parity test below is skipped. One row
# per branch: Gaussian's g stripped for gd2/gd3/gd3bj only, dispersion suffixes
# in the method, the EmpiricalDispersion= prefix, the refit-year split,
# significant underscores and parentheses, and a None basis.
ARC_LEVEL_IDENTITIES = [
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd2"}, ("b3lypd2", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3"}, ("b3lypd3", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"}, ("b3lypd3bj", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd4"}, ("b3lypgd4", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "D3(BJ)"}, ("b3lypd3bj", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2-TZVP", "dispersion": "EmpiricalDispersion=GD3BJ"}, ("b3lypd3bj", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "EmpiricalDispersion=GD2"}, ("b3lypd2", "def2tzvp")),
    ({"method": "b3lyp-gd2", "basis": "def2tzvp"}, ("b3lypd2", "def2tzvp")),
    ({"method": "b3lyp-gd3", "basis": "def2tzvp"}, ("b3lypd3", "def2tzvp")),
    ({"method": "b3lyp-gd3bj", "basis": "def2tzvp"}, ("b3lypd3bj", "def2tzvp")),
    ({"method": "b3lyp-d3(bj)", "basis": "def2tzvp"}, ("b3lypd3bj", "def2tzvp")),
    ({"method": "b3lyp-d3bj", "basis": "def2tzvp"}, ("b3lypd3bj", "def2tzvp")),
    ({"method": "b3lyp-gd4", "basis": "def2tzvp"}, ("b3lypgd4", "def2tzvp")),
    ({"method": "b3lyp-d3(0)", "basis": "def2tzvp"}, ("b3lypd3(0)", "def2tzvp")),
    ({"method": "b3lyp-d3zero", "basis": "def2tzvp"}, ("b3lypd3zero", "def2tzvp")),
    ({"method": "b97d3", "basis": "def2tzvp"}, ("b97d3", "def2tzvp")),
    ({"method": "wb97xd", "basis": "def2tzvp"}, ("wb97xd", "def2tzvp")),
    ({"method": "wb97xd3", "basis": "def2tzvp"}, ("wb97xd3", "def2tzvp")),
    ({"method": "wb97m-v", "basis": "def2tzvp"}, ("wb97mv", "def2tzvp")),
    ({"method": "b3lyp2023", "basis": "def2tzvp"}, ("b3lyp", "def2tzvp")),
    ({"method": "dsd-pbep86-2013", "basis": "def2tzvp"}, ("dsdpbep86", "def2tzvp")),
    ({"method": "revdsd-pbep86-d4", "basis": "def2tzvp"}, ("revdsdpbep86d4", "def2tzvp")),
    ({"method": "DLPNO-CCSD(T)-F12-2023", "basis": "cc-pVTZ-F12"}, ("dlpnoccsd(t)f12", "ccpvtzf12")),
    ({"method": "ccsd(t)", "basis": "cc-pvtz"}, ("ccsd(t)", "ccpvtz")),
    ({"method": "ccsdt", "basis": "cc-pvtz"}, ("ccsdt", "ccpvtz")),
    ({"method": "b3lyp_d3", "basis": "def2tzvp"}, ("b3lyp_d3", "def2tzvp")),
    ({"method": "b3lyp", "basis": "def2_tzvp"}, ("b3lyp", "def2_tzvp")),
    ({"method": "b3lyp", "basis": "Def2 TZVP"}, ("b3lyp", "def2tzvp")),
    ({"method": "b3lyp", "basis": "6-311+G(d,p)"}, ("b3lyp", "6311+g(d,p)")),
    ({"method": "cbs-qb3"}, ("cbsqb3", None)),
    ({"method": "CBS-QB3", "method_type": "composite"}, ("cbsqb3", None)),
]


@pytest.mark.parametrize("level,expected", ARC_LEVEL_IDENTITIES,
                         ids=[str(i) for i in range(len(ARC_LEVEL_IDENTITIES))])
def test_level_identity_matches_captured_arc_outputs(level, expected):
    assert _level_identity(level) == expected


# Level spellings for the ARC parity test: every divergence the port must
# not reintroduce (parentheses in CCSD(T), dispersion suffixes and fields,
# Gaussian's g prefix, refit years), plus the ordinary spellings.
PARITY_LEVELS = [
    {"method": "wb97xd", "basis": "def2tzvp"},
    {"method": "wB97X-D", "basis": "def2-TZVP"},
    {"method": "wb97xd3", "basis": "def2tzvp"},
    {"method": "wb97x", "basis": "def2tzvp"},
    {"method": "wb97x", "basis": "def2tzvp", "dispersion": "gd3"},
    {"method": "b97d3", "basis": "def2tzvp"},
    {"method": "b97", "basis": "def2tzvp", "dispersion": "gd3"},
    {"method": "b97-d3", "basis": "def2 tzvp"},
    {"method": "b3lyp", "basis": "def2tzvp"},
    {"method": "B3LYP", "basis": "Def2-TZVP"},
    {"method": "b3lyp2023", "basis": "def2tzvp"},
    {"method": "b3lyp-2023", "basis": "def2tzvp"},
    {"method": "b3lyp-d3bj", "basis": "def2tzvp"},
    {"method": "b3lyp-d3(bj)", "basis": "def2tzvp"},
    {"method": "b3lyp-d3-bj", "basis": "def2tzvp"},
    {"method": "b3lyp-gd3bj", "basis": "def2tzvp"},
    {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"},
    {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "GD3BJ"},
    {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "D3(BJ)"},
    {"method": "b3lyp", "basis": "def2-TZVP", "dispersion": "EmpiricalDispersion=GD3BJ"},
    {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd2"},
    {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd4"},
    {"method": "b3lyp-d4", "basis": "def2tzvp"},
    {"method": "b3lyp-gd4", "basis": "def2tzvp"},
    {"method": "b3lyp", "basis": "def2_tzvp"},
    {"method": "b3lyp", "basis": "6-311+G(d,p)"},
    {"method": "b3lyp", "basis": "6-311+g(d,p)", "software": "qchem"},
    {"method": "dsd-pbep86-2013", "basis": "def2tzvp"},
    {"method": "dsd-pbep86", "basis": "def2tzvp"},
    {"method": "dsdpbep862013", "basis": "def2tzvp"},
    {"method": "cbs-qb3"},
    {"method": "CBS-QB3", "method_type": "composite"},
    {"method": "g4"},
    {"method": "ccsd(t)", "basis": "cc-pvtz"},
    {"method": "ccsdt", "basis": "cc-pvtz"},
    {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"},
    {"method": "ccsd(t)f12", "basis": "ccpvtzf12"},
    {"method": "dlpno-ccsd(t)-f12", "basis": "cc-pvtz-f12"},
    {"method": "DLPNO-CCSD(T)-F12-2023", "basis": "cc-pVTZ-F12"},
    {"method": "dlpno-ccsd(t)", "basis": "def2-tzvp"},
    {"method": "apfd", "basis": "def2svp"},
    {"method": "bmk", "basis": "cbsb7"},
    {"method": "m062x", "basis": "def2tzvp", "dispersion": "gd3"},
    {"method": "m06-2x-d3", "basis": "def2tzvp"},
    {"method": "b3lyp-d3(0)", "basis": "def2tzvp"},
    {"method": "b3lyp-d3zero", "basis": "def2tzvp"},
    {"method": "wb97m-v", "basis": "def2tzvp"},
    {"method": "b3lyp_d3", "basis": "def2tzvp"},
    {"method": "revdsd-pbep86-d4", "basis": "def2tzvp"},
    {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "EmpiricalDispersion=GD2"},
]


def test_level_identity_matches_arc_normalization():
    """The ported normalizers agree with ARC 1977e53b's on every spelling in the corpus."""
    pytest.importorskip("arc")
    import arc.main
    if not hasattr(arc.main, "_normalized_method_and_basis"):
        pytest.skip("this ARC predates arc.main._normalized_method_and_basis (ARC 1977e53b)")
    from arc.level import Level
    from arc.output import _level_to_dict

    assert len(PARITY_LEVELS) >= 30
    # The hard-coded table must still be what this ARC produces.
    for spec, expected in ARC_LEVEL_IDENTITIES:
        assert arc.main._normalized_method_and_basis(Level(**spec)) == expected, spec
    arc_keys, adapter_keys = [], []
    for spec in PARITY_LEVELS:
        level = Level(**spec)
        arc_key = arc.main._normalized_method_and_basis(level)
        # The adapter reads the dict ARC writes to output.yml, and must agree
        # on the user's own spelling as well.
        assert _level_identity(_level_to_dict(level)) == arc_key, spec
        assert _level_identity(spec) == arc_key, spec
        arc_keys.append(arc_key)
        adapter_keys.append(_level_identity(spec))
    for i, j in itertools.combinations(range(len(PARITY_LEVELS)), 2):
        assert (adapter_keys[i] == adapter_keys[j]) == (arc_keys[i] == arc_keys[j]), (
            PARITY_LEVELS[i], PARITY_LEVELS[j])
    # Non-vacuous: the corpus has both matching and distinct pairs.
    assert len(set(arc_keys)) < len(arc_keys)
    assert ("ccsd(t)", "ccpvtz") in arc_keys and ("ccsdt", "ccpvtz") in arc_keys
