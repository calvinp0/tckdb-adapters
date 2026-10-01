"""ARC output schema 1.3 at PR #1059 head ebc88ec8: the keys that changed after the first 1.3 draft.

* ``reactant_species_labels`` / ``product_species_labels``: the order and repeats of ``reactant_keys`` /
  ``product_keys`` on both reaction routes (the earlier draft's recovery from ``atom_map_*_labels`` /
  ``irc_participant_mapping`` stays as the fallback);
* ``ts_atom_map``: TCKDB's reaction ``atom_map`` (``atom_to_ts``) on both routes;
* ``nmd_forced``: a forced normal-mode-displacement pass is sent as ``mode_displacement_agrees: False`` (the check ran and failed), never ``True``;
* ``conformer_ess_software`` / ``conformer_ess_version``: screened conformers are filed with the program
  ARC observed in each conformer's own log;
* ``conformer_energies`` ``null`` entries, ``standard_state_pressure_pa`` and the ``bac_type`` check.

Fixtures: ``fixtures/arc_1_3_ts_atom_map`` (derived by hand from a real document, see its README) and
``fixtures/arc_1_3_samples_ebc88ec8`` (ARC's real writer at ebc88ec8). Both are validated here against ARC's
JSON schema, vendored at ``fixtures/arc_output_yml_schema_ebc88ec8.json`` (``ARC_OUTPUT_YML_SCHEMA`` may override it).
"""

import copy
import json
import os
from pathlib import Path

import pytest
import yaml

from _contract import contract_validate
from tckdb_arc.adapter import (
    TCKDBAdapter,
    _freq_result_payload,
    _ts_imaginary_mode_validation_evidence,
    _with_stated_participants,
)
from tckdb_arc.config import TCKDBConfig
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

FIXTURES = Path(__file__).parent / "fixtures"
DERIVED = FIXTURES / "arc_1_3_ts_atom_map" / "output.yml"
SAMPLES = FIXTURES / "arc_1_3_samples_ebc88ec8"
ARC_SCHEMA = FIXTURES / "arc_output_yml_schema_ebc88ec8.json"
NOT_SENT = "reaction_ts_atom_map_not_sent"
OLD_NOT_SENT = "reaction_atom_map_ts_order_not_stated"


def _no_routes(doc):
    """The staged logs behind these documents are other levels' logs; see test_arc_1_3_reactions."""
    for record in (*doc["species"], *doc["transition_states"]):
        for key in ("opt_route", "freq_route", "sp_route"):
            if key in record:
                record[key] = None
        if record.get("irc_log_routes"):
            record["irc_log_routes"] = [None] * len(record["irc_log_routes"])
    return doc


def _derived():
    return _no_routes(copy.deepcopy(yaml.safe_load(DERIVED.read_text())))


def _sample(name):
    return _no_routes(yaml.safe_load((SAMPLES / f"{name}.output.yml").read_text()))


def _adapter():
    return TCKDBAdapter(
        TCKDBConfig(enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=".",
                    api_key_env="X_TCKDB_API_KEY", project_label="proj-1-3",
                    upload_mode="computed_reaction", upload=False),
        project_directory=".")


def _bundle(doc, index=0):
    warnings = []
    payload = _adapter()._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][index], warnings=warnings)
    return payload, warnings


def _standalone(doc, index=0):
    warnings = []
    request = _adapter()._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][index],
        reaction_record=doc["reactions"][index], warnings=warnings)
    return request, warnings


def _codes(warnings):
    return [w["code"] for w in warnings]


def _by_slot(atom_map):
    return {(p["side"], p["participant_index"]): p for p in atom_map["participants"]}


# ---------------------------------------------------------------------------
# the fixtures are valid ARC documents (and say what their README says)
# ---------------------------------------------------------------------------

def _arc_schema():
    return json.loads(Path(os.environ.get("ARC_OUTPUT_YML_SCHEMA") or ARC_SCHEMA).read_text())


@pytest.mark.parametrize("path", [DERIVED, SAMPLES / "species_thermo.output.yml",
                                  SAMPLES / "reaction_kinetics.output.yml"], ids=lambda p: p.parent.name)
