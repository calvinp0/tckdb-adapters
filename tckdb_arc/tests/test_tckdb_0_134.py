"""The adapter's side of tckdb-schemas 0.74 to 0.134 (batch M).

* 0.130: a Hessian's ``parser_version`` text became ``parser: {name, version}`` (``ParserStamp``).
* 0.121 (#635): a calculation's geometry and conformer may not disagree; a ``freq``/``sp`` that
  declares ``input_geometries`` declares its owner's geometry.
* 0.99: a kinetics block can declare the level its energies came from.
* 0.116: an artifact target is the ``calc_`` ref only (tests in ``test_artifact_batch_response``).
"""

import copy
import json
import os
import shutil
import tempfile
from unittest import mock

import pytest
from _contract import contract_validate
from tckdb_schemas.fragments.calculation import HessianPayload
from tckdb_schemas.workflows.computed_reaction_upload import BundleKineticsIn

from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import (
    TCKDBAdapter,
    _enforce_geometry_anchoring,
    _hessian_parser_block,
)
from tckdb_arc.config import TCKDBConfig

from test_adapter import _StubClient, _StubResponse, _reaction_output_doc, _reaction_record

H2 = "2\n\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74"


# --- 0.130: the Hessian parser stamp ------------------------------------------------------

@pytest.mark.parametrize("stamp,expected", [
    ("arc-hessian-1", {"parser": {"name": "arc-hessian", "version": "1"}}),
    ("arc-hessian-12", {"parser": {"name": "arc-hessian", "version": "12"}}),
])
def test_arcs_one_token_parser_stamp_splits_at_its_own_trailing_version(stamp, expected):
    assert _hessian_parser_block(stamp) == expected


@pytest.mark.parametrize("stamp", ["arc-hessian", "arc-hessian-v2", "-1", "", "1", None])
def test_a_stamp_without_arcs_name_dash_version_shape_is_left_out_not_guessed(stamp):
    # ``parser`` is optional: a name is never invented for a stamp that does not carry one.
    assert _hessian_parser_block(stamp) == {}


def test_the_hessian_block_is_a_valid_contract_hessian_and_has_no_parser_version():
    block = {
        "geometry": {"xyz_text": H2}, "lower_triangle_hartree_bohr2": [0.1] * 21,
        "source": "parsed_log", **_hessian_parser_block("arc-hessian-1"),
    }
    contract_validate(HessianPayload, block)
    assert "parser_version" not in block


def test_the_old_parser_version_field_is_refused_by_the_contract():
    block = {"geometry": {"xyz_text": H2}, "lower_triangle_hartree_bohr2": [0.1] * 21,
             "source": "parsed_log", "parser_version": "arc-hessian-1"}
    with pytest.raises(Exception, match="parser_version"):
        contract_validate(HessianPayload, block)


# --- 0.121: geometry and conformer agree --------------------------------------------------

def _reaction_payload():
    """A computed-reaction payload built from the reaction fixture (every route detail is the adapter's)."""
    tmp = tempfile.mkdtemp(prefix="arc-tckdb-0134-")
    cfg = TCKDBConfig(enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=tmp,
                      api_key_env="X_TCKDB_API_KEY", project_label="proj-A", upload_mode="computed_reaction")
    adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: _StubClient(response=_StubResponse({})))
    doc = _reaction_output_doc()
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=doc["reactions"][0])
    payload = json.loads(outcome.payload_path.read_text())
    shutil.rmtree(tmp, ignore_errors=True)
    return payload.get("payload", payload), outcome


def test_the_adapters_own_reaction_payload_passes_both_geometry_rules_untouched():
    payload, outcome = _reaction_payload()
    before = copy.deepcopy(payload)
    warnings = []
    _enforce_geometry_anchoring(payload, "computed_reaction", warnings)
    assert payload == before and warnings == []
    # and it does declare the conformer's own geometry as the freq/sp input (so the rule has teeth)
    species = payload["species"][0]
    geometry = species["conformers"][0]["geometry"]
    sp = next(c for c in species["calculations"] if c["type"] == "sp")
    assert sp["geometry_key"] == geometry["key"]
    assert sp["input_geometries"][0]["xyz_text"] == geometry["xyz_text"]


