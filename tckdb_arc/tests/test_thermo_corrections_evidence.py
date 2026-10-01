"""Corrections and statmech claims are sent only as far as the evidence goes (adapter 0.6.3).

* A1: TCKDB refuses a ``bac_petersson`` ``bac_total`` with no bond component
  (``bac_total_requires_components``) unless it targets a monatomic species,
  and never checks that components sum to the total. ARC drops the whole
  component list when a bond lacks a parameter, so the adapter omits the BAC
  correction (keeping the AEC and the rest of the payload) and warns.
* A5: Arkane ignores every rotor when the freq log has no force-constant
  matrix, so ``statmech_treatment`` is named only with Hessian evidence.
* A12: ARC's ``reference_atom_energies`` are ``scheme.atom_params``.
"""

import json
import os
from unittest import mock

import pytest
import yaml

from tckdb_arc.adapter import (
    _build_applied_energy_corrections,
    _correction_records_from_record,
)
from tckdb_schemas.energy_correction import EnergyCorrectionSchemeRef

from test_adapter import (
    _aec_record,
    _mbac_record,
    _pbac_record,
    _reaction_output_doc,
    _reaction_record,
    _StubClient,
    _StubResponse,
)
from test_provenance_passthrough import FIXTURE, _adapter, _benzene, _submit

CODE = "bac_correction_omitted_components_incomplete"
BONDED = ("C", "H", "H", "H", "H")


def _build(records, **kwargs):
    warnings = []
    out = _build_applied_energy_corrections(
        records, source_calculation_key="sp", warnings=warnings, **kwargs)
    return out, warnings


def _componentless_pbac():
    rec = _pbac_record()
    rec["components"] = []
    return rec


def _roles(out):
    return [c["application_role"] for c in out]


# ---------------------------------------------------------------- A1 helper

@pytest.mark.parametrize("kwargs", [
    {"target_kind": "species", "element_symbols": BONDED},
    {"target_kind": "species", "element_symbols": None},  # composition unknown
    {"target_kind": "transition_state", "element_symbols": BONDED},
    {"target_kind": "transition_state", "element_symbols": ("O",)},
])
def test_componentless_petersson_bac_is_omitted_and_aec_kept(kwargs):
    out, warnings = _build([_aec_record(), _componentless_pbac()],
                           target_label="X", **kwargs)
    assert _roles(out) == ["aec_total"]
    [warning] = warnings
    assert warning["code"] == CODE
    assert warning["context"]["reason"] == "no_components"
    assert warning["context"]["species"] == "X"
    assert warning["context"]["target_kind"] == kwargs["target_kind"]


def test_monatomic_species_keeps_its_componentless_petersson_bac():
    out, warnings = _build([_aec_record(), _componentless_pbac()],
                           target_kind="species", element_symbols=("O",))
    assert _roles(out) == ["aec_total", "bac_total"]
    assert out[1]["components"] == []
    assert warnings == []


def test_partial_decomposition_omits_the_whole_bac_not_the_component():
    rec = _pbac_record()
    rec["components"].append(
        {"component_kind": "bond", "key": "C-C", "multiplicity": 1,
         "parameter_value": None, "parameter_unit": "kcal_mol",
         "contribution_value": None})
    out, warnings = _build([_aec_record(), rec], target_kind="species",
                           element_symbols=BONDED)
    assert _roles(out) == ["aec_total"]
    assert warnings[0]["context"]["reason"] == "component_unusable"


def test_partial_decomposition_is_omitted_even_for_a_monatomic_species():
    rec = _pbac_record()
    rec["components"][0]["contribution_value"] = None
    out, warnings = _build([rec], target_kind="species", element_symbols=("O",))
    assert out == [] and warnings[0]["context"]["reason"] == "component_unusable"


def test_non_bond_placeholder_component_does_not_stand_in_for_a_bond():
    rec = _pbac_record()
    rec["components"] = [{"component_kind": "other", "key": "unspecified", "multiplicity": 1,
                          "parameter_value": 0.0, "contribution_value": 0.0}]
    out, warnings = _build([rec], target_kind="species", element_symbols=BONDED)
    assert out == [] and warnings[0]["context"]["reason"] == "no_bond_component"


