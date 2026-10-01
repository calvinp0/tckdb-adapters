"""Levels the adapter states only when ARC's output supports them.

* A14: a TS's ``validation_evidence`` comes from ARC's explicit IRC verdict
  ``ts_checks['IRC']`` and never from ``irc_converged``.
* A2b: under ARC ``adaptive_levels`` output.yml records one level per run, so
  the levels of the job types the adaptive levels name are not attributable
  per species. The adapter detects such runs from the project's
  ``restart.yml`` / ``input.yml`` and stops stating those levels.

(A3, the NEB/GSM path-search level, and A4, screened conformers, are covered
in ``test_adapter.py`` and ``test_golden_corpus.py``.)
"""

import copy
import json
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from _contract import contract_validate
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.adaptive import detect_adaptive_levels
from tckdb_arc.config import TCKDBConfig
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

from test_adapter import (
    _StubClient,
    _StubResponse,
    _fake_output_doc,
    _full_record,
    _reaction_output_doc,
    _reaction_record,
)


def _adapter(tmp_path, *, mode="all", project_directory=True, **kwargs):
    cfg = TCKDBConfig(
        enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=str(tmp_path / "payloads"),
        api_key_env="X_TCKDB_API_KEY", project_label="levels", upload_mode=mode,
        upload=False, preflight=False,
    )
    client = _StubClient(response=_StubResponse({"id": 1}))
    return TCKDBAdapter(
        cfg, project_directory=str(tmp_path) if project_directory else None,
        client_factory=lambda c, k: client, **kwargs)


def _built(outcome):
    return json.loads(outcome.payload_path.read_text())


def _codes(outcome):
    return [w["code"] for w in outcome.warnings]


# ---------------------------------------------------------------------------
# A14: TS validation evidence from ts_checks['IRC']
# ---------------------------------------------------------------------------

# ``freq`` is left unassessed: these tests are about the IRC verdict, and a stated freq verdict would
# add an ``imaginary_mode`` record of its own (test_ts_imaginary_mode_evidence.py).
IRC_CHECKS = {"E0": None, "e_elect": None, "freq": None, "NMD": None}


def _ts_doc(*, irc, warnings="", irc_converged=True, irc_logs=True, checks=True):
    doc = copy.deepcopy(_reaction_output_doc(with_irc=irc_logs))
    ts = doc["transition_states"][0]
    ts["converged"] = True
    ts["irc_converged"] = irc_converged
    if checks:
        ts["ts_checks"] = {**IRC_CHECKS, "IRC": irc, "warnings": warnings}
    return doc


def _reaction_payload(tmp_path, doc):
    outcome = _adapter(tmp_path).submit_computed_reaction_from_output(
        output_doc=doc, reaction_record=doc["reactions"][0])
    return outcome, _built(outcome)


def _standalone_payload(tmp_path, doc):
    outcome = _adapter(tmp_path).submit_computed_ts_from_output(
        output_doc=doc, ts_record=doc["transition_states"][0],
        reaction_record=doc["reactions"][0])
    return outcome, _built(outcome)


@pytest.mark.parametrize("verdict", [True, False])
def test_reaction_route_sends_the_irc_verdict_as_validation_evidence(tmp_path, verdict):
    outcome, payload = _reaction_payload(tmp_path, _ts_doc(irc=verdict))
    ts = payload["transition_state"]
    assert ts["validation_evidence"] == [{
        "kind": "irc", "passed": verdict,
        "rationale": f"ARC ts_checks['IRC'] = {verdict}",
        "source_calculation_key": "ts_irc",
    }]
    assert "ts_irc" in [c["key"] for c in ts["calculations"]]
    contract_validate(ComputedReactionUploadRequest, payload)


@pytest.mark.parametrize("verdict", [True, False])
def test_standalone_route_sends_the_irc_verdict_without_a_calculation_key(tmp_path, verdict):
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=verdict))
    # The standalone request has no calculation-key namespace: the evidence
    # binds to its single irc calculation and must omit the key.
    assert payload["validation_evidence"] == [{
        "kind": "irc", "passed": verdict,
        "rationale": f"ARC ts_checks['IRC'] = {verdict}",
    }]
    assert [c["type"] for c in payload["additional_calculations"]].count("irc") == 1
    contract_validate(TransitionStateUploadRequest, payload)


@pytest.mark.parametrize("route", ["reaction", "standalone"])
@pytest.mark.parametrize("checks", [
    pytest.param({"irc": None}, id="verdict_none"),
    pytest.param({"irc": None, "checks": False}, id="no_ts_checks"),
])
def test_no_verdict_sends_no_evidence_even_when_the_irc_jobs_converged(tmp_path, route, checks):
    # irc_converged says only that the IRC jobs finished (ARC arc/output.py
    # ``_ts_checks_to_dict``); it is never a verdict.
    doc = _ts_doc(irc_converged=True, **checks)
    build = _reaction_payload if route == "reaction" else _standalone_payload
    outcome, payload = build(tmp_path, doc)
    assert "validation_evidence" not in json.dumps(payload)
    assert "ts_irc_evidence_without_irc_calculation" not in _codes(outcome)


@pytest.mark.parametrize("route", ["reaction", "standalone"])
def test_the_verdict_is_ts_checks_irc_not_irc_converged(tmp_path, route):
    build = _reaction_payload if route == "reaction" else _standalone_payload

    def evidence(payload):
        block = payload["transition_state"] if route == "reaction" else payload
        return [e["passed"] for e in block["validation_evidence"]]

    _, failed_but_converged = build(tmp_path, _ts_doc(irc=False, irc_converged=True))
    assert evidence(failed_but_converged) == [False]
    _, passed_but_unconverged = build(tmp_path, _ts_doc(irc=True, irc_converged=False))
    assert evidence(passed_but_unconverged) == [True]


def test_rationale_never_carries_arcs_ts_check_warnings(tmp_path):
    # ts_checks['warnings'] come only from the e_elect and normal-mode-
    # displacement checks (arc/checks/ts.py, arc/checks/nmd.py), never the IRC,
    # so they are not the IRC's rationale.
    doc = _ts_doc(irc=False, warnings="Normal mode displacement of the TS is unclear")
    _, payload = _reaction_payload(tmp_path, doc)
    [evidence] = payload["transition_state"]["validation_evidence"]
    assert evidence["rationale"] == "ARC ts_checks['IRC'] = False"
    contract_validate(ComputedReactionUploadRequest, payload)


