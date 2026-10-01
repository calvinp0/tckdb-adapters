"""Interim enthalpy checks for thermo without ARC's atom-correction flag.

ARC output.yml 1.0/1.1 (and species Arkane loaded from YAML under 1.2)
record no ``thermo.atom_corrections_applied``. Besides the non-finite and
magnitude checks, the adapter then strips enthalpy it cannot verify as
formation_298k: a light species the magnitude guard cannot catch, a header
``arkane_level_of_theory`` that is not the energy level, and a dispersion or
solvation field on either level. Output.yml 1.2's flag supersedes them.
"""

import copy
import json
import os
from unittest import mock

from _contract import contract_validate
import pytest

from tckdb_arc.adapter import (
    _build_thermo_block,
    _formula_element_symbols,
    _species_element_symbols,
    _xyz_element_symbols,
)
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.workflows.computed_reaction_upload import (
    BundleThermoIn,
    ComputedReactionUploadRequest,
)
from tckdb_schemas.workflows.computed_species_upload import (
    ComputedSpeciesUploadRequest,
    ThermoInBundle,
)

from test_adapter import _fake_output_doc, _full_record, _reaction_output_doc, _reaction_record
from test_thermo_enthalpy_declaration import _adapter

APFD = {"method": "apfd", "basis": "def2svp", "software": "gaussian"}
BMK = {"method": "bmk", "basis": "cbsb7", "method_type": "dft", "software": "gaussian"}
WB97XD = {"method": "wb97xd", "basis": "def2tzvp", "method_type": "dft", "software": "gaussian"}
XYZ = {
    "H": "H 0.0 0.0 0.0",
    "H2": "H 0.0 0.0 0.0\nH 0.0 0.0 0.74",
    "H3": "H 0.0 0.0 -0.93\nH 0.0 0.0 0.0\nH 0.0 0.0 0.93",
    "He": "He 0.0 0.0 0.0",
    "Li": "Li 0.0 0.0 0.0",
    "HeH": "He 0.0 0.0 0.0\nH 0.0 0.0 0.77",
    "LiH": "Li 0.0 0.0 0.0\nH 0.0 0.0 1.6",
    "CH4": ("C 0.0 0.0 0.0\nH 0.63 0.63 0.63\nH -0.63 -0.63 0.63\n"
            "H -0.63 0.63 -0.63\nH 0.63 -0.63 -0.63"),
    "H2O": "O 0.0 0.0 0.12\nH 0.0 0.76 -0.47\nH 0.0 -0.76 -0.47",
    "H15": "\n".join(f"H 0.0 0.0 {i}.0" for i in range(15)),
    "H16": "\n".join(f"H 0.0 0.0 {i}.0" for i in range(16)),
}
# Formation-magnitude thermo, as ARC 1.1 writes it (no correction flags).
# It records the standard-state pressure, so these tests see only the
# enthalpy findings (the pressure omission is tested on its own).
THERMO = {
    "h298_kj_mol": -74.6, "s298_j_mol_k": 186.3, "tmin_k": 100.0, "tmax_k": 5000.0,
    "standard_state_pressure_pa": 101325.0,
    "nasa_low": {"tmin_k": 100.0, "tmax_k": 1000.0,
                 "coeffs": [4.1, -1e-3, 2e-6, -1e-9, 4e-13, -9000.0, 1.0]},
    "nasa_high": {"tmin_k": 1000.0, "tmax_k": 5000.0,
                  "coeffs": [3.6, 1e-3, -2e-7, 1e-11, -3e-15, -8900.0, 5.0]},
    "thermo_points": [
        {"temperature_k": 300.0, "cp_j_mol_k": 35.7, "h_kj_mol": -74.5,
         "s_j_mol_k": 186.5, "g_kj_mol": -130.5},
        {"temperature_k": 1000.0, "cp_j_mol_k": 71.8, "h_kj_mol": -36.1,
         "s_j_mol_k": 247.9, "g_kj_mol": -284.0},
    ],
}
STRIPPED = {
    "s298_j_mol_k": 186.3, "tmin_k": 100.0, "tmax_k": 5000.0,
    "points": [{"temperature_k": 300.0, "cp_j_mol_k": 35.7, "s_j_mol_k": 186.5},
               {"temperature_k": 1000.0, "cp_j_mol_k": 71.8, "s_j_mol_k": 247.9}],
    "reference_pressure_bar": 1.01325,
}
HEADER = "output_header.arkane_level_of_theory"