def test_complete_petersson_bac_is_sent_unchanged_without_warning():
    out, warnings = _build([_pbac_record()], target_kind="species", element_symbols=BONDED)
    assert _roles(out) == ["bac_total"] and len(out[0]["components"]) == 1
    assert warnings == []


def test_melius_and_aec_are_not_subject_to_the_petersson_rule():
    # TCKDB exempts bac_melius; an AEC has no bond decomposition.
    aec = _aec_record(with_components=False)
    out, warnings = _build([aec, _mbac_record()], target_kind="transition_state")
    assert _roles(out) == ["aec_total", "bac_total"]
    assert warnings == []


# ------------------------------------------------------------ A1 on routes

def _bac(payload):
    return [c for c in payload["applied_energy_corrections"]
            if c["application_role"] == "bac_total"]


@pytest.mark.parametrize("variant", ["arc_drops_all", "null_parameter"])
def test_benzene_species_route_omits_an_incomplete_bac(tmp_path, variant):
    doc, record = _benzene(tmp_path)
    bac = next(c for c in record["energy_corrections"] if c["correction_type"] == "bond_additivity")
    if variant == "arc_drops_all":
        bac["components"] = None  # ARC arc/output.py:1592-1596
    else:
        bac["components"][0]["parameter_value"] = None
    adapter = _adapter(tmp_path, mode="computed_species")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_species_from_output(output_doc=doc, species_record=record)
    payload = json.loads(outcome.payload_path.read_text())
    assert [c["scheme"]["kind"] for c in payload["applied_energy_corrections"]] == ["atom_energy"]
    [warning] = [w for w in outcome.warnings if w["code"] == CODE]
    assert warning["field"] == "applied_energy_corrections"
    assert warning["context"]["species"] == record["label"]
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
    # The rest of the payload is untouched.
    assert "thermo" in payload and "statmech" in payload


def test_benzene_complete_bac_is_still_sent(tmp_path):
    payload, outcome = _submit(tmp_path, "computed_species")
    [bac] = _bac(payload)
    assert [c["key"] for c in bac["components"]] == ["C-C", "C-H", "C=C"]
    assert CODE not in [w["code"] for w in outcome.warnings]


def _reaction_submit(tmp_path, doc):
    client = _StubClient(response=_StubResponse({"reaction_id": 42}))
    adapter = _adapter(tmp_path, mode="computed_reaction", client=client)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=_reaction_record())
    return outcome, json.loads(outcome.payload_path.read_text())


def test_reaction_route_omits_componentless_bac_on_species_and_ts(tmp_path):
    doc = _reaction_output_doc()
    for sp in doc["species"]:
        sp["applied_energy_corrections"] = [_aec_record(), _componentless_pbac()]
    doc["transition_states"][0]["applied_energy_corrections"] = [
        _aec_record(), _componentless_pbac()]
    outcome, payload = _reaction_submit(tmp_path, doc)
    for sp in payload["species"]:
        assert _roles(sp["applied_energy_corrections"]) == ["aec_total"], sp["key"]
    ts = payload["transition_state"]
    assert _roles(ts["applied_energy_corrections"]) == ["aec_total"]
    warnings = [w for w in outcome.warnings if w["code"] == CODE]
    fields = sorted(w["field"] for w in warnings)
    assert fields == sorted(
        [f"species[{sp['key']}].applied_energy_corrections" for sp in payload["species"]]
        + ["transition_state.applied_energy_corrections"])
    assert {w["context"]["target_kind"] for w in warnings} == {"species", "transition_state"}
    assert all(w["context"]["reason"] == "no_components" for w in warnings)


def test_reaction_route_keeps_complete_bacs(tmp_path):
    doc = _reaction_output_doc()
    for sp in doc["species"]:
        sp["applied_energy_corrections"] = [_aec_record(), _pbac_record()]
    outcome, payload = _reaction_submit(tmp_path, doc)
    for sp in payload["species"]:
        assert _roles(sp["applied_energy_corrections"]) == ["aec_total", "bac_total"]
    assert CODE not in [w["code"] for w in outcome.warnings]


# ------------------------------------------------------------------- A5

