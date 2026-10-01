"""Output.yml schema 1.3, batch E: levels, programs, conformers, IRC endpoints, isotopes, routes.

Schema 1.3 (ARC PR #1059) states the level of each job whose log a record exports
(``levels``), the header levels of the other job types, the conformer provenance,
the IRC endpoint species, the composite job, the isotopes beside every geometry and
the observed keyword lines. These tests pin what the adapter now reads from them,
on the species, reaction and TS routes, with the pre-1.3 behaviour as the fallback
(the existing suite covers it). The conftest hook validates every payload built
here against the published TCKDB contract.

Fixtures: ``fixtures/arc_1_3_levels`` (written by ARC's real writer, see its
``generate_fixture.py``) and ``fixtures/arc_1_3_samples`` (the ARC agent's three
sample documents).
"""

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from _contract import contract_validate
from test_adapter import _reaction_output_doc
from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig
from tckdb_arc.evidence import EvidenceStore
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

FIXTURES = Path(__file__).parent / "fixtures"
E_H_KJ_MOL = 2625.499638


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _adapter(tmp_path, *, upload_artifacts=False, kinds=("output_log", "input")):
    from tckdb_arc.config import TCKDBArtifactConfig as ArtifactConfig
    kwargs = {}
    if upload_artifacts:
        kwargs["artifacts"] = ArtifactConfig(upload=True, kinds=tuple(kinds))
    cfg = TCKDBConfig(
        enabled=True, upload=False, base_url="http://localhost", payload_dir=str(tmp_path / "payloads"),
        api_key_env="X_TCKDB_API_KEY", **kwargs)
    return TCKDBAdapter(cfg, project_directory=str(tmp_path))


def _project(tmp_path, name="arc_1_3_levels", *, with_sidecar=True, keep_routes=False):
    """Lay a fixture out as an ARC project (``output/output.yml``, ``calcs/...``) and load it."""
    source = FIXTURES / name
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.name == "calcs":
            shutil.copytree(path, tmp_path / "calcs", dirs_exist_ok=True)
        elif path.name == "parser_evidence.json":
            if with_sidecar:
                shutil.copy(path, tmp_path / "output" / path.name)
        elif path.suffix in {".yml", ".yaml"}:
            shutil.copy(path, tmp_path / "output" / "output.yml")
    doc = yaml.safe_load((tmp_path / "output" / "output.yml").read_text())
    if not keep_routes:
        # The staged logs are other species' logs, so their routes do not match the recorded
        # levels (and the adapter would, rightly, refuse that contradiction).
        for record in (*doc["species"], *doc["transition_states"]):
            for key in ("opt_route", "freq_route", "sp_route"):
                record[key] = None
            if "irc_log_routes" in record:
                record["irc_log_routes"] = [None] * len(record["irc_log_routes"])
    return doc


def _sample(name):
    return yaml.safe_load((FIXTURES / "arc_1_3_samples" / f"{name}.output.yml").read_text())


def _record(doc, label):
    for record in (*doc["species"], *doc["transition_states"]):
        if record["label"] == label:
            return record
    raise KeyError(label)


def _species_payload(adapter, doc, label, warnings=None):
    doc = adapter._with_adaptive_levels(doc, [])
    return adapter._build_computed_species_payload(
        output_doc=doc, species_record=_record(doc, label), conformer_key="c0",
        warnings=warnings if warnings is not None else [])


def _calcs(payload):
    block = payload["conformers"][0]
    return {c["key"]: c for c in [block["primary_calculation"], *block["additional_calculations"]]}


def _codes(warnings):
    return [w["code"] for w in warnings]


def _reaction_payload(adapter, doc, warnings=None):
    doc = adapter._with_adaptive_levels(doc, [])
    return adapter._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0],
        warnings=warnings if warnings is not None else [])


def _ts_calcs(payload):
    ts = payload["transition_state"]
    return {c["key"]: c for c in [ts["calculation"], *ts["calculations"]]}


def _standalone(adapter, doc):
    doc = adapter._with_adaptive_levels(doc, [])
    return adapter._compose_transition_state_request(
        output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0])


def _lot(level):
    return {k: v for k, v in level.items() if k in {"method", "basis"}}


# A hand-built 1.3 reaction document: levels on every record, programs in ess_software.
LVL = {"method": "wb97xd", "basis": "def2tzvp"}
SP_LVL = {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}


def _doc13(*, irc=True, adaptive=False):
    doc = copy.deepcopy(_reaction_output_doc(with_irc=irc))
    doc["schema_version"] = "1.3"
    doc.pop("opt_level", None)
    doc.pop("scan_level", None)
    doc.update({
        "opt_level": {**LVL, "software": "gaussian"},
        "freq_level": {**LVL, "software": "gaussian"},
        "sp_level": {**LVL, "software": "gaussian"},
        "scan_level": None, "irc_level": None, "conformer_opt_level": None, "conformer_sp_level": None,
        "ts_guess_level": None, "gsm_level": None, "composite_method": None,
        "adaptive_levels": ([{"atom_range": [1, "inf"], "levels": {"opt freq": LVL}}] if adaptive else None),
    })
    for record in (*doc["species"], *doc["transition_states"]):
        record.update({
            "levels": {"opt": LVL, "freq": LVL, "sp": SP_LVL, "composite": None, "irc": None},
            "opt_log": f"calcs/{record['label']}/opt.log", "freq_log": f"calcs/{record['label']}/freq.log",
            "sp_log": f"calcs/{record['label']}/sp.log", "composite_log": None,
            "ess_software": {"opt": "gaussian", "freq": "gaussian", "sp": "orca"},
            "ess_versions": {"opt": "Gaussian 16, Revision A.03", "freq": "Gaussian 16, Revision A.03",
                             "sp": "ORCA 6.0.0"},
            "xyz_isotopes": None, "conformers_isotopes": None,
        })
        if not record["is_ts"]:
            record.update({"irc_endpoint_of": None, "irc_endpoint_direction": None})
    # ARC states the participants per occurrence (G's rule: a non-balanced reaction with only sorted
    # label lists is refused), so these hand-built reactions state them.
    doc["reactions"][0]["atom_map_reactant_labels"] = list(doc["reactions"][0]["reactant_labels"])
    doc["reactions"][0]["atom_map_product_labels"] = list(doc["reactions"][0]["product_labels"])
    ts = doc["transition_states"][0]
    ts["levels"]["irc"] = LVL
    ts["irc_log_levels"] = [LVL, LVL] if irc else []
    ts["irc_log_routes"] = [None, None] if irc else []
    ts["ess_software"]["irc"] = "gaussian"
    ts["ess_versions"]["irc"] = "Gaussian 16, Revision A.03"
    return doc