def test_fixtures_validate_against_arcs_json_schema(path):
    schema = _arc_schema()
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(schema).iter_errors(yaml.safe_load(path.read_text())))
    assert not errors, [e.message[:200] for e in errors[:5]]


def test_the_derived_fixture_obeys_the_semantic_rules_the_schema_cannot_express():
    doc = yaml.safe_load(DERIVED.read_text())
    species = {s["label"]: s for s in doc["species"]}
    ts = {t["label"]: t for t in doc["transition_states"]}
    for rxn in doc["reactions"]:
        sizes = {"reactants": [], "products": []}
        for side in sizes:
            sizes[side] = [len(species[label]["xyz"].splitlines()) for label in rxn[f"atom_map_{side[:-1]}_labels"]]
            assert rxn[f"{side[:-1]}_species_labels"] == rxn[f"atom_map_{side[:-1]}_labels"]
        tsm, amap = rxn["ts_atom_map"], rxn["atom_map"]
        n = len(amap)
        assert sum(sizes["reactants"]) == sum(sizes["products"]) == n == len(ts[rxn["ts_label"]]["xyz"].splitlines())
        assert sorted(tsm["reactants"]) == sorted(tsm["products"]) == list(range(n))
        assert all(tsm["products"][amap[i]] == tsm["reactants"][i] for i in range(n))
        assert tsm["ts_atom_order_follows_reactants"] == (tsm["reactants"] == list(range(n)))


# ---------------------------------------------------------------------------
# reactant_species_labels / product_species_labels
# ---------------------------------------------------------------------------

def test_species_labels_give_the_order_and_repeats_of_the_keys_on_both_routes():
    doc = _derived()
    payload, _ = _bundle(doc, 0)
    assert payload["reactant_keys"] == ["r0_OH", "r1_CH4"]          # ARC's species order, not the sorted labels
    assert payload["product_keys"] == ["p0_H2O", "p1_CH3"]
    payload, warnings = _bundle(doc, 1)
    assert payload["reactant_keys"] == ["r0_CH3", "r0_CH3"]          # one block, referenced twice
    assert [s["key"] for s in payload["species"]] == ["r0_CH3", "p0_C2H6"]
    assert "reaction_stoichiometry_not_stated" not in _codes(warnings)
    request, _ = _standalone(doc, 1)
    assert [m["species_entry"]["smiles"] for m in request["reaction"]["reactants"]] == ["[CH3]", "[CH3]"]
    contract_validate(ComputedReactionUploadRequest, payload)
    contract_validate(TransitionStateUploadRequest, request)


def test_species_labels_are_the_source_even_when_the_collapsed_lists_would_balance():
    """No repeat is guessed from a label and no stated repeat is lost: the stated order is sent as it is."""
    doc = _derived()
    record = doc["reactions"][0]
    record["reactant_species_labels"] = ["OH", "CH4"]
    stated = _with_stated_participants(record, ts_record=None, species_index={})
    assert stated["reactant_labels"] == ["OH", "CH4"] and record["reactant_labels"] == ["CH4", "OH"]


def test_the_earlier_draft_without_the_species_labels_still_recovers_repeats_from_the_atom_map_labels():
    doc = _derived()
    record = doc["reactions"][1]
    del record["reactant_species_labels"], record["product_species_labels"]
    assert _with_stated_participants(record, ts_record=None, species_index={})["reactant_labels"] == ["CH3", "CH3"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(reactant_species_labels=["CH3", "CH4"]),            # other species than reactant_labels
    lambda r: r.update(atom_map_reactant_labels=["CH3", "C2H6"]),           # disagrees with atom_map_reactant_labels
    lambda r: r.update(reactant_species_labels=["CH3", 3]),                 # not labels
    lambda r: r.pop("product_species_labels"),                              # one side only
])
def test_species_labels_that_contradict_the_record_refuse_the_reaction_with_a_warning(mutate):
    doc = _derived()
    mutate(doc["reactions"][1])
    warnings = []
    with pytest.raises(ValueError, match="contradicted"):
        _adapter()._build_computed_reaction_payload(
            output_doc=doc, reaction_record=doc["reactions"][1], warnings=warnings)
    assert "reaction_species_labels_contradicted" in _codes(warnings)
    with pytest.raises(ValueError, match="contradicted"):
        _adapter()._compose_transition_state_request(
            output_doc=doc, ts_record=doc["transition_states"][1], reaction_record=doc["reactions"][1])


