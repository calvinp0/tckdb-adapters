"""The shared upload pipeline, driven by a minimal producer that knows no ARC."""

import json
import logging
from types import SimpleNamespace
from unittest import mock

import pytest

from tckdb_client.errors import TCKDBError

from tckdb_core.config import TCKDBArtifactConfig, TCKDBConfig
from tckdb_core.constants import PREFLIGHT_MAX_ATTEMPTS
from tckdb_core.outcomes import TCKDBReadinessError
from tckdb_core.uploader import (
    TCKDBUploaderBase,
    _artifact_batch_idempotency_prefix,
    _calculation_ref_not_returned_warning,
    _extract_calc_refs,
    _extract_tckdb_public_refs,
)


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"


def _config(tmp_path, **kw):
    kw.setdefault("preflight", False)
    return TCKDBConfig(
        enabled=True, base_url="https://example.invalid", payload_dir=str(tmp_path),
        api_key_env="DEMO_TCKDB_KEY", **kw,
    )


def _client(data=None, status=201, replayed=False):
    client = mock.Mock()
    client.request_json.return_value = SimpleNamespace(
        data=data if data is not None else {}, status_code=status,
        idempotency_replayed=replayed, headers={"X-Request-ID": "req-1"},
    )
    return client


def _written(adapter, key="demo-key-0123456789"):
    return adapter._writer.write(
        label="demo", payload={"a": 1}, endpoint="/uploads/conformers", idempotency_key=key,
    )


def test_upload_records_the_response_warnings_refs_and_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")
    warning = {"code": "w", "message": "m", "field": None, "context": {}}
    body = {"warnings": [warning], "submission_ref": "sub_1",
            "primary_calculation": {"calculation_id": 3, "type": "opt"}}
    client = _client(body)
    adapter = DemoAdapter(_config(tmp_path), client_factory=lambda *a, **k: client)
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})

    sidecar = json.loads(written.sidecar_path.read_text())
    assert outcome.status == sidecar["status"] == "uploaded"
    assert outcome.warnings == sidecar["warnings"] == [warning]
    assert outcome.submission_ref == "sub_1"
    assert outcome.primary_calculation == {"calculation_id": 3, "type": "opt"}
    assert sidecar["request_ids"] == [
        {"operation": "upload", "request_id": "req-1", "status_code": 201}]
    client.request_json.assert_called_once_with(
        "POST", "/uploads/conformers", json={"a": 1}, idempotency_key=written.sidecar.idempotency_key)


def test_a_missing_key_is_a_recorded_failure_and_strict_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_TCKDB_KEY", raising=False)
    adapter = DemoAdapter(_config(tmp_path))
    written = _written(adapter)
    outcome = adapter._upload(written, {})
    assert outcome.status == "failed" and "API key not configured" in outcome.error

    strict = DemoAdapter(_config(tmp_path / "s", strict=True))
    with pytest.raises(ValueError):
        strict._upload(_written(strict), {})


def test_the_log_hook_decides_where_the_pipeline_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")
    sink = mock.Mock()

    class Redirected(DemoAdapter):
        @property
        def _log(self):
            return sink

    adapter = Redirected(_config(tmp_path), client_factory=lambda *a, **k: _client())
    adapter._upload(_written(adapter), {})
    assert sink.info.called


def test_the_shared_logger_follows_the_producer_name(caplog):
    from tckdb_core import _logging

    original = _logging._logger_name
    try:
        _logging.set_logger_name("demo_producer")
        with caplog.at_level(logging.INFO, logger="demo_producer"):
            _logging.get_logger().info("hello")
        assert [r.name for r in caplog.records] == ["demo_producer"]
    finally:
        _logging.set_logger_name(original)


def test_readiness_is_probed_with_backoff_then_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")
    client = mock.Mock()
    client.request_json.side_effect = TCKDBError("down")
    adapter = DemoAdapter(_config(tmp_path, preflight=True), client_factory=lambda *a, **k: client)
    sleeps = []
    adapter._sleep_between_probes = sleeps.append
    outcome = adapter._upload(_written(adapter), {})
    assert outcome.status == "failed"
    assert [c.args[1] for c in client.request_json.call_args_list] == ["/readyz"] * PREFLIGHT_MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    with pytest.raises(TCKDBReadinessError):
        adapter._ensure_ready(client)


def test_the_artifact_skip_reason_names_the_producer(tmp_path):
    adapter = DemoAdapter(_config(tmp_path, artifacts=TCKDBArtifactConfig(upload=True, kinds=("checkpoint",))))
    outcome = adapter._prepare_artifact_upload(
        output_doc={}, species_label="CH4", calculation_id=None, calculation_ref="calc_1",
        kind="checkpoint", file_path="x", artifact_cfg=adapter._config.artifacts,
    )
    assert outcome.status == "skipped"
    assert outcome.skip_reason == "kind 'checkpoint' is server-accepted but Demo has no upload path yet"