# ---------------------------------------------------------------------------
# 1. Per-record levels
# ---------------------------------------------------------------------------

def test_levels_are_per_record_and_beat_the_header_under_adaptive_levels(tmp_path):
    doc = _project(tmp_path)
    adapter = _adapter(tmp_path)
    small = _calcs(_species_payload(adapter, doc, "iC3H7"))
    big = _calcs(_species_payload(adapter, doc, "nC3H7"))
    assert doc["opt_level"]["method"] == "uhf"          # the run-level default is not what ran
    assert small["opt"]["level_of_theory"] == {"method": "b3lyp", "basis": "6-31g"}
    assert big["opt"]["level_of_theory"] == {"method": "wb97xd", "basis": "def2tzvp"}
    assert big["sp"]["level_of_theory"] == {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}
    assert big["freq"]["level_of_theory"] == big["opt"]["level_of_theory"]


def test_program_is_the_observed_one_not_a_level_deduction(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record["ess_software"]["sp"] = "molpro"
    record["ess_versions"]["sp"] = "Molpro 2022.3"
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, record["label"]))
    assert calcs["opt"]["software_release"]["name"] == "gaussian"
    assert calcs["sp"]["software_release"] == {"name": "molpro", "version": "2022.3"}


def test_a_job_with_no_observed_program_is_not_given_a_deduced_one(tmp_path):
    doc = _doc13()
    del doc["species"][0]["ess_software"]["sp"]
    # the levels inside a record state no software, and the header opt_level's is a deduction
    warnings = []
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, doc["species"][0]["label"], warnings))
    assert "sp" not in calcs and "opt" in calcs


def test_restart_yml_does_not_override_a_record_that_states_its_levels(tmp_path):
    doc = _doc13(adaptive=True)
    (tmp_path / "restart.yml").write_text(yaml.safe_dump({
        "adaptive_levels": [{"atom_range": [1, "inf"], "levels": {"opt freq": {
            "method": "b3lyp", "basis": "6-31g", "software": "gaussian"}}}],
        "species": [{"label": doc["species"][0]["label"]}]}))
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, doc["species"][0]["label"]))
    assert calcs["opt"]["level_of_theory"] == LVL


def test_unrecorded_level_with_a_log_falls_back_to_the_header_only_for_a_plain_run(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record["levels"] = {k: None for k in record["levels"]}     # a restart that predates level recording
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, record["label"]))
    assert calcs["opt"]["level_of_theory"] == LVL                # header opt_level (pre-1.3 rule)
    adaptive = _doc13(adaptive=True)
    adaptive["species"][0]["levels"] = {k: None for k in record["levels"]}
    with pytest.raises(ValueError, match="adaptive_levels"):
        _species_payload(_adapter(tmp_path), adaptive, adaptive["species"][0]["label"])


def test_a_record_with_no_opt_job_gets_a_marked_placeholder_opt_at_the_header_level(tmp_path):
    doc = _sample("species_thermo")                       # CH3NH2: only a Molpro sp is exported
    warnings = []
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, "CH3NH2", warnings))
    opt = calcs["opt"]
    assert opt["level_of_theory"] == {"method": "uhf", "basis": "3-21g"}          # header opt_level
    origin = opt["parameters_json"]["tckdb_origin"]
    assert origin["origin_detail"] == "placeholder_primary_opt_no_opt_job"
    assert "opt_result" not in opt or set(opt["opt_result"]) <= {"converged"}      # nothing invented
    assert calcs["sp"]["level_of_theory"] == {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}
    assert calcs["sp"]["software_release"]["name"] == "molpro"
    assert "primary_opt_placeholder_no_opt_job" in _codes(warnings)
    # the conformer route files the same placeholder
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_payload(
        output_doc=marked, species_record=_record(marked, "CH3NH2"), warnings=warnings)
    assert payload["calculation"]["parameters_json"]["tckdb_origin"]["origin_detail"].endswith("no_opt_job")
    assert "primary_opt_placeholder_no_opt_job" in _codes(warnings)


def test_ordinary_records_have_no_placeholder_marker(tmp_path):
    warnings = []
    opt = _calcs(_species_payload(_adapter(tmp_path), _project(tmp_path), "iC3H7", warnings))["opt"]
    assert "parameters_json" not in opt or "tckdb_origin" not in opt["parameters_json"]
    assert not {"primary_opt_placeholder_no_opt_job", "composite_geometry_level_not_stated"} & set(_codes(warnings))


def test_no_opt_job_placeholder_on_the_reaction_route(tmp_path):
    doc = _doc13()
    species = doc["reactions"][0]["reactant_labels"][0]
    record = next(r for r in doc["species"] if r["label"] == species)
    record.update(opt_log=None, composite_log=None)
    record["levels"]["opt"] = None
    warnings = []
    payload = _reaction_payload(_adapter(tmp_path), doc, warnings)
    marked = [c for b in payload["species"] for c in [b["conformers"][0]["calculation"], *b["calculations"]]
              if (c.get("parameters_json") or {}).get("tckdb_origin", {}).get("origin_detail", "").endswith("no_opt_job")]
    assert len(marked) == 1 and marked[0]["level_of_theory"] == {"method": "wb97xd", "basis": "def2tzvp"}
    assert "primary_opt_placeholder_no_opt_job" in _codes(warnings)


def test_levels_on_the_ts_route(tmp_path):
    doc = _doc13()
    doc["transition_states"][0]["levels"]["opt"] = {"method": "b3lyp", "basis": "6-31g"}
    calcs = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))
    assert calcs["ts_opt"]["level_of_theory"] == {"method": "b3lyp", "basis": "6-31g"}
    assert calcs["ts_freq"]["level_of_theory"] == LVL
    assert calcs["ts_sp"]["level_of_theory"] == SP_LVL
    standalone = _standalone(_adapter(tmp_path), doc)
    assert standalone["primary_opt"]["level_of_theory"] == {"method": "b3lyp", "basis": "6-31g"}