def _statmech_payload(tmp_path, *, evidence):
    doc, record = _benzene(tmp_path)
    if not evidence:
        (tmp_path / "output" / "parser_evidence.json").unlink()
    adapter = _adapter(tmp_path, mode="computed_species")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_species_from_output(output_doc=doc, species_record=record)
    return json.loads(outcome.payload_path.read_text()), outcome


def test_benzene_with_hessian_evidence_names_its_treatment(tmp_path):
    payload, outcome = _statmech_payload(tmp_path, evidence=True)
    assert payload["statmech"]["statmech_treatment"] == "rrho"
    assert "statmech_treatment_not_stated" not in [w["code"] for w in outcome.warnings]


def test_benzene_rrho_needs_no_hessian_evidence(tmp_path):
    # Benzene has no rotors: plain RRHO is Arkane's treatment either way.
    payload, outcome = _statmech_payload(tmp_path, evidence=False)
    assert payload["statmech"]["statmech_treatment"] == "rrho"
    assert payload["statmech"]["external_symmetry"] == 12
    assert "statmech_treatment_not_stated" not in [w["code"] for w in outcome.warnings]
    [freq] = [c for c in payload["conformers"][0]["additional_calculations"]
              if c["type"] == "freq"]
    assert "hessian" not in freq


# ------------------------------------------------------------------ A12