@pytest.mark.parametrize("route", ["reaction", "standalone"])
def test_a_verdict_with_no_irc_calculation_is_not_sent_and_is_reported(tmp_path, route):
    build = _reaction_payload if route == "reaction" else _standalone_payload
    outcome, payload = build(tmp_path, _ts_doc(irc=True, irc_logs=False))
    assert "validation_evidence" not in json.dumps(payload)
    [warning] = [w for w in outcome.warnings
                 if w["code"] == "ts_irc_evidence_without_irc_calculation"]
    assert warning["context"]["ts_checks_irc"] == "true"


# ---------------------------------------------------------------------------
# A2b: adaptive_levels detection
# ---------------------------------------------------------------------------

ADAPTIVE_SP_ONLY = [
    {"atom_range": [1, 5], "levels": {"sp": {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}}},
    {"atom_range": [6, "inf"], "levels": {"sp": {"method": "wb97xd", "basis": "def2tzvp"}}},
]
ADAPTIVE_OPT_FREQ_SP = [
    {"atom_range": [1, 5], "levels": {"opt freq": {"method": "wb97xd", "basis": "def2tzvp"},
                                      "sp": {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}}},
    {"atom_range": [6, "inf"], "levels": {"opt freq": {"method": "b3lyp", "basis": "6-31g(d,p)"},
                                          "sp": {"method": "wb97xd", "basis": "def2tzvp"}}},
]


def _write_restart(project, adaptive):
    Path(project, "restart.yml").write_text(yaml.safe_dump({"adaptive_levels": adaptive}))


def _species_payload(tmp_path, **adapter_kwargs):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species", **adapter_kwargs)\
            .submit_computed_species_from_output(
                output_doc=_fake_output_doc(), species_record=_full_record())
    return outcome, _built(outcome)


def _calc_types(payload):
    conformer = payload["conformers"][0]
    return [conformer["primary_calculation"]["type"],
            *(c["type"] for c in conformer["additional_calculations"])]


def test_baseline_without_adaptive_levels_states_sp_and_enthalpy(tmp_path):
    outcome, payload = _species_payload(tmp_path)
    assert _calc_types(payload) == ["opt", "freq", "sp"]
    assert payload["thermo"]["enthalpy_reference_kind"] == "formation_298k"
    assert not [c for c in _codes(outcome) if "adaptive" in c]


@pytest.mark.parametrize("source", ["restart.yml", "input.yml"])
def test_adaptive_sp_levels_omit_the_sp_calculation_and_the_enthalpy(tmp_path, source):
    # Detection works from either file of the project directory. restart.yml
    # is ARC's own record (levels as dicts); the user's input.yml may spell
    # levels as strings.
    levels = ADAPTIVE_SP_ONLY if source == "restart.yml" else [
        {"atom_range": [1, 5], "levels": {"sp": "ccsd(t)-f12/cc-pvtz-f12"}},
        {"atom_range": [6, "inf"], "levels": {"sp": "wb97xd/def2tzvp"}},
    ]
    Path(tmp_path, source).write_text(yaml.safe_dump({"adaptive_levels": levels}))
    outcome, payload = _species_payload(tmp_path)
    # opt and freq are not named, so their levels stay attributable.
    assert _calc_types(payload) == ["opt", "freq"]
    thermo = payload["thermo"]
    for stripped in ("h298_kj_mol", "nasa", "enthalpy_reference_kind"):
        assert stripped not in thermo
    assert thermo["s298_j_mol_k"] == _full_record()["thermo"]["s298_j_mol_k"]
    assert {s["role"] for s in thermo["source_calculations"]} == {"opt", "freq"}
    codes = _codes(outcome)
    assert "sp_level_adaptive_not_attributable" in codes
    assert "enthalpy_adaptive_levels_unverifiable" in codes
    [enthalpy] = [w for w in outcome.warnings if w["code"] == "enthalpy_adaptive_levels_unverifiable"]
    assert enthalpy["context"]["action"] == "thermo_enthalpy_omitted"
    [sp] = [w for w in outcome.warnings if w["code"] == "sp_level_adaptive_not_attributable"]
    assert source in sp["context"]["adaptive_levels_sources"]
    assert json.loads(outcome.sidecar_path.read_text())["warnings"] == outcome.warnings
    contract_validate(ComputedSpeciesUploadRequest, payload)


def test_adaptive_levels_from_a_parsed_input_dict(tmp_path):
    # The CLI hands over its parsed input.yml, which need not sit in the
    # project directory.
    outcome, payload = _species_payload(
        tmp_path, input_dict={"adaptive_levels": ADAPTIVE_SP_ONLY})
    assert _calc_types(payload) == ["opt", "freq"]
    assert "enthalpy_adaptive_levels_unverifiable" in _codes(outcome)


def test_adaptive_run_without_a_project_directory_cannot_be_detected(tmp_path):
    # output.yml alone does not record adaptive_levels: with no project
    # directory (and no input dict) the run reads as an ordinary one.
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"adaptive_levels": ADAPTIVE_SP_ONLY}))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species", project_directory=False)\
            .submit_computed_species_from_output(
                output_doc=_fake_output_doc(), species_record=_full_record())
    payload = _built(outcome)
    assert _calc_types(payload) == ["opt", "freq", "sp"]
    assert payload["thermo"]["enthalpy_reference_kind"] == "formation_298k"


def test_a_restart_yml_without_adaptive_levels_changes_nothing(tmp_path):
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"opt_level": {"method": "wb97xd"}}))
    Path(tmp_path, "input.yml").write_text(yaml.safe_dump({"adaptive_levels": []}))
    _, payload = _species_payload(tmp_path)
    assert _calc_types(payload) == ["opt", "freq", "sp"]


@pytest.mark.parametrize("adaptive", [ADAPTIVE_OPT_FREQ_SP, [
    {"atom_range": [1, "inf"], "levels": {"opt": "b3lyp/6-31g(d)"}}]],
    ids=["opt_freq_sp", "opt_only"])