def test_sp_energy_read_from_the_opt_log_is_marked_reused_and_pairs_with_the_opt_level(tmp_path):
    doc = _project(tmp_path)
    sp = _calcs(_species_payload(_adapter(tmp_path), doc, "iC3H7"))["sp"]
    assert sp["parameters_json"]["tckdb_origin"]["reused_from"] == {"calculation_type": "opt"}
    real = _calcs(_species_payload(_adapter(tmp_path), doc, "nC3H7"))["sp"]
    assert "tckdb_origin" not in (real.get("parameters_json") or {})


def test_thermo_energy_level_is_the_records_own_under_adaptive_levels(tmp_path):
    doc = _doc13(adaptive=True)
    record = doc["species"][0]
    record["thermo"] = {"h298_kj_mol": -100.0, "s298_j_mol_k": 200.0, "standard_state_pressure_pa": 101325.0,
                        "atom_corrections_applied": True, "bond_corrections_applied": True,
                        "atom_corrections_level": SP_LVL}
    payload = _species_payload(_adapter(tmp_path), doc, record["label"])
    # the record's sp level (not the header sp_level) is the level the enthalpy check compares with
    assert payload["thermo"]["energy_level_of_theory"] == {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}
    assert payload["thermo"]["h298_kj_mol"] == -100.0


# ---- IRC level -----------------------------------------------------------------

def test_irc_uses_the_recorded_level_and_the_observed_program(tmp_path):
    doc = _doc13()
    doc["transition_states"][0]["levels"]["irc"] = {"method": "b3lyp", "basis": "6-31g"}
    doc["transition_states"][0]["ess_software"]["irc"] = "gaussian"
    warnings = []
    payload = _reaction_payload(_adapter(tmp_path), doc, warnings)
    irc = _ts_calcs(payload)["ts_irc"]
    assert irc["level_of_theory"] == {"method": "b3lyp", "basis": "6-31g"}
    assert irc["software_release"] == {"name": "gaussian", "version": "16", "revision": "A.03"}
    assert "irc_level_assumed_opt_level" not in _codes(warnings)
    assert _standalone(_adapter(tmp_path), doc)["additional_calculations"][-1]["level_of_theory"] == \
        {"method": "b3lyp", "basis": "6-31g"}


def test_irc_level_from_per_job_levels_when_levels_irc_is_null_but_they_agree(tmp_path):
    doc = _doc13()
    ts = doc["transition_states"][0]
    ts["levels"]["irc"] = None
    ts["irc_log_levels"] = [{"method": "b3lyp", "basis": "6-31g"}] * 2
    irc = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))["ts_irc"]
    assert irc["level_of_theory"] == {"method": "b3lyp", "basis": "6-31g"}


def test_irc_jobs_at_different_levels_file_no_single_irc_calculation(tmp_path):
    doc = _doc13()
    ts = doc["transition_states"][0]
    ts["levels"]["irc"] = None
    ts["irc_log_levels"] = [{"method": "b3lyp", "basis": "6-31g"}, LVL]
    warnings = []
    calcs = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc, warnings))
    assert "ts_irc" not in calcs
    assert "irc_level_not_stated" in _codes(warnings)
    assert "irc_level_assumed_opt_level" not in _codes(warnings)


def test_irc_falls_back_to_the_header_irc_level_then_to_nothing_never_to_opt(tmp_path):
    doc = _doc13()
    ts = doc["transition_states"][0]
    ts["levels"]["irc"] = None
    ts["irc_log_levels"] = [None, None]
    doc["irc_level"] = {"method": "b3lyp", "basis": "6-31g", "software": "gaussian"}
    assert _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))["ts_irc"]["level_of_theory"] == \
        {"method": "b3lyp", "basis": "6-31g"}
    doc["irc_level"] = None
    warnings = []
    assert "ts_irc" not in _ts_calcs(_reaction_payload(_adapter(tmp_path), doc, warnings))
    assert "irc_level_not_stated" in _codes(warnings)


def test_irc_without_an_observed_program_is_not_filed(tmp_path):
    doc = _doc13()
    del doc["transition_states"][0]["ess_software"]["irc"]
    warnings = []
    assert "ts_irc" not in _ts_calcs(_reaction_payload(_adapter(tmp_path), doc, warnings))
    assert "irc_software_not_stated" in _codes(warnings)


def test_pre_1_3_irc_still_assumes_the_opt_level_with_its_warning(tmp_path):
    doc = _reaction_output_doc(with_irc=True)
    warnings = []
    calcs = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc, warnings))
    assert calcs["ts_irc"]["level_of_theory"]["method"] == doc["opt_level"]["method"]
    assert "irc_level_assumed_opt_level" in _codes(warnings)


# ---- scans -----------------------------------------------------------------------

def _scan(**kwargs):
    return {"key": "scan_rotor_0", "ess_software": "gaussian", "ess_version": "Gaussian 09, Revision D.01",
            "source_log": "calcs/scan.log",
            "result": {"dimension": 1, "relaxed": True,
                       "coordinate": {"coordinate_type": "dihedral", "atom_indices": [0, 1, 0, 1],
                                      "index_base": 0, "unit": "degree"},
                       "samples": [{"source_index": 0, "angle_degrees": 0.0, "relative_energy_kj_mol": 0.0},
                                   {"source_index": 1, "angle_degrees": 60.0, "relative_energy_kj_mol": 1.0}]},
            **kwargs}


def _scan_doc():
    doc = _doc13()
    doc["scan_level"] = {"method": "b3lyp", "basis": "6-31g", "software": "orca"}   # deduced: not trusted
    scan = _scan()
    scan["result"]["coordinate"]["atom_indices"] = [0, 1, 2, 3]
    doc["species"][0]["xyz"] = "C 0 0 0\nC 1 0 0\nC 2 0 0\nC 3 0 0"
    doc["species"][0]["rotor_scans"] = [scan]
    return doc


