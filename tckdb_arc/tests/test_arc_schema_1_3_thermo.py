"""ARC output schema 1.3, batch F: corrections, statmech, kinetics provenance, TS frequencies, parsed properties.

The documents under ``fixtures/arc_1_3_thermo`` are written by ARC's own ``write_output_yml``
(PR ReactionMechanismGenerator/ARC#1059 @ bc731fb4) and validate against its JSON Schema; see the README there.
Every request built here is also checked against the producer contract by the conftest hook.

1.3 keys are read from 1.3 documents only: a document of an earlier version never states them
(BRIDGE_ROADMAP A19), so the same record relabelled ``1.2`` must give the pre-1.3 payload.
"""

import copy
import json
import math
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import (
    _build_kinetics_block,
    _build_slim_torsions,
    _is_output_schema_1_3_or_later,
    _scheme_data_revisions,
    _skipped_bonds_note,
    _wavefunction_diagnostic_payload,
)
from test_thermo_enthalpy_declaration import _adapter

FIXTURE = Path(__file__).parent / "fixtures" / "arc_1_3_thermo" / "output.yml"
ROUTES = ("computed_species", "conformer", "computed_reaction")
# The fixture's reaction is sBuOH <=> nBuOH, so these are its r0 and p0 species blocks.
REACTION_BLOCK = {"sBuOH": 0, "nBuOH": 1}
SHA = "ab" * 32


def _doc(version="1.3"):
    doc = yaml.safe_load(FIXTURE.read_text())
    doc["schema_version"] = version
    # The staged logs behind this fixture are other levels' logs, so their observed route lines
    # contradict the recorded levels (the adapter refuses that, ``level_contradicted_by_route``).
    for record in (*doc["species"], *doc["transition_states"]):
        for key in ("opt_route", "freq_route", "sp_route"):
            if key in record:
                record[key] = None
        if record.get("irc_log_routes"):
            record["irc_log_routes"] = [None] * len(record["irc_log_routes"])
    return doc


def _species(doc, label):
    return next(s for s in doc["species"] if s["label"] == label)


def _correction(species, correction_type):
    return next(c for c in species["energy_corrections"] if c["correction_type"] == correction_type)


def _walk_calcs(node):
    if isinstance(node, dict):
        if "type" in node and "software_release" in node and "level_of_theory" in node:
            yield node
        for value in node.values():
            yield from _walk_calcs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_calcs(value)


class Built:
    """What one route built for one species of a document."""

    def __init__(self, payload, warnings, route, label):
        self.payload, self.warnings, self.route = payload, warnings, route
        if route == "computed_reaction":
            self.block = payload["species"][REACTION_BLOCK[label]]
        else:
            self.block = payload
        self.statmech = self.block.get("statmech")
        self.corrections = self.block.get("applied_energy_corrections") or []
        self.thermo = self.block.get("thermo")

    def calcs(self, calc_type):
        return [c for c in _walk_calcs(self.block) if c["type"] == calc_type]

    def warning_codes(self):
        return [w["code"] for w in self.warnings]

    def torsions(self):
        """The treated torsions: the conformer upload also lists ARC's rejected rotors (``invalidated_reason``)."""
        return [t for t in (self.statmech or {}).get("torsions", []) if "invalidated_reason" not in t]


def build(tmp_path, route, doc, label="sBuOH"):
    adapter = _adapter(tmp_path)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        if route == "computed_species":
            outcome = adapter.submit_computed_species_from_output(
                output_doc=doc, species_record=copy.deepcopy(_species(doc, label)))
        elif route == "conformer":
            outcome = adapter.submit_from_output(
                output_doc=doc, species_record=copy.deepcopy(_species(doc, label)))
        else:
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=copy.deepcopy(doc["reactions"][0]))
    payload = json.loads(outcome.payload_path.read_text())
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
    return Built(payload, outcome.warnings, route, label)


def build_reaction(tmp_path, doc):
    adapter = _adapter(tmp_path)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=copy.deepcopy(doc["reactions"][0]))
    return json.loads(outcome.payload_path.read_text()), outcome.warnings


def build_ts(tmp_path, doc):
    adapter = _adapter(tmp_path)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_ts_from_output(
            output_doc=doc, ts_record=copy.deepcopy(doc["transition_states"][0]),
            reaction_record=copy.deepcopy(doc["reactions"][0]))
    return json.loads(outcome.payload_path.read_text()), outcome.warnings


def test_the_fixture_is_a_1_3_document_with_the_keys_under_test():
    doc = _doc()
    assert doc["schema_version"] == "1.3"
    assert doc["rmg_database"]["path_kind"] == "package"
    sbuoh, nbuoh, nh3 = (_species(doc, label) for label in ("sBuOH", "nBuOH", "NH3"))
    assert sbuoh["statmech"]["arkane_treatment"] == "rrho_1d"
    assert nbuoh["statmech"]["arkane_treatment"] == "rrho"
    assert [t["treatment"] for t in nbuoh["statmech"]["torsions"]] == [None, None]
    assert nh3["statmech"]["rigid_rotor_kind"] == "symmetric_top"
    assert _correction(nbuoh, "bond_additivity")["skipped_components"] == [{"bond": "C-C", "count": 3}]
    ts = doc["transition_states"][0]
    assert ts["freq_frequencies_cm1_ess_order"] == [-1235.4, -18.2, 1300.0, 1500.0]
    assert ts["reaction_coordinate_mode_index"] == 1
    assert sbuoh["sp_t1_diagnostic"] == pytest.approx(0.0123)
    assert doc["reactions"][0]["kinetics"]["ts_validation"]


def test_schema_version_selects_the_1_3_reads():
    assert _is_output_schema_1_3_or_later({"schema_version": "1.3"})
    assert not _is_output_schema_1_3_or_later({"schema_version": "1.2"})
    assert not _is_output_schema_1_3_or_later({"schema_version": "1.0"})
    assert not _is_output_schema_1_3_or_later({})
    assert not _is_output_schema_1_3_or_later({"schema_version": "x"})


