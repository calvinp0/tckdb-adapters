"""Thermo and statmech declare ``energy_level_of_theory`` (adapter 0.6.6, roadmap A11).

TCKDB checks a declared energy level against the calculations the record links
(``calculation_levels.assert_role_consistency``): the linked sp's level, else the
linked opts'. A level's identity hashes ``spin_treatment`` (NULL folds to
``"unknown"``) and the adapter stamps ``spin_treatment`` on the sp's level from
ARC's ``scf_reference``, so a declaration built from ARC's bare level would be a
different level row and a 422 (``thermo_energy_level_contradiction`` /
``statmech_energy_level_contradiction``). The declaration is the linked energy
calculation's own ``level_of_theory``, and the tests replay the backend's
identity and link rules (``_backend_level_rules``), which the offline contract
hook does not run.
"""

import copy
import os

import pytest

from _backend_level_rules import calculations_by_key, energy_level_verdict, level_hash
from tckdb_arc.adapter import _energy_level_declaration

from test_adapter import _fake_output_doc, _full_record, _reaction_record
from test_provenance_passthrough import _benzene, _submit
from test_thermo_enthalpy_declaration import _adapter, _reaction_doc_with_thermo

import json
from unittest import mock

STATMECH = {"external_symmetry": 1, "optical_isomers": 1, "is_linear": False,
            "rigid_rotor_kind": "asymmetric_top", "point_group": "C1"}


def _species_calcs(payload):
    conformer = payload["conformers"][0]
    return calculations_by_key(
        conformer["primary_calculation"], conformer["additional_calculations"])


def _open_shell(record, *, freq="unrestricted", sp="unrestricted"):
    record["scf_reference"] = {"freq_reference": freq, "sp_reference": sp}
    return record


def _computed_species(tmp_path, record, doc=None):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_species_from_output(
            output_doc=doc or _fake_output_doc(), species_record=record)
    return json.loads(outcome.payload_path.read_text())


def _blocks(payload):
    return [payload[k] for k in ("thermo", "statmech") if k in payload]


# ---------------------------------------------------------------- the trap


def test_benzene_declares_the_sp_calculations_own_level_with_spin_treatment(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    calcs = _species_calcs(payload)
    sp_level = calcs["sp"]["level_of_theory"]
    # ARC recorded restricted references for benzene's freq and sp jobs.
    assert sp_level["spin_treatment"] == "restricted"
    for name in ("thermo", "statmech"):
        assert payload[name]["energy_level_of_theory"] == sp_level
        assert energy_level_verdict(payload[name], calcs) is None
    # The trap: the bare level ARC ran at is another level row for TCKDB.
    bare = {k: v for k, v in sp_level.items() if k != "spin_treatment"}
    assert level_hash(bare) != level_hash(sp_level)
    assert energy_level_verdict(
        {**payload["thermo"], "energy_level_of_theory": bare}, calcs
    ) == "energy_level_contradiction"


def test_benzene_conformer_mode_statmech_declares_the_sp_level(tmp_path):
    payload, _ = _submit(tmp_path, "conformer")
    calcs = calculations_by_key(payload["calculation"], payload["additional_calculations"])
    sp_level = calcs["sp"]["level_of_theory"]
    assert sp_level["spin_treatment"] == "restricted"
    assert payload["statmech"]["energy_level_of_theory"] == sp_level
    assert energy_level_verdict(payload["statmech"], calcs) is None


def test_open_shell_species_declares_the_unrestricted_sp_level(tmp_path):
    record = _open_shell(_full_record(), freq="unrestricted", sp="restricted_open")
    record["statmech"] = dict(STATMECH)
    payload = _computed_species(tmp_path, record)
    calcs = _species_calcs(payload)
    assert calcs["sp"]["level_of_theory"]["spin_treatment"] == "restricted_open"
    assert calcs["freq"]["level_of_theory"]["spin_treatment"] == "unrestricted"
    for block in _blocks(payload):
        assert block["energy_level_of_theory"] == calcs["sp"]["level_of_theory"]
        assert energy_level_verdict(block, calcs) is None


def test_reaction_participants_declare_their_own_sp_level(tmp_path):
    doc = _reaction_doc_with_thermo()
    for record in doc["species"]:
        _open_shell(record, sp="unrestricted" if record["multiplicity"] == 2 else "restricted")
        record["statmech"] = dict(STATMECH)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=_reaction_record())
    payload = json.loads(outcome.payload_path.read_text())
    seen = 0
    for species in payload["species"]:
        calcs = calculations_by_key(species["conformers"][0]["calculation"], species["calculations"])
        for name in ("thermo", "statmech"):
            block = species[name]
            assert block["energy_level_of_theory"] == calcs[block["source_calculations"][-1]["calculation_key"]]["level_of_theory"]
            assert energy_level_verdict(block, calcs) is None
            seen += 1
    assert seen == 8


def test_levels_with_dispersion_and_solvation_are_declared_whole(tmp_path):
    doc = _fake_output_doc()
    doc["opt_level"] = {"method": "wb97xd", "basis": "Def2TZVP", "software": "gaussian",
                        "dispersion": "gd3bj", "solvent": "water", "solvation_method": "smd"}
    doc["sp_level"] = dict(doc["opt_level"], method="wb97m-v", basis="def2-QZVP")
    record = _open_shell(_full_record(), sp="unrestricted")
    record["statmech"] = dict(STATMECH)
    payload = _computed_species(tmp_path, record, doc)
    calcs = _species_calcs(payload)
    declared = payload["thermo"]["energy_level_of_theory"]
    assert declared == calcs["sp"]["level_of_theory"]
    assert declared["method"] == "wb97m-v" and declared["spin_treatment"] == "unrestricted"
    assert {"dispersion": "gd3bj", "solvent": "water", "solvent_model": "smd"}.items() <= declared.items()
    for block in _blocks(payload):
        assert energy_level_verdict(block, calcs) is None