def test_scan_program_is_the_scans_own_and_its_level_the_header_scan_level(tmp_path):
    doc = _scan_doc()
    calc = _calcs(_species_payload(_adapter(tmp_path), doc, doc["species"][0]["label"]))["scan_rotor_0"]
    assert calc["level_of_theory"] == {"method": "b3lyp", "basis": "6-31g"}
    assert calc["software_release"] == {"name": "gaussian", "version": "09", "revision": "D.01"}


def test_scan_without_a_stated_program_is_not_filed_at_the_deduced_one(tmp_path):
    doc = _scan_doc()
    doc["species"][0]["rotor_scans"][0]["ess_software"] = None
    doc["species"][0]["rotor_scans"][0]["ess_version"] = None
    assert "scan_rotor_0" not in _calcs(_species_payload(_adapter(tmp_path), doc, doc["species"][0]["label"]))


def test_scan_level_null_under_adaptive_scan_is_not_attributable(tmp_path):
    doc = _scan_doc()
    doc["scan_level"] = None
    doc["adaptive_levels"] = [{"atom_range": [1, "inf"], "levels": {"scan": LVL}}]
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    record = marked["species"][0]
    payload = adapter._build_computed_species_payload(
        output_doc=marked, species_record=record, conformer_key="c0", warnings=[])
    assert "scan_rotor_0" not in _calcs(payload)
    warnings = []
    adapter._report_adaptive_omissions(marked, warnings)
    assert "scan_level_adaptive_not_attributable" in _codes(warnings)
    assert "output.yml 1.3 states no scan level" in warnings[0]["message"]


def test_scan_on_the_ts_route_uses_its_own_program(tmp_path):
    doc = _scan_doc()
    doc["transition_states"][0]["xyz"] = doc["species"][0]["xyz"]
    doc["transition_states"][0]["rotor_scans"] = [_scan()]
    doc["transition_states"][0]["rotor_scans"][0]["result"]["coordinate"]["atom_indices"] = [0, 1, 2, 3]
    calc = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))["ts_scan_rotor_0"]
    assert calc["software_release"]["name"] == "gaussian"


def test_frequency_scale_factor_keeps_its_own_level_and_reports_another_freq_level(tmp_path):
    doc = _doc13(adaptive=True)
    doc.update(freq_scale_factor=0.97, freq_scale_factor_key=None, freq_scale_factor_source="CCCBDB")
    record = doc["species"][0]
    warnings = []
    fsf = _species_payload(_adapter(tmp_path), doc, record["label"], warnings)["statmech"]["freq_scale_factor"]
    assert fsf["level_of_theory"] == LVL and fsf["software"] == {"name": "gaussian"}
    assert "freq_scale_factor_fitted_for_other_level" not in _codes(warnings)
    record["levels"]["freq"] = {"method": "b3lyp", "basis": "6-31g"}      # another level than the header
    warnings = []
    fsf = _species_payload(_adapter(tmp_path), doc, record["label"], warnings)["statmech"]["freq_scale_factor"]
    assert fsf["level_of_theory"] == LVL and fsf["value"] == 0.97     # Arkane applied it; it keeps its level
    assert "freq_scale_factor_fitted_for_other_level" in _codes(warnings)


# ---- route vs level --------------------------------------------------------------

def test_a_level_contradicted_by_the_route_the_job_ran_with_is_not_sent(tmp_path):
    doc = _sample("reaction_kinetics")           # IRC levels uhf/3-21g, but the route says ub3lyp/cbsb7
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_computed_reaction_payload(
        output_doc=marked, reaction_record=marked["reactions"][0], warnings=warnings)
    adapter._report_adaptive_omissions(marked, warnings)
    assert "ts_irc" not in _ts_calcs(payload)
    assert "ts_opt" in _ts_calcs(payload)                      # its route (uhf/3-21g) agrees with its level
    assert "level_contradicted_by_route" in _codes(warnings)
    assert "ub3lyp/cbsb7" in next(w for w in warnings if w["code"] == "level_contradicted_by_route")["message"]


def test_consistent_routes_are_sent_and_unparseable_or_composite_routes_are_never_flagged(tmp_path):
    doc = _sample("reaction_kinetics")
    ts = doc["transition_states"][0]
    ts["irc_log_routes"] = ["#P irc=(CalcAll, forward) guess=read ub3lyp/cbsb7"] * 2
    ts["levels"]["irc"] = {"method": "b3lyp", "basis": "cbsb7"}
    assert "ts_irc" in _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))       # u prefix is the spin treatment
    for route in ("#P irc=(forward) a/b c/d", "! wb97xd def2-tzvp", "#CBS-QB3 opt freq", "#P opt"):
        ts["irc_log_routes"] = [route] * 2
        assert "ts_irc" in _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))
    for route, expected in (("#P opt wb97xd/def2tzvp", True), ("#P opt b3lyp/def2tzvp", False),
                            ("#P opt wb97xd/def2svp", False)):
        doc2 = _doc13()
        doc2["species"][0]["opt_route"] = route
        calcs = _calcs(_species_payload(_adapter(tmp_path), doc2, doc2["species"][0]["label"])) \
            if expected else None
        if expected:
            assert "opt" in calcs
        else:
            with pytest.raises(ValueError, match="level_contradicted_by_route"):
                _species_payload(_adapter(tmp_path), doc2, doc2["species"][0]["label"])


# ---------------------------------------------------------------------------
# 2. Header levels: the GSM path search
# ---------------------------------------------------------------------------

def test_gsm_path_search_is_filed_with_gsm_level_and_the_xtb_program(tmp_path):
    doc = _project(tmp_path)
    payload = _reaction_payload(_adapter(tmp_path), doc)
    guess = _ts_calcs(payload)["ts_guess"]
    assert guess["type"] == "path_search"
    assert guess["level_of_theory"] == {"method": "gfn2"}
    assert guess["software_release"] == {"name": "xtb", "version": "6.7.1", "build": "edcfbbe"}
    standalone = _standalone(_adapter(tmp_path), doc)
    assert [c for c in standalone["additional_calculations"] if c["type"] == "path_search"]


