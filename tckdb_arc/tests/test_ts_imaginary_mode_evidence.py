"""``imaginary_mode`` validation evidence from ARC's ``ts_checks['freq']`` and the TS frequency result (0.64).

The record states what the TS frequency calculation found, read from the ``freq_result`` the same upload sends
for it, because TCKDB refuses a record whose count disagrees with that result, or whose frequency is more
than 1 cm^-1 from it, and refuses a pass with several imaginary modes unless that result designates the
reaction coordinate. So:

* ``passed`` is ``ts_checks['freq']`` when it is a bool, otherwise nothing is sent (``passed`` is required);
* ``imaginary_frequency_count`` is the result's ``n_imag``; ``imaginary_frequency_cm1`` the designated mode's
  frequency (ARC 1.3 ``reaction_coordinate_mode_index`` into ``freq_frequencies_cm1_ess_order``), or with one
  imaginary mode ``imag_freq_cm1``, written negative;
* ``mode_displacement_agrees`` is ``True`` only when ARC states ``reaction_coordinate_mode_index`` (set only by a
  genuine, non-forced normal mode displacement pass), ``False`` when ``ts_checks['NMD']`` is ``False`` with no
  index stated or ``nmd_forced`` is true (a forced pass follows a failed check), and omitted otherwise (not assessed is not ``False``);
* the bundle binds it to the TS ``freq`` calculation by key; the standalone route omits the key and binds to
  its single ``freq`` calculation.
"""

import copy
import pathlib
import tempfile

import pytest

from _contract import assert_fragment_matches_contract, contract_validate
from test_arc_1_3_reactions import ROUTES, _fixture, _reaction_payload, _ts_payload
from test_arc_schema_1_3_thermo import _doc as _thermo_doc
from test_arc_schema_1_3_thermo import build_reaction, build_ts
from tckdb_schemas.fragments.ts_validation_evidence import TransitionStateValidationEvidenceIn
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

from tckdb_arc.adapter import _ts_imaginary_mode_validation_evidence

CODE = "ts_imaginary_mode_evidence_not_sent"
MODELS = {"computed_reaction": ComputedReactionUploadRequest, "transition_state": TransitionStateUploadRequest}


def _block(payload, route):
    return payload if route == "transition_state" else payload["transition_state"]


def _mode(payload, route):
    return [r for r in _block(payload, route).get("validation_evidence", []) if r["kind"] == "imaginary_mode"]


def _freq_calc(payload, route):
    block = _block(payload, route)
    calcs = [block["primary_opt"], *block.get("additional_calculations", [])] if route == "transition_state" else [
        block["calculation"], *block.get("calculations", [])]
    (freq,) = [c for c in calcs if c["type"] == "freq"]
    return freq


def _checks(**changes):
    return {"E0": None, "e_elect": None, "IRC": None, "freq": True, "NMD": None, "warnings": "", **changes}


def _reaction_fixture(**checks):
    doc = _fixture()
    doc["transition_states"][0]["ts_checks"] = _checks(**checks)
    return doc


def _thermo(route, mutate=lambda doc: None):
    """The 1.3 thermo fixture's TS: two imaginary modes [-1235.4, -18.2], ARC's index 1, NMD True."""
    doc = _thermo_doc()
    mutate(doc)
    tmp = pathlib.Path(tempfile.mkdtemp())
    return build_ts(tmp, doc) if route == "transition_state" else build_reaction(tmp, doc)


@pytest.mark.parametrize("route,build", ROUTES)
def test_a_passing_freq_verdict_is_sent_with_what_the_frequency_result_states(route, build):
    payload, warnings = build(_reaction_fixture())
    [record] = _mode(payload, route)
    assert record["kind"] == "imaginary_mode" and record["passed"] is True
    assert record["imaginary_frequency_count"] == 1 and record["imaginary_frequency_cm1"] == -1235.4
    assert "mode_displacement_agrees" not in record          # ARC states no reaction_coordinate_mode_index
    assert record["rationale"] == "ARC ts_checks['freq'] = True"
    if route == "transition_state":
        assert "source_calculation_key" not in record        # binds to the single freq calculation
    else:
        assert record["source_calculation_key"] == "ts_freq" == _freq_calc(payload, route)["key"]
    # it agrees with the result it cites
    freq = _freq_calc(payload, route)
    result = freq.get("freq_result") or {"n_imag": freq["freq_n_imag"], "imag_freq_cm1": freq["freq_imag_freq_cm1"]}
    assert record["imaginary_frequency_count"] == result["n_imag"]
    assert abs(abs(record["imaginary_frequency_cm1"]) - abs(result["imag_freq_cm1"])) < 1.0
    TransitionStateValidationEvidenceIn.model_validate(record)
    assert_fragment_matches_contract(record, "TransitionStateValidationEvidenceIn")
    contract_validate(MODELS[route], payload)
    assert CODE not in [w["code"] for w in warnings]


