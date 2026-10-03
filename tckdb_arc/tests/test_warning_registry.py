"""The warning-code registry, the generated ``WARNING_CODES.md`` and the shape of what is emitted."""

import ast
import json
import re
import warnings
from pathlib import Path
from unittest import mock

import pytest

from tckdb_arc import adapter as adapter_module
from tckdb_arc import warning_codes
from tckdb_arc.warning_codes import ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE, ArcWarning
from tckdb_core.adapter_warnings import WarningSink
from tckdb_core.warning_codes import CoreWarning

REPO = Path(__file__).resolve().parents[2]
ADAPTER_SOURCE = Path(adapter_module.__file__)


def test_the_registry_is_well_formed():
    codes = [member.value for member in ArcWarning]
    assert len(codes) == len(set(codes)), "a code is registered twice"
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", code) for code in codes)
    assert not set(codes) & {member.value for member in CoreWarning}, "a code is in both registries"
    for member in ArcWarning:
        assert member.description.strip() and "\n" not in member.description, member.name
        assert type(member.value) is str


def test_every_w_constant_in_the_adapter_is_a_registered_code():
    registered = {member.value for member in ArcWarning} | {member.value for member in CoreWarning}
    constants = {name: value for name, value in vars(adapter_module).items()
                 if name.startswith("_W_") and isinstance(value, str)}
    assert len(constants) >= 45
    assert set(constants.values()) <= registered
    assert dict(adapter_module._W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE) == dict(ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE)
    assert set(ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.values()) <= registered


def test_every_registered_code_is_emitted_somewhere_in_the_adapter():
    """No dead entries: each code's registry member (or its ``_W_`` alias) is used by the adapter source."""
    source = ADAPTER_SOURCE.read_text()
    aliases = {value: name for name, value in vars(adapter_module).items()
               if name.startswith("_W_") and isinstance(value, str)}
    for member in ArcWarning:
        alias = aliases.get(member.value)
        used = (alias is not None and len(re.findall(rf"\b{alias}\b", source)) > 1) or \
            f"ArcWarning.{member.name}" in source.replace(f"= ArcWarning.{member.name}.value", "") or \
            member.value in ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.values()
        assert used, f"{member.name} is registered but never emitted"