def test_gsm_is_not_filed_when_gsm_level_is_null_or_xtb_is_not_identified(tmp_path):
    doc = _project(tmp_path)
    doc["gsm_level"] = None
    doc["adaptive_levels"] = None       # a plain run: the header opt_level is there to be (wrongly) borrowed
    warnings = []
    assert "ts_guess" not in _ts_calcs(_reaction_payload(_adapter(tmp_path), doc, warnings))
    assert "ts_guess_level_not_stated" in _codes(warnings)
    assert "gsm_level is null" in next(w for w in warnings if w["code"] == "ts_guess_level_not_stated")["message"]
    doc = _project(tmp_path / "again")
    del _record(doc, "TS0")["ess_software"]["gsm"]
    warnings = []
    assert "ts_guess" not in _ts_calcs(_reaction_payload(_adapter(tmp_path / "again"), doc, warnings))
    assert "ts_guess_software_not_stated" in _codes(warnings)


def test_gsm_before_1_3_is_still_not_filed(tmp_path):
    doc = _project(tmp_path)
    del doc["gsm_level"]
    warnings = []
    assert "ts_guess" not in _ts_calcs(_reaction_payload(_adapter(tmp_path), doc, warnings))
    assert "BRIDGE_ROADMAP B3" in next(w for w in warnings if w["code"] == "ts_guess_level_not_stated")["message"]


# ---------------------------------------------------------------------------
# 3. Conformers
# ---------------------------------------------------------------------------

def test_screened_conformers_are_filed_at_their_stated_level_with_an_electronic_energy(tmp_path):
    doc = _project(tmp_path)
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "nC3H7", warnings)
    alts = [c for c in payload["conformers"] if c["key"].startswith("alt")]
    assert len(alts) == 1
    opt = alts[0]["primary_calculation"]
    assert opt["level_of_theory"] == {"method": "wb97xd", "basis": "def2svp"}
    assert opt["software_release"]["name"] == "gaussian"
    assert opt["opt_result"] == {"final_energy_hartree": pytest.approx(-307143.0 / E_H_KJ_MOL, rel=1e-7)}
    assert opt["parameters_json"]["tckdb_origin"]["origin_detail"] == "screened_conformer"
    # the force-field geometry of the third conformer is not an ESS calculation
    assert "conformer_geometry_not_esss_optimized" in _codes(warnings)
    assert "conformer_level_not_stated" not in _codes(warnings)


def test_force_field_conformers_are_omitted_with_a_reason_and_no_energy_is_sent(tmp_path):
    doc = _project(tmp_path)
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "nC3H7_ff", warnings)
    assert [c["key"] for c in payload["conformers"]] == ["c0"]
    message = next(w for w in warnings if w["code"] == "conformer_geometry_not_esss_optimized")["message"]
    assert "MMFF94s (rdkit)" in message
    assert "kcal" not in json.dumps(payload)


def test_conformer_whose_program_the_header_does_not_name_is_not_filed(tmp_path):
    doc = _project(tmp_path)
    doc["conformer_opt_level"] = None                        # e.g. an adaptive conf_opt
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "nC3H7", warnings)
    assert [c["key"] for c in payload["conformers"]] == ["c0"]
    assert "conformer_program_not_stated" in _codes(warnings)
    doc = _project(tmp_path / "other")
    doc["conformer_opt_level"] = {"method": "b3lyp", "basis": "6-31g", "software": "gaussian"}
    warnings = []
    assert len(_species_payload(_adapter(tmp_path / "other"), doc, "nC3H7", warnings)["conformers"]) == 1
    assert "conformer_program_not_stated" in _codes(warnings)


def test_conformer_energy_is_sent_only_with_a_stated_kind_and_level(tmp_path):
    for mutate in (
        lambda r: r.update(conformer_energy_kind=None),
        lambda r: r.update(conformer_energy_kind="force_field_kcal_mol"),
        lambda r: r.update(conformer_energy_level=None),
        lambda r: r.update(conformer_energy_level={"method": "b3lyp", "basis": "6-31g"}),
        lambda r: r.update(conformer_energies=[None, None, None]),
    ):
        base = tmp_path / str(id(mutate))
        doc = _project(base)
        mutate(_record(doc, "nC3H7"))
        alts = [c for c in _species_payload(_adapter(base), doc, "nC3H7")["conformers"]
                if c["key"].startswith("alt")]
        assert alts and "opt_result" not in alts[0]["primary_calculation"]


def test_conformer_single_point_level_gets_its_own_sp_calculation(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "nC3H7")
    sp_level = {"method": "ccsd(t)-f12", "basis": "cc-pvtz-f12"}
    record["conformer_energy_level"] = sp_level
    doc["conformer_sp_level"] = {**sp_level, "software": "molpro"}
    alt = next(c for c in _species_payload(_adapter(tmp_path), doc, "nC3H7")["conformers"]
               if c["key"].startswith("alt"))
    assert "opt_result" not in alt["primary_calculation"]
    (sp,) = alt["additional_calculations"]
    assert sp["type"] == "sp" and sp["level_of_theory"] == sp_level
    assert sp["software_release"]["name"] == "molpro"
    assert sp["sp_result"] == {"electronic_energy_hartree": pytest.approx(-307143.0 / E_H_KJ_MOL, rel=1e-7)}
    assert sp["depends_on"] == [{"parent_calculation_key": "alt0_opt", "role": "single_point_on"}]


def test_pre_1_3_conformers_keep_the_restart_yml_path(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record["xyz"] = "C 0 0 0\nH 1 0 0"
    record["conformers"] = ["C 0 0 0\nH 1.1 0 0"]
    record["conformer_energies"] = [-1.0]
    for key in ("levels", "xyz_isotopes", "conformers_isotopes"):
        del record[key]
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, record["label"], warnings)
    assert [c["key"] for c in payload["conformers"]] == ["c0"]
    assert "conformer_level_not_stated" in _codes(warnings)


