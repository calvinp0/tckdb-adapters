"""``AdapterWarning`` and ``WarningSink`` produce the sidecar's warning dict shape."""

import json

from tckdb_core.adapter_warnings import AdapterWarning, WarningSink


def test_to_dict_is_the_sidecar_shape_with_the_producer_tag():
    warning = AdapterWarning(
        code="x_not_stated",
        message="X was not stated.",
        field="thermo.x",
        context={"action": "block_omitted", "label": "CH4"},
    )
    assert warning.to_dict("tckdb_arc") == {
        "code": "x_not_stated",
        "message": "X was not stated.",
        "field": "thermo.x",
        "context": {"source": "tckdb_arc_self_check", "action": "block_omitted", "label": "CH4"},
    }


def test_source_comes_first_in_the_serialised_context():
    rendered = AdapterWarning("c", "m", "f", {"action": "a"}).to_dict("p")
    assert list(rendered) == ["code", "message", "field", "context"]
    assert list(rendered["context"]) == ["source", "action"]
    assert json.loads(json.dumps(rendered)) == rendered


def test_the_producer_is_a_parameter_not_a_default():
    assert AdapterWarning("c", "m").to_dict("rmg")["context"]["source"] == "rmg_self_check"


def test_sink_add_and_append_store_the_same_dict():
    sink = WarningSink("tckdb_arc")
    added = sink.add("c", "m", "f", action="a", n=1)
    sink.append(AdapterWarning("c", "m", "f", {"action": "a", "n": 1}))
    assert sink[0] == sink[1] == added
    assert isinstance(sink, list) and all(isinstance(item, dict) for item in sink)


def test_sink_passes_plain_dicts_through_unchanged():
    legacy = {"code": "c", "message": "m", "field": None, "context": {"source": "other"}}
    sink = WarningSink("p", [legacy])
    assert sink == [legacy] and sink[0] is legacy