def test_benzene_atom_energy_scheme_carries_atom_params_from_reference_atom_energies(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    aec = next(c for c in payload["applied_energy_corrections"]
               if c["application_role"] == "aec_total")
    doc = yaml.safe_load((FIXTURE / "output.yml").read_text())
    table = next(c for c in doc["species"][0]["energy_corrections"]
                 if c["correction_type"] == "atom_energy")["reference_atom_energies"]
    assert table["unit"] == "hartree"
    assert aec["scheme"]["units"] == "hartree"
    assert aec["scheme"]["atom_params"] == [
        {"element": k, "value": v} for k, v in sorted(table["values"].items())]
    assert len(aec["scheme"]["atom_params"]) == 8
    # The applied total and components are ARC's own numbers, never rebuilt
    # from the reference energies.
    assert aec["value"] == pytest.approx(232.30728491146994)
    assert {c["key"]: c["multiplicity"] for c in aec["components"]} == {"C": 6, "H": 6}
    EnergyCorrectionSchemeRef.model_validate(aec["scheme"] | {"level_of_theory": None})


def test_legacy_verbatim_scheme_atom_params_still_pass_through():
    rec = _aec_record()
    rec["scheme"]["atom_params"] = [{"element": "H", "value": -0.5}]
    [out] = _build_applied_energy_corrections([rec])
    assert out["scheme"]["atom_params"] == [{"element": "H", "value": -0.5}]


def test_neutral_petersson_parameter_table_still_maps_to_bond_params():
    [rec] = _correction_records_from_record({"energy_corrections": [{
        "correction_type": "bond_additivity", "model": "petersson",
        "total": {"value": -0.7, "unit": "kcal_mol"},
        "parameter_table": {"unit": "kcal_mol", "values": {"C-H": -0.2}},
    }]})
    assert rec["scheme"]["bond_params"] == [{"bond_key": "C-H", "value": -0.2}]
    assert "atom_params" not in rec["scheme"]


# ------------------------------------------------- Arkane as workflow tool

def test_benzene_table_schemes_name_arkane_as_workflow_tool(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    for correction in payload["applied_energy_corrections"]:
        assert correction["scheme"]["workflow_tool_release"] == {
            "name": "Arkane", "version": "4.0.0",
            "git_commit": "e6f47b425c9dc37ee65c072fac46c932aa265603"}


def test_arkane_workflow_tool_release_only_from_recorded_fields():
    from tckdb_arc.adapter import _arkane_workflow_tool_release as release
    assert release({}) is None
    assert release({"arkane_version": "4.0.0"}) == {"name": "Arkane", "version": "4.0.0"}
    assert release({"arkane_git_commit": "abc"}) == {"name": "Arkane", "git_commit": "abc"}
    # An over-long commit is not a valid git_commit; it is not truncated.
    assert release({"arkane_git_commit": "a" * 41}) is None


def test_workflow_tool_release_on_arkane_table_schemes_and_never_overwritten():
    aec, mbac = _aec_record(with_components=False), _mbac_record()
    aec["scheme"]["workflow_tool_release"] = {"name": "Custom"}
    out, _ = _build([aec, _pbac_record(), mbac], target_kind="species",
                    element_symbols=BONDED, arkane_release={"name": "Arkane", "version": "4"})
    by_kind = {c["scheme"]["kind"]: c["scheme"] for c in out}
    assert by_kind["atom_energy"]["workflow_tool_release"] == {"name": "Custom"}
    assert by_kind["bac_petersson"]["workflow_tool_release"] == {"name": "Arkane", "version": "4"}
    assert by_kind["bac_melius"]["workflow_tool_release"] == {"name": "Arkane", "version": "4"}


# ------------------------------------------------- thermo note for omitted BAC

def _species_with_bac_omitted(tmp_path, *, flag="absent"):
    doc, record = _benzene(tmp_path)
    # The fixture is output 1.2 and records the flag; drop it for the
    # "absent" case, or set it.
    if flag == "absent":
        record["thermo"].pop("bond_corrections_applied", None)
    else:
        record["thermo"]["bond_corrections_applied"] = flag
    bac = next(c for c in record["energy_corrections"] if c["correction_type"] == "bond_additivity")
    bac["components"] = None
    adapter = _adapter(tmp_path, mode="computed_species")
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_species_from_output(output_doc=doc, species_record=record)
    return json.loads(outcome.payload_path.read_text())


def test_thermo_note_states_what_arc_exported_when_flag_absent(tmp_path):
    payload = _species_with_bac_omitted(tmp_path)
    note = payload["thermo"]["note"]
    assert "not deposited as an applied correction" in note and "no_components" in note
    # No 1.2 flag: state what ARC exported, do not assert Arkane applied it.
    assert note.startswith("ARC exported a Petersson bond additivity correction total")
    assert "applied by Arkane" not in note


@pytest.mark.parametrize("flag,expected", [(True, "applied by Arkane"), (False, None)])
def test_thermo_note_follows_bond_corrections_applied_flag(tmp_path, flag, expected):
    payload = _species_with_bac_omitted(tmp_path, flag=flag)
    if expected is None:
        assert "note" not in payload["thermo"]
    else:
        assert expected in payload["thermo"]["note"]
        assert "not deposited" in payload["thermo"]["note"]


def test_thermo_has_no_note_when_bac_is_deposited(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    assert "note" not in payload["thermo"]


def test_thermo_note_appends_to_an_existing_note():
    from tckdb_arc.adapter import _note_omitted_bac_on_thermo
    block = {"note": "kept"}
    _note_omitted_bac_on_thermo(block, ["no_components"])
    assert block["note"].startswith("kept; ARC exported a Petersson")
    _note_omitted_bac_on_thermo(None, ["x"])  # no thermo: nothing to annotate


def test_reaction_species_thermo_note_for_omitted_bac(tmp_path):
    doc = _reaction_output_doc()
    for sp in doc["species"]:
        sp["applied_energy_corrections"] = [_aec_record(), _componentless_pbac()]
        sp["thermo"] = {"h298_kj_mol": -10.0, "s298_j_mol_k": 200.0}
    outcome, payload = _reaction_submit(tmp_path, doc)
    for sp in payload["species"]:
        if "thermo" in sp:
            assert "not deposited" in sp["thermo"]["note"], sp["key"]


def test_atom_energy_scheme_records_the_subtraction_convention(tmp_path):
    payload, _ = _submit(tmp_path, "computed_species")
    aec = next(c for c in payload["applied_energy_corrections"]
               if c["application_role"] == "aec_total")
    # The subtraction is structured (0.62 ``atom_params_applied_as``); the note keeps what that field
    # does not carry: Arkane's per-atom addend (atom_hf - atom_thermal) is not an atom_param.
    assert aec["scheme"]["atom_params_applied_as"] == "subtracted"
    note = aec["scheme"]["note"]
    assert "subtracts" not in note
    assert "adds each atom's gas-phase formation enthalpy" in note
    assert "atom_hf - atom_thermal" in note and "not atom_params" in note
    bac = next(c for c in payload["applied_energy_corrections"]
               if c["application_role"] == "bac_total")
    assert "note" not in bac["scheme"]
