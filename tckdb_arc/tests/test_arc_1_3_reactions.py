"""ARC output schema 1.3: reaction atom map and IRC participant mapping.

The fixture ``fixtures/arc_1_3_reactions`` is a document written by ARC's own
``write_output_yml`` (PR #1059 @ bc731fb4) for ``OH + CH4 <=> H2O + CH3``: the
reaction's ``atom_map`` was computed by ARC's mapper, and the TS record's
``irc_participant_mapping`` was produced by ARC's own IRC check
(``arc.checks.ts.check_irc_species_and_rxn``) on endpoint geometries in the TS
atom order. ``reactant_labels`` is ``['CH4', 'OH']`` (sorted) while ARC counts
the atoms of the map and the participants of the IRC mapping over
``['OH', 'CH4']``, which is what makes the label/occurrence matching below
necessary.

Two decisions are pinned here:

* the IRC participant mapping is sent, as the TCKDB participant mappings of
  the IRC validation evidence, only when everything TCKDB checks is stated;
* the reaction ``atom_map`` is not sent, because TCKDB's ``atom_map`` is
  participant atom -> transition-state atom and ARC states no relation
  between its map and the transition-state atom order (and the standalone
  transition-state route has no ``atom_map`` field at all).

TCKDB's route-handler cross-checks are not run offline; the ones that apply to
the evidence are replicated in ``_handler_errors`` from
``app/services/reaction_resolution.py`` (``validate_ts_evidence_participant_composition``)
and ``tckdb_schemas.fragments.ts_validation_evidence`` (``validate_ts_evidence_set``).
"""

import copy
from collections import Counter
from pathlib import Path

import pytest
import yaml

from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

from _contract import contract_validate
from tckdb_arc.adapter import TCKDBAdapter, _xyz_element_symbols
from tckdb_arc.config import TCKDBConfig

FIXTURE = Path(__file__).parent / "fixtures" / "arc_1_3_reactions" / "output.yml"
MAPPING_CODE = "ts_irc_participant_mapping_not_sent"
ATOM_MAP_CODE = "reaction_atom_map_ts_order_not_stated"

REACTANT_MAPPING = {"reactant:1": [1, 2, 3, 4, 5], "reactant:2": [6, 7]}   # CH4, OH
PRODUCT_MAPPING = {"product:1": [1, 2, 3, 4], "product:2": [5, 6, 7]}      # CH3, H2O


def _no_routes(doc):
    """Drop the observed route lines: the staged logs behind these documents are other levels' logs
    (the sample's IRC route says ub3lyp/cbsb7 under a recorded uhf/3-21g), which the adapter refuses
    as ``level_contradicted_by_route``; these tests are about the reaction mapping, not routes."""
    for record in (*doc["species"], *doc["transition_states"]):
        for key in ("opt_route", "freq_route", "sp_route"):
            if key in record:
                record[key] = None
        if record.get("irc_log_routes"):
            record["irc_log_routes"] = [None] * len(record["irc_log_routes"])
    return doc


def _fixture():
    return _no_routes(copy.deepcopy(yaml.safe_load(FIXTURE.read_text())))


def _adapter():
    return TCKDBAdapter(
        TCKDBConfig(enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=".",
                    api_key_env="X_TCKDB_API_KEY", project_label="proj-1-3",
                    upload_mode="computed_reaction", upload=False),
        project_directory=".")


def _reaction_payload(doc):
    warnings = []
    payload = _adapter()._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0], warnings=warnings)
    return payload, warnings


def _ts_payload(doc):
    warnings = []
    payload = _adapter()._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0],
        reaction_record=doc["reactions"][0], warnings=warnings)
    return payload, warnings


def _evidence(payload):
    block = payload.get("transition_state", payload)
    (record,) = block["validation_evidence"]
    return record


ROUTES = [("computed_reaction", _reaction_payload), ("transition_state", _ts_payload)]


def _codes(warnings):
    return [w["code"] for w in warnings]