def test_adaptive_opt_levels_refuse_the_upload(tmp_path, adaptive):
    # A primary optimization requires a level and output.yml's opt_level is
    # not the level any given species ran at: there is no honest partial form.
    _write_restart(tmp_path, adaptive)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        with pytest.raises(ValueError, match="opt_level_adaptive_not_attributable"):
            _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
                output_doc=_fake_output_doc(), species_record=_full_record())
        with pytest.raises(ValueError, match="opt_level_adaptive_not_attributable"):
            _adapter(tmp_path, mode="conformer").submit_from_output(
                output_doc=_fake_output_doc(), species_record=_full_record())


def test_adaptive_opt_refuses_even_with_a_distinct_sp_level(tmp_path):
    # ``opt`` named: the upload is refused whatever the run-level sp_level is.
    _write_restart(tmp_path, [{"atom_range": [1, "inf"], "levels": {"opt freq": "b3lyp/6-31g(d)"}}])
    doc = _fake_output_doc()
    doc["sp_level"] = {"method": "ccsd(t)", "basis": "cc-pvtz", "software": "gaussian"}
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        with pytest.raises(ValueError, match="opt_level_adaptive_not_attributable"):
            _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
                output_doc=doc, species_record=_full_record())


def test_adaptive_freq_only_omits_the_freq_calculation(tmp_path):
    # ``freq`` named without ``opt``: the freq level is per species, the opt
    # level is the run's. The freq calculation is omitted and reported.
    _write_restart(tmp_path, [
        {"atom_range": [1, 5], "levels": {"freq": "wb97xd/def2tzvp"}},
        {"atom_range": [6, "inf"], "levels": {"freq": "b3lyp/6-31g(d)"}},
    ])
    outcome, payload = _species_payload(tmp_path)
    assert _calc_types(payload) == ["opt", "sp"]
    assert "freq_level_adaptive_not_attributable" in _codes(outcome)
    assert "sp_level_adaptive_not_attributable" not in _codes(outcome)


def test_adaptive_composite_level_strips_the_enthalpy_only(tmp_path):
    _write_restart(tmp_path, [
        {"atom_range": [1, 5], "levels": {"composite": "cbs-qb3"}},
        {"atom_range": [6, "inf"], "levels": {"composite": "g4"}},
    ])
    doc = _fake_output_doc()
    doc["composite_method"] = {"method": "cbs-qb3", "software": "gaussian"}
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
            output_doc=doc, species_record=_full_record())
    payload = _built(outcome)
    assert _calc_types(payload) == ["opt", "freq", "sp"]
    assert "h298_kj_mol" not in payload["thermo"]
    assert "enthalpy_adaptive_levels_unverifiable" in _codes(outcome)
    assert "sp_level_adaptive_not_attributable" not in _codes(outcome)


def test_reaction_route_omits_every_sp_and_every_enthalpy(tmp_path):
    _write_restart(tmp_path, ADAPTIVE_SP_ONLY)
    doc = _reaction_output_doc()
    for record in doc["species"]:
        record["thermo"] = copy.deepcopy(_full_record()["thermo"])
    outcome, payload = _reaction_payload(tmp_path, doc)
    for species in payload["species"]:
        assert "sp" not in [c["type"] for c in species["calculations"]]
        assert "h298_kj_mol" not in species["thermo"]
    assert "sp" not in [c["type"] for c in payload["transition_state"]["calculations"]]
    codes = _codes(outcome)
    assert codes.count("sp_level_adaptive_not_attributable") == 1  # once per upload
    assert codes.count("enthalpy_adaptive_levels_unverifiable") == 4  # 4 participants
    contract_validate(ComputedReactionUploadRequest, payload)


def test_standalone_ts_route_omits_its_sp(tmp_path):
    _write_restart(tmp_path, ADAPTIVE_SP_ONLY)
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=None))
    assert "sp" not in [c["type"] for c in payload["additional_calculations"]]
    assert "sp_level_adaptive_not_attributable" in _codes(outcome)
    contract_validate(TransitionStateUploadRequest, payload)


def test_adaptive_irc_level_omits_the_irc_calculation_and_its_evidence(tmp_path):
    _write_restart(tmp_path, [{"atom_range": [1, "inf"], "levels": {"irc": "b3lyp/6-31g(d)"}}])
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=True))
    assert "irc" not in [c["type"] for c in payload["additional_calculations"]]
    assert "validation_evidence" not in payload
    codes = _codes(outcome)
    assert "irc_level_adaptive_not_attributable" in codes
    assert "ts_irc_evidence_without_irc_calculation" in codes


def test_conformer_route_omits_the_adaptive_sp(tmp_path):
    _write_restart(tmp_path, ADAPTIVE_SP_ONLY)
    outcome = _adapter(tmp_path, mode="conformer").submit_from_output(
        output_doc=_fake_output_doc(), species_record=_full_record())
    payload = _built(outcome)
    assert [c["type"] for c in payload["additional_calculations"]] == ["freq"]
    assert "sp_level_adaptive_not_attributable" in _codes(outcome)


def test_the_callers_output_doc_is_not_mutated(tmp_path):
    _write_restart(tmp_path, ADAPTIVE_SP_ONLY)
    doc = _fake_output_doc()
    before = copy.deepcopy(doc)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
            output_doc=doc, species_record=_full_record())
    assert doc == before


# ---------------------------------------------------------------------------
# detect_adaptive_levels
# ---------------------------------------------------------------------------


def test_detection_names_the_job_types_and_their_sources(tmp_path):
    _write_restart(tmp_path, ADAPTIVE_OPT_FREQ_SP)
    Path(tmp_path, "input.yml").write_text(yaml.safe_dump(
        {"adaptive_levels": [{"atom_range": [1, "inf"], "levels": {"opt, freq": "b3lyp/6-31g"}}]}))
    found = detect_adaptive_levels(tmp_path)
    assert found.job_types == {"opt", "freq", "sp"}
    assert found.sources == ("restart.yml", "input.yml")


@pytest.mark.parametrize("content", ["", "- just\n- a list\n", "adaptive_levels: not-a-list\n",
                                     "adaptive_levels: [1, 2]\n", ": : :\n"])
def test_detection_ignores_absent_or_malformed_specs(tmp_path, content):
    Path(tmp_path, "restart.yml").write_text(content)
    assert detect_adaptive_levels(tmp_path) is None


