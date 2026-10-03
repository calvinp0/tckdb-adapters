"""What the adapter sends as transition-state validation evidence obeys tckdb-schemas 0.64's rules.

0.64 added two evidence kinds (``energy_ordering``, ``imaginary_mode``) and tightened the record:
"A pass that the record's own *stated* numbers contradict is refused, a field is refused on a kind it
does not describe, and only a passing ``irc`` record silences ``transition_state_missing_irc_evidence``.
``energy_ordering`` is accepted on the computed-reaction ... bundles and refused on the standalone
transition-state upload ...; ``imaginary_mode`` binds there to the single ``freq`` additional
calculation ... ``imaginary_frequency_cm1`` is finite ... a pass with more than one imaginary mode
needs that result to designate the reaction coordinate."

The adapter sends ``irc`` (ARC's ``ts_checks['IRC']``), ``imaginary_mode`` (``ts_checks['freq']`` and
the TS frequency result) and, on the bundle only, ``energy_ordering`` (``ts_checks['e_elect']`` and each
participant's ``sp_energy_hartree``), so the rules that can bite it are: fields belong to the kind, the
participant mappings (both sides or neither, non-empty, 1-based, no repeated atom), the source binding on
each route (``energy_ordering`` names a calculation per energy, of the participant it is the energy of,
and is refused standalone), one record per kind, a pass the record's own numbers contradict, an
``imaginary_mode`` count or frequency that disagrees with the cited frequency result (1 cm^-1), and the
TS frequency result designating its reaction coordinate. The replica is
``tckdb_core.ts_evidence_rules.offline_evidence_errors`` (the published model and JSON Schema also run on
every built request via the conftest hook; the route handlers do not; the two checks that need the stored
calculation, the ownership of each energy's source and the freq-result cross-check, are replicated from
``app/services/transition_state_validation.py``). This module reads ARC's ``ts_checks`` into requests and
runs the replica on them.
"""

import copy
import math
import re

import pytest

from test_arc_1_3_reactions import (
    ROUTES, _evidence, _fixture, _reaction_payload, _ts_payload)
from tckdb_schemas.fragments.ts_validation_evidence import TransitionStateValidationEvidenceIn

from _contract import assert_fragment_matches_contract
from tckdb_core.ts_evidence_rules import (  # noqa: F401  (the replica of TCKDB's rules moved to the core)
    COMMON_FIELDS,
    ENERGY_ORDERING_FIELDS,
    HARTREE_TO_KJ_MOL,
    IMAGINARY_MODE_FIELDS,
    IMAGINARY_TOLERANCE_CM1,
    IRC_FIELDS,
    KIND_FIELDS,
    OTHER_KIND_FIELDS,
    offline_evidence_errors,
)

def _block(payload, route):
    return payload if route == "transition_state" else payload["transition_state"]


def _kind(payload, route, kind):
    (record,) = [r for r in _block(payload, route)["validation_evidence"] if r["kind"] == kind]
    return record


def _fixture_with_a_passing_ordering():
    """The fixture's TS energy is put above both wells: ARC's ``e_elect`` verdict (True) is then not contradicted."""
    doc = _fixture()
    doc["transition_states"][0]["sp_energy_hartree"] = -116.19
    return doc


@pytest.mark.parametrize("route,build", ROUTES)
def test_the_irc_evidence_obeys_the_0_64_rules(route, build):
    payload, _ = build(_fixture())
    assert offline_evidence_errors(payload, standalone=route == "transition_state") == []
    record = _kind(payload, route, "irc")
    assert record["passed"] is True
    assert set(record) <= IRC_FIELDS
    # the published model accepts exactly what is sent
    TransitionStateValidationEvidenceIn.model_validate(record)