def _build(*, species="CH4", energy_level=WB97XD, header=None, thermo=None,
           target="ThermoInBundle"):
    warnings = []
    block = _build_thermo_block(
        copy.deepcopy(thermo or THERMO), calc_keys_by_role={}, target_model=target,
        warnings=warnings, energy_level=energy_level, header_corrections_level=header,
        element_symbols=_xyz_element_symbols(XYZ[species]))
    return block, warnings


def _shape(warnings):
    return [{k: w[k] for k in ("code", "field", "context")} for w in warnings]


def _warning(code, level_source="not_recorded", field="thermo"):
    return {"code": code, "field": field, "context": {
        "source": "tckdb_arc_self_check", "action": "thermo_enthalpy_omitted",
        "atom_corrections_applied": "not_recorded", "corrections_level_source": level_source}}


def _assert_stripped(block):
    assert block == STRIPPED
    assert enthalpy_reference_error(block) is None
    contract_validate(ThermoInBundle, block)
    contract_validate(BundleThermoIn, block)


def _assert_declared(block, warnings):
    assert warnings == []
    assert block["enthalpy_reference_kind"] == "formation_298k"
    assert block["h298_kj_mol"] == -74.6
    assert "nasa" in block
    assert [p["h_kj_mol"] for p in block["points"]] == [-74.5, -36.1]


# --- composition ------------------------------------------------------------

@pytest.mark.parametrize("xyz,expected", [
    ("H 0.0 0.0 0.0", ("H",)),
    ("C 0 0 0\nH 1 0 0\n", ("C", "H")),
    ("2\ncomment line\nLi 0 0 0\nH 0 0 1.6", ("Li", "H")),
    ("HE 0 0 0", ("He",)),
    (None, None),
    ("", None),
    ({"symbols": ("H",)}, None),
    ("1 0 0 0\nH 0 0 0", None),
])
def test_xyz_element_symbols(xyz, expected):
    assert _xyz_element_symbols(xyz) == expected


@pytest.mark.parametrize("formula,expected", [
    ("H", ("H",)),
    ("H2", ("H", "H")),
    ("He", ("He",)),
    ("CH4", ("C", "H", "H", "H", "H")),
    ("C2H5O", ("C", "C", "H", "H", "H", "H", "H", "O")),
    ("H10", ("H",) * 10),
    (None, None),
    ("", None),
    ("H+", None),
    ("CH3(OH)", None),
    ("h2", None),
    ("[2H]2", None),
    ("H02", None),
    ("H 2", None),
])
def test_formula_element_symbols(formula, expected):
    assert _formula_element_symbols(formula) == expected


def test_composition_prefers_xyz_over_formula():
    assert _species_element_symbols({"xyz": XYZ["CH4"], "formula": "H"}) == ("C", "H", "H", "H", "H")
    assert _species_element_symbols({"xyz": None, "formula": "H2"}) == ("H", "H")
    assert _species_element_symbols({"formula": "Li"}) == ("Li",)
    assert _species_element_symbols({"xyz": None, "formula": None}) is None


def _build_record(record, **kwargs):
    warnings = []
    block = _build_thermo_block(
        copy.deepcopy(THERMO), calc_keys_by_role={}, target_model="ThermoInBundle",
        warnings=warnings, energy_level=WB97XD,
        element_symbols=_species_element_symbols(record), **kwargs)
    return block, warnings


# --- rule 1: light species ----------------------------------------------------