def test_detection_without_any_source_is_none(tmp_path):
    assert detect_adaptive_levels(tmp_path) is None
    assert detect_adaptive_levels(None) is None
    assert detect_adaptive_levels(None, {"opt_level": "x"}) is None


# ---------------------------------------------------------------------------
# Exact per-species attribution from restart.yml
# ---------------------------------------------------------------------------

from tckdb_arc.adaptive import RUN_LEVEL, UNDETERMINABLE, read_restart_info  # noqa: E402

SMALL = {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12", "software": "molpro"}
LARGE = {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}
OPT_SMALL = {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}
OPT_LARGE = {"method": "b3lyp", "basis": "6-31g(d,p)", "software": "gaussian"}
# What ARC's restart.yml stores for a level: Level.as_dict() keeps ``repr`` and
# ``compatible_ess``, which output.yml's levels drop.
RESTART_EXTRAS = {"repr": "x", "compatible_ess": ["gaussian", "orca"], "method_type": "dft"}
THREE_HEAVY_XYZ = "C 0.0 0.0 0.0\nC 1.5 0.0 0.0\nO 2.5 0.0 0.0\nH 0.0 1.0 0.0"


def _restart(project, ranges, species=("ethanol",), **extra):
    """Write a restart.yml: adaptive ranges, a species list, and other entries."""
    listing = [
        {"label": label, **({"adaptive_lot_n_heavy": n} if n is not None else {})}
        for label, n in (
            (entry, None) if isinstance(entry, str) else entry for entry in species)
    ]
    adaptive = [
        {"atom_range": list(atom_range),
         "levels": {key: {**level, **RESTART_EXTRAS} for key, level in levels.items()}}
        for atom_range, levels in ranges
    ]
    Path(project, "restart.yml").write_text(yaml.safe_dump(
        {"adaptive_levels": adaptive, "species": listing, **extra}))


TWO_RANGES = [
    ((1, 2), {"opt freq": OPT_SMALL, "sp": SMALL}),
    ((3, "inf"), {"opt freq": OPT_LARGE, "sp": LARGE}),
]


def _levels(payload):
    conformer = payload["conformers"][0]
    calcs = [conformer["primary_calculation"], *conformer["additional_calculations"]]
    return {c["type"]: (c["level_of_theory"]["method"], c["level_of_theory"]["basis"],
                        c["software_release"]["name"]) for c in calcs}


def _submit_record(tmp_path, record, doc=None, **kwargs):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species", **kwargs)\
            .submit_computed_species_from_output(
                output_doc=doc or _fake_output_doc(), species_record=record)
    return outcome, _built(outcome)


def _with_flags(record, level):
    record["thermo"]["atom_corrections_applied"] = True
    record["thermo"]["atom_corrections_level"] = dict(level)
    return record


def test_uniform_spec_attributes_every_named_job_type_exactly(tmp_path):
    _restart(tmp_path, [((1, "inf"), {"opt freq": OPT_LARGE, "sp": LARGE})])
    outcome, payload = _submit_record(tmp_path, _full_record())
    assert _levels(payload) == {
        "opt": ("b3lyp", "6-31g(d,p)", "gaussian"),
        "freq": ("b3lyp", "6-31g(d,p)", "gaussian"),
        "sp": ("wb97xd", "def2tzvp", "gaussian"),
    }
    assert not [c for c in _codes(outcome) if "adaptive" in c]
    # The restart-only keys ARC's Level.as_dict() adds never reach the payload.
    assert "compatible_ess" not in json.dumps(payload) and '"repr"' not in json.dumps(payload)


def test_two_ranges_give_each_species_its_own_level(tmp_path):
    _restart(tmp_path, TWO_RANGES, species=("small", "large"))
    _, small = _submit_record(tmp_path, {**_full_record(), "label": "small"})
    large_record = {**_full_record(), "label": "large", "xyz": THREE_HEAVY_XYZ}
    _, large = _submit_record(tmp_path, large_record)
    assert _levels(small)["sp"] == ("ccsd(t)-f12", "cc-pvtz-f12", "molpro")
    assert _levels(small)["opt"] == ("wb97xd", "def2tzvp", "gaussian")
    assert _levels(large)["sp"] == ("wb97xd", "def2tzvp", "gaussian")
    assert _levels(large)["opt"] == ("b3lyp", "6-31g(d,p)", "gaussian")


def test_enthalpy_is_kept_when_the_correction_level_matches_the_attributed_energy_level(tmp_path):
    # The species' energy level is its attributed sp level, not the run's.
    _restart(tmp_path, TWO_RANGES, species=("ethanol",))
    matching = _with_flags(_full_record(), SMALL)
    outcome, payload = _submit_record(tmp_path, matching)
    assert payload["thermo"]["enthalpy_reference_kind"] == "formation_298k"
    assert "h298_kj_mol" in payload["thermo"]
    assert not [c for c in _codes(outcome) if "enthalpy" in c]
    # Corrections applied at another level than the attributed sp level: stripped
    # for the level mismatch, not for being adaptive.
    mismatched = _with_flags(_full_record(), LARGE)
    outcome, payload = _submit_record(tmp_path, mismatched)
    assert "h298_kj_mol" not in payload["thermo"]
    assert "enthalpy_atom_corrections_level_mismatch" in _codes(outcome)
    assert "enthalpy_adaptive_levels_unverifiable" not in _codes(outcome)


def test_adaptive_lot_n_heavy_override_selects_the_range(tmp_path):
    # ARC keys the level on adaptive_lot_n_heavy when set (reaction-wide rule),
    # not on the species' own atoms: one heavy atom, keyed as three.
    _restart(tmp_path, TWO_RANGES, species=(("ethanol", 3),))
    _, payload = _submit_record(tmp_path, _full_record())
    assert _levels(payload)["sp"] == ("wb97xd", "def2tzvp", "gaussian")
    assert _levels(payload)["opt"][0] == "b3lyp"


def test_a_reaction_ts_copy_takes_the_level_of_its_own_range(tmp_path):
    # ARC creates ``<label>_TS<i>`` copies keyed on the reaction-wide heavy-atom
    # count; the original keeps its own.
    _restart(tmp_path, TWO_RANGES, species=("ethanol", ("ethanol_TS0", 3)))
    _, original = _submit_record(tmp_path, _full_record())
    _, copy_ = _submit_record(tmp_path, {**_full_record(), "label": "ethanol_TS0"})
    assert _levels(original)["sp"][0] == "ccsd(t)-f12"
    assert _levels(copy_)["sp"][0] == "wb97xd"