@pytest.mark.parametrize("route,build", ROUTES)
def test_every_record_sent_obeys_the_0_64_rules(route, build):
    """irc and imaginary_mode on both routes; energy_ordering on the bundle only."""
    payload, _ = build(_fixture_with_a_passing_ordering())
    assert offline_evidence_errors(payload, standalone=route == "transition_state") == []
    kinds = [r["kind"] for r in _block(payload, route)["validation_evidence"]]
    assert kinds == (["irc", "imaginary_mode", "energy_ordering"] if route == "computed_reaction"
                     else ["irc", "imaginary_mode"])
    for record in _block(payload, route)["validation_evidence"]:
        assert set(record) <= KIND_FIELDS[record["kind"]]
        TransitionStateValidationEvidenceIn.model_validate(record)
        assert_fragment_matches_contract(record, "TransitionStateValidationEvidenceIn")
        # only a passing irc record silences ``transition_state_missing_irc_evidence``: the new kinds are
        # never sent as a stand-in for it
        if record["kind"] != "irc":
            assert record["passed"] is True and record["rationale"]


@pytest.mark.parametrize("route,build", ROUTES)
def test_a_failed_irc_is_sent_as_a_failure_with_no_mapping(route, build):
    """Only a passing irc record silences ``transition_state_missing_irc_evidence``; a failed one must not claim a map."""
    doc = _fixture()
    doc["transition_states"][0]["ts_checks"]["IRC"] = False
    payload, _ = build(doc)
    record = _kind(payload, route, "irc")
    assert record["passed"] is False
    assert "reactant_participant_mapping" not in record and "product_participant_mapping" not in record
    assert offline_evidence_errors(payload, standalone=route == "transition_state") == []
    TransitionStateValidationEvidenceIn.model_validate(record)


@pytest.mark.parametrize("route,build", ROUTES)
def test_no_verdict_sends_no_evidence_at_all(route, build):
    doc = _fixture()
    doc["transition_states"][0]["ts_checks"].update(IRC=None, freq=None, e_elect=None)
    payload, _ = build(doc)
    assert "validation_evidence" not in _block(payload, route)


# ---------------------------------------------------------------------------
# The checker (and so the replication) is real: each rule is refused by the published model
# ---------------------------------------------------------------------------

def _record():
    payload, _ = _reaction_payload(_fixture())
    return copy.deepcopy(_evidence(payload))


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.update(energies=[{"participant": "ts", "energy_kind": "electronic", "energy_hartree": -1.0,
                                   "source_calculation_key": "ts_sp"}]), None),
    (lambda r: r.update(imaginary_frequency_cm1=-1200.0), None),
    (lambda r: r.update(imaginary_frequency_count=1), None),
    (lambda r: r.update(mode_displacement_agrees=True), None),
    (lambda r: r.pop("product_participant_mapping"), "both sides or neither"),
    (lambda r: r["reactant_participant_mapping"].update({"reactant:1": [0, 2, 3, 4, 5]}), "1-based"),
    (lambda r: r.update(reactant_participant_mapping={}), None),
])
def test_the_published_model_refuses_what_the_checker_flags(mutate, match):
    record = _record()
    mutate(record)
    with pytest.raises(ValueError, match=match):
        TransitionStateValidationEvidenceIn.model_validate(record)


def test_the_checker_flags_each_rule():
    payload, _ = _reaction_payload(_fixture())
    ts = payload["transition_state"]
    for mutate, needle in [
        (lambda t: t["validation_evidence"][0].update(energies=[]), "carries"),
        (lambda t: t["validation_evidence"][0].update(source_calculation_key="ts_freq"), "must name a irc"),
        (lambda t: t["validation_evidence"][0].pop("product_participant_mapping"), "one-sided"),
        (lambda t: t["validation_evidence"][0]["reactant_participant_mapping"]["reactant:1"].append(1), "repeats"),
        (lambda t: t["validation_evidence"].append(dict(t["validation_evidence"][0])), "more than one"),
        (lambda t: t["validation_evidence"][0].update(passed=False), "passing record"),
    ]:
        broken = copy.deepcopy(payload)
        mutate(broken["transition_state"])
        assert any(needle in e for e in offline_evidence_errors(broken, standalone=False)), needle
    standalone, _ = _ts_payload(_fixture())
    broken = copy.deepcopy(standalone)
    broken["validation_evidence"][0]["source_calculation_key"] = "ts_irc"
    assert any("must be omitted" in e for e in offline_evidence_errors(broken, standalone=True))
    broken = copy.deepcopy(standalone)
    broken["additional_calculations"] = [c for c in broken["additional_calculations"] if c["type"] != "irc"]
    assert any("single irc" in e for e in offline_evidence_errors(broken, standalone=True))
    del ts