def _handler_errors(evidence, *, ts_symbols, participants):
    """The offline-checkable TCKDB rules on IRC participant mappings.

    ``participants`` is ``{"reactant:N": element symbols of that participant}``.
    Replicates ``validate_ts_evidence_set`` (every participant named, every
    transition-state atom exactly once on each side, 1-based) and
    ``validate_ts_evidence_participant_composition`` (the assigned atoms are the
    participant's own elements). Returns a list of problems.
    """
    errors = []
    for side, key in (("reactant", "reactant_participant_mapping"),
                      ("product", "product_participant_mapping")):
        mapping = evidence[key]
        expected = {k for k in participants if k.startswith(side)}
        if set(mapping) != expected:
            errors.append(f"{side}: participants {sorted(mapping)} != {sorted(expected)}")
            continue
        atoms = [a for v in mapping.values() for a in v]
        if sorted(atoms) != list(range(1, len(ts_symbols) + 1)):
            errors.append(f"{side}: atoms do not cover 1..{len(ts_symbols)} exactly once")
        for participant, indices in mapping.items():
            assigned = Counter(ts_symbols[i - 1] for i in indices if 1 <= i <= len(ts_symbols))
            if assigned != Counter(participants[participant]):
                errors.append(f"{participant}: element mismatch {dict(assigned)}")
    return errors


def _participants(doc):
    symbols = {s["label"]: _xyz_element_symbols(s["xyz"]) for s in doc["species"]}
    rxn = doc["reactions"][0]
    result = {}
    for side, labels in (("reactant", rxn["reactant_labels"]), ("product", rxn["product_labels"])):
        for index, label in enumerate(labels, start=1):
            result[f"{side}:{index}"] = symbols[label]
    return result


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


def test_fixture_is_arc_1_3_with_the_new_keys():
    doc = _fixture()
    assert doc["schema_version"] == "1.3"
    rxn = doc["reactions"][0]
    assert rxn["atom_map"] == [0, 1, 3, 2, 4, 5, 6]
    assert rxn["atom_map_reactant_labels"] == ["OH", "CH4"]
    assert rxn["atom_map_product_labels"] == ["H2O", "CH3"]
    assert rxn["atom_map_source"] == "inferred"
    assert rxn["atom_map_method"].startswith("arc.mapping.driver.map_reaction")
    # ``reactant_labels`` is sorted, ARC's map order is not.
    assert rxn["reactant_labels"] == ["CH4", "OH"]
    mapping = doc["transition_states"][0]["irc_participant_mapping"]
    assert mapping["atom_order_matches_ts"] is True and mapping["sides_distinguishable"] is True
    assert doc["transition_states"][0]["ts_checks"]["IRC"] is True


def test_the_atom_map_and_the_irc_mapping_agree_in_the_fixture():
    """ARC's map and its IRC partition describe one reaction (a fixture sanity check).

    Counting, for each (reactant participant, product participant) pair, the
    atoms the atom map sends from one to the other must give the number of
    transition-state atoms the IRC endpoints put in both. This is a property of
    this fixture, not a rule the adapter or TCKDB applies: among
    symmetry-equivalent atoms ARC's map is arbitrary, so it cannot gate an upload.
    """
    doc = _fixture()
    rxn = doc["reactions"][0]
    mapping = doc["transition_states"][0]["irc_participant_mapping"]
    symbols = {s["label"]: _xyz_element_symbols(s["xyz"]) for s in doc["species"]}

    def owners(labels):  # each label occurs once in this fixture
        return [(label, 1) for label in labels for _ in symbols[label]]

    r_owner, p_owner = owners(rxn["atom_map_reactant_labels"]), owners(rxn["atom_map_product_labels"])
    via_map = Counter((r_owner[i], p_owner[j]) for i, j in enumerate(rxn["atom_map"]))
    ts_side = {side: {(p["label"], p["occurrence"]): set(p["atom_indices"])
                      for p in mapping[side]["participants"]} for side in ("reactants", "products")}
    via_irc = Counter()
    for r_key, r_atoms in ts_side["reactants"].items():
        for p_key, p_atoms in ts_side["products"].items():
            if r_atoms & p_atoms:
                via_irc[(r_key, p_key)] = len(r_atoms & p_atoms)
    assert via_map == via_irc


# ---------------------------------------------------------------------------
# IRC participant mapping -> validation evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route,build", ROUTES)
def test_irc_participant_mapping_is_sent_in_tckdb_slot_order(route, build):
    payload, warnings = build(_fixture())
    evidence = _evidence(payload)
    assert evidence["passed"] is True
    # TCKDB's slots follow ``reactant_labels`` (CH4, OH), not ARC's ``position``
    # (OH, CH4); indices are 1-based transition-state atoms.
    assert evidence["reactant_participant_mapping"] == REACTANT_MAPPING
    assert evidence["product_participant_mapping"] == PRODUCT_MAPPING
    assert MAPPING_CODE not in _codes(warnings)
    if route == "transition_state":
        assert "source_calculation_key" not in evidence
    else:
        assert evidence["source_calculation_key"] == "ts_irc"
        assert payload["reactant_keys"] == ["r0_CH4", "r1_OH"]