def test_a_job_type_the_range_does_not_name_runs_at_the_run_level(tmp_path):
    # Only ``sp`` is named: opt and freq keep the run's opt_level, as in ARC.
    _restart(tmp_path, [((1, "inf"), {"sp": SMALL})])
    outcome, payload = _submit_record(tmp_path, _full_record())
    assert _levels(payload) == {
        "opt": ("wb97xd", "def2-tzvp", "gaussian"),
        "freq": ("wb97xd", "def2-tzvp", "gaussian"),
        "sp": ("ccsd(t)-f12", "cc-pvtz-f12", "molpro"),
    }
    assert not [c for c in _codes(outcome) if "adaptive" in c]


def test_a_job_type_named_only_in_another_range_runs_at_the_run_level(tmp_path):
    _restart(tmp_path, [((1, 2), {"opt freq": OPT_SMALL}), ((3, "inf"), {"sp": LARGE})])
    _, payload = _submit_record(tmp_path, _full_record())  # 1 heavy atom: range 1
    assert _levels(payload)["opt"] == ("wb97xd", "def2tzvp", "gaussian")
    # sp is named only in range 2, so range 1 uses the run's sp level (null: the
    # opt level, which range 1 attributes exactly).
    assert _levels(payload)["sp"] == ("wb97xd", "def2tzvp", "gaussian")


def test_keys_match_job_types_case_sensitively(tmp_path):
    # ARC compares ``job_type in job_type_tuple`` with no case folding: ``SP``
    # names nothing, so sp runs at the run level and nothing is unattributable.
    _restart(tmp_path, [((1, "inf"), {"SP": SMALL, "Opt": OPT_LARGE})])
    outcome, payload = _submit_record(tmp_path, _full_record())
    assert _levels(payload)["sp"] == ("wb97xd", "def2-tzvp", "gaussian")
    assert _levels(payload)["opt"] == ("wb97xd", "def2-tzvp", "gaussian")
    assert not [c for c in _codes(outcome) if "adaptive" in c]
    found = detect_adaptive_levels(tmp_path)
    assert found.job_types == {"SP", "Opt"}


@pytest.mark.parametrize("ranges,n,expected", [
    (TWO_RANGES, 2, "small"), (TWO_RANGES, 3, "large"), (TWO_RANGES, 400, "large"),
])
def test_range_bounds_follow_arcs_rule(tmp_path, ranges, n, expected):
    _restart(tmp_path, ranges, species=("x",))
    restart = read_restart_info(tmp_path)
    level = restart.attribute_level("x", "sp", n)
    assert level["method"] == ("ccsd(t)-f12" if expected == "small" else "wb97xd")


def test_attribution_falls_back_when_the_level_cannot_be_worked_out(tmp_path):
    _restart(tmp_path, TWO_RANGES, species=("x",))
    restart = read_restart_info(tmp_path)
    assert restart.attribute_level("y", "sp", 3) == UNDETERMINABLE  # no species entry
    assert restart.attribute_level("x", "sp", None) == UNDETERMINABLE  # no atom count
    assert restart.attribute_level("x", "irc", 3) == RUN_LEVEL  # range names no irc
    # A range that is not covered (ARC would raise) is not guessed.
    _restart(tmp_path, [((1, 2), {"sp": SMALL})], species=("x",))
    assert read_restart_info(tmp_path).attribute_level("x", "sp", 9) == UNDETERMINABLE
    # A malformed spec is not partially trusted.
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({
        "adaptive_levels": [{"atom_range": [1, "inf"], "levels": {"sp": "not-a-dict"}}],
        "species": [{"label": "x"}]}))
    assert read_restart_info(tmp_path).attribute_level("x", "sp", 3) == UNDETERMINABLE


def test_species_absent_from_restart_yml_is_refused_or_omitted_as_before(tmp_path):
    # restart.yml holds the adaptive spec but not this species (e.g. a copy
    # created after the restart file was written): no exact level, so the
    # fallback: opt named -> refused.
    _restart(tmp_path, TWO_RANGES, species=("someone_else",))
    with pytest.raises(ValueError, match="opt_level_adaptive_not_attributable"):
        _submit_record(tmp_path, _full_record())


def test_sp_only_spec_without_the_species_omits_sp_and_strips_enthalpy(tmp_path):
    _restart(tmp_path, [((1, "inf"), {"sp": SMALL})], species=("someone_else",))
    outcome, payload = _submit_record(tmp_path, _full_record())
    assert list(_levels(payload)) == ["opt", "freq"]
    assert {"sp_level_adaptive_not_attributable", "enthalpy_adaptive_levels_unverifiable"} \
        <= set(_codes(outcome))
    [warning] = [w for w in outcome.warnings if w["code"] == "sp_level_adaptive_not_attributable"]
    assert warning["context"]["labels"] == "ethanol"


def test_input_yml_alone_cannot_attribute(tmp_path):
    # input.yml has string levels and no adaptive_lot_n_heavy: refuse/omit.
    Path(tmp_path, "input.yml").write_text(yaml.safe_dump({"adaptive_levels": [
        {"atom_range": [1, "inf"], "levels": {"sp": "ccsd(t)-f12/cc-pvtz-f12"}}]}))
    outcome, payload = _submit_record(tmp_path, _full_record())
    assert list(_levels(payload)) == ["opt", "freq"]
    assert "sp_level_adaptive_not_attributable" in _codes(outcome)


def test_restart_yml_names_but_holds_no_spec_when_only_input_yml_has_it(tmp_path):
    _restart(tmp_path, [], species=("ethanol",))
    Path(tmp_path, "input.yml").write_text(yaml.safe_dump({"adaptive_levels": [
        {"atom_range": [1, "inf"], "levels": {"sp": "ccsd(t)-f12/cc-pvtz-f12"}}]}))
    outcome, payload = _submit_record(tmp_path, _full_record())
    assert list(_levels(payload)) == ["opt", "freq"]