def test_species_labels_that_disagree_only_in_order_with_the_atom_map_labels_are_refused():
    doc = _derived()
    doc["reactions"][0]["reactant_species_labels"] = ["CH4", "OH"]
    with pytest.raises(ValueError, match="atom_map_reactant_labels"):
        _bundle(doc, 0)


# ---------------------------------------------------------------------------
# ts_atom_map -> TCKDB atom_map
# ---------------------------------------------------------------------------

def test_ts_atom_map_becomes_the_bundle_atom_map_with_a_ts_order_unlike_the_reactants():
    doc = _derived()
    payload, warnings = _bundle(doc, 0)
    atom_map = payload["atom_map"]
    contract_validate(ComputedReactionUploadRequest, payload)
    assert atom_map["source"] == "inferred"
    assert atom_map["ts_geometry_key"] == payload["transition_state"]["geometry"]["key"]
    slots = _by_slot(atom_map)
    # OH (O H): TS atoms 6, 7; CH4 (C H H H H): TS atoms 1, 5, 2, 3, 4 (1-based); the products as in the fixture.
    assert slots[("reactant", 1)]["atom_to_ts"] == {"1": 6, "2": 7}
    assert slots[("reactant", 2)]["atom_to_ts"] == {"1": 1, "2": 5, "3": 2, "4": 3, "5": 4}
    assert slots[("product", 1)]["atom_to_ts"] == {"1": 6, "2": 7, "3": 5}
    assert slots[("product", 2)]["atom_to_ts"] == {"1": 1, "2": 2, "3": 3, "4": 4}
    assert [(p["species_key"], p["geometry_key"]) for p in atom_map["participants"]][:2] == [
        ("r0_OH", "r0_OH_geom"), ("r1_CH4", "r1_CH4_geom")]
    # the IRC mapping is also sent and the two agree (otherwise TCKDB blocks atom_map_contradicts_irc_mapping)
    irc = next(e for e in payload["transition_state"]["validation_evidence"] if e["kind"] == "irc")
    assert irc["reactant_participant_mapping"] == {"reactant:1": [6, 7], "reactant:2": [1, 2, 3, 4, 5]}
    assert not [w for w in warnings if w["code"] in (NOT_SENT, OLD_NOT_SENT)]


def test_the_note_names_the_method_and_quotes_arcs_symmetry_convention():
    note = _bundle(_derived())[0]["atom_map"]["note"]
    assert "irc_endpoint_cgr_isomorphism" in note and "endpoint 1" in note
    assert "constitutional (2D) correspondence" in note
    assert "symmetry-equivalent atoms, including diastereotopic ones, are assigned by a deterministic convention" in note


def test_a_repeated_reactant_is_two_participants_of_one_species_block_with_different_maps():
    payload, warnings = _bundle(_derived(), 1)
    contract_validate(ComputedReactionUploadRequest, payload)
    slots = _by_slot(payload["atom_map"])
    first, second = slots[("reactant", 1)], slots[("reactant", 2)]
    assert first["species_key"] == second["species_key"] == "r0_CH3"
    assert first["geometry_key"] == second["geometry_key"]
    assert first["atom_to_ts"] == {"1": 2, "2": 3, "3": 4, "4": 5}
    assert second["atom_to_ts"] == {"1": 1, "2": 6, "3": 7, "4": 8}
    assert slots[("product", 1)]["atom_to_ts"] == {"1": 2, "2": 1, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8}
    assert not [w for w in warnings if w["code"] in (NOT_SENT, OLD_NOT_SENT)]