# ---------------------------------------------------------------------------
# Imaginary modes: the TS frequency result designates its reaction coordinate
# ---------------------------------------------------------------------------

TAU_CM1 = 50.0      # TCKDB's default stiffness threshold when no protocol is recorded (ADR 0012)


def _ts_freq(block, *, standalone):
    calcs = [block["primary_opt"], *block.get("additional_calculations", [])] if standalone else [
        block["calculation"], *block.get("calculations", [])]
    return next(c for c in calcs if c["type"] == "freq")


def designation_errors(freq):
    """``validate_reaction_coordinate_contract`` as it can be replicated offline: at least one imaginary
    mode and exactly one reaction coordinate, stated by index or the one imaginary mode at or above tau."""
    if "freq_result" in freq:                      # the standalone request's nested result
        result = freq["freq_result"]
        frequencies = [m["frequency_cm1"] for m in result["modes"]]
        stated = result.get("reaction_coordinate_mode_index")
    else:                                          # the reaction bundle's flattened fields
        frequencies = freq["freq_frequencies_cm1"]
        stated = freq.get("freq_reaction_coordinate_mode_index")
    errors = []
    imaginary = [i + 1 for i, f in enumerate(frequencies) if f < 0]
    if not imaginary:
        return ["no imaginary mode"]
    if any(f != f or abs(f) == float("inf") for f in frequencies):
        errors.append("a frequency is not finite")
    if stated is not None:
        if stated not in imaginary:
            errors.append(f"reaction_coordinate_mode_index {stated} is not an imaginary mode")
    elif len([i for i in imaginary if abs(frequencies[i - 1]) >= TAU_CM1]) != 1:
        errors.append("no single imaginary mode at or above tau and none designated")
    return errors


@pytest.mark.parametrize("route,build", ROUTES)
def test_the_ts_frequency_result_designates_a_reaction_coordinate(route, build):
    """The single-imaginary-mode fixture: the one mode above tau is the coordinate, nothing else is claimed."""
    payload, _ = build(_fixture())
    block = payload if route == "transition_state" else payload["transition_state"]
    assert designation_errors(_ts_freq(block, standalone=route == "transition_state")) == []


def _thermo_fixture_ts(route):
    # Imported here: the 1.3 thermo fixture's TS states reaction_coordinate_mode_index and two imaginary modes.
    from test_arc_schema_1_3_thermo import _doc, build_reaction, build_ts
    import tempfile, pathlib
    doc = _doc()
    tmp = pathlib.Path(tempfile.mkdtemp())
    return (build_ts(tmp, doc) if route == "transition_state" else build_reaction(tmp, doc))[0]


@pytest.mark.parametrize("route", ["computed_reaction", "transition_state"])
def test_a_ts_with_several_imaginary_modes_states_its_reaction_coordinate(route):
    payload = _thermo_fixture_ts(route)
    block = payload if route == "transition_state" else payload["transition_state"]
    freq = _ts_freq(block, standalone=route == "transition_state")
    frequencies = (freq["freq_result"]["modes"] if "freq_result" in freq else None)
    imaginary = (len([m for m in frequencies if m["is_imaginary"]]) if frequencies is not None
                 else len([f for f in freq["freq_frequencies_cm1"] if f < 0]))
    assert imaginary > 1, "the fixture must carry more than one imaginary mode for this check to mean anything"
    assert designation_errors(freq) == []
    stated = (freq["freq_result"].get("reaction_coordinate_mode_index") if "freq_result" in freq
              else freq.get("freq_reaction_coordinate_mode_index"))
    assert stated is not None