def test_the_ref_not_returned_warning_carries_the_producer_tag():
    warning = _calculation_ref_not_returned_warning(7, producer="demo")
    assert warning["code"] == "calculation_ref_not_returned"
    assert warning["field"] == "calculation_ref"
    assert warning["context"] == {"source": "demo_self_check", "calculation_id": 7}


def test_the_artifact_batch_prefix_takes_the_producer_namespace():
    assert _artifact_batch_idempotency_prefix("my proj", "CH4", namespace="demo") == "demo:my-proj:CH4:artifact"


def test_response_ref_extraction():
    assert _extract_tckdb_public_refs({"species_entry_ref": "se_1", "request": {"x_ref": "no"}}) == {
        "species_entry_refs": ["se_1"]}
    assert _extract_calc_refs({"conformers": [{"primary_calculation": {"calculation_id": 1}}]}) == (
        {"calculation_id": 1}, [])


# --- pre-0.10 legacy artifact sidecar skip ---------------------------------------------------


def _legacy_case(tmp_path, *, sidecar=None, base_url="https://example.invalid"):
    """An adapter whose payload dir holds (optionally) a ``calc{int}`` sidecar for one artifact."""
    import hashlib

    adapter = DemoAdapter(_config(tmp_path / "out"))
    artifact = tmp_path / "output.out"
    artifact.write_bytes(b"log bytes")
    sha = hashlib.sha256(b"log bytes").hexdigest()
    if sidecar is not None:
        record = {"status": "uploaded", "sha256": sha, "base_url": base_url, **sidecar}
        record = {k: v for k, v in record.items() if v is not Ellipsis}
        directory = adapter._writer._root / adapter._writer.ARTIFACT_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        name = f"H.calc7.output_log{adapter._writer.ARTIFACT_SIDECAR_SUFFIX}"
        (directory / name).write_text(json.dumps(record))
    return adapter, artifact


def _prepare(adapter, artifact):
    return adapter._prepare_artifact_upload(
        output_doc={}, species_label="H", calculation_id=7, kind="output_log", file_path=artifact,
        artifact_cfg=TCKDBArtifactConfig(upload=True), calculation_ref="calc_abc",
    )


def test_an_uploaded_legacy_sidecar_with_the_same_sha_and_server_skips_the_artifact(tmp_path):
    adapter, artifact = _legacy_case(tmp_path, sidecar={})
    outcome = _prepare(adapter, artifact)
    assert outcome.status == "skipped"
    assert "pre-0.10" in outcome.skip_reason and "H.calc7.output_log" in outcome.skip_reason
    # nothing new was written for the ref-keyed name
    directory = adapter._writer._root / adapter._writer.ARTIFACT_SUBDIR
    assert [p.name for p in directory.iterdir()] == [
        f"H.calc7.output_log{adapter._writer.ARTIFACT_SIDECAR_SUFFIX}"]


def test_an_uploaded_legacy_sidecar_without_a_base_url_still_skips(tmp_path):
    adapter, artifact = _legacy_case(tmp_path, sidecar={"base_url": Ellipsis})
    assert _prepare(adapter, artifact).status == "skipped"


@pytest.mark.parametrize(
    "sidecar",
    [
        {"status": "pending"},
        {"sha256": "0" * 64},
        {"base_url": "https://other.invalid"},
    ],
    ids=["pending", "different_sha", "different_base_url"],
)
def test_a_legacy_sidecar_that_does_not_match_does_not_skip(tmp_path, sidecar):
    adapter, artifact = _legacy_case(tmp_path, sidecar=sidecar)
    prepared = _prepare(adapter, artifact)
    assert prepared.__class__.__name__ == "_PreparedArtifactUpload"
    assert prepared.calculation_ref == "calc_abc"


def test_no_legacy_sidecar_does_not_skip(tmp_path):
    adapter, artifact = _legacy_case(tmp_path, sidecar=None)
    assert _prepare(adapter, artifact).__class__.__name__ == "_PreparedArtifactUpload"


def test_the_legacy_sidecar_is_not_consulted_without_a_calculation_ref(tmp_path):
    adapter, artifact = _legacy_case(tmp_path, sidecar={})
    prepared = adapter._prepare_artifact_upload(
        output_doc={}, species_label="H", calculation_id=7, kind="output_log", file_path=artifact,
        artifact_cfg=TCKDBArtifactConfig(upload=True),
    )
    assert prepared.__class__.__name__ == "_PreparedArtifactUpload"