def test_monoatomic_without_xyz_uses_its_formula():
    # ARC output.yml 1.0 writes xyz: null for monoatomic species (they skip opt).
    block, warnings = _build_record({"label": "H", "xyz": None, "formula": "H"})
    _assert_stripped(block)
    assert _shape(warnings) == [_warning("enthalpy_formation_unverifiable_light_species")]


@pytest.mark.parametrize("formula", ["CH4", "H+", "H(1)", None])
def test_heavy_or_unparsable_formula_without_xyz_is_not_light(formula):
    block, warnings = _build_record({"xyz": None, "formula": formula})
    _assert_declared(block, warnings)


@pytest.mark.parametrize("species", ["H", "H2", "H3", "He", "Li", "HeH", "H15"])
def test_light_species_enthalpy_is_stripped(species):
    block, warnings = _build(species=species)
    _assert_stripped(block)
    assert _shape(warnings) == [_warning("enthalpy_formation_unverifiable_light_species")]
    assert "within the 20000 kJ/mol magnitude bound" in warnings[0]["message"]


def test_light_species_message_names_the_formula():
    _block, warnings = _build(species="H3")
    assert "this H3 species' raw total energy (about 3.94e+03 kJ/mol)" in warnings[0]["message"]


@pytest.mark.parametrize("species", ["LiH", "CH4", "H2O", "H16"])
def test_species_the_magnitude_guard_covers_are_not_light(species):
    block, warnings = _build(species=species)
    _assert_declared(block, warnings)


def test_unknown_composition_is_not_light():
    warnings = []
    block = _build_thermo_block(copy.deepcopy(THERMO), calc_keys_by_role={},
                                target_model="ThermoInBundle", warnings=warnings,
                                energy_level=WB97XD, element_symbols=None)
    _assert_declared(block, warnings)


# --- rule 2: header arkane_level_of_theory -----------------------------------

def test_stand_in_header_level_strips_enthalpy():
    # ARC's examples/Stationary/bde: bmk/cbsb7 atom energies for apfd/def2svp energies.
    block, warnings = _build(energy_level=APFD, header=BMK)
    _assert_stripped(block)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch", HEADER)]
    assert "arkane_level_of_theory bmk/cbsb7" in warnings[0]["message"]
    assert "apfd/def2svp" in warnings[0]["message"]


def test_header_level_with_unknown_energy_level_strips():
    block, warnings = _build(energy_level=None, header=WB97XD)
    _assert_stripped(block)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch", HEADER)]


@pytest.mark.parametrize("energy_level,header", [
    (WB97XD, dict(WB97XD)),
    ({"method": "wB97X-D", "basis": "def2-TZVP"}, WB97XD),
    ({"method": "B3LYP-D3(BJ)", "basis": "def2-TZVP", "software": "gaussian"},
     {"method": "b3lyp-d3bj", "basis": "def2tzvp", "software": "qchem", "year": 2023}),
    ({"method": "b3lyp", "basis": "def2tzvp"}, {"method": "b3lyp2023", "basis": "def2tzvp"}),
    ({"method": "CBS-QB3", "method_type": "composite"}, {"method": "cbs-qb3", "software": "gaussian"}),
], ids=["identical", "hyphens_case", "dispersion_suffix_software_year", "refit_year", "composite"])
def test_matching_header_level_declares_formation_298k(energy_level, header):
    block, warnings = _build(energy_level=energy_level, header=header)
    _assert_declared(block, warnings)


@pytest.mark.parametrize("header", [None, {}, {"basis": "cbsb7"}],
                         ids=["null", "empty", "no_method"])
def test_unrecorded_header_level_is_not_checked(header):
    block, warnings = _build(energy_level=APFD, header=header)
    _assert_declared(block, warnings)


# --- rule 3: dispersion / solvation fields -----------------------------------