@pytest.mark.parametrize("route,build", ROUTES)
@pytest.mark.parametrize("no_ts_checks", [False, True], ids=["verdict_none", "no_ts_checks"])
def test_without_a_verdict_nothing_is_sent_and_nothing_is_reported(route, build, no_ts_checks):
    doc = _reaction_fixture(freq=None)
    if no_ts_checks:
        del doc["transition_states"][0]["ts_checks"]
    payload, warnings = build(doc)
    assert _mode(payload, route) == []
    assert CODE not in [w["code"] for w in warnings]


@pytest.mark.parametrize("route,build", ROUTES)
def test_a_failed_verdict_is_sent_as_a_failure(route, build):
    payload, _ = build(_reaction_fixture(freq=False))
    [record] = _mode(payload, route)
    assert record["passed"] is False and record["imaginary_frequency_count"] == 1
    contract_validate(MODELS[route], payload)


@pytest.mark.parametrize("route,build", ROUTES)
def test_a_ts_without_a_frequency_calculation_is_reported_not_sent(route, build):
    doc = _reaction_fixture()
    ts = doc["transition_states"][0]
    for key in ("freq_n_imag", "imag_freq_cm1", "zpe_hartree", "freq_frequencies_cm1_ess_order",
                "reaction_coordinate_mode_index"):
        ts.pop(key, None)
    ts["statmech"] = None
    payload, warnings = build(doc)
    assert _mode(payload, route) == []
    [warning] = [w for w in warnings if w["code"] == CODE]
    assert "no frequency calculation" in warning["message"]
    assert warning["context"] == {"source": "tckdb_arc_self_check", "action": "validation_evidence_omitted",
                                  "ts_label": "TS0", "ts_checks_freq": "true"}
    assert warning["field"] == "transition_state.validation_evidence"


@pytest.mark.parametrize("route", ["computed_reaction", "transition_state"])
def test_several_imaginary_modes_with_ARCs_designated_coordinate(route):
    """n_imag 2, ARC's index 1 from a genuine NMD pass: the coordinate's value, and the displacement verdict."""
    payload, _ = _thermo(route)
    [record] = _mode(payload, route)
    assert record["passed"] is True and record["imaginary_frequency_count"] == 2
    assert record["imaginary_frequency_cm1"] == -1235.4          # not the -18.2 extra mode
    assert record["mode_displacement_agrees"] is True
    assert "reaction_coordinate_mode_index = 1" in record["rationale"]
    freq = _freq_calc(payload, route)
    designated = (freq["freq_result"] if "freq_result" in freq else
                  {"reaction_coordinate_mode_index": freq["freq_reaction_coordinate_mode_index"]})
    assert designated["reaction_coordinate_mode_index"] == 1
    contract_validate(MODELS[route], payload)


@pytest.mark.parametrize("route", ["computed_reaction", "transition_state"])
def test_several_imaginary_modes_without_ARCs_index_use_the_results_designation_and_claim_no_displacement_verdict(route):
    """No stated index (a forced or skipped NMD): TCKDB's tau designates the one mode at or above it."""
    def mutate(doc):
        ts = doc["transition_states"][0]
        ts["reaction_coordinate_mode_index"] = None
        ts["ts_checks"]["NMD"] = True                           # forced / unverified: not a verdict
    payload, _ = _thermo(route, mutate)
    [record] = _mode(payload, route)
    assert record["passed"] is True and record["imaginary_frequency_count"] == 2
    assert record["imaginary_frequency_cm1"] == -1235.4
    assert "mode_displacement_agrees" not in record
    assert "mode_displacement_agrees" not in record["rationale"]
    contract_validate(MODELS[route], payload)