def test_reaction_route_attributes_each_participant_by_its_own_atoms(tmp_path):
    doc = _reaction_output_doc()
    labels = [s["label"] for s in doc["species"]]
    _restart(tmp_path, [((1, 1), {"sp": SMALL}), ((2, "inf"), {"sp": LARGE})], species=labels)
    for record in doc["species"]:
        record["xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0"  # 1 heavy atom each
    doc["transition_states"][0]["label"] = "TS0"
    _restart(tmp_path, [((1, 1), {"sp": SMALL}), ((2, "inf"), {"sp": LARGE})],
             species=[*labels, "TS0"])
    outcome, payload = _reaction_payload(tmp_path, doc)
    for species in payload["species"]:
        sp = next(c for c in species["calculations"] if c["type"] == "sp")
        assert sp["level_of_theory"]["method"] == "ccsd(t)-f12"
    # The TS record is 2 heavy atoms + H (C, H, H -> 1 heavy atom here).
    ts_sp = next(c for c in payload["transition_state"]["calculations"] if c["type"] == "sp")
    assert ts_sp["level_of_theory"]["method"] == "ccsd(t)-f12"
    assert not [c for c in _codes(outcome) if "adaptive" in c]


def _scan_record():
    from test_adapter import TestScanCalculations
    case = TestScanCalculations("test_scan_calc_emitted_in_additional_calculations")
    return case._record_with_scan(), case._doc()


def _scan_levels(payload):
    return [c["level_of_theory"]["method"]
            for c in payload["conformers"][0]["additional_calculations"] if c["type"] == "scan"]


def test_ess_scan_takes_the_scan_level_and_directed_scan_the_directed_one(tmp_path):
    ranges = [((1, "inf"), {"scan": {"method": "b3lyp", "basis": "6-31g", "software": "gaussian"},
                            "directed_scan": {"method": "pm7", "basis": "sto-3g", "software": "gaussian"}})]
    record, doc = _scan_record()
    for scan_type, expected in (("ess", "b3lyp"), ("brute_force_opt", "pm7"), (None, "b3lyp")):
        _restart(tmp_path, ranges, species=("ethanol",))
        restart = yaml.safe_load(Path(tmp_path, "restart.yml").read_text())
        restart["species"][0]["rotors_dict"] = {0: {"directed_scan_type": scan_type}}
        Path(tmp_path, "restart.yml").write_text(yaml.safe_dump(restart))
        _, payload = _submit_record(tmp_path, record, doc)
        assert _scan_levels(payload) == [expected], scan_type


def test_a_named_directed_scan_alone_leaves_ess_scans_at_the_run_scan_level(tmp_path):
    _restart(tmp_path, [((1, "inf"), {"directed_scan": {"method": "pm7", "basis": "sto-3g",
                                                        "software": "gaussian"}})])
    restart = yaml.safe_load(Path(tmp_path, "restart.yml").read_text())
    restart["species"][0]["rotors_dict"] = {0: {"directed_scan_type": "ess"}}
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump(restart))
    record, doc = _scan_record()
    _, payload = _submit_record(tmp_path, record, doc)
    assert _scan_levels(payload) == ["wb97xd"]  # the run's scan_level


@pytest.mark.parametrize("named", ["scan", "directed_scan"])
def test_scan_or_directed_scan_named_without_restart_species_omits_scans(tmp_path, named):
    # No way to tell which scans ran as which job type: any spec naming either
    # leaves the exported scans without a stated level (fallback omit path).
    Path(tmp_path, "input.yml").write_text(yaml.safe_dump({"adaptive_levels": [
        {"atom_range": [1, "inf"], "levels": {named: "b3lyp/6-31g"}}]}))
    record, doc = _scan_record()
    outcome, payload = _submit_record(tmp_path, record, doc)
    assert _scan_levels(payload) == []
    assert "scan_level_adaptive_not_attributable" in _codes(outcome)


# ---------------------------------------------------------------------------
# IRC level from restart.yml
# ---------------------------------------------------------------------------

IRC_LEVEL = {"method": "b3lyp", "basis": "6-31g(d)", "software": "gaussian",
             "repr": "b3lyp/6-31g(d)", "compatible_ess": ["gaussian"]}


def _irc_calc(payload):
    return next(c for c in payload["additional_calculations"] if c["type"] == "irc")


def test_irc_level_recorded_in_restart_yml_is_used_exactly(tmp_path):
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"irc_level": IRC_LEVEL}))
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=None))
    irc = _irc_calc(payload)
    assert (irc["level_of_theory"]["method"], irc["level_of_theory"]["basis"]) == ("b3lyp", "6-31g(d)")
    assert irc["software_release"]["name"] == "gaussian"
    assert "irc_level_assumed_opt_level" not in _codes(outcome)
    assert "repr" not in json.dumps(payload)


@pytest.mark.parametrize("method,basis,software", [
    ("wb97xd", "def2tzvp", "orca"),          # the level dict's own software is not what ran
    ("b3lyp", "6-31g(d)", "qchem"),
    ("dlpno-ccsd(t)", "def2-tzvp", "orca"),  # DLPNO would pick orca, but IRC forces Gaussian
    ("b3lyp", "6-31g(d)", None),
])
def test_irc_program_is_gaussian_whatever_the_level_dict_says(tmp_path, method, basis, software):
    # arc/level.py:413-418: deduce_software(job_type='irc') forces Gaussian.
    doc = _ts_doc(irc=None)
    doc["transition_states"][0]["ess_software"] = {"opt": "orca"}
    doc["transition_states"][0]["ess_versions"] = {"opt": "ORCA 5.0.4"}
    level = {**IRC_LEVEL, "method": method, "basis": basis}
    level.pop("software")
    if software:
        level["software"] = software
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"irc_level": level}))
    _, payload = _standalone_payload(tmp_path, doc)
    assert _irc_calc(payload)["software_release"] == {"name": "gaussian"}  # no version observed


@pytest.mark.parametrize("method", ["uma", "gfn2-xtb", "torchani-ani2x"])
def test_irc_is_not_filed_where_arcs_rule_names_no_gaussian(tmp_path, method):
    level = {**IRC_LEVEL, "method": method, "basis": None}
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"irc_level": level}))
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=True))
    assert "irc" not in [c["type"] for c in payload["additional_calculations"]]
    assert "irc_software_not_stated" in _codes(outcome)
    assert "validation_evidence" not in payload


