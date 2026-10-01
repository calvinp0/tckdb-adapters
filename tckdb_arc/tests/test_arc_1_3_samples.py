"""ARC's own three 1.3 sample documents (fixtures/arc_1_3_samples, see the README there).

Their thermo, kinetics, correction and switch numbers are placeholders, so these tests check structure
and plumbing: which key reaches which payload field, what is omitted when null. Numeric invariants are in
test_arc_schema_1_3_thermo.py, on records this suite controls. None of the documents has the
``parser_evidence.json`` it names, so every build here is the no-sidecar fallback.
"""

import copy
import json
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from tckdb_arc.adapter import (
    _build_kinetics_block,
    _build_statmech_block_for_species,
    _correction_records_from_record,
    _freq_result_payload,
    _scheme_data_revisions,
    _wavefunction_diagnostic_payload,
)
from test_thermo_enthalpy_declaration import _adapter

SAMPLES = Path(__file__).parent / "fixtures" / "arc_1_3_samples"


def _load(name):
    return yaml.safe_load((SAMPLES / f"{name}.output.yml").read_text())


def _species(doc, label):
    return copy.deepcopy(next(s for s in doc["species"] if s["label"] == label))


def _build(tmp_path, route, doc, label):
    assert not (tmp_path / "output" / "parser_evidence.json").exists()
    adapter = _adapter(tmp_path)
    submit = {"computed_species": adapter.submit_computed_species_from_output,
              "conformer": adapter.submit_from_output}[route]
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = submit(output_doc=copy.deepcopy(doc), species_record=_species(doc, label))
    return json.loads(outcome.payload_path.read_text()), outcome.warnings


def _calcs(payload, calc_type):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == calc_type and "software_release" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(payload)
    return found


ROUTES = ("computed_species", "conformer")


def test_the_samples_are_1_3_documents():
    for name in ("species_thermo", "reaction_kinetics", "legacy_restart"):
        assert _load(name)["schema_version"] == "1.3"


@pytest.mark.parametrize("route", ROUTES)
def test_a_rotor_species_states_the_treatment_arkane_applied(tmp_path, route):
    payload, warnings = _build(tmp_path, route, _load("species_thermo"), "iC3H7")
    statmech = payload["statmech"]
    assert statmech["statmech_treatment"] == "rrho_1d"
    assert [t["treatment_kind"] for t in statmech["torsions"]] == ["hindered_rotor", "hindered_rotor"]
    assert statmech["rigid_rotor_kind"] == "asymmetric_top"
    assert "statmech_treatment_not_stated" not in [w["code"] for w in warnings]


@pytest.mark.parametrize("route", ROUTES)
def test_the_sample_petersson_record_is_sent_with_its_components_and_database_release(tmp_path, route):
    doc = _load("species_thermo")
    # The sample's placeholder total (-1.94) is not the sum of its components (-1.9411); output 1.3 promises
    # that a real record's components sum to its total, so make the placeholder keep that promise.
    bac = next(c for c in _species(doc, "iC3H7")["energy_corrections"] if c["correction_type"] == "bond_additivity")
    for record in doc["species"]:
        for correction in record["energy_corrections"]:
            if correction["correction_type"] == "bond_additivity":
                correction["total"]["value"] = sum(c["contribution_value"] for c in bac["components"])
    payload, warnings = _build(tmp_path, route, doc, "iC3H7")
    roles = {c["application_role"]: c for c in payload["applied_energy_corrections"]}
    assert set(roles) == {"aec_total", "bac_total"}
    assert [c["key"] for c in roles["bac_total"]["components"]] == ["C-H", "C-C"]
    assert "bac_correction_omitted_components_incomplete" not in [w["code"] for w in warnings]
    # The Petersson record lists no skipped bond, so it has no skipped-bond note.
    assert "note" not in roles["bac_total"]
    for correction in roles.values():
        scheme = correction["scheme"]
        # The tables' revision is the scheme's identity (0.62); the Arkane build that read them is kept as provenance.
        assert scheme["data_revision"] == "4.0.0"
        assert scheme["workflow_tool_release"] == {
            "name": "Arkane", "version": "3.3.0", "git_commit": "6b1368de6c19204c7ce4fda6fbecb05da4a0fe0e"}


