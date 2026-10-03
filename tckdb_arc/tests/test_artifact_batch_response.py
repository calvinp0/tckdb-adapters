"""The artifact batch keeps the server's full response (BRIDGE_ROADMAP A8).

``TCKDBClient.upload_artifacts`` returns only each response's parsed body, so
the status code, request id and replay header were lost. The adapter now sends
each calculation's batch through the public ``request_json`` (which returns the
full ``TCKDBResponse``), the way bundle uploads already are, and records the
same fields for artifacts: status, request id, replay flag and the response's
``warnings``. These tests run the real ``TCKDBClient`` over an httpx mock
transport so the envelope is the client's, not a stub's.
"""

import base64
import json
import logging
import os
from pathlib import Path
from unittest import mock

import httpx
import pytest
from tckdb_client import TCKDBClient

from _contract import artifacts_request_bodies
from tckdb_arc.adapter import (
    TCKDBAdapter,
    _PreparedArtifactUpload,
    _artifact_batch_bodies,
)
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig

from test_adapter import _fake_output_doc, _fake_record

WARNING = {
    "code": "software_release_version_filled_from_artifact",
    "message": "filled the version from the uploaded log",
    "field": "software_release.version",
}


def _handler(seen, *, warnings, replayed=False):
    def handle(request):
        if request.url.path.endswith("/readyz"):
            return httpx.Response(200, json={"status": "ready"},
                                  headers={"X-Request-ID": "req-readyz"})
        body = json.loads(request.content)
        seen.append({"path": request.url.path, "body": body,
                     "idempotency_key": request.headers.get("Idempotency-Key")})
        # The adapter names the calculation by its calc_ ref (calc_<n> here); the
        # integer path is the deprecated fallback.
        handle = request.url.path.split("/")[-2]
        calculation_id = int(handle.removeprefix("calc_"))
        headers = {"X-Request-ID": f"req-{calculation_id}"}
        if replayed:
            headers["Idempotency-Replayed"] = "true"
        return httpx.Response(201, headers=headers, json={
            "calculation_id": calculation_id,
            "calculation_ref": f"calc_{calculation_id}",
            "artifacts": [{"artifact_ref": f"artifact_{calculation_id}_{i}"}
                          for i, _ in enumerate(body["artifacts"])],
            "warnings": warnings.get(calculation_id, []),
        })
    return handle


@pytest.fixture
def project(tmp_path):
    log = tmp_path / "project" / "calcs" / "output.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b" Gaussian 16, Revision A.03\n")
    deck = log.parent / "input.gjf"
    deck.write_bytes(b"# opt\n")
    return tmp_path / "project", log, deck


def _adapter(project_dir, transport):
    cfg = TCKDBConfig(
        enabled=True, base_url="http://tckdb.test/api/v1", payload_dir="payloads",
        api_key_env="X_TCKDB_API_KEY", project_label="proj-A",
        artifacts=TCKDBArtifactConfig(upload=True, kinds=("output_log", "input")))
    return TCKDBAdapter(
        cfg, project_directory=project_dir,
        client_factory=lambda c, key: TCKDBClient(
            "http://tckdb.test/api/v1", api_key=key, transport=transport))


def _submit(adapter, calc_id, artifacts, *, by_ref=True):
    ids = {"calculation_ref": f"calc_{calc_id}"} if by_ref else {"calculation_id": calc_id}
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        return adapter.submit_artifact_batch_for_calculation(
            output_doc=_fake_output_doc(), species_record=_fake_record(),
            calculation_type="opt", artifacts=artifacts, **ids)


def test_server_warnings_status_request_id_and_replay_reach_sidecar_and_outcome(
        project, caplog):
    project_dir, log, deck = project
    seen = []
    transport = httpx.MockTransport(_handler(seen, warnings={42: [WARNING]}, replayed=True))
    with caplog.at_level(logging.WARNING, logger="tckdb_arc"):
        outcomes = _submit(_adapter(project_dir, transport), 42,
                           [("output_log", log), ("input", deck)])
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]
    assert len(seen) == 1 and seen[0]["path"].endswith("/calculations/calc_42/artifacts")
    for outcome in outcomes:
        assert outcome.warnings == [WARNING]
        sidecar = json.loads(outcome.sidecar_path.read_text())
        assert sidecar["warnings"] == [WARNING]
        assert sidecar["response_status_code"] == 201
        assert sidecar["idempotency_replayed"] is True
        assert [(r["operation"], r["request_id"], r["status_code"])
                for r in sidecar["request_ids"] if r["operation"] == "artifact_upload"
                ] == [("artifact_upload", "req-42", 201)]
    assert "software_release_version_filled_from_artifact" in caplog.text