def test_no_adapter_site_builds_a_self_check_dict_by_hand():
    """Every warning dict goes through ``_self_check`` (``AdapterWarning``); none is a hand-built literal."""
    tree = ast.parse(ADAPTER_SOURCE.read_text())
    literal_keys = {"code", "message", "field", "context"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            assert not literal_keys <= keys, f"hand-built warning dict at line {node.lineno}"
    assert '"source": "tckdb_arc_self_check"' not in ADAPTER_SOURCE.read_text()


def test_every_self_check_call_names_a_registered_code():
    tree = ast.parse(ADAPTER_SOURCE.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_self_check"]
    assert len(calls) >= 40
    for call in calls:
        code = call.args[0]
        ok = (isinstance(code, ast.Name) and (code.id.startswith("_W_") or code.id == "code")) or \
            (isinstance(code, ast.Attribute) and isinstance(code.value, ast.Attribute)
             and isinstance(code.value.value, ast.Name) and code.value.value.id == "ArcWarning")
        assert ok, f"line {call.lineno}: code is not a registry constant"


def test_the_generated_document_is_current():
    path = REPO / warning_codes.DOC_RELATIVE_PATH
    assert path.read_text() == warning_codes.render_markdown(), (
        "docs/contract/WARNING_CODES.md is stale; run `python tools/gen_warning_codes.py`")
    for member in ArcWarning:
        assert f"| `{member.value}` |" in path.read_text()
    assert "| `calculation_ref_not_returned` |" in path.read_text()


def test_self_check_is_the_shape_the_sidecar_has_always_held():
    rendered = adapter_module._self_check(
        "some_code", "A message.", "thermo.x", {"action": "block_omitted", "label": "CH4"})
    assert rendered == {
        "code": "some_code", "message": "A message.", "field": "thermo.x",
        "context": {"source": "tckdb_arc_self_check", "action": "block_omitted", "label": "CH4"},
    }
    assert list(rendered) == ["code", "message", "field", "context"]
    assert list(rendered["context"]) == ["source", "action", "label"]
    assert json.dumps(rendered) == json.dumps({
        "code": "some_code", "message": "A message.", "field": "thermo.x",
        "context": {"source": "tckdb_arc_self_check", "action": "block_omitted", "label": "CH4"}})
    assert adapter_module._self_check("c", "m")["context"] == {"source": "tckdb_arc_self_check"}
    assert adapter_module._self_check("c", "m")["field"] is None
    sink = WarningSink("tckdb_arc")
    sink.append(rendered)
    assert sink == [rendered]


def _previous_literal(code, message, field, **context):
    """What the call sites built before the registry: a hand-written dict literal."""
    return {"code": code, "message": message, "field": field,
            "context": {"source": "tckdb_arc_self_check", **context}}


def test_converted_sites_emit_the_previous_literal_dicts():
    warnings_out = []
    adapter_module._warn_isotopes_not_stated(warnings_out, label="CD4", what="Alternate conformers")
    assert warnings_out == [_previous_literal(
        "geometry_isotopes_not_stated",
        "Alternate conformers of the isotopically substituted 'CD4' were left out: ARC states no "
        "isotope list that matches the species' own geometry for them, and TCKDB would "
        "read an unlabelled geometry as the unsubstituted species.",
        "geometry.isotopes", action="geometry_omitted")]
    assert type(warnings_out[0]["code"]) is str

    ts_out = []
    adapter_module._warn_ts_evidence_not_sent(
        ts_out, code=adapter_module._W_TS_IMAGINARY_MODE_NOT_SENT, ts_label="TS0", message="msg",
        context={"ts_checks_freq": "true"})
    assert ts_out == [_previous_literal(
        "ts_imaginary_mode_evidence_not_sent", "msg", "transition_state.validation_evidence",
        action="validation_evidence_omitted", ts_label="TS0", ts_checks_freq="true")]

    skipped = adapter_module._irc_endpoint_skip(
        {}, {"label": "IRC_TS0_1", "irc_endpoint_of": "TS0", "irc_endpoint_direction": "forward"})
    assert skipped.warnings == [_previous_literal(
        "irc_endpoint_species_skipped",
        "'IRC_TS0_1' is an IRC endpoint of 'TS0' (output.yml irc_endpoint_of, forward IRC job), "
        "not a stationary species of the run, so it is not uploaded.",
        "species", action="species_skipped", ts_label="TS0")]


def test_the_ts_reaction_coordinate_refusal_carries_the_previous_warning_dict():
    exc = adapter_module.TSReactionCoordinateNotDesignated(
        label="TS0", n_imag=2, imaginary_cm1=[-800.0, -700.0], stated_index=None, n_above_tau=2)
    assert exc.warning == _previous_literal(
        "ts_reaction_coordinate_not_designated", str(exc),
        "transition_state.freq_result.reaction_coordinate_mode_index",
        action="record_refused", label="TS0", imaginary_cm1=[-800.0, -700.0],
        tau_cm1=50.0, n_above_tau=2)
    assert str(exc).startswith("[ts_reaction_coordinate_not_designated] label='TS0'")


def test_moved_names_log_through_the_adapter_module_logger():
    """``mock.patch("tckdb_arc.adapter.logger")`` intercepts the records of functions that moved to the core."""
    with mock.patch("tckdb_arc.adapter.logger") as log:
        assert adapter_module._build_nasa_block(None, None) is None  # not mappings: silent
        assert adapter_module._build_nasa_block(
            {"coeffs": [1.0], "tmin_k": 1, "tmax_k": 2}, {"coeffs": [1.0] * 7}) is None
        assert adapter_module._build_thermo_points([{"temperature_k": -1}]) == []
        assert adapter_module.arc_to_tckdb_a_units("furlongs") is None
        assert adapter_module.arc_to_tckdb_ea_units("furlongs") is None
        adapter_module._serialize_calc_constraints([{"constraint_kind": "bond", "atoms": [1, 2]}])
    assert log.warning.call_count == 3  # short coeffs, dropped point, constraint without index_base
    assert log.debug.call_count == 2
