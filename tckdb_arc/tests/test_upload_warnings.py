"""Accepted uploads retain scientific warnings without changing response handling."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig


@pytest.mark.parametrize("replayed", [False, True])
def test_upload_preserves_and_logs_complete_structured_warnings(tmp_path, monkeypatch, replayed):
    monkeypatch.setenv("TCKDB_WARNING_TEST_KEY", "test-key")
    warnings = [{
        "code": "freq_list_incomplete_for_geometry",
        "message": "Incomplete spectrum " + "x" * 3000,
        "context": {"calculation_key": "TS_freq", "expected_modes": 6},
        "field": "transition_state.calculations[0]",
    }]
    body = {"species_entry_id": 1, "warnings": warnings}
    client = Mock()
    client.request_json.return_value = SimpleNamespace(
        data=body, status_code=201, idempotency_replayed=replayed, headers={},
    )
    adapter = TCKDBAdapter(
        TCKDBConfig(base_url="https://example.invalid", payload_dir=str(tmp_path),
                    api_key_env="TCKDB_WARNING_TEST_KEY", preflight=False),
        client_factory=lambda *args, **kwargs: client,
    )
    written = adapter._writer.write(
        label="warning", payload={}, endpoint="/api/v1/uploads/computed-species",
        idempotency_key="warning-test",
    )
    logger = Mock()
    monkeypatch.setattr("tckdb_arc.adapter.logger", logger)
    outcome = adapter._upload(written, {})
    sidecar = json.loads(written.sidecar_path.read_text())
    assert outcome.status == "uploaded"
    assert outcome.response == body
    assert outcome.warnings == warnings
    assert sidecar["warnings"] == warnings
    assert sidecar["response_body"] == body
    assert sidecar["idempotency_replayed"] is replayed
    logger.warning.assert_called_once_with("TCKDB upload warning: %s", warnings[0])


@pytest.mark.parametrize("body", [{}, {"warnings": None}, "accepted"])
def test_upload_without_warning_list_keeps_legacy_response(tmp_path, monkeypatch, body):
    monkeypatch.setenv("TCKDB_WARNING_TEST_KEY", "test-key")
    client = Mock()
    client.request_json.return_value = SimpleNamespace(
        data=body, status_code=201, idempotency_replayed=False, headers={},
    )
    adapter = TCKDBAdapter(
        TCKDBConfig(base_url="https://example.invalid", payload_dir=str(tmp_path),
                    api_key_env="TCKDB_WARNING_TEST_KEY", preflight=False),
        client_factory=lambda *args, **kwargs: client,
    )
    written = adapter._writer.write(
        label="no-warning", payload={}, endpoint="/api/v1/uploads/computed-species",
        idempotency_key="no-warning-test",
    )
    outcome = adapter._upload(written, {})
    assert outcome.status == "uploaded"
    assert outcome.response == body
    assert outcome.warnings == []
    assert json.loads(written.sidecar_path.read_text())["warnings"] == []
