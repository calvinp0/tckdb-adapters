"""The 0.121 geometry/conformer rules and the 0.99 kinetics energy level, as producer-agnostic pre-checks."""

import copy

import pytest

from tckdb_core import rules

H2 = "2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74"
H2_OTHER = "2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.80"


def _reaction():
    return {
        "species": [{
            "key": "r0",
            "conformers": [
                {"key": "c0", "geometry": {"key": "g0", "xyz_text": H2},
                 "calculation": {"key": "opt0", "type": "opt"}},
                {"key": "c1", "geometry": {"key": "g1", "xyz_text": H2_OTHER},
                 "calculation": {"key": "opt1", "type": "opt"}},
            ],
            "calculations": [
                {"key": "sp0", "type": "sp", "conformer_key": "c0", "geometry_key": "g0",
                 "input_geometries": [{"xyz_text": H2}]},
            ],
        }],
        "transition_state": {"geometry": {"xyz_text": H2_OTHER}, "calculations": [
            {"key": "ts_sp", "type": "sp", "input_geometries": [{"xyz_text": H2_OTHER}]}]},
    }


def test_an_agreeing_payload_has_no_refusal_and_no_undeclarable_input():
    payload = _reaction()
    assert rules.geometry_anchor_refusals(payload, "computed_reaction") == []
    assert rules.undeclarable_input_geometries(payload, "computed_reaction") == []


def test_other_routes_have_no_keys_to_compare():
    assert rules.geometry_anchor_refusals(_reaction(), "computed_species") == []
    assert rules.geometry_anchor_refusals(_reaction(), "transition_state") == []


def test_a_calculation_whose_two_keys_name_different_conformers_is_a_refusal():
    payload = _reaction()
    payload["species"][0]["calculations"][0]["geometry_key"] = "g1"
    [(code, detail)] = rules.geometry_anchor_refusals(payload, "computed_reaction")
    assert code == "calculation_geometry_conformer_mismatch" and "sp0" in detail


def test_a_transition_state_calculation_may_not_be_anchored_to_a_species():
    payload = _reaction()
    payload["transition_state"]["calculations"][0]["conformer_key"] = "c0"
    assert [c for c, _ in rules.geometry_anchor_refusals(payload, "computed_reaction")] == [
        "transition_state_calculation_conformer_key"]
    payload = _reaction()
    payload["transition_state"]["calculations"][0]["geometry_key"] = "g0"
    assert [c for c, _ in rules.geometry_anchor_refusals(payload, "computed_reaction")] == [
        "transition_state_calculation_species_geometry"]


def test_whitespace_is_not_a_different_structure_but_other_coordinates_are():
    payload = _reaction()
    payload["species"][0]["calculations"][0]["input_geometries"] = [{"xyz_text": H2.replace(" ", "  ") + "\n"}]
    assert rules.undeclarable_input_geometries(payload, "computed_reaction") == []
    payload["species"][0]["calculations"][0]["input_geometries"] = [{"xyz_text": H2_OTHER}]
    [(calc, field)] = rules.undeclarable_input_geometries(payload, "computed_reaction")
    assert calc["key"] == "sp0" and field == "species[0].calculations[0]"


def test_one_matching_entry_among_several_is_enough():
    payload = _reaction()
    payload["species"][0]["calculations"][0]["input_geometries"] = [{"xyz_text": H2_OTHER}, {"xyz_text": H2}]
    assert rules.undeclarable_input_geometries(payload, "computed_reaction") == []


def test_computed_species_and_standalone_ts_owners():
    species = {"conformers": [{"geometry": {"xyz_text": H2},
                               "primary_calculation": {"type": "opt"},
                               "additional_calculations": [
                                   {"key": "f", "type": "freq", "input_geometries": [{"xyz_text": H2_OTHER}]}]}]}
    [(calc, field)] = rules.undeclarable_input_geometries(species, "computed_species")
    assert calc["key"] == "f" and field == "conformers[0].additional_calculations[0]"
    ts = {"geometry": {"xyz_text": H2}, "primary_opt": {"type": "opt", "input_geometries": [{"xyz_text": H2_OTHER}]},
          "additional_calculations": [{"type": "sp", "input_geometries": [{"xyz_text": H2}]}]}
    assert rules.undeclarable_input_geometries(ts, "transition_state") == []


# --- kinetics energy level -----------------------------------------------------------------