def test_a_first_upload_is_not_marked_replayed_and_has_no_warnings(project):
    project_dir, log, _ = project
    transport = httpx.MockTransport(_handler([], warnings={}))
    (outcome,) = _submit(_adapter(project_dir, transport), 42, [("output_log", log)])
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["idempotency_replayed"] is False
    assert sidecar["warnings"] == [] and outcome.warnings == []
    assert sidecar["response_status_code"] == 201


def test_each_item_takes_the_response_to_its_own_calculation(project):
    project_dir, log, deck = project
    adapter = _adapter(project_dir, httpx.MockTransport(
        _handler([], warnings={1: [WARNING], 2: []})))
    prepared = []
    for calc_id, path, kind in ((1, log, "output_log"), (2, deck, "input")):
        prepared.append(adapter._prepare_artifact_upload(
            output_doc=_fake_output_doc(), species_label="ethanol",
            calculation_id=None, calculation_ref=f"calc_{calc_id}", kind=kind, file_path=path,
            artifact_cfg=adapter._config.artifacts))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcomes = adapter._upload_artifact_batch(
            prepared=prepared, idempotency_key_prefix="arc:proj-A:ethanol:artifact")
    assert [(o.calculation_ref, o.warnings) for o in outcomes] == [("calc_1", [WARNING]), ("calc_2", [])]
    ids = [json.loads(o.sidecar_path.read_text())["request_ids"][-1]["request_id"]
           for o in outcomes]
    assert ids == ["req-1", "req-2"]


def test_requests_match_what_the_client_itself_would_send(project):
    """Same bodies and idempotency keys as ``TCKDBClient.upload_artifacts``.

    That equality is what lets a replay of a batch stored by an earlier adapter
    (which used ``upload_artifacts``) still hit the same idempotency key.
    """
    project_dir, log, deck = project
    adapter = _adapter(project_dir, httpx.MockTransport(_handler([], warnings={})))
    prepared = [
        adapter._prepare_artifact_upload(
            output_doc=_fake_output_doc(), species_label="ethanol",
            calculation_id=calc_id, calculation_ref=f"calc_{calc_id}", kind=kind,
            file_path=path, artifact_cfg=adapter._config.artifacts)
        for calc_id, path, kind in ((7, log, "output_log"), (7, deck, "input"),
                                    (9, log, "output_log"))
    ]
    assert all(isinstance(p, _PreparedArtifactUpload) for p in prepared)
    prefix = "arc:proj-A:ethanol:artifact"

    client_seen = []
    client = TCKDBClient(
        "http://tckdb.test/api/v1", api_key="k",
        transport=httpx.MockTransport(_handler(client_seen, warnings={})))
    client.upload_artifacts(prepared, idempotency_key_prefix=prefix, batch_by_calculation=True)

    adapter_seen = []
    adapter_client = TCKDBClient(
        "http://tckdb.test/api/v1", api_key="k",
        transport=httpx.MockTransport(_handler(adapter_seen, warnings={})))
    adapter._post_artifact_batches(adapter_client, prepared, idempotency_key_prefix=prefix)

    assert adapter_seen == client_seen
    assert [b for _, _, b in _artifact_batch_bodies(prepared)] == artifacts_request_bodies(prepared)
    assert base64.b64decode(adapter_seen[0]["body"]["artifacts"][0]["content_base64"]) == log.read_bytes()


def test_a_refused_batch_is_recorded_failed_with_the_server_status(project):
    project_dir, log, _ = project

    def refuse(request):
        if request.url.path.endswith("/readyz"):
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(422, json={"detail": "artifact_integrity_failed"},
                              headers={"X-Request-ID": "req-refused"})

    (outcome,) = _submit(_adapter(project_dir, httpx.MockTransport(refuse)), 42,
                         [("output_log", log)])
    assert outcome.status == "failed"
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["response_status_code"] == 422
    assert sidecar["request_ids"][-1]["request_id"] == "req-refused"


def test_without_a_ref_the_integer_id_is_the_fallback_and_is_warned_about(project, caplog):
    project_dir, log, _ = project
    seen = []
    transport = httpx.MockTransport(_handler(seen, warnings={}))
    with caplog.at_level(logging.WARNING, logger="tckdb_arc"):
        (outcome,) = _submit(_adapter(project_dir, transport), 42, [("output_log", log)], by_ref=False)
    assert seen[0]["path"].endswith("/calculations/42/artifacts")
    assert outcome.status == "uploaded" and outcome.calculation_ref is None and outcome.calculation_id == 42
    assert [w["code"] for w in outcome.warnings] == ["calculation_ref_not_returned"]
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["calculation_ref"] is None and sidecar["calculation_id"] == 42
    assert "calculation_ref_not_returned" in caplog.text