@pytest.mark.parametrize("route", ROUTES)
def test_a_species_whose_atom_corrections_were_off_deposits_no_correction(tmp_path, route):
    """1.3 drops the records of a run whose switch was false, so there is nothing to deposit; the enthalpy of
    that run is absolute and is stripped by the 1.2 flags, which 1.3 leaves unchanged."""
    doc = _load("species_thermo")
    nc3h7 = _species(doc, "nC3H7")
    assert nc3h7["energy_corrections"] == [] and nc3h7["thermo"]["atom_corrections_applied"] is False
    payload, warnings = _build(tmp_path, route, doc, "nC3H7")
    assert "applied_energy_corrections" not in payload
    if route == "computed_species":
        assert "h298_kj_mol" not in payload["thermo"]
        assert "enthalpy_atom_corrections_not_applied" in [w["code"] for w in warnings]
    # Its E0 switches are False too, but no E0 is deposited, so they gate nothing.
    assert "e0_" not in json.dumps(payload)


@pytest.mark.parametrize("route", ROUTES)
def test_a_species_arkane_output_was_not_parsed_for_states_no_treatment(tmp_path, route):
    payload, warnings = _build(tmp_path, route, _load("species_thermo"), "CH3NH2")
    assert "statmech_treatment" not in (payload.get("statmech") or {})
    (warning,) = [w for w in warnings if w["code"] == "statmech_treatment_not_stated"]
    assert warning["context"]["reason"] == "arkane_treatment_not_recorded"


@pytest.mark.parametrize("route", ROUTES)
def test_the_sample_t1_diagnostic_and_spin_diagnostic_reach_the_sp_calculation(tmp_path, route):
    doc = _load("species_thermo")
    payload, _ = _build(tmp_path, route, doc, "CH3NH2")
    (sp,) = _calcs(payload, "sp")
    assert sp["wavefunction_diagnostic"] == {"t1_diagnostic": pytest.approx(0.0086766)}
    payload, _ = _build(tmp_path, route, doc, "iC3H7")
    sp = next((c for c in _calcs(payload, "sp")), None)
    if sp is not None:
        assert "wavefunction_diagnostic" not in sp
        assert sp["spin_diagnostic"] == {"s_squared": 0.7646, "s_squared_expected": 0.75,
                                         "s_squared_annihilated": 0.7502}


def test_the_dipole_moment_of_the_sample_has_no_home(tmp_path):
    payload, _ = _build(tmp_path, "computed_species", _load("species_thermo"), "iC3H7")
    assert "dipole" not in json.dumps(payload).lower() and "0.2355" not in json.dumps(payload)


def _reaction(tmp_path, doc):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
            output_doc=copy.deepcopy(doc), reaction_record=copy.deepcopy(doc["reactions"][0]))
    return json.loads(outcome.payload_path.read_text()), outcome.warnings


def _ts(tmp_path, doc):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path).submit_computed_ts_from_output(
            output_doc=copy.deepcopy(doc), ts_record=copy.deepcopy(doc["transition_states"][0]),
            reaction_record=copy.deepcopy(doc["reactions"][0]))
    return json.loads(outcome.payload_path.read_text()), outcome.warnings


def test_the_sample_reaction_states_reversibility_and_the_kinetics_provenance(tmp_path):
    doc = _load("reaction_kinetics")
    payload, _ = _reaction(tmp_path, doc)
    assert payload["reversible"] is True
    (kinetics,) = payload["kinetics"]
    assert kinetics["note"].splitlines() == [
        "Fitted by Arkane over 300-2000 K.",
        "Fitted to 50 data points; dA = *|/ 1.48",
        "Arkane kinetics run atom energy corrections: applied.",
    ]
    assert kinetics["tunneling_model"] == "eckart"
    # ts_validation is null (no IRC failure found), so nothing about validation is claimed here.
    assert doc["reactions"][0]["kinetics"]["ts_validation"] is None


