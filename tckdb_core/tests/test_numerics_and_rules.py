"""The producer-agnostic numerics and TCKDB rule pre-checks that moved out of the ARC adapter."""

import json
import logging
import math
from unittest import mock

import pytest
from tckdb_schemas import stationary_point as schemas_stationary_point

from tckdb_core import composition, isotopes, reaction_flatten, rmg_units, route_level, rules
from tckdb_core import software_release, thermo_numerics
from tckdb_core.physical_constants import E_h_kJmol
from tckdb_core.xyz import ConverterError, check_xyz_dict, xyz_from_data, xyz_to_str


# --- rmg_units -----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("cm^3/(mol*s)", "cm3_mol_s"), (" CM^3/(MOLECULE*S) ", "cm3_molecule_s"),
    ("1/s", "per_s"), ("s^-1", "per_s"), ("m^6/(mol^2*s)", "m6_mol2_s"),
    (None, None), ("", None), ("furlongs", None),
])
def test_a_units(text, expected):
    assert rmg_units.a_units_to_tckdb(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("kJ/mol", "kj_mol"), (" kcal/mol", "kcal_mol"), ("J/mol", "j_mol"), ("cal/mol", "cal_mol"),
    (None, None), ("eV", None),
])
def test_ea_units(text, expected):
    assert rmg_units.ea_units_to_tckdb(text) == expected


def test_unrecognised_units_log_a_debug_line_to_the_given_logger():
    sink = mock.Mock()
    assert rmg_units.a_units_to_tckdb("bogus", log=sink) is None
    assert rmg_units.ea_units_to_tckdb("bogus", log=sink) is None
    assert [c.args[0] for c in sink.debug.call_args_list] == [
        "TCKDB kinetics: unrecognized A_units %r; field will be omitted.",
        "TCKDB kinetics: unrecognized Ea_units %r; field will be omitted.",
    ]


# --- thermo_numerics -----------------------------------------------------------------

def _nasa(mid=1000.0):
    low = {"tmin_k": 200.0, "tmax_k": mid, "coeffs": [1.0 + i for i in range(7)]}
    high = {"tmin_k": mid, "tmax_k": 3000.0, "coeffs": [2.0 + i for i in range(7)]}
    return low, high


def test_nasa_block_shape_and_h298():
    block = thermo_numerics.build_nasa_block(*_nasa())
    assert list(block)[:3] == ["t_low", "t_mid", "t_high"]
    assert block["a1"] == 1.0 and block["b7"] == 8.0 and block["t_mid"] == 1000.0
    t = 298.15
    c = [block[f"a{i}"] for i in range(1, 8)]
    h_rt = c[0] + c[1] * t / 2 + c[2] * t**2 / 3 + c[3] * t**3 / 4 + c[4] * t**4 / 5 + c[5] / t
    assert thermo_numerics.nasa_h298_kj_mol(block) == pytest.approx(h_rt * 8.314462618 * t / 1000.0)


@pytest.mark.parametrize("broken", ["short", "mismatch", "bounds", "nonnumeric"])
def test_a_malformed_nasa_block_is_skipped_with_a_log(broken):
    low, high = _nasa()
    if broken == "short":
        low["coeffs"] = low["coeffs"][:6]
    elif broken == "mismatch":
        high["tmin_k"] = 900.0
    elif broken == "bounds":
        del high["tmax_k"]
    else:
        high["coeffs"][3] = "x"
    sink = mock.Mock()
    assert thermo_numerics.build_nasa_block(low, high, log=sink) is None
    assert sink.warning.call_count == 1


def test_thermo_points_drop_bad_rows_and_keep_good_ones():
    sink = mock.Mock()
    points = thermo_numerics.build_thermo_points([
        {"temperature_k": 300, "cp_j_mol_k": 30.0, "s_j_mol_k": "x"},
        {"temperature_k": 300}, {"temperature_k": -1}, {"cp_j_mol_k": 1}, "row",
        {"temperature_k": 400, "h_kj_mol": 1.5, "g_kj_mol": 2},
    ], log=sink)
    assert points == [{"temperature_k": 300.0, "cp_j_mol_k": 30.0},
                      {"temperature_k": 400.0, "h_kj_mol": 1.5, "g_kj_mol": 2.0}]
    assert sink.warning.call_count == 5


# --- reaction_flatten ----------------------------------------------------------------

def test_flatten_promotes_wrapped_results_and_guards_unknown_fields():
    calc = {"opt_result": {"converged": True, "n_steps": 3, "final_energy_hartree": -1.0},
            "freq_result": {"n_imag": 1, "modes": [
                {"mode_index": 1, "frequency_cm1": -500.0, "is_imaginary": True,
                 "imaginary_disposition": "reaction_coordinate"},
                {"mode_index": 2, "frequency_cm1": 100.0, "is_imaginary": False}]}}
    reaction_flatten.flatten_result_fields(calc)
    assert calc == {
        "opt_converged": True, "opt_n_steps": 3, "opt_final_energy_hartree": -1.0,
        "freq_n_imag": 1, "freq_frequencies_cm1": [-500.0, 100.0],
        "freq_imaginary_dispositions": {1: "reaction_coordinate"}}
    with pytest.raises(ValueError, match="_REACTION_FLAT_RESULT_FIELDS"):
        reaction_flatten.flatten_result_fields({"opt_result": {"surprise": 1}})
    with pytest.raises(ValueError, match="freq_result.modes carries field"):
        reaction_flatten.flatten_result_fields(
            {"freq_result": {"modes": [{"mode_index": 1, "frequency_cm1": 1.0, "surprise": 2}]}})