def test_the_standalone_route_sends_the_same_map_with_participant_keys_and_geometries():
    doc = _derived()
    bundle, _ = _bundle(doc, 0)
    request, warnings = _standalone(doc, 0)
    contract_validate(TransitionStateUploadRequest, request)
    assert request["geometry_key"] == request["atom_map"]["ts_geometry_key"]
    members = [*request["reaction"]["reactants"], *request["reaction"]["products"]]
    assert [m["key"] for m in members] == ["reactant_1", "reactant_2", "product_1", "product_2"]
    assert len({m["geometry"]["key"] for m in members}) == 4
    assert [p["species_key"] for p in request["atom_map"]["participants"]] == [m["key"] for m in members]
    assert ([p["atom_to_ts"] for p in request["atom_map"]["participants"]]
            == [p["atom_to_ts"] for p in bundle["atom_map"]["participants"]])
    assert request["atom_map"]["note"] == bundle["atom_map"]["note"]
    assert not [w for w in warnings if w["code"] in (NOT_SENT, OLD_NOT_SENT)]


def test_the_standalone_repeated_reactant_has_unique_keys_per_slot():
    request, _ = _standalone(_derived(), 1)
    contract_validate(TransitionStateUploadRequest, request)
    assert [m["key"] for m in request["reaction"]["reactants"]] == ["reactant_1", "reactant_2"]
    assert request["atom_map"]["participants"][1]["atom_to_ts"] == {"1": 1, "2": 6, "3": 7, "4": 8}


def test_ts_atom_map_of_the_regenerated_real_sample_is_sent_on_both_routes():
    doc = _sample("reaction_kinetics")
    payload, warnings = _bundle(doc)
    contract_validate(ComputedReactionUploadRequest, payload)
    request, _ = _standalone(doc)
    contract_validate(TransitionStateUploadRequest, request)
    assert [p["atom_to_ts"] for p in request["atom_map"]["participants"]] == [
        p["atom_to_ts"] for p in payload["atom_map"]["participants"]]
    assert payload["atom_map"]["participants"][1]["atom_to_ts"]["1"] == 3       # ARC's own map, products permuted
    assert "reaction_ts_atom_map_not_sent" not in _codes(warnings)
    assert "endpoint 2" in payload["atom_map"]["note"]


def test_the_earlier_draft_without_ts_atom_map_keeps_the_omission_and_its_warning():
    doc = _derived()
    for key in ("ts_atom_map", "ts_atom_map_unavailable_reason"):
        del doc["reactions"][0][key]
    payload, warnings = _bundle(doc)
    assert "atom_map" not in payload and OLD_NOT_SENT in _codes(warnings) and NOT_SENT not in _codes(warnings)
    request, standalone_warnings = _standalone(doc)
    assert "atom_map" not in request and "geometry_key" not in request
    assert "key" not in request["reaction"]["reactants"][0] and OLD_NOT_SENT in _codes(standalone_warnings)


def test_a_null_ts_atom_map_is_omitted_with_arcs_reason_in_the_warning_context():
    doc = _derived()
    doc["reactions"][0].update(ts_atom_map=None, ts_atom_map_unavailable_reason="endpoint_perception_mismatch")
    for route in (_bundle, _standalone):
        result, warnings = route(doc)
        assert "atom_map" not in result
        (warning,) = [w for w in warnings if w["code"] == NOT_SENT]
        assert warning["context"]["ts_atom_map_unavailable_reason"] == "endpoint_perception_mismatch"
        assert "endpoint_perception_mismatch" in warning["message"]
        assert OLD_NOT_SENT not in _codes(warnings)


def test_no_atom_map_at_all_says_nothing_new():
    doc = _derived()
    doc["reactions"][0].update(atom_map=None, atom_map_reactant_labels=None, atom_map_product_labels=None,
                               ts_atom_map=None, ts_atom_map_unavailable_reason="no_atom_map")
    payload, warnings = _bundle(doc)
    assert "atom_map" not in payload and NOT_SENT not in _codes(warnings)


def _corrupt(path_to_value):
    def mutate(rxn):
        path_to_value(rxn["ts_atom_map"], rxn)
    return mutate