# ---------------------------------------------------------------------------
# 4. IRC endpoint species
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["computed_species", "conformer"])
def test_irc_endpoint_species_are_skipped_by_the_output_marker(tmp_path, mode):
    doc = _sample("reaction_kinetics")           # the sample with IRC endpoint species
    adapter = _adapter(tmp_path)
    submit = (adapter.submit_computed_species_from_output if mode == "computed_species"
              else adapter.submit_from_output)
    for label, direction in (("IRC_TS0_1", "forward"), ("IRC_TS0_2", "reverse")):
        record = _record(doc, label)
        assert record["irc_endpoint_of"] == "TS0" and record["irc_endpoint_direction"] == direction
        outcome = submit(output_doc=doc, species_record=record)
        assert outcome.status == "skipped" and outcome.payload_path is None
        (warning,) = outcome.warnings
        assert warning["code"] == "irc_endpoint_species_skipped"
        assert warning["context"]["ts_label"] == "TS0"
        assert direction in warning["message"]
    # ordinary species of the same document are not skipped
    ordinary = submit(output_doc=doc, species_record=_record(doc, "nC3H7"))
    assert ordinary.payload_path is not None and ordinary.payload_path.exists()


def test_a_null_marker_is_authoritative_over_restart_yml(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record["label"] = "IRC_TS0_1"
    assert record["irc_endpoint_of"] is None
    (tmp_path / "restart.yml").write_text(yaml.safe_dump({"species": [
        {"label": "IRC_TS0_1", "is_ts": False, "irc_label": "TS0"},
        {"label": "TS0", "is_ts": True, "irc_label": "IRC_TS0_1"}]}))
    outcome = _adapter(tmp_path).submit_computed_species_from_output(output_doc=doc, species_record=record)
    assert outcome.payload_path is not None and "irc_endpoint_species_skipped" not in _codes(outcome.warnings)


def test_a_marked_endpoint_without_restart_yml_is_skipped(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record.update({"label": "IRC_TS0_2", "irc_endpoint_of": "TS0", "irc_endpoint_direction": None})
    outcome = _adapter(tmp_path).submit_computed_species_from_output(output_doc=doc, species_record=record)
    assert outcome.payload_path is None and _codes(outcome.warnings) == ["irc_endpoint_species_skipped"]


# ---------------------------------------------------------------------------
# 5. The composite-method calculation
# ---------------------------------------------------------------------------

def test_composite_run_files_the_composite_job_at_the_composite_level(tmp_path):
    doc = _project(tmp_path)
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "SO2OO", warnings)
    calcs = _calcs(payload)
    opt = calcs["opt"]
    assert opt["level_of_theory"] == {"method": "cbs-qb3"}        # as ARC states it; no internal level invented
    assert opt["parameters_json"]["tckdb_origin"]["origin_detail"] == "placeholder_primary_opt_composite"
    assert "composite_geometry_level_not_stated" in _codes(warnings)
    assert opt["software_release"] == {"name": "gaussian", "version": "03", "revision": "D.01"}
    assert (opt["parameters"] or [{}])[0]["raw_value"] == "#CBS-QB3 opt freq"       # composite_route
    sp = calcs["sp"]
    assert sp["level_of_theory"] == {"method": "cbs-qb3"}
    assert sp["parameters_json"]["tckdb_origin"]["reused_from"] == {
        "calculation_type": "opt", "source_job": "composite"}
    # the composite job's own frequencies are at a level the composite level does not name
    assert "freq" not in calcs


def test_composite_frequencies_are_not_filed_at_a_header_level_and_energy_level_is_the_records(tmp_path):
    doc = _project(tmp_path)
    doc["adaptive_levels"] = None                 # a plain run: header freq/composite levels are there to borrow
    doc["composite_method"] = {"method": "g4", "software": "gaussian"}
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, "SO2OO"))
    assert "freq" not in calcs
    payload = _species_payload(_adapter(tmp_path), doc, "SO2OO")
    # the declared energy level is the record's composite level, not the header composite_method
    assert payload["statmech"]["energy_level_of_theory"] == {"method": "cbs-qb3"}


def test_composite_role_links_thermo_and_statmech_and_the_energy_level_is_the_composite_one(tmp_path):
    doc = _project(tmp_path)
    payload = _species_payload(_adapter(tmp_path), doc, "SO2OO")
    sources = payload["statmech"]["source_calculations"]
    assert {"calculation_key": "opt", "role": "composite"} in sources
    assert {"calculation_key": "sp", "role": "sp"} in sources
    assert payload["statmech"]["energy_level_of_theory"] == {"method": "cbs-qb3"}


def test_composite_role_links_the_thermo_block_too(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "SO2OO")
    record["thermo"] = {"h298_kj_mol": -300.0, "s298_j_mol_k": 300.0, "standard_state_pressure_pa": 101325.0,
                        "atom_corrections_applied": True, "bond_corrections_applied": True,
                        "atom_corrections_level": {"method": "cbs-qb3"}}
    payload = _species_payload(_adapter(tmp_path), doc, "SO2OO")
    assert {"calculation_key": "opt", "role": "composite"} in payload["thermo"]["source_calculations"]
    assert payload["thermo"]["energy_level_of_theory"] == {"method": "cbs-qb3"}
    assert payload["thermo"]["h298_kj_mol"] == -300.0


def test_composite_log_and_input_are_the_calculations_artifacts(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "SO2OO")
    log = tmp_path / record["composite_log"]
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(" Entering Gaussian System\n")
    deck = log.parent / "input.gjf"
    deck.write_text("#CBS-QB3 opt freq\n")
    record["composite_input"] = str(deck.relative_to(tmp_path))
    adapter = _adapter(tmp_path, upload_artifacts=True)
    kinds = [a["kind"] for a in adapter._inline_artifacts_for_calc(record, calc_role="opt")]
    assert kinds == ["output_log", "input"]
    # the sp reuses the composite energy and has no log of its own
    assert adapter._inline_artifacts_for_calc(record, calc_role="sp") == []


def test_standalone_artifact_sweep_uses_the_composite_log_for_the_opt_calculation():
    from tckdb_arc.sweep import _resolve_artifact_path
    record = {"levels": {"composite": {"method": "cbs-qb3"}}, "composite_log": "calcs/c/output.out",
              "composite_input": "calcs/c/input.gjf", "opt_log": None, "freq_log": None, "sp_log": None}
    for kind, expected in (("output_log", "calcs/c/output.out"), ("input", "calcs/c/input.gjf")):
        assert _resolve_artifact_path(kind=kind, calc_type="opt", species_record=record, output_doc={}) == expected
        assert _resolve_artifact_path(kind=kind, calc_type="sp", species_record=record, output_doc={}) is None
    del record["levels"]            # pre-1.3: nothing to borrow
    assert _resolve_artifact_path(kind="output_log", calc_type="opt", species_record=record, output_doc={}) is None