LEVEL = {"method": "wb97xd", "basis": "def2-tzvp", "spin_treatment": "restricted"}
BARE = {"method": "wb97xd", "basis": "def2-tzvp"}
LINKS = [{"calculation_key": "r", "role": "reactant_energy"},
         {"calculation_key": "p", "role": "product_energy"},
         {"calculation_key": "t", "role": "ts_energy"},
         {"calculation_key": "tf", "role": "freq"}]


def _calcs(**levels):
    return {k: {"key": k, "level_of_theory": v} for k, v in levels.items()}


SINGLETS = {"r": 1, "p": 1, "t": 1, "tf": 1}


def test_a_level_all_linked_energies_share_and_the_producer_states_is_declared_as_sent():
    declared = rules.kinetics_energy_level_declaration(
        LINKS, calculations=_calcs(r=LEVEL, p=LEVEL, t=LEVEL, tf=BARE), stated_levels=[BARE, BARE, BARE],
        multiplicities=SINGLETS)
    assert declared == LEVEL and declared is not LEVEL


@pytest.mark.parametrize("calcs,stated", [
    (dict(r=LEVEL, p=LEVEL, t={"method": "b3lyp", "basis": "6-31g"}), [BARE] * 3),          # level differs
    (dict(r=LEVEL, p=LEVEL), [BARE] * 3),                                                    # a link has no calculation
    (dict(r=LEVEL, p=LEVEL, t=LEVEL), []),                                                   # nothing stated
    (dict(r=LEVEL, p=LEVEL, t=LEVEL), [BARE, None, BARE]),                                   # one participant unattributed
    (dict(r=LEVEL, p=LEVEL, t=LEVEL), [BARE, {"method": "b3lyp", "basis": "6-31g"}, BARE]),  # stated another level
])
def test_no_single_level_means_no_declaration(calcs, stated):
    assert rules.kinetics_energy_level_declaration(
        LINKS, calculations=_calcs(**calcs), stated_levels=stated, multiplicities=SINGLETS) is None


def test_without_an_energy_link_nothing_is_declared():
    assert rules.kinetics_energy_level_declaration(
        [{"calculation_key": "tf", "role": "freq"}], calculations=_calcs(tf=LEVEL), stated_levels=[BARE],
        multiplicities={"tf": 1}) is None


def test_a_crash_in_a_shipped_rule_is_not_reported_as_a_refusal():
    # Only the shipped coded validation error is a refusal; any other exception is a defect and propagates.
    payload = _reaction()
    payload["species"][0]["calculations"] = [5]      # not a mapping: the replica crashes, which is not a refusal
    with pytest.raises(Exception) as caught:
        rules.geometry_anchor_refusals(payload, "computed_reaction")
    from tckdb_schemas.coded_error import CodedValidationError
    assert not isinstance(caught.value, CodedValidationError)


# --- scan / IRC point labelling (0.94) ---------------------------------------------------------

from tckdb_core import isotopes  # noqa: E402


def _irc(frame, software_irrelevant=None):
    return {"irc_result": {"points": [{"geometry": {"xyz_text": frame}}, {"geometry": {"xyz_text": frame}}]}}


@pytest.mark.parametrize("software,expected_left", [("gaussian", 0), ("ORCA", 0), ("molpro", 2), (None, 2)])
def test_points_are_labelled_only_for_an_order_preserving_program(software, expected_left):
    calc = _irc(H2)
    left = isotopes.label_point_geometries(
        calc, reference_geometry={"xyz_text": H2, "isotopes": {"1": 2}}, software=software)
    assert len(left) == expected_left
    labelled = [p["geometry"].get("isotopes") for p in calc["irc_result"]["points"]]
    assert labelled == ([{"1": 2}] * 2 if not expected_left else [None] * 2)


def test_points_in_another_element_order_or_under_an_unlabelled_reference_are_left():
    other = "2\n\nO 0 0 0\nH 0 0 1"
    calc = _irc(other)
    assert len(isotopes.label_point_geometries(
        calc, reference_geometry={"xyz_text": H2, "isotopes": {"1": 2}}, software="gaussian")) == 2
    assert len(isotopes.label_point_geometries(
        _irc(H2), reference_geometry={"xyz_text": H2}, software="gaussian")) == 2
    assert len(isotopes.label_point_geometries(_irc(H2), reference_geometry=None, software="gaussian")) == 2
    path = {"path_search_result": {"points": [{"geometry": {"xyz_text": H2}}]}}
    assert len(isotopes.label_point_geometries(
        path, reference_geometry={"xyz_text": H2, "isotopes": {"1": 2}}, software="gaussian")) == 1
