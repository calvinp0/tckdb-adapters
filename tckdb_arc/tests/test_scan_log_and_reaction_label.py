"""Adapter-only gaps from BRIDGE_ROADMAP A15 that need no ARC change.

* a rotor scan's own log (``rotor_scans[].source_log``) is its calculation's
  ``output_log`` artifact;
* a reaction species' conformer carries ``label``, as the computed-species
  route already sends.
"""

import base64
import copy

import pytest
from _contract import contract_validate

import test_adapter as fixtures
from test_current_contract_mapping import _neutral_scan, _scan_doc
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig

SCAN_LOG = b" Gaussian, Inc.\n Gaussian 16, Revision A.03\n scan\n"


@pytest.fixture
def project(tmp_path):
    log = tmp_path / "calcs" / "rotor_0" / "scan.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(SCAN_LOG)
    return tmp_path


def _adapter(project, *, upload_artifacts=True):
    return TCKDBAdapter(
        TCKDBConfig(
            enabled=True, upload=False, base_url="http://localhost",
            payload_dir=str(project / "payloads"),
            artifacts=TCKDBArtifactConfig(upload=upload_artifacts, kinds=("output_log", "input"))),
        project_directory=project)


def _scan_record(doc, source_log):
    doc["scan_level"] = {"method": "b3lyp", "basis": "def2-svp", "software": "gaussian"}
    record = doc["species"][0]
    scan = _neutral_scan()
    if source_log is not None:
        scan["source_log"] = source_log
    record["rotor_scans"] = [scan]
    for rec in (record, doc["transition_states"][0]):
        rec["ess_software"] = {"opt": "gaussian", "scan": "gaussian"}
        rec["ess_versions"] = {"opt": "Gaussian 16, Revision A.03", "scan": "Gaussian 16, Revision A.03"}
    return record


def _artifacts(calc):
    return [(a["kind"], base64.b64decode(a["content_base64"])) for a in calc.get("artifacts", [])]


def test_reaction_scan_carries_its_log(project):
    doc = _scan_doc()
    _scan_record(doc, "calcs/rotor_0/scan.log")
    payload = _adapter(project)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])
    scan = next(c for c in payload["species"][0]["calculations"] if c["type"] == "scan")
    assert _artifacts(scan) == [("output_log", SCAN_LOG)]
    artifact = scan["artifacts"][0]
    assert artifact["filename"] == "scan.log" and artifact["bytes"] == len(SCAN_LOG)
    contract_validate(ComputedReactionUploadRequest, payload)


def test_species_scan_carries_its_log(project):
    doc = _scan_doc()
    record = _scan_record(doc, "calcs/rotor_0/scan.log")
    payload = _adapter(project)._build_computed_species_payload(
        output_doc=doc, species_record=record, conformer_key="conf0")
    scan = next(c for c in payload["conformers"][0]["additional_calculations"]
                if c["type"] == "scan")
    assert _artifacts(scan) == [("output_log", SCAN_LOG)]
    contract_validate(ComputedSpeciesUploadRequest, payload)


@pytest.mark.parametrize("source_log,upload", [
    (None, True), ("calcs/rotor_0/missing.log", True), ("calcs/rotor_0/scan.log", False)])
def test_no_scan_log_no_artifact(project, source_log, upload):
    doc = _scan_doc()
    _scan_record(doc, source_log)
    payload = _adapter(project, upload_artifacts=upload)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])
    scan = next(c for c in payload["species"][0]["calculations"] if c["type"] == "scan")
    assert "artifacts" not in scan


def test_scan_log_is_not_attached_to_other_calculations(project):
    doc = _scan_doc()
    _scan_record(doc, "calcs/rotor_0/scan.log")
    payload = _adapter(project)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])
    for calc in payload["species"][0]["calculations"]:
        if calc["type"] != "scan":
            assert SCAN_LOG not in [content for _, content in _artifacts(calc)]


def test_reaction_conformers_carry_the_species_label(tmp_path):
    doc = fixtures._reaction_output_doc()
    payload = _adapter(tmp_path, upload_artifacts=False)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])
    assert [s["conformers"][0]["label"] for s in payload["species"]] == [
        "CHO", "CH4", "CH2O", "CH3"]
    contract_validate(ComputedReactionUploadRequest, payload)


def test_a_long_label_is_capped_like_the_species_route(tmp_path):
    doc = fixtures._reaction_output_doc()
    doc["species"][0]["label"] = "X" * 80
    record = copy.deepcopy(doc["reactions"][0])
    record["reactant_labels"][0] = "X" * 80
    payload = _adapter(tmp_path, upload_artifacts=False)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=record)
    assert payload["species"][0]["conformers"][0]["label"] == "X" * 64