def test_flatten_all_walks_species_and_the_ts():
    bundle = {"species": [{"conformers": [{"calculation": {"sp_result": {"electronic_energy_hartree": -2.0}}}],
                           "calculations": [{"opt_result": {"converged": False}}]}],
              "transition_state": {"calculation": {"opt_result": {"converged": True}},
                                   "calculations": [{"sp_result": {"electronic_energy_hartree": -3.0}}]}}
    reaction_flatten.flatten_all_reaction_calcs(bundle)
    assert bundle["species"][0]["conformers"][0]["calculation"] == {"sp_electronic_energy_hartree": -2.0}
    assert bundle["species"][0]["calculations"] == [{"opt_converged": False}]
    assert bundle["transition_state"]["calculations"] == [{"sp_electronic_energy_hartree": -3.0}]


# --- composition and xyz -------------------------------------------------------------

def test_element_symbols_from_xyz_and_formula():
    assert composition.xyz_element_symbols("C 0 0 0\nh 1 0 0") == ("C", "H")
    assert composition.xyz_element_symbols("2\ncomment\nO 0 0 0\nH 0 0 1") == ("O", "H")
    assert composition.xyz_element_symbols("1 2 3") is None and composition.xyz_element_symbols(None) is None
    assert composition.formula_element_symbols("C2H5O") == ("C", "C", "H", "H", "H", "H", "H", "O")
    assert composition.formula_element_symbols("H2+") is None and composition.formula_element_symbols("H0") is None
    assert composition.formula(("H", "C", "H")) == "CH2"
    assert composition.is_single_atom_geometry("He 0 0 0") and not composition.is_single_atom_geometry("H 0 0 0\nH 0 0 1")


def test_xyz_helpers_round_trip():
    xyz = xyz_from_data(coords=[(0, 0, 0), (0, 0, 0.74)], symbols=["H", "H"])
    assert xyz["isotopes"] == (1, 1)
    text = xyz_to_str(xyz)
    assert text.splitlines()[0].startswith("H ") and len(text.splitlines()) == 2
    assert check_xyz_dict(None) is None
    with pytest.raises(ConverterError):
        check_xyz_dict("H 0 0 0")
    assert E_h_kJmol == pytest.approx(2625.4998583629967)


# --- isotopes and route_level --------------------------------------------------------

def test_isotope_substitutions_and_reconciliation():
    xyz = "C 0 0 0\nH 0 0 1\nH 0 1 0"
    assert isotopes.geometry_isotope_substitutions(xyz, [12, 1, 2]) == {3: 2}
    assert isotopes.geometry_isotope_substitutions(xyz, [12, 1]) is None
    assert isotopes.geometry_isotope_substitutions(xyz, [12, 1, 0]) is None
    assert isotopes.smiles_isotope_multiset("[2H]C") == {("H", 2): 1}
    assert isotopes.smiles_isotope_multiset("[12C]") == {}
    assert isotopes.smiles_isotope_multiset(None) is None
    declared = isotopes.smiles_isotope_multiset("[2H]C")
    ok = isotopes.isotope_reconciliation_error(
        "X", declared=declared, substitutions={3: 2}, xyz_text=xyz, producer_name="P", isotopes_field="f")
    assert ok is None
    bad = isotopes.isotope_reconciliation_error(
        "X", declared=declared, substitutions={}, xyz_text=xyz, producer_name="P", isotopes_field="f")
    assert "P states geometry isotope substitutions [] but the SMILES declares [('H', 2)]" in str(bad)
    unusable = isotopes.isotope_reconciliation_error(
        "X", declared=declared, substitutions=None, xyz_text=xyz, producer_name="P", isotopes_field="my_field")
    assert "(my_field is null" in str(unusable)
    assert isotopes.isotope_reconciliation_error(
        "X", declared=None, substitutions={3: 2}, xyz_text=xyz, producer_name="P", isotopes_field="f") is None


def test_geometry_payload_rule():
    xyz = "C 0 0 0\nH 0 0 1"
    assert isotopes.geometry_payload(xyz, [12, 1], isotopes_stated=False, record_xyz_text=xyz,
                                     record_substitutions=None, key="k") == {"xyz_text": xyz, "key": "k"}
    assert isotopes.geometry_payload(xyz, [12, 2], isotopes_stated=True, record_xyz_text=xyz,
                                     record_substitutions={2: 2}) == {"xyz_text": xyz, "isotopes": {"2": 2}}
    # a geometry that states other substitutions than the species' own is left out
    assert isotopes.geometry_payload(xyz, [12, 1], isotopes_stated=True, record_xyz_text=xyz,
                                     record_substitutions={2: 2}) is None
    assert isotopes.geometry_payload(xyz, [12, 1], isotopes_stated=True, record_xyz_text=xyz,
                                     record_substitutions={}) == {"xyz_text": xyz}