def test_a_freq_or_sp_input_geometry_that_is_not_its_conformers_is_left_out_with_a_warning():
    payload, _ = _reaction_payload()
    species = payload["species"][0]
    sp = next(c for c in species["calculations"] if c["type"] == "sp")
    # Coordinates re-printed by a program (not the conformer's own xyz) are another structure to TCKDB.
    sp["input_geometries"] = [{"xyz_text": species["conformers"][0]["geometry"]["xyz_text"].replace("0", "0.00001", 1)}]
    warnings = []
    _enforce_geometry_anchoring(payload, "computed_reaction", warnings)
    assert "input_geometries" not in sp
    assert [w["code"] for w in warnings] == ["calculation_input_geometry_not_declared"]
    assert warnings[0]["context"]["calculation_key"] == sp["key"]
    # an untouched sibling keeps its declaration
    freq = next(c for c in species["calculations"] if c["type"] == "freq")
    assert freq["input_geometries"]


def test_an_opt_is_exempt_from_the_input_geometry_rule():
    payload, _ = _reaction_payload()
    primary = payload["species"][0]["conformers"][0]["calculation"]
    assert primary["type"] == "opt"
    primary["input_geometries"] = [{"xyz_text": "1\n\nH 9 9 9"}]
    warnings = []
    _enforce_geometry_anchoring(payload, "computed_reaction", warnings)
    assert primary["input_geometries"] and warnings == []


@pytest.mark.parametrize("mutate,code", [
    # a species calculation anchored to one conformer but on another conformer's geometry
    ("species_calc_other_geometry", "calculation_geometry_conformer_mismatch"),
    # a conformer's own calculation that names another conformer
    ("primary_other_conformer", "conformer_key_undeclared"),
    # a transition-state calculation with a conformer_key / on a species geometry
    ("ts_conformer_key", "transition_state_calculation_conformer_key"),
    ("ts_species_geometry", "transition_state_calculation_species_geometry"),
])
def test_a_payload_tckdb_would_refuse_for_its_keys_is_never_built(mutate, code):
    payload, _ = _reaction_payload()
    species = payload["species"][0]
    conf = species["conformers"][0]
    if mutate == "species_calc_other_geometry":
        second = copy.deepcopy(conf)
        second["key"] = "other_conf"
        second["geometry"] = {**second["geometry"], "key": "other_geom"}
        second["calculation"] = {**second["calculation"], "key": "other_opt"}
        species["conformers"].append(second)
        calc = species["calculations"][0]
        calc["conformer_key"], calc["geometry_key"] = conf["key"], "other_geom"
    elif mutate == "primary_other_conformer":
        conf["calculation"]["conformer_key"] = "nope"
    elif mutate == "ts_conformer_key":
        payload["transition_state"]["calculations"][0]["conformer_key"] = conf["key"]
    else:
        payload["transition_state"]["calculations"][0]["geometry_key"] = conf["geometry"]["key"]
    with pytest.raises(ValueError, match=code):
        _enforce_geometry_anchoring(payload, "computed_reaction", [])


def test_the_computed_species_and_ts_routes_pass_the_adapters_own_payloads(tmp_path):
    from test_golden_corpus import TestGoldenCorpus

    case = TestGoldenCorpus()
    case.tmp = str(tmp_path)
    output_doc = case._phase3_corpus()
    adapter = case._adapter("all")
    species = case._built_payload(adapter.submit_computed_species_from_output(
        output_doc=output_doc, species_record=output_doc["species"][0]))
    ts = case._built_payload(adapter.submit_computed_ts_from_output(
        output_doc=output_doc, ts_record=output_doc["transition_states"][0],
        reaction_record=output_doc["reactions"][0]))
    for route, payload in (("computed_species", species), ("transition_state", ts)):
        warnings = []
        _enforce_geometry_anchoring(copy.deepcopy(payload), route, warnings)
        assert warnings == [], route
    # the standalone TS declares its saddle point as freq/sp input; a drifted one is dropped
    drifted = copy.deepcopy(ts)
    drifted["additional_calculations"][0]["input_geometries"] = [{"xyz_text": "1\n\nH 1 2 3"}]
    warnings = []
    _enforce_geometry_anchoring(drifted, "transition_state", warnings)
    assert "input_geometries" not in drifted["additional_calculations"][0]
    assert [w["code"] for w in warnings] == ["calculation_input_geometry_not_declared"]


# --- 0.99: the kinetics energy level -----------------------------------------------------