@pytest.mark.parametrize("route", ["computed_reaction", "transition_state"])
def test_a_failed_nmd_check_is_the_only_source_of_a_false_displacement_verdict(route):
    def mutate(doc):
        ts = doc["transition_states"][0]
        ts["reaction_coordinate_mode_index"] = None
        ts["ts_checks"]["NMD"] = False
    payload, _ = _thermo(route, mutate)
    [record] = _mode(payload, route)
    assert record["mode_displacement_agrees"] is False and "ts_checks['NMD'] = False" in record["rationale"]
    # ARC states an index and NMD False together: contradictory, so no verdict at all
    def both(doc):
        doc["transition_states"][0]["ts_checks"]["NMD"] = False
    payload, _ = _thermo(route, both)
    assert "mode_displacement_agrees" not in _mode(payload, route)[0]


@pytest.mark.parametrize("route,build", ROUTES)
def test_an_nmd_pass_without_a_stated_index_is_not_a_verdict(route, build):
    doc = _reaction_fixture(NMD=True)
    assert doc["transition_states"][0]["reaction_coordinate_mode_index"] is None
    payload, _ = build(doc)
    assert "mode_displacement_agrees" not in _mode(payload, route)[0]


# ---------------------------------------------------------------------------
# the cited frequency result decides what can be passed
# ---------------------------------------------------------------------------

def _call(ts_checks, freq_result, *, key="ts_freq", ts_extra=None):
    warnings = []
    sent = _ts_imaginary_mode_validation_evidence(
        {"ts_checks": ts_checks, **(ts_extra or {})}, freq_calc_key=key, freq_result=freq_result,
        ts_label="TS0", warnings=warnings)
    return sent, warnings


MODES = [{"mode_index": 1, "frequency_cm1": -1000.0, "is_imaginary": True},
         {"mode_index": 2, "frequency_cm1": -300.0, "is_imaginary": True},
         {"mode_index": 3, "frequency_cm1": 500.0, "is_imaginary": False}]


def test_a_pass_with_several_imaginary_modes_and_no_designation_is_not_sent():
    result = {"n_imag": 2, "imag_freq_cm1": -1000.0, "modes": MODES}
    sent, warnings = _call({"freq": True}, result)
    assert sent == []
    [warning] = warnings
    assert warning["code"] == CODE and "designates no reaction coordinate" in warning["message"]
    # a failure needs no designation (TCKDB's rule is on a pass), and states no frequency it cannot place
    [record], warnings = _call({"freq": False}, result)
    assert record["passed"] is False and record["imaginary_frequency_count"] == 2
    assert "imaginary_frequency_cm1" not in record and warnings == []
    # designated: the designated mode's value, whichever mode that is
    [record], _ = _call({"freq": True}, {**result, "reaction_coordinate_mode_index": 2})
    assert record["imaginary_frequency_cm1"] == -300.0


def test_a_pass_whose_result_found_no_imaginary_mode_is_not_sent():
    sent, warnings = _call({"freq": True}, {"n_imag": 0})
    assert sent == [] and "reports no imaginary mode" in warnings[0]["message"]


def test_the_frequency_is_written_negative_and_a_zero_one_is_not_sent():
    [record], _ = _call({"freq": True}, {"n_imag": 1, "imag_freq_cm1": 1500.0})     # a positive scalar
    assert record["imaginary_frequency_cm1"] == -1500.0
    [record], _ = _call({"freq": True}, {"n_imag": 1, "imag_freq_cm1": 0.0})
    assert "imaginary_frequency_cm1" not in record
    [record], _ = _call({"freq": True}, {"zpe_hartree": 0.1})                       # n_imag not stated
    assert record == {"kind": "imaginary_mode", "passed": True, "source_calculation_key": "ts_freq",
                      "rationale": "ARC ts_checks['freq'] = True"}


def test_no_freq_calculation_key_or_no_result_is_reported():
    for key, result in (("ts_freq", None), (None, {"n_imag": 1})):
        sent, warnings = _call({"freq": True}, result, key=key)
        assert sent == [] and [w["code"] for w in warnings] == [CODE]