def test_the_sample_ts_frequencies_keep_the_ess_order_and_arcs_reaction_coordinate(tmp_path):
    doc = _load("reaction_kinetics")
    expected = doc["transition_states"][0]["freq_frequencies_cm1_ess_order"]
    assert len(expected) == 24 and expected[0] < 0
    payload, _ = _reaction(tmp_path, doc)
    ts = payload["transition_state"]
    (freq,) = [c for c in [ts["calculation"], *ts["calculations"]] if c["type"] == "freq"]
    assert freq["freq_frequencies_cm1"] == expected
    assert freq["freq_n_imag"] == 1 and freq["freq_reaction_coordinate_mode_index"] == 1
    assert "freq_imaginary_dispositions" not in freq
    standalone, _ = _ts(tmp_path, doc)
    (freq,) = [c for c in standalone["additional_calculations"] if c["type"] == "freq"]
    assert [m["frequency_cm1"] for m in freq["freq_result"]["modes"]] == expected
    assert [m["mode_index"] for m in freq["freq_result"]["modes"]] == list(range(1, 25))
    assert freq["freq_result"]["reaction_coordinate_mode_index"] == 1
    assert standalone["reaction"]["reversible"] is True


def test_a_ts_whose_normal_mode_check_did_not_establish_the_mode_is_still_uploaded_when_it_has_one_imaginary_mode(
        tmp_path):
    """README open question 2: a null ``reaction_coordinate_mode_index`` does not say why. With one imaginary
    mode there is nothing to designate, so the modes are sent in ESS order and no index is claimed."""
    doc = _load("reaction_kinetics")
    doc["transition_states"][0]["reaction_coordinate_mode_index"] = None
    standalone, _ = _ts(tmp_path, doc)
    (freq,) = [c for c in standalone["additional_calculations"] if c["type"] == "freq"]
    assert len(freq["freq_result"]["modes"]) == 24
    assert "reaction_coordinate_mode_index" not in freq["freq_result"]


def test_the_sample_ts_has_no_corrections_or_validation_note_to_carry(tmp_path):
    doc = _load("reaction_kinetics")
    assert doc["transition_states"][0]["energy_corrections"] == []
    payload, _ = _reaction(tmp_path, doc)
    assert "applied_energy_corrections" not in payload["transition_state"]


# The legacy document's records are built by no route (they name no level, so no calculation can be
# declared); the null pattern is exercised at the 1.3 readers themselves.


def test_the_legacy_null_pattern_is_read_without_inventing_anything():
    doc = _load("legacy_restart")
    for record in doc["species"]:
        statmech = record["statmech"]
        assert statmech["arkane_treatment"] is None and statmech["e0_atom_corrections_applied"] is None
        block = _build_statmech_block_for_species(
            output_doc=doc, species_record=record, calc_keys_by_role={"opt": "opt"},
            workflow_tool_release=None, target_model="StatmechInBundle", warnings=(warnings := []))
        assert "statmech_treatment" not in (block or {})
        assert not any(t.get("treatment_kind") for t in (block or {}).get("torsions", []))
        assert _wavefunction_diagnostic_payload(record) is None
        assert _correction_records_from_record(record, schema_1_3=True) == []
    ts = doc["transition_states"][0]
    assert ts["freq_frequencies_cm1_ess_order"] is None and ts["reaction_coordinate_mode_index"] is None
    assert _freq_result_payload(ts, schema_1_3=True) is None
    reaction = doc["reactions"][0]
    assert reaction["kinetics"] is None and reaction["reversible"] is True


def test_the_legacy_header_states_the_database_identity_it_always_emits():
    assert _scheme_data_revisions(_load("legacy_restart"))["bac_petersson"] == "4.0.0"


def test_the_sample_kinetics_block_reads_the_run_provenance_only_for_1_3():
    kinetics = _load("reaction_kinetics")["reactions"][0]["kinetics"]
    keys = dict(reactant_keys=["r"], product_keys=["p"], actor_calc_keys={}, ts_calc_keys={})
    assert "atom energy corrections" in _build_kinetics_block(
        kinetics_record=kinetics, schema_1_3=True, **keys)["note"]
    assert "note" not in _build_kinetics_block(kinetics_record=kinetics, schema_1_3=False, **keys)