def _kinetics_block(output_doc_edit=None):
    tmp = tempfile.mkdtemp(prefix="arc-tckdb-0134-")
    cfg = TCKDBConfig(enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=tmp,
                      api_key_env="X_TCKDB_API_KEY", project_label="proj-A", upload_mode="computed_reaction")
    adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: _StubClient(response=_StubResponse({})))
    doc = _reaction_output_doc()
    if output_doc_edit:
        output_doc_edit(doc)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = adapter.submit_computed_reaction_from_output(
            output_doc=doc, reaction_record=doc["reactions"][0])
    payload = json.loads(outcome.payload_path.read_text())
    shutil.rmtree(tmp, ignore_errors=True)
    payload = payload.get("payload", payload)
    return payload["kinetics"][0], payload


def test_kinetics_declares_the_level_every_linked_energy_ran_at():
    block, payload = _kinetics_block()
    declared = block["energy_level_of_theory"]
    contract_validate(BundleKineticsIn, block)
    by_key = {c["key"]: c for s in payload["species"] for c in [s["conformers"][0]["calculation"], *s["calculations"]]}
    by_key.update({c["key"]: c for c in payload["transition_state"]["calculations"]})
    energies = [by_key[link["calculation_key"]] for link in block["source_calculations"]
                if link["role"] in ("reactant_energy", "product_energy", "ts_energy")]
    assert energies and all(c["level_of_theory"] == declared for c in energies)


def test_kinetics_declaration_is_withheld_when_the_energy_level_is_unattributable():
    # The rule's refusals (different levels, spin treatments, an unstated participant) are
    # pinned in tckdb_core/tests/test_geometry_anchoring_rules.py; here ARC's adaptive-level
    # run, whose energy level it cannot attribute to a record, reaches the same outcome.
    with mock.patch.object(adapter_module, "_thermo_energy_level", return_value=None):
        block, _ = _kinetics_block()
    assert "energy_level_of_theory" not in block


# --- 0.94: a Hessian / path / scan point geometry must carry its subject's isotopes -----------

def test_a_substituted_species_hessian_is_labelled_with_the_records_isotopes(tmp_path):
    from test_arc_schema_1_3_levels import _adapter, _calcs, _project, _species_payload

    payload = _species_payload(_adapter(tmp_path), _project(tmp_path), "iC3H7_d")
    freq = _calcs(payload)["freq"]
    assert freq["hessian"]["geometry"]["isotopes"] == {"4": 2}
    assert freq["hessian"]["parser"] == {"name": "arc-hessian", "version": "1"}
    contract_validate(HessianPayload, freq["hessian"])
    # an ordinary species' Hessian geometry is the bare frame, as before
    plain = _calcs(_species_payload(_adapter(tmp_path / "p"), _project(tmp_path / "p"), "iC3H7"))["freq"]
    assert "isotopes" not in plain["hessian"]["geometry"]


def test_a_substituted_species_hessian_in_another_atom_order_is_left_out(tmp_path):
    from test_arc_schema_1_3_levels import _adapter, _calcs, _project, _record, _species_payload

    doc = _project(tmp_path)
    record = _record(doc, "iC3H7_d")
    frame = record["xyz"].strip().splitlines()
    record["xyz"] = "\n".join([*frame[:1], *reversed(frame[1:])]) if frame[0].strip().isdigit() else \
        "\n".join(reversed(frame))
    record["xyz_isotopes"] = list(reversed(record["xyz_isotopes"]))
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "iC3H7_d", warnings)
    assert "hessian" not in _calcs(payload)["freq"]
    [hessian] = [w for w in warnings if w["context"].get("action") == "hessian_omitted"]
    assert hessian["code"] == "geometry_isotopes_not_stated" and hessian["field"] == "hessian.geometry"


def _point_calc(software=None, result="irc_result"):
    calc = {"type": "irc" if result == "irc_result" else "scan", "key": "irc", result: {"points": [
        {"point_index": 0, "geometry": {"xyz_text": H2}, "electronic_energy_hartree": -1.0},
        {"point_index": 1, "geometry": {"xyz_text": H2, "isotopes": {"1": 2}}},
        {"point_index": 2, "electronic_energy_hartree": -1.1}]}}
    if software:
        calc["software_release"] = {"name": software}
    return calc