@pytest.mark.parametrize("route,build", ROUTES)
def test_what_is_sent_passes_the_offline_handler_rules(route, build):
    doc = _fixture()
    payload, _ = build(doc)
    ts_symbols = _xyz_element_symbols(doc["transition_states"][0]["xyz"])
    assert _handler_errors(_evidence(payload), ts_symbols=ts_symbols,
                           participants=_participants(doc)) == []


def test_arcs_position_is_not_the_participant_index():
    """Mutation: numbering by ARC's ``position`` swaps the participants, and is caught."""
    doc = _fixture()
    payload, _ = _reaction_payload(doc)
    evidence = copy.deepcopy(_evidence(payload))
    by_position = {"reactant:1": evidence["reactant_participant_mapping"]["reactant:2"],
                   "reactant:2": evidence["reactant_participant_mapping"]["reactant:1"]}
    evidence["reactant_participant_mapping"] = by_position
    errors = _handler_errors(evidence, ts_symbols=_xyz_element_symbols(doc["transition_states"][0]["xyz"]),
                             participants=_participants(doc))
    assert any("element mismatch" in e for e in errors)


def test_a_zero_based_mapping_is_refused_by_the_published_model():
    """Mutation: ARC's 0-based indices sent as they are."""
    doc = _fixture()
    payload, _ = _reaction_payload(doc)
    bad = copy.deepcopy(payload)
    mapping = _evidence(bad)["reactant_participant_mapping"]
    mapping["reactant:1"] = [i - 1 for i in mapping["reactant:1"]]
    with pytest.raises(ValueError, match="1-based"):
        contract_validate(ComputedReactionUploadRequest, bad)
    bad = copy.deepcopy(_ts_payload(doc)[0])
    mapping = _evidence(bad)["reactant_participant_mapping"]
    mapping["reactant:1"] = [i - 1 for i in mapping["reactant:1"]]
    with pytest.raises(ValueError, match="1-based"):
        contract_validate(TransitionStateUploadRequest, bad)


def test_a_one_sided_mapping_is_refused_by_the_published_model():
    payload, _ = _reaction_payload(_fixture())
    del _evidence(payload)["product_participant_mapping"]
    with pytest.raises(ValueError, match="both sides or neither"):
        contract_validate(ComputedReactionUploadRequest, payload)


def test_an_incomplete_passed_mapping_is_refused_by_the_published_model():
    payload, _ = _reaction_payload(_fixture())
    _evidence(payload)["reactant_participant_mapping"]["reactant:2"] = [6]
    with pytest.raises(ValueError, match="cover every one"):
        contract_validate(ComputedReactionUploadRequest, payload)


def test_participants_are_matched_by_label_not_by_position_or_record_order():
    doc = _fixture()
    mapping = doc["transition_states"][0]["irc_participant_mapping"]
    for side in ("reactants", "products"):
        participants = mapping[side]["participants"]
        participants.reverse()
        for p in participants:
            p["position"] = 9
    payload, warnings = _reaction_payload(doc)
    assert _evidence(payload)["reactant_participant_mapping"] == REACTANT_MAPPING
    assert MAPPING_CODE not in _codes(warnings)


@pytest.mark.parametrize("route,build", ROUTES)
def test_slots_follow_the_uploaded_reactant_order(route, build):
    doc = _fixture()
    doc["reactions"][0]["reactant_labels"] = ["OH", "CH4"]
    payload, warnings = build(doc)
    evidence = _evidence(payload)
    assert evidence["reactant_participant_mapping"] == {"reactant:1": [6, 7], "reactant:2": [1, 2, 3, 4, 5]}
    assert _handler_errors(evidence, ts_symbols=_xyz_element_symbols(doc["transition_states"][0]["xyz"]),
                           participants=_participants(doc)) == []


def _set(path_to_value):
    def mutate(doc):
        mapping = doc["transition_states"][0]["irc_participant_mapping"]
        path_to_value(mapping)
    return mutate