def test_the_designation_checker_flags_each_failure():
    payload = _thermo_fixture_ts("computed_reaction")
    freq = _ts_freq(payload["transition_state"], standalone=False)
    for mutate, needle in [
        (lambda f: (f.pop("freq_reaction_coordinate_mode_index"), f["freq_frequencies_cm1"].__setitem__(1, -200.0)),
         "no single imaginary"),
        (lambda f: f.update(freq_reaction_coordinate_mode_index=len(f["freq_frequencies_cm1"])), "not an imaginary"),
        (lambda f: f.update(freq_frequencies_cm1=[abs(x) for x in f["freq_frequencies_cm1"]]), "no imaginary mode"),
        (lambda f: f["freq_frequencies_cm1"].__setitem__(0, float("nan")), "not finite"),
    ]:
        broken = copy.deepcopy(freq)
        mutate(broken)
        assert any(needle in e for e in designation_errors(broken)), needle


# ---------------------------------------------------------------------------
# 0.64: energy_ordering and imaginary_mode. The checker is real: each rule is refused by the published
# model (or by ``validate_ts_evidence_set``, which knows the reaction's participants), and flagged by
# the offline checker above.
# ---------------------------------------------------------------------------

from tckdb_schemas.enums import MoleculeKind
from tckdb_schemas.fragments.ts_validation_evidence import validate_ts_evidence_set


def _bundle():
    payload, _ = _reaction_payload(_fixture_with_a_passing_ordering())
    return payload


def _record_of(payload, kind):
    return copy.deepcopy(next(r for r in payload["transition_state"]["validation_evidence"] if r["kind"] == kind))


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.update(energies=[]), "requires the energies"),
    (lambda r: r.update(source_calculation_key="ts_sp"), "source_calculation_key is not accepted"),
    (lambda r: r["energies"].append(dict(r["energies"][0])), "more than one"),
    (lambda r: r.update(energies=[e for e in r["energies"] if not e["participant"].startswith("product")]),
     "at least one product"),
    (lambda r: r["energies"][0].update(energy_hartree=0.5), "less than or equal to 0"),
    (lambda r: r.update(imaginary_frequency_count=1), "only on kind='imaginary_mode'"),
    (lambda r: r["energies"][0].update(participant="the_ts"), "should match pattern"),
])
def test_the_published_model_refuses_what_an_energy_ordering_checker_flags(mutate, match):
    record = _record_of(_bundle(), "energy_ordering")
    mutate(record)
    with pytest.raises(ValueError, match=match):
        TransitionStateValidationEvidenceIn.model_validate(record)


KINDS = ([MoleculeKind.molecule] * 2, [MoleculeKind.molecule] * 2)


def _validate_set(record):
    validate_ts_evidence_set([TransitionStateValidationEvidenceIn.model_validate(record)],
                             subject_label="TS0", xyz_text="3\n\nH 0 0 0\nH 0 0 1\nH 0 1 0\n",
                             reactant_kinds=KINDS[0], product_kinds=KINDS[1])


def test_the_published_set_rules_refuse_a_pass_the_numbers_contradict():
    record = _record_of(_bundle(), "energy_ordering")
    _validate_set(record)                                           # as sent: accepted
    low = copy.deepcopy(record)
    low["energies"][0]["energy_hartree"] = -116.21                 # below the wells
    with pytest.raises(ValueError, match="at or below"):
        _validate_set(low)
    low["passed"] = False                                          # "the reverse is not refused"
    _validate_set(low)
    short = copy.deepcopy(record)
    short["energies"] = [e for e in short["energies"] if e["participant"] != "reactant:2"]
    with pytest.raises(ValueError, match="omit reactant:2"):
        _validate_set(short)
    extra = copy.deepcopy(record)
    extra["energies"].append({**extra["energies"][1], "participant": "reactant:3"})
    with pytest.raises(ValueError, match="does not declare"):
        _validate_set(extra)