def test_point_geometries_of_a_substituted_subject_are_dropped_with_a_warning_when_they_cannot_be_labelled():
    calc = _point_calc()                       # no stated program: the atom order is not known to be kept
    payload = {"species_entry": {"smiles": "[2H]O"}, "conformers": [
        {"geometry": {"xyz_text": H2, "isotopes": {"1": 2}},
         "primary_calculation": {"type": "opt"}, "additional_calculations": [calc]}]}
    warnings = []
    adapter_module._withhold_unlabelled_point_geometries(payload, "computed_species", warnings)
    points = calc["irc_result"]["points"]
    assert "geometry" not in points[0] and points[0]["electronic_energy_hartree"] == -1.0
    assert points[1]["geometry"]["isotopes"] == {"1": 2}         # a labelled point is kept
    assert [w["code"] for w in warnings] == ["geometry_isotopes_not_stated"]
    assert warnings[0]["context"] == {"source": "tckdb_arc_self_check", "action": "geometry_omitted"}


@pytest.mark.parametrize("software,field", [("gaussian", "irc_result"), ("orca", "scan_result"),
                                            ("Gaussian", "irc_result")])
def test_scan_and_irc_points_of_an_order_preserving_program_are_labelled_like_the_hessian(software, field):
    calc = _point_calc(software, field)
    payload = {"species_entry": {"smiles": "[2H]O"}, "conformers": [
        {"geometry": {"xyz_text": H2, "isotopes": {"1": 2}},
         "primary_calculation": {"type": "opt"}, "additional_calculations": [calc]}]}
    warnings = []
    adapter_module._withhold_unlabelled_point_geometries(payload, "computed_species", warnings)
    points = calc[field]["points"]
    assert points[0]["geometry"] == {"xyz_text": H2, "isotopes": {"1": 2}} and warnings == []
    assert points[0]["geometry"]["isotopes"] is not payload["conformers"][0]["geometry"]["isotopes"]


@pytest.mark.parametrize("software,frame,field", [
    ("gaussian", "2\n\nH 0 0 0\nO 0 0 1", "irc_result"),       # another element order
    ("molpro", H2, "scan_result"),                                # a program not known to keep the order
    (None, H2, "irc_result"),                                     # program not stated
    ("gaussian", H2, "path_search_result"),                       # path-search points are never labelled
])
def test_a_point_that_cannot_be_labelled_is_dropped_with_the_warning(software, frame, field):
    calc = _point_calc(software, field)
    calc[field]["points"][0]["geometry"] = {"xyz_text": frame}
    payload = {"species_entry": {"smiles": "[2H][H]"}, "conformers": [
        {"geometry": {"xyz_text": "2\n\nH 0 0 0\nH 0 0 0.74", "isotopes": {"1": 2}},
         "primary_calculation": {"type": "opt"}, "additional_calculations": [calc]}]}
    warnings = []
    adapter_module._withhold_unlabelled_point_geometries(payload, "computed_species", warnings)
    assert "geometry" not in calc[field]["points"][0]
    assert [w["code"] for w in warnings] == ["geometry_isotopes_not_stated"]


def test_a_transition_states_subject_is_its_reactants_substitutions():
    calc = _point_calc()
    reaction = {"species": [{"key": "r0", "species_entry": {"smiles": "[2H]O"}, "calculations": []},
                            {"key": "p0", "species_entry": {"smiles": "O"}, "calculations": []}],
                "reactant_keys": ["r0"], "product_keys": ["p0"],
                "transition_state": {"calculations": [calc]}}
    warnings = []
    adapter_module._withhold_unlabelled_point_geometries(reaction, "computed_reaction", warnings)
    assert "geometry" not in calc["irc_result"]["points"][0] and len(warnings) == 1
    plain = _point_calc()
    unsubstituted = {"species": [{"key": "r0", "species_entry": {"smiles": "O"}, "calculations": []}],
                     "reactant_keys": ["r0"], "product_keys": ["r0"],
                     "transition_state": {"calculations": [plain]}}
    warnings = []
    adapter_module._withhold_unlabelled_point_geometries(unsubstituted, "computed_reaction", warnings)
    assert plain == _point_calc() and warnings == []
    standalone = {"reaction": {"reactants": [{"species_entry": {"smiles": "[13CH4]"}}]},
                  "primary_opt": {"type": "opt"}, "additional_calculations": [_point_calc()]}
    warnings = []
    adapter_module._withhold_unlabelled_point_geometries(standalone, "transition_state", warnings)
    assert len(warnings) == 1