def test_irc_level_reaction_route_and_zero_reference(tmp_path):
    # The IRC now sits at a level other than opt_level: the TS reference energy
    # must not come from the opt-level energy.
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"irc_level": IRC_LEVEL}))
    doc = _ts_doc(irc=None)
    outcome, payload = _reaction_payload(tmp_path, doc)
    irc = next(c for c in payload["transition_state"]["calculations"] if c["type"] == "irc")
    assert irc["level_of_theory"]["method"] == "b3lyp"
    assert "irc_level_assumed_opt_level" not in _codes(outcome)


def test_without_a_recorded_irc_level_the_assumption_is_kept_and_reworded(tmp_path):
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=None))
    irc = _irc_calc(payload)
    assert irc["level_of_theory"]["method"] == "wb97xd"  # the opt level
    [warning] = [w for w in outcome.warnings if w["code"] == "irc_level_assumed_opt_level"]
    assert "settings default" in warning["message"]
    assert "cannot read" in warning["message"]
    assert "assuming the two are equal" in warning["message"]


def test_irc_named_by_the_adaptive_levels_is_attributed_over_irc_level(tmp_path):
    _restart(tmp_path, [((1, "inf"), {"irc": {"method": "pm6", "basis": "sto-3g", "software": "gaussian"}})],
             species=("TS0",), irc_level=IRC_LEVEL)
    outcome, payload = _standalone_payload(tmp_path, _ts_doc(irc=None))
    assert _irc_calc(payload)["level_of_theory"]["method"] == "pm6"
    assert "irc_level_assumed_opt_level" not in _codes(outcome)


# ---------------------------------------------------------------------------
# Screened conformers at the restart.yml conformer level
# ---------------------------------------------------------------------------

CONFORMER_LEVEL = {"method": "wb97xd", "basis": "def2svp", "software": "gaussian",
                   "repr": "wb97xd/def2svp", "compatible_ess": ["gaussian"]}


def _benzene_submit(tmp_path, **restart):
    from test_provenance_passthrough import _benzene
    doc, record = _benzene(tmp_path)
    if restart is not None:
        Path(tmp_path, "restart.yml").write_text(yaml.safe_dump(restart))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species")\
            .submit_computed_species_from_output(output_doc=doc, species_record=record)
    return outcome, _built(outcome)


def test_screened_conformers_are_filed_at_the_restart_conformer_level(tmp_path):
    outcome, payload = _benzene_submit(
        tmp_path, conformer_opt_level=CONFORMER_LEVEL, job_types={"conf_opt": True})
    selected, *alts = payload["conformers"]
    assert [c["key"] for c in alts] == ["alt0"]
    alt = alts[0]["primary_calculation"]
    assert (alt["level_of_theory"]["method"], alt["level_of_theory"]["basis"]) == ("wb97xd", "def2svp")
    assert alt["software_release"]["name"] == "gaussian"
    # Neither the opt level nor the selected conformer's banner.
    assert alt["level_of_theory"] != selected["primary_calculation"]["level_of_theory"]
    assert "version" not in alt["software_release"]
    assert alt["parameters_json"]["tckdb_origin"]["origin_detail"] == "screened_conformer"
    assert "opt_result" not in alt
    assert "conformer_level_not_stated" not in _codes(outcome)


@pytest.mark.parametrize("restart", [
    pytest.param({}, id="no_conformer_level"),
    pytest.param({"conformer_opt_level": CONFORMER_LEVEL, "job_types": {"conf_opt": False}},
                 id="conf_opt_did_not_run"),
    pytest.param({"conformer_opt_level": CONFORMER_LEVEL, "job_types": {}}, id="job_types_silent"),
    pytest.param({"conformer_opt_level": {"method": "wb97xd", "basis": "def2svp"},
                  "job_types": {"conf_opt": True}}, id="level_without_program"),
])
def test_screened_conformers_stay_omitted_without_a_usable_conformer_level(tmp_path, restart):
    outcome, payload = _benzene_submit(tmp_path, **restart)
    assert [c["key"] for c in payload["conformers"]] == ["conf0"]
    assert "conformer_level_not_stated" in _codes(outcome)


def test_screened_conformers_omitted_when_the_spec_names_conf_opt_for_the_range(tmp_path):
    named = [{"atom_range": [1, "inf"], "levels": {"conf_opt": {"method": "pm7", "basis": "x",
                                                               "software": "gaussian"}}}]
    outcome, payload = _benzene_submit(
        tmp_path, conformer_opt_level=CONFORMER_LEVEL, job_types={"conf_opt": True},
        adaptive_levels=named, species=[{"label": "benzene"}])
    assert [c["key"] for c in payload["conformers"]] == ["conf0"]
    assert "conformer_level_not_stated" in _codes(outcome)


def test_screened_conformers_kept_when_conf_opt_is_named_only_for_another_range(tmp_path):
    ranges = [
        {"atom_range": [1, 2], "levels": {"conf_opt": {"method": "pm7", "basis": "x", "software": "gaussian"}}},
        {"atom_range": [3, "inf"], "levels": {"sp": {"method": "b3lyp", "basis": "x", "software": "gaussian"}}},
    ]
    outcome, payload = _benzene_submit(
        tmp_path, conformer_opt_level=CONFORMER_LEVEL, job_types={"conf_opt": True},
        adaptive_levels=ranges, species=[{"label": "benzene"}])
    # benzene has 6 heavy atoms: range 2, which does not name conf_opt.
    assert [c["key"] for c in payload["conformers"]] == ["conf0", "alt0"]


def test_irc_zero_reference_stays_at_the_irc_level():
    from tckdb_arc.adapter import _resolve_irc_zero_energy_reference
    doc = {"opt_level": {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}}
    ts = {"label": "TS0", "opt_final_energy_hartree": -154.1}
    same = {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}
    other = {"method": "b3lyp", "basis": "6-31g(d)", "software": "gaussian"}
    # Assumed opt-level IRC, or an IRC known to be at the opt level: the TS opt
    # energy is at the IRC's level.
    assert _resolve_irc_zero_energy_reference(output_doc=doc, ts_record=ts) == -154.1
    assert _resolve_irc_zero_energy_reference(output_doc=doc, ts_record=ts, irc_level=same) == -154.1
    # An IRC at another level: the opt-level energy would mix two levels.
    assert _resolve_irc_zero_energy_reference(output_doc=doc, ts_record=ts, irc_level=other) is None
    # ...unless the TS single point is at the IRC's level.
    doc["sp_level"] = dict(other)
    ts["sp_energy_hartree"] = -155.5
    assert _resolve_irc_zero_energy_reference(output_doc=doc, ts_record=ts, irc_level=other) == -155.5