def test_non_composite_records_have_no_composite_role(tmp_path):
    payload = _species_payload(_adapter(tmp_path), _project(tmp_path), "iC3H7")
    assert "composite" not in {s["role"] for s in payload["statmech"]["source_calculations"]}


# ---------------------------------------------------------------------------
# 6. Isotopes
# ---------------------------------------------------------------------------

def test_substituted_species_sends_the_stated_isotopes_on_every_geometry(tmp_path):
    doc = _project(tmp_path)
    payload = _species_payload(_adapter(tmp_path), doc, "iC3H7_d")
    block = payload["conformers"][0]
    assert block["geometry"]["isotopes"] == {"4": 2}
    calcs = _calcs(payload)
    assert calcs["opt"]["output_geometries"][0]["geometry"]["isotopes"] == {"4": 2}
    assert calcs["opt"]["input_geometries"][0]["isotopes"] == {"4": 2}       # opt_input_xyz_isotopes
    assert calcs["freq"]["input_geometries"][0]["isotopes"] == {"4": 2}


def test_all_standard_isotopes_send_none(tmp_path):
    payload = _species_payload(_adapter(tmp_path), _project(tmp_path), "iC3H7")
    assert "isotopes" not in json.dumps(payload)


def test_unstated_input_isotopes_leave_that_geometry_out_for_a_substituted_species(tmp_path):
    doc = _project(tmp_path)
    _record(doc, "iC3H7_d")["opt_input_xyz_isotopes"] = None
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, "iC3H7_d"))
    assert "input_geometries" not in calcs["opt"]
    ordinary = _project(tmp_path / "ord")
    _record(ordinary, "iC3H7")["opt_input_xyz_isotopes"] = None
    assert "input_geometries" in _calcs(_species_payload(_adapter(tmp_path / "ord"), ordinary, "iC3H7"))["opt"]


def test_isotopes_that_contradict_the_smiles_refuse_the_species(tmp_path):
    doc = _project(tmp_path)
    _record(doc, "iC3H7_d")["smiles"] = "C[CH]C"
    with pytest.raises(ValueError, match="species_geometry_isotope_mismatch"):
        _species_payload(_adapter(tmp_path), doc, "iC3H7_d")
    doc = _project(tmp_path / "b")
    _record(doc, "iC3H7")["smiles"] = "[2H]C[CH]C"          # SMILES says D, geometry says all standard
    with pytest.raises(ValueError, match="species_geometry_isotope_mismatch"):
        _species_payload(_adapter(tmp_path / "b"), doc, "iC3H7")
    doc = _project(tmp_path / "c")
    _record(doc, "iC3H7_d")["xyz_isotopes"] = None
    with pytest.raises(ValueError, match="no usable isotope list"):
        _species_payload(_adapter(tmp_path / "c"), doc, "iC3H7_d")


def test_conformer_isotopes_come_from_conformers_isotopes(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "iC3H7_d")
    record["conformers"] = [record["xyz"].replace("0.0129", "0.0229")]
    record["conformer_energies"] = [-100.0]
    record["conformer_levels"] = [dict(CONF_LEVEL)]
    record["conformer_energy_kind"] = "electronic_kj_mol"
    record["conformer_energy_level"] = dict(CONF_LEVEL)
    record["conformers_isotopes"] = [[12, 12, 12, 2, 1, 1, 1, 1, 1, 1]]
    doc["conformer_opt_level"] = {**CONF_LEVEL, "software": "gaussian"}
    alt = next(c for c in _species_payload(_adapter(tmp_path), doc, "iC3H7_d")["conformers"]
               if c["key"] == "alt0")
    assert alt["geometry"]["isotopes"] == {"4": 2}
    assert alt["primary_calculation"]["output_geometries"][0]["geometry"]["isotopes"] == {"4": 2}
    record["conformers_isotopes"] = [None]
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "iC3H7_d", warnings)
    assert [c["key"] for c in payload["conformers"]] == ["c0"]
    assert "geometry_isotopes_not_stated" in _codes(warnings)


CONF_LEVEL = {"method": "wb97xd", "basis": "def2svp"}


def test_scan_sample_isotopes_are_sent_as_stated(tmp_path):
    doc = _scan_doc()
    record = doc["species"][0]
    record["xyz_isotopes"] = [12, 12, 12, 12]
    record["rotor_scans"][0]["result"]["samples"][0].update(
        geometry_xyz="C 0 0 0\nC 1 0 0\nC 2 0 0\nD 3 0 0", geometry_isotopes=None)
    # an all-standard sample states none
    record["rotor_scans"][0]["result"]["samples"][0]["geometry_xyz"] = "C 0 0 0\nC 1 0 0\nC 2 0 0\nC 3 0 0"
    record["rotor_scans"][0]["result"]["samples"][0]["geometry_isotopes"] = [12, 12, 12, 13]
    record["smiles"] = "[13CH3]CCC"
    record["xyz_isotopes"] = [12, 12, 12, 13]
    point = _calcs(_species_payload(_adapter(tmp_path), doc, record["label"]))["scan_rotor_0"][
        "scan_result"]["points"][0]
    assert point["geometry"]["isotopes"] == {"4": 13}