REFUSALS = {
    "short block": (lambda m, r: m["reactants"].pop(), "block lengths"),
    "index out of range": (lambda m, r: m["reactants"].__setitem__(0, 99), "one-to-one"),
    "repeated TS atom": (lambda m, r: m["products"].__setitem__(0, m["products"][1]), "one-to-one"),
    "element changes": (lambda m, r: (m["reactants"].__setitem__(0, 0), m["reactants"].__setitem__(2, 5)), None),
    "contradicts atom_map": (lambda m, r: r["atom_map"].reverse(), "atom_map"),
    "wrong TS": (lambda m, r: m.update(ts_label="TS9"), "transition state"),
    "wrong method": (lambda m, r: m.update(method="other"), "documented shape"),
    "follows flag lies": (lambda m, r: m.update(ts_atom_order_follows_reactants=True), None),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_a_ts_atom_map_that_fails_a_check_is_not_sent_and_says_why(name):
    mutate, expected = REFUSALS[name]
    doc = _derived()
    rxn = doc["reactions"][0]
    mutate(rxn["ts_atom_map"], rxn)
    for route in (_bundle, _standalone):
        result, warnings = route(doc)
        assert "atom_map" not in result, name
        (warning,) = [w for w in warnings if w["code"] == NOT_SENT]
        if expected:
            assert expected in warning["message"]


def test_an_atom_whose_element_differs_from_its_ts_atom_is_refused():
    doc = _derived()
    rxn = doc["reactions"][0]
    ts_map = rxn["ts_atom_map"]
    # exchange the TS atoms of an O (reactant atom 0) and a C (reactant atom 2) on both sides consistently
    for side in ("reactants", "products"):
        values = ts_map[side]
        i, j = values.index(5), values.index(0)
        values[i], values[j] = values[j], values[i]
    for route in (_bundle, _standalone):
        result, warnings = route(doc)
        assert "atom_map" not in result
        assert "is O but the transition-state atom" in next(w for w in warnings if w["code"] == NOT_SENT)["message"]


def test_a_ts_atom_map_that_contradicts_the_sent_irc_participant_mapping_is_not_sent():
    # an element-preserving contradiction: the IRC mapping gives the first CH3 the other H atoms
    doc = _derived()
    mapping = doc["transition_states"][1]["irc_participant_mapping"]
    first, second = mapping["reactants"]["participants"]
    first["atom_indices"], second["atom_indices"] = [1, 5, 6, 7], [0, 2, 3, 4]
    payload, warnings = _bundle(doc, 1)
    assert "atom_map" not in payload
    assert "contradicts the IRC participant mapping" in next(w for w in warnings if w["code"] == NOT_SENT)["message"]
    request, _ = _standalone(doc, 1)
    assert "atom_map" not in request


def test_when_the_irc_mapping_is_not_sent_the_ts_atom_map_still_is():
    doc = _derived()
    doc["transition_states"][0]["irc_participant_mapping"]["sides_distinguishable"] = False
    payload, warnings = _bundle(doc, 0)
    irc = next(e for e in payload["transition_state"]["validation_evidence"] if e["kind"] == "irc")
    assert "reactant_participant_mapping" not in irc and "atom_map" in payload
    assert "ts_irc_participant_mapping_not_sent" in _codes(warnings)


# ---------------------------------------------------------------------------
# nmd_forced
# ---------------------------------------------------------------------------

def _ts(**overrides):
    record = {"ts_checks": {"freq": True, "NMD": True}, "freq_n_imag": 1, "is_ts": True}
    record.update(overrides)
    return record


FREQ = {"n_imag": 1, "imag_freq_cm1": 1000.0, "modes": [{"mode_index": 1, "frequency_cm1": -1000.0,
                                                          "is_imaginary": True}],
        "reaction_coordinate_mode_index": 1}


def _evidence(record, freq=FREQ):
    warnings = []
    out = _ts_imaginary_mode_validation_evidence(
        record, freq_calc_key="ts_freq", freq_result=freq, ts_label="TS", warnings=warnings)
    return out, warnings


def test_a_genuine_nmd_pass_states_the_agreement_and_nmd_forced_false_keeps_it():
    for forced in (False, None):
        (record,), warnings = _evidence(_ts(reaction_coordinate_mode_index=1, nmd_forced=forced))
        assert record["mode_displacement_agrees"] is True and not warnings


def test_a_forced_nmd_pass_states_a_failed_displacement_never_an_agreement_even_with_an_index():
    (record,), warnings = _evidence(_ts(reaction_coordinate_mode_index=1, nmd_forced=True))
    assert record["mode_displacement_agrees"] is False and record["passed"] is True
    assert "nmd_forced = true" in record["rationale"] and "skip_nmd" in record["rationale"]
    (warning,) = warnings
    assert warning["code"] == "ts_nmd_forced_contradicts_reaction_coordinate_index"
    assert warning["context"]["reaction_coordinate_mode_index"] == "1"


def test_a_forced_nmd_pass_without_an_index_is_not_a_contradiction():
    (record,), warnings = _evidence(_ts(reaction_coordinate_mode_index=None, nmd_forced=True))
    assert record["mode_displacement_agrees"] is False and "nmd_forced = true" in record["rationale"]
    assert not warnings


def test_a_forced_pass_does_not_make_the_stated_index_the_designation():
    base = {"is_ts": True, "freq_n_imag": 2, "freq_frequencies_cm1_ess_order": [-1000.0, -20.0, 300.0, 400.0, 500.0],
            "imaginary_frequencies_cm1": [-1000.0, -20.0], "reaction_coordinate_mode_index": 2}
    with_flag = _freq_result_payload({**base, "nmd_forced": True}, schema_1_3=True)
    assert with_flag["reaction_coordinate_mode_index"] == 1          # TCKDB's tau rule, not ARC's index 2
    without = _freq_result_payload({**base, "nmd_forced": False}, schema_1_3=True)
    assert without["reaction_coordinate_mode_index"] == 2


def test_the_derived_fixtures_forced_ts_sends_a_failed_displacement_on_both_routes():
    doc = _derived()
    payload, _ = _bundle(doc, 1)
    (mode,) = [e for e in payload["transition_state"]["validation_evidence"] if e["kind"] == "imaginary_mode"]
    assert mode["mode_displacement_agrees"] is False and "nmd_forced = true" in mode["rationale"]
    request, _ = _standalone(doc, 1)
    (mode,) = [e for e in request["validation_evidence"] if e["kind"] == "imaginary_mode"]
    assert mode["mode_displacement_agrees"] is False
    genuine, _ = _bundle(doc, 0)
    (mode,) = [e for e in genuine["transition_state"]["validation_evidence"] if e["kind"] == "imaginary_mode"]
    assert mode["mode_displacement_agrees"] is True


def test_a_forced_flag_beside_an_index_through_the_whole_build_warns_once():
    doc = _derived()
    doc["transition_states"][0]["nmd_forced"] = True
    payload, warnings = _bundle(doc, 0)
    assert _codes(warnings).count("ts_nmd_forced_contradicts_reaction_coordinate_index") == 1
    (mode,) = [e for e in payload["transition_state"]["validation_evidence"] if e["kind"] == "imaginary_mode"]
    assert mode["mode_displacement_agrees"] is False


# ---------------------------------------------------------------------------
# conformer_ess_software / conformer_ess_version, conformer_energies null entries
# ---------------------------------------------------------------------------

def _species(doc, label, warnings):
    return _adapter()._build_computed_species_payload(
        output_doc=doc, species_record=next(s for s in doc["species"] if s["label"] == label),
        conformer_key="c0", warnings=warnings)


def _alts(payload):
    return [c for c in payload["conformers"] if c["key"].startswith("alt")]


def test_screened_conformers_are_filed_with_the_program_and_banner_of_their_own_log():
    warnings = []
    payload = _species(_derived(), "CH4", warnings)
    contract_validate(ComputedSpeciesUploadRequest, payload)
    alts = _alts(payload)
    assert len(alts) == 2
    first, second = (a["primary_calculation"] for a in alts)
    assert first["software_release"] == {"name": "gaussian", "version": "09", "revision": "D.01"}
    assert second["software_release"]["name"] == "orca" and second["software_release"]["version"] == "6.0.1"
    assert first["level_of_theory"] == second["level_of_theory"] == {"method": "wb97xd", "basis": "def2svp"}
    # the energy of a conformer ARC states one for; none is invented for the one whose entry is null
    assert first["opt_result"] == {"final_energy_hartree": pytest.approx(-306000.0 / 2625.499638, rel=1e-7)}
    assert "opt_result" not in second
    assert sorted(_codes(warnings)) == ["conformer_geometry_not_esss_optimized", "conformer_program_not_stated"]
    message = next(w for w in warnings if w["code"] == "conformer_program_not_stated")["message"]
    assert "conformer_ess_software entry is null" in message


def test_a_null_program_is_not_replaced_by_the_header_level_program():
    doc = _derived()
    doc["conformer_opt_level"] = {"method": "wb97xd", "basis": "def2svp", "software": "gaussian"}
    warnings = []
    assert len(_alts(_species(doc, "CH4", warnings))) == 2        # the lost-log conformer is still not filed
    assert "conformer_program_not_stated" in _codes(warnings)


def test_without_the_ess_lists_the_header_level_program_is_still_the_rule():
    doc = _derived()
    ch4 = next(s for s in doc["species"] if s["label"] == "CH4")
    del ch4["conformer_ess_software"], ch4["conformer_ess_version"]
    doc["conformer_opt_level"] = {"method": "wb97xd", "basis": "def2svp", "software": "gaussian"}
    alts = _alts(_species(doc, "CH4", []))
    assert [a["primary_calculation"]["software_release"]["name"] for a in alts] == ["gaussian", "gaussian", "gaussian"]
    doc["conformer_opt_level"] = None
    warnings = []
    assert not _alts(_species(doc, "CH4", warnings))
    assert "conformer_program_not_stated" in _codes(warnings)


def test_misaligned_ess_lists_are_not_used():
    doc = _derived()
    next(s for s in doc["species"] if s["label"] == "CH4")["conformer_ess_software"] = ["gaussian"]
    warnings = []
    assert not _alts(_species(doc, "CH4", warnings))              # no header program either: nothing filed


def test_a_conformer_with_a_program_but_no_banner_is_filed_without_a_version():
    doc = _derived()
    ch4 = next(s for s in doc["species"] if s["label"] == "CH4")
    ch4["conformer_ess_version"][0] = None
    (first, _) = _alts(_species(doc, "CH4", []))
    assert first["primary_calculation"]["software_release"] == {"name": "gaussian"}


def test_null_conformer_energies_never_become_an_energy_whatever_the_kind_says():
    doc = _derived()
    ch4 = next(s for s in doc["species"] if s["label"] == "CH4")
    assert ch4["conformer_energy_kind"] == "electronic_kj_mol" and None in ch4["conformer_energies"]
    first, second = (a["primary_calculation"] for a in _alts(_species(doc, "CH4", [])))
    assert "opt_result" in first and "opt_result" not in second
    assert "None" not in json.dumps([first, second])


# ---------------------------------------------------------------------------
# standard_state_pressure_pa, bac_type
# ---------------------------------------------------------------------------

def test_a_null_standard_state_pressure_still_omits_the_reference_pressure_never_defaults_it():
    doc = yaml.safe_load((SAMPLES / "species_thermo.output.yml").read_text())
    record = doc["species"][0]
    assert record["thermo"]["standard_state_pressure_pa"] is None
    payload = _adapter()._build_computed_species_payload(
        output_doc=_no_routes(doc), species_record=record, conformer_key="c0", warnings=[])
    assert "reference_pressure_bar" not in payload["thermo"]
    record["thermo"]["standard_state_pressure_pa"] = 101325.0
    payload = _adapter()._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="c0", warnings=[])
    assert payload["thermo"]["reference_pressure_bar"] == pytest.approx(1.01325)


def test_bond_corrections_applied_with_a_null_bac_type_warns_and_still_builds_the_scheme():
    doc = _no_routes(yaml.safe_load((SAMPLES / "species_thermo.output.yml").read_text()))
    record = doc["species"][0]
    assert doc["bac_type"] == "p"
    record["thermo"]["bond_corrections_applied"] = True
    warnings = []
    baseline = _adapter()._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="c0", warnings=warnings)
    assert "bac_type_not_stated" not in _codes(warnings)
    doc["bac_type"] = None
    warnings = []
    payload = _adapter()._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="c0", warnings=warnings)
    (warning,) = [w for w in warnings if w["code"] == "bac_type_not_stated"]
    assert warning["context"]["bac_type"] == "None"
    assert payload.get("applied_energy_corrections") == baseline.get("applied_energy_corrections")
    record["thermo"]["bond_corrections_applied"] = False
    warnings = []
    _adapter()._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="c0", warnings=warnings)
    assert "bac_type_not_stated" not in _codes(warnings)