def test_a_ref_names_the_url_the_key_and_the_sidecar_and_wins_over_the_id(project):
    project_dir, log, _ = project
    seen = []
    adapter = _adapter(project_dir, httpx.MockTransport(_handler(seen, warnings={})))
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        (outcome,) = adapter.submit_artifact_batch_for_calculation(
            output_doc=_fake_output_doc(), species_record=_fake_record(),
            calculation_id=42, calculation_ref="calc_42", calculation_type="opt",
            artifacts=[("output_log", log)])
    assert seen[0]["path"].endswith("/calculations/calc_42/artifacts")
    assert "calc_42" in seen[0]["idempotency_key"] and ":42:" not in seen[0]["idempotency_key"]
    assert outcome.warnings == []
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["calculation_ref"] == "calc_42" and sidecar["calculation_id"] == 42
    assert "calc_42" in outcome.idempotency_key and ":artifact:42:" not in outcome.idempotency_key
    assert "calc_42" in outcome.sidecar_path.name


def test_the_sweep_names_artifacts_by_the_responses_calculation_ref():
    from tckdb_arc import sweep

    class _Spy:
        def __init__(self):
            self.calls = []

        def submit_artifact_batch_for_calculation(self, **kw):
            self.calls.append(kw)
            return []

    spy, counts, failures = _Spy(), {}, []
    outcome = mock.Mock(
        primary_calculation={"calculation_id": 10, "calculation_ref": "calc_aaaa", "type": "opt"},
        additional_calculations=[{"calculation_id": 11, "type": "freq"}])
    with mock.patch.object(sweep, "_resolve_artifact_path", return_value="x.log"):
        sweep._sweep_artifacts_for_species(
            adapter=spy, output_doc=_fake_output_doc(), species_record=_fake_record(), outcome=outcome,
            counts=counts, failures=failures, kinds=("output_log",))
    assert [(c["calculation_ref"], c["calculation_id"]) for c in spy.calls] == [("calc_aaaa", 10), (None, 11)]


# ---- a project uploaded before 0.10 (integer-id keys and ``calc{int}`` sidecars)


def _submit_both(adapter, calc_id, artifacts):
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        return adapter.submit_artifact_batch_for_calculation(
            output_doc=_fake_output_doc(), species_record=_fake_record(),
            calculation_id=calc_id, calculation_ref=f"calc_{calc_id}", calculation_type="opt",
            artifacts=artifacts)


def _pre_0_10_upload(project_dir, log):
    """What an adapter before 0.10 left behind: an uploaded ``calc42`` sidecar."""
    seen = []
    (outcome,) = _submit(_adapter(project_dir, httpx.MockTransport(_handler(seen, warnings={}))),
                         42, [("output_log", log)], by_ref=False)
    assert outcome.status == "uploaded" and "calc42" in outcome.sidecar_path.name
    return outcome.sidecar_path


def test_an_artifact_a_pre_0_10_sidecar_records_as_uploaded_is_not_posted_again(project):
    project_dir, log, deck = project
    legacy = _pre_0_10_upload(project_dir, log)
    seen = []
    adapter = _adapter(project_dir, httpx.MockTransport(_handler(seen, warnings={})))
    outcomes = _submit_both(adapter, 42, [("output_log", log), ("input", deck)])
    by_kind = {o.kind: o for o in outcomes}
    assert by_kind["output_log"].status == "skipped"
    assert legacy.name in by_kind["output_log"].skip_reason and "pre-0.10" in by_kind["output_log"].skip_reason
    # the other artifact has no old sidecar: it uploads under the ref key
    assert by_kind["input"].status == "uploaded"
    assert [b["kind"] for s in seen for b in s["body"]["artifacts"]] == ["input"]
    assert "calc_42" in by_kind["input"].idempotency_key
    assert json.loads(legacy.read_text())["status"] == "uploaded"       # the old sidecar is untouched


@pytest.mark.parametrize("change", ["pending", "other_bytes", "other_server"])
def test_a_pre_0_10_sidecar_that_does_not_prove_the_upload_does_not_skip(project, change):
    project_dir, log, _ = project
    legacy = _pre_0_10_upload(project_dir, log)
    data = json.loads(legacy.read_text())
    if change == "pending":
        data["status"] = "pending"
    elif change == "other_bytes":
        data["sha256"] = "0" * 64
    else:
        data["base_url"] = "http://elsewhere.test/api/v1"
    legacy.write_text(json.dumps(data))
    seen = []
    adapter = _adapter(project_dir, httpx.MockTransport(_handler(seen, warnings={})))
    (outcome,) = _submit_both(adapter, 42, [("output_log", log)])
    assert outcome.status == "uploaded"
    assert len(seen) == 1 and "calc_42" in outcome.sidecar_path.name


def test_without_a_pre_0_10_sidecar_the_ref_keyed_upload_proceeds(project):
    project_dir, log, _ = project
    seen = []
    adapter = _adapter(project_dir, httpx.MockTransport(_handler(seen, warnings={})))
    (outcome,) = _submit_both(adapter, 42, [("output_log", log)])
    assert outcome.status == "uploaded" and len(seen) == 1
