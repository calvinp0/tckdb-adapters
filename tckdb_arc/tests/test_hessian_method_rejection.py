"""The 422 ``hessian_method_contradicts_input_deck`` reaches the user, and nothing retries around it.

ARC states ``freq.hessian_method``; the adapter forwards it; TCKDB refuses a value that its input deck
contradicts. The refusal must be visible by its code (sidecar ``last_error``, the structured response body,
the log line, the sweep's printed ``failed:`` line), and the upload must not be repeated without the parameter:
a contradiction is for ARC and the depositor to correct (ARC PR #1059), not for the adapter to hide.

The stubbed server error is the real client's own mapping of a 422 body (``TCKDBClient._build_http_error``),
and the code is the pinned client's ``RejectionCode``, not a string copy.
"""

import copy
import io
import json
import logging
import os
from contextlib import redirect_stdout
from unittest import mock

import pytest

from test_adapter import _StubClient
from test_arc_schema_1_3_thermo import _doc, _species
from test_hessian_method_deck_agreement import ANALYTIC, FD_GRADIENT, ORCA_NUMFREQ
from tckdb_arc.adapter import COMPUTED_SPECIES_ENDPOINT, TCKDBAdapter
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig
from tckdb_arc.sweep import _run_species_sweep
from tckdb_client.client import TCKDBClient
from tckdb_client.rejection_codes import RejectionCode

CODE = RejectionCode.HESSIAN_METHOD_CONTRADICTS_INPUT_DECK.value


def _refusal():
    body = {
        "code": CODE,
        "detail": ("the recorded freq.hessian_method says the Hessian method was analytic but the input "
                   "deck attached to the same calculation says finite_difference_gradient."),
        "context": {"recorded": ANALYTIC, "input_deck": FD_GRADIENT, "calculation_key": "freq"},
    }
    return TCKDBClient._build_http_error(
        status_code=422, parsed=body, text=json.dumps(body), headers={"X-Request-ID": "req-hessian"})


def _sweep(tmp_path, *, strict=False):
    doc = _doc()
    doc["species"] = [copy.deepcopy(_species(doc, "sBuOH"))]
    record = doc["species"][0]
    (tmp_path / "freq.in").write_text(ORCA_NUMFREQ)
    record.update(freq_input="freq.in", freq_hessian_method=ANALYTIC)  # ARC's value contradicts its own deck
    record["ess_software"]["freq"] = "orca"
    cfg = TCKDBConfig(
        enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=str(tmp_path / "payloads"),
        api_key_env="X_TCKDB_API_KEY", project_label="hessian", upload_mode="computed_species",
        upload=True, preflight=False, strict=strict,
        artifacts=TCKDBArtifactConfig(upload=True, kinds=("input",)),
    )
    client = _StubClient(raise_exc=_refusal())
    adapter = TCKDBAdapter(cfg, project_directory=str(tmp_path), client_factory=lambda c, k: client)
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), redirect_stdout(out):
        _run_species_sweep(adapter=adapter, output_doc=doc, tckdb_config=cfg)
    return client, out.getvalue()


@pytest.mark.payload_refused_by_contract  # the built payload carries the contradiction the stub server refuses
def test_the_contradiction_code_reaches_the_sidecar_the_log_and_the_sweep_output(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        client, printed = _sweep(tmp_path)

    (sidecar_path,) = (tmp_path / "payloads").rglob("*.meta.json")
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["status"] == "failed"
    assert sidecar["response_status_code"] == 422
    assert sidecar["response_body"]["code"] == CODE
    assert sidecar["response_body"]["context"] == {
        "recorded": ANALYTIC, "input_deck": FD_GRADIENT, "calculation_key": "freq"}
    assert CODE in sidecar["last_error"] and sidecar["last_error"].startswith("HTTP 422")
    assert CODE in caplog.text
    # What the user sees: the failure count and the code, with what disagreed.
    assert "failed: 1" in printed and "uploaded: 0" in printed
    (line,) = [ln for ln in printed.splitlines() if ln.strip().startswith("failed: sBuOH")]
    assert CODE in line and "calculation_key" in line and FD_GRADIENT in line


@pytest.mark.payload_refused_by_contract
def test_a_contradiction_refusal_is_never_retried_without_the_parameter(tmp_path):
    client, _ = _sweep(tmp_path)
    posts = [c for c in client.calls if c["path"] != "/readyz"]
    # One POST, to the bundle route, with the parameter exactly as ARC stated it; no second attempt.
    assert [c["path"] for c in posts] == [COMPUTED_SPECIES_ENDPOINT]
    freq = [c for c in posts[0]["json"]["conformers"][0]["additional_calculations"] if c["type"] == "freq"]
    assert [p["canonical_value"] for p in freq[0]["parameters"] if p["canonical_key"] == "freq.hessian_method"] == [ANALYTIC]
    # The real client the adapter builds (no client_factory) has no retry policy, so the transport cannot repeat it either.
    cfg = TCKDBConfig(enabled=True, base_url="http://localhost:8000/api/v1", payload_dir=str(tmp_path / "p2"),
                      api_key_env="X_TCKDB_API_KEY", project_label="hessian", upload_mode="computed_species")
    own_client = TCKDBAdapter(cfg, project_directory=str(tmp_path))._make_client("k")
    assert isinstance(own_client, TCKDBClient)
    assert own_client._retry is None


@pytest.mark.payload_refused_by_contract
def test_strict_mode_still_shows_the_code_in_the_sweep_output(tmp_path):
    """Strict mode raises the client error into the sweep; its message is the prose, so the code is added."""
    client, printed = _sweep(tmp_path, strict=True)
    (line,) = [ln for ln in printed.splitlines() if ln.strip().startswith("failed: sBuOH")]
    assert "TCKDBValidationError" in line and f"[{CODE}]" in line
    assert len([c for c in client.calls if c["path"] != "/readyz"]) == 1