def test_a_hessian_omitted_under_a_substituted_species_is_a_sidecar_warning():
    record = {"label": "D2", "smiles": "[2H][2H]", "xyz": "H 0 0 0\nH 0 0 0.74", "xyz_isotopes": [2, 2]}
    warnings = []
    assert adapter_module._hessian_geometry(record, "2\n\nO 0 0 0\nH 0 0 1", warnings) is None
    assert [w["code"] for w in warnings] == ["geometry_isotopes_not_stated"]
    assert warnings[0]["context"]["action"] == "hessian_omitted" and warnings[0]["field"] == "hessian.geometry"
    ok = []
    labelled = adapter_module._hessian_geometry(record, "2\n\nH 0 0 0\nH 0 0 0.74", ok)
    assert labelled["isotopes"] == {"1": 2, "2": 2} and ok == []
    assert adapter_module._hessian_geometry({"label": "H2", "xyz": record["xyz"]}, H2, []) == {"xyz_text": H2}


# --- the submit-site hooks, end to end through the submit_* entry points -------------------
#
# ``_enforce_geometry_anchoring`` and ``_withhold_unlabelled_point_geometries`` are called by each
# of the three whole-request builders' submit methods. These tests go through the public entry
# points with a client that refuses every request (nothing is posted: the payload and the outcome's
# warnings are what a producer's caller sees), so deleting a hook from any one route fails the test
# of that route and that hook.

class _RefusingClient:
    def __getattr__(self, name):
        raise AssertionError(f"the offline submit tests must not use the client ({name})")


def _offline_adapter(tmp_path, mode):
    cfg = TCKDBConfig(enabled=True, upload=False, base_url="http://localhost:8000/api/v1",
                      payload_dir=str(tmp_path / "payloads"), api_key_env="X_TCKDB_API_KEY",
                      project_label="proj-A", upload_mode=mode)
    return TCKDBAdapter(cfg, project_directory=str(tmp_path), client_factory=lambda c, k: _RefusingClient())


def _submit(adapter, route, doc, label):
    """Run one ``submit_*`` entry point; return (outcome, the payload it wrote, the sidecar's warnings)."""
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        if route == "computed_species":
            record = next(r for r in doc["species"] if r["label"] == label)
            outcome = adapter.submit_computed_species_from_output(output_doc=doc, species_record=record)
        elif route == "computed_reaction":
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=doc["reactions"][0])
        else:
            outcome = adapter.submit_computed_ts_from_output(
                output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0])
    assert outcome is not None and outcome.payload_path is not None
    written = json.loads(outcome.payload_path.read_text())
    payload = written.get("payload", written)
    sidecar = json.loads(outcome.sidecar_path.read_text()) if outcome.sidecar_path else {}
    return outcome, payload, [w["code"] for w in sidecar.get("warnings", [])]


def _calculations(payload, route):
    """Every calculation dict of a whole-request payload, by route."""
    if route == "computed_species":
        return [c for conf in payload["conformers"]
                for c in [conf["primary_calculation"], *conf.get("additional_calculations", [])]]
    if route == "computed_reaction":
        return [c for sp in payload["species"] for c in [*(x["calculation"] for x in sp["conformers"]),
                                                       *sp.get("calculations", [])]] + \
            list(payload["transition_state"]["calculations"])
    return [payload["primary_opt"], *payload.get("additional_calculations", [])]


def _points(calc):
    return [p for name in ("irc_result", "scan_result", "path_search_result")
            for p in (calc.get(name) or {}).get("points", [])]


def _drift(xyz_text):
    lines = xyz_text.strip().splitlines()
    body = lines[2:] if lines[0].strip().isdigit() else lines
    parts = body[0].split()
    parts[1] = f"{float(parts[1]) + 0.01:.8f}"
    body[0] = " ".join(parts)
    return "\n".join([*lines[:len(lines) - len(body)], *body])


@pytest.fixture
def drifting_input_geometries():
    """freq/sp declare an input geometry that has drifted from the conformer's own xyz."""
    original = TCKDBAdapter._input_geometries_for_calc

    def drifted(*, calc_role, species_record, conformer_xyz_text):
        out = original(calc_role=calc_role, species_record=species_record, conformer_xyz_text=conformer_xyz_text)
        if calc_role in ("freq", "sp"):
            return [{**g, "xyz_text": _drift(g["xyz_text"])} for g in out]
        return out

    with mock.patch.object(TCKDBAdapter, "_input_geometries_for_calc", staticmethod(drifted)):
        yield