def test_the_checker_flags_each_energy_ordering_rule():
    payload = _bundle()
    for mutate, needle in [
        (lambda t: t["validation_evidence"][2].update(source_calculation_key="ts_sp"), "carries"),
        (lambda t: t["validation_evidence"][2]["energies"][1].update(source_calculation_key="p0_sp"),
         "does not belong"),
        (lambda t: t["validation_evidence"][2]["energies"][0].update(source_calculation_key="ts_irc"),
         "cannot come from a irc"),
        (lambda t: t["validation_evidence"][2]["energies"][0].update(energy_hartree=-130.0), "at or below"),
        (lambda t: t["validation_evidence"][2]["energies"][0].update(energy_hartree=1.0), "not positive"),
        (lambda t: t["validation_evidence"][2]["energies"].pop(1), "omits"),
        (lambda t: t["validation_evidence"][2]["energies"].append(dict(t["validation_evidence"][2]["energies"][0])),
         "more than one energy"),
        (lambda t: t["validation_evidence"][2]["energies"][0].update(energy_kind="e0"), "cannot come from a sp"),
    ]:
        broken = copy.deepcopy(payload)
        mutate(broken["transition_state"])
        assert any(needle in e for e in offline_evidence_errors(broken, standalone=False)), needle
    standalone, _ = _ts_payload(_fixture_with_a_passing_ordering())
    broken = copy.deepcopy(standalone)
    broken["validation_evidence"].append(copy.deepcopy(payload["transition_state"]["validation_evidence"][2]))
    assert any("refused on the standalone" in e for e in offline_evidence_errors(broken, standalone=True))


@pytest.mark.parametrize("mutate,match", [
    (lambda r: (r.update(imaginary_frequency_count=0), r.pop("imaginary_frequency_cm1")), "found no imaginary mode"),
    (lambda r: r.update(passed=False, imaginary_frequency_count=0), "count is 0"),
    (lambda r: r.update(imaginary_frequency_cm1=1235.4), "less than 0"),
    (lambda r: r.update(imaginary_frequency_cm1=float("-inf")), "finite"),
    (lambda r: r.update(energies=[{"participant": "ts", "energy_kind": "electronic", "energy_hartree": -1.0,
                                   "source_calculation_key": "ts_sp"}]), "only on kind='energy_ordering'"),
])
def test_the_published_model_refuses_what_an_imaginary_mode_checker_flags(mutate, match):
    record = _record_of(_bundle(), "imaginary_mode")
    mutate(record)
    with pytest.raises(ValueError, match=match):
        TransitionStateValidationEvidenceIn.model_validate(record)


def test_the_checker_flags_each_imaginary_mode_rule():
    for route, build in ROUTES:
        payload, _ = build(_fixture_with_a_passing_ordering())
        standalone = route == "transition_state"
        for mutate, needle in [
            (lambda r: r.update(imaginary_frequency_count=2), "disagrees with the frequency result's 1"),
            (lambda r: r.update(imaginary_frequency_cm1=-1230.0), "disagrees with the frequency result's"),
            (lambda r: r.update(imaginary_frequency_cm1=1235.4), "negative"),
            (lambda r: r.update(imaginary_frequency_count=0), "a pass with no imaginary mode"),
            (lambda r: r.update(energies=[]), "carries"),
        ]:
            broken = copy.deepcopy(payload)
            mutate(next(r for r in _block(broken, route)["validation_evidence"] if r["kind"] == "imaginary_mode"))
            assert any(needle in e for e in offline_evidence_errors(broken, standalone=standalone)), (route, needle)
        if standalone:
            broken = copy.deepcopy(payload)
            next(r for r in broken["validation_evidence"] if r["kind"] == "imaginary_mode")[
                "source_calculation_key"] = "ts_freq"
            assert any("must be omitted" in e for e in offline_evidence_errors(broken, standalone=True))


def test_a_pass_with_several_imaginary_modes_needs_the_result_to_designate_the_coordinate():
    payload = _thermo_fixture_ts("computed_reaction")
    record = _kind(payload, "computed_reaction", "imaginary_mode")
    assert record["imaginary_frequency_count"] == 2 and record["passed"] is True
    assert offline_evidence_errors(payload, standalone=False) == []
    broken = copy.deepcopy(payload)
    freq = _ts_freq(broken["transition_state"], standalone=False)
    freq.pop("freq_reaction_coordinate_mode_index")
    assert any("designate" in e for e in offline_evidence_errors(broken, standalone=False))