def test_route_vs_level():
    level = {"method": "B3LYP", "basis": "6-31G(d)"}
    assert route_level.route_method_basis("#p opt freq ub3lyp/6-31g(d) scf=tight") == ("ub3lyp", "6-31g(d)")
    assert route_level.route_method_basis("#cbs-qb3 opt") is None
    assert route_level.route_contradicts_level("#p ub3lyp/6-31g(d)", level) is None
    assert route_level.route_contradicts_level("#p b3lyp/def2tzvp", level) == \
        "route b3lyp/def2tzvp vs level B3LYP/6-31G(d)"
    assert route_level.route_contradicts_level("#p b3lyp/def2tzvp", {"method": "x"}) is None


# --- software_release ----------------------------------------------------------------

@pytest.mark.parametrize("name,banner,expected", [
    ("gaussian", "Gaussian 16, Revision C.02", {"version": "16", "revision": "C.02"}),
    ("orca", "ORCA 5.0.4", {"version": "5.0.4"}),
    ("molpro", "2022.3", {"version": "2022.3"}),
    ("gaussian", "ORCA 6.0.0", {"version": "ORCA 6.0.0"}),
    ("xtb", "xtb version 6.7.1 (edcfbbe) compiled by x", {"version": "6.7.1", "build": "edcfbbe"}),
])
def test_split_version_banner(name, banner, expected):
    assert software_release.split_version_banner(name, banner) == expected


# --- rules ---------------------------------------------------------------------------

def test_reaction_coordinate_window_rule():
    window = (75.0, 10000.0)
    assert rules.designate_reaction_coordinate_index([-1320.5, -12000.0], window_cm1=window) == 1
    assert rules.designate_reaction_coordinate_index([-300.0, -1200.0], window_cm1=window) == 2
    assert rules.designate_reaction_coordinate_index([-500.0, -500.0], window_cm1=window) is None
    assert rules.designate_reaction_coordinate_index([-50.0], window_cm1=window) is None


def test_tau_rule():
    assert rules.TAU_PROTOCOL_NOT_RECORDED_CM1 == 50.0
    assert rules.TAU_PROTOCOL_NOT_RECORDED_CM1 == schemas_stationary_point.TAU_PROTOCOL_NOT_RECORDED_CM1
    assert rules.designate_by_tau([(2, -800.0), (3, -20.0)], 2) == (2, 1)
    assert rules.designate_by_tau([(2, -800.0), (3, -60.0)], 2) == (None, 2)
    assert rules.designate_by_tau([(2, -30.0), (3, -20.0)], 2) == (None, 0)
    assert rules.designate_by_tau([(2, -800.0)], 2) == (None, 1)  # count disagrees with n_imag


def test_energy_level_declaration_follows_assert_role_consistency():
    sp_level = {"method": "wb97xd", "basis": "def2tzvp", "spin_treatment": "restricted"}
    opt_level = {"method": "b3lyp", "basis": "6-31g"}
    calcs = {"c_sp": {"level_of_theory": sp_level}, "c_opt": {"level_of_theory": opt_level}}
    roles = {"sp": "c_sp", "opt": "c_opt"}
    stated = {"method": "wb97xd", "basis": "def2tzvp"}
    declared = rules.energy_level_declaration(
        stated, energy_level_description="d", calc_keys_by_role=roles, calculations=calcs, producer_name="P")
    assert declared == ("sp", sp_level) and declared[1] is not sp_level
    # no sp linked: the opt's level
    assert rules.energy_level_declaration(
        opt_level, energy_level_description="d", calc_keys_by_role={"opt": "c_opt"},
        calculations=calcs, producer_name="P") == ("opt", opt_level)
    # a stated level that is another level than the linked calculation's: not declared, and logged
    sink = mock.Mock()
    assert rules.energy_level_declaration(
        {"method": "ccsd(t)"}, energy_level_description="CC", calc_keys_by_role=roles,
        calculations=calcs, producer_name="P", log=sink) is None
    sink.info.assert_called_once_with(
        "TCKDB energy level not declared: %s's energy level %s is not the level of the linked %s calculation.",
        "P", "CC", "sp")
    assert rules.energy_level_declaration(
        None, energy_level_description="", calc_keys_by_role=roles, calculations=calcs, producer_name="P") is None
    assert rules.energy_level_declaration(
        stated, energy_level_description="", calc_keys_by_role={}, calculations=calcs, producer_name="P") is None


def test_describe_level_and_calculations_by_key():
    assert rules.describe_level({"method": "b3lyp", "dispersion": "gd3bj", "basis": "6-31g"}) == "b3lyp + gd3bj/6-31g"
    assert rules.describe_level({}) == "an unrecorded level"
    assert rules.calculations_by_key({"key": "a"}, [{"key": "b"}, {"nokey": 1}], None) == {
        "a": {"key": "a"}, "b": {"key": "b"}}