def test_records_without_isotope_keys_send_none(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    del record["xyz_isotopes"], record["conformers_isotopes"]
    assert "isotopes" not in json.dumps(_species_payload(_adapter(tmp_path), doc, record["label"]))


def test_ts_geometry_isotopes_on_both_ts_routes(tmp_path):
    doc = _doc13()
    ts = doc["transition_states"][0]
    ts["xyz_isotopes"] = [12, 2, 1]
    reaction = _reaction_payload(_adapter(tmp_path), doc)
    assert reaction["transition_state"]["geometry"]["isotopes"] == {"2": 2}
    assert _standalone(_adapter(tmp_path), doc)["geometry"]["isotopes"] == {"2": 2}


@pytest.mark.parametrize("label", ["iC3H7", "nC3H7", "iC3H7_d", "SO2OO", "sBuOH"])
def test_conformer_route_carries_the_same_levels_isotopes_and_routes(tmp_path, label):
    adapter = _adapter(tmp_path)
    doc = adapter._with_adaptive_levels(_project(tmp_path), [])
    record = _record(doc, label)
    payload = adapter._build_payload(output_doc=doc, species_record=record, warnings=[])
    calcs = [payload["calculation"], *payload.get("additional_calculations", [])]
    assert calcs[0]["level_of_theory"] == adapter._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="c0", warnings=[]
    )["conformers"][0]["primary_calculation"]["level_of_theory"]
    if label == "iC3H7_d":
        assert payload["geometry"]["isotopes"] == {"4": 2}
    if label == "SO2OO":
        assert calcs[0]["software_release"]["name"] == "gaussian"
        assert {s["role"] for s in payload["statmech"]["source_calculations"]} >= {"composite", "sp"}


# ---------------------------------------------------------------------------
# 7. Routes
# ---------------------------------------------------------------------------

def test_route_lines_are_sent_as_stated_execution_controls(tmp_path):
    doc = _sample("species_thermo")
    record = _record(doc, "iC3H7")
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, "iC3H7"))
    for kind, field in (("opt", "opt_route"), ("freq", "freq_route"), ("sp", "sp_route")):
        route = [p for p in calcs[kind]["parameters"] if p["raw_key"] == "route"]
        assert route == [{"raw_key": "route", "raw_value": record[field], "section": kind,
                          "value_type": "string"}]
    # the freq hessian method parameter is still there beside it
    assert {p["raw_key"] for p in calcs["freq"]["parameters"]} == {"freq_hessian_method", "route"}


def test_ts_routes_including_the_irc_lines(tmp_path):
    doc = _doc13()
    ts = doc["transition_states"][0]
    ts["opt_route"] = "#P opt=(ts, calcfc) wb97xd/def2tzvp"
    ts["irc_log_routes"] = ["#P irc=(forward) wb97xd/def2tzvp", "#P irc=(forward) wb97xd/def2tzvp"]
    calcs = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))
    assert [p["raw_value"] for p in calcs["ts_opt"]["parameters"]] == ["#P opt=(ts, calcfc) wb97xd/def2tzvp"]
    assert calcs["ts_irc"]["parameters"] == [{
        "raw_key": "route", "raw_value": "#P irc=(forward) wb97xd/def2tzvp", "section": "irc", "value_type": "string"}]
    ts["irc_log_routes"] = ["#P irc=(forward) x", "#P irc=(reverse) x"]
    calcs = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))
    assert [(p["parameter_index"], p["raw_value"]) for p in calcs["ts_irc"]["parameters"]] == [
        (0, "#P irc=(forward) x"), (1, "#P irc=(reverse) x")]
    standalone = _standalone(_adapter(tmp_path), doc)
    assert standalone["primary_opt"]["parameters"][0]["section"] == "opt"


def test_unstated_routes_and_pre_1_3_records_send_none(tmp_path):
    old = _doc13()
    record = old["species"][0]
    record["opt_route"] = "#P opt"
    for key in ("levels", "xyz_isotopes", "conformers_isotopes"):
        del record[key]
    record["ess_software"] = {"opt": "gaussian", "freq": "gaussian", "sp": "gaussian"}
    old["opt_level"]["software"] = "gaussian"
    plain = _calcs(_species_payload(_adapter(tmp_path), old, record["label"]))
    assert "parameters" not in plain["opt"]
    none_stated = _doc13()
    assert "parameters" not in _calcs(_species_payload(_adapter(tmp_path), none_stated,
                                                        none_stated["species"][0]["label"]))["opt"]


def test_coarse_opt_does_not_inherit_the_opt_route(tmp_path):
    doc = _doc13()
    record = doc["species"][0]
    record.update({"opt_route": "#P opt b3lyp", "coarse_opt_log": "calcs/c.log",
                   "coarse_opt_output_xyz": "C 0.0 0.0 0.0\nH 1.0 0.0 0.1", "coarse_opt_n_steps": 3})
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, record["label"]))
    assert calcs["opt"]["parameters"][0]["raw_value"] == "#P opt b3lyp"
    assert "parameters" not in calcs["opt_coarse"]


# ---------------------------------------------------------------------------
# The ARC agent's sample documents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["species_thermo", "reaction_kinetics", "legacy_restart"])
def test_samples_are_1_3_and_the_missing_sidecar_falls_back(tmp_path, name):
    doc = _sample(name)
    assert doc["schema_version"] == "1.3"
    # the descriptor names a parser_evidence.json that is not shipped: the evidence store falls back
    store = EvidenceStore(tmp_path)
    assert store.lookup(doc, "species", doc["species"][0]["label"], "freq_hessian").state == "fallback"


def test_species_thermo_sample_builds_on_the_species_route(tmp_path):
    doc = _sample("species_thermo")
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, "iC3H7"))
    assert calcs["opt"]["level_of_theory"] == {"method": "uhf", "basis": "3-21g"}
    assert calcs["opt"]["software_release"]["name"] == "gaussian"


def test_reaction_kinetics_sample_builds_on_the_reaction_and_ts_routes(tmp_path):
    doc = _sample("reaction_kinetics")
    payload = _reaction_payload(_adapter(tmp_path), doc)
    calcs = _ts_calcs(payload)
    assert "ts_irc" not in calcs                               # route contradicts the level (see above)
    assert {p["raw_key"] for p in calcs["ts_opt"]["parameters"]} == {"route"}
    assert {s["label"] if "label" in s else None for s in payload["species"]} <= {None, "nC3H7", "iC3H7"}
    assert _standalone(_adapter(tmp_path), doc)["primary_opt"]["parameters"][0]["section"] == "opt"


def test_legacy_restart_sample_states_nothing_and_nothing_is_invented(tmp_path):
    doc = _sample("legacy_restart")
    for record in doc["species"]:
        with pytest.raises(ValueError, match="no level of theory"):
            _species_payload(_adapter(tmp_path), doc, record["label"])
    warnings = []
    with pytest.raises(ValueError):
        _reaction_payload(_adapter(tmp_path), doc, warnings)