def _reaction_doc():
    from test_arc_schema_1_3_levels import _doc13
    return _doc13(irc=False)


@pytest.mark.parametrize("route", ["computed_species", "computed_reaction", "transition_state"])
def test_a_drifted_freq_sp_input_geometry_is_dropped_on_every_submit_route(
        tmp_path, drifting_input_geometries, route):
    doc = _reaction_doc()
    label = doc["species"][0]["label"]
    outcome, payload, sidecar_codes = _submit(_offline_adapter(tmp_path, route), route, doc, label)
    calcs = _calculations(payload, route)
    declared = [c["type"] for c in calcs if c["type"] in ("freq", "sp") and "input_geometries" in c]
    assert not declared
    assert {c["type"] for c in calcs} >= {"freq", "sp"}
    assert "calculation_input_geometry_not_declared" in [w["code"] for w in outcome.warnings]
    assert "calculation_input_geometry_not_declared" in sidecar_codes


def _isotopic_reaction_doc(*, scan=True):
    """A 1.3 reaction whose reactant CH3D and transition state state their isotopes, with a scan and a trajectory."""
    from test_arc_schema_1_3_levels import _scan
    doc = _reaction_doc()
    ch4 = next(r for r in doc["species"] if r["label"] == "CH4")
    ch4.update(smiles="[2H]C", xyz_isotopes=[12, 2, 1, 1, 1])
    ts = doc["transition_states"][0]
    ts["xyz_isotopes"] = [12, 2, 1]
    if scan:
        doc["scan_level"] = {"method": "b3lyp", "basis": "6-31g", "software": "orca"}
        entry = _scan()
        entry["result"]["coordinate"]["atom_indices"] = [0, 1, 2, 3]
        bare = [{"source_index": i, "angle_degrees": 60.0 * i, "relative_energy_kj_mol": float(i),
                 "geometry_xyz": ch4["xyz"].replace("1.0", f"1.{i}", 1), "geometry_isotopes": None}
                for i in range(3)]
        # one frame in another element order (a stand-in for a log that renumbered the atoms)
        bare[2]["geometry_xyz"] = "H 1.0 0.0 0.0\nC 0.0 0.0 0.0\nH -1.0 0.0 0.0\nH 0.0 1.0 0.0\nH 0.0 -1.0 0.0"
        entry["result"]["samples"] = bare
        ch4["rotor_scans"] = [entry]
    return doc


def _irc_trajectories(self, output_doc, ts_record):
    on = {"symbols": ("C", "H", "H"), "isotopes": (12, 1, 1)}
    off = {"symbols": ("H", "C", "H"), "isotopes": (1, 12, 1)}
    coords = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.5, 0.5, 0.0))
    return [{"direction": "forward", "rich_points": None,
             "geom_points": [{**on, "coords": coords}, {**off, "coords": coords}]},
            {"direction": "reverse", "rich_points": None, "geom_points": [{**on, "coords": coords}]}]


@pytest.fixture
def irc_trajectories():
    with mock.patch.object(TCKDBAdapter, "_parse_irc_trajectories", _irc_trajectories):
        yield


def _assert_points_are_labelled_or_absent(calc):
    for point in _points(calc):
        geometry = point.get("geometry")
        assert geometry is None or geometry.get("isotopes"), (calc["key"], point)


@pytest.mark.parametrize("route", ["computed_reaction", "transition_state"])
def test_irc_points_of_a_substituted_reaction_are_labelled_or_dropped_on_the_ts_routes(
        tmp_path, irc_trajectories, route):
    doc = _isotopic_reaction_doc(scan=False)
    ts = doc["transition_states"][0]
    ts["irc_logs"] = ["TS0_irc_f.log", "TS0_irc_r.log"]
    ts["irc_log_directions"] = ["forward", "reverse"]
    ts["irc_log_levels"] = [dict(doc["opt_level"])] * 2
    ts["irc_log_routes"] = [None, None]
    ts["irc_converged"] = True
    outcome, payload, sidecar_codes = _submit(_offline_adapter(tmp_path, route), route, doc, "CH4")
    irc = next(c for c in _calculations(payload, route) if c["type"] == "irc")
    points = _points(irc)
    # Frames in the TS's own element order carry the TS's isotopes (atom 2 deuterium); the marker too.
    labelled = [p for p in points if p.get("geometry")]
    assert labelled and all(p["geometry"]["isotopes"] == {"2": 2} for p in labelled)
    # The frame in another element order lost its geometry, not its point, and is reported.
    assert len(labelled) == len(points) - 1
    assert "geometry_isotopes_not_stated" in [w["code"] for w in outcome.warnings]
    assert "geometry_isotopes_not_stated" in sidecar_codes
    for calc in _calculations(payload, route):
        _assert_points_are_labelled_or_absent(calc)