# ---------------------------------------------------------------------------
# 1. Energy corrections: a partial Petersson BAC is sent with the bonds it applied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_a_partial_petersson_bac_is_sent_with_its_applied_components(tmp_path, route):
    built = build(tmp_path, route, _doc(), "nBuOH")
    (bac,) = [c for c in built.corrections if c["application_role"] == "bac_total"]
    assert [c["key"] for c in bac["components"]] == ["C-H"]
    assert sum(c["contribution_value"] for c in bac["components"]) == pytest.approx(bac["value"])
    assert "bac_correction_omitted_components_incomplete" not in built.warning_codes()
    # The skipped bond is the correction's note; it is not part of the scheme's identity.
    assert "C-C x3" in bac["note"] and "no parameter" in bac["note"]
    assert "C-C x3" not in json.dumps(bac["scheme"])


@pytest.mark.parametrize("route", ROUTES)
def test_a_complete_bac_and_the_atom_energy_carry_no_skipped_note(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    assert {c["application_role"] for c in built.corrections} == {"aec_total", "bac_total"}
    assert all("note" not in c for c in built.corrections)


@pytest.mark.parametrize("route", ROUTES)
def test_before_1_3_the_same_record_is_still_omitted_when_its_components_are_partial(tmp_path, route):
    doc = _doc("1.2")
    bac = _correction(_species(doc, "nBuOH"), "bond_additivity")
    # What 1.2 wrote for a partly parametrized table: no component list at all.
    bac["components"] = []
    del bac["skipped_components"]
    built = build(tmp_path, route, doc, "nBuOH")
    assert [c["application_role"] for c in built.corrections] == ["aec_total"]
    assert "bac_correction_omitted_components_incomplete" in built.warning_codes()


def test_the_1_3_partial_bac_is_the_only_difference_from_the_1_2_reading(tmp_path):
    """Relabelling the 1.3 record 1.2 must withhold its skipped-bond note and reading, nothing else."""
    sent = build(tmp_path, "computed_species", _doc(), "nBuOH")
    older = build(tmp_path, "computed_species", _doc("1.2"), "nBuOH")
    (bac13,) = [c for c in sent.corrections if c["application_role"] == "bac_total"]
    (bac12,) = [c for c in older.corrections if c["application_role"] == "bac_total"]
    assert "note" in bac13 and "note" not in bac12


@pytest.mark.parametrize("mutate,reason", [
    (lambda bac: bac.update(components=[]), "no_bond_had_parameter"),
    (lambda bac: bac["components"][0].update(contribution_value=None), "component_unusable"),
    (lambda bac: bac["components"][0].update(contribution_value=-5.0), "components_do_not_sum"),
    (lambda bac: bac["components"][0].update(component_kind="other"), "no_bond_component"),
])
def test_a_1_3_bac_without_usable_components_is_still_omitted(tmp_path, mutate, reason):
    doc = _doc()
    mutate(_correction(_species(doc, "nBuOH"), "bond_additivity"))
    built = build(tmp_path, "computed_species", doc, "nBuOH")
    assert [c["application_role"] for c in built.corrections] == ["aec_total"]
    (warning,) = [w for w in built.warnings if w["code"] == "bac_correction_omitted_components_incomplete"]
    assert warning["context"]["reason"] == reason
    if reason == "no_bond_had_parameter":
        assert "no bond had a parameter" in built.thermo["note"]
        assert "applied by Arkane" not in built.thermo["note"]
    else:
        assert "not deposited as an applied correction" in built.thermo["note"]


def test_a_1_3_bac_whose_every_bond_was_skipped_is_omitted_not_sent_empty(tmp_path):
    doc = _doc()
    bac = _correction(_species(doc, "nBuOH"), "bond_additivity")
    bac["components"] = []
    bac["total"]["value"] = 0.0
    bac["skipped_components"] = [{"bond": "C-C", "count": 3}, {"bond": "C-H", "count": 9}]
    built = build(tmp_path, "computed_species", doc, "nBuOH")
    assert [c["application_role"] for c in built.corrections] == ["aec_total"]
    (warning,) = [w for w in built.warnings if w["code"] == "bac_correction_omitted_components_incomplete"]
    assert "no bond Arkane applied" in warning["message"]


def test_a_bac_on_a_monatomic_species_may_still_be_componentless(tmp_path):
    from tckdb_arc.adapter import _build_applied_energy_corrections, _correction_records_from_record
    record = {"energy_corrections": [copy.deepcopy(_correction(_species(_doc(), "nBuOH"), "bond_additivity"))]}
    record["energy_corrections"][0]["components"] = []
    records = _correction_records_from_record(record, schema_1_3=True)
    kept = _build_applied_energy_corrections(
        records, target_kind="species", element_symbols=["H"], target_label="H")
    assert [c["application_role"] for c in kept] == ["bac_total"]


@pytest.mark.parametrize("skipped,expected", [
    ([{"bond": "C-C", "count": 3}, {"bond": "C=O", "count": 1}], "C-C x3, C=O x1"),
    ([{"bond": "C-C", "count": 3}], "C-C x3"),
    ([], None), (None, None),
    ([{"bond": "C-C"}], None), ([{"bond": "C-C", "count": 0}], None), ([{"count": 2}], None), ("C-C", None),
])
def test_the_skipped_bond_note_states_each_bond_or_nothing(skipped, expected):
    note = _skipped_bonds_note(skipped)
    assert (expected is None) == (note is None)
    if expected:
        assert expected in note


def test_a_melius_record_has_no_skipped_components_and_sends_no_note():
    from tckdb_arc.adapter import _correction_records_from_record
    record = {"energy_corrections": [{
        "correction_type": "bond_additivity", "model": "melius", "level_of_theory": None,
        "total": {"value": -2.31, "unit": "kcal_mol"}, "components": [], "skipped_components": None,
    }]}
    (converted,) = _correction_records_from_record(record, schema_1_3=True)
    assert "note" not in converted and "components_are_applied_bonds" not in converted


# ---------------------------------------------------------------------------
# 2. Statmech: the treatment Arkane applied, torsion treatments as recorded, the rigid-rotor kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_the_statmech_treatment_is_what_arkane_applied(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    assert built.statmech["statmech_treatment"] == "rrho_1d"
    assert [t["treatment_kind"] for t in built.torsions()] == ["hindered_rotor", "free_rotor"]
    assert "statmech_treatment_not_stated" not in built.warning_codes()


@pytest.mark.parametrize("route", ROUTES)
def test_the_hessian_gate_is_retired_for_1_3(tmp_path, route):
    """Without a force-constant matrix 1.2 withheld the rotor treatment; 1.3 states what Arkane did."""
    with mock.patch.object(adapter_module.TCKDBAdapter, "_freq_hessian_available", return_value=False):
        built = build(tmp_path, route, _doc(), "sBuOH")
        assert built.statmech["statmech_treatment"] == "rrho_1d"
        assert [t["treatment_kind"] for t in built.torsions()] == ["hindered_rotor", "free_rotor"]
        assert "statmech_treatment_not_stated" not in built.warning_codes()
        older = build(tmp_path, route, _doc("1.2"), "sBuOH")
    assert "statmech_treatment" not in older.statmech
    assert all("treatment_kind" not in t for t in older.torsions())
    assert "statmech_treatment_not_stated" in older.warning_codes()


@pytest.mark.parametrize("route", ROUTES)
def test_a_species_whose_rotors_arkane_dropped_is_rrho_with_its_torsions_unclassified(tmp_path, route):
    built = build(tmp_path, route, _doc(), "nBuOH")
    assert built.statmech["statmech_treatment"] == "rrho"
    assert [t["torsion_index"] for t in built.torsions()] == [1, 2]
    # ``treatment`` is null in the record and is never defaulted to a hindered rotor.
    assert all("treatment_kind" not in t for t in built.torsions())
    assert "statmech_treatment_not_stated" not in built.warning_codes()


@pytest.mark.parametrize("route", ROUTES)
def test_before_1_3_a_torsion_without_a_treatment_is_dropped_and_the_treatment_inferred(tmp_path, route):
    """The same record relabelled 1.2: ARC's rotor list, not Arkane's output, decides, and a rotor without a
    recognised treatment is omitted."""
    doc = _doc("1.2")
    torsions = _species(doc, "nBuOH")["statmech"]["torsions"]
    torsions[0]["treatment"] = "hindered_rotor"
    with mock.patch.object(adapter_module.TCKDBAdapter, "_freq_hessian_available", return_value=True):
        built = build(tmp_path, route, doc, "nBuOH")
    assert [t["torsion_index"] for t in built.torsions()] == [1]
    assert built.torsions()[0]["treatment_kind"] == "hindered_rotor"
    # The pre-1.3 inference, from the rotors sent, is a rotor treatment: not what Arkane stated ("rrho").
    assert built.statmech["statmech_treatment"] == "rrho_1d"


@pytest.mark.parametrize("route", ROUTES)
def test_an_unrecorded_arkane_treatment_is_not_inferred_from_the_rotor_list(tmp_path, route):
    doc = _doc()
    statmech = _species(doc, "sBuOH")["statmech"]
    statmech["arkane_treatment"] = None
    statmech["arkane_rotors_applied"] = None
    for torsion in statmech["torsions"]:
        torsion["treatment"] = None
    built = build(tmp_path, route, doc, "sBuOH")
    assert "statmech_treatment" not in built.statmech
    assert len(built.torsions()) == 2
    (warning,) = [w for w in built.warnings if w["code"] == "statmech_treatment_not_stated"]
    assert warning["context"]["reason"] == "arkane_treatment_not_recorded"


def test_a_torsion_keeps_the_treatment_the_record_states_even_when_the_summary_is_unknown(tmp_path):
    doc = _doc()
    statmech = _species(doc, "sBuOH")["statmech"]
    statmech["arkane_treatment"] = None
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert [t["treatment_kind"] for t in built.torsions()] == ["hindered_rotor", "free_rotor"]


def test_a_rotor_treatment_is_withheld_when_no_torsion_is_sent(tmp_path):
    """TCKDB refuses a rotor-aware treatment that lists no rotor."""
    doc = _doc()
    statmech = _species(doc, "sBuOH")["statmech"]
    statmech["torsions"] = []
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "statmech_treatment" not in built.statmech
    (warning,) = [w for w in built.warnings if w["code"] == "statmech_treatment_not_stated"]
    assert warning["context"]["reason"] == "no_torsion_sent_for_rotor_treatment"


def test_an_unrecognised_arkane_treatment_is_not_sent(tmp_path):
    doc = _doc()
    _species(doc, "sBuOH")["statmech"]["arkane_treatment"] = "rrao"
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "statmech_treatment" not in built.statmech
    (warning,) = [w for w in built.warnings if w["code"] == "statmech_treatment_not_stated"]
    assert warning["context"]["reason"] == "arkane_treatment_unrecognized"


@pytest.mark.parametrize("treatment", ["rrho", "rrho_1d", "rrho_nd", "rrho_1d_nd"])
def test_every_treatment_arc_can_state_is_sent_as_stated(tmp_path, treatment):
    doc = _doc()
    _species(doc, "sBuOH")["statmech"]["arkane_treatment"] = treatment
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert built.statmech["statmech_treatment"] == treatment


def test_a_null_torsion_treatment_survives_only_for_1_3():
    torsions = [{"treatment": None, "atom_indices": [1, 2, 3, 4], "symmetry_number": 3},
                {"treatment": "free_rotor", "atom_indices": [2, 3, 4, 5], "symmetry_number": 1},
                {"treatment": "rigid_top", "atom_indices": [2, 3, 4, 5], "symmetry_number": 1}]
    kept = _build_slim_torsions(torsions, schema_1_3=True)
    assert [t["torsion_index"] for t in kept] == [1, 2]
    assert "treatment_kind" not in kept[0] and kept[1]["treatment_kind"] == "free_rotor"
    assert [t["torsion_index"] for t in _build_slim_torsions(torsions)] == [2]


@pytest.mark.parametrize("route", ROUTES)
def test_a_symmetric_top_is_sent_as_stated(tmp_path, route):
    if route == "computed_reaction":
        pytest.skip("NH3 is not a participant of the fixture's reaction")
    built = build(tmp_path, route, _doc(), "NH3")
    assert built.statmech["rigid_rotor_kind"] == "symmetric_top"
    assert built.statmech["statmech_treatment"] == "rrho"


@pytest.mark.parametrize("kind,sent", [
    ("spherical_top", "spherical_top"), ("linear", "linear"), ("asymmetric_top", "asymmetric_top"), (None, None),
])
def test_the_rigid_rotor_kind_is_sent_as_stated_and_omitted_when_null(tmp_path, kind, sent):
    doc = _doc()
    _species(doc, "NH3")["statmech"]["rigid_rotor_kind"] = kind
    built = build(tmp_path, "computed_species", doc, "NH3")
    assert built.statmech.get("rigid_rotor_kind") == sent


def test_the_e0_switches_and_the_rotor_count_are_not_deposited(tmp_path):
    """TCKDB's statmech has no E0 and no rotor-count field; the adapter deposits no E0 either (so no E0 is
    mistaken for a formation enthalpy)."""
    built = build(tmp_path, "computed_species", _doc(), "sBuOH")
    text = json.dumps(built.payload)
    for key in ("e0_kj_mol", "e0_atom_corrections_applied", "e0_bond_corrections_applied",
                "arkane_rotors_applied", "enthalpy_formation_0k_kj_mol"):
        assert key not in text


# ---------------------------------------------------------------------------
# 3. Kinetics provenance and the reaction's reversibility
# ---------------------------------------------------------------------------


def test_the_kinetics_note_carries_the_arkane_comment_the_ts_validation_and_the_switch(tmp_path):
    payload, _ = build_reaction(tmp_path, _doc())
    (kinetics,) = payload["kinetics"]
    lines = kinetics["note"].splitlines()
    assert lines[0] == "Fitted by Arkane over 300-3000 K."
    assert "Fitted to 50 data points; dA = *|/ 1.48" in lines
    # The marker already ends the comment, so it is not repeated.
    assert kinetics["note"].count("TS failed the IRC check") == 1
    assert lines[-1].startswith("Arkane kinetics run atom energy corrections: applied.")


def test_a_ts_validation_the_comment_does_not_carry_is_added_to_the_note(tmp_path):
    doc = _doc()
    doc["reactions"][0]["kinetics"]["comment"] = "Fitted to 50 data points."
    payload, _ = build_reaction(tmp_path, doc)
    note = payload["kinetics"][0]["note"]
    assert "Fitted to 50 data points." in note and "TS failed the IRC check" in note


@pytest.mark.parametrize("switch,wording", [(True, "applied."), (False, "not applied")])
def test_the_atom_correction_switch_of_the_kinetics_run_is_stated(tmp_path, switch, wording):
    doc = _doc()
    doc["reactions"][0]["kinetics"]["atom_corrections_applied"] = switch
    payload, _ = build_reaction(tmp_path, doc)
    assert f"atom energy corrections: {wording}" in payload["kinetics"][0]["note"]


def test_an_unstated_switch_and_absent_comment_leave_only_the_description(tmp_path):
    doc = _doc()
    kinetics = doc["reactions"][0]["kinetics"]
    kinetics.update(atom_corrections_applied=None, comment=None, ts_validation=None)
    payload, _ = build_reaction(tmp_path, doc)
    assert payload["kinetics"][0]["note"] == "Fitted by Arkane over 300-3000 K."


def test_the_kinetics_keys_are_not_read_before_1_3(tmp_path):
    payload, _ = build_reaction(tmp_path, _doc("1.2"))
    assert payload["kinetics"][0]["note"] == "Fitted by Arkane over 300-3000 K."
    assert "reversible" not in payload  # the schema default applies; 1.3 states it (below)


@pytest.mark.parametrize("stated,expected", [(True, True), (False, False)])
def test_a_stated_reversibility_is_sent_on_the_reaction_bundle(tmp_path, stated, expected):
    doc = _doc()
    doc["reactions"][0]["reversible"] = stated
    payload, _ = build_reaction(tmp_path, doc)
    assert payload["reversible"] is expected


def test_an_unknown_arrow_leaves_the_schema_default_on_the_reaction_bundle(tmp_path):
    from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
    doc = _doc()
    doc["reactions"][0]["reversible"] = None
    payload, _ = build_reaction(tmp_path, doc)
    assert ComputedReactionUploadRequest.model_fields["reversible"].default is True
    assert "reversible" not in payload


def test_the_bundle_does_not_read_reversible_before_1_3(tmp_path):
    doc = _doc("1.2")
    doc["reactions"][0]["reversible"] = False
    payload, _ = build_reaction(tmp_path, doc)
    assert "reversible" not in payload


@pytest.mark.parametrize("stated,expected", [(True, True), (False, False), (None, True)])
def test_the_standalone_ts_route_sends_a_stated_reversibility(tmp_path, stated, expected):
    doc = _doc()
    doc["reactions"][0]["reversible"] = stated
    payload, _ = build_ts(tmp_path, doc)
    assert payload["reaction"]["reversible"] is expected


def test_the_standalone_ts_route_keeps_true_before_1_3(tmp_path):
    doc = _doc("1.2")
    doc["reactions"][0]["reversible"] = False
    payload, _ = build_ts(tmp_path, doc)
    assert payload["reaction"]["reversible"] is True


def test_the_reference_temperature_is_sent_with_the_unnormalised_pre_exponential_factor(tmp_path):
    """tckdb-schemas 0.63: ``t0_k`` is T0 of k = A (T/T0)^n exp(-Ea/RT) and ``a`` is stored as sent."""
    doc = _doc()
    kinetics = doc["reactions"][0]["kinetics"]
    kinetics.update(T0_k=298.15, A=1.2e5, n=2.1)
    payload, _ = build_reaction(tmp_path, doc)
    assert payload["kinetics"][0]["a"] == 1.2e5
    assert payload["kinetics"][0]["t0_k"] == 298.15


def test_a_null_reference_temperature_still_omits_a(tmp_path):
    doc = _doc()
    doc["reactions"][0]["kinetics"]["T0_k"] = None
    payload, _ = build_reaction(tmp_path, doc)
    assert "a" not in payload["kinetics"][0] and "a_units" not in payload["kinetics"][0]


# ---------------------------------------------------------------------------
# 4. Correction-scheme identity (tckdb-schemas 0.62): ``data_revision`` is the RMG-database table's
#    revision and the Arkane build stays on ``workflow_tool_release`` as provenance only
# ---------------------------------------------------------------------------

ARKANE = {"name": "Arkane", "version": "3.3.0", "git_commit": "6b1368de6c19204c7ce4fda6fbecb05da4a0fe0e"}


def _schemes(built):
    return {c["scheme"]["kind"]: c["scheme"] for c in built.corrections}


@pytest.mark.parametrize("route", ROUTES)
def test_a_package_database_names_its_version_as_the_data_revision(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    schemes = _schemes(built)
    assert set(schemes) == {"atom_energy", "bac_petersson"}
    for scheme in schemes.values():
        assert scheme["data_revision"] == "4.0.0"
        # The Arkane build that looked the table up is stamped as the tool release (provenance only now).
        assert scheme["workflow_tool_release"] == ARKANE


def test_the_ts_block_scheme_carries_the_data_revision_too(tmp_path):
    payload, _ = build_reaction(tmp_path, _doc())
    (correction,) = payload["transition_state"]["applied_energy_corrections"]
    assert correction["scheme"]["data_revision"] == "4.0.0"
    assert correction["scheme"]["workflow_tool_release"] == ARKANE


@pytest.mark.parametrize("route", ROUTES)
def test_a_git_database_is_identified_by_its_commit(tmp_path, route):
    doc = _doc()
    doc["rmg_database"].update(path_kind="git", git_commit="f" * 40, version=None)
    built = build(tmp_path, route, doc, "sBuOH")
    for scheme in _schemes(built).values():
        assert scheme["data_revision"] == "f" * 40
        assert scheme["workflow_tool_release"] == ARKANE


def test_a_commit_is_sent_lower_cased_the_way_tckdb_stores_it_and_a_tag_as_written():
    doc = _doc()
    doc["rmg_database"].update(path_kind="git", git_commit="ABCDEF0123456789" * 2 + "ABCDEF01", version=None)
    assert _scheme_data_revisions(doc)["atom_energy"] == "abcdef0123456789" * 2 + "abcdef01"
    doc["rmg_database"].update(git_commit="v4.0.0-rc.1")      # not 7-64 hex: kept exactly
    assert _scheme_data_revisions(doc)["atom_energy"] == "v4.0.0-rc.1"


def test_a_database_of_unknown_origin_is_identified_by_its_digest_as_a_plain_string():
    doc = _doc()
    doc["rmg_database"].update(path_kind="unknown", git_commit=None, version=None,
                               quantum_corrections_sha256=SHA.upper())
    revisions = _scheme_data_revisions(doc)
    assert revisions == {kind: SHA for kind in ("atom_energy", "bac_petersson", "bac_melius")}
    assert not revisions["atom_energy"].startswith("sha256:")


def test_a_revision_longer_than_the_contract_allows_is_not_sent():
    doc = _doc()
    doc["rmg_database"].update(path_kind="package", version="v" * 201)
    assert _scheme_data_revisions(doc)["atom_energy"] == "a" * 64      # falls through to the digest


def test_a_revised_table_is_a_new_scheme_and_an_unrelated_arkane_build_is_not():
    """Scheme identity is (kind, name, level, literature, software, data_revision): the build is not part of it."""
    first, second = _doc(), _doc()
    first["rmg_database"].update(path_kind="unknown", version=None, quantum_corrections_sha256="1" * 64)
    second["rmg_database"].update(path_kind="unknown", version=None, quantum_corrections_sha256="2" * 64)
    assert _scheme_data_revisions(first)["bac_petersson"] != _scheme_data_revisions(second)["bac_petersson"]
    third = copy.deepcopy(first)
    third["arkane_git_commit"] = "0" * 40
    assert _scheme_data_revisions(third) == _scheme_data_revisions(first)


@pytest.mark.parametrize("identity", [
    {"path_kind": "git", "git_commit": None, "version": None, "quantum_corrections_sha256": None},
    {"path_kind": "git", "git_commit": "f" * 201, "version": None, "quantum_corrections_sha256": None},
    {"path_kind": "package", "git_commit": None, "version": None, "quantum_corrections_sha256": None},
    {"path_kind": "unknown", "git_commit": None, "version": None, "quantum_corrections_sha256": "short"},
])
def test_an_unusable_database_identity_sends_no_revision_and_keeps_the_arkane_build(tmp_path, identity):
    doc = _doc()
    doc["rmg_database"].update(identity)
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert _scheme_data_revisions(doc) == {}
    for scheme in _schemes(built).values():
        assert "data_revision" not in scheme and scheme["workflow_tool_release"] == ARKANE


@pytest.mark.parametrize("route", ROUTES)
def test_an_atom_energy_record_is_not_deposited_when_arc_rendered_the_atom_energies_from_aec_yml(tmp_path, route):
    """ARC renders atomEnergies from data/AEC.yml whenever it matches, yet recomputes the exported record from
    data.py, so for a level both files cover the record may describe the wrong table. The BAC is unaffected."""
    doc = _doc()
    doc["arc_aec_yml_sha256"] = SHA
    built = build(tmp_path, route, doc, "sBuOH")
    assert [c["application_role"] for c in built.corrections] == ["bac_total"]
    assert _schemes(built)["bac_petersson"]["data_revision"] == "4.0.0"
    warnings = [w for w in built.warnings if w["code"] == "atom_energy_record_not_deposited_aec_yml"]
    assert warnings and all(w["context"]["arc_aec_yml_sha256"] == SHA for w in warnings)
    assert SHA not in json.dumps(built.payload)


def test_the_aec_yml_digest_is_ignored_before_1_3_and_when_malformed(tmp_path):
    doc = _doc("1.2")
    doc["arc_aec_yml_sha256"] = SHA
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "aec_total" in [c["application_role"] for c in built.corrections]
    doc = _doc()
    doc["arc_aec_yml_sha256"] = "short"
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "aec_total" in [c["application_role"] for c in built.corrections]


@pytest.mark.parametrize("route", ROUTES)
def test_before_1_3_the_scheme_keeps_the_arkane_build_and_states_no_revision(tmp_path, route):
    doc = _doc("1.2")
    built = build(tmp_path, route, doc, "sBuOH")
    for scheme in _schemes(built).values():
        assert scheme["workflow_tool_release"]["name"] == "Arkane" and scheme["workflow_tool_release"]["version"] == "3.3.0"
        assert "data_revision" not in scheme


@pytest.mark.parametrize("route", ROUTES)
def test_the_atom_energy_scheme_states_how_its_atom_params_are_applied(tmp_path, route):
    """0.62 ``atom_params_applied_as``: ARC records ``applied_as: subtracted`` with the table."""
    built = build(tmp_path, route, _doc(), "sBuOH")
    schemes = _schemes(built)
    assert schemes["atom_energy"]["atom_params"]
    assert schemes["atom_energy"]["atom_params_applied_as"] == "subtracted"
    # It "covers every entry of atom_params and nothing else": a scheme without atom_params states none.
    assert "atom_params_applied_as" not in schemes["bac_petersson"]


def test_applied_as_is_sent_as_arc_states_it_and_never_without_the_params_it_covers(tmp_path):
    for stated, sent in (("added", "added"), (None, None), ("other", None)):
        doc = _doc()
        for record in doc["species"]:
            for correction in record.get("energy_corrections") or []:
                if correction.get("reference_atom_energies"):
                    correction["reference_atom_energies"]["applied_as"] = stated
        built = build(tmp_path / str(stated), "computed_species", doc, "sBuOH")
        assert _schemes(built)["atom_energy"].get("atom_params_applied_as") == sent
    doc = _doc()
    for record in doc["species"]:
        for correction in record.get("energy_corrections") or []:
            if correction.get("reference_atom_energies"):
                correction["reference_atom_energies"]["values"] = {}     # no usable table: no atom_params
    scheme = _schemes(build(tmp_path / "noparams", "computed_species", doc, "sBuOH"))["atom_energy"]
    assert "atom_params" not in scheme and "atom_params_applied_as" not in scheme


# ---------------------------------------------------------------------------
# 5. TS frequencies in ESS order and the reaction-coordinate mode
# ---------------------------------------------------------------------------


def _ts_freq_calc(payload):
    ts = payload["transition_state"]
    return next(c for c in [ts["calculation"], *ts["calculations"]] if c["type"] == "freq")


def _modes(calc):
    return [(m["mode_index"], m["frequency_cm1"], m.get("imaginary_disposition"))
            for m in calc["freq_result"]["modes"]]


def test_the_reaction_route_sends_the_ts_frequencies_in_ess_order_with_arcs_index(tmp_path):
    payload, _ = build_reaction(tmp_path, _doc())
    calc = _ts_freq_calc(payload)
    assert calc["freq_frequencies_cm1"] == [-1235.4, -18.2, 1300.0, 1500.0]
    assert calc["freq_reaction_coordinate_mode_index"] == 1
    assert calc["freq_imaginary_dispositions"] == {"2": "unassigned"} or \
        calc["freq_imaginary_dispositions"] == {2: "unassigned"}
    assert calc["freq_imag_freq_cm1"] == -1235.4


def test_the_standalone_ts_route_sends_modes_in_ess_order_with_arcs_index(tmp_path):
    payload, _ = build_ts(tmp_path, _doc())
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert _modes(calc) == [(1, -1235.4, None), (2, -18.2, "unassigned"), (3, 1300.0, None), (4, 1500.0, None)]
    assert calc["freq_result"]["reaction_coordinate_mode_index"] == 1
    assert calc["freq_result"]["n_imag"] == 2


def test_the_designated_mode_follows_arcs_index_not_the_most_negative_frequency(tmp_path):
    """ARC validated the second listed mode, and the first is a larger artifact."""
    doc = _doc()
    ts = doc["transition_states"][0]
    ts["freq_frequencies_cm1_ess_order"] = [-12000.0, -1235.4, 1300.0, 1500.0]
    ts["imaginary_frequencies_cm1"] = [-12000.0, -1235.4]
    ts["reaction_coordinate_mode_index"] = 2
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert _modes(calc)[:2] == [(1, -12000.0, "unassigned"), (2, -1235.4, None)]
    assert calc["freq_result"]["reaction_coordinate_mode_index"] == 2
    assert calc["freq_result"]["imag_freq_cm1"] == -1235.4


def test_modes_keep_the_position_the_ess_printed_when_the_imaginary_mode_is_not_first(tmp_path):
    doc = _doc()
    ts = doc["transition_states"][0]
    ts.update(freq_frequencies_cm1_ess_order=[100.0, -900.0, 1300.0, 1500.0], freq_n_imag=1,
              imaginary_frequencies_cm1=[-900.0], reaction_coordinate_mode_index=2)
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert _modes(calc) == [(1, 100.0, None), (2, -900.0, None), (3, 1300.0, None), (4, 1500.0, None)]
    assert calc["freq_result"]["reaction_coordinate_mode_index"] == 2


def test_a_ts_with_several_stiff_imaginary_modes_and_no_stated_index_is_refused(tmp_path):
    doc = _doc()
    _ts_with(doc, [-1235.4, -300.0, 1300.0, 1500.0], None)
    with pytest.raises(ValueError, match="ts_reaction_coordinate_not_designated"):
        build_ts(tmp_path, doc)


def test_an_index_that_names_a_real_mode_is_not_used(tmp_path):
    doc = _doc()
    _ts_with(doc, [-1235.4, -300.0, 1300.0, 1500.0], 3)
    with pytest.raises(ValueError, match="ts_reaction_coordinate_not_designated"):
        build_ts(tmp_path, doc)
    # One imaginary mode: nothing to designate, and the unusable index is not sent.
    _ts_with(doc, [100.0, -900.0, 1300.0, 1500.0], 4)
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert "reaction_coordinate_mode_index" not in calc["freq_result"]
    assert len(calc["freq_result"]["modes"]) == 4


def test_ess_frequencies_without_the_imaginary_modes_are_not_padded_with_guessed_ones(tmp_path):
    """Re-inserting the imaginary modes in front would shift ARC's index off the mode it names."""
    doc = _doc()
    doc["transition_states"][0].update(
        freq_frequencies_cm1_ess_order=[1300.0, 1500.0], freq_n_imag=1,
        imaginary_frequencies_cm1=[-900.0], reaction_coordinate_mode_index=None)
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert "modes" not in calc["freq_result"]


def test_before_1_3_the_imaginary_modes_are_reinserted_and_designated_by_the_window(tmp_path):
    doc = _doc("1.2")
    ts = doc["transition_states"][0]
    ts["reaction_coordinate_mode_index"] = None
    ts["statmech"]["harmonic_frequencies_cm1"] = [1300.0, 1500.0]
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert [m["frequency_cm1"] for m in calc["freq_result"]["modes"]] == [-1235.4, -18.2, 1300.0, 1500.0]
    # (75, 10000) cm-1 window: only the first is a candidate.
    assert calc["freq_result"]["reaction_coordinate_mode_index"] == 1


def test_a_species_record_never_reads_the_ts_keys(tmp_path):
    doc = _doc()
    sbuoh = _species(doc, "sBuOH")
    sbuoh["freq_frequencies_cm1_ess_order"] = [-500.0, 100.0]
    sbuoh["reaction_coordinate_mode_index"] = 1
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "reaction_coordinate_mode_index" not in json.dumps(built.payload)


# ---------------------------------------------------------------------------
# 6. Parsed properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_the_t1_diagnostic_goes_on_the_sp_calculation(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    (sp,) = built.calcs("sp")
    assert sp["wavefunction_diagnostic"] == {"t1_diagnostic": pytest.approx(0.0123)}
    assert all("wavefunction_diagnostic" not in c for c in built.calcs("freq") + built.calcs("opt"))


@pytest.mark.parametrize("route", ROUTES)
def test_a_null_t1_sends_no_block(tmp_path, route):
    built = build(tmp_path, route, _doc(), "nBuOH")
    assert all("wavefunction_diagnostic" not in c for c in built.calcs("sp"))


@pytest.mark.parametrize("route", ROUTES)
def test_the_t1_diagnostic_is_not_read_before_1_3(tmp_path, route):
    built = build(tmp_path, route, _doc("1.2"), "sBuOH")
    assert all("wavefunction_diagnostic" not in c for c in built.calcs("sp"))


@pytest.mark.parametrize("value,sent", [
    (0.0, True), (0.05, True), (-0.01, False), (math.nan, False), (math.inf, False),
    (True, False), ("0.01", False), (None, False),
])
def test_only_a_finite_non_negative_number_is_a_t1_diagnostic(value, sent):
    block = _wavefunction_diagnostic_payload({"sp_t1_diagnostic": value})
    assert (block is not None) is sent


def test_the_t1_diagnostic_reaches_the_ts_sp_calculation(tmp_path):
    doc = _doc()
    doc["transition_states"][0]["sp_t1_diagnostic"] = 0.031
    payload, _ = build_reaction(tmp_path, doc)
    ts = payload["transition_state"]
    (sp,) = [c for c in [ts["calculation"], *ts["calculations"]] if c["type"] == "sp"]
    assert sp["wavefunction_diagnostic"] == {"t1_diagnostic": pytest.approx(0.031)}
    standalone, _ = build_ts(tmp_path, doc)
    (sp,) = [c for c in standalone["additional_calculations"] if c["type"] == "sp"]
    assert sp["wavefunction_diagnostic"] == {"t1_diagnostic": pytest.approx(0.031)}


@pytest.mark.parametrize("route", ROUTES)
def test_the_spin_diagnostic_extensions_are_sent(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    (sp,) = built.calcs("sp")
    assert sp["spin_diagnostic"] == {
        "s_squared": pytest.approx(0.7646), "s_squared_expected": pytest.approx(0.0),
        "s_squared_annihilated": pytest.approx(0.7502)}


def test_a_spin_diagnostic_without_the_extensions_sends_only_s_squared(tmp_path):
    doc = _doc()
    block = _species(doc, "sBuOH")["sp_spin_diagnostic"]
    del block["s_squared_expected"], block["s_squared_annihilated"]
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    (sp,) = built.calcs("sp")
    assert sp["spin_diagnostic"] == {"s_squared": pytest.approx(0.7646)}


@pytest.mark.parametrize("route", ROUTES)
def test_the_hessian_method_is_still_a_freq_parameter(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    (freq,) = built.calcs("freq")
    assert {"raw_key": "freq_hessian_method", "raw_value": "analytic", "canonical_key": "freq.hessian_method",
            "canonical_value": "analytic", "section": "freq", "value_type": "string"} in freq["parameters"]


@pytest.mark.parametrize("route", ROUTES)
def test_the_dipole_moment_and_polarizability_have_no_home_on_these_routes(tmp_path, route):
    """TCKDB holds them only on the standalone transport record (``dipole_debye``,
    ``polarizability_angstrom3``), which the adapter does not upload."""
    doc = _doc()
    sbuoh = _species(doc, "sBuOH")
    assert sbuoh["opt_dipole_moment_debye"] == pytest.approx(0.2355)
    sbuoh["freq_polarizability_angstrom3"] = 4.2
    built = build(tmp_path, route, doc, "sBuOH")
    text = json.dumps(built.payload).lower()
    assert "dipole" not in text and "polariz" not in text and "0.2355" not in text


# ---------------------------------------------------------------------------
# 7. The 1.2 thermo flags are unchanged under 1.3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["computed_species", "conformer", "computed_reaction"])
def test_the_thermo_block_is_identical_under_1_2_and_1_3(tmp_path, route):
    newer = build(tmp_path, route, _doc("1.3"), "sBuOH")
    older = build(tmp_path, route, _doc("1.2"), "sBuOH")
    assert newer.thermo == older.thermo
    assert [w["code"] for w in newer.warnings if w["code"].startswith(("enthalpy", "thermo"))] == \
        [w["code"] for w in older.warnings if w["code"].startswith(("enthalpy", "thermo"))]


def test_the_atom_corrections_level_mismatch_still_strips_the_enthalpy(tmp_path):
    built = build(tmp_path, "computed_species", _doc(), "NH3")
    assert "enthalpy_atom_corrections_level_mismatch" in built.warning_codes()
    assert "h298_kj_mol" not in built.thermo and "enthalpy_reference_kind" not in built.thermo


def test_a_matching_atom_corrections_level_keeps_the_formation_enthalpy(tmp_path):
    doc = _doc()
    nh3 = _species(doc, "NH3")
    nh3["thermo"].update(nasa_low=None, nasa_high=None)  # the fixture's placeholder coefficients are not physical
    nh3["thermo"]["atom_corrections_level"] = dict(doc["sp_level"]) if doc.get("sp_level") else \
        {"method": "dlpno-ccsd(t)", "basis": "cc-pvtz", "software": "orca"}
    built = build(tmp_path, "computed_species", doc, "NH3")
    assert built.thermo["enthalpy_reference_kind"] == "formation_298k"
    assert built.thermo["h298_kj_mol"] == pytest.approx(nh3["thermo"]["h298_kj_mol"])


def test_an_uncorrected_run_still_strips_the_enthalpy(tmp_path):
    doc = _doc()
    thermo = _species(doc, "NH3")["thermo"]
    thermo.update(atom_corrections_applied=False, bond_corrections_applied=False, atom_corrections_level=None)
    built = build(tmp_path, "computed_species", doc, "NH3")
    assert "enthalpy_atom_corrections_not_applied" in built.warning_codes()
    assert "h298_kj_mol" not in built.thermo


# ---------------------------------------------------------------------------
# Review follow-ups: tau, pre-1.3 switched-off records
# ---------------------------------------------------------------------------


def _ts_with(doc, ess, index, n_imag=None):
    ts = doc["transition_states"][0]
    ts["freq_frequencies_cm1_ess_order"] = ess
    ts["reaction_coordinate_mode_index"] = index
    imag = sorted(f for f in ess if f < 0)
    ts["imaginary_frequencies_cm1"] = imag
    ts["freq_n_imag"] = n_imag if n_imag is not None else len(imag)


def test_without_a_stated_index_the_one_mode_above_tau_is_the_reaction_coordinate(tmp_path):
    """TCKDB's noise floor with no recorded protocol is 50 cm-1: -18.2 is noise, -1235.4 is real."""
    from tckdb_schemas.stationary_point import TAU_PROTOCOL_NOT_RECORDED_CM1
    assert adapter_module._TS_TAU_PROTOCOL_NOT_RECORDED_CM1 == TAU_PROTOCOL_NOT_RECORDED_CM1
    doc = _doc()
    _ts_with(doc, [-18.2, 100.0, -1235.4, 1500.0], None)
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert _modes(calc)[:3] == [(1, -18.2, "unassigned"), (2, 100.0, None), (3, -1235.4, None)]
    assert calc["freq_result"]["reaction_coordinate_mode_index"] == 3
    reaction, _ = build_reaction(tmp_path, doc)
    assert _ts_freq_calc(reaction)["freq_reaction_coordinate_mode_index"] == 3


@pytest.mark.parametrize("ess", [[-900.0, -300.0, 1300.0], [-40.0, -20.0, 1300.0]])
def test_two_modes_at_or_above_tau_or_none_is_refused_with_a_structured_reason(tmp_path, ess):
    doc = _doc()
    _ts_with(doc, ess, None)
    with pytest.raises(adapter_module.TSReactionCoordinateNotDesignated) as info:
        build_ts(tmp_path, doc)
    warning = info.value.warning
    assert warning["code"] == "ts_reaction_coordinate_not_designated"
    assert warning["context"]["tau_cm1"] == 50.0
    assert warning["context"]["n_above_tau"] == sum(abs(f) >= 50 for f in ess if f < 0)
    assert str(info.value).startswith("[ts_reaction_coordinate_not_designated]")


def test_the_tau_rule_does_not_apply_before_1_3(tmp_path):
    doc = _doc("1.2")
    ts = doc["transition_states"][0]
    ts["statmech"]["harmonic_frequencies_cm1"] = [1300.0, 1500.0]
    ts["freq_frequencies_cm1_ess_order"] = [-900.0, -300.0, 1300.0]
    ts["imaginary_frequencies_cm1"] = [-900.0, -300.0]
    ts["reaction_coordinate_mode_index"] = None
    # The pre-1.3 window designates the largest in (75, 10000): unchanged, and ESS order is not read.
    payload, _ = build_ts(tmp_path, doc)
    calc = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
    assert [m["frequency_cm1"] for m in calc["freq_result"]["modes"]] == [-900.0, -300.0, 1300.0, 1500.0]


@pytest.mark.parametrize("route", ROUTES)
def test_before_1_3_a_record_its_thermo_run_did_not_apply_is_not_deposited(tmp_path, route):
    doc = _doc("1.2")
    sbuoh = _species(doc, "sBuOH")
    sbuoh["thermo"].update(atom_corrections_applied=False, bond_corrections_applied=False,
                           atom_corrections_level=None)
    built = build(tmp_path, route, doc, "sBuOH")
    assert built.corrections == []
    doc = _doc("1.2")
    _species(doc, "sBuOH")["thermo"]["bond_corrections_applied"] = False
    built = build(tmp_path, route, doc, "sBuOH")
    assert [c["application_role"] for c in built.corrections] == ["aec_total"]