def test_attribute_level_matches_job_types_exactly_and_returns_output_yml_shaped_levels(tmp_path):
    _restart(tmp_path, [((1, "inf"), {"SP": SMALL, "opt, freq": OPT_LARGE})], species=("x",))
    restart = read_restart_info(tmp_path)
    assert restart.attribute_level("x", "sp", 3) == RUN_LEVEL       # 'SP' is not 'sp'
    assert restart.attribute_level("x", "SP", 3)["method"] == "ccsd(t)-f12"
    freq = restart.attribute_level("x", "freq", 3)                   # comma-separated key
    assert freq == {**OPT_LARGE, "method_type": "dft"}               # repr, compatible_ess dropped


# --- conformers: null energy means the geometry was never optimized ---------

def test_a_screened_conformer_with_a_null_energy_is_not_filed(tmp_path):
    from test_provenance_passthrough import _benzene
    doc, record = _benzene(tmp_path)
    assert len(record["conformer_energies"]) == len(record["conformers"])
    record["conformer_energies"] = [None] * len(record["conformers"])
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump(
        {"conformer_opt_level": CONFORMER_LEVEL, "job_types": {"conf_opt": True}}))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = _built(outcome)
    assert [c["key"] for c in payload["conformers"]] == ["conf0"]
    assert "conformer_level_not_stated" in _codes(outcome)


def test_only_the_conformers_whose_conf_opt_finished_are_filed(tmp_path):
    from test_provenance_passthrough import _benzene
    doc, record = _benzene(tmp_path)
    extra = "\n".join(l for l in record["conformers"][0].splitlines())  # a duplicate of conf 0
    shifted = "\n".join(f"{l.split()[0]} {float(l.split()[1]) + 0.1} {' '.join(l.split()[2:])}"
                        for l in extra.splitlines())
    selected = record["xyz"]
    record["conformers"] = [selected, shifted, extra.replace("0", "1", 1)]
    record["conformer_energies"] = [0.0, None, 3.0]
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump(
        {"conformer_opt_level": CONFORMER_LEVEL, "job_types": {"conf_opt": True}}))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcome = _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
            output_doc=doc, species_record=record)
    payload = _built(outcome)
    filed = [c["geometry"]["xyz_text"] for c in payload["conformers"][1:]]
    assert len(filed) == 1 and shifted.splitlines()[0] not in filed[0].splitlines()[2:3]
    warning = next(w for w in outcome.warnings if w["code"] == "conformer_level_not_stated")
    assert warning["context"]["omitted_count"] == "1"


def test_misaligned_or_missing_conformer_energies_leave_the_conformers_unverified(tmp_path):
    for energies in (None, [0.0, 1.0]):  # the fixture lists one screened conformer
        doc, record = _benzene_copy(tmp_path, energies)
        Path(tmp_path, "restart.yml").write_text(yaml.safe_dump(
            {"conformer_opt_level": CONFORMER_LEVEL, "job_types": {"conf_opt": True}}))
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = _adapter(tmp_path, mode="computed_species").submit_computed_species_from_output(
                output_doc=doc, species_record=record)
        assert [c["key"] for c in _built(outcome)["conformers"]] == ["conf0"], energies


def _benzene_copy(tmp_path, energies):
    from test_provenance_passthrough import FIXTURE
    import shutil
    (tmp_path / "output").mkdir(exist_ok=True)
    shutil.copyfile(FIXTURE / "parser_evidence.json", tmp_path / "output" / "parser_evidence.json")
    doc = yaml.safe_load((FIXTURE / "output.yml").read_text())
    [record] = doc["species"]
    if energies is None:
        record.pop("conformer_energies", None)
    else:
        record["conformer_energies"] = energies
    return doc, record


# --- level matching for the IRC reference ---------------------------------

def test_level_match_treats_dispersion_and_solvation_differences_as_mismatch():
    from tckdb_arc.adapter import _level_keys_match
    base = {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}
    assert _level_keys_match(base, {**base, "software": "orca"})  # software still ignored
    assert not _level_keys_match(base, {**base, "solvation_method": "smd"})
    assert not _level_keys_match({**base, "dispersion": "gd3bj"}, base)
    assert not _level_keys_match({**base, "dispersion": "gd3bj"}, {**base, "dispersion": "gd3"})
    assert _level_keys_match({**base, "dispersion": "GD3BJ"}, {**base, "dispersion": "gd3bj"})


def test_an_smd_single_point_is_not_the_reference_of_a_gas_phase_irc():
    from tckdb_arc.adapter import _resolve_irc_zero_energy_reference
    gas = {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}
    smd = {**gas, "solvation_method": "smd", "solvent": "water"}
    doc = {"opt_level": gas, "sp_level": smd}
    ts = {"label": "TS0", "sp_energy_hartree": -155.5, "opt_final_energy_hartree": -154.1}
    assert _resolve_irc_zero_energy_reference(output_doc=doc, ts_record=ts, irc_level=gas) == -154.1
    assert _resolve_irc_zero_energy_reference(output_doc=doc, ts_record=ts, irc_level=smd) == -155.5
    assert _resolve_irc_zero_energy_reference(
        output_doc={"opt_level": smd, "sp_level": smd}, ts_record=ts, irc_level=gas) is None


def test_irc_never_borrows_the_opt_banner_version_even_for_the_same_program(tmp_path):
    # No banner is observed for the IRC job; the opt's Gaussian banner is not its.
    doc = _ts_doc(irc=None)
    doc["transition_states"][0]["ess_software"] = {"opt": "gaussian"}
    doc["transition_states"][0]["ess_versions"] = {"opt": "Gaussian 16, Revision A.03"}
    Path(tmp_path, "restart.yml").write_text(yaml.safe_dump({"irc_level": IRC_LEVEL}))
    _, payload = _standalone_payload(tmp_path, doc)
    assert _irc_calc(payload)["software_release"] == {"name": "gaussian"}