def test_scan_points_of_a_substituted_reaction_species_are_labelled_or_dropped(tmp_path):
    doc = _isotopic_reaction_doc()
    outcome, payload, sidecar_codes = _submit(
        _offline_adapter(tmp_path, "computed_reaction"), "computed_reaction", doc, "CH4")
    ch4 = next(sp for sp in payload["species"] if sp["species_entry"]["smiles"] == "[2H]C")
    scan = next(c for c in ch4["calculations"] if c["type"] == "scan")
    points = scan["scan_result"]["points"]
    assert [p["geometry"]["isotopes"] for p in points[:2]] == [{"2": 2}, {"2": 2}]
    assert "geometry" not in points[2]
    assert "geometry_isotopes_not_stated" in [w["code"] for w in outcome.warnings]
    assert "geometry_isotopes_not_stated" in sidecar_codes


def test_scan_points_of_a_substituted_species_on_the_computed_species_route(tmp_path):
    from test_arc_schema_1_3_levels import _project, _record, _scan

    doc = _project(tmp_path)
    record = _record(doc, "iC3H7_d")
    doc["scan_level"] = {"method": "b3lyp", "basis": "6-31g", "software": "orca"}
    entry = _scan()
    entry["result"]["coordinate"]["atom_indices"] = [0, 1, 2, 3]
    xyz = record["xyz"]
    swapped = "\n".join(reversed(xyz.strip().splitlines()))
    entry["result"]["samples"] = [
        {"source_index": 0, "angle_degrees": 0.0, "relative_energy_kj_mol": 0.0, "geometry_xyz": xyz,
         "geometry_isotopes": None},
        {"source_index": 1, "angle_degrees": 60.0, "relative_energy_kj_mol": 1.0, "geometry_xyz": swapped,
         "geometry_isotopes": None}]
    record["rotor_scans"] = [entry]
    outcome, payload, sidecar_codes = _submit(
        _offline_adapter(tmp_path, "computed_species"), "computed_species", doc, "iC3H7_d")
    scan = next(c for c in _calculations(payload, "computed_species") if c["type"] == "scan")
    points = scan["scan_result"]["points"]
    assert points[0]["geometry"]["isotopes"] == {"4": 2}
    assert "geometry" not in points[1]
    assert "geometry_isotopes_not_stated" in [w["code"] for w in outcome.warnings]
    assert "geometry_isotopes_not_stated" in sidecar_codes


def test_the_sweep_prints_why_an_artifact_was_skipped(capsys):
    from types import SimpleNamespace
    from tckdb_arc import sweep
    from tckdb_arc.adapter import ArtifactUploadOutcome

    skipped = ArtifactUploadOutcome(
        status="skipped", sidecar_path=None, idempotency_key=None, calculation_id=7, calculation_ref=None,
        kind="output_log", skip_reason="calculation_ref_not_returned: no ref")
    adapter = SimpleNamespace(submit_artifact_batch_for_calculation=lambda **kw: [skipped])
    counts, failures = {}, []
    outcome = SimpleNamespace(primary_calculation={"calculation_id": 7, "type": "opt"}, additional_calculations=[])
    with mock.patch.object(sweep, "_resolve_artifact_path", return_value="/x/opt.log"):
        sweep._sweep_artifacts_for_species(
            adapter=adapter, output_doc={}, species_record={"label": "CH4"}, outcome=outcome,
            counts=counts, failures=failures, kinds=["output_log"])
    assert counts == {"skipped": 1} and failures == []
    assert "artifact skipped: CH4 output_log: calculation_ref_not_returned: no ref" in capsys.readouterr().out