@pytest.mark.parametrize("energy_level,header,side,field,level_source", [
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"},
     {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"},
     "energy level", "dispersion", HEADER),
    ({"method": "b3lyp-d3bj", "basis": "def2tzvp"},
     {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"},
     "arkane_level_of_theory", "dispersion", HEADER),
    ({"method": "b3lyp", "basis": "def2tzvp", "solvation_method": "smd", "solvent": "water"},
     {"method": "b3lyp", "basis": "def2tzvp"}, "energy level", "solvation_method", HEADER),
    ({"method": "b3lyp", "basis": "def2tzvp"},
     {"method": "b3lyp", "basis": "def2tzvp", "solvation_method": "smd", "solvent": "water"},
     "arkane_level_of_theory", "solvation_method", HEADER),
    ({"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"}, None,
     "energy level", "dispersion", "not_recorded"),
    ({"method": "b3lyp", "basis": "def2tzvp", "solvation_method": "smd"}, None,
     "energy level", "solvation_method", "not_recorded"),
], ids=["dispersion_both", "dispersion_header", "solvation_energy", "solvation_header",
        "dispersion_no_header", "solvation_no_header"])
def test_dispersion_or_solvation_field_is_unverifiable(energy_level, header, side, field,
                                                       level_source):
    block, warnings = _build(energy_level=energy_level, header=header)
    _assert_stripped(block)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_unverifiable", level_source)]
    assert warnings[0]["message"].startswith(f"The {side} ")
    assert f"sets {field}=" in warnings[0]["message"]


# --- precedence: non-finite, magnitude, mismatch, unverifiable, light ---------

def test_magnitude_outranks_the_interim_rules():
    thermo = copy.deepcopy(THERMO)
    thermo["h298_kj_mol"] = -106_404.6
    block, warnings = _build(species="H2", energy_level=APFD, header=BMK, thermo=thermo)
    assert "h298_kj_mol" not in block
    assert [w["code"] for w in warnings] == ["enthalpy_not_formation_magnitude"]
    assert warnings[0]["context"] == {"source": "tckdb_arc_self_check",
                                      "action": "thermo_enthalpy_omitted"}


def test_mismatch_outranks_unverifiable_and_light():
    energy_level = dict(APFD, dispersion="gd3bj")
    _block, warnings = _build(species="H2", energy_level=energy_level, header=BMK)
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch", HEADER)]


def test_unverifiable_outranks_light():
    level = {"method": "b3lyp", "basis": "def2tzvp", "dispersion": "gd3bj"}
    _block, warnings = _build(species="H2", energy_level=level, header=dict(level))
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_unverifiable", HEADER)]


def test_light_species_with_matching_header_notes_the_header():
    _block, warnings = _build(species="H", header=WB97XD)
    assert _shape(warnings) == [_warning("enthalpy_formation_unverifiable_light_species", HEADER)]


# --- the output.yml 1.2 flags are unaffected ----------------------------------

def test_true_flag_ignores_the_interim_rules():
    thermo = dict(copy.deepcopy(THERMO), atom_corrections_applied=True,
                  atom_corrections_level=dict(APFD))
    block, warnings = _build(species="H2", energy_level=APFD, header=BMK, thermo=thermo)
    _assert_declared(block, warnings)


def test_false_flag_keeps_its_own_warning():
    thermo = dict(copy.deepcopy(THERMO), atom_corrections_applied=False)
    block, warnings = _build(species="H2", energy_level=APFD, header=BMK, thermo=thermo)
    assert block == STRIPPED
    assert [w["context"] for w in warnings] == [
        {"source": "tckdb_arc_self_check", "action": "thermo_enthalpy_omitted"}]
    assert warnings[0]["code"] == "enthalpy_atom_corrections_not_applied"


# --- through the upload routes, with 1.1-shaped documents ---------------------

def _doc_1_1(**header):
    doc = _fake_output_doc()
    doc.update(schema_version="1.1", composite_method=None, sp_level=dict(APFD),
               arkane_level_of_theory=dict(APFD))
    doc.update(header)
    return doc


def _submit_species(tmp_path, doc, record):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    contract_validate(ComputedSpeciesUploadRequest, payload)
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
    return payload["thermo"], outcome.warnings


def _heavy_record(xyz=XYZ["CH4"]):
    record = _full_record()
    record.update(label="CH4", smiles="C", xyz=xyz, thermo=copy.deepcopy(THERMO))
    return record


def test_heavy_species_with_matching_header_declares_full_thermo(tmp_path):
    thermo, warnings = _submit_species(tmp_path, _doc_1_1(), _heavy_record())
    assert warnings == []
    assert thermo["enthalpy_reference_kind"] == "formation_298k"
    assert thermo["h298_kj_mol"] == -74.6
    assert thermo["reference_pressure_bar"] == 1.01325
    assert "nasa" in thermo and all("h_kj_mol" in p for p in thermo["points"])


def test_species_route_reads_the_header_level(tmp_path):
    thermo, warnings = _submit_species(tmp_path, _doc_1_1(arkane_level_of_theory=BMK),
                                       _heavy_record())
    assert "h298_kj_mol" not in thermo and "enthalpy_reference_kind" not in thermo
    assert _shape(warnings) == [_warning("enthalpy_atom_corrections_level_mismatch", HEADER)]


def test_species_route_reads_the_composition(tmp_path):
    thermo, warnings = _submit_species(tmp_path, _doc_1_1(), _heavy_record(XYZ["H2"]))
    assert "h298_kj_mol" not in thermo
    assert _shape(warnings) == [_warning("enthalpy_formation_unverifiable_light_species", HEADER)]


def _submit_reaction(tmp_path, doc, **reaction_keys):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
            output_doc=doc, reaction_record={**_reaction_record(), **reaction_keys})
    payload = json.loads(outcome.payload_path.read_text())
    contract_validate(ComputedReactionUploadRequest, payload)
    return payload, outcome.warnings