# ------------------------------------------------- when nothing is declared


def test_no_sp_calculation_declares_the_opt_level(tmp_path):
    record = _full_record()
    del record["sp_energy_hartree"]
    record["statmech"] = dict(STATMECH)
    payload = _computed_species(tmp_path, record)
    calcs = _species_calcs(payload)
    assert "sp" not in calcs
    assert payload["statmech"]["energy_level_of_theory"] == calcs["opt"]["level_of_theory"]
    assert energy_level_verdict(payload["statmech"], calcs) is None
    # thermo has no sp either, and enthalpy may be stripped, but the level agrees.
    for block in _blocks(payload):
        assert energy_level_verdict(block, calcs) is None


def test_composite_method_is_not_declared(tmp_path):
    # A composite level is not sent as a calculation; declaring it would
    # contradict the sp/opt levels actually linked.
    doc = _fake_output_doc()
    doc["composite_method"] = {"method": "cbs-qb3", "software": "gaussian"}
    record = _full_record()
    record["statmech"] = dict(STATMECH)
    payload = _computed_species(tmp_path, record, doc)
    assert all("energy_level_of_theory" not in block for block in _blocks(payload))


def test_sp_level_whose_sp_was_not_built_is_not_declared(tmp_path):
    doc = _fake_output_doc()
    doc["sp_level"] = {"method": "ccsd(t)", "basis": "cc-pvtz", "software": "gaussian"}
    record = _full_record()
    del record["sp_energy_hartree"]
    record["statmech"] = dict(STATMECH)
    payload = _computed_species(tmp_path, record, doc)
    assert all("energy_level_of_theory" not in block for block in _blocks(payload))


# ------------------------------------------------------------ helper rules

OPT = {"key": "opt", "level_of_theory": {"method": "m", "basis": "b"}}
SP = {"key": "sp", "level_of_theory": {"method": "m", "basis": "b", "spin_treatment": "restricted"}}


@pytest.mark.parametrize("level,keys,calcs,expected", [
    (None, {"opt": "opt", "sp": "sp"}, {"opt": OPT, "sp": SP}, None),
    ({"method": "m", "basis": "b"}, {"opt": "opt", "sp": "sp"}, {"opt": OPT, "sp": SP},
     ("sp", SP["level_of_theory"])),
    ({"method": "m", "basis": "b"}, {"opt": "opt"}, {"opt": OPT}, ("opt", OPT["level_of_theory"])),
    # No energy calculation linked: nothing to check the declaration against.
    ({"method": "m", "basis": "b"}, {"freq": "freq"}, {"freq": OPT}, None),
    # ARC's energy level is another method than the linked calculation's.
    ({"method": "other", "basis": "b"}, {"opt": "opt", "sp": "sp"}, {"opt": OPT, "sp": SP}, None),
    # A field the calculation's level lacks (dispersion) is a different level too.
    ({"method": "m", "basis": "b", "dispersion": "gd3bj"}, {"sp": "sp"}, {"sp": SP}, None),
])
def test_declaration_is_the_linked_calculations_level_or_nothing(level, keys, calcs, expected):
    assert _energy_level_declaration(
        level, calc_keys_by_role=keys, calculations=calcs) == expected


def test_declaration_is_a_copy():
    role, declared = _energy_level_declaration(
        {"method": "m", "basis": "b"}, calc_keys_by_role={"sp": "sp"}, calculations={"sp": SP})
    declared["method"] = "changed"
    assert SP["level_of_theory"]["method"] == "m"


def test_declaration_needs_a_stated_energy_level():
    # ARC adaptive_levels run whose species level cannot be attributed.
    assert _energy_level_declaration(
        None, calc_keys_by_role={"sp": "sp"}, calculations={"sp": SP}) is None


# ------------------------------------------------- replicated backend rules


def test_hash_ignores_basis_spelling_and_folds_spin_to_unknown():
    assert level_hash({"method": "m", "basis": "Def2TZVP"}) == level_hash(
        {"method": "m", "basis": "def2-tzvp", "spin_treatment": "unknown"})
    assert level_hash({"method": "m"}) != level_hash({"method": "m", "spin_treatment": "restricted"})


def test_hash_matches_the_backend():
    from tckdb_core.testing.backend_import import backend_level_hash
    for level in (
        {"method": "wb97xd", "basis": "Def2TZVP"},
        {"method": "b3lyp", "basis": "def2tzvp", "spin_treatment": "restricted"},
        {"method": "m", "basis": "cc-pVTZ", "dispersion": "gd3bj", "solvent": "water",
         "solvent_model": "smd", "keywords": "k"},
        # DLPNO-style level with auxiliary and CABS basis sets.
        {"method": "dlpno-ccsd(t)", "basis": "def2-TZVP", "aux_basis": "def2-TZVP/C",
         "cabs_basis": "cc-pVTZ-F12-CABS", "spin_treatment": "unrestricted"},
        # 0.67-0.69 identity rules: composite aliases, folded dispersion, core treatment.
        {"method": "cbsqb3"},
        {"method": "g4(mp2)"},
        {"method": "b3lyp-d3(bj)", "basis": "def2tzvp"},
        {"method": "CCSD(T)", "basis": "cc-pCVTZ", "core_treatment": "frozen_core"},
    ):
        assert level_hash(level) == backend_level_hash(level), level
