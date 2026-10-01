"""What the adapter sends as transition-state validation evidence obeys tckdb-schemas 0.64's rules.

0.64 added two evidence kinds (``energy_ordering``, ``imaginary_mode``) and tightened the record:
"A pass that the record's own *stated* numbers contradict is refused, a field is refused on a kind it
does not describe, and only a passing ``irc`` record silences ``transition_state_missing_irc_evidence``.
``energy_ordering`` is accepted on the computed-reaction ... bundles and refused on the standalone
transition-state upload ...; ``imaginary_mode`` binds there to the single ``freq`` additional
calculation ... ``imaginary_frequency_cm1`` is finite ... a pass with more than one imaginary mode
needs that result to designate the reaction coordinate."

The adapter sends only ``kind="irc"`` evidence (ARC's ``ts_checks['IRC']``), so the rules that can bite
it are: fields belong to the kind, the participant mappings (both sides or neither, non-empty,
1-based, no repeated atom), the source binding on each route, one record per kind, and the TS
frequency result designating its reaction coordinate. They are replicated here (the published model
and JSON Schema also run on every built request via the conftest hook; the route handlers do not).
ARC's other TS checks (``E0``/``e_elect`` energy ordering, ``NMD``/``freq`` imaginary mode) are not
sent: see BRIDGE_ROADMAP C10.
"""

import copy
import re

import pytest

from test_arc_1_3_reactions import (
    ROUTES, _evidence, _fixture, _reaction_payload, _ts_payload)
from tckdb_schemas.fragments.ts_validation_evidence import TransitionStateValidationEvidenceIn

IRC_FIELDS = {"kind", "passed", "rationale", "source_calculation_key",
              "reactant_participant_mapping", "product_participant_mapping"}
OTHER_KIND_FIELDS = {"energies", "imaginary_frequency_count", "imaginary_frequency_cm1",
                     "mode_displacement_agrees"}


def offline_evidence_errors(payload, *, standalone):
    """The 0.64 rules a built request's ``validation_evidence`` can break, checked offline."""
    block = payload if standalone else payload["transition_state"]
    records = block.get("validation_evidence", [])
    errors = []
    kinds = [r["kind"] for r in records]
    if len(set(kinds)) != len(kinds):
        errors.append("more than one record of a kind")
    calcs = [block["primary_opt"], *block.get("additional_calculations", [])] if standalone else [
        block["calculation"], *block.get("calculations", [])]
    for record in records:
        if record["kind"] == "irc":
            extra = OTHER_KIND_FIELDS & set(record)
            if extra:
                errors.append(f"irc record carries {sorted(extra)}")
            unknown = set(record) - IRC_FIELDS
            if unknown:
                errors.append(f"irc record carries {sorted(unknown)}")
        elif standalone and record["kind"] == "energy_ordering":
            errors.append("energy_ordering is refused on the standalone route")
        if record.get("passed") is not True and (
                "reactant_participant_mapping" in record or "product_participant_mapping" in record):
            errors.append("a mapping is only meaningful on a passing record")
        sides = [k for k in ("reactant_participant_mapping", "product_participant_mapping") if record.get(k) is not None]
        if len(sides) == 1:
            errors.append("a one-sided participant mapping")
        for key in sides:
            mapping = record[key]
            if not mapping:
                errors.append(f"{key} is empty")
            for participant, atoms in mapping.items():
                if not re.fullmatch(r"(reactant|product):[1-9][0-9]*", participant):
                    errors.append(f"{key}: bad participant key {participant!r}")
                if not atoms or any(not isinstance(a, int) or a < 1 for a in atoms):
                    errors.append(f"{key}[{participant}]: atom indices must be 1-based integers")
                if len(set(atoms)) != len(atoms):
                    errors.append(f"{key}[{participant}] repeats an atom index")
        # the source binding
        if record["kind"] in ("irc", "imaginary_mode"):
            wanted = {"irc": "irc", "imaginary_mode": "freq"}[record["kind"]]
            of_type = [c for c in calcs if c["type"] == wanted]
            if standalone:
                if record.get("source_calculation_key") is not None:
                    errors.append("the standalone route has no key namespace: source_calculation_key must be omitted")
                if len(of_type) != 1:
                    errors.append(f"binds to the single {wanted} calculation, found {len(of_type)}")
            else:
                owned = {c["key"]: c["type"] for c in calcs}
                if owned.get(record.get("source_calculation_key")) != wanted:
                    errors.append(f"source_calculation_key must name a {wanted} calculation of this transition state")
    return errors


@pytest.mark.parametrize("route,build", ROUTES)
def test_the_irc_evidence_obeys_the_0_64_rules(route, build):
    payload, _ = build(_fixture())
    assert offline_evidence_errors(payload, standalone=route == "transition_state") == []
    (record,) = (payload if route == "transition_state" else payload["transition_state"])["validation_evidence"]
    assert record["kind"] == "irc" and record["passed"] is True
    assert set(record) <= IRC_FIELDS
    # the published model accepts exactly what is sent
    TransitionStateValidationEvidenceIn.model_validate(record)


@pytest.mark.parametrize("route,build", ROUTES)
def test_only_irc_evidence_is_ever_sent(route, build):
    """ARC's E0/e_elect and NMD verdicts are not turned into energy_ordering / imaginary_mode records."""
    doc = _fixture()
    assert doc["transition_states"][0]["ts_checks"]["E0"] is True and doc["transition_states"][0]["ts_checks"]["NMD"] is True
    payload, _ = build(doc)
    block = payload if route == "transition_state" else payload["transition_state"]
    assert [r["kind"] for r in block["validation_evidence"]] == ["irc"]


@pytest.mark.parametrize("route,build", ROUTES)
def test_a_failed_irc_is_sent_as_a_failure_with_no_mapping(route, build):
    """Only a passing irc record silences ``transition_state_missing_irc_evidence``; a failed one must not claim a map."""
    doc = _fixture()
    doc["transition_states"][0]["ts_checks"]["IRC"] = False
    payload, _ = build(doc)
    block = payload if route == "transition_state" else payload["transition_state"]
    (record,) = block["validation_evidence"]
    assert record["passed"] is False
    assert "reactant_participant_mapping" not in record and "product_participant_mapping" not in record
    assert offline_evidence_errors(payload, standalone=route == "transition_state") == []
    TransitionStateValidationEvidenceIn.model_validate(record)


@pytest.mark.parametrize("route,build", ROUTES)
def test_no_verdict_sends_no_evidence_at_all(route, build):
    doc = _fixture()
    doc["transition_states"][0]["ts_checks"]["IRC"] = None
    payload, _ = build(doc)
    block = payload if route == "transition_state" else payload["transition_state"]
    assert "validation_evidence" not in block


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