def _reaction_doc_1_1(**header):
    doc = _reaction_output_doc()
    doc.update(schema_version="1.1", composite_method=None, sp_level=dict(APFD),
               arkane_level_of_theory=dict(APFD))
    doc.update(header)
    for sp in doc["species"]:
        sp["thermo"] = copy.deepcopy(THERMO)
    return doc


def test_reaction_route_reads_the_header_level_and_composition(tmp_path):
    payload, warnings = _submit_reaction(tmp_path, _reaction_doc_1_1())
    keys = [sp["key"] for sp in payload["species"]]
    assert warnings == []
    assert all(sp["thermo"]["enthalpy_reference_kind"] == "formation_298k"
               for sp in payload["species"])

    payload, warnings = _submit_reaction(tmp_path / "standin",
                                         _reaction_doc_1_1(arkane_level_of_theory=BMK))
    assert all("h298_kj_mol" not in sp["thermo"] for sp in payload["species"])
    assert _shape(warnings) == [
        _warning("enthalpy_atom_corrections_level_mismatch", HEADER, field=f"species[{key}].thermo")
        for key in keys]

    doc = _reaction_doc_1_1()
    doc["species"][0]["xyz"] = XYZ["H"]
    # ARC states the participants (1.3), so the adapter does not balance-check the
    # deliberately unbalanced light-species swap.
    payload, warnings = _submit_reaction(
        tmp_path / "light", doc,
        atom_map_reactant_labels=_reaction_record()["reactant_labels"],
        atom_map_product_labels=_reaction_record()["product_labels"])
    # The atom's primary is its sp (tckdb-schemas 0.59), so it draws no placeholder-opt warning.
    assert not any("placeholder" in w["code"] for w in warnings)
    light = [sp for sp in payload["species"] if "h298_kj_mol" not in sp["thermo"]]
    assert light and len(light) < len(payload["species"])
    assert {w["code"] for w in warnings} == {"enthalpy_formation_unverifiable_light_species"}
    assert [w["field"] for w in warnings] == [f"species[{sp['key']}].thermo" for sp in light]