REFUSALS = {
    "endpoints_not_in_ts_order": _set(lambda m: m.update(atom_order_matches_ts=False)),
    "ts_order_unchecked": _set(lambda m: m.update(atom_order_matches_ts=None)),
    "sides_not_distinguishable": _set(lambda m: m.update(sides_distinguishable=False)),
    "unknown_participant": _set(lambda m: m["reactants"]["participants"][0].update(label="C2H6")),
    "missing_participant": _set(lambda m: m["products"]["participants"].pop()),
    "extra_occurrence": _set(lambda m: m["reactants"]["participants"][0].update(occurrence=2)),
    "duplicated_participant": _set(lambda m: m["reactants"]["participants"].append(
        dict(m["reactants"]["participants"][0]))),
    "atom_out_of_range": _set(lambda m: m["reactants"]["participants"][0]["atom_indices"].__setitem__(0, 7)),
    "negative_atom": _set(lambda m: m["reactants"]["participants"][0]["atom_indices"].__setitem__(0, -1)),
    "atom_not_covered": _set(lambda m: m["products"]["participants"][1]["atom_indices"].pop()),
    "atom_claimed_twice": _set(lambda m: m["products"]["participants"][1]["atom_indices"].append(0)),
    # right elements, but atom 2 is listed twice and atom 1 never: not a partition
    "atom_repeated_not_covered": _set(lambda m: m["products"]["participants"][1]["atom_indices"].__setitem__(1, 2)),
    # the O of OH and an H of CH4 trade places: a valid partition of the wrong atoms
    "wrong_elements": _set(lambda m: (m["reactants"]["participants"][0]["atom_indices"].__setitem__(0, 4),
                                      m["reactants"]["participants"][1]["atom_indices"].__setitem__(4, 5))),
    "no_participants": _set(lambda m: m["reactants"].update(participants=[])),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
@pytest.mark.parametrize("route,build", ROUTES)
def test_a_mapping_that_cannot_be_stated_sends_neither_side(route, build, name):
    doc = _fixture()
    REFUSALS[name](doc)
    payload, warnings = build(doc)
    evidence = _evidence(payload)
    assert evidence["passed"] is True
    assert "reactant_participant_mapping" not in evidence
    assert "product_participant_mapping" not in evidence
    (warning,) = [w for w in warnings if w["code"] == MAPPING_CODE]
    assert warning["field"] == "transition_state.validation_evidence"


def test_a_repeated_reactant_collapsed_in_reactant_labels_is_not_mapped():
    """ARC's participants are per occurrence; a single uploaded slot cannot take two."""
    doc = _fixture()
    mapping = doc["transition_states"][0]["irc_participant_mapping"]
    mapping["products"]["participants"] = [
        {"label": "H2O", "position": 1, "occurrence": 1, "atom_indices": [4, 5, 6]},
        {"label": "H2O", "position": 2, "occurrence": 2, "atom_indices": [0, 1, 2, 3]}]
    payload, warnings = _reaction_payload(doc)
    assert "product_participant_mapping" not in _evidence(payload)
    assert MAPPING_CODE in _codes(warnings)


@pytest.mark.parametrize("route,build", ROUTES)
@pytest.mark.parametrize("remove", ["null", "absent", "ts_checks_irc_false"])
def test_no_stated_mapping_sends_none_and_says_nothing(route, build, remove):
    doc = _fixture()
    ts = doc["transition_states"][0]
    if remove == "null":
        ts["irc_participant_mapping"] = None
    elif remove == "absent":
        del ts["irc_participant_mapping"]            # a pre-1.3 record
    else:
        ts["ts_checks"]["IRC"] = False               # ARC states the mapping only for a pass
    payload, warnings = build(doc)
    evidence = _evidence(payload)
    assert evidence["passed"] is (remove != "ts_checks_irc_false")
    assert "reactant_participant_mapping" not in evidence
    assert MAPPING_CODE not in _codes(warnings)


# ---------------------------------------------------------------------------
# Reaction atom map: not sent
# ---------------------------------------------------------------------------


def _all_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


@pytest.mark.parametrize("route,build", ROUTES)
@pytest.mark.parametrize("source", ["inferred", "declared", None])
def test_the_atom_map_is_not_sent_and_the_reason_is_reported(route, build, source):
    doc = _fixture()
    rxn = doc["reactions"][0]
    rxn["atom_map_source"] = source
    if source != "inferred":
        rxn["atom_map_method"] = None
    payload, warnings = build(doc)
    assert "atom_map" not in set(_all_keys(payload))
    (warning,) = [w for w in warnings if w["code"] == ATOM_MAP_CODE]
    # the standalone route has no atom_map field at all (tckdb-schemas contract)
    assert ("no atom_map field" in warning["message"]) is (route == "transition_state")
    assert warning["context"]["atom_map_source"] == str(source)
    assert warning["context"]["atom_map_method"] == str(rxn["atom_map_method"])
    # With no atom map in the upload, TCKDB's ``atom_map_contradicts_irc_mapping``
    # cannot arise, and the IRC mapping above is still sent.
    assert "reactant_participant_mapping" in _evidence(payload)


@pytest.mark.parametrize("route,build", ROUTES)
def test_no_stated_atom_map_reports_nothing(route, build):
    doc = _fixture()
    rxn = doc["reactions"][0]
    for key in ("atom_map", "atom_map_reactant_labels", "atom_map_product_labels",
                "atom_map_source", "atom_map_method"):
        rxn[key] = None
    _, warnings = build(doc)
    assert ATOM_MAP_CODE not in _codes(warnings)
    del rxn["atom_map"]                               # a pre-1.3 record
    _, warnings = build(doc)
    assert ATOM_MAP_CODE not in _codes(warnings)


def test_a_reaction_without_a_transition_state_reports_no_atom_map_warning():
    doc = _fixture()
    doc["reactions"][0]["ts_label"] = None
    _, warnings = _reaction_payload(doc)
    assert ATOM_MAP_CODE not in _codes(warnings)


def test_the_contract_has_no_atom_map_on_the_standalone_route():
    """The standalone TS request cannot carry an atom map; the computed-reaction request can."""
    from tckdb_schemas import contract
    assert "atom_map" not in contract.json_schema("TransitionStateUploadRequest")["properties"]
    assert "atom_map" in contract.json_schema("ComputedReactionUploadRequest")["properties"]


# ---------------------------------------------------------------------------
# The ARC agent's 1.3 samples (fixtures/arc_1_3_samples), no parser_evidence.json
# ---------------------------------------------------------------------------

SAMPLES = Path(__file__).parent / "fixtures" / "arc_1_3_samples"


def _sample(name):
    return _no_routes(copy.deepcopy(yaml.safe_load((SAMPLES / f"{name}.output.yml").read_text())))


@pytest.mark.parametrize("route,build", ROUTES)
def test_sample_reaction_sends_the_irc_mapping_and_no_atom_map(route, build):
    assert not (SAMPLES / "parser_evidence.json").exists()    # the no-sidecar case
    doc = _sample("reaction_kinetics")
    # ARC's real map is a non-identity permutation: TS order cannot be read off it
    assert doc["reactions"][0]["atom_map"] != sorted(doc["reactions"][0]["atom_map"])
    payload, warnings = build(doc)
    evidence = _evidence(payload)
    assert evidence["reactant_participant_mapping"] == {"reactant:1": list(range(1, 11))}
    assert evidence["product_participant_mapping"] == {"product:1": list(range(1, 11))}
    assert "atom_map" not in set(_all_keys(payload))
    assert ATOM_MAP_CODE in _codes(warnings) and MAPPING_CODE not in _codes(warnings)
    ts_symbols = _xyz_element_symbols(doc["transition_states"][0]["xyz"])
    assert _handler_errors(evidence, ts_symbols=ts_symbols, participants=_participants(doc)) == []


def test_sample_endpoint_numbering_is_not_the_side():
    """ARC's reactant side is endpoint 2 here; the side, not the endpoint number, decides."""
    mapping = _sample("reaction_kinetics")["transition_states"][0]["irc_participant_mapping"]
    assert mapping["reactants"]["endpoint"] == 2 and mapping["products"]["endpoint"] == 1


def test_legacy_sample_states_nothing_and_sends_nothing():
    from tckdb_arc.adapter import _irc_participant_mappings, _warn_atom_map_not_sent
    doc = _sample("legacy_restart")
    rxn, ts = doc["reactions"][0], doc["transition_states"][0]
    assert rxn["atom_map"] is None and ts["irc_participant_mapping"] is None
    warnings = []
    assert _irc_participant_mappings(ts, reaction_record=rxn, species_index={}, ts_xyz_text=ts["xyz"],
                                     ts_label="TS", warnings=warnings) is None
    _warn_atom_map_not_sent(rxn, ts_label="TS", warnings=warnings)
    assert warnings == []
