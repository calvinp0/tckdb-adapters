#!/usr/bin/env python3
# encoding: utf-8

"""Unit tests for tckdb_arc.adapter.

These tests do not require a live TCKDB server. The TCKDBClient is
replaced by a stub via the adapter's ``client_factory`` parameter.
"""

import copy
import json
import math
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from _contract import contract_validate
from tckdb_arc.adapter import (
    ARTIFACTS_ENDPOINT_TEMPLATE,
    ArtifactUploadOutcome,
    COMPUTED_REACTION_ENDPOINT,
    COMPUTED_REACTION_KIND,
    CONFORMER_UPLOAD_ENDPOINT,
    PAYLOAD_KIND,
    TCKDBAdapter,
    UploadOutcome,
    _extract_tckdb_public_refs,
    _freq_result_payload,
    arc_to_tckdb_a_units,
    arc_to_tckdb_ea_units,
)
from tckdb_arc.config import TCKDBArtifactConfig, TCKDBConfig

import pytest

# ARC is an optional (conda/PYTHONPATH) dependency — not pip-installable. A few
# tests below reparse real ESS logs from ARC's `arc/testing/` corpus or import
# `arc.parser` / `arc.output`; they are guarded with `pytest.importorskip("arc")`
# so the base (no-ARC) suite stays green.
try:
    from arc.common import ARC_PATH
    ARC_TESTING_PATH = os.path.join(ARC_PATH, 'arc', 'testing')
except ImportError:
    ARC_PATH = None
    ARC_TESTING_PATH = None

from tckdb_schemas.fragments.calculation import (
    FreqResultPayload,
    HessianPayload,
    HessianSource,
    IRCResultPayload,
    PathSearchResultPayload,
)
from tckdb_schemas.workflows.computed_species_upload import (
    ComputedSpeciesUploadRequest,
)
from tckdb_schemas.workflows.computed_reaction_upload import (
    ComputedReactionUploadRequest,
)

# Optional GSM stringfile fixture for path-search tests. Set
# ``ARC_GSM_STRINGFILE_FIXTURE`` to a real ``stringfile.xyz0000``
# (multi-frame XYZ) to enable end-to-end tests against a real GSM
# run. Tests auto-skip when the fixture is unset or missing.
_GSM_STRINGFILE_FIXTURE = os.environ.get('ARC_GSM_STRINGFILE_FIXTURE')
_GSM_FIXTURE_SKIP_MSG = (
    "GSM stringfile fixture not present; "
    "set ARC_GSM_STRINGFILE_FIXTURE to a real stringfile.xyz0000."
)


def _upload_calls(client):
    return [call for call in client.calls if call.get("path") != "/readyz"]


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, data, status_code=201, replayed=False, headers=None):
        self.data = data
        self.status_code = status_code
        self.idempotency_replayed = replayed
        self.headers = headers or {}


class _StubBatchResult:
    def __init__(
        self,
        *,
        calculation_id,
        calculation_keys,
        artifact_count,
        response,
        status_code=None,
        replayed=None,
        headers=None,
    ):
        self.calculation_id = calculation_id
        self.calculation_keys = tuple(calculation_keys)
        self.artifact_count = artifact_count
        self.response = response
        self.status_code = status_code
        self.idempotency_replayed = replayed
        self.headers = headers or {}


class _StubClient:
    """Minimal TCKDBClient lookalike for adapter tests."""

    def __init__(self, *, response=None, raise_exc=None, readyz_response=None):
        self._response = response
        self._raise_exc = raise_exc
        self._readyz_response = readyz_response or _StubResponse(
            {"status": "ready", "alembic_revision": "test-rev"},
            status_code=200,
            headers={"X-Request-ID": "req-readyz"},
        )
        self.calls = []
        self.closed = False

    def request_json(
        self,
        method,
        path,
        *,
        json=None,
        authenticated=True,
        idempotency_key=None,
    ):
        self.calls.append(
            dict(
                method=method,
                path=path,
                json=json,
                authenticated=authenticated,
                idempotency_key=idempotency_key,
            )
        )
        if path == "/readyz":
            if isinstance(self._readyz_response, BaseException):
                raise self._readyz_response
            return self._readyz_response
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response

    def upload_artifacts(
        self,
        plan,
        *,
        idempotency_key_prefix=None,
        batch_by_calculation=False,
    ):
        items = list(plan)
        self.calls.append(dict(
            method="upload_artifacts",
            plan=items,
            idempotency_key_prefix=idempotency_key_prefix,
            batch_by_calculation=batch_by_calculation,
        ))
        if self._raise_exc is not None:
            raise self._raise_exc
        data = getattr(self._response, "data", self._response)
        status_code = getattr(self._response, "status_code", None)
        replayed = getattr(self._response, "idempotency_replayed", None)
        headers = getattr(self._response, "headers", None)
        groups = {}
        for item in items:
            groups.setdefault(item.calculation_id, []).append(item)
        return [
            _StubBatchResult(
                calculation_id=calc_id,
                calculation_keys=[item.calculation_key for item in group],
                artifact_count=len(group),
                response=data,
                status_code=status_code,
                replayed=replayed,
                headers=headers,
            )
            for calc_id, group in groups.items()
        ]

    def close(self):
        self.closed = True


class _SequencedReadyzClient(_StubClient):
    """Stub whose ``/readyz`` responses are consumed from a sequence.

    Each ``/readyz`` call pops the next item in ``readyz_sequence``; when
    the sequence is exhausted the last item repeats (steady state). An
    item that is a ``BaseException`` is raised (simulating a request
    exception); otherwise it is returned. Non-readyz calls behave like
    the base ``_StubClient``. Lets a test drive "K transient failures then
    ready" without real sleeps.
    """

    def __init__(self, *, readyz_sequence, response=None, raise_exc=None):
        super().__init__(response=response, raise_exc=raise_exc)
        self._readyz_sequence = list(readyz_sequence)
        self._readyz_idx = 0

    def request_json(
        self,
        method,
        path,
        *,
        json=None,
        authenticated=True,
        idempotency_key=None,
    ):
        if path == "/readyz":
            self.calls.append(dict(method=method, path=path, json=json,
                                   authenticated=authenticated,
                                   idempotency_key=idempotency_key))
            idx = min(self._readyz_idx, len(self._readyz_sequence) - 1)
            self._readyz_idx += 1
            item = self._readyz_sequence[idx]
            if isinstance(item, BaseException):
                raise item
            return item
        return super().request_json(
            method, path, json=json, authenticated=authenticated,
            idempotency_key=idempotency_key,
        )


def _fake_record(label="ethanol", smiles="CCO", charge=0, multiplicity=1, is_ts=False):
    """Mimics one entry from output.yml's `species:` (or `transition_states:`) list.

    Note ``ess_versions`` is keyed by job type (``opt``/``freq``/``sp``/``neb``),
    matching ``arc/output.py::_get_ess_versions``.
    """
    return {
        "label": label,
        "smiles": smiles,
        "charge": charge,
        "multiplicity": multiplicity,
        "is_ts": is_ts,
        "converged": True,
        "xyz": "C 0.0 0.0 0.0\nH 1.0 0.0 0.0",
        "opt_n_steps": 12,
        "opt_final_energy_hartree": -154.123,
        "ess_versions": {"opt": "Gaussian 16, Revision A.03"},
    }


def _fake_output_doc():
    """Mimics the top-level fields of output.yml that the adapter reads."""
    return {
        "schema_version": "1.0",
        "project": "test_project",
        "arc_version": "1.2.3",
        "arc_git_commit": "deadbeef",
        "opt_level": {"method": "wb97xd", "basis": "def2-tzvp", "software": "gaussian"},
        "scan_level": {"method": "wb97xd", "basis": "def2-tzvp", "software": "gaussian"},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTCKDBPublicRefExtraction(unittest.TestCase):
    """Public-ref summary extraction for TCKDB upload responses."""

    def test_collects_nested_refs(self):
        refs = _extract_tckdb_public_refs({
            "species_ref": "spe_1",
            "conformers": [{
                "species_entry_ref": "spee_1",
                "primary_calculation": {"calculation_ref": "calc_1"},
                "results": {"thermo_ref": "thermo_1"},
            }],
            "artifacts": [{"artifact_ref": "artifact_1"}],
        })
        self.assertEqual(refs["species_refs"], ["spe_1"])
        self.assertEqual(refs["species_entry_refs"], ["spee_1"])
        self.assertEqual(refs["calculation_refs"], ["calc_1"])
        self.assertEqual(refs["thermo_refs"], ["thermo_1"])
        self.assertEqual(refs["artifact_refs"], ["artifact_1"])

    def test_dedupes_preserving_order(self):
        refs = _extract_tckdb_public_refs({
            "items": [
                {"calculation_ref": "calc_1"},
                {"calculation_ref": "calc_2"},
                {"calculation_ref": "calc_1"},
            ]
        })
        self.assertEqual(refs, {"calculation_refs": ["calc_1", "calc_2"]})

    def test_ignores_none_non_strings_and_request_echoes(self):
        refs = _extract_tckdb_public_refs({
            "species_ref": None,
            "reaction_ref": 123,
            "request": {"species_ref": "spe_request_echo"},
            "payload": {"calculation_ref": "calc_request_echo"},
            "result": {"reaction_ref": "rxe_1"},
        })
        self.assertEqual(refs, {"reaction_refs": ["rxe_1"]})


class TestAdapterDisabled(unittest.TestCase):
    """Test 1: disabled config does nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_disabled_returns_none_and_writes_nothing(self):
        cfg = TCKDBConfig(enabled=False, base_url="http://x", payload_dir=self.tmp)
        client = _StubClient(response=_StubResponse({"ok": True}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        self.assertIsNone(outcome)
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertEqual(client.calls, [])


class TestAdapterPayloadAndUpload(unittest.TestCase):
    """Tests 2, 3, 9, 10, 11: payload + sidecar + replay metadata + no DB IDs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    def test_payload_written_before_failed_upload(self):
        """Test 2: payload written even when upload fails."""
        client = _StubClient(raise_exc=RuntimeError("network down"))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())

        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.payload_path.exists())
        self.assertTrue(outcome.sidecar_path.exists())
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "failed")
        self.assertIn("network down", sc["last_error"])
        # Payload file is intact and parseable
        json.loads(outcome.payload_path.read_text())

    def test_upload_success_updates_sidecar(self):
        """Test 3: upload success -> sidecar status = uploaded."""
        client = _StubClient(response=_StubResponse(
            {"conformer_observation_id": 42},
            headers={"x-request-id": "req-upload"},
        ))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())

        self.assertEqual(outcome.status, "uploaded")
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "uploaded")
        self.assertIsNotNone(sc["uploaded_at"])
        self.assertEqual(sc["response_status_code"], 201)
        self.assertEqual(sc["idempotency_replayed"], False)
        # Test 11: replay-ready metadata present
        self.assertEqual(sc["endpoint"], CONFORMER_UPLOAD_ENDPOINT)
        self.assertEqual(sc["payload_kind"], PAYLOAD_KIND)
        self.assertEqual(sc["payload_file"], str(outcome.payload_path))
        self.assertTrue(sc["idempotency_key"])
        self.assertEqual(sc["base_url"], self.cfg.base_url)
        self.assertEqual(client.calls[0]["path"], "/readyz")
        self.assertEqual(_upload_calls(client)[0]["path"], CONFORMER_UPLOAD_ENDPOINT)
        self.assertEqual(sc["preflight"]["ready"], True)
        self.assertEqual(sc["preflight"]["request_id"], "req-readyz")
        self.assertEqual(
            [entry["request_id"] for entry in sc["request_ids"]],
            ["req-readyz", "req-upload"],
        )

    def test_readyz_preflight_failure_prevents_upload(self):
        readyz = _StubResponse(
            {"status": "not_ready", "code": "schema_not_initialized"},
            status_code=200,
            headers={"X-Request-ID": "req-not-ready"},
        )
        client = _StubClient(
            response=_StubResponse({"conformer_observation_id": 42}),
            readyz_response=readyz,
        )
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), \
                mock.patch("tckdb_arc.adapter._preflight_sleep"):
            outcome = adapter.submit_from_output(
                output_doc=_fake_output_doc(),
                species_record=_fake_record(),
            )
            outcome2 = adapter.submit_from_output(
                output_doc=_fake_output_doc(),
                species_record=_fake_record(label="methanol", smiles="CO"),
            )
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome2.status, "failed")
        # A persistently not-ready server is probed PREFLIGHT_MAX_ATTEMPTS
        # times on the first submit; the second submit re-raises the cached
        # preflight error without probing again (single-check semantics).
        from tckdb_arc.adapter import PREFLIGHT_MAX_ATTEMPTS
        self.assertEqual(
            [call["path"] for call in client.calls],
            ["/readyz"] * PREFLIGHT_MAX_ATTEMPTS,
        )
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertIn("schema_not_initialized", sc["last_error"])
        self.assertEqual(sc["preflight"]["ready"], False)
        self.assertEqual(sc["request_ids"][0]["request_id"], "req-not-ready")

    def test_upload_false_skips_readyz_preflight(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload=False,
        )
        client = _StubClient(response=_StubResponse({"conformer_observation_id": 42}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(
                output_doc=_fake_output_doc(),
                species_record=_fake_record(),
            )
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])

    def test_readyz_preflight_runs_once_per_adapter(self):
        client = _StubClient(response=_StubResponse({"conformer_observation_id": 42}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
            adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record(label="methanol", smiles="CO"))
        readyz_calls = [call for call in client.calls if call.get("path") == "/readyz"]
        self.assertEqual(len(readyz_calls), 1)

    def test_readyz_recovers_after_transient_failures(self):
        # The server is not-ready / unreachable for K attempts, then ready.
        # The retry loop must ride through the blip and upload succeeds
        # without any error surfacing. K=3 transient failures < the 5-attempt
        # budget, so the 4th probe reports ready.
        from tckdb_client.errors import TCKDBConnectionError
        not_ready = _StubResponse(
            {"status": "not_ready", "code": "schema_not_initialized"},
            status_code=200,
        )
        ready = _StubResponse(
            {"status": "ready", "alembic_revision": "test-rev"},
            status_code=200,
            headers={"X-Request-ID": "req-readyz"},
        )
        # Mix both failure modes: a request exception, then not-ready
        # bodies, then ready.
        readyz_seq = [
            TCKDBConnectionError("server unreachable"),
            not_ready,
            not_ready,
            ready,
        ]
        client = _SequencedReadyzClient(
            readyz_sequence=readyz_seq,
            response=_StubResponse({"conformer_observation_id": 42}),
        )
        adapter = TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), \
                mock.patch("tckdb_arc.adapter._preflight_sleep") as sleep:
            outcome = adapter.submit_from_output(
                output_doc=_fake_output_doc(), species_record=_fake_record(),
            )
        self.assertEqual(outcome.status, "uploaded")
        # 4 readyz probes (3 failures + 1 ready), then the upload POST.
        readyz_calls = [c for c in client.calls if c["path"] == "/readyz"]
        self.assertEqual(len(readyz_calls), 4)
        self.assertEqual(sleep.call_count, 3)  # one backoff between each retry

    def test_readyz_raises_after_all_attempts_exhausted(self):
        # Every probe fails → after PREFLIGHT_MAX_ATTEMPTS the adapter gives
        # up with a TCKDBReadinessError (surfaced as a failed outcome under
        # strict=False, and the payload is still on disk).
        from tckdb_arc.adapter import (
            PREFLIGHT_MAX_ATTEMPTS,
            TCKDBReadinessError,
        )
        from tckdb_client.errors import TCKDBConnectionError
        client = _SequencedReadyzClient(
            readyz_sequence=[TCKDBConnectionError("down")],  # last repeats
            response=_StubResponse({"conformer_observation_id": 42}),
        )
        adapter = TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), \
                mock.patch("tckdb_arc.adapter._preflight_sleep"):
            outcome = adapter.submit_from_output(
                output_doc=_fake_output_doc(), species_record=_fake_record(),
            )
        self.assertEqual(outcome.status, "failed")
        readyz_calls = [c for c in client.calls if c["path"] == "/readyz"]
        self.assertEqual(len(readyz_calls), PREFLIGHT_MAX_ATTEMPTS)
        # No upload POST ever happened.
        self.assertFalse(any(c["path"] != "/readyz" for c in client.calls))
        self.assertIsInstance(adapter._preflight_error, TCKDBReadinessError)

    def test_readiness_exhaustion_logs_recovery_command(self):
        # On genuine exhaustion the user must get a copy-pasteable recovery
        # command telling them the payloads are on disk and how to re-run.
        from tckdb_arc.adapter import PREFLIGHT_MAX_ATTEMPTS
        from tckdb_client.errors import TCKDBConnectionError
        client = _SequencedReadyzClient(
            readyz_sequence=[TCKDBConnectionError("down")],
            response=_StubResponse({"conformer_observation_id": 42}),
        )
        adapter = TCKDBAdapter(
            self.cfg,
            project_directory="/proj/run7",
            client_factory=lambda c, k: client,
        )
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), \
                mock.patch("tckdb_arc.adapter._preflight_sleep"), \
                self.assertLogs("tckdb_arc", level="WARNING") as logs:
            adapter.submit_from_output(
                output_doc=_fake_output_doc(), species_record=_fake_record(),
            )
        blob = "\n".join(logs.output)
        self.assertIn(
            f"TCKDB server was not ready after {PREFLIGHT_MAX_ATTEMPTS} attempts",
            blob,
        )
        # The exact CLI command, with the real project dir + upload mode.
        self.assertIn(
            "tckdb-arc-upload /proj/run7/input.yml "
            f"-p /proj/run7 --upload-mode {self.cfg.upload_mode}",
            blob,
        )

    def test_upload_records_idempotency_replay(self):
        client = _StubClient(
            response=_StubResponse({"conformer_observation_id": 42}, replayed=True)
        )
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertTrue(sc["idempotency_replayed"])

    def test_payload_contains_no_db_ids(self):
        """Test 9: payload must not include raw TCKDB DB IDs."""
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        flat = outcome.payload_path.read_text()
        for forbidden in (
            '"species_id"',
            '"species_entry_id"',
            '"calculation_id"',
            '"conformer_observation_id"',
            '"literature_id"',
            '"software_release_id"',
            '"workflow_tool_release_id"',
        ):
            self.assertNotIn(forbidden, flat, msg=f"raw DB id in payload: {forbidden}")

    def test_payload_validates_against_expected_shape(self):
        """Test 10: payload has the conformer-upload top-level shape."""
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        payload = json.loads(outcome.payload_path.read_text())
        self.assertIn("species_entry", payload)
        self.assertIn("geometry", payload)
        self.assertIn("calculation", payload)
        self.assertEqual(payload["species_entry"]["smiles"], "CCO")
        self.assertEqual(
            payload["geometry"]["xyz_text"],
            "2\nethanol\nC 0.0 0.0 0.0\nH 1.0 0.0 0.0",
        )
        self.assertEqual(payload["calculation"]["type"], "opt")
        # ARC's banner is split as TCKDB's SoftwareReleaseRef would split it.
        self.assertEqual(
            payload["calculation"]["software_release"],
            {"name": "gaussian", "version": "16", "revision": "A.03"},
        )
        self.assertEqual(payload["calculation"]["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(payload["calculation"]["level_of_theory"]["basis"], "def2-tzvp")


class TestAdapterSkipped(unittest.TestCase):
    """Test 4: upload=false -> payload written, sidecar skipped, no network."""

    def test_upload_skipped_writes_payload_no_call(self):
        tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=tmp,
            upload=False,
        )
        client = _StubClient(response=_StubResponse({"ok": True}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        self.assertEqual(outcome.status, "skipped")
        self.assertTrue(outcome.payload_path.exists())
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "skipped")
        self.assertEqual(client.calls, [])


class TestAdapterStrict(unittest.TestCase):
    """Tests 5, 6: strict raises; non-strict swallows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_strict_mode_raises_and_records(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            strict=True,
        )
        client = _StubClient(raise_exc=RuntimeError("503"))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            with self.assertRaises(RuntimeError):
                adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        # Sidecar still written, status failed
        files = os.listdir(os.path.join(self.tmp, "conformer_calculation"))
        sidecar = [f for f in files if f.endswith(".meta.json")][0]
        with open(os.path.join(self.tmp, "conformer_calculation", sidecar)) as f:
            sc = json.load(f)
        self.assertEqual(sc["status"], "failed")

    def test_non_strict_does_not_raise(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            strict=False,
        )
        client = _StubClient(raise_exc=RuntimeError("503"))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        self.assertEqual(outcome.status, "failed")


class TestAdapterIdempotency(unittest.TestCase):
    """Test 7: same logical input -> same key; changed input -> different key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
        )

    def test_idempotency_key_stable_and_distinct(self):
        client_a = _StubClient(response=_StubResponse({"id": 1}))
        adapter = TCKDBAdapter(self.cfg, client_factory=lambda c, k: client_a)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            o1 = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
            o2 = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
            o3 = adapter.submit_from_output(
                output_doc=_fake_output_doc(),
                species_record=_fake_record(label="methanol", smiles="CO"),
            )
        self.assertEqual(o1.idempotency_key, o2.idempotency_key)
        self.assertNotEqual(o1.idempotency_key, o3.idempotency_key)
        # The header sent must match the recorded key.
        sent_keys = {call["idempotency_key"] for call in _upload_calls(client_a)}
        self.assertEqual(sent_keys, {o1.idempotency_key, o3.idempotency_key})


class TestAdapterApiKey(unittest.TestCase):
    """Test 8: missing API key with upload=true produces failed sidecar (no raise)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_api_key_records_failure(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            api_key_env="DEFINITELY_NOT_SET_X_X",
        )
        client = _StubClient(response=_StubResponse({"ok": True}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        os.environ.pop("DEFINITELY_NOT_SET_X_X", None)
        outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(client.calls, [])  # never called the network
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertIn("DEFINITELY_NOT_SET_X_X", sc["last_error"])

    def test_missing_api_key_strict_raises(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            api_key_env="DEFINITELY_NOT_SET_X_X",
            strict=True,
        )
        client = _StubClient(response=_StubResponse({"ok": True}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        os.environ.pop("DEFINITELY_NOT_SET_X_X", None)
        with self.assertRaises(ValueError):
            adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())


# ---------------------------------------------------------------------------
# Artifact upload tests
# ---------------------------------------------------------------------------


_GAUSSIAN_LOG_HEADER = (
    b" Gaussian, Inc.,  Pittsburgh PA, All Rights Reserved.\n"
    b" Cite this work as:\n Gaussian 16, Revision A.03\n"
)


class TestArtifactUpload(unittest.TestCase):
    """Tests for TCKDBAdapter.submit_artifacts_for_calculation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-art-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project_dir = os.path.join(self.tmp, "project")
        os.makedirs(self.project_dir)
        self.payload_dir = "tckdb_payloads"   # relative -> resolved under project_dir
        self.log_path = os.path.join(self.project_dir, "calcs", "Species", "ethanol", "opt", "output.log")
        os.makedirs(os.path.dirname(self.log_path))
        with open(self.log_path, "wb") as fh:
            fh.write(_GAUSSIAN_LOG_HEADER)

    def _cfg(
        self,
        *,
        artifacts=None,
        strict=False,
        api_key_env="X_TCKDB_API_KEY",
        preflight=True,
    ):
        return TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.payload_dir,
            api_key_env=api_key_env,
            project_label="proj-A",
            strict=strict,
            preflight=preflight,
            artifacts=artifacts or TCKDBArtifactConfig(upload=True, kinds=("output_log",)),
        )

    def _adapter(self, client, cfg=None):
        return TCKDBAdapter(
            cfg or self._cfg(),
            project_directory=self.project_dir,
            client_factory=lambda c, k: client,
        )

    def _submit(
        self,
        adapter,
        *,
        log_path=None,
        calculation_id=42,
        calculation_type="opt",
        kind="output_log",
    ):
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            return adapter.submit_artifacts_for_calculation(
                output_doc=_fake_output_doc(),
                species_record=_fake_record(),
                calculation_id=calculation_id,
                calculation_type=calculation_type,
                file_path=log_path or self.log_path,
                kind=kind,
            )

    def test_disabled_artifact_upload_returns_skipped_no_call(self):
        cfg = self._cfg(artifacts=TCKDBArtifactConfig(upload=False))
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=cfg)
        outcome = self._submit(adapter)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn("artifacts.upload", outcome.skip_reason)

    def test_kind_not_in_config_returns_skipped(self):
        cfg = self._cfg(artifacts=TCKDBArtifactConfig(upload=True, kinds=("input",)))
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=cfg)
        outcome = self._submit(adapter)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn("not in config.kinds", outcome.skip_reason)

    def test_missing_log_file_returns_skipped(self):
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client)
        outcome = self._submit(adapter, log_path=os.path.join(self.project_dir, "no_such_log.log"))
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn("file missing", outcome.skip_reason)

    def test_oversized_log_returns_skipped(self):
        cfg = self._cfg(
            artifacts=TCKDBArtifactConfig(upload=True, kinds=("output_log",), max_size_mb=1)
        )
        # Write a 2 MB file
        big_log = os.path.join(self.project_dir, "big.log")
        with open(big_log, "wb") as fh:
            fh.write(b"\x00" * (2 * 1024 * 1024))
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=cfg)
        outcome = self._submit(adapter, log_path=big_log)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn(">1 MB cap", outcome.skip_reason)

    def test_successful_upload_writes_sidecar_and_posts(self):
        client = _StubClient(response=_StubResponse(
            {
                "calculation_id": 42,
                "calculation_ref": "calc_42",
                "artifacts": [{"id": 7, "artifact_ref": "artifact_7"}],
            },
            status_code=201,
            headers={"X-Request-ID": "req-artifact-upload"},
        ))
        adapter = self._adapter(client)
        outcome = self._submit(adapter)
        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(outcome.calculation_id, 42)
        self.assertEqual(outcome.kind, "output_log")
        calls = _upload_calls(client)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "upload_artifacts")
        self.assertTrue(call["batch_by_calculation"])
        self.assertTrue(call["idempotency_key_prefix"].startswith("arc:proj-A:ethanol:artifact"))
        # Plan shape passed to tckdb-client.
        plan = call["plan"]
        self.assertEqual(len(plan), 1)
        a = plan[0]
        self.assertEqual(a.kind, "output_log")
        self.assertEqual(a.filename, "output.log")
        self.assertEqual(a.bytes, len(_GAUSSIAN_LOG_HEADER))
        self.assertEqual(len(a.sha256), 64)
        self.assertRegex(a.sha256, r"^[0-9a-f]{64}$")
        # Sidecar
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "uploaded")
        self.assertEqual(sc["calculation_id"], 42)
        self.assertEqual(sc["kind"], "output_log")
        self.assertEqual(sc["bytes"], len(_GAUSSIAN_LOG_HEADER))
        self.assertEqual(sc["public_refs"]["calculation_refs"], ["calc_42"])
        self.assertEqual(sc["public_refs"]["artifact_refs"], ["artifact_7"])
        self.assertEqual(sc["request_ids"][-1]["request_id"], "req-artifact-upload")

    def test_strict_failure_raises(self):
        cfg = self._cfg(strict=True)
        client = _StubClient(raise_exc=RuntimeError("422 ESS signature missing"))
        adapter = self._adapter(client, cfg=cfg)
        with self.assertRaises(RuntimeError):
            self._submit(adapter)
        # Sidecar still recorded as failed
        sidecar_files = [
            f for f in os.listdir(os.path.join(self.project_dir, self.payload_dir, "calculation_artifacts"))
            if f.endswith(".artifact.meta.json")
        ]
        self.assertEqual(len(sidecar_files), 1)
        with open(os.path.join(self.project_dir, self.payload_dir,
                               "calculation_artifacts", sidecar_files[0])) as f:
            sc = json.load(f)
        self.assertEqual(sc["status"], "failed")
        self.assertIn("422", sc["last_error"])

    def test_non_strict_failure_returns_outcome(self):
        client = _StubClient(raise_exc=RuntimeError("422 ESS signature missing"))
        adapter = self._adapter(client)
        outcome = self._submit(adapter)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("422", outcome.error)
        # Sidecar marks failure
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "failed")

    def test_old_client_without_batch_artifacts_fails_clearly(self):
        class _OldClient:
            calls = []
            closed = False

            def close(self):
                self.closed = True

        cfg = self._cfg(preflight=False)
        adapter = self._adapter(_OldClient(), cfg=cfg)
        outcome = self._submit(adapter)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("does not support batch_by_calculation", outcome.error)
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertIn("Upgrade tckdb-client", sc["last_error"])

    def test_idempotency_replay_recorded(self):
        client = _StubClient(response=_StubResponse(
            {
                "calculation_id": 42,
                "calculation_ref": "calc_replayed",
                "artifacts": [{"id": 7, "artifact_ref": "artifact_replayed"}],
            },
            status_code=201,
            replayed=True,
            headers={"X-Request-ID": "req-replayed"},
        ))
        adapter = self._adapter(client)
        outcome = self._submit(adapter)
        self.assertEqual(outcome.status, "uploaded")
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertTrue(sc["idempotency_replayed"])
        self.assertEqual(sc["public_refs"]["calculation_refs"], ["calc_replayed"])
        self.assertEqual(sc["public_refs"]["artifact_refs"], ["artifact_replayed"])
        self.assertEqual(sc["request_ids"][-1]["request_id"], "req-replayed")

    def test_idempotency_key_stable_for_same_inputs(self):
        client = _StubClient(response=_StubResponse(
            {"calculation_id": 42, "artifacts": []}, status_code=201
        ))
        adapter = self._adapter(client)
        o1 = self._submit(adapter)
        o2 = self._submit(adapter)
        self.assertEqual(o1.idempotency_key, o2.idempotency_key)
        calls = _upload_calls(client)
        prefixes = {c["idempotency_key_prefix"] for c in calls}
        self.assertEqual(len(prefixes), 1)
        plan_keys = {c["plan"][0].calculation_key for c in calls}
        self.assertEqual(len(plan_keys), 1)

    def test_idempotency_key_distinct_for_different_calc(self):
        client = _StubClient(response=_StubResponse(
            {"calculation_id": 99, "artifacts": []}, status_code=201
        ))
        adapter = self._adapter(client)
        o1 = self._submit(adapter, calculation_id=42)
        o2 = self._submit(adapter, calculation_id=99)
        self.assertNotEqual(o1.idempotency_key, o2.idempotency_key)

    def test_endpoint_template_matches_artifact_request(self):
        client = _StubClient(response=_StubResponse(
            {"calculation_id": 314, "artifacts": []}, status_code=201
        ))
        adapter = self._adapter(client)
        outcome = self._submit(adapter, calculation_id=314)
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["endpoint"], ARTIFACTS_ENDPOINT_TEMPLATE.format(calculation_id=314))
        self.assertEqual(outcome.status, "uploaded")

    def test_input_kind_uploads_with_correct_marshalling(self):
        # Input deck file (input.gjf) gets uploaded with kind="input".
        # Use a config that allows both kinds, then upload an input file.
        cfg = self._cfg(
            artifacts=TCKDBArtifactConfig(
                upload=True,
                kinds=("output_log", "input"),
            )
        )
        input_path = os.path.join(self.project_dir, "calcs", "Species", "ethanol", "opt", "input.gjf")
        with open(input_path, "wb") as fh:
            fh.write(b"%mem=42GB\n# wb97xd/def2tzvp opt\n")
        client = _StubClient(response=_StubResponse(
            {"calculation_id": 42, "artifacts": []}, status_code=201
        ))
        adapter = self._adapter(client, cfg=cfg)
        outcome = self._submit(adapter, log_path=input_path, kind="input")
        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(outcome.kind, "input")
        call = _upload_calls(client)[0]
        self.assertEqual(call["plan"][0].kind, "input")
        self.assertEqual(call["plan"][0].filename, "input.gjf")

    def test_batch_upload_groups_artifacts_for_one_calculation(self):
        cfg = self._cfg(
            artifacts=TCKDBArtifactConfig(
                upload=True,
                kinds=("output_log", "input"),
            )
        )
        input_path = os.path.join(self.project_dir, "calcs", "Species", "ethanol", "opt", "input.gjf")
        with open(input_path, "wb") as fh:
            fh.write(b"%mem=42GB\n# wb97xd/def2tzvp opt\n")
        client = _StubClient(response=_StubResponse(
            {
                "calculation_id": 42,
                "calculation_ref": "calc_42",
                "artifacts": [
                    {"artifact_ref": "artifact_log"},
                    {"artifact_ref": "artifact_input"},
                ],
            },
            status_code=201,
            headers={"X-Request-ID": "req-artifact-batch"},
        ))
        adapter = self._adapter(client, cfg=cfg)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcomes = adapter.submit_artifact_batch_for_calculation(
                output_doc=_fake_output_doc(),
                species_record=_fake_record(),
                calculation_id=42,
                calculation_type="opt",
                artifacts=[
                    ("output_log", self.log_path),
                    ("input", input_path),
                ],
            )
        self.assertEqual([o.status for o in outcomes], ["uploaded", "uploaded"])
        calls = _upload_calls(client)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertTrue(call["batch_by_calculation"])
        self.assertEqual([item.kind for item in call["plan"]], ["output_log", "input"])
        self.assertEqual({item.calculation_id for item in call["plan"]}, {42})
        sidecars = [json.loads(o.sidecar_path.read_text()) for o in outcomes]
        self.assertEqual({sc["kind"] for sc in sidecars}, {"output_log", "input"})
        for sc in sidecars:
            self.assertEqual(sc["public_refs"]["calculation_refs"], ["calc_42"])
            self.assertEqual(
                sc["public_refs"]["artifact_refs"],
                ["artifact_log", "artifact_input"],
            )
            self.assertEqual(sc["request_ids"][-1]["request_id"], "req-artifact-batch")

    def test_unimplemented_kind_defensive_skip(self):
        # Even if the user somehow gets `checkpoint` into config.kinds
        # AND a caller passes kind="checkpoint", the adapter refuses
        # rather than uploading bytes that may not match the kind's
        # semantic contract.
        cfg = self._cfg(
            artifacts=TCKDBArtifactConfig(
                upload=True,
                kinds=("output_log", "checkpoint"),  # parse-time warning only
            )
        )
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=cfg)
        outcome = self._submit(adapter, kind="checkpoint")
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("no upload path yet", outcome.skip_reason)
        self.assertEqual(client.calls, [])

    # ---------------- bundle-mode suppression
    def _bundle_cfg(self, upload_mode: str):
        return TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.payload_dir,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode=upload_mode,
            artifacts=TCKDBArtifactConfig(upload=True, kinds=("output_log", "input")),
        )

    def test_computed_species_mode_suppresses_standalone_output_log_artifact(self):
        # In computed_species bundle mode the bundle payload already
        # carries output_log inline under each calc; a standalone POST
        # would (a) duplicate the upload and (b) bake a stale calc id
        # into the sidecar that 404s on DB resets.
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=self._bundle_cfg("computed_species"))
        outcome = self._submit(adapter, kind="output_log")
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn("computed_species", outcome.skip_reason)
        self.assertIn("inline", outcome.skip_reason)
        # No sidecar should land on disk for the suppressed call.
        sidecar_dir = pathlib.Path(self.project_dir) / self.payload_dir / "calculation_artifacts"
        self.assertFalse(sidecar_dir.exists() and any(sidecar_dir.iterdir()))

    def test_computed_species_mode_suppresses_standalone_input_artifact(self):
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=self._bundle_cfg("computed_species"))
        # Stage an input deck file so the kind passes upstream gates.
        input_path = os.path.join(self.project_dir, "calcs", "Species", "ethanol", "opt", "input.gjf")
        with open(input_path, "wb") as fh:
            fh.write(b"# dummy gjf\n")
        outcome = self._submit(adapter, log_path=input_path, kind="input")
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn("inline", outcome.skip_reason)

    def test_computed_reaction_mode_suppresses_standalone_artifact(self):
        client = _StubClient(response=_StubResponse({}, status_code=201))
        adapter = self._adapter(client, cfg=self._bundle_cfg("computed_reaction"))
        outcome = self._submit(adapter, kind="output_log")
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        self.assertIn("computed_reaction", outcome.skip_reason)

    def test_conformer_mode_still_uploads_standalone_artifact(self):
        # Legacy conformer mode must keep working — primitive flows
        # don't have an inline artifact path, so disabling standalone
        # uploads here would silently drop data.
        client = _StubClient(response=_StubResponse(
            {"calculation_id": 42, "artifacts": [{"id": 7}]},
            status_code=201,
        ))
        # Default upload_mode is "conformer".
        adapter = self._adapter(client)
        outcome = self._submit(adapter, kind="output_log")
        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(len(_upload_calls(client)), 1)


class TestUploadOutcomeCalcRefs(unittest.TestCase):
    """Conformer upload exposes primary/additional calc refs from response."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_calc_refs_extracted_from_response(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
        )
        client = _StubClient(response=_StubResponse({
            "id": 1,
            "primary_calculation": {"calculation_id": 10, "type": "opt", "request_index": None},
            "additional_calculations": [
                {"calculation_id": 11, "type": "freq", "request_index": 0},
                {"calculation_id": 12, "type": "sp", "request_index": 1},
            ],
        }))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(outcome.primary_calculation["calculation_id"], 10)
        self.assertEqual(outcome.primary_calculation["type"], "opt")
        self.assertEqual(len(outcome.additional_calculations), 2)
        self.assertEqual(outcome.additional_calculations[0]["calculation_id"], 11)
        self.assertEqual(outcome.additional_calculations[1]["type"], "sp")

    def test_calc_refs_default_when_response_omits_them(self):
        cfg = TCKDBConfig(
            enabled=True, base_url="http://x", payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
        )
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=_fake_output_doc(), species_record=_fake_record())
        self.assertIsNone(outcome.primary_calculation)
        self.assertEqual(outcome.additional_calculations, [])


class TestAdditionalCalculations(unittest.TestCase):
    """opt/freq/sp chain: payload must carry additional_calculations for freq+sp."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    def _submit(self, *, output_doc, record):
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(output_doc=output_doc, species_record=record)
        return outcome, client, json.loads(outcome.payload_path.read_text())

    def test_opt_only_record_has_no_additional_calculations(self):
        """1. opt-only record produces no additional_calculations key."""
        record = _fake_record()  # no freq_*, no sp_energy_hartree
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        self.assertNotIn("additional_calculations", payload)

    def test_opt_plus_freq_record_yields_one_freq_additional(self):
        """2. opt+freq record produces one freq additional calculation."""
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["zpe_hartree"] = 0.024131
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        additional = payload["additional_calculations"]
        self.assertEqual(len(additional), 1)
        freq = additional[0]
        self.assertEqual(freq["type"], "freq")
        self.assertEqual(freq["freq_result"]["n_imag"], 0)
        self.assertAlmostEqual(freq["freq_result"]["zpe_hartree"], 0.024131)
        self.assertNotIn("imag_freq_cm1", freq["freq_result"])
        # No sp data → no sp calc
        self.assertEqual([c["type"] for c in additional], ["freq"])

    def test_opt_plus_sp_record_yields_one_sp_additional(self):
        """3. opt+sp record produces one sp additional calculation."""
        record = _fake_record()
        record["sp_energy_hartree"] = -154.987
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        additional = payload["additional_calculations"]
        self.assertEqual(len(additional), 1)
        sp = additional[0]
        self.assertEqual(sp["type"], "sp")
        self.assertAlmostEqual(sp["sp_result"]["electronic_energy_hartree"], -154.987)

    def test_sp_carries_spin_diagnostic_when_open_shell(self):
        """Open-shell sp: spin_diagnostic block attached with s_squared + companions."""
        record = _fake_record(multiplicity=3)
        record["sp_energy_hartree"] = -154.987
        record["sp_spin_diagnostic"] = {
            "s_squared": 2.0153,
            "s_squared_expected": 2.0,
            "s_squared_annihilated": 2.0001,
        }
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        self.assertIn("spin_diagnostic", sp)
        self.assertAlmostEqual(sp["spin_diagnostic"]["s_squared"], 2.0153)
        self.assertAlmostEqual(sp["spin_diagnostic"]["s_squared_expected"], 2.0)
        self.assertAlmostEqual(sp["spin_diagnostic"]["s_squared_annihilated"], 2.0001)
        # Full-payload validation against the installed tckdb-schemas.
        from tckdb_schemas.fragments.calculation import CalculationWithResultsPayload
        model = contract_validate(CalculationWithResultsPayload, sp)
        self.assertAlmostEqual(model.spin_diagnostic.s_squared, 2.0153)

    def test_sp_spin_diagnostic_minimal_only_s_squared(self):
        """Only s_squared present (ORCA/QChem, no annihilation): companions omitted."""
        record = _fake_record(multiplicity=2)
        record["sp_energy_hartree"] = -154.987
        record["sp_spin_diagnostic"] = {
            "s_squared": 0.7572,
            "s_squared_expected": 0.75,
            "s_squared_annihilated": None,
        }
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        self.assertIn("spin_diagnostic", sp)
        self.assertNotIn("s_squared_annihilated", sp["spin_diagnostic"])
        self.assertAlmostEqual(sp["spin_diagnostic"]["s_squared_expected"], 0.75)

    def test_sp_omits_spin_diagnostic_when_closed_shell(self):
        """Closed-shell/restricted sp (no S**2): spin_diagnostic omitted entirely."""
        record = _fake_record(multiplicity=1)
        record["sp_energy_hartree"] = -154.987
        record["sp_spin_diagnostic"] = None
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        self.assertNotIn("spin_diagnostic", sp)

    def test_sp_omits_spin_diagnostic_when_field_absent(self):
        """No sp_spin_diagnostic key at all: spin_diagnostic omitted (back-compat)."""
        record = _fake_record()
        record["sp_energy_hartree"] = -154.987
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        self.assertNotIn("spin_diagnostic", sp)

    def test_opt_freq_sp_record_is_refused_when_minimum_reports_an_imaginary_mode(self):
        """4 (was "opt+freq+sp record produces two additional calculations").

        This fixture is unchanged from the original: ``is_ts=False`` (the
        default) with ``freq_n_imag=1``. A previous fix pass changed it to
        ``is_ts=True``, calling the original "arbitrary" -- it was not.
        ``is_ts=True`` was itself wrong: ``adapter.py``'s
        ``_species_entry_payload`` would then emit
        ``species_entry_kind="transition_state"``, a value that does not
        exist in TCKDB 0.22.0's ``StationaryPointKind`` enum.

        Restored to ``is_ts=False``, this fixture is a genuine
        ``species_entry_kind='minimum'`` reporting one imaginary mode --
        exactly the contradiction TCKDB 0.22.0 refuses
        (``n_imag_contradicts_minimum``). The correct behaviour (D2 in
        ``_freq_result_payload``) is for the adapter to refuse to build
        this record at all, loudly, rather than upload two additional
        calculations under a classification the evidence disputes.
        opt+freq+sp chaining coverage (both additional calcs present,
        non-contradictory data) lives on
        ``TestFreqNImagSpeciesEntryKindConsistency.
        test_ts_two_imaginary_modes_designates_reaction_coordinate``.
        """
        record = _fake_record()
        record["freq_n_imag"] = 1
        record["imag_freq_cm1"] = -512.3
        record["zpe_hartree"] = 0.0399
        record["sp_energy_hartree"] = -155.111
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            with self.assertRaises(ValueError) as ctx:
                adapter.submit_from_output(
                    output_doc=_fake_output_doc(), species_record=record,
                )
        self.assertIn("n_imag_contradicts_minimum", str(ctx.exception))

    def test_freq_refused_when_minimum_reports_imaginary_mode_with_statmech_populated(self):
        """Restored is_ts=False fixture is a genuine contradiction, not a
        modes/sign-derivation exercise.

        Renamed from ``test_freq_modes_populated_from_statmech_harmonic_
        frequencies`` -- that name asserted the opposite of what this test
        now does (a raise, not populated modes). Its sibling immediately
        above was renamed correctly when this fixture was fixed; this one
        was missed.

        This fixture is unchanged from the original: ``is_ts=False`` (the
        default), ``freq_n_imag=1``, and a negative entry in
        ``statmech.harmonic_frequencies_cm1``. A previous fix pass changed
        it to ``is_ts=True``, calling ``freq_n_imag=1`` "arbitrary" -- it
        was not: this is ``is_ts=False``'s realistic default value.
        ``is_ts=True`` was wrong for a separate, independent reason too:
        ARC's ``_statmech_to_dict`` (arc/output.py) strips every negative
        frequency out of ``harmonic_frequencies_cm1`` whenever
        ``spc.is_ts`` ("Exclude the imaginary mode"), so a converged TS
        can never carry ``-1320.5`` there -- with ``is_ts=True`` this
        fixture was impossible ARC output.

        Restored to ``is_ts=False``, ``freq_n_imag=1`` plus a negative
        harmonic entry is a genuine ``species_entry_kind='minimum'``
        reporting an imaginary mode -- exactly the contradiction TCKDB
        0.22.0 refuses (``n_imag_contradicts_minimum``). The correct
        behaviour (D2 in ``_freq_result_payload``) is refusal, not a
        payload with ``modes`` built under a disputed classification.
        Sign-derivation coverage (a negative harmonic entry becoming
        ``is_imaginary=True``) on VALID, non-contradictory data is
        exercised directly on ``_freq_result_payload`` in
        ``test_freq_result_payload_modes_mode_index_and_is_imaginary``
        below, and on the TS fixture in
        ``TestFreqNImagSpeciesEntryKindConsistency.
        test_ts_two_imaginary_modes_designates_reaction_coordinate``.
        """
        record = _fake_record()
        record["freq_n_imag"] = 1
        record["imag_freq_cm1"] = -1320.5
        record["zpe_hartree"] = 0.05
        record["statmech"] = {
            "harmonic_frequencies_cm1": [-1320.5, 800.0, 1500.0, 3000.0],
        }
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            with self.assertRaises(ValueError) as ctx:
                adapter.submit_from_output(
                    output_doc=_fake_output_doc(), species_record=record,
                )
        self.assertIn("n_imag_contradicts_minimum", str(ctx.exception))

    def test_freq_result_payload_modes_mode_index_and_is_imaginary(self):
        """D3: direct coverage of ``_freq_result_payload`` on valid,
        non-contradictory data -- restoring the suite's only assertions
        on literal ``mode_index``/``is_imaginary`` values, which the
        fixture above stopped providing once it became (correctly) a
        contradiction/raise test.

        No species-entry path (``species_entry_kind='minimum'``) can
        ever carry an imaginary mode without tripping a D2 contradiction
        -- that is the whole point of D2 -- so there is no way to reach
        a valid, schema-accepted payload with an ``is_imaginary=True``
        mode through ``submit_from_output``/``submit_computed_species_
        from_output``. ``is_ts=True`` with ``freq_n_imag=1`` is
        non-contradictory (a converged TS has exactly one imaginary
        mode by definition), so this calls ``_freq_result_payload``
        directly on a TS-shaped record instead: ARC's own convention
        (arc/output.py) strips negative frequencies from
        ``statmech.harmonic_frequencies_cm1`` whenever ``spc.is_ts``, so
        the single imaginary mode is reinserted from ``imag_freq_cm1``
        at ``mode_index=1``, ahead of the three real modes.

        Validated against the real ``FreqResultPayload`` schema
        directly -- the exact shape this function's own docstring says
        it builds -- rather than a full ``submit_*`` pipeline, since no
        such pipeline can produce this shape (see above).
        """
        record = {
            "label": "TS0",
            "is_ts": True,
            "converged": True,
            "freq_n_imag": 1,
            "imag_freq_cm1": -1320.5,
            "zpe_hartree": 0.05,
            "statmech": {
                "harmonic_frequencies_cm1": [800.0, 1500.0, 3000.0],
            },
        }
        result = _freq_result_payload(record)
        modes = result["modes"]
        self.assertEqual([m["mode_index"] for m in modes], [1, 2, 3, 4])
        self.assertEqual(
            [m["frequency_cm1"] for m in modes], [-1320.5, 800.0, 1500.0, 3000.0],
        )
        self.assertEqual(
            [m["is_imaginary"] for m in modes], [True, False, False, False],
        )
        self.assertNotIn("reaction_coordinate_mode_index", result)
        contract_validate(FreqResultPayload, result)

    def test_freq_modes_only_record_still_emits_freq_calc(self):
        """When only statmech.harmonic_frequencies_cm1 is populated (no
        flat freq_n_imag/imag_freq_cm1/zpe_hartree), the freq calc is still
        emitted with just modes."""
        record = _fake_record()
        record["statmech"] = {
            "harmonic_frequencies_cm1": [800.0, 1500.0, 3000.0],
        }
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        freq = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
        self.assertEqual(set(freq["freq_result"].keys()), {"modes"})
        self.assertEqual(len(freq["freq_result"]["modes"]), 3)

    def test_empty_harmonic_frequencies_list_emits_no_modes(self):
        """Empty list is treated as no-modes-data and skipped; other freq
        fields still emit a freq calc without a modes field."""
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["zpe_hartree"] = 0.0399
        record["statmech"] = {"harmonic_frequencies_cm1": []}
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        freq = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
        self.assertNotIn("modes", freq["freq_result"])
        self.assertEqual(freq["freq_result"]["n_imag"], 0)

    def test_distinct_levels_preserved_per_calculation(self):
        """5. freq/sp use their own level-of-theory when output_doc has them."""
        doc = _fake_output_doc()
        doc["freq_level"] = {"method": "wb97xd", "basis": "6-31g*", "software": "gaussian"}
        doc["sp_level"] = {"method": "ccsd(t)-f12a", "basis": "cc-pvtz-f12", "software": "molpro"}
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["zpe_hartree"] = 0.024131
        record["sp_energy_hartree"] = -154.987
        # Job-type-keyed ess_versions: freq from gaussian, sp from molpro
        record["ess_versions"] = {
            "opt": "Gaussian 16, Revision A.03",
            "freq": "Gaussian 16, Revision A.03",
            "sp": "Molpro 2022.3",
        }
        _, _, payload = self._submit(output_doc=doc, record=record)
        primary = payload["calculation"]
        freq, sp = payload["additional_calculations"]
        # opt uses opt_level + ess_versions['opt']
        self.assertEqual(primary["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(primary["level_of_theory"]["basis"], "def2-tzvp")
        self.assertEqual(primary["software_release"]["version"], "16")
        self.assertEqual(primary["software_release"]["revision"], "A.03")
        # freq uses freq_level (distinct basis) + ess_versions['freq']
        self.assertEqual(freq["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(freq["level_of_theory"]["basis"], "6-31g*")
        self.assertEqual(freq["software_release"],
                         {"name": "gaussian", "version": "16", "revision": "A.03"})
        # sp uses sp_level (different method+software) + ess_versions['sp']
        self.assertEqual(sp["level_of_theory"]["method"], "ccsd(t)-f12a")
        self.assertEqual(sp["level_of_theory"]["basis"], "cc-pvtz-f12")
        self.assertEqual(sp["software_release"], {"name": "molpro", "version": "2022.3"})

    def test_freq_sp_levels_fall_back_to_opt_level_when_missing(self):
        """Option B: missing freq_level/sp_level falls back to opt_level."""
        doc = _fake_output_doc()
        # freq_level / sp_level absent (the common ARC case)
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["sp_energy_hartree"] = -154.5
        _, _, payload = self._submit(output_doc=doc, record=record)
        freq, sp = payload["additional_calculations"]
        self.assertEqual(freq["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(freq["level_of_theory"]["basis"], "def2-tzvp")
        self.assertEqual(sp["level_of_theory"]["method"], "wb97xd")
        # ess_versions has only 'opt' → both freq and sp fall back to that
        for calc in (freq, sp):
            self.assertEqual((calc["software_release"]["version"],
                              calc["software_release"]["revision"]), ("16", "A.03"))

    def test_ess_versions_uses_job_type_key_not_software_name(self):
        """7. ess_versions lookup must use job-type keys ('opt'/'freq'/'sp')."""
        # Record with job-type-keyed ess_versions (matches arc/output.py).
        # If the adapter were still using software-name keys, version would be missing.
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["sp_energy_hartree"] = -154.5
        record["ess_versions"] = {
            "opt": "OPT_VER",
            "freq": "FREQ_VER",
            "sp": "SP_VER",
        }
        _, _, payload = self._submit(output_doc=_fake_output_doc(), record=record)
        self.assertEqual(payload["calculation"]["software_release"]["version"], "OPT_VER")
        freq, sp = payload["additional_calculations"]
        self.assertEqual(freq["software_release"]["version"], "FREQ_VER")
        self.assertEqual(sp["software_release"]["version"], "SP_VER")

    def test_idempotency_key_changes_when_freq_sp_added(self):
        """Adding freq/sp to a previously opt-only record must change the idempotency key."""
        record_opt_only = _fake_record()
        record_full = _fake_record()
        record_full["freq_n_imag"] = 0
        record_full["zpe_hartree"] = 0.024
        record_full["sp_energy_hartree"] = -154.5
        o1, _, _ = self._submit(output_doc=_fake_output_doc(), record=record_opt_only)
        o2, _, _ = self._submit(output_doc=_fake_output_doc(), record=record_full)
        self.assertNotEqual(o1.idempotency_key, o2.idempotency_key)

    # ------------------------------------------------------------------
    # SP origin metadata: reused-from-opt vs independently executed.
    # ------------------------------------------------------------------

    def test_sp_distinct_level_has_no_reused_marker(self):
        """sp_level differing from opt_level → no tckdb_origin marker on SP."""
        doc = _fake_output_doc()
        doc["sp_level"] = {"method": "ccsd(t)-f12a", "basis": "cc-pvtz-f12", "software": "molpro"}
        record = _fake_record()
        record["sp_energy_hartree"] = -154.987
        record["ess_versions"] = {"opt": "Gaussian 16, Revision A.03", "sp": "Molpro 2022.3"}
        _, _, payload = self._submit(output_doc=doc, record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        self.assertNotIn(
            "parameters_json", sp,
            msg="distinct sp_level must not produce a tckdb_origin marker",
        )

    def test_sp_equal_level_marks_reused(self):
        """sp_level structurally equal to opt_level → SP carries tckdb_origin=reused_result."""
        doc = _fake_output_doc()
        # Same dict contents as opt_level — explicit duplication, not just absence.
        doc["sp_level"] = dict(doc["opt_level"])
        record = _fake_record()
        record["sp_energy_hartree"] = -154.123
        _, _, payload = self._submit(output_doc=doc, record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        origin = sp["parameters_json"]["tckdb_origin"]
        self.assertEqual(origin["origin_kind"], "reused_result")
        self.assertFalse(origin["independent_ess_job"])
        self.assertEqual(origin["reused_from"]["calculation_type"], "opt")
        self.assertEqual(origin["producer"], "ARC")

    def test_sp_missing_level_falls_back_and_marks_reused(self):
        """Missing sp_level (the common ARC case) is treated as equal-to-opt → reused."""
        doc = _fake_output_doc()  # no sp_level key
        record = _fake_record()
        record["sp_energy_hartree"] = -154.5
        _, _, payload = self._submit(output_doc=doc, record=record)
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")
        origin = sp["parameters_json"]["tckdb_origin"]
        self.assertEqual(origin["origin_kind"], "reused_result")
        self.assertEqual(origin["reused_from"]["calculation_type"], "opt")
        self.assertFalse(origin["independent_ess_job"])

    def test_opt_freq_reused_sp_keeps_per_calc_invariants(self):
        """opt + freq + reused SP: primary unchanged, each row has only its own result."""
        doc = _fake_output_doc()  # only opt_level → sp falls back, freq falls back
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["zpe_hartree"] = 0.024131
        record["sp_energy_hartree"] = -154.5
        _, _, payload = self._submit(output_doc=doc, record=record)

        # Primary opt unchanged: still type=opt, has opt_result, no parameters_json.
        primary = payload["calculation"]
        self.assertEqual(primary["type"], "opt")
        self.assertIn("opt_result", primary)
        self.assertNotIn("freq_result", primary)
        self.assertNotIn("sp_result", primary)
        self.assertNotIn("parameters_json", primary)

        freq = next(c for c in payload["additional_calculations"] if c["type"] == "freq")
        sp = next(c for c in payload["additional_calculations"] if c["type"] == "sp")

        # freq has only freq_result and no origin marker.
        self.assertIn("freq_result", freq)
        self.assertNotIn("sp_result", freq)
        self.assertNotIn("opt_result", freq)
        self.assertNotIn("parameters_json", freq)

        # sp has only sp_result and the reused-result marker.
        self.assertIn("sp_result", sp)
        self.assertNotIn("freq_result", sp)
        self.assertNotIn("opt_result", sp)
        self.assertEqual(
            sp["parameters_json"]["tckdb_origin"]["origin_kind"], "reused_result"
        )

    def test_no_sp_energy_no_sp_additional_calculation(self):
        """No sp_energy_hartree on the record → no SP row, regardless of sp_level."""
        doc = _fake_output_doc()
        doc["sp_level"] = {"method": "ccsd(t)-f12a", "basis": "cc-pvtz-f12", "software": "molpro"}
        record = _fake_record()  # no sp_energy_hartree
        _, _, payload = self._submit(output_doc=doc, record=record)
        types = [c["type"] for c in payload.get("additional_calculations", [])]
        self.assertNotIn("sp", types)


class TestMalformedAdditionalCalcFields(unittest.TestCase):
    """6. Malformed optional freq/sp fields skip the calc with a warning."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
        )

    def _record_with_opt_freq_sp(self):
        record = _fake_record()
        record["freq_n_imag"] = 0
        record["zpe_hartree"] = 0.0399
        record["sp_energy_hartree"] = -154.5
        return record

    def _submit(self, record):
        client = _StubClient(response=_StubResponse({"id": 1}))
        adapter = TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_from_output(
                output_doc=_fake_output_doc(), species_record=record
            )
        return outcome, json.loads(outcome.payload_path.read_text())

    def test_malformed_freq_field_skips_freq_but_keeps_opt_and_sp(self):
        """Malformed freq value drops the freq calc only; opt + sp survive."""
        record = self._record_with_opt_freq_sp()
        record["zpe_hartree"] = "not_a_number"

        with self.assertLogs("tckdb_arc", level="WARNING") as logs:
            outcome, payload = self._submit(record)

        self.assertEqual(outcome.status, "uploaded")
        # Primary opt calculation present and intact
        self.assertEqual(payload["calculation"]["type"], "opt")
        self.assertIn("opt_result", payload["calculation"])
        # freq dropped, sp preserved
        additional = payload.get("additional_calculations", [])
        types = [c["type"] for c in additional]
        self.assertNotIn("freq", types)
        self.assertIn("sp", types)
        # Warning names the calc type and the offending field
        self.assertTrue(
            any("freq" in m and "zpe_hartree" in m for m in logs.output),
            msg=f"expected warning naming freq/zpe_hartree; got {logs.output}",
        )

    def test_malformed_sp_field_skips_sp_but_keeps_opt_and_freq(self):
        """Malformed sp value drops the sp calc only; opt + freq survive."""
        record = self._record_with_opt_freq_sp()
        record["sp_energy_hartree"] = "not-a-float"

        with self.assertLogs("tckdb_arc", level="WARNING") as logs:
            outcome, payload = self._submit(record)

        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(payload["calculation"]["type"], "opt")
        self.assertIn("opt_result", payload["calculation"])
        additional = payload.get("additional_calculations", [])
        types = [c["type"] for c in additional]
        self.assertIn("freq", types)
        self.assertNotIn("sp", types)
        self.assertTrue(
            any("sp" in m and "sp_energy_hartree" in m for m in logs.output),
            msg=f"expected warning naming sp/sp_energy_hartree; got {logs.output}",
        )

    def test_malformed_harmonic_frequency_skips_freq_calc(self):
        """A non-numeric entry in statmech.harmonic_frequencies_cm1 drops
        the whole freq calc with a warning; opt + sp survive."""
        record = self._record_with_opt_freq_sp()
        record["statmech"] = {"harmonic_frequencies_cm1": [800.0, "not_a_number", 1500.0]}

        with self.assertLogs("tckdb_arc", level="WARNING") as logs:
            outcome, payload = self._submit(record)

        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(payload["calculation"]["type"], "opt")
        types = [c["type"] for c in payload.get("additional_calculations", [])]
        self.assertNotIn("freq", types)
        self.assertIn("sp", types)
        self.assertTrue(
            any("freq" in m and "harmonic_frequencies_cm1" in m for m in logs.output),
            msg=f"expected warning naming freq/harmonic_frequencies_cm1; got {logs.output}",
        )

    def test_both_malformed_yields_two_warnings_and_no_additional_calcs(self):
        """Malformed freq AND sp: two warnings, payload has only opt."""
        record = self._record_with_opt_freq_sp()
        record["zpe_hartree"] = "not_a_number"
        record["sp_energy_hartree"] = "not-a-float"

        with self.assertLogs("tckdb_arc", level="WARNING") as logs:
            outcome, payload = self._submit(record)

        self.assertEqual(outcome.status, "uploaded")
        self.assertEqual(payload["calculation"]["type"], "opt")
        # Either no key, or empty list — both are "no additional calcs"
        self.assertEqual(payload.get("additional_calculations", []), [])

        freq_warnings = [m for m in logs.output if "freq" in m and "zpe_hartree" in m]
        sp_warnings = [m for m in logs.output if "sp" in m and "sp_energy_hartree" in m]
        self.assertEqual(
            len(freq_warnings), 1,
            msg=f"expected exactly one freq warning; got {logs.output}",
        )
        self.assertEqual(
            len(sp_warnings), 1,
            msg=f"expected exactly one sp warning; got {logs.output}",
        )


# ---------------------------------------------------------------------------
# Computed-species bundle (POST /uploads/computed-species)
# ---------------------------------------------------------------------------


_FORBIDDEN_BUNDLE_ID_FIELDS = {
    "existing_calculation_id",
    "existing_conformer_id",
    "existing_conformer_observation_id",
    "existing_species_entry_id",
    "source_calculation_id",
    "source_conformer_observation_id",
}


def _walk_for_keys(value, keys):
    """Yield every (path, key) pair in a JSON-like value matching ``keys``."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in keys:
                yield k
            yield from _walk_for_keys(v, keys)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_for_keys(v, keys)


def _full_record():
    """A species record with opt+freq+sp results plus thermo populated."""
    record = _fake_record()
    record["freq_n_imag"] = 0
    record["zpe_hartree"] = 0.024131
    record["sp_energy_hartree"] = -154.987
    record["thermo"] = {
        "h298_kj_mol": -235.1,
        "s298_j_mol_k": 282.6,
        "tmin_k": 100.0,
        "tmax_k": 5000.0,
        # Current ARC records the standard state RMG's partition function
        # used (arc/output.py ``_thermo_to_dict``); older output does not,
        # and the adapter then omits reference_pressure_bar.
        "standard_state_pressure_pa": 101325.0,
        "nasa_low": {
            "tmin_k": 100.0,
            "tmax_k": 1000.0,
            "coeffs": [4.0, -1e-3, 2e-6, -1e-9, 4e-13, -29000.0, 1.0],
        },
        "nasa_high": {
            "tmin_k": 1000.0,
            "tmax_k": 5000.0,
            "coeffs": [3.5, 1e-3, -2e-7, 1e-11, -3e-15, -28500.0, 5.0],
        },
        "thermo_points": [
            {"temperature_k": 300.0, "cp_j_mol_k": 33.6,
             "h_kj_mol": -230.5, "s_j_mol_k": 285.1, "g_kj_mol": -315.9},
            {"temperature_k": 400.0, "cp_j_mol_k": 35.2,
             "h_kj_mol": -227.1, "s_j_mol_k": 295.3, "g_kj_mol": -345.2},
            {"temperature_k": 500.0, "cp_j_mol_k": 37.0,
             "h_kj_mol": -223.5, "s_j_mol_k": 303.4, "g_kj_mol": -375.2},
        ],
    }
    return record


class TestComputedSpeciesBundle(unittest.TestCase):
    """Producer-side tests for the /uploads/computed-species bundle path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-bundle-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )

    def _adapter(self, client, *, project_directory=None, cfg=None):
        return TCKDBAdapter(
            cfg or self.cfg,
            project_directory=project_directory,
            client_factory=lambda c, k: client,
        )

    def _submit(self, *, output_doc=None, record=None, client=None,
                project_directory=None, cfg=None):
        client = client or _StubClient(response=_StubResponse({
            "species_entry_id": 7,
            "conformers": [{
                "key": "conf0",
                "conformer_group_id": 3,
                "conformer_observation_id": 11,
                "primary_calculation": {"key": "opt", "calculation_id": 100, "type": "opt", "role": "primary"},
                "additional_calculations": [
                    {"key": "freq", "calculation_id": 101, "type": "freq", "role": "additional"},
                    {"key": "sp", "calculation_id": 102, "type": "sp", "role": "additional"},
                ],
            }],
            "thermo": {"thermo_id": 9},
        }))
        adapter = self._adapter(client, project_directory=project_directory, cfg=cfg)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=output_doc or _fake_output_doc(),
                species_record=record or _full_record(),
            )
        return outcome, client, json.loads(outcome.payload_path.read_text())

    # ---------------- 1: payload contains species_entry + one conformer
    def test_payload_contains_species_entry_and_one_conformer(self):
        # Default fixture has no screened-conformer list on the species
        # record, so the bundle still emits exactly one conformer (the
        # selected one). This guards backward compat: payloads from
        # older output.yml files (no ``conformers`` field) hash and ship
        # identically to before the multi-conformer change landed.
        _, _, payload = self._submit()
        self.assertIn("species_entry", payload)
        self.assertEqual(payload["species_entry"]["smiles"], "CCO")
        self.assertEqual(payload["species_entry"]["charge"], 0)
        self.assertEqual(payload["species_entry"]["multiplicity"], 1)
        self.assertEqual(len(payload["conformers"]), 1)
        self.assertEqual(payload["conformers"][0]["key"], "conf0")

    def test_species_entry_does_not_assert_an_electronic_state(self):
        """ARC does not state the electronic state (it computes the
        multiplicity it was given, which need not be the ground state), so
        ``electronic_state_kind`` is omitted and TCKDB applies its own
        default (maintainer decision, adapter 0.6.0)."""
        _, _, payload = self._submit()
        self.assertNotIn("electronic_state_kind", payload["species_entry"])

    def test_species_entry_omits_unsupported_identity_fields(self):
        """ARC has no honest source for stereo_label, electronic_state_label,
        term_symbol[_raw], or isotopologue_label. They must be omitted (or
        null) so the TCKDB dedupe key (which uses nulls_not_distinct) keeps
        rows from fragmenting on guessed labels."""
        _, _, payload = self._submit()
        entry = payload["species_entry"]
        for absent in (
            "stereo_label",
            "electronic_state_label",
            "term_symbol",
            "term_symbol_raw",
            "isotopologue_label",
        ):
            self.assertNotIn(absent, entry)

    # ---------------- unmapped_smiles passthrough on species_entry
    def test_species_entry_emits_unmapped_smiles_when_distinct(self):
        # When a producer surfaces an explicit ``unmapped_smiles`` that
        # differs from the main ``smiles`` (e.g. main is an atom-mapped
        # form, unmapped is the canonical), forward both. Adapter does
        # NOT derive — it only passes through what's already present.
        record = _full_record()
        record["smiles"] = "[CH3:1][CH2:2][OH:3]"  # mapped
        record["unmapped_smiles"] = "CCO"           # canonical
        _, _, payload = self._submit(record=record)
        entry = payload["species_entry"]
        self.assertEqual(entry["smiles"], "[CH3:1][CH2:2][OH:3]")
        self.assertEqual(entry["unmapped_smiles"], "CCO")

    def test_species_entry_omits_unmapped_smiles_when_absent(self):
        # Default fixture has no unmapped_smiles → field omitted.
        # Backward-compat for older output.yml files.
        _, _, payload = self._submit()
        self.assertNotIn("unmapped_smiles", payload["species_entry"])

    def test_species_entry_omits_unmapped_smiles_when_empty_or_whitespace(self):
        # Defensive: a producer that wrote "" or whitespace must not
        # yield ``unmapped_smiles: ""`` on the wire.
        for raw in ("", "   ", "\n"):
            with self.subTest(value=repr(raw)):
                record = _full_record()
                record["unmapped_smiles"] = raw
                _, _, payload = self._submit(record=record)
                self.assertNotIn("unmapped_smiles", payload["species_entry"])

    def test_species_entry_omits_unmapped_smiles_when_identical_to_smiles(self):
        # When ARC's mol.to_smiles() happens to be already canonical
        # (the typical case), a producer that copies it into both
        # slots must not produce a duplicate on the wire — same
        # string in two fields adds no identity information.
        record = _full_record()
        record["unmapped_smiles"] = record["smiles"]
        _, _, payload = self._submit(record=record)
        self.assertNotIn("unmapped_smiles", payload["species_entry"])

    def test_species_entry_omits_unmapped_smiles_when_non_string(self):
        # Defensive: hand-edited records with a non-string value
        # (None, list, dict) must omit rather than crash. The adapter
        # never str()-coerces an arbitrary object — that's brittle
        # chemistry inference.
        for raw in (None, ["CCO"], {"value": "CCO"}, 42):
            with self.subTest(value=raw):
                record = _full_record()
                record["unmapped_smiles"] = raw
                _, _, payload = self._submit(record=record)
                self.assertNotIn("unmapped_smiles", payload["species_entry"])

    def test_species_entry_unmapped_smiles_strips_whitespace(self):
        # Light normalization only — ``strip()``. No regex, no atom-map
        # stripping, no canonicalization.
        record = _full_record()
        record["smiles"] = "[CH3:1][CH2:2][OH:3]"
        record["unmapped_smiles"] = "  CCO  "
        _, _, payload = self._submit(record=record)
        self.assertEqual(payload["species_entry"]["unmapped_smiles"], "CCO")

    # ---------------- 2: primary opt maps correctly
    def test_primary_opt_calculation_maps_correctly(self):
        _, _, payload = self._submit()
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertEqual(primary["key"], "opt")
        self.assertEqual(primary["type"], "opt")
        self.assertEqual(primary["quality"], "raw")
        self.assertEqual(primary["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(primary["software_release"]["name"], "gaussian")
        self.assertIn("opt_result", primary)
        self.assertEqual(primary["opt_result"]["n_steps"], 12)

    # ---------------- 2a: opt_converged → opt_result.converged
    def test_opt_converged_true_maps_to_opt_result_converged(self):
        """``opt_converged: True`` on the record (from output.py:541) must
        land as ``opt_result.converged: true`` so calc_opt_result.converged
        isn't NULL in the DB for known-good runs."""
        record = _full_record()
        record["opt_converged"] = True
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertIs(primary["opt_result"]["converged"], True)

    def test_opt_converged_false_maps_through_unchanged(self):
        """A failed-to-converge opt should still upload, with
        ``converged: false`` so the downstream consumer sees the truth."""
        record = _full_record()
        record["opt_converged"] = False
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertIs(primary["opt_result"]["converged"], False)

    def test_opt_converged_absent_omits_field(self):
        """Records that don't carry opt_converged (older output.yml or
        non-converged species) should produce an opt_result without the
        ``converged`` key — not ``converged: null`` — so the schema's
        default kicks in cleanly."""
        record = _full_record()
        record.pop("opt_converged", None)  # ensure absent
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("converged", primary["opt_result"])

    def test_opt_converged_none_omits_field(self):
        """Explicit None on the record (treated as absent) → key omitted."""
        record = _full_record()
        record["opt_converged"] = None
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("converged", primary["opt_result"])

    # ---------------- 2b: opt_input_xyz → opt.input_geometries
    def test_opt_input_xyz_attaches_as_input_geometry(self):
        """``opt_input_xyz`` from output.yml lands as
        ``primary_calculation.input_geometries[0].xyz_text``."""
        record = _full_record()
        record["opt_input_xyz"] = "C 0.001 0.000 0.000\nH 1.090 0.000 0.000"
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertIn("input_geometries", primary)
        self.assertEqual(len(primary["input_geometries"]), 1)
        # The atom-only string gets normalized to TCKDB's standard
        # "<n>\n<comment>\n<atoms>" format inside the bundle.
        text = primary["input_geometries"][0]["xyz_text"]
        self.assertEqual(text.splitlines()[0].strip(), "2")  # atom count header

    def test_freq_and_sp_input_geometries_set_to_conformer_geom(self):
        """Freq + sp now explicitly declare the conformer's optimized
        geometry as their input. ARC's invariant guarantees they ran
        on this geometry; explicit-over-implicit removes ambiguity for
        consumers that don't replicate TCKDB's auto-fill rule."""
        _, _, payload = self._submit()
        conformer_xyz = payload["conformers"][0]["geometry"]["xyz_text"]
        for calc in payload["conformers"][0]["additional_calculations"]:
            self.assertIn(
                "input_geometries", calc,
                msg=f"{calc['key']} should explicitly declare input_geometries "
                "set to the conformer's optimized geometry",
            )
            self.assertEqual(len(calc["input_geometries"]), 1)
            self.assertEqual(
                calc["input_geometries"][0]["xyz_text"], conformer_xyz,
                msg=f"{calc['key']}'s input geometry must match the conformer geometry",
            )

    def test_opt_input_xyz_does_not_leak_into_freq_sp(self):
        """Even when the record carries ``opt_input_xyz``, freq + sp
        must use the conformer (optimized) geometry, NOT the pre-opt
        xyz. They genuinely ran on the optimized output."""
        record = _full_record()
        record["opt_input_xyz"] = "C 9.999 9.999 9.999\nH 8.888 8.888 8.888"  # distinctive
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        # Opt has the pre-opt xyz.
        self.assertIn("9.999", primary["input_geometries"][0]["xyz_text"])
        # Freq + sp do NOT — their input is the conformer (post-opt) geom.
        for calc in payload["conformers"][0]["additional_calculations"]:
            self.assertNotIn(
                "9.999", calc["input_geometries"][0]["xyz_text"],
                msg=f"{calc['key']}'s input geometry must not be the pre-opt xyz",
            )

    def test_opt_input_xyz_omitted_when_absent_no_fallback_to_optimized(self):
        """No ``opt_input_xyz`` on the record → no ``input_geometries``
        on opt's calc block. We must NOT silently substitute the
        conformer (optimized) geometry for opt — that's opt's output,
        not its input. Backend has no auto-fill for opt either, so the
        ``calculation_input_geometry`` row stays absent for that calc.
        Honest-empty beats wrong-link."""
        record = _full_record()
        record.pop("opt_input_xyz", None)  # ensure absent
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("input_geometries", primary)
        # Sanity: freq/sp still get explicit conformer geom even when
        # opt's input is unknown.
        for calc in payload["conformers"][0]["additional_calculations"]:
            self.assertIn("input_geometries", calc)

    def test_opt_input_xyz_preserves_existing_count_header(self):
        """If output.yml ever ships a properly-headered xyz, pass it
        through untouched rather than double-headering it."""
        record = _full_record()
        record["opt_input_xyz"] = "2\nethanol\nC 0.001 0.000 0.000\nH 1.090 0.000 0.000"
        _, _, payload = self._submit(record=record)
        text = payload["conformers"][0]["primary_calculation"]["input_geometries"][0]["xyz_text"]
        # Already-headered → identical
        self.assertEqual(text.splitlines()[0].strip(), "2")
        self.assertEqual(text.splitlines()[1].strip(), "ethanol")

    def test_no_db_ids_in_input_geometries(self):
        """``input_geometries`` entries must carry only ``xyz_text``,
        never ``geometry_id`` / ``existing_geometry_id`` / etc.
        (DR-0029: bundles are self-contained, local data only)."""
        record = _full_record()
        record["opt_input_xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0"
        _, _, payload = self._submit(record=record)
        forbidden = {"geometry_id", "existing_geometry_id", "id"}
        primary = payload["conformers"][0]["primary_calculation"]
        for ig in primary.get("input_geometries", []):
            self.assertEqual(set(ig.keys()) & forbidden, set(),
                             msg=f"opt input_geometry has DB id key(s): {ig}")
        for calc in payload["conformers"][0]["additional_calculations"]:
            for ig in calc.get("input_geometries", []):
                self.assertEqual(set(ig.keys()) & forbidden, set(),
                                 msg=f"{calc['key']} input_geometry has DB id key(s): {ig}")

    # ------------------------------------------------------------------
    # Coarse → fine optimization provenance.
    # ------------------------------------------------------------------

    def _coarse_record(self):
        """A record that mirrors a real two-stage opt: coarse log + parsed
        coarse output xyz, fine opt fields populated, freq + sp + thermo
        all present so the bundle exercises the full chain."""
        record = _full_record()
        record["coarse_opt_log"] = "calcs/.../coarse/input.log"
        record["coarse_opt_n_steps"] = 8
        record["coarse_opt_final_energy_hartree"] = -154.108
        record["coarse_opt_input_xyz"] = (
            "C 9.999 9.999 9.999\nH 8.888 8.888 8.888"  # distinctive pre-coarse
        )
        record["coarse_opt_output_xyz"] = (
            "C 1.111 2.222 3.333\nH 4.444 5.555 6.666"  # distinctive coarse output
        )
        # opt_input_xyz now means "fine opt's input" = coarse output.
        record["opt_input_xyz"] = record["coarse_opt_output_xyz"]
        return record

    # ---------------- spec test 1: single-stage unchanged
    def test_single_stage_opt_emits_no_opt_coarse(self):
        record = _full_record()
        # Sanity: no coarse_opt_log on this record.
        self.assertNotIn("coarse_opt_log", record)
        _, _, payload = self._submit(record=record)
        keys = [c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        self.assertNotIn("opt_coarse", keys)
        # Primary remains opt; no optimized_from edge.
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertEqual(primary["key"], "opt")
        self.assertNotIn("depends_on", primary)

    # ---------------- spec test 2: coarse + fine emits two opt calcs
    def test_coarse_plus_fine_emits_two_opt_calcs_primary_is_fine(self):
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        additional_keys = [c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        # Primary is the FINE opt — geometry of record.
        self.assertEqual(primary["key"], "opt")
        self.assertEqual(primary["type"], "opt")
        # opt_coarse is an additional calc, alongside freq + sp.
        self.assertIn("opt_coarse", additional_keys)
        self.assertIn("freq", additional_keys)
        self.assertIn("sp", additional_keys)
        # Type of opt_coarse is also "opt" (it's an opt-stage calc).
        opt_coarse = next(c for c in payload["conformers"][0]["additional_calculations"]
                          if c["key"] == "opt_coarse")
        self.assertEqual(opt_coarse["type"], "opt")

    # ---------------- spec test 3: geometry chain is correct
    def test_coarse_geometry_chain_is_correct(self):
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        opt_coarse = next(c for c in payload["conformers"][0]["additional_calculations"]
                          if c["key"] == "opt_coarse")
        # opt_coarse's input is the species' truly-initial xyz.
        self.assertIn("9.999", opt_coarse["input_geometries"][0]["xyz_text"])
        # The fine opt's input is the COARSE output, not the pre-coarse xyz.
        self.assertIn("1.111", primary["input_geometries"][0]["xyz_text"])
        self.assertNotIn("9.999", primary["input_geometries"][0]["xyz_text"])
        # Conformer geometry is the fine opt's output (= record["xyz"]).
        conformer_xyz = payload["conformers"][0]["geometry"]["xyz_text"]
        # _full_record xyz is "C 0.0 0.0 0.0\nH 1.0 0.0 0.0" — neither
        # the coarse-input nor coarse-output coords appear.
        self.assertNotIn("9.999", conformer_xyz)
        self.assertNotIn("1.111", conformer_xyz)

    # ---------------- spec test 4: dependency edges
    def test_coarse_dependency_edges_are_correct(self):
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        # opt → opt_coarse with role optimized_from.
        self.assertEqual(
            primary["depends_on"],
            [{"parent_calculation_key": "opt_coarse", "role": "optimized_from"}],
        )
        # freq → opt with role freq_on.
        freq = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["key"] == "freq")
        self.assertEqual(
            freq["depends_on"],
            [{"parent_calculation_key": "opt", "role": "freq_on"}],
        )
        # sp → opt with role single_point_on.
        sp = next(c for c in payload["conformers"][0]["additional_calculations"]
                  if c["key"] == "sp")
        self.assertEqual(
            sp["depends_on"],
            [{"parent_calculation_key": "opt", "role": "single_point_on"}],
        )
        # opt_coarse has no upstream calc (chain head).
        opt_coarse = next(c for c in payload["conformers"][0]["additional_calculations"]
                          if c["key"] == "opt_coarse")
        self.assertNotIn("depends_on", opt_coarse)

    # ---------------- spec test 5: thermo source links exclude opt_coarse
    def test_coarse_thermo_source_calculations_exclude_opt_coarse(self):
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        sources = payload["thermo"]["source_calculations"]
        keys = [s["calculation_key"] for s in sources]
        # opt + freq + sp are sources. opt_coarse is upstream provenance,
        # not a direct thermo source.
        self.assertEqual(keys, ["opt", "freq", "sp"])
        self.assertNotIn("opt_coarse", keys)

    # ---------------- spec test 6: unparseable coarse → fall back safely
    def test_unparseable_coarse_geometry_falls_back_to_single_stage_bundle(self):
        record = self._coarse_record()
        record["coarse_opt_output_xyz"] = None  # parse failure shape from output.py
        # Fine opt's input would have come from coarse output; if absent,
        # the producer fell back so opt_input_xyz = the species' truly
        # initial xyz. Mirror that here.
        record["opt_input_xyz"] = "C 0.001 0.000 0.000\nH 1.090 0.000 0.000"
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        additional_keys = [c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        # No opt_coarse calc — would have been half-described without
        # the output geometry to chain.
        self.assertNotIn("opt_coarse", additional_keys)
        # Fine opt's depends_on stays empty (no optimized_from edge) —
        # we wouldn't introduce a dangling edge to a non-existent calc.
        self.assertNotIn("depends_on", primary)
        # Bundle still validates as a single-stage opt+freq+sp.
        self.assertEqual(primary["key"], "opt")
        self.assertIn("freq", additional_keys)
        self.assertIn("sp", additional_keys)

    # ---------------- coarse opt's result block carries n_steps / energy
    def test_coarse_opt_result_block_carries_coarse_metrics(self):
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        opt_coarse = next(c for c in payload["conformers"][0]["additional_calculations"]
                          if c["key"] == "opt_coarse")
        self.assertIn("opt_result", opt_coarse)
        self.assertEqual(opt_coarse["opt_result"]["n_steps"], 8)
        self.assertAlmostEqual(opt_coarse["opt_result"]["final_energy_hartree"], -154.108)
        # Convergence is implicit "ran to completion" — output.py only
        # writes coarse_opt_log on the success path.
        self.assertIs(opt_coarse["opt_result"]["converged"], True)

    # ---------------- determinism: same inputs → same idempotency key
    def test_coarse_bundle_idempotency_key_is_deterministic(self):
        out1, _, _ = self._submit(record=self._coarse_record())
        out2, _, _ = self._submit(record=self._coarse_record())
        self.assertEqual(out1.idempotency_key, out2.idempotency_key)
        # And distinct from the single-stage bundle's key.
        out3, _, _ = self._submit(record=_full_record())
        self.assertNotEqual(out1.idempotency_key, out3.idempotency_key)

    # ------------------------------------------------------------------
    # output_geometries (TCKDB-side support landed; ARC declares them
    # explicitly for opt + opt_coarse; freq/sp omit by design).
    # ------------------------------------------------------------------

    def test_single_stage_opt_emits_output_geometries_with_final_role(self):
        """Single-stage opt's output_geometries[0] = conformer xyz, role=final."""
        _, _, payload = self._submit()
        primary = payload["conformers"][0]["primary_calculation"]
        conformer_xyz = payload["conformers"][0]["geometry"]["xyz_text"]
        self.assertIn("output_geometries", primary)
        self.assertEqual(len(primary["output_geometries"]), 1)
        entry = primary["output_geometries"][0]
        self.assertEqual(entry["geometry"]["xyz_text"], conformer_xyz)
        self.assertEqual(entry["role"], "final")

    def test_coarse_opt_output_geometry_is_coarse_output_xyz(self):
        """opt_coarse's output_geometries[0] = parsed coarse output xyz, role=final."""
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        opt_coarse = next(c for c in payload["conformers"][0]["additional_calculations"]
                          if c["key"] == "opt_coarse")
        self.assertIn("output_geometries", opt_coarse)
        self.assertEqual(len(opt_coarse["output_geometries"]), 1)
        entry = opt_coarse["output_geometries"][0]
        # Distinctive coords from _coarse_record's coarse_opt_output_xyz
        # ("C 1.111 2.222 3.333\n...") — must match.
        self.assertIn("1.111", entry["geometry"]["xyz_text"])
        self.assertEqual(entry["role"], "final")

    def test_coarse_plus_fine_opt_output_geometry_is_conformer_xyz(self):
        """In a coarse+fine bundle, the FINE opt's output is still the
        conformer geometry of record (= ``xyz``), not the coarse output."""
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        primary = payload["conformers"][0]["primary_calculation"]
        conformer_xyz = payload["conformers"][0]["geometry"]["xyz_text"]
        self.assertEqual(len(primary["output_geometries"]), 1)
        entry = primary["output_geometries"][0]
        self.assertEqual(entry["geometry"]["xyz_text"], conformer_xyz)
        self.assertEqual(entry["role"], "final")
        # Cross-check: fine opt's output ≠ coarse opt's output xyz
        # (the coarse-output coords from the record).
        self.assertNotIn("1.111", entry["geometry"]["xyz_text"])

    def test_freq_and_sp_have_no_output_geometries_by_default(self):
        """freq/sp don't move atoms — we don't claim a separate output
        geometry. Backend's new contract drops the auto-fallback for
        these calcs, so omission means zero output rows server-side."""
        _, _, payload = self._submit()
        for calc in payload["conformers"][0]["additional_calculations"]:
            self.assertNotIn(
                "output_geometries", calc,
                msg=f"{calc['key']} unexpectedly carries output_geometries",
            )

    def test_no_db_ids_in_output_geometries(self):
        """Each output_geometries entry must carry only ``geometry``
        (with its own ``xyz_text``) and ``role`` — no DB id keys.
        Same DR-0029 self-containment invariant as input_geometries."""
        record = self._coarse_record()
        _, _, payload = self._submit(record=record)
        forbidden = {"geometry_id", "existing_geometry_id", "id", "calculation_id"}

        def _walk_outputs(calc):
            for entry in calc.get("output_geometries", []):
                self.assertEqual(set(entry.keys()), {"geometry", "role"},
                                 msg=f"unexpected keys on output entry: {entry}")
                self.assertEqual(set(entry["geometry"].keys()) & forbidden, set(),
                                 msg=f"DB id key in geometry payload: {entry['geometry']}")

        _walk_outputs(payload["conformers"][0]["primary_calculation"])
        for calc in payload["conformers"][0]["additional_calculations"]:
            _walk_outputs(calc)

    def test_output_geometries_idempotency_key_stable(self):
        """Adding ``output_geometries`` doesn't make the idempotency key
        depend on transient state — same record → same key, repeatable."""
        out1, _, _ = self._submit()
        out2, _, _ = self._submit()
        self.assertEqual(out1.idempotency_key, out2.idempotency_key)

    # ---------------- multi-conformer emission (lifted one-conformer cap)
    def _record_with_alt_conformers(
        self,
        *,
        alt_xyzs=("C 0.1 0.0 0.0\nH 1.1 0.0 0.0",
                  "C 0.2 0.0 0.0\nH 1.2 0.0 0.0"),
        energies=(0.0, 1.5, 3.7),
    ):
        """Full record + ``conformers`` list (selected first, then alts).

        The selected geometry must lead the ``conformers`` list because
        ARC's ``most_stable_conformer`` is index 0 of ``species.conformers``
        in the typical case — exercises the dedup path that drops the
        selected xyz from the alt set.
        """
        record = _full_record()
        selected = record["xyz"]
        record["conformers"] = [selected, *alt_xyzs]
        record["conformer_energies"] = list(energies)
        return record

    def test_screened_conformers_are_not_uploaded_and_are_reported(self):
        # ARC screens conformers at its conformer level, which output.yml does
        # not export; a calculation for a screened conformer would have to
        # state a level, and opt_level is not the level the screen ran at
        # (default wb97xd/def2svp against wb97xd/def2tzvp). The 2 distinct
        # screened geometries are omitted and reported; the selected
        # conformer is the only one sent.
        record = self._record_with_alt_conformers()
        outcome, _, payload = self._submit(record=record)
        self.assertEqual([c["key"] for c in payload["conformers"]], ["conf0"])
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "conformer_level_not_stated"]
        self.assertEqual(warning["context"]["omitted_count"], "2")
        self.assertEqual(warning["context"]["action"], "screened_conformers_omitted")
        self.assertIn("conformer level", warning["message"])
        self.assertNotIn("alt0", json.dumps(payload))

    def test_screened_conformers_are_never_labelled_with_the_opt_level(self):
        # The regression this pins: every screened conformer used to carry a
        # primary opt calculation at opt_level. No calculation of the bundle
        # other than the selected conformer's own may exist.
        record = self._record_with_alt_conformers()
        _, _, payload = self._submit(record=record)
        keys = [payload["conformers"][0]["primary_calculation"]["key"]] + [
            c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        self.assertEqual(keys, ["opt", "freq", "sp"])
        self.assertFalse(
            [c for c in payload["conformers"] if c["key"] != "conf0"])

    def test_no_screened_conformer_warning_without_distinct_alternatives(self):
        record = _full_record()
        record["conformers"] = [record["xyz"], record["xyz"]]
        outcome, _, payload = self._submit(record=record)
        self.assertEqual(len(payload["conformers"]), 1)
        self.assertNotIn("conformer_level_not_stated",
                         {w["code"] for w in outcome.warnings})

    def test_multi_conformer_keys_are_deterministic_across_replays(self):
        # Same input → same idempotency key, even with multiple alt
        # conformers in the bundle.
        record = self._record_with_alt_conformers()
        out1, _, payload1 = self._submit(record=record)
        out2, _, payload2 = self._submit(record=record)
        self.assertEqual(out1.idempotency_key, out2.idempotency_key)
        self.assertEqual(
            [c["key"] for c in payload1["conformers"]],
            [c["key"] for c in payload2["conformers"]],
        )

    def test_selected_conformer_keeps_full_provenance(self):
        # Adding alts must not perturb the selected conformer's existing
        # opt/freq/sp wiring or its key.
        record = self._record_with_alt_conformers()
        _, _, payload = self._submit(record=record)
        selected = payload["conformers"][0]
        self.assertEqual(selected["key"], "conf0")
        self.assertEqual(selected["primary_calculation"]["key"], "opt")
        addl_keys = [c["key"] for c in selected["additional_calculations"]]
        self.assertEqual(addl_keys, ["freq", "sp"])
        # Thermo block still references the selected conformer's calc keys
        # (alt conformers never enter the thermo source-calculation set).
        thermo_calc_keys = {
            s["calculation_key"]
            for s in payload["thermo"]["source_calculations"]
        }
        self.assertTrue(thermo_calc_keys.issubset({"opt", "freq", "sp"}))

    def test_every_emitted_origin_kind_is_a_valid_enum_member(self):
        # Schema-conformance guard. The backend hoists
        # ``parameters_json.tckdb_origin.origin_kind`` into the validated
        # ``CalculationWithResultsPayload.origin_kind`` enum, so ANY
        # origin_kind ARC emits — anywhere in the bundle, at any depth —
        # must be one of {executed, reused_result, imported, derived}.
        # This bundle exercises the marker ARC produces today: the
        # reused-result SP row (screened conformers are no longer filed,
        # so no ``derived`` row). A regression that reintroduces a non-enum
        # value (e.g. the historical "screened_conformer") is what triggered
        # the 422.
        from tckdb_arc.adapter import VALID_TCKDB_ORIGIN_KINDS
        record = self._record_with_alt_conformers()
        _, _, payload = self._submit(record=record)
        found = []

        def _walk(obj):
            if isinstance(obj, dict):
                if "origin_kind" in obj:
                    found.append(obj["origin_kind"])
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(payload)
        # The reused-result marker is present and nothing outside the enum
        # leaked through.
        self.assertTrue(found, "expected at least one origin_kind in payload")
        for kind in found:
            self.assertIn(
                kind, VALID_TCKDB_ORIGIN_KINDS,
                f"origin_kind {kind!r} is not a valid backend enum member",
            )
        self.assertIn("reused_result", found)

    def test_no_relative_energy_string_anywhere_in_payload(self):
        # Hard guardrail: even if a future producer or fixture stages
        # conformer_energies on the species record, the substring
        # ``relative_e0_kj_mol`` must never appear ANYWHERE in the
        # emitted multi-conformer payload (key or value, at any depth).
        # Locks down the "do not move into parameters_json" rule too.
        record = self._record_with_alt_conformers(
            energies=(0.0, 2.34, 5.0, 99.9),
        )
        _, _, payload = self._submit(record=record)
        # Serialize and scan — catches the field regardless of where
        # someone might try to reintroduce it.
        self.assertNotIn("relative_e0_kj_mol", json.dumps(payload))

    def test_duplicate_selected_geometry_is_not_emitted_twice(self):
        # The selected geometry typically appears in ``conformers`` too
        # (most_stable_conformer is one of them). The adapter must not
        # double-emit it as both ``conf0`` and ``alt*``.
        record = _full_record()
        record["conformers"] = [record["xyz"], record["xyz"]]
        record["conformer_energies"] = [0.0, 0.0]
        _, _, payload = self._submit(record=record)
        # Selected stays as conf0; both list entries match it → no alts
        # survive the dedup pass.
        self.assertEqual(len(payload["conformers"]), 1)
        self.assertEqual(payload["conformers"][0]["key"], "conf0")

    def test_alt_conformer_invalid_xyz_is_not_counted_as_omitted(self):
        # Empty / malformed xyz entries in ``conformers`` are not conformers
        # ARC screened to a usable geometry: only the one valid distinct
        # candidate is reported as omitted.
        record = self._record_with_alt_conformers(
            alt_xyzs=("", "C 0.3 0.0 0.0\nH 1.3 0.0 0.0", None),
            energies=(0.0, 0.5, 1.0, 2.0),
        )
        outcome, _, payload = self._submit(record=record)
        self.assertEqual(len(payload["conformers"]), 1)
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "conformer_level_not_stated"]
        self.assertEqual(warning["context"]["omitted_count"], "1")

    def test_alt_conformer_energy_data_never_reaches_payload(self):
        # Garbage / misaligned / missing conformer_energies on the
        # source record must never affect the payload — the field is
        # ignored at the adapter seam, regardless of shape or length.
        for energies in [
            (0.0,),                                    # short list
            (0.0, "garbage", None, 1.0),               # mixed types
            ("not-numeric",) * 4,                      # all garbage
            [],                                        # explicitly empty
        ]:
            with self.subTest(energies=energies):
                record = self._record_with_alt_conformers(
                    alt_xyzs=("C 0.4 0.0 0.0\nH 1.4 0.0 0.0",),
                    energies=energies,
                )
                _, _, payload = self._submit(record=record)
                self.assertEqual(len(payload["conformers"]), 1)
                self.assertNotIn("relative_e0_kj_mol",
                                 json.dumps(payload))

    def test_multi_conformer_payload_validates_against_tckdb_schema(self):
        # End-to-end: the multi-conformer payload must satisfy
        # ComputedSpeciesUploadRequest server-side. Catches schema-level
        # mistakes (e.g. an alt opt calc missing a required field) the
        # local fixture asserts don't surface.
        record = self._record_with_alt_conformers()
        _, _, payload = self._submit(record=record)
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_no_conformers_field_preserves_single_conformer_behavior(self):
        # Records without a ``conformers`` field (older output.yml) must
        # produce exactly one conformer block with the existing key.
        record = _full_record()
        self.assertNotIn("conformers", record)
        _, _, payload = self._submit(record=record)
        self.assertEqual(len(payload["conformers"]), 1)
        self.assertEqual(payload["conformers"][0]["key"], "conf0")

    # ---------------- final_settings under parameters_json
    def test_final_settings_emitted_when_present_on_record(self):
        # Producer surfaces calc-specific scientific knobs (optimization
        # stage, grid, symmetry, …) per-job. Adapter must thread them
        # through to ``parameters_json.final_settings`` so the row
        # records what actually defined the calc, distinct from LoT
        # identity. Today only ``optimization_stage`` has a producer-
        # side honest source; the test also exercises an extra key
        # to lock in the pass-through behavior for future producers.
        record = _full_record()
        record["opt_final_settings"] = {
            "optimization_stage": "fine", "grid": "ultrafine",
        }
        record["freq_final_settings"] = {"symmetry": "nosymm"}
        _, _, payload = self._submit(record=record)
        opt = payload["conformers"][0]["primary_calculation"]
        self.assertEqual(
            opt["parameters_json"]["final_settings"],
            {"optimization_stage": "fine", "grid": "ultrafine"},
        )
        freq = payload["conformers"][0]["additional_calculations"][0]
        self.assertEqual(
            freq["parameters_json"]["final_settings"],
            {"symmetry": "nosymm"},
        )

    def test_final_settings_omitted_when_absent_or_empty(self):
        # No ``<role>_final_settings`` on the record → no
        # ``final_settings`` key under ``parameters_json``. We don't
        # check that ``parameters_json`` itself is absent because other
        # writers (e.g. ``tckdb_origin`` on reused-from-opt sp rows)
        # may still populate it independently.
        record = _full_record()
        # Default fixture has no *_final_settings keys.
        _, _, payload = self._submit(record=record)
        for calc in [payload["conformers"][0]["primary_calculation"],
                     *payload["conformers"][0]["additional_calculations"]]:
            pj = calc.get("parameters_json", {})
            self.assertNotIn("final_settings", pj,
                             f"calc {calc.get('key')!r} unexpectedly has "
                             "final_settings")

        # Empty dict → still omitted from parameters_json.
        record["opt_final_settings"] = {}
        _, _, payload = self._submit(record=record)
        opt = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("final_settings", opt.get("parameters_json", {}))

        # Non-mapping → still omitted (defensive).
        record["opt_final_settings"] = "ultrafine"
        _, _, payload = self._submit(record=record)
        opt = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("final_settings", opt.get("parameters_json", {}))

    def test_final_settings_strips_none_valued_keys(self):
        # ARC's producer often writes ``key: None`` for "not known" rather
        # than omitting the key. The adapter must drop those rather than
        # emit ``"grid": null`` which would be a different shape than the
        # equivalent "key absent" payload.
        record = _full_record()
        record["opt_final_settings"] = {
            "optimization_stage": "fine", "grid": None,
        }
        _, _, payload = self._submit(record=record)
        final_settings = (
            payload["conformers"][0]["primary_calculation"]
            ["parameters_json"]["final_settings"]
        )
        self.assertEqual(final_settings, {"optimization_stage": "fine"})

    def test_final_settings_and_screened_origin_coexist_on_alt_opt(self):
        # If a future producer ever populates an alt conformer's
        # final_settings (the adapter today doesn't, but the merge
        # helper must be ready), both ``tckdb_origin`` and
        # ``final_settings`` keys must live side-by-side under
        # ``parameters_json`` — neither shadowing the other.
        from tckdb_arc.adapter import _merge_parameters_json
        merged = _merge_parameters_json(
            tckdb_origin={"origin_kind": "derived",
                          "origin_detail": "screened_conformer",
                          "independent_ess_job": False},
            final_settings={"optimization_stage": "fine"},
        )
        self.assertEqual(set(merged.keys()),
                         {"tckdb_origin", "final_settings"})
        self.assertEqual(merged["tckdb_origin"]["origin_kind"], "derived")
        self.assertEqual(merged["tckdb_origin"]["origin_detail"],
                         "screened_conformer")
        self.assertEqual(merged["final_settings"],
                         {"optimization_stage": "fine"})

    def test_merge_parameters_json_returns_none_when_empty(self):
        # Callers rely on ``None`` to decide whether to omit
        # ``parameters_json`` entirely.
        from tckdb_arc.adapter import _merge_parameters_json
        self.assertIsNone(_merge_parameters_json())
        self.assertIsNone(_merge_parameters_json(tckdb_origin={},
                                                 final_settings={}))

    def test_parameters_json_excludes_operational_fields(self):
        # Hard guardrail: even if a fixture or hand-edited output.yml
        # places operational/scheduler keys (server, queue, job_id,
        # walltime, …) under ``<role>_final_settings``, the resulting
        # ``parameters_json.final_settings`` is what we control, and
        # the adapter must NOT host these. The adapter is intentionally
        # narrow: it forwards whatever the producer puts there
        # verbatim. The contract is enforced producer-side (this test
        # confirms the producer doesn't sneak operational keys in
        # via filesystem-state inference) AND by the explicit field
        # vocabulary in ``_FINAL_SETTINGS_FIELD_BY_CALC_ROLE`` which
        # doesn't include any operational-data role.
        record = _full_record()
        _, _, payload = self._submit(record=record)
        # Sweep the whole payload for any operational field name that
        # has no business landing in a TCKDB row.
        forbidden = {
            "server", "queue", "job_id", "run_time", "walltime",
            "attempted_queues", "ess_trsh_methods", "restart",
            "restart_count", "scratch_path", "local_path",
            "username", "token", "api_key", "password",
        }
        # Payload is JSON-deserialized → mappings are plain dicts.
        def _walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(
                        k, forbidden,
                        f"operational field {k!r} leaked into payload at {path}",
                    )
                    _walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _walk(item, f"{path}[{i}]")
        _walk(payload)

    # ---------------- 3: freq+sp included when fields exist
    def test_freq_and_sp_included_when_fields_exist(self):
        _, _, payload = self._submit()
        keys = [c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        self.assertEqual(keys, ["freq", "sp"])
        freq = payload["conformers"][0]["additional_calculations"][0]
        sp = payload["conformers"][0]["additional_calculations"][1]
        self.assertEqual(freq["freq_result"]["n_imag"], 0)
        self.assertAlmostEqual(freq["freq_result"]["zpe_hartree"], 0.024131)
        self.assertAlmostEqual(sp["sp_result"]["electronic_energy_hartree"], -154.987)

    # ---------------- 4: dependencies point to opt by local key
    def test_dependencies_point_to_opt_by_local_key(self):
        _, _, payload = self._submit()
        freq, sp = payload["conformers"][0]["additional_calculations"]
        self.assertEqual(
            freq["depends_on"],
            [{"parent_calculation_key": "opt", "role": "freq_on"}],
        )
        self.assertEqual(
            sp["depends_on"],
            [{"parent_calculation_key": "opt", "role": "single_point_on"}],
        )
        # Primary opt has no dependencies in the bundle namespace.
        self.assertNotIn("depends_on", payload["conformers"][0]["primary_calculation"])

    # ---------------- 5: missing freq/sp omits calc + dep
    def test_missing_freq_sp_omits_calc_and_dep(self):
        record = _fake_record()  # no freq_*, no sp_*
        _, _, payload = self._submit(record=record)
        self.assertEqual(payload["conformers"][0]["additional_calculations"], [])
        # Thermo also has no freq/sp source links, since neither calc was included.
        self.assertNotIn("thermo", payload)

    # ---------------- 6: thermo scalar fields
    def test_thermo_scalar_fields_map_correctly(self):
        _, _, payload = self._submit()
        thermo = payload["thermo"]
        self.assertAlmostEqual(thermo["h298_kj_mol"], -235.1)
        self.assertAlmostEqual(thermo["s298_j_mol_k"], 282.6)
        self.assertAlmostEqual(thermo["tmin_k"], 100.0)
        self.assertAlmostEqual(thermo["tmax_k"], 5000.0)

    # ---------------- 7: NASA coefficients
    def test_nasa_coefficients_map_correctly(self):
        _, _, payload = self._submit()
        nasa = payload["thermo"]["nasa"]
        self.assertAlmostEqual(nasa["t_low"], 100.0)
        self.assertAlmostEqual(nasa["t_mid"], 1000.0)
        self.assertAlmostEqual(nasa["t_high"], 5000.0)
        self.assertAlmostEqual(nasa["a1"], 4.0)
        self.assertAlmostEqual(nasa["a7"], 1.0)
        self.assertAlmostEqual(nasa["b1"], 3.5)
        self.assertAlmostEqual(nasa["b7"], 5.0)

    # ---------------- 8: malformed NASA skips block, keeps scalar thermo
    def test_malformed_nasa_skips_block_keeps_scalar(self):
        record = _full_record()
        # Drop one coefficient → not 7 → NASA block must be skipped.
        record["thermo"]["nasa_low"]["coeffs"] = [1.0] * 6
        with self.assertLogs("tckdb_arc", level="WARNING") as logs:
            _, _, payload = self._submit(record=record)
        self.assertNotIn("nasa", payload["thermo"])
        # Scalar thermo retained.
        self.assertAlmostEqual(payload["thermo"]["h298_kj_mol"], -235.1)
        self.assertAlmostEqual(payload["thermo"]["s298_j_mol_k"], 282.6)
        # Cp points retained.
        self.assertEqual(len(payload["thermo"]["points"]), 3)
        self.assertTrue(any("NASA block skipped" in m for m in logs.output))

    # ---------------- 9: thermo points (Cp + H + S + G)
    def test_thermo_points_map_correctly(self):
        _, _, payload = self._submit()
        points = payload["thermo"]["points"]
        self.assertEqual([p["temperature_k"] for p in points], [300.0, 400.0, 500.0])
        self.assertAlmostEqual(points[0]["cp_j_mol_k"], 33.6)
        # Enrichment: H/S/G now ride per-point.
        self.assertAlmostEqual(points[0]["h_kj_mol"], -230.5)
        self.assertAlmostEqual(points[0]["s_j_mol_k"], 285.1)
        self.assertAlmostEqual(points[0]["g_kj_mol"], -315.9)
        self.assertAlmostEqual(points[2]["h_kj_mol"], -223.5)
        self.assertAlmostEqual(points[2]["g_kj_mol"], -375.2)

    def test_thermo_points_h_s_g_omitted_when_absent(self):
        """A point that supplies only Cp (e.g., legacy ``cp_data`` rows still
        on disk) must round-trip without inventing zeroes for H/S/G."""
        record = _full_record()
        record["thermo"]["thermo_points"] = [
            {"temperature_k": 300.0, "cp_j_mol_k": 33.6},
        ]
        _, _, payload = self._submit(record=record)
        point = payload["thermo"]["points"][0]
        self.assertEqual(point["temperature_k"], 300.0)
        self.assertAlmostEqual(point["cp_j_mol_k"], 33.6)
        self.assertNotIn("h_kj_mol", point)
        self.assertNotIn("s_j_mol_k", point)
        self.assertNotIn("g_kj_mol", point)

    # ---------------- 10: thermo source links use local keys (full opt/freq/sp triple)
    def test_thermo_source_calculations_use_local_keys(self):
        """All three contributing calcs must be linked: opt (geometry),
        freq (modes/ZPE), sp (electronic energy reference). Order is
        deterministic — opt, freq, sp — so the bundle hashes stably."""
        _, _, payload = self._submit()
        sources = payload["thermo"]["source_calculations"]
        self.assertEqual(
            sources,
            [
                {"calculation_key": "opt", "role": "opt"},
                {"calculation_key": "freq", "role": "freq"},
                {"calculation_key": "sp", "role": "sp"},
            ],
        )

    # ---------------- 10a: subset coverage — opt only
    def test_thermo_source_calculations_opt_only(self):
        """A bundle with only the opt calc (no freq, no sp) must still
        link opt as the thermo source — useful for thermo-from-opt-only
        edge cases (e.g. composite methods that hide freq/sp internally)."""
        record = _full_record()
        # Strip freq/sp signals so only opt + thermo remain on the record.
        for key in ("freq_n_imag", "imag_freq_cm1", "zpe_hartree",
                    "sp_energy_hartree", "electronic_energy_hartree"):
            record.pop(key, None)
        _, _, payload = self._submit(record=record)
        # Sanity: only opt made it into the bundle's calc namespace.
        self.assertEqual(payload["conformers"][0]["additional_calculations"], [])
        sources = payload["thermo"]["source_calculations"]
        self.assertEqual(sources, [{"calculation_key": "opt", "role": "opt"}])

    # ---------------- 10b: subset coverage — opt + freq, no sp
    def test_thermo_source_calculations_opt_plus_freq(self):
        record = _full_record()
        record.pop("sp_energy_hartree", None)
        record.pop("electronic_energy_hartree", None)
        _, _, payload = self._submit(record=record)
        addn_keys = [c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        self.assertEqual(addn_keys, ["freq"])
        sources = payload["thermo"]["source_calculations"]
        self.assertEqual(
            sources,
            [
                {"calculation_key": "opt", "role": "opt"},
                {"calculation_key": "freq", "role": "freq"},
            ],
        )

    # ---------------- 10c: subset coverage — opt + sp, no freq
    def test_thermo_source_calculations_opt_plus_sp(self):
        record = _full_record()
        for key in ("freq_n_imag", "imag_freq_cm1", "zpe_hartree"):
            record.pop(key, None)
        _, _, payload = self._submit(record=record)
        addn_keys = [c["key"] for c in payload["conformers"][0]["additional_calculations"]]
        self.assertEqual(addn_keys, ["sp"])
        sources = payload["thermo"]["source_calculations"]
        self.assertEqual(
            sources,
            [
                {"calculation_key": "opt", "role": "opt"},
                {"calculation_key": "sp", "role": "sp"},
            ],
        )

    # ---------------- 10d: ordering is deterministic — opt, freq, sp
    def test_thermo_source_calculations_ordering_is_deterministic(self):
        """Same record submitted twice produces identical source lists.
        The fixed (opt, freq, sp) order is what makes the bundle's
        idempotency hash stable across runs."""
        out1, _, payload1 = self._submit()
        out2, _, payload2 = self._submit()
        self.assertEqual(
            payload1["thermo"]["source_calculations"],
            payload2["thermo"]["source_calculations"],
        )
        # And that order is exactly opt, freq, sp.
        keys = [s["calculation_key"] for s in payload1["thermo"]["source_calculations"]]
        self.assertEqual(keys, ["opt", "freq", "sp"])
        # Order-stability also means the idempotency keys match.
        self.assertEqual(out1.idempotency_key, out2.idempotency_key)

    # ---------------- 10e: no DB ids in source_calculations
    def test_thermo_source_calculations_have_no_db_ids(self):
        """Each source link must carry only ``calculation_key`` + ``role`` —
        no ``calculation_id`` / ``existing_calculation_id`` / etc., per
        DR-0029 Requirement 1 (bundles are self-contained, local keys only)."""
        _, _, payload = self._submit()
        sources = payload["thermo"]["source_calculations"]
        for src in sources:
            self.assertEqual(
                set(src.keys()), {"calculation_key", "role"},
                msg=f"unexpected keys on thermo source link: {src}",
            )

    # ---------------- 11: payload has no DB ids anywhere
    def test_payload_has_no_existing_or_source_calculation_id(self):
        _, _, payload = self._submit()
        forbidden_hits = list(_walk_for_keys(payload, _FORBIDDEN_BUNDLE_ID_FIELDS))
        self.assertEqual(forbidden_hits, [], msg=f"forbidden DB id keys present: {forbidden_hits}")

    # ---------------- 12: artifacts attach under correct calc when enabled
    def test_artifacts_attach_under_correct_calc_when_enabled(self):
        # Create a project dir with three real log files, configure artifacts on.
        proj = pathlib.Path(self.tmp) / "project"
        proj.mkdir()
        for name in ("opt.log", "freq.log", "sp.log"):
            (proj / name).write_bytes(
                # Fake but valid Gaussian header so any future signature check passes;
                # the producer doesn't validate, but real fakes are cheaper than mocks.
                b" Entering Gaussian System, Link 0\n" + b"x" * 256
            )
        record = _full_record()
        record["opt_log"] = "opt.log"
        record["freq_log"] = "freq.log"
        record["sp_log"] = "sp.log"
        cfg_with_art = TCKDBConfig(
            enabled=True,
            base_url=self.cfg.base_url,
            payload_dir=str(proj / "tckdb_payloads"),
            api_key_env=self.cfg.api_key_env,
            project_label=self.cfg.project_label,
            upload_mode="computed_species",
            artifacts=TCKDBArtifactConfig(upload=True, kinds=("output_log",), max_size_mb=50),
        )
        _, _, payload = self._submit(
            record=record,
            project_directory=str(proj),
            cfg=cfg_with_art,
        )
        primary = payload["conformers"][0]["primary_calculation"]
        freq, sp = payload["conformers"][0]["additional_calculations"]
        self.assertEqual(len(primary["artifacts"]), 1)
        self.assertEqual(primary["artifacts"][0]["kind"], "output_log")
        self.assertEqual(primary["artifacts"][0]["filename"], "opt.log")
        self.assertEqual(len(freq["artifacts"]), 1)
        self.assertEqual(freq["artifacts"][0]["filename"], "freq.log")
        self.assertEqual(len(sp["artifacts"]), 1)
        self.assertEqual(sp["artifacts"][0]["filename"], "sp.log")

    # ---------------- 12c: Cartesian Hessian attaches to the freq calc
    def test_freq_calc_carries_parsed_cartesian_hessian(self):
        pytest.importorskip("arc")  # arc-gated: needs ARC on PYTHONPATH
        """A freq_log with a Gaussian FC block yields an inline ``hessian``.

        Uses the real 2-atom NH Gaussian freq log (21-entry lower triangle,
        native hartree/bohr²). The default record's 2-atom xyz matches, so the
        HessianPayload's 3N(3N+1)/2 invariant holds.
        """
        record = _full_record()
        record["freq_log"] = os.path.join(
            ARC_TESTING_PATH, 'restart', '2_restart_rate', 'calcs',
            'Species', 'NH_freq.out',
        )
        _, _, payload = self._submit(record=record)
        freq = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["type"] == "freq")
        self.assertIn("hessian", freq)
        hessian = freq["hessian"]
        self.assertEqual(hessian["source"], "parsed_log")
        self.assertEqual(hessian["parser_version"], "arc-hessian-1")
        triangle = hessian["lower_triangle_hartree_bohr2"]
        self.assertEqual(len(triangle), 21)  # 2 atoms -> 3N=6 -> 6*7/2
        # Native hartree/bohr²: N-H stretch diagonal ~0.39, never ~1e3 (SI J/m²).
        self.assertLess(max(abs(v) for v in triangle), 5.0)
        # The attached hessian dict validates under the TCKDB schema.
        payload_obj = contract_validate(HessianPayload, hessian)
        self.assertEqual(payload_obj.source, HessianSource.parsed_log)
        # Cartesian Hessians must use their parser frame, independently of
        # the conformer's orientation.
        from tckdb_arc._arc_optional import ess_factory
        from tckdb_arc._vendor import xyz_to_str
        from tckdb_arc.adapter import _normalize_xyz_text
        xyz, frame = ess_factory(record["freq_log"], "gaussian").parse_cartesian_hessian_geometry()
        self.assertEqual(frame, "gaussian_input_orientation")
        self.assertEqual(hessian["geometry"]["xyz_text"], _normalize_xyz_text(xyz_to_str(xyz), record["label"]))

    # ---------------- 12d: absent freq_log => no hessian, never raises
    def test_freq_calc_without_log_has_no_hessian(self):
        """No freq_log on the record => no ``hessian`` key, payload still builds."""
        record = _full_record()
        record.pop("freq_log", None)
        _, _, payload = self._submit(record=record)
        freq = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["type"] == "freq")
        self.assertNotIn("hessian", freq)

    # ---------------- 12e: freq_log without an FC block => no hessian
    def test_freq_calc_log_without_fc_block_has_no_hessian(self):
        pytest.importorskip("arc")  # arc-gated: needs ARC on PYTHONPATH
        """A freq log lacking the FC block (no IOp(7/33=1)) attaches no hessian."""
        record = _full_record()
        # A Q-Chem freq log has no Gaussian FC block and an unsupported ESS
        # for Hessian extraction; the adapter must skip silently.
        record["freq_log"] = os.path.join(
            ARC_TESTING_PATH, 'freq', 'C2H6_freq_QChem.out',
        )
        _, _, payload = self._submit(record=record)
        freq = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["type"] == "freq")
        self.assertNotIn("hessian", freq)

    # ---------------- 13: artifacts disabled = no artifact lists emitted
    def test_artifact_disabled_produces_no_artifacts(self):
        _, _, payload = self._submit()  # default cfg has artifacts.upload=False
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("artifacts", primary)
        for calc in payload["conformers"][0]["additional_calculations"]:
            self.assertNotIn("artifacts", calc)

    # ---------------- 12b: input-deck artifacts inline alongside output_log
    def test_input_artifacts_inline_when_kind_enabled_and_path_recorded(self):
        """``<calc>_input`` from output.yml drives the inline ``input`` artifact."""
        proj = pathlib.Path(self.tmp) / "project_inputs"
        proj.mkdir()
        # Produce all six artifact files: opt log + deck, freq log + deck, sp log + deck.
        # output_log files need a fake ESS header so any future signature check
        # would pass; deck contents are arbitrary text.
        for name in ("opt.log", "freq.log", "sp.log"):
            (proj / name).write_bytes(
                b" Entering Gaussian System, Link 0\n" + b"x" * 256
            )
        for name in ("opt.gjf", "freq.gjf", "sp.gjf"):
            (proj / name).write_text("# opt freq=hpmodes wb97xd/def2tzvp\n")
        record = _full_record()
        record["opt_log"] = "opt.log"
        record["freq_log"] = "freq.log"
        record["sp_log"] = "sp.log"
        # The new schema-extension keys from arc/output.py.
        record["opt_input"] = "opt.gjf"
        record["freq_input"] = "freq.gjf"
        record["sp_input"] = "sp.gjf"
        cfg = TCKDBConfig(
            enabled=True,
            base_url=self.cfg.base_url,
            payload_dir=str(proj / "tckdb_payloads"),
            api_key_env=self.cfg.api_key_env,
            project_label=self.cfg.project_label,
            upload_mode="computed_species",
            artifacts=TCKDBArtifactConfig(
                upload=True, kinds=("output_log", "input"), max_size_mb=50,
            ),
        )
        _, _, payload = self._submit(record=record, project_directory=str(proj), cfg=cfg)
        primary = payload["conformers"][0]["primary_calculation"]
        freq, sp = payload["conformers"][0]["additional_calculations"]
        # Each calc should now have BOTH kinds inlined.
        for calc, expected_files in (
            (primary, ("opt.log", "opt.gjf")),
            (freq,    ("freq.log", "freq.gjf")),
            (sp,      ("sp.log", "sp.gjf")),
        ):
            self.assertEqual(len(calc["artifacts"]), 2,
                             msg=f"{calc['key']}: expected log+input, got {calc['artifacts']}")
            kinds = [a["kind"] for a in calc["artifacts"]]
            self.assertEqual(sorted(kinds), ["input", "output_log"])
            filenames = {a["filename"] for a in calc["artifacts"]}
            self.assertEqual(filenames, set(expected_files))

    def test_input_artifact_omitted_when_kind_not_configured(self):
        """``input`` not in config.artifacts.kinds → only output_log emitted, even if path recorded."""
        proj = pathlib.Path(self.tmp) / "project_no_input_kind"
        proj.mkdir()
        (proj / "opt.log").write_bytes(b" Entering Gaussian System, Link 0\n" + b"x" * 256)
        (proj / "opt.gjf").write_text("# opt\n")
        record = _full_record()
        record["opt_log"] = "opt.log"
        record["opt_input"] = "opt.gjf"   # path is set, but kind isn't enabled
        record.pop("freq_log", None); record.pop("sp_log", None)
        record["freq_n_imag"] = None      # drop freq/sp so we only test the opt calc
        record["zpe_hartree"] = None
        record.pop("sp_energy_hartree", None)
        record.pop("thermo", None)        # avoid thermo's source-link assertions
        cfg = TCKDBConfig(
            enabled=True,
            base_url=self.cfg.base_url,
            payload_dir=str(proj / "tckdb_payloads"),
            api_key_env=self.cfg.api_key_env,
            project_label=self.cfg.project_label,
            upload_mode="computed_species",
            artifacts=TCKDBArtifactConfig(
                upload=True, kinds=("output_log",), max_size_mb=50,
            ),
        )
        _, _, payload = self._submit(record=record, project_directory=str(proj), cfg=cfg)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertEqual(len(primary["artifacts"]), 1)
        self.assertEqual(primary["artifacts"][0]["kind"], "output_log")

    def test_input_artifact_omitted_when_path_field_null(self):
        """``input`` enabled but record has no ``<calc>_input`` → just output_log emitted."""
        proj = pathlib.Path(self.tmp) / "project_input_null"
        proj.mkdir()
        (proj / "opt.log").write_bytes(b" Entering Gaussian System, Link 0\n" + b"x" * 256)
        record = _full_record()
        record["opt_log"] = "opt.log"
        record["opt_input"] = None        # explicitly None — file wasn't kept
        record.pop("freq_log", None); record.pop("sp_log", None)
        record["freq_n_imag"] = None
        record["zpe_hartree"] = None
        record.pop("sp_energy_hartree", None)
        record.pop("thermo", None)
        cfg = TCKDBConfig(
            enabled=True,
            base_url=self.cfg.base_url,
            payload_dir=str(proj / "tckdb_payloads"),
            api_key_env=self.cfg.api_key_env,
            project_label=self.cfg.project_label,
            upload_mode="computed_species",
            artifacts=TCKDBArtifactConfig(
                upload=True, kinds=("output_log", "input"), max_size_mb=50,
            ),
        )
        _, _, payload = self._submit(record=record, project_directory=str(proj), cfg=cfg)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertEqual(len(primary["artifacts"]), 1)
        self.assertEqual(primary["artifacts"][0]["kind"], "output_log")

    # ---------------- 14/15: sidecar contract
    def test_sidecar_payload_kind_and_endpoint(self):
        outcome, _, _ = self._submit()
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["payload_kind"], "computed_species")
        self.assertEqual(sc["endpoint"], "/uploads/computed-species")
        self.assertEqual(sc["bundle_format_version"], "0")
        # Sidecar must live under the dedicated computed_species subdir
        # so replay can sweep it without colliding with conformer sidecars.
        self.assertIn("computed_species", str(outcome.sidecar_path))

    def test_upload_response_public_refs_written_to_sidecar(self):
        client = _StubClient(response=_StubResponse({
            "species_ref": "spe_1",
            "species_entry_ref": "spee_1",
            "conformers": [{
                "key": "conf0",
                "primary_calculation": {
                    "key": "opt",
                    "calculation_id": 100,
                    "calculation_ref": "calc_opt",
                },
                "additional_calculations": [
                    {"key": "freq", "calculation_ref": "calc_freq"},
                    {"key": "sp", "calculation_ref": "calc_sp"},
                    {"key": "sp-repeat", "calculation_ref": "calc_sp"},
                ],
            }],
            "thermo": {"thermo_ref": "thermo_1"},
            "statmech": {"statmech_ref": "statmech_1"},
            "transport": {"transport_ref": None},
        }))
        outcome, _, _ = self._submit(client=client)
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["public_refs"]["species_refs"], ["spe_1"])
        self.assertEqual(sc["public_refs"]["species_entry_refs"], ["spee_1"])
        self.assertEqual(
            sc["public_refs"]["calculation_refs"],
            ["calc_opt", "calc_freq", "calc_sp"],
        )
        self.assertEqual(sc["public_refs"]["thermo_refs"], ["thermo_1"])
        self.assertEqual(sc["public_refs"]["statmech_refs"], ["statmech_1"])
        self.assertNotIn("transport_refs", sc["public_refs"])

    def test_sidecar_valid_when_response_has_no_public_refs(self):
        client = _StubClient(response=_StubResponse({"species_entry_id": 7}))
        outcome, _, _ = self._submit(client=client)
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "uploaded")
        self.assertEqual(sc["public_refs"], {})

    # ---------------- 16: stable idempotency key
    def test_idempotency_key_stable_for_identical_payload(self):
        out1, _, _ = self._submit()
        out2, _, _ = self._submit()
        self.assertEqual(out1.idempotency_key, out2.idempotency_key)
        # And distinct from a payload-altering change.
        modified_record = _full_record()
        modified_record["sp_energy_hartree"] = -200.0
        out3, _, _ = self._submit(record=modified_record)
        self.assertNotEqual(out1.idempotency_key, out3.idempotency_key)

    # ---------------- 17: payload written before live upload
    def test_payload_written_before_live_upload(self):
        client = _StubClient(raise_exc=RuntimeError("network down"))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=_full_record(),
            )
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.payload_path.exists(),
                        "payload must hit disk before any network call")
        # Sidecar exists and records the failure.
        self.assertTrue(outcome.sidecar_path.exists())
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "failed")
        self.assertIn("network down", sc["last_error"])

    # ---------------- 18: live upload success marks sidecar uploaded
    def test_live_upload_success_marks_sidecar_uploaded(self):
        client = _StubClient(response=_StubResponse(
            {
                "species_entry_id": 7,
                "conformers": [{
                    "key": "conf0",
                    "primary_calculation": {"calculation_id": 100, "type": "opt"},
                }],
            },
            headers={"X-Request-ID": "req-species-upload"},
        ))
        outcome, client, _ = self._submit(client=client)
        self.assertEqual(outcome.status, "uploaded")
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["status"], "uploaded")
        self.assertEqual(sc["response_status_code"], 201)
        # And the call really went to the bundle endpoint.
        upload_call = _upload_calls(client)[0]
        self.assertEqual(upload_call["path"], "/uploads/computed-species")
        # Idempotency-Key was sent.
        self.assertEqual(upload_call["idempotency_key"], outcome.idempotency_key)
        self.assertEqual(sc["request_ids"][-1]["request_id"], "req-species-upload")

    # ---------------- 19: live upload failure preserves payload, marks failed
    def test_live_upload_failure_marks_failed_and_preserves_payload(self):
        client = _StubClient(raise_exc=RuntimeError("HTTP 503"))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=_full_record(),
            )
        # Payload still on disk verbatim — replay-ready.
        self.assertTrue(outcome.payload_path.exists())
        replay_payload = json.loads(outcome.payload_path.read_text())
        self.assertIn("species_entry", replay_payload)
        self.assertIn("conformers", replay_payload)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("HTTP 503", outcome.error)

    # ---------------- 19a: HTTP 422 detail body preserved in failure record
    def test_http_422_detail_preserved_in_outcome_and_sidecar(self):
        # Regression: failures used to flatten to ``HTTP 422`` because
        # ``_record_failure`` only stored ``str(exc)``. The TCKDB client's
        # HTTP-error subclasses carry ``status_code`` / ``response_json``
        # / ``response_text``; the adapter now lifts those into the
        # sidecar and the outcome.error so operators can see *why* a
        # validation rejection happened.
        class _FakeValidationError(RuntimeError):
            def __init__(self):
                super().__init__("HTTP 422")
                self.status_code = 422
                self.response_json = {
                    "detail": [{
                        "type": "value_error",
                        "loc": ["body", "species", 1, "statmech", "torsions", 0,
                                "source_scan_calculation_key"],
                        "msg": ("Value error, source_scan_calculation_key "
                                "'scan_rotor_0' references undefined "
                                "calculation_key."),
                    }],
                }
                self.response_text = json.dumps(self.response_json)
                self.headers = {"X-Request-ID": "req-422"}

        client = _StubClient(raise_exc=_FakeValidationError())
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=_full_record(),
            )
        self.assertEqual(outcome.status, "failed")
        # Outcome.error now carries enough detail to diagnose without
        # a manual replay POST.
        self.assertIn("HTTP 422", outcome.error)
        self.assertIn("scan_rotor_0", outcome.error)
        self.assertIn("undefined calculation_key", outcome.error)
        # Sidecar has the structured body + status code intact.
        sc = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sc["response_status_code"], 422)
        self.assertIsInstance(sc["response_body"], dict)
        self.assertIn("detail", sc["response_body"])
        self.assertEqual(sc["request_ids"][-1]["request_id"], "req-422")
        # status stays "failed" so reruns / replays know to try again.
        self.assertEqual(sc["status"], "failed")

    # ---------------- 20: producer reads only output_doc + species_record (mapping arg shape)
    def test_producer_consumes_only_output_doc_and_record_mappings(self):
        # Pass plain dicts (no ARC class instances). If the adapter ever
        # reaches into ARC live objects, this test would surface that
        # via a missing-attribute crash.
        plain_doc = dict(_fake_output_doc())
        plain_record = dict(_full_record())
        outcome, _, _ = self._submit(output_doc=plain_doc, record=plain_record)
        self.assertEqual(outcome.status, "uploaded")


def _reaction_record(*, with_kinetics=True, ts_label="TS0"):
    """Mimics one entry from output.yml's `reactions:` list."""
    record = {
        "label": "CHO + CH4 <=> CH2O + CH3",
        "family": "H_Abstraction",
        "multiplicity": 2,
        "reactant_labels": ["CHO", "CH4"],
        "product_labels": ["CH2O", "CH3"],
        "ts_label": ts_label,
        "kinetics": None,
    }
    if with_kinetics:
        record["kinetics"] = {
            "A": 0.204298,
            "A_units": "cm^3/(mol*s)",
            "n": 4.37949,
            "Ea": 78.9012,
            "Ea_units": "kJ/mol",
            "Tmin_k": 300.0,
            "Tmax_k": 3000.0,
            "dA": 1.48466,
            "dn": 0.0514735,
            "dEa": 0.294363,
            "dEa_units": "kJ/mol",
            "n_data_points": 50,
            "tunneling": "Eckart",
        }
    return record


# ARC's ``neb_level`` header entry (arc/output.py ``_level_to_dict``): the ORCA
# NEB level from ``orca_neb_settings``.
NEB_LEVEL = {"method": "wb97xd", "basis": "def2tzvp", "software": "orca"}


def _reaction_output_doc(*, with_irc=False):
    """Output document with the four species, one TS, and one reaction populated."""
    doc = _fake_output_doc()

    def _spc(label, smiles, mult, *, sp_e, freq_n_imag=0):
        return {
            "label": label,
            "smiles": smiles,
            "charge": 0,
            "multiplicity": mult,
            "is_ts": False,
            "xyz": "C 0.0 0.0 0.0\nH 1.0 0.0 0.0",
            "opt_n_steps": 10,
            "opt_final_energy_hartree": -100.0,
            "opt_converged": True,
            "freq_n_imag": freq_n_imag,
            "zpe_hartree": 0.02,
            "sp_energy_hartree": sp_e,
            "ess_versions": {"opt": "Gaussian 16, Revision A.03"},
        }

    doc["species"] = [
        _spc("CHO", "[CH]=O", 2, sp_e=-113.7),
        _spc("CH4", "C", 1, sp_e=-40.5),
        _spc("CH2O", "C=O", 1, sp_e=-114.5),
        _spc("CH3", "[CH3]", 2, sp_e=-39.8),
    ]
    ts = {
        "label": "TS0",
        "smiles": None,
        "charge": 0,
        "multiplicity": 2,
        "is_ts": True,
        "xyz": "C 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.5 0.5 0.0",
        "opt_n_steps": 25,
        "opt_final_energy_hartree": -154.1,
        "opt_converged": True,
        "freq_n_imag": 1,
        "imag_freq_cm1": -1320.5,
        "zpe_hartree": 0.05,
        "sp_energy_hartree": -154.7,
        "ess_versions": {"opt": "Gaussian 16, Revision A.03"},
    }
    if with_irc:
        ts["irc_logs"] = ["irc_forward.log", "irc_reverse.log"]
        ts["irc_converged"] = True
    doc["transition_states"] = [ts]
    doc["reactions"] = [_reaction_record()]
    return doc


# ---------------------------------------------------------------------------
# Computed-reaction tests
# ---------------------------------------------------------------------------


class TestKineticsUnitMapping(unittest.TestCase):
    """Test 1: ARC kinetics unit strings map to TCKDB enum strings."""

    def test_arrhenius_units(self):
        self.assertEqual(arc_to_tckdb_a_units("cm^3/(mol*s)"), "cm3_mol_s")
        self.assertEqual(arc_to_tckdb_a_units("s^-1"), "per_s")
        self.assertEqual(arc_to_tckdb_a_units("cm^3/(molecule*s)"), "cm3_molecule_s")

    def test_arrhenius_units_case_insensitive(self):
        self.assertEqual(arc_to_tckdb_a_units("CM^3/(MOL*S)"), "cm3_mol_s")
        self.assertEqual(arc_to_tckdb_a_units(" s^-1 "), "per_s")

    def test_arrhenius_units_unknown(self):
        self.assertIsNone(arc_to_tckdb_a_units("bushels/fortnight"))

    def test_arrhenius_units_none(self):
        self.assertIsNone(arc_to_tckdb_a_units(None))
        self.assertIsNone(arc_to_tckdb_a_units(""))

    def test_activation_energy_units(self):
        self.assertEqual(arc_to_tckdb_ea_units("kJ/mol"), "kj_mol")
        self.assertEqual(arc_to_tckdb_ea_units("kcal/mol"), "kcal_mol")
        self.assertEqual(arc_to_tckdb_ea_units("J/mol"), "j_mol")
        self.assertEqual(arc_to_tckdb_ea_units("cal/mol"), "cal_mol")

    def test_activation_energy_units_unknown(self):
        self.assertIsNone(arc_to_tckdb_ea_units("erg/molecule"))


class TestChargePropagation(unittest.TestCase):
    """Verify charge round-trips from ARC's species record into every
    TCKDB payload field that carries it.

    Background: an early observation that ``species.charge`` was 0 for
    every TCKDB row prompted this audit. The wiring is correct — every
    real ARC dataset uploaded so far happens to be neutral. These tests
    pin the wiring so a future change can't silently drop charge for a
    cation/anion.

    Charge sources, in priority order along the pipeline:
    ``ARCSpecies.charge`` (constructor / RMG ``mol.get_net_charge`` /
    fallback 0)  →  ``output.yml`` species record's ``charge`` key
    (arc/output.py:865)  →  ``species_entry.charge`` and (for TS rows)
    ``transition_state.charge`` in the upload payload (adapter.py:2210
    and :2159).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-charge-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ---------------- 1: static helper preserves every signed integer ----

    def test_species_entry_payload_neutral_closed_shell(self):
        record = _fake_record(smiles="CCO", charge=0, multiplicity=1)
        payload = TCKDBAdapter._species_entry_payload(record)
        self.assertEqual(payload["charge"], 0)
        self.assertEqual(payload["multiplicity"], 1)

    def test_species_entry_payload_neutral_radical(self):
        # The classic ARC case: charge stays 0, the unpaired electron
        # is encoded in multiplicity. Multiplicity must NOT bleed into
        # charge.
        record = _fake_record(smiles="[CH3]", charge=0, multiplicity=2)
        payload = TCKDBAdapter._species_entry_payload(record)
        self.assertEqual(payload["charge"], 0)
        self.assertEqual(payload["multiplicity"], 2)

    def test_species_entry_payload_cation(self):
        record = _fake_record(smiles="[NH4+]", charge=1, multiplicity=1)
        payload = TCKDBAdapter._species_entry_payload(record)
        self.assertEqual(payload["charge"], 1)
        self.assertEqual(payload["multiplicity"], 1)

    def test_species_entry_payload_anion(self):
        record = _fake_record(smiles="[OH-]", charge=-1, multiplicity=1)
        payload = TCKDBAdapter._species_entry_payload(record)
        # A negative charge must survive as itself, never collapse to 0.
        self.assertEqual(payload["charge"], -1)
        self.assertEqual(payload["multiplicity"], 1)

    def test_species_entry_payload_charged_radical(self):
        # Cation radical: nonzero charge AND multiplicity > 1. Both
        # must survive independently.
        record = _fake_record(smiles="[OH+]", charge=1, multiplicity=2)
        payload = TCKDBAdapter._species_entry_payload(record)
        self.assertEqual(payload["charge"], 1)
        self.assertEqual(payload["multiplicity"], 2)

    def test_species_entry_payload_refuses_unstated_charge_or_multiplicity(self):
        # ARC writes charge and multiplicity for every record
        # (arc/output.py ``_spc_to_dict``); a record without a usable value
        # is malformed. TCKDB requires both with no default, and a
        # defaulted 0 / singlet is a different identity (a radical filed as
        # a singlet), so the adapter refuses instead of filling one in.
        for field, bad in (("charge", None), ("charge", "1"), ("charge", True),
                           ("charge", 0.5), ("multiplicity", None),
                           ("multiplicity", 0), ("multiplicity", "2")):
            with self.subTest(field=field, value=bad):
                record = _fake_record(smiles="[CH3]", charge=0, multiplicity=2)
                record[field] = bad
                with self.assertRaises(ValueError) as ctx:
                    TCKDBAdapter._species_entry_payload(record)
                self.assertIn(f"states no usable {field}", str(ctx.exception))
        for field in ("charge", "multiplicity"):
            with self.subTest(field=field, value="absent"):
                record = _fake_record(smiles="[CH3]", charge=0, multiplicity=2)
                del record[field]
                with self.assertRaises(ValueError):
                    TCKDBAdapter._species_entry_payload(record)
        # An integral float is ARC's integer written through YAML.
        record = _fake_record(smiles="[CH3]", charge=0.0, multiplicity=2.0)
        payload = TCKDBAdapter._species_entry_payload(record)
        self.assertEqual((payload["charge"], payload["multiplicity"]), (0, 2))

    def test_unstated_charge_refuses_the_upload_not_the_sweep(self):
        # Species and TS both: the per-record build fails (the sweep's
        # existing channel counts and logs it), nothing is written.
        cfg = TCKDBConfig(
            enabled=True, base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp, api_key_env="X_TCKDB_API_KEY",
            project_label="proj-charge", upload_mode="computed_reaction",
        )
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: _StubClient())
        for target in ("species", "transition_state"):
            with self.subTest(target=target):
                doc = _reaction_output_doc()
                if target == "species":
                    doc["species"][0].pop("charge")
                else:
                    doc["transition_states"][0]["charge"] = None
                with self.assertRaises(ValueError) as ctx:
                    adapter.submit_computed_reaction_from_output(
                        output_doc=doc, reaction_record=doc["reactions"][0])
                self.assertIn("states no usable charge", str(ctx.exception))

    def test_species_entry_payload_refuses_is_ts_record(self):
        """D2: applying the same standard as ``_freq_result_payload``'s
        contradiction raises. No path inside ARC's own pipeline is known
        to reach here with ``is_ts=True`` (a converged TS is routed to
        ``output_doc['transition_states']`` and its own upload path), so
        this is defensive validation for a hand-written/older/third-party
        output.yml -- the same threat model D2 exists for. TCKDB
        0.22.0's StationaryPointKind has no 'transition_state' member, so
        the previous behaviour (silently emitting that invalid string)
        would have failed model_validate anyway; refusing here is
        earlier and more attributable than a downstream 422.
        """
        record = _fake_record(smiles="CCO", is_ts=True)
        with self.assertRaises(ValueError) as ctx:
            TCKDBAdapter._species_entry_payload(record)
        self.assertIn("is_ts=True", str(ctx.exception))

    # ---------------- 2: full computed-species bundle preserves charge ---

    def _bundle_payload(self, *, charge, multiplicity, smiles):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-charge",
            upload_mode="computed_species",
        )
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{
                "key": "conf0",
                "conformer_group_id": 1,
                "conformer_observation_id": 1,
                "primary_calculation": {"key": "opt", "calculation_id": 1, "type": "opt", "role": "primary"},
                "additional_calculations": [],
            }],
        }))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        record = _full_record()
        record["smiles"] = smiles
        record["charge"] = charge
        record["multiplicity"] = multiplicity
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=record,
            )
        return json.loads(outcome.payload_path.read_text())

    def test_bundle_preserves_cation_charge(self):
        payload = self._bundle_payload(charge=1, multiplicity=1, smiles="[NH4+]")
        self.assertEqual(payload["species_entry"]["charge"], 1)
        self.assertEqual(payload["species_entry"]["smiles"], "[NH4+]")

    def test_bundle_preserves_anion_charge(self):
        payload = self._bundle_payload(charge=-1, multiplicity=1, smiles="[OH-]")
        self.assertEqual(payload["species_entry"]["charge"], -1)
        self.assertEqual(payload["species_entry"]["smiles"], "[OH-]")

    # ---------------- 3: TS block preserves charge -----------------------

    def test_transition_state_block_preserves_cation_charge(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-charge",
            upload_mode="computed_reaction",
        )
        client = _StubClient(response=_StubResponse({"reaction_entry_id": 1}))
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        # Build a charged-reaction doc by mutating the standard fixture.
        # Every species and the TS itself become +1 cations to keep the
        # mass-balanced reaction valid; what we're pinning here is that
        # the TS block's ``charge`` field carries the nonzero value
        # rather than silently zeroing it.
        doc = _reaction_output_doc()
        for sp in doc["species"]:
            sp["charge"] = 1
        doc["transition_states"][0]["charge"] = 1
        doc["reactions"][0]["charge"] = 1
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc,
                reaction_record=doc["reactions"][0],
            )
        payload = json.loads(outcome.payload_path.read_text())
        self.assertEqual(payload["transition_state"]["charge"], 1)
        for sp in payload["species"]:
            self.assertEqual(sp["species_entry"]["charge"], 1)


class TestComputedSpeciesStatmechFreqScaleFactor(unittest.TestCase):
    """Statmech frequency-scale-factor provenance in computed-species bundles."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-fsf-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    def _build_payload(self, output_doc):
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{
                "key": "conf0",
                "primary_calculation": {"key": "opt", "calculation_id": 100, "type": "opt", "role": "primary"},
                "additional_calculations": [],
            }],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=output_doc, species_record=_full_record(),
            )
        return outcome, json.loads(outcome.payload_path.read_text())

    @staticmethod
    def _doc_with_fsf(*, value=0.961, source="J. Chem. Theory Comput. 2010, 6, 2872",
                     include_freq_level=True):
        doc = _fake_output_doc()
        doc["freq_scale_factor"] = value
        doc["freq_scale_factor_source"] = source
        if include_freq_level:
            doc["freq_level"] = {
                "method": "wb97xd", "basis": "def2-tzvp", "software": "gaussian",
            }
        return doc

    # ---------------- 1: FSF emitted under statmech when value present
    def test_statmech_emitted_with_freq_scale_factor(self):
        _, payload = self._build_payload(self._doc_with_fsf())
        self.assertIn("statmech", payload)
        sm = payload["statmech"]
        self.assertIn("freq_scale_factor", sm)
        fsf = sm["freq_scale_factor"]
        self.assertAlmostEqual(fsf["value"], 0.961)
        # ARC does not state the factor's kind; TCKDB applies its default.
        self.assertNotIn("scale_kind", fsf)
        self.assertEqual(fsf["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(fsf["level_of_theory"]["basis"], "def2-tzvp")
        self.assertEqual(fsf["software"], {"name": "gaussian"})

    # ---------------- 2: bare citation lands in note, never source_literature
    def test_bare_citation_string_maps_to_note_not_literature(self):
        citation = "J. Chem. Theory Comput. 2010, 6, 2872, DOI: 10.1021/ct100326h"
        _, payload = self._build_payload(self._doc_with_fsf(source=citation))
        fsf = payload["statmech"]["freq_scale_factor"]
        self.assertEqual(fsf["note"], citation)
        self.assertNotIn("source_literature", fsf)

    # ---------------- 3: ARC tagged as workflow_tool_release only when sourced from ARC's data
    def test_arc_workflow_tool_release_set_when_arc_data_file_was_source(self):
        _, payload = self._build_payload(self._doc_with_fsf(source="some citation"))
        fsf = payload["statmech"]["freq_scale_factor"]
        self.assertEqual(fsf["workflow_tool_release"]["name"], "ARC")

    def test_workflow_tool_release_omitted_when_user_supplied(self):
        # No source string → user-supplied factor → ARC must not claim
        # itself as the proximate source (would fork registry rows
        # since workflow_tool_release is part of FSF identity).
        doc = self._doc_with_fsf(source=None)
        _, payload = self._build_payload(doc)
        fsf = payload["statmech"]["freq_scale_factor"]
        self.assertNotIn("workflow_tool_release", fsf)
        # And no note either, since note carried only the citation.
        self.assertNotIn("note", fsf)

    # ---------------- 4: missing FSF cleanly omits the entire statmech block
    def test_missing_freq_scale_factor_omits_statmech(self):
        # Default fixture has no freq_scale_factor — must look identical
        # to the pre-change behavior (no statmech at all).
        doc = _fake_output_doc()
        self.assertNotIn("freq_scale_factor", doc)
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{"key": "conf0", "primary_calculation": {"key": "opt", "calculation_id": 1, "type": "opt", "role": "primary"}, "additional_calculations": []}],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=doc, species_record=_full_record(),
            )
        payload = json.loads(outcome.payload_path.read_text())
        self.assertNotIn("statmech", payload)

    def test_zero_or_negative_fsf_is_rejected_silently(self):
        # Schema requires gt 0; producer must not emit a registry-
        # poisoning value. Falls through to "no FSF" behavior.
        doc = self._doc_with_fsf(value=0.0)
        _, payload = self._build_payload(doc)
        self.assertNotIn("statmech", payload)

    def test_missing_method_on_freq_level_omits_fsf(self):
        # LevelOfTheoryRef.method is required; if ARC's freq_level
        # entry is malformed (no method), the producer can't build a
        # well-formed FSF ref → omit cleanly rather than fail.
        doc = self._doc_with_fsf(include_freq_level=False)
        # Also strip arkane_level_of_theory so neither LOT source resolves.
        doc.pop("arkane_level_of_theory", None)
        _, payload = self._build_payload(doc)
        self.assertNotIn("statmech", payload)

    def test_arkane_level_of_theory_used_as_lot_fallback(self):
        # When freq_level is absent, the producer falls back to
        # arkane_level_of_theory — common in single-LOT runs.
        doc = self._doc_with_fsf(include_freq_level=False)
        doc["arkane_level_of_theory"] = {
            "method": "ccsd(t)-f12", "basis": "cc-pvtz-f12", "software": "molpro",
        }
        _, payload = self._build_payload(doc)
        fsf = payload["statmech"]["freq_scale_factor"]
        self.assertEqual(fsf["level_of_theory"]["method"], "ccsd(t)-f12")
        self.assertEqual(fsf["software"], {"name": "molpro"})

    # ---------------- 5: source_calculations use local keys, exclude opt_coarse
    def test_source_calculations_use_local_keys(self):
        _, payload = self._build_payload(self._doc_with_fsf())
        sm = payload["statmech"]
        self.assertIn("source_calculations", sm)
        # _full_record() carries opt + freq + sp (no coarse), so all
        # three roles should resolve.
        by_role = {entry["role"]: entry["calculation_key"] for entry in sm["source_calculations"]}
        self.assertEqual(by_role, {"opt": "opt", "freq": "freq", "sp": "sp"})

    def test_source_calculations_exclude_opt_coarse(self):
        # Build a record with the coarse-opt stage so opt_coarse lands
        # in included_calc_keys; statmech.source_calculations must
        # still emit only opt/freq/sp (no opt_coarse role exists in
        # the StatmechCalculationRole enum).
        record = _full_record()
        record["coarse_opt_log"] = "coarse.log"
        record["coarse_opt_output_xyz"] = "C 0 0 0\nH 1 0 0"
        record["coarse_opt_n_steps"] = 5
        record["coarse_opt_final_energy_hartree"] = -154.0
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{"key": "conf0", "primary_calculation": {"key": "opt", "calculation_id": 1, "type": "opt", "role": "primary"}, "additional_calculations": []}],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=self._doc_with_fsf(), species_record=record,
            )
        payload = json.loads(outcome.payload_path.read_text())
        sources = payload["statmech"]["source_calculations"]
        keys = {entry["calculation_key"] for entry in sources}
        self.assertNotIn("opt_coarse", keys)
        self.assertEqual(keys, {"opt", "freq", "sp"})

    def test_source_calculations_only_include_emitted_keys(self):
        # Strip sp from the record so only opt + freq emit; statmech
        # source list must shrink in lockstep.
        record = _full_record()
        record["sp_energy_hartree"] = None
        record.pop("electronic_energy_hartree", None)
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{"key": "conf0", "primary_calculation": {"key": "opt", "calculation_id": 1, "type": "opt", "role": "primary"}, "additional_calculations": []}],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=self._doc_with_fsf(), species_record=record,
            )
        payload = json.loads(outcome.payload_path.read_text())
        roles = [entry["role"] for entry in payload["statmech"]["source_calculations"]]
        self.assertEqual(roles, ["opt", "freq"])

    # ---------------- 6: idempotency — same content → same key
    def test_payload_with_fsf_is_deterministic(self):
        outcome1, payload1 = self._build_payload(self._doc_with_fsf())
        outcome2, payload2 = self._build_payload(self._doc_with_fsf())
        self.assertEqual(payload1, payload2)
        self.assertEqual(outcome1.idempotency_key, outcome2.idempotency_key)

    def test_idempotency_key_changes_when_fsf_value_changes(self):
        outcome1, _ = self._build_payload(self._doc_with_fsf(value=0.961))
        outcome2, _ = self._build_payload(self._doc_with_fsf(value=0.999))
        self.assertNotEqual(outcome1.idempotency_key, outcome2.idempotency_key)

    # ---------------- 7: live TCKDB schema validation
    def test_payload_validates_against_tckdb_schema(self):
        _, payload = self._build_payload(self._doc_with_fsf())
        # Sanity: the field is populated, not just bypassed.
        self.assertTrue(payload["statmech"]["freq_scale_factor"]["value"])
        contract_validate(ComputedSpeciesUploadRequest, payload)


class TestComputedSpeciesStatmechBaseFields(unittest.TestCase):
    """Computed-species statmech: richer base fields (sym, point group, rotor kind, treatment, torsions)."""

    # A freq Hessian is what shows Arkane kept the rotors; see A5 tests below.
    hessian_available = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-stm-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    @staticmethod
    def _record_with_statmech(**overrides):
        record = _full_record()
        # Mirrors what arc/output.py::_statmech_to_dict writes for a
        # converged non-monoatomic species. ARC's emitted fields that
        # don't map onto TCKDB statmech (e0_kj_mol, spin_multiplicity,
        # harmonic_frequencies_cm1) are kept here so the test fixture
        # matches output.yml shape; the producer is expected to ignore
        # them. optical_isomers now maps onto the bundle statmech block.
        record["statmech"] = {
            "e0_kj_mol": 12.5,
            "spin_multiplicity": 1,
            "optical_isomers": 1,
            "is_linear": False,
            "external_symmetry": 2,
            "point_group": "C2v",
            "rigid_rotor_kind": "asymmetric_top",
            "harmonic_frequencies_cm1": [3000.0, 1500.0, 800.0],
            "torsions": [
                {
                    "symmetry_number": 3,
                    "treatment": "hindered_rotor",
                    "atom_indices": [1, 2, 3, 4],
                    "pivot_atoms": [2, 3],
                    "barrier_kj_mol": 12.0,
                },
            ],
        }
        record["statmech"].update(overrides.pop("statmech", {}))
        record.update(overrides)
        return record

    def _submit(self, *, doc, record):
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{
                "key": "conf0",
                "primary_calculation": {
                    "key": "opt", "calculation_id": 1, "type": "opt", "role": "primary",
                },
                "additional_calculations": [],
            }],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), \
                mock.patch.object(TCKDBAdapter, "_freq_hessian_available",
                                  return_value=self.hessian_available):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=doc, species_record=record,
            )
        return outcome, json.loads(outcome.payload_path.read_text())

    def _doc(self):
        # Reuse the FSF doc helper from the FSF test class — ensures
        # the existing FSF wiring keeps working alongside the new
        # base-fields wiring.
        return TestComputedSpeciesStatmechFreqScaleFactor._doc_with_fsf()

    # ---------------- 1: external_symmetry
    def test_external_symmetry_emitted(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertEqual(payload["statmech"]["external_symmetry"], 2)

    # ---------------- 2: point_group (computed-species only)
    def test_point_group_emitted_in_computed_species(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertEqual(payload["statmech"]["point_group"], "C2v")

    # ---------------- 3: is_linear
    def test_is_linear_emitted(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertIs(payload["statmech"]["is_linear"], False)

    # ---------------- 4: rigid_rotor_kind
    def test_rigid_rotor_kind_emitted(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertEqual(payload["statmech"]["rigid_rotor_kind"], "asymmetric_top")

    # ---------------- 5: statmech_treatment derivation
    def test_statmech_treatment_rrho_when_no_torsions(self):
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = []
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho")

    def test_statmech_treatment_rrho_1d_for_1d_rotors(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho_1d")

    def test_statmech_treatment_rrho_nd_for_nd_only(self):
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 1,
            "treatment": "hindered_rotor",
            # ND scan: list of 4-int lists (ARC's directed_scan shape).
            "atom_indices": [[1, 2, 3, 4], [5, 2, 3, 6]],
            "pivot_atoms": [2, 3],
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho_nd")

    def test_statmech_treatment_rrho_1d_nd_for_mixed(self):
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [
            {"symmetry_number": 3, "treatment": "hindered_rotor",
             "atom_indices": [1, 2, 3, 4], "pivot_atoms": [2, 3]},
            {"symmetry_number": 1, "treatment": "hindered_rotor",
             "atom_indices": [[5, 6, 7, 8], [9, 6, 7, 10]], "pivot_atoms": [6, 7]},
        ]
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho_1d_nd")

    def test_statmech_treatment_omitted_when_no_statmech_subdict(self):
        # Without a statmech subdict, ARC didn't run an RRHO evaluation
        # for this species — the spec forbids fabricating "rrho" in that
        # case. The treatment field must be absent.
        record = _full_record()
        self.assertNotIn("statmech", record)
        _, payload = self._submit(doc=self._doc(), record=record)
        # FSF block still present; no statmech_treatment.
        self.assertNotIn("statmech_treatment", payload["statmech"])

    def test_statmech_treatment_omitted_when_every_rotor_is_dropped(self):
        # A rotor whose ARC ``treatment`` has no TCKDB equivalent is dropped
        # from ``torsions``, but its atom_indices are a well-formed 1D
        # quartet. Deriving the treatment from ARC's raw input would claim
        # "rrho_1d" over an empty torsion list, which TCKDB refuses as
        # self-contradictory ("must list the torsions it treated").
        #
        # "rrho" would be wrong too: ARC's record says rotors *were*
        # treated, so asserting a plain rigid-rotor treatment would
        # misstate the science. Omission is the only honest claim.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 3,
            "treatment": "some_future_arc_rotor_kind",
            "atom_indices": [1, 2, 3, 4],
            "pivot_atoms": [2, 3],
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertNotIn("statmech_treatment", payload["statmech"])
        self.assertNotIn("torsions", payload["statmech"])

    def test_statmech_treatment_reflects_only_the_rotors_emitted(self):
        # One 1D rotor survives, one ND rotor is dropped. The treatment must
        # describe the payload's own torsion list ("rrho_1d"), not ARC's
        # richer input ("rrho_1d_nd") -- a consumer reading rrho_1d_nd would
        # look for an ND rotor that is not there.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [
            {"symmetry_number": 3, "treatment": "hindered_rotor",
             "atom_indices": [1, 2, 3, 4], "pivot_atoms": [2, 3]},
            {"symmetry_number": 1, "treatment": "some_future_arc_rotor_kind",
             "atom_indices": [[5, 6, 7, 8], [9, 6, 7, 10]], "pivot_atoms": [6, 7]},
        ]
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho_1d")
        self.assertEqual(len(payload["statmech"]["torsions"]), 1)

    # ---------------- A5: treatment only with Hessian evidence
    def test_rotor_aware_treatment_and_torsion_kinds_omitted_without_freq_hessian(self):
        # Arkane ignores every rotor when the freq log has no force-constant
        # matrix, so neither the rotor-aware treatment nor each torsion's
        # 'hindered_rotor' is supported. The torsions themselves (atom quartet,
        # symmetry) are recorded facts and are still sent.
        self.hessian_available = False
        outcome, payload = self._submit(
            doc=self._doc(), record=self._record_with_statmech())
        self.assertNotIn("statmech_treatment", payload["statmech"])
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 1)
        self.assertNotIn("treatment_kind", torsions[0])
        self.assertEqual(torsions[0]["symmetry_number"], 3)
        self.assertEqual(torsions[0]["dimension"], 1)
        [warned] = [w for w in outcome.warnings
                    if w["code"] == "statmech_treatment_not_stated"]
        self.assertEqual(warned["field"], "statmech.statmech_treatment")
        self.assertEqual(warned["context"]["reason"], "no_freq_hessian")
        self.assertEqual(warned["context"]["inferred_treatment"], "rrho_1d")
        self.assertEqual(warned["context"]["torsion_count"], 1)
        self.assertEqual(warned["context"]["omitted"],
                         ["statmech_treatment", "torsions[].treatment_kind"])

    def test_torsion_treatment_kind_is_kept_with_freq_hessian(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertEqual(payload["statmech"]["torsions"][0]["treatment_kind"],
                         "hindered_rotor")

    def test_plain_rrho_needs_no_hessian(self):
        # No rotors: Arkane runs RRHO with or without a force-constant matrix
        # (monatomics never have one), so nothing is withheld or warned.
        self.hessian_available = False
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = []
        outcome, payload = self._submit(doc=self._doc(), record=record)
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho")
        self.assertNotIn("torsions", payload["statmech"])
        self.assertNotIn("statmech_treatment_not_stated",
                         [w["code"] for w in outcome.warnings])

    def test_no_statmech_treatment_warning_when_hessian_present(self):
        outcome, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertEqual(payload["statmech"]["statmech_treatment"], "rrho_1d")
        self.assertNotIn("statmech_treatment_not_stated",
                         [w["code"] for w in outcome.warnings])

    def test_no_treatment_warning_when_nothing_was_inferred(self):
        # No statmech subdict: there was no claim to withhold.
        self.hessian_available = False
        outcome, payload = self._submit(doc=self._doc(), record=_full_record())
        self.assertNotIn("statmech_treatment", payload["statmech"])
        self.assertNotIn("statmech_treatment_not_stated",
                         [w["code"] for w in outcome.warnings])

    # ---------------- 6: FSF behavior preserved
    def test_freq_scale_factor_behavior_preserved(self):
        # Sanity: the FSF block remains fully populated even when the
        # richer base-fields machinery runs.
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertIn("freq_scale_factor", payload["statmech"])
        self.assertAlmostEqual(payload["statmech"]["freq_scale_factor"]["value"], 0.961)

    # ---------------- 7: source_calculations still emitted (computed-species)
    def test_source_calculations_present_in_computed_species(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertIn("source_calculations", payload["statmech"])
        roles = {e["role"] for e in payload["statmech"]["source_calculations"]}
        self.assertEqual(roles, {"opt", "freq", "sp"})

    # ---------------- 14/15: slim torsion shape only
    def test_slim_torsions_emitted(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 1)
        t = torsions[0]
        self.assertEqual(t["torsion_index"], 1)
        self.assertEqual(t["symmetry_number"], 3)
        self.assertEqual(t["treatment_kind"], "hindered_rotor")

    def test_slim_torsions_omit_only_unsupported_fields(self):
        # The bundle schema accepts structured ``coordinates`` with
        # atom1_index..atom4_index quartets and a ``dimension`` field,
        # and now (since ARC emits scan calcs in the bundle) also
        # ``source_scan_calculation_key``. Other ARC-side fields
        # (``pivot_atoms``, ``barrier_kj_mol``, the raw ARC-shape
        # ``atom_indices``) still have no destination column.
        # ``source_scan_calculation_id`` is server-assigned and must
        # never come from the producer side. This test's fixture
        # carries no scan key, so the key is correctly absent here —
        # see TestScanCalculations for the positive-pass-through case.
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        for t in payload["statmech"]["torsions"]:
            for forbidden in (
                "atom_indices",  # ARC's input shape — never the output shape
                "pivot_atoms", "barrier_kj_mol",
                "source_scan_calculation_id",
            ):
                self.assertNotIn(forbidden, t)
            # No fixture key in this test, so absent in payload.
            self.assertNotIn("source_scan_calculation_key", t)

    def test_slim_torsions_skip_unsupported_treatment(self):
        # ARC could (now or in the future) emit a torsion with a
        # treatment that isn't in TCKDB's TorsionTreatmentKind enum.
        # The producer must drop those entries rather than emit an
        # entry with a missing/invalid treatment_kind.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [
            {"symmetry_number": 3, "treatment": "hindered_rotor",
             "atom_indices": [1, 2, 3, 4], "pivot_atoms": [2, 3]},
            {"symmetry_number": 1, "treatment": "experimental_rotor_xyz",
             "atom_indices": [5, 6, 7, 8], "pivot_atoms": [6, 7]},
        ]
        _, payload = self._submit(doc=self._doc(), record=record)
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 1)
        self.assertEqual(torsions[0]["treatment_kind"], "hindered_rotor")
        # torsion_index is this entry's 1-based position in ARC's
        # torsions list, not a counter of emitted entries — it stayed 1
        # here only because the surviving entry is also first in the
        # list. See test_slim_torsions_index_is_source_position_not_a_
        # compacted_counter below for the case that actually
        # distinguishes the two policies (PHASE_C_PLAN.md C-7).
        self.assertEqual(torsions[0]["torsion_index"], 1)

    def test_slim_torsions_index_is_source_position_not_a_compacted_counter(self):
        # C-7: dropping an earlier rotor (unsupported treatment) must
        # leave a gap in torsion_index, not renumber every later rotor
        # down by one. A compacting counter silently breaks the only
        # cross-reference back to ARC's rotors_dict ordering — exactly
        # what a curator re-deriving a barrier would rely on. TCKDB only
        # requires torsion_index to be unique, not contiguous
        # (BundleStatmechIn / StatmechTorsionInBundle
        # .validate_unique_torsion_indices), so a gap is valid.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [
            {"symmetry_number": 1, "treatment": "experimental_rotor_xyz",
             "atom_indices": [1, 2, 3, 4], "pivot_atoms": [2, 3]},   # dropped: ARC rotor #1
            {"symmetry_number": 3, "treatment": "hindered_rotor",
             "atom_indices": [5, 6, 7, 8], "pivot_atoms": [6, 7]},   # ARC rotor #2
            {"symmetry_number": 1, "treatment": "free_rotor",
             "atom_indices": [2, 6, 7, 8], "pivot_atoms": [6, 7]},   # ARC rotor #3
        ]
        _, payload = self._submit(doc=self._doc(), record=record)
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 2)
        # Rotor #2 keeps index 2 (its real source position), not 1.
        self.assertEqual(torsions[0]["torsion_index"], 2)
        self.assertEqual(torsions[0]["treatment_kind"], "hindered_rotor")
        # Rotor #3 keeps index 3, not 2.
        self.assertEqual(torsions[1]["torsion_index"], 3)
        self.assertEqual(torsions[1]["treatment_kind"], "free_rotor")
        contract_validate(ComputedSpeciesUploadRequest, payload)

    # ---------------- coordinate definitions (1D, ND, malformed)
    def test_torsion_coordinates_emitted_for_1d(self):
        # Spec example: atom_indices=[5,1,2,3] should emit dimension=1
        # with one coordinate carrying atom1..atom4_index in order.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 3, "treatment": "hindered_rotor",
            "atom_indices": [5, 1, 2, 3], "pivot_atoms": [1, 2],
            "barrier_kj_mol": 11.99,
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 1)
        t = torsions[0]
        self.assertEqual(t["dimension"], 1)
        self.assertEqual(len(t["coordinates"]), 1)
        c = t["coordinates"][0]
        self.assertEqual(c, {
            "coordinate_index": 1,
            "atom1_index": 5, "atom2_index": 1,
            "atom3_index": 2, "atom4_index": 3,
        })

    def test_torsion_coordinate_atom_indices_preserved_1based(self):
        # Indices must round-trip exactly — no off-by-one, no
        # canonicalization, no sort.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 1, "treatment": "free_rotor",
            "atom_indices": [9, 4, 7, 12], "pivot_atoms": [4, 7],
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        c = payload["statmech"]["torsions"][0]["coordinates"][0]
        self.assertEqual(
            (c["atom1_index"], c["atom2_index"], c["atom3_index"], c["atom4_index"]),
            (9, 4, 7, 12),
        )
        # All ≥ 1 (server-side CheckConstraint).
        for i in range(1, 5):
            self.assertGreaterEqual(c[f"atom{i}_index"], 1)

    def test_torsion_coordinates_emitted_for_nd(self):
        # ND shape: ARC's directed_scan rotor has atom_indices as a
        # list of 4-int sub-lists. The producer emits one coordinate
        # per sub-list with contiguous coordinate_index values.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 1, "treatment": "hindered_rotor",
            "atom_indices": [[1, 2, 3, 4], [5, 2, 3, 6]],
            "pivot_atoms": [2, 3],
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        t = payload["statmech"]["torsions"][0]
        self.assertEqual(t["dimension"], 2)
        self.assertEqual(len(t["coordinates"]), 2)
        self.assertEqual(t["coordinates"][0]["coordinate_index"], 1)
        self.assertEqual(t["coordinates"][1]["coordinate_index"], 2)
        self.assertEqual(t["coordinates"][0]["atom1_index"], 1)
        self.assertEqual(t["coordinates"][1]["atom1_index"], 5)

    def test_torsion_with_missing_atom_indices_emits_summary_only(self):
        # No atom_indices at all → emit symmetry/treatment but no
        # coordinates and no dimension override. Must not fail upload.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 3, "treatment": "hindered_rotor",
            "pivot_atoms": [2, 3],  # no atom_indices key at all
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 1)
        t = torsions[0]
        self.assertEqual(t["torsion_index"], 1)
        self.assertEqual(t["symmetry_number"], 3)
        self.assertEqual(t["treatment_kind"], "hindered_rotor")
        self.assertNotIn("coordinates", t)
        self.assertNotIn("dimension", t)

    def test_torsion_with_malformed_atom_indices_emits_summary_only(self):
        # Length != 4, non-positive ints, or duplicate atoms → producer
        # logs a warning and falls back to summary-only. Asserting
        # against multiple bad-input shapes pins the same fallback for
        # all of them.
        for bad_indices in ([1, 2, 3], [1, 2, 3, 4, 5], [0, 1, 2, 3],
                            [-1, 2, 3, 4], [1, 1, 2, 3], "1234", None):
            record = self._record_with_statmech()
            record["statmech"]["torsions"] = [{
                "symmetry_number": 3, "treatment": "hindered_rotor",
                "atom_indices": bad_indices,
            }]
            _, payload = self._submit(doc=self._doc(), record=record)
            torsions = payload["statmech"]["torsions"]
            # Always one torsion emitted (fallback, not skip).
            self.assertEqual(
                len(torsions), 1,
                f"bad atom_indices={bad_indices!r} dropped the torsion entirely",
            )
            self.assertNotIn(
                "coordinates", torsions[0],
                f"bad atom_indices={bad_indices!r} produced coordinates anyway",
            )

    def test_torsion_source_scan_calculation_key_omitted(self):
        # ARC doesn't yet emit `type=scan` calcs in the bundle, so
        # there's no in-bundle key for source_scan_calculation_key to
        # reference. The server-side validator rejects keys that don't
        # resolve to a real bundle calc; producer must omit the field
        # rather than fabricate one.
        record = self._record_with_statmech()
        record["statmech"]["torsions"] = [{
            "symmetry_number": 3, "treatment": "hindered_rotor",
            "atom_indices": [5, 1, 2, 3], "pivot_atoms": [1, 2],
            # ARC has scan_path on rotor dicts, but the producer must
            # not turn that into a bundle key out of thin air.
            "scan_path": "calcs/Species/x/scan_a45/output.log",
        }]
        _, payload = self._submit(doc=self._doc(), record=record)
        t = payload["statmech"]["torsions"][0]
        self.assertNotIn("source_scan_calculation_key", t)
        self.assertNotIn("source_scan_calculation_id", t)

    # ---------------- 16: unknown enum values omitted
    def test_invalid_rigid_rotor_kind_omitted(self):
        record = self._record_with_statmech()
        record["statmech"]["rigid_rotor_kind"] = "futuristic_top"
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertNotIn("rigid_rotor_kind", payload["statmech"])

    def test_invalid_external_symmetry_omitted(self):
        # ge=1 in both ORM (CheckConstraint) and Pydantic Field.
        record = self._record_with_statmech()
        record["statmech"]["external_symmetry"] = 0
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertNotIn("external_symmetry", payload["statmech"])

    def test_optical_isomers_emitted(self):
        # Schema expansion: StatmechInBundle now accepts optical_isomers
        # (1 = no chiral center, 2 = chiral). ARC must emit the value it
        # already carries in output.yml so the R*ln(optical_isomers)
        # Arkane thermo term can be replicated server-side.
        record = self._record_with_statmech()
        record["statmech"]["optical_isomers"] = 2
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertEqual(payload["statmech"]["optical_isomers"], 2)

    def test_optical_isomers_absent_omitted(self):
        record = self._record_with_statmech()
        del record["statmech"]["optical_isomers"]
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertNotIn("optical_isomers", payload["statmech"])

    def test_optical_isomers_non_int_omitted(self):
        record = self._record_with_statmech()
        record["statmech"]["optical_isomers"] = "2"
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertNotIn("optical_isomers", payload["statmech"])

    def test_optical_isomers_zero_omitted(self):
        # Backend enforces ge=1; a spurious 0 must be dropped, not sent.
        record = self._record_with_statmech()
        record["statmech"]["optical_isomers"] = 0
        _, payload = self._submit(doc=self._doc(), record=record)
        self.assertNotIn("optical_isomers", payload["statmech"])

    def test_uses_projected_frequencies_omitted(self):
        # ARC doesn't reliably record this; the field must stay absent.
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        self.assertNotIn("uses_projected_frequencies", payload["statmech"])

    # ---------------- 17: live schema validation
    def test_payload_validates_against_tckdb_schema(self):
        _, payload = self._submit(doc=self._doc(), record=self._record_with_statmech())
        contract_validate(ComputedSpeciesUploadRequest, payload)


class TestScanCalculations(unittest.TestCase):
    """Computed-species: rotor-scan additional_calculations + statmech link.

    Pins the contract between ``arc/output.py`` (which writes
    ``species_record["additional_calculations"]`` and
    ``statmech.torsions[i]["source_scan_calculation_key"]``) and the
    TCKDB adapter (which projects those onto the bundle).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-scan-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    @staticmethod
    def _scan_result_payload():
        """A minimal but TCKDB-valid scan_result dict (1D dihedral, 3 points)."""
        return {
            "dimension": 1,
            "is_relaxed": True,
            "zero_energy_reference_hartree": -154.123456,
            "coordinates": [{
                "coordinate_index": 1,
                "coordinate_kind": "dihedral",
                "atom1_index": 5,
                "atom2_index": 1,
                "atom3_index": 2,
                "atom4_index": 3,
                "step_count": 3,
                "value_unit": "degree",
                "symmetry_number": 3,
            }],
            "points": [
                {
                    "point_index": 1,
                    "electronic_energy_hartree": -154.123456,
                    "relative_energy_kj_mol": 0.0,
                    "coordinate_values": [{
                        "coordinate_index": 1, "coordinate_value": 0.0,
                        "value_unit": "degree",
                    }],
                },
                {
                    "point_index": 2,
                    "electronic_energy_hartree": -154.121456,
                    "relative_energy_kj_mol": 5.25,
                    "coordinate_values": [{
                        "coordinate_index": 1, "coordinate_value": 120.0,
                        "value_unit": "degree",
                    }],
                },
                {
                    "point_index": 3,
                    "electronic_energy_hartree": -154.122456,
                    "relative_energy_kj_mol": 2.62,
                    "coordinate_values": [{
                        "coordinate_index": 1, "coordinate_value": 240.0,
                        "value_unit": "degree",
                    }],
                },
            ],
        }

    def _record_with_scan(self, *, with_torsion_link=True, scan_key="scan_rotor_0"):
        record = _full_record()
        # statmech torsions: one 1D rotor whose source_scan_calculation_key
        # points at the bundle-local scan calc emitted below.
        torsion = {
            "symmetry_number": 3,
            "treatment": "hindered_rotor",
            "atom_indices": [5, 1, 2, 3],
            "pivot_atoms": [1, 2],
            "barrier_kj_mol": 5.25,
        }
        if with_torsion_link:
            torsion["source_scan_calculation_key"] = scan_key
        record["statmech"] = {
            "is_linear": False,
            "external_symmetry": 1,
            "rigid_rotor_kind": "asymmetric_top",
            "harmonic_frequencies_cm1": [3000.0, 1500.0, 800.0],
            "torsions": [torsion],
        }
        record["additional_calculations"] = [{
            "key": scan_key,
            "type": "scan",
            "scan_result": self._scan_result_payload(),
        }]
        return record

    def _doc(self):
        return TestComputedSpeciesStatmechFreqScaleFactor._doc_with_fsf()

    def _submit(self, *, record, doc=None):
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{
                "key": "conf0",
                "primary_calculation": {
                    "key": "opt", "calculation_id": 1, "type": "opt", "role": "primary",
                },
                "additional_calculations": [],
            }],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=doc or self._doc(), species_record=record,
            )
        return outcome, client, json.loads(outcome.payload_path.read_text())

    # ---- 3: adapter emits a type=scan calc with scan_result
    def test_scan_calc_emitted_in_additional_calculations(self):
        _, _, payload = self._submit(record=self._record_with_scan())
        adds = payload["conformers"][0]["additional_calculations"]
        scans = [c for c in adds if c.get("type") == "scan"]
        self.assertEqual(len(scans), 1)
        scan = scans[0]
        self.assertEqual(scan["key"], "scan_rotor_0")
        self.assertEqual(scan["type"], "scan")
        self.assertIn("scan_result", scan)
        # scan_result round-tripped intact.
        self.assertEqual(scan["scan_result"]["dimension"], 1)
        self.assertEqual(len(scan["scan_result"]["points"]), 3)

    def test_scan_calc_uses_explicit_scan_level(self):
        # Scan provenance must be supplied explicitly by the producer.
        _, _, payload = self._submit(record=self._record_with_scan())
        scan = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["type"] == "scan")
        self.assertEqual(scan["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(scan["software_release"]["name"], "gaussian")

    def test_scan_calc_depends_on_opt(self):
        _, _, payload = self._submit(record=self._record_with_scan())
        scan = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["type"] == "scan")
        # Role must match the TCKDB ``DependencyRole`` enum value for
        # scan→opt edges; using a producer-side string would 422.
        self.assertEqual(scan["depends_on"],
                         [{"parent_calculation_key": "opt", "role": "scan_parent"}])

    # ---- 4: statmech torsion linked via source_scan_calculation_key
    def test_statmech_torsion_links_to_scan_key(self):
        _, _, payload = self._submit(record=self._record_with_scan())
        torsions = payload["statmech"]["torsions"]
        self.assertEqual(len(torsions), 1)
        self.assertEqual(torsions[0]["source_scan_calculation_key"], "scan_rotor_0")

    def test_statmech_torsion_has_no_link_when_record_omits_key(self):
        record = self._record_with_scan(with_torsion_link=False)
        # Drop the matching scan calc too — the torsion has no upstream
        # to point at, so producer correctly emits nothing.
        record["additional_calculations"] = []
        _, _, payload = self._submit(record=record)
        torsion = payload["statmech"]["torsions"][0]
        self.assertNotIn("source_scan_calculation_key", torsion)

    @pytest.mark.payload_refused_by_contract
    def test_no_scan_calcs_when_additional_calculations_empty(self):
        # The record's torsion still names scan_rotor_0, which is no longer
        # emitted; the adapter forwards the dangling reference unrepaired
        # and TCKDB refuses it (statmech torsion scan key undeclared).
        record = self._record_with_scan()
        record["additional_calculations"] = []
        _, _, payload = self._submit(record=record)
        adds = payload["conformers"][0]["additional_calculations"]
        self.assertEqual([c["type"] for c in adds if c.get("type") == "scan"], [])

    def test_unknown_calc_type_in_additional_calculations_skipped(self):
        # If the producer adds a future calc type before the adapter
        # learns about it, the unknown type is dropped rather than
        # uploaded blind.
        record = self._record_with_scan()
        record["additional_calculations"].append({
            "key": "future_thing", "type": "future", "future_result": {},
        })
        _, _, payload = self._submit(record=record)
        types = [c.get("type") for c in payload["conformers"][0]["additional_calculations"]]
        # Only the recognized types pass through.
        self.assertNotIn("future", types)
        self.assertIn("scan", types)

    def test_malformed_scan_calc_skipped_no_crash(self):
        # Missing scan_result / non-string key / non-dict scan_result.
        record = self._record_with_scan()
        record["additional_calculations"] = [
            {"key": "scan_rotor_0", "type": "scan"},                       # no scan_result
            {"key": "", "type": "scan", "scan_result": {"dimension": 1}},  # empty key
            {"key": "scan_rotor_2", "type": "scan", "scan_result": "oops"},# wrong shape
        ]
        # Drop torsion link so we don't expect a scan to back it.
        record["statmech"]["torsions"][0].pop("source_scan_calculation_key", None)
        _, _, payload = self._submit(record=record)
        scans = [c for c in payload["conformers"][0]["additional_calculations"]
                 if c.get("type") == "scan"]
        self.assertEqual(scans, [])  # nothing usable, nothing emitted

    # ---- 5: payload validates against the live TCKDB schema
    def test_payload_validates_against_live_schema(self):
        _, _, payload = self._submit(record=self._record_with_scan())
        contract_validate(ComputedSpeciesUploadRequest, payload)

    # ---- 6: per-point scan geometries flow through unchanged.
    #
    # The producer (``arc/output.py::_build_scan_result_for_rotor``)
    # attaches ``geometry.xyz_text`` to each scan point when aligned
    # geometries are available. The adapter must pass that through
    # unchanged so TCKDB can resolve it into ``calc_scan_point.geometry_id``.

    def _record_with_scan_geometries(self):
        record = self._record_with_scan()
        scan_calc = record["additional_calculations"][0]
        # Inject one geometry per scan point — TCKDB count-headered shape.
        for i, point in enumerate(scan_calc["scan_result"]["points"]):
            point["geometry"] = {
                "xyz_text": f"2\n\nC 0.0 0.0 {0.1 * i}\nH 1.0 0.0 0.0",
            }
        return record

    def test_scan_point_geometries_pass_through_to_payload(self):
        record = self._record_with_scan_geometries()
        _, _, payload = self._submit(record=record)
        scan = next(c for c in payload["conformers"][0]["additional_calculations"]
                    if c["type"] == "scan")
        for point in scan["scan_result"]["points"]:
            self.assertIn("geometry", point)
            self.assertIn("xyz_text", point["geometry"])
            # No DB ids may leak in.
            self.assertNotIn("geometry_id", point)
            self.assertNotIn("geometry_id", point["geometry"])
            self.assertEqual(set(point["geometry"].keys()), {"xyz_text"})

    def test_scan_with_geometries_validates_against_live_schema(self):
        _, _, payload = self._submit(record=self._record_with_scan_geometries())
        # Server's CalculationScanPointPayload now accepts inline
        # ``geometry: GeometryPayload | None`` — the bundle must validate.
        contract_validate(ComputedSpeciesUploadRequest, payload)


class TestComputedReactionStatmechBaseFields(unittest.TestCase):
    """Computed-reaction per-species statmech: subset accepted by BundleStatmechIn."""

    hessian_available = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-rxn-stm-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    @staticmethod
    def _doc_with_statmech_and_fsf():
        doc = _reaction_output_doc()
        doc["freq_scale_factor"] = 0.961
        doc["freq_scale_factor_source"] = "J. Chem. Theory Comput. 2010, 6, 2872"
        doc["freq_level"] = {
            "method": "wb97xd", "basis": "def2-tzvp", "software": "gaussian",
        }
        # Attach a statmech subdict to one reactant species so we can
        # exercise the full computed-reaction statmech path.
        for s in doc["species"]:
            if s["label"] == "CHO":
                s["statmech"] = {
                    "e0_kj_mol": 8.0,
                    "spin_multiplicity": 2,
                    "optical_isomers": 1,
                    "is_linear": False,
                    "external_symmetry": 2,
                    "point_group": "Cs",   # MUST be filtered out
                    "rigid_rotor_kind": "asymmetric_top",
                    "harmonic_frequencies_cm1": [],
                    "torsions": [
                        {"symmetry_number": 3, "treatment": "hindered_rotor",
                         "atom_indices": [1, 2, 3, 4], "pivot_atoms": [2, 3]},
                    ],
                }
        return doc

    def _submit(self, *, doc):
        client = _StubClient(response=_StubResponse({"reaction_id": 42}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}), \
                mock.patch.object(TCKDBAdapter, "_freq_hessian_available",
                                  return_value=self.hessian_available):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=_reaction_record(),
            )
        return outcome, json.loads(outcome.payload_path.read_text())

    def _r0_statmech(self, payload):
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        self.assertIn("statmech", r0)
        return r0["statmech"]

    # ---------------- 8/9/10/11: schema-supported fields propagate
    def test_external_symmetry_emitted(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        self.assertEqual(self._r0_statmech(payload)["external_symmetry"], 2)

    def test_optical_isomers_emitted(self):
        # Same shared builder as computed-species: BundleStatmechIn now
        # accepts optical_isomers, and the producer surfaces it per
        # per-species statmech block.
        doc = self._doc_with_statmech_and_fsf()
        for s in doc["species"]:
            if s["label"] == "CHO":
                s["statmech"]["optical_isomers"] = 2
        _, payload = self._submit(doc=doc)
        self.assertEqual(self._r0_statmech(payload)["optical_isomers"], 2)

    def test_optical_isomers_zero_omitted(self):
        doc = self._doc_with_statmech_and_fsf()
        for s in doc["species"]:
            if s["label"] == "CHO":
                s["statmech"]["optical_isomers"] = 0
        _, payload = self._submit(doc=doc)
        self.assertNotIn("optical_isomers", self._r0_statmech(payload))

    def test_is_linear_emitted(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        self.assertIs(self._r0_statmech(payload)["is_linear"], False)

    def test_rigid_rotor_kind_emitted(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        self.assertEqual(
            self._r0_statmech(payload)["rigid_rotor_kind"], "asymmetric_top"
        )

    def test_statmech_treatment_emitted(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        self.assertEqual(self._r0_statmech(payload)["statmech_treatment"], "rrho_1d")

    def test_statmech_treatment_omitted_and_warned_without_freq_hessian(self):
        self.hessian_available = False
        outcome, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        self.assertNotIn("statmech_treatment", self._r0_statmech(payload))
        warned = [w for w in outcome.warnings
                  if w["code"] == "statmech_treatment_not_stated"]
        self.assertEqual(len(warned), 1)
        self.assertTrue(warned[0]["field"].endswith(".statmech.statmech_treatment"))

    # ---------------- 1: point_group flows through
    def test_point_group_emitted_in_computed_reaction(self):
        # Schema expansion: BundleStatmechIn now accepts point_group.
        # The producer must surface it from the species statmech subdict
        # written by arc/output.py::_statmech_to_dict.
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        self.assertEqual(self._r0_statmech(payload)["point_group"], "Cs")

    # ---------------- 2 + 3: scoped source_calculations
    def test_source_calculations_emitted_with_scoped_keys(self):
        # Schema expansion: BundleStatmechIn now accepts
        # source_calculations. Each species block must reference only
        # its own scoped calculation keys (r0_*, p0_*, ...). Sibling
        # species and the TS use disjoint namespaces.
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        sm = self._r0_statmech(payload)
        sources = sm["source_calculations"]
        by_role = {sc["role"]: sc["calculation_key"] for sc in sources}
        self.assertEqual(by_role, {"opt": "r0_opt", "freq": "r0_freq", "sp": "r0_sp"})

    # ---------------- 4 + 5: no TS / sibling-species leakage
    def test_source_calculations_never_reference_ts_or_siblings(self):
        # Defense in depth: every species block's statmech.source_calcs
        # must reference only that species's own calc keys. Cross-actor
        # references (TS keys leaking into a reactant, or r0 keys
        # leaking into r1) would 422 server-side under the workflow's
        # owner-consistency check.
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        for species in payload["species"]:
            sm = species.get("statmech")
            if sm is None or "source_calculations" not in sm:
                continue
            own_prefix = species["key"].split("_", 1)[0] + "_"
            for sc in sm["source_calculations"]:
                key = sc["calculation_key"]
                self.assertTrue(
                    key.startswith(own_prefix),
                    f"species[{species['key']}] statmech references "
                    f"foreign calc key {key!r} (expected prefix {own_prefix!r})",
                )
                self.assertFalse(key.startswith("ts_"))

    # ---------------- 6: missing source calculations omitted cleanly
    def test_source_calculations_drop_missing_roles(self):
        # If a species lacks an SP calc (sp_energy_hartree absent), the
        # SP source must not be emitted, but opt/freq still flow.
        doc = self._doc_with_statmech_and_fsf()
        for s in doc["species"]:
            if s["label"] == "CHO":
                # Strip SP so the reaction species block emits opt+freq only.
                s["sp_energy_hartree"] = None
                s.pop("electronic_energy_hartree", None)
        _, payload = self._submit(doc=doc)
        sm = self._r0_statmech(payload)
        roles = [sc["role"] for sc in sm["source_calculations"]]
        self.assertEqual(roles, ["opt", "freq"])
        keys = {sc["calculation_key"] for sc in sm["source_calculations"]}
        self.assertEqual(keys, {"r0_opt", "r0_freq"})

    # ---------------- 14: slim torsions in reaction mode
    def test_slim_torsions_emitted(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        torsions = self._r0_statmech(payload)["torsions"]
        self.assertEqual(len(torsions), 1)
        self.assertEqual(torsions[0]["torsion_index"], 1)
        self.assertEqual(torsions[0]["symmetry_number"], 3)
        self.assertEqual(torsions[0]["treatment_kind"], "hindered_rotor")

    def test_freq_scale_factor_behavior_preserved(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        sm = self._r0_statmech(payload)
        self.assertIn("freq_scale_factor", sm)
        self.assertAlmostEqual(sm["freq_scale_factor"]["value"], 0.961)

    def test_no_statmech_when_no_fsf_and_no_subdict(self):
        # Backward-compat: a reaction doc with no FSF and no statmech
        # subdict on any species must not produce a statmech block on
        # those species.
        doc = _reaction_output_doc()  # no FSF, no statmech subdict
        _, payload = self._submit(doc=doc)
        for s in payload["species"]:
            self.assertNotIn("statmech", s)

    # ---------------- 18: live schema validation
    def test_payload_validates_against_tckdb_schema(self):
        _, payload = self._submit(doc=self._doc_with_statmech_and_fsf())
        contract_validate(ComputedReactionUploadRequest, payload)


class TestFreqNImagSpeciesEntryKindConsistency(unittest.TestCase):
    """TCKDB 0.22.0 cross-checks freq_result.n_imag against the stationary-
    point kind the record is uploaded under (species_entry_kind='minimum'
    for every species/reactant/product path; "this is the transition_state
    block" for a TS) and BLOCKS the whole upload on contradiction
    (n_imag_contradicts_minimum / transition_state_no_imaginary_mode /
    transition_state_reaction_coordinate_not_designated).

    The adapter derives the kind from ARC's ``is_ts`` flag
    (``_species_entry_payload``) completely independently of the n_imag it
    reads in ``_freq_result_payload``, so nothing used to reconcile them.

    No concrete path inside ARC's own current pipeline is known to
    produce a contradiction. ``Scheduler.check_negative_freq``
    (arc/scheduler.py) refuses to mark a non-TS species converged while
    any negative frequency remains, and refuses to mark a TS converged
    with zero. A species loaded from a pre-existing Arkane YAML
    (``species.yml_path``) looks like a bypass at first --
    ``Scheduler.schedule_jobs`` sets ``self.output[label]['convergence']
    = True`` unconditionally for it, with no frequency sanity check at
    all -- but ``ARCSpecies.from_yml_file`` never sets ``spc.freqs``
    (only ``final_xyz``, ``mol``, ``multiplicity``, ``charge``, and a
    handful of Arkane-conformer fields), and ``_get_imaginary_freqs``
    (arc/output.py) reads only ``spc.freqs`` / ``spc.
    ts_guesses[chosen].imaginary_freqs``. A YAML-loaded species reports
    ``freq_n_imag=None``, so every check below is skipped for it, not
    contradicted. The scenarios below are therefore defensive validation
    for an ``output.yml`` this adapter did not produce -- hand-written,
    third-party, or from a future/older ARC -- which the adapter
    genuinely accepts: ``evidence.py::validate_output_schema`` checks
    only ``schema_version``, never the document shape.

    On a genuine contradiction, ``_freq_result_payload`` raises
    ``ValueError`` rather than quietly omitting the offending field(s):
    omission would defeat TCKDB's own guard (both
    ``evaluate_species_entry_frequency`` and
    ``evaluate_transition_state_frequency`` report nothing when n_imag is
    absent) and deposit the record under a classification its own
    evidence disputes, with the disproving evidence quietly removed. The
    raise is deliberately left uncaught by every ``submit_*`` call site
    (see their own "build failures raise" docstrings), so it propagates to
    the sweep's per-record ``except Exception`` (``sweep.py``), which
    counts the record ``failed`` and prints
    ``failed: <label> -- <error>`` -- the loud, attributable signal a
    ``logger.warning`` alone is not.

    Separately: a converged TS can carry more than one imaginary mode even
    on ARC's normal, non-bypass path. ``check_imaginary_frequencies``
    (arc/checks/ts.py) accepts a TS as long as exactly one imaginary mode
    falls within (LOWEST_MAJOR_TS_FREQ, HIGHEST_MAJOR_TS_FREQ) = (75, 10000)
    cm-1 -- any *other* imaginary modes (small grid-noise artifacts, a
    torsional maximum riding along) are tolerated. ADR 0012 then requires
    the payload to designate which mode is the reaction coordinate, and
    the adapter designates it by that same (75, 10000) cm-1 window --
    never by magnitude alone, which can never be contradicted by a
    stiffer artifact mode (see
    ``test_ts_designates_window_mode_over_a_stiffer_artifact`` below).
    """

    # ---------------- computed-reaction path ----------------

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-nimag-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    def _submit_reaction(self, *, doc):
        client = _StubClient(response=_StubResponse({"reaction_id": 42}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=_reaction_record(),
            )
        return outcome, json.loads(outcome.payload_path.read_text())

    def _refused_reaction(self, *, doc):
        """Submit ``doc`` and return the ``ValueError`` the build raises.

        No payload/sidecar is written -- the exception propagates out of
        ``submit_computed_reaction_from_output`` before ``self._writer.
        write`` is ever reached (mirrors the caller contract every
        ``submit_*`` docstring already advertises: "build failures
        raise").
        """
        client = _StubClient(response=_StubResponse({"reaction_id": 42}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            with self.assertRaises(ValueError) as ctx:
                adapter.submit_computed_reaction_from_output(
                    output_doc=doc, reaction_record=_reaction_record(),
                )
        return ctx.exception

    def test_ts_two_imaginary_modes_designates_reaction_coordinate(self):
        """A converged TS with 2 imaginary modes (the major TS mode plus a
        small extra one check_imaginary_frequencies tolerates) must
        designate freq_reaction_coordinate_mode_index or TCKDB 0.22.0
        blocks with transition_state_reaction_coordinate_not_designated.

        ARC strips ALL negative frequencies from
        statmech.harmonic_frequencies_cm1 for a TS (arc/output.py), so the
        adapter must reinsert every entry of ``imaginary_frequencies_cm1``
        -- not just the scalar ``imag_freq_cm1`` -- to reconcile n_imag=2
        with the emitted modes. -1320.5 cm-1 is designated because it is
        the unique mode inside ARC's own (75, 10000) cm-1 "major TS mode"
        window (arc/checks/ts.py::check_imaginary_frequencies); -30.0 cm-1
        is excluded because it sits below the window. -1320.5 also happens
        to be the most negative of the two, so this alone does not
        distinguish "designate by ARC's window" from the wrong "designate
        by magnitude" rule -- see
        ``test_ts_designates_window_mode_over_a_stiffer_artifact`` for a
        case where the two rules disagree.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = [-1320.5, -30.0]
        ts["statmech"] = {"harmonic_frequencies_cm1": [800.0, 1500.0]}

        _, payload = self._submit_reaction(doc=doc)
        freq = next(
            c for c in payload["transition_state"]["calculations"]
            if c["type"] == "freq"
        )
        self.assertEqual(freq["freq_n_imag"], 2)
        self.assertEqual(
            freq["freq_frequencies_cm1"], [-1320.5, -30.0, 800.0, 1500.0]
        )
        self.assertEqual(freq["freq_reaction_coordinate_mode_index"], 1)
        # Would have raised CodedValidationError pre-fix.
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_ts_two_imaginary_modes_designates_without_statmech_harmonic_frequencies(self):
        """D4(a): n_imag=2 with NO statmech.harmonic_frequencies_cm1 at all.

        Reachable on ARC's own path: when ``spc.freqs`` is empty,
        ``_get_imaginary_freqs`` (arc/output.py) falls back to
        ``ts_guesses[chosen].imaginary_freqs`` for
        ``imaginary_frequencies_cm1``, while ``_statmech_to_dict`` builds
        ``harmonic_frequencies_cm1`` from that same empty ``spc.freqs``
        and gets ``None``. Designation must not be gated on a harmonic
        list being present -- ``modes`` here is imaginary-only.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = [-1320.5, -30.0]
        # No "statmech" key at all -- unlike the sibling test above.

        _, payload = self._submit_reaction(doc=doc)
        freq = next(
            c for c in payload["transition_state"]["calculations"]
            if c["type"] == "freq"
        )
        self.assertEqual(freq["freq_n_imag"], 2)
        self.assertEqual(freq["freq_frequencies_cm1"], [-1320.5, -30.0])
        self.assertEqual(freq["freq_reaction_coordinate_mode_index"], 1)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_ts_designates_reaction_coordinate_when_harmonic_frequencies_already_include_imaginary_modes(self):
        """D1: designation must run whenever n_imag > 1, independent of
        WHETHER ``modes`` needed reinsertion at all.

        A previous fix pass gated the whole reinsertion-and-designation
        block on ``modes`` having zero ``is_imaginary`` entries yet --
        i.e. on reinsertion being necessary. That left a hole: when
        ``statmech.harmonic_frequencies_cm1`` already carries every
        imaginary value inline (unlike ARC's usual REAL-modes-only
        convention for a TS -- e.g. a producer that doesn't follow it, or
        ARC itself on some path this adapter doesn't specifically know
        about), the guard saw modes were already internally consistent
        with n_imag and skipped designation entirely. The resulting
        payload had ``freq_n_imag=2`` and a full ``modes`` list but no
        ``freq_reaction_coordinate_mode_index`` -- exactly the
        transition_state_reaction_coordinate_not_designated finding this
        function exists to prevent, emitted from inside the function
        meant to prevent it.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = [-1320.5, -30.0]
        # Unlike the sibling tests above: both imaginary values are
        # already present in the harmonic list itself, so no reinsertion
        # is needed -- ``modes`` is already internally consistent with
        # n_imag=2 before designation ever runs.
        ts["statmech"] = {
            "harmonic_frequencies_cm1": [-1320.5, -30.0, 800.0],
        }

        _, payload = self._submit_reaction(doc=doc)
        freq = next(
            c for c in payload["transition_state"]["calculations"]
            if c["type"] == "freq"
        )
        self.assertEqual(freq["freq_n_imag"], 2)
        self.assertEqual(freq["freq_frequencies_cm1"], [-1320.5, -30.0, 800.0])
        # Would have been absent pre-fix, and model_validate below would
        # have raised transition_state_reaction_coordinate_not_designated.
        self.assertEqual(freq["freq_reaction_coordinate_mode_index"], 1)
        self.assertEqual(freq["freq_imaginary_dispositions"], {"2": "unassigned"})
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_ts_designates_window_mode_over_a_stiffer_artifact(self):
        """D5: designation follows ARC's (75, 10000) cm-1 window, not
        "most negative wins".

        -12000.0 cm-1 is the more negative of the two, but it sits
        outside ARC's own major-TS-mode window (too stiff -- an SCF/parse
        artifact per arc/checks/ts.py::check_imaginary_frequencies).
        -1320.5 cm-1 is the one ARC's own check would call the reaction
        coordinate. A magnitude-only rule would wrongly designate mode 1
        (-12000.0) here and assert a reaction coordinate TCKDB can never
        have grounds to contradict, since nothing is ever more negative
        than "most negative".

        Also covers D6 and D4 on this same fixture: the ``imag_freq_cm1``
        scalar must agree with the designated mode (not ARC's raw
        min()), and the excluded artifact mode must carry an
        ``unassigned`` disposition in ``freq_imaginary_dispositions`` --
        see ``test_removing_freq_imaginary_dispositions_would_fail_
        validation`` below for proof that disposition is load-bearing on
        this exact data.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -12000.0
        ts["imaginary_frequencies_cm1"] = [-12000.0, -1320.5]

        _, payload = self._submit_reaction(doc=doc)
        freq = next(
            c for c in payload["transition_state"]["calculations"]
            if c["type"] == "freq"
        )
        self.assertEqual(freq["freq_frequencies_cm1"], [-12000.0, -1320.5])
        # Mode 2 (-1320.5), NOT mode 1 (-12000.0, "most negative").
        self.assertEqual(freq["freq_reaction_coordinate_mode_index"], 2)
        # D6: the scalar must agree with the designation, not carry ARC's
        # raw -12000.0 artifact past a consumer that only reads the
        # scalar (e.g. tunneling correction code).
        self.assertEqual(freq["freq_imag_freq_cm1"], -1320.5)
        # D4: the excluded artifact (mode 1) is declared, not left
        # undeclared; the designated mode (mode 2) is not.
        self.assertEqual(freq["freq_imaginary_dispositions"], {"1": "unassigned"})
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_removing_freq_imaginary_dispositions_would_fail_validation(self):
        """D4: prove ``freq_imaginary_dispositions`` is load-bearing, not
        decoration, on the exact data from
        ``test_ts_designates_window_mode_over_a_stiffer_artifact``.

        -12000.0 cm-1 (mode 1, excluded/artifact) is stiffer than the
        designated -1320.5 cm-1 (mode 2): without a declared disposition,
        stationary_point.py's ambiguity check blocks because an
        undeclared extra mode is at least as stiff as the designated one
        (``W_TS_REACTION_COORDINATE_AMBIGUOUS``). With ``unassigned``
        declared, the same payload is accepted. A test that only checks
        the accept case (as the sibling test above does) can't tell
        "dispositions are required" from "dispositions are ignored" --
        this one can.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -12000.0
        ts["imaginary_frequencies_cm1"] = [-12000.0, -1320.5]

        _, payload = self._submit_reaction(doc=doc)
        freq = next(
            c for c in payload["transition_state"]["calculations"]
            if c["type"] == "freq"
        )
        dispositions = freq.pop("freq_imaginary_dispositions")
        self.assertEqual(dispositions, {"1": "unassigned"})

        with self.assertRaises(Exception) as ctx:
            contract_validate(ComputedReactionUploadRequest, payload)
        self.assertIn(
            "transition_state_reaction_coordinate_ambiguous", str(ctx.exception),
        )

        freq["freq_imaginary_dispositions"] = dispositions
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_ts_two_in_window_candidates_designates_larger_magnitude(self):
        """D5: two DISTINCT-magnitude candidates both inside ARC's (75,
        10000) cm-1 window is not the same as a genuine tie.

        TCKDB's own ambiguity check (stationary_point.py) compares with
        ``>=``, so an unequal, undesignated smaller mode never trips it
        -- only an exact tie (or a mode at least as stiff) would. The
        adapter must designate the larger-magnitude in-window candidate
        rather than refuse the whole record as ambiguous; refusal is
        reserved for a genuine tie (see
        ``test_ts_degenerate_pair_refused_as_ambiguous``) or zero
        in-window candidates.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = [-1320.5, -400.0]

        _, payload = self._submit_reaction(doc=doc)
        freq = next(
            c for c in payload["transition_state"]["calculations"]
            if c["type"] == "freq"
        )
        self.assertEqual(freq["freq_frequencies_cm1"], [-1320.5, -400.0])
        self.assertEqual(freq["freq_reaction_coordinate_mode_index"], 1)
        self.assertEqual(freq["freq_imag_freq_cm1"], -1320.5)
        self.assertEqual(freq["freq_imaginary_dispositions"], {"2": "unassigned"})
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_ts_degenerate_pair_refused_as_ambiguous(self):
        """D5: a degenerate pair sharing one magnitude, both inside ARC's
        window, is genuinely ambiguous -- refuse rather than designate
        one arbitrarily.

        Designating either arbitrarily would 422 downstream anyway:
        stationary_point.py's ambiguity check compares with ``>=``, so
        the un-designated twin (same magnitude, no declared disposition)
        would trip transition_state_reaction_coordinate_ambiguous.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = [-1320.5, -1320.5]

        exc = self._refused_reaction(doc=doc)
        self.assertIn(
            "transition_state_reaction_coordinate_not_designated", str(exc),
        )

    def test_ts_multi_imaginary_length_mismatch_refused(self):
        """D4(b): len(imaginary_frequencies_cm1) disagrees with n_imag --
        cannot say which values are the n_imag modes, so cannot designate.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = [-1320.5]  # only 1, n_imag says 2

        exc = self._refused_reaction(doc=doc)
        self.assertIn(
            "transition_state_reaction_coordinate_not_designated", str(exc),
        )

    def test_ts_multi_imaginary_without_plural_list_refused(self):
        """D4(c): imaginary_frequencies_cm1 absent entirely with n_imag>1 --
        only the scalar imag_freq_cm1 is available, which reconciles
        n_imag==1 but can never designate among several modes.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 2
        ts["imag_freq_cm1"] = -1320.5
        ts["imaginary_frequencies_cm1"] = None

        exc = self._refused_reaction(doc=doc)
        self.assertIn(
            "transition_state_reaction_coordinate_not_designated", str(exc),
        )

    def test_ts_zero_imaginary_refused(self):
        """A TS record whose own frequency evidence shows no imaginary mode
        contradicts "this is a transition state" outright
        (transition_state_no_imaginary_mode is a BLOCKING finding). The
        adapter cannot fabricate an imaginary mode that was never
        computed, so it refuses to build the record -- loudly, via a
        raise the sweep counts as a failure -- rather than quietly strip
        the disproving n_imag/imag_freq_cm1 and deposit a TS with no
        evidence either way.
        """
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["freq_n_imag"] = 0
        ts["imag_freq_cm1"] = None

        exc = self._refused_reaction(doc=doc)
        self.assertIn("transition_state_no_imaginary_mode", str(exc))

    def test_reactant_species_positive_imaginary_refused(self):
        """A reactant/product species entry always carries
        species_entry_kind='minimum' (the adapter never emits
        'vdw_complex' or a TS-flavored kind for a non-TS record). One
        reporting an imaginary mode contradicts that outright
        (n_imag_contradicts_minimum is BLOCKING). There is no vdw_complex
        evidence to fall back on, so the adapter refuses to build the
        whole reaction bundle rather than quietly strip the contradictory
        n_imag/imag_freq_cm1 and deposit the species as an undisputed
        minimum.
        """
        doc = _reaction_output_doc()
        for s in doc["species"]:
            if s["label"] == "CHO":
                s["freq_n_imag"] = 1
                s["imag_freq_cm1"] = -45.0

        exc = self._refused_reaction(doc=doc)
        self.assertIn("n_imag_contradicts_minimum", str(exc))

    # ---------------- computed-species path ----------------

    def _submit_species(self, *, record):
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{
                "key": "conf0",
                "primary_calculation": {
                    "key": "opt", "calculation_id": 1, "type": "opt", "role": "primary",
                },
                "additional_calculations": [],
            }],
        }))
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(), species_record=record,
            )
        return outcome, json.loads(outcome.payload_path.read_text())

    def _refused_species(self, *, record):
        client = _StubClient(response=_StubResponse({"species_entry_id": 1}))
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )
        adapter = TCKDBAdapter(cfg, client_factory=lambda c, k: client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            with self.assertRaises(ValueError) as ctx:
                adapter.submit_computed_species_from_output(
                    output_doc=_fake_output_doc(), species_record=record,
                )
        return ctx.exception

    def test_computed_species_minimum_with_positive_imaginary_refused(self):
        """Same contract on the /uploads/computed-species path: a species
        bundle's species_entry_kind is always 'minimum' (is_ts=False is
        the only value that reaches this endpoint), so a record reporting
        an imaginary mode must refuse the whole bundle rather than upload
        it as a self-contradictory 'minimum' with the evidence stripped.
        """
        record = _full_record()
        record["freq_n_imag"] = 1
        record["imag_freq_cm1"] = -20.0

        exc = self._refused_species(record=record)
        self.assertIn("n_imag_contradicts_minimum", str(exc))

    def test_computed_species_minimum_with_negative_harmonic_entry_refused(self):
        """D7: apply the same standard when the contradiction shows up in
        ``statmech.harmonic_frequencies_cm1`` instead of the
        ``freq_n_imag``/``imag_freq_cm1`` scalars directly.
        ``freq_n_imag=0`` with a negative value hiding in the harmonic
        list is the identical n_imag_contradicts_minimum contradiction --
        just discovered through the frequency list. Quietly stripping the
        negative value and depositing ``freq_n_imag=0`` would defeat
        TCKDB's guard exactly as D2's docstring condemns; refuse the
        whole record instead, the same as when the scalar field itself
        carries the contradiction.
        """
        record = _full_record()
        record["freq_n_imag"] = 0
        record["statmech"] = {"harmonic_frequencies_cm1": [-45.0, 800.0]}

        exc = self._refused_species(record=record)
        self.assertIn("n_imag_contradicts_minimum", str(exc))


class TestComputedReactionBundle(unittest.TestCase):
    """Producer-side tests for the /uploads/computed-reaction bundle path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-rxn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
        )

    def _adapter(self, client, *, project_directory=None, cfg=None):
        return TCKDBAdapter(
            cfg or self.cfg,
            project_directory=project_directory,
            client_factory=lambda c, k: client,
        )

    def _submit(self, *, output_doc=None, reaction=None, client=None,
                project_directory=None, cfg=None):
        client = client or _StubClient(response=_StubResponse({"reaction_id": 42}))
        adapter = self._adapter(client, project_directory=project_directory, cfg=cfg)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=output_doc or _reaction_output_doc(),
                reaction_record=reaction or _reaction_record(),
            )
        return outcome, client, json.loads(outcome.payload_path.read_text())

    def test_every_emitted_origin_kind_is_a_valid_enum_member(self):
        # Same schema-conformance guard as the species bundle, on the
        # reaction path: any origin_kind on any species/TS calc row must
        # be a valid backend enum member. The reaction bundle carries the
        # reused-result SP marker on its species blocks.
        from tckdb_arc.adapter import VALID_TCKDB_ORIGIN_KINDS
        _, _, payload = self._submit()
        found = []

        def _walk(obj):
            if isinstance(obj, dict):
                if "origin_kind" in obj:
                    found.append(obj["origin_kind"])
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(payload)
        for kind in found:
            self.assertIn(
                kind, VALID_TCKDB_ORIGIN_KINDS,
                f"origin_kind {kind!r} is not a valid backend enum member",
            )

    # ---------------- 1: payload top-level shape
    def test_payload_top_level_keys(self):
        _, _, payload = self._submit()
        self.assertIn("species", payload)
        self.assertIn("reactant_keys", payload)
        self.assertIn("product_keys", payload)
        self.assertIn("transition_state", payload)
        self.assertIn("kinetics", payload)
        self.assertEqual(payload["reaction_family"], "H_Abstraction")

    # ---------------- 2: deterministic local keys for species
    def test_species_local_keys_namespaced(self):
        _, _, payload = self._submit()
        self.assertEqual(payload["reactant_keys"], ["r0_CHO", "r1_CH4"])
        self.assertEqual(payload["product_keys"], ["p0_CH2O", "p1_CH3"])
        species_keys = sorted(s["key"] for s in payload["species"])
        self.assertEqual(species_keys, ["p0_CH2O", "p1_CH3", "r0_CHO", "r1_CH4"])

    def test_reaction_species_entries_do_not_assert_an_electronic_state(self):
        """Reaction-bundle species_entry blocks omit ``electronic_state_kind``
        like the species bundle; both go through ``_species_entry_payload``."""
        _, _, payload = self._submit()
        for sp in payload["species"]:
            self.assertNotIn("electronic_state_kind", sp["species_entry"])
            for absent in (
                "stereo_label",
                "electronic_state_label",
                "term_symbol",
                "term_symbol_raw",
                "isotopologue_label",
            ):
                self.assertNotIn(absent, sp["species_entry"])

    # ---------------- 3: species blocks have one conformer + opt + freq + sp
    def test_species_block_contains_opt_freq_sp(self):
        _, _, payload = self._submit()
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        self.assertEqual(r0["species_entry"]["smiles"], "[CH]=O")
        self.assertEqual(len(r0["conformers"]), 1)
        primary = r0["conformers"][0]["calculation"]
        self.assertEqual(primary["key"], "r0_opt")
        self.assertEqual(primary["type"], "opt")
        additional_keys = sorted(c["key"] for c in r0["calculations"])
        self.assertEqual(additional_keys, ["r0_freq", "r0_sp"])

    def test_species_freq_modes_flattened_to_freq_frequencies_cm1(self):
        """Computed-reaction bundles flatten freq_result.modes into the
        sibling list[float] freq_frequencies_cm1. The wrapper is gone."""
        doc = _reaction_output_doc()
        cho = next(s for s in doc["species"] if s["label"] == "CHO")
        cho["statmech"] = {"harmonic_frequencies_cm1": [800.0, 1500.0, 3000.0]}
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        freq = next(c for c in r0["calculations"] if c["type"] == "freq")
        self.assertNotIn("freq_result", freq)
        self.assertEqual(freq["freq_frequencies_cm1"], [800.0, 1500.0, 3000.0])

    def test_ts_freq_modes_flattened_to_freq_frequencies_cm1(self):
        """Same flattening applies on the TS branch — sign preserved so
        the imaginary mode round-trips as a negative float."""
        doc = _reaction_output_doc()
        doc["transition_states"][0]["statmech"] = {
            "harmonic_frequencies_cm1": [-1320.5, 800.0, 1500.0],
        }
        _, _, payload = self._submit(output_doc=doc)
        ts_calcs = payload["transition_state"]["calculations"]
        freq = next(c for c in ts_calcs if c["type"] == "freq")
        self.assertNotIn("freq_result", freq)
        self.assertEqual(freq["freq_frequencies_cm1"], [-1320.5, 800.0, 1500.0])

    # ---------------- 3a: rotor-scan calcs emitted alongside opt/freq/sp
    #
    # Regression: ``_build_reaction_species_block`` previously emitted
    # only opt/freq/sp; statmech torsions referencing ``scan_rotor_<i>``
    # ended up dangling and the server's bundle validator 422'd with
    # ``source_scan_calculation_key references undefined calculation_key``.
    # The fix mirrors the ``_build_conformer_block`` scan loop into the
    # reaction-side species builder.
    @staticmethod
    def _scan_result_payload():
        """Minimal but TCKDB-valid 1D dihedral scan_result, 3 points."""
        return {
            "dimension": 1,
            "is_relaxed": True,
            "zero_energy_reference_hartree": -113.7,
            "coordinates": [{
                "coordinate_index": 1,
                "coordinate_kind": "dihedral",
                "atom1_index": 1,
                "atom2_index": 2,
                "atom3_index": 3,
                "atom4_index": 4,
                "step_count": 3,
                "value_unit": "degree",
                "symmetry_number": 3,
            }],
            "points": [
                {
                    "point_index": 1,
                    "electronic_energy_hartree": -113.7,
                    "relative_energy_kj_mol": 0.0,
                    "coordinate_values": [{
                        "coordinate_index": 1, "coordinate_value": 0.0,
                        "value_unit": "degree",
                    }],
                },
                {
                    "point_index": 2,
                    "electronic_energy_hartree": -113.69,
                    "relative_energy_kj_mol": 5.25,
                    "coordinate_values": [{
                        "coordinate_index": 1, "coordinate_value": 120.0,
                        "value_unit": "degree",
                    }],
                },
                {
                    "point_index": 3,
                    "electronic_energy_hartree": -113.695,
                    "relative_energy_kj_mol": 2.62,
                    "coordinate_values": [{
                        "coordinate_index": 1, "coordinate_value": 240.0,
                        "value_unit": "degree",
                    }],
                },
            ],
        }

    def _doc_with_scan_on_reactant(self, *, scan_keys=("scan_rotor_0",),
                                   target_label="CHO"):
        """Augment one reactant species with hindered-rotor torsions and
        matching ``additional_calculations`` scan entries. Mirrors what
        ``arc/output.py`` writes into ``output.yml`` for a species with
        parsed 1D rotors."""
        doc = _reaction_output_doc()
        spc = next(s for s in doc["species"] if s["label"] == target_label)
        spc["additional_calculations"] = [
            {"key": k, "type": "scan", "scan_result": self._scan_result_payload()}
            for k in scan_keys
        ]
        spc["statmech"] = {
            "is_linear": False,
            "external_symmetry": 1,
            "rigid_rotor_kind": "asymmetric_top",
            "harmonic_frequencies_cm1": [3000.0, 1500.0, 800.0],
            "torsions": [
                {
                    "symmetry_number": 3,
                    "treatment": "hindered_rotor",
                    "atom_indices": [1, 2, 3, 4],
                    "pivot_atoms": [2, 3],
                    "barrier_kj_mol": 5.25,
                    "source_scan_calculation_key": k,
                }
                for k in scan_keys
            ],
        }
        return doc

    def test_scan_calcs_emitted_in_species_calculations(self):
        doc = self._doc_with_scan_on_reactant()
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        scans = [c for c in r0["calculations"] if c.get("type") == "scan"]
        self.assertEqual(len(scans), 1)
        scan = scans[0]
        # Per-species namespace: ``validate_unique_keys`` enforces
        # globally-unique calc keys across the bundle, so the producer
        # prefixes scan keys with the species's calc_prefix (``r0`` for
        # the first reactant). The matching rewrite happens on the
        # torsion side, asserted below.
        self.assertEqual(scan["key"], "r0_scan_rotor_0")
        self.assertEqual(scan["type"], "scan")
        self.assertIn("scan_result", scan)
        self.assertEqual(scan["scan_result"]["dimension"], 1)
        self.assertEqual(len(scan["scan_result"]["points"]), 3)

    def test_scan_calc_carries_geometry_key_to_conformer(self):
        # Reaction-path schema requires non-opt species calcs to point
        # at the conformer geometry by key (same reason freq/sp do).
        doc = self._doc_with_scan_on_reactant()
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        scan = next(c for c in r0["calculations"] if c["type"] == "scan")
        self.assertEqual(scan["geometry_key"], "r0_CHO_geom")

    def test_scan_calc_depends_on_species_opt(self):
        # Edge points back to *this* species's opt — not the TS's,
        # not a sibling reactant's. Owner-consistent or 422.
        doc = self._doc_with_scan_on_reactant()
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        scan = next(c for c in r0["calculations"] if c["type"] == "scan")
        self.assertEqual(
            scan["depends_on"],
            [{"parent_calculation_key": "r0_opt", "role": "scan_parent"}],
        )

    def test_statmech_torsion_reference_rewritten_to_namespaced_key(self):
        # The torsion's ``source_scan_calculation_key`` must be rewritten
        # in lockstep with the calc-key namespacing — otherwise it would
        # dangle and 422.
        doc = self._doc_with_scan_on_reactant()
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        torsion_refs = {
            t["source_scan_calculation_key"]
            for t in r0["statmech"]["torsions"]
            if "source_scan_calculation_key" in t
        }
        emitted_calc_keys = {
            c["key"] for c in r0["calculations"] if c.get("type") == "scan"
        }
        self.assertEqual(torsion_refs, {"r0_scan_rotor_0"})
        # And the rewritten key resolves to an actually-emitted calc.
        self.assertTrue(torsion_refs.issubset(emitted_calc_keys))

    def test_scan_keys_namespaced_per_species_to_avoid_collisions(self):
        # Two reactants both report a raw ``scan_rotor_0`` from output.yml.
        # Globally-unique-key validation requires the producer namespace
        # them as ``r0_scan_rotor_0`` and ``r1_scan_rotor_0``; both
        # torsions correspondingly point at their species's namespaced
        # calc, so neither dangles.
        doc = self._doc_with_scan_on_reactant(target_label="CHO")
        ch4 = next(s for s in doc["species"] if s["label"] == "CH4")
        ch4["additional_calculations"] = [{
            "key": "scan_rotor_0",
            "type": "scan",
            "scan_result": self._scan_result_payload(),
        }]
        ch4["statmech"] = {
            "is_linear": False,
            "external_symmetry": 1,
            "rigid_rotor_kind": "asymmetric_top",
            "harmonic_frequencies_cm1": [3000.0, 1500.0, 800.0],
            "torsions": [{
                "symmetry_number": 3,
                "treatment": "hindered_rotor",
                "atom_indices": [1, 2, 3, 4],
                "pivot_atoms": [2, 3],
                "barrier_kj_mol": 5.25,
                "source_scan_calculation_key": "scan_rotor_0",
            }],
        }
        _, _, payload = self._submit(output_doc=doc)
        # Calc keys disjoint between the two reactants.
        expected_by_species = {
            "r0_CHO": "r0_scan_rotor_0",
            "r1_CH4": "r1_scan_rotor_0",
        }
        for sp_key, expected_calc_key in expected_by_species.items():
            sp = next(s for s in payload["species"] if s["key"] == sp_key)
            scans = [c for c in sp["calculations"] if c.get("type") == "scan"]
            self.assertEqual([s["key"] for s in scans], [expected_calc_key])
            torsion_refs = [
                t.get("source_scan_calculation_key")
                for t in sp["statmech"]["torsions"]
            ]
            self.assertEqual(torsion_refs, [expected_calc_key])
        # Sanity: globally unique across the whole bundle.
        all_calc_keys = []
        for sp in payload["species"]:
            for c in sp["calculations"]:
                all_calc_keys.append(c["key"])
            for conf in sp["conformers"]:
                all_calc_keys.append(conf["calculation"]["key"])
        self.assertEqual(len(set(all_calc_keys)), len(all_calc_keys),
                         msg=f"duplicate calc keys: {all_calc_keys}")

    def test_payload_with_scans_validates_against_live_reaction_schema(self):
        doc = self._doc_with_scan_on_reactant(
            scan_keys=("scan_rotor_0", "scan_rotor_1"),
        )
        _, _, payload = self._submit(output_doc=doc)
        # If any torsion's source_scan_calculation_key still dangled,
        # this validator (computed_reaction_upload.py:840-848) would
        # raise — exactly the 422 we hit in the field.
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- 3b: per-point scan geometries flow through to the bundle
    def test_scan_point_geometries_pass_through_in_reaction_bundle(self):
        # Producer (``arc/output.py``) emits ``points[i].geometry.xyz_text``
        # in the scan_result; the reaction-bundle adapter must preserve
        # that intact so TCKDB resolves it into calc_scan_point.geometry_id.
        doc = self._doc_with_scan_on_reactant()
        spc = next(s for s in doc["species"] if s["label"] == "CHO")
        scan_entry = spc["additional_calculations"][0]
        for i, point in enumerate(scan_entry["scan_result"]["points"]):
            point["geometry"] = {
                "xyz_text": f"2\n\nC 0.0 0.0 {0.1 * i}\nH 1.0 0.0 0.0",
            }
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        scan = next(c for c in r0["calculations"] if c["type"] == "scan")
        # Every scan point came through with its geometry intact;
        # nothing was rewritten or coerced by the adapter.
        for point in scan["scan_result"]["points"]:
            self.assertIn("geometry", point)
            self.assertEqual(set(point["geometry"].keys()), {"xyz_text"})
            self.assertNotIn("geometry_id", point)
            self.assertNotIn("geometry_id", point["geometry"])

    # ---------------- 4: inline TS block with charge, multiplicity, geometry, calcs
    def test_ts_block_inline(self):
        _, _, payload = self._submit()
        ts = payload["transition_state"]
        self.assertEqual(ts["charge"], 0)
        self.assertEqual(ts["multiplicity"], 2)
        self.assertEqual(ts["geometry"]["key"], "ts_geom")
        self.assertIn("xyz_text", ts["geometry"])
        self.assertEqual(ts["calculation"]["key"], "ts_opt")
        self.assertEqual(ts["calculation"]["type"], "opt")
        ts_additional_keys = sorted(c["key"] for c in ts["calculations"])
        self.assertEqual(ts_additional_keys, ["ts_freq", "ts_sp"])
        self.assertEqual(ts["label"], "TS0")

    # ---------------- TS unmapped_smiles handle
    def test_ts_unmapped_smiles_built_from_reactant_product_smiles(self):
        # Default fixture: TS has no smiles of its own, but every
        # reactant/product carries one. Producer should synthesize a
        # reaction-SMILES handle ``"<r1>.<r2>>><p1>.<p2>"``.
        _, _, payload = self._submit()
        ts = payload["transition_state"]
        # Fixture SMILES: CHO=[CH]=O, CH4=C, CH2O=C=O, CH3=[CH3].
        self.assertEqual(ts.get("unmapped_smiles"), "[CH]=O.C>>C=O.[CH3]")

    def test_ts_block_carries_no_mol_field(self):
        # Producer must not invent a normal-molecule representation
        # from the TS geometry. ``mol`` (under any common spelling)
        # must not appear anywhere in the TS subtree.
        _, _, payload = self._submit()
        text = json.dumps(payload["transition_state"])
        for forbidden in ("\"mol\":", "\"rdkit_mol\":", "\"mol_object\":"):
            self.assertNotIn(forbidden, text)

    def test_ts_unmapped_smiles_omitted_when_smiles_missing(self):
        # If any reactant/product is missing a SMILES, the producer
        # refuses to emit a half-built handle — better null than
        # misleading.
        doc = _reaction_output_doc()
        cho = next(s for s in doc["species"] if s["label"] == "CHO")
        cho["smiles"] = None
        # Adapter requires SMILES on every species_entry, so we have
        # to bypass that gate to test ts-side behavior in isolation:
        # direct-call the helper rather than running the full submit.
        from tckdb_arc.adapter import _ts_unmapped_smiles_handle, _index_species
        species_index = _index_species(doc)
        ts_record = doc["transition_states"][0]
        rxn = doc["reactions"][0]
        self.assertIsNone(_ts_unmapped_smiles_handle(
            ts_record=ts_record, reaction_record=rxn, species_index=species_index,
        ))

    def test_ts_record_smiles_takes_precedence_over_derived_handle(self):
        # When ARC happens to attach an explicit SMILES to the TS
        # record itself (rare), it should win over the derived
        # reaction handle: it's higher-fidelity producer intent.
        doc = _reaction_output_doc()
        doc["transition_states"][0]["smiles"] = "[H]...[CH3]"  # nonsense but explicit
        _, _, payload = self._submit(output_doc=doc)
        self.assertEqual(
            payload["transition_state"].get("unmapped_smiles"), "[H]...[CH3]"
        )

    def test_ts_unmapped_smiles_is_deterministic(self):
        # Same output_doc → same payload string → same idempotency key.
        outcome1, _, payload1 = self._submit()
        outcome2, _, payload2 = self._submit()
        self.assertEqual(
            payload1["transition_state"]["unmapped_smiles"],
            payload2["transition_state"]["unmapped_smiles"],
        )
        self.assertEqual(outcome1.idempotency_key, outcome2.idempotency_key)

    def test_ts_unmapped_smiles_does_not_perturb_kinetics_or_irc(self):
        # Adding a TS handle is a TS-block-only concern. Kinetics,
        # source_calculations, and any IRC subtree must be byte-equal
        # to a baseline payload built by emptying out unmapped_smiles.
        baseline_doc = _reaction_output_doc()
        ts_baseline_smiles = baseline_doc["transition_states"][0].get("smiles")
        _, _, payload = self._submit(output_doc=baseline_doc)

        # Re-build with the TS handle forced to None via direct stub
        # of the helper. Compare kinetics + (if present) IRC subtrees.
        from unittest.mock import patch
        with patch("tckdb_arc.adapter._ts_unmapped_smiles_handle", return_value=None):
            _, _, payload_no_handle = self._submit(output_doc=baseline_doc)

        self.assertEqual(payload["kinetics"], payload_no_handle["kinetics"])
        self.assertEqual(
            [c for c in payload["transition_state"]["calculations"]],
            [c for c in payload_no_handle["transition_state"]["calculations"]],
        )
        # The TS primary calc and geometry are unaffected too.
        self.assertEqual(
            payload["transition_state"]["calculation"],
            payload_no_handle["transition_state"]["calculation"],
        )

    def test_ts_unmapped_smiles_payload_validates_against_tckdb_schema(self):
        _, _, payload = self._submit()
        # Sanity: the field is populated, not just bypassed.
        self.assertTrue(payload["transition_state"].get("unmapped_smiles"))
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_ts_block_includes_irc_when_present(self):
        doc = _reaction_output_doc(with_irc=True)
        _, _, payload = self._submit(output_doc=doc)
        ts = payload["transition_state"]
        ts_calc_keys = {c["key"] for c in ts["calculations"]}
        self.assertIn("ts_irc", ts_calc_keys)
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        self.assertEqual(irc_calc["type"], "irc")
        # IRC carries depends_on(role=irc_start) → ts_opt: IRC is seeded
        # from the optimized TS saddle, so ts_opt is the geometry-
        # producing parent. NOT freq_on (the TS freq validates the
        # saddle but isn't the seed geometry).
        self.assertEqual(
            irc_calc["depends_on"],
            [{"parent_calculation_key": "ts_opt", "role": "irc_start"}],
        )

    # ---------------- 5: kinetics: modified-Arrhenius mapping + units
    def test_kinetics_modified_arrhenius_mapping(self):
        _, _, payload = self._submit()
        self.assertEqual(len(payload["kinetics"]), 1)
        kin = payload["kinetics"][0]
        self.assertEqual(kin["model_kind"], "modified_arrhenius")
        self.assertAlmostEqual(kin["a"], 0.204298)
        self.assertEqual(kin["a_units"], "cm3_mol_s")
        self.assertAlmostEqual(kin["n"], 4.37949)
        self.assertAlmostEqual(kin["reported_ea"], 78.9012)
        self.assertEqual(kin["reported_ea_units"], "kj_mol")
        self.assertAlmostEqual(kin["tmin_k"], 300.0)
        self.assertAlmostEqual(kin["tmax_k"], 3000.0)
        self.assertEqual(kin["reactant_keys"], ["r0_CHO", "r1_CH4"])
        self.assertEqual(kin["product_keys"], ["p0_CH2O", "p1_CH3"])
        # And the whole bundle satisfies the real validator, not just a
        # dict-shape assertion.
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- C-4 / adversarial-review D1: A/Ea with no usable
    # unit must never be deposited as a bare (unitless) magnitude — see
    # PHASE_C_PLAN.md C-4 — but an unresolvable unit on this one
    # *optional* field must never take down the whole reaction bundle
    # either. ``A_units: null`` alongside a numeric ``A`` is valid,
    # deliberate ARC output (``arc/schemas/output_yml_schema.json``:
    # ``A_units`` is ``["string","null"]`` and required; ``arc/output.py``
    # emits ``null`` whenever ``A`` isn't a ``(value, unit)`` tuple), so
    # raising here would discard a reaction's species/TS/geometry/IRC
    # data over one missing rate-coefficient unit. The fix: omit
    # ``a``/``a_units`` (or ``reported_ea``/``reported_ea_units``) and
    # keep building everything else. These tests cover both hand-written/
    # third-party ``output.yml`` (unrecognized unit strings — ``evidence.py``
    # only checks ``schema_version``, and ARC's own JSON schema doesn't
    # constrain ``A_units``/``Ea_units`` to an enum) and ARC's own
    # documented ``null``-unit shape.
    def test_kinetics_a_units_unrecognized_omits_a_and_keeps_rest(self):
        rxn = _reaction_record()
        rxn["kinetics"]["A_units"] = "bushels/fortnight"
        with self.assertLogs("tckdb_arc", level="WARNING") as log_ctx:
            _, _, payload = self._submit(reaction=rxn)
        self.assertTrue(any("A_units" in m for m in log_ctx.output))
        kin = payload["kinetics"][0]
        self.assertNotIn("a", kin)
        self.assertNotIn("a_units", kin)
        # The rest of the kinetics block survives.
        self.assertAlmostEqual(kin["n"], 4.37949)
        self.assertAlmostEqual(kin["reported_ea"], 78.9012)
        self.assertEqual(kin["reported_ea_units"], "kj_mol")
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_a_units_missing_omits_a_and_keeps_rest(self):
        # A present, A_units absent entirely — same defect as an
        # unrecognized string: a magnitude with an implied-but-unstated
        # unit is exactly as wrong as one with a typo'd unit, and gets
        # the same "omit, don't discard the bundle" treatment.
        rxn = _reaction_record()
        rxn["kinetics"].pop("A_units")
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        self.assertNotIn("a", kin)
        self.assertNotIn("a_units", kin)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_a_units_explicit_null_omits_a_and_keeps_rest(self):
        # D1 repro: ARC's documented, deliberate ``A_units: null`` shape
        # (numeric A, unit explicitly null) -- not just a missing key.
        rxn = _reaction_record()
        rxn["kinetics"]["A_units"] = None
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        self.assertNotIn("a", kin)
        self.assertNotIn("a_units", kin)
        self.assertAlmostEqual(kin["reported_ea"], 78.9012)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_ea_units_unrecognized_omits_ea_and_keeps_rest(self):
        rxn = _reaction_record()
        rxn["kinetics"]["Ea_units"] = "erg/molecule"
        with self.assertLogs("tckdb_arc", level="WARNING") as log_ctx:
            _, _, payload = self._submit(reaction=rxn)
        self.assertTrue(any("Ea_units" in m for m in log_ctx.output))
        kin = payload["kinetics"][0]
        self.assertNotIn("reported_ea", kin)
        self.assertNotIn("reported_ea_units", kin)
        self.assertAlmostEqual(kin["a"], 0.204298)
        self.assertEqual(kin["a_units"], "cm3_mol_s")
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_ea_units_missing_omits_ea_and_keeps_rest(self):
        rxn = _reaction_record()
        rxn["kinetics"].pop("Ea_units")
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        self.assertNotIn("reported_ea", kin)
        self.assertNotIn("reported_ea_units", kin)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_a_present_n_and_ea_absent_still_omits_bad_a_units(self):
        # The omit-not-raise behavior must fire even when A is the only
        # substantive field (not masked by a healthy Ea alongside it).
        rxn = _reaction_record()
        rxn["kinetics"] = {
            "A": 1.0e13, "A_units": "not-a-unit",
            "n": None, "Ea": None, "Ea_units": None,
            "Tmin_k": None, "Tmax_k": None,
        }
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        self.assertNotIn("a", kin)
        self.assertNotIn("a_units", kin)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_recognized_units_do_not_omit_and_validate(self):
        # Control case: a fully-recognized unit pair still builds and
        # validates cleanly — the omit path only fires on the
        # unresolvable case, it doesn't regress the happy path.
        _, _, payload = self._submit()
        kin = payload["kinetics"][0]
        self.assertIn("a", kin)
        self.assertIn("a_units", kin)
        self.assertIn("reported_ea", kin)
        self.assertIn("reported_ea_units", kin)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_kinetics_a_units_null_reaction_still_uploads_species_ts_geometry(self):
        # D1: the defect under review was that an unresolvable A unit
        # discarded the *entire* upload -- species blocks, TS block,
        # geometries, IRC and path-search data -- because the kinetics
        # builder raised past an unguarded call site
        # (``_build_computed_reaction_payload``). Prove the fix: with
        # ARC's own deliberate ``A_units: null`` shape, everything except
        # the rate coefficient still lands, and the whole payload still
        # validates against the real model.
        rxn = _reaction_record()
        rxn["kinetics"]["A_units"] = None
        doc = _reaction_output_doc(with_irc=True)
        _, _, payload = self._submit(output_doc=doc, reaction=rxn)

        # Species blocks: both reactants and both products present.
        species_keys = sorted(s["key"] for s in payload["species"])
        self.assertEqual(species_keys, ["p0_CH2O", "p1_CH3", "r0_CHO", "r1_CH4"])
        for sp in payload["species"]:
            self.assertTrue(sp["calculations"], f"species {sp['key']} lost its calculations")

        # TS block, with geometry and an IRC calc slot still present.
        self.assertIsNotNone(payload["transition_state"])
        self.assertTrue(
            payload["transition_state"].get("geometry", {}).get("xyz_text"),
            "TS geometry must survive an unresolvable A unit",
        )
        ts_calcs = payload["transition_state"]["calculations"]
        self.assertTrue(ts_calcs)
        has_irc_calc = any(c.get("type") == "irc" for c in ts_calcs)
        self.assertTrue(has_irc_calc, "IRC calc slot must survive an unresolvable A unit")

        # Kinetics: rate coefficient omitted, not the whole record -- and
        # the kinetics builder still links the IRC calc, proof IRC
        # survived into source_calculations, not just the TS block.
        kin = payload["kinetics"][0]
        irc_roles = [e for e in kin["source_calculations"] if e["role"] == "irc"]
        self.assertEqual(len(irc_roles), 1)
        self.assertEqual(irc_roles[0]["calculation_key"], "ts_irc")
        self.assertNotIn("a", kin)
        self.assertNotIn("a_units", kin)
        self.assertAlmostEqual(kin["reported_ea"], 78.9012)

        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- tunneling_model passthrough
    def test_kinetics_tunneling_model_passes_through(self):
        # output.yml records the tunneling method ARC asked Arkane to
        # apply (currently always Eckart). The adapter surfaces it as
        # ``tunneling_model`` on the BundleKineticsIn so the DB row records
        # which correction was applied to A/n/Ea, spelled as the
        # contract's ``TunnelingModel`` token (the value the server stores).
        _, _, payload = self._submit()
        self.assertEqual(payload["kinetics"][0]["tunneling_model"], "eckart")

    def test_kinetics_tunneling_model_arbitrary_value(self):
        # TCKDB's tunneling_model is the ``TunnelingModel`` enum (none,
        # wigner, eckart, sct, other). A known method maps to its token;
        # one the enum does not name is ``other``, exactly as the server's
        # ``normalize_tunneling_model`` would store it.
        for arc_value, token in (("Wigner", "wigner"), ("Skodje-Truhlar", "other")):
            with self.subTest(arc_value=arc_value):
                rxn = _reaction_record()
                rxn["kinetics"]["tunneling"] = arc_value
                _, _, payload = self._submit(reaction=rxn)
                self.assertEqual(payload["kinetics"][0]["tunneling_model"], token)

    def test_kinetics_tunneling_field_omitted_when_absent(self):
        # Backward compat: output.yml from before the tunneling-surfacing
        # change has no 'tunneling' key. The adapter must omit
        # tunneling_model entirely (not emit ``null``) so older payloads
        # stay structurally equivalent to what they would have produced
        # before this change landed.
        rxn = _reaction_record()
        rxn["kinetics"].pop("tunneling", None)
        _, _, payload = self._submit(reaction=rxn)
        self.assertNotIn("tunneling_model", payload["kinetics"][0])

    def test_kinetics_tunneling_dict_with_method_extracts_label(self):
        # If output.yml ever carries tunneling as a structured object
        # (e.g. method + cutoff), the adapter must surface a clean label
        # rather than a Python repr or stringified dict.
        rxn = _reaction_record()
        rxn["kinetics"]["tunneling"] = {"method": "Eckart", "cutoff": 0.5}
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["tunneling_model"], "eckart")

    def test_kinetics_tunneling_dict_with_model_key(self):
        # Producers may use 'model' instead of 'method' as the canonical
        # key; helper should accept both.
        rxn = _reaction_record()
        rxn["kinetics"]["tunneling"] = {"model": "Wigner"}
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["tunneling_model"], "wigner")

    def test_kinetics_tunneling_dict_without_canonical_falls_back_to_other(self):
        # No recognized key: the object names no tunneling method the
        # enum knows, so the token is ``other`` (the deterministic JSON
        # label the helper builds is not a ``TunnelingModel`` value).
        rxn = _reaction_record()
        rxn["kinetics"]["tunneling"] = {"foo": "bar", "cutoff": 0.5}
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["tunneling_model"], "other")

    def test_kinetics_tunneling_empty_string_omits(self):
        # Empty / whitespace-only strings carry no information; treat
        # them the same as a missing key.
        rxn = _reaction_record()
        rxn["kinetics"]["tunneling"] = "   "
        _, _, payload = self._submit(reaction=rxn)
        self.assertNotIn("tunneling_model", payload["kinetics"][0])

    # ---------------- degeneracy: ``BundleKineticsIn.degeneracy``
    # TCKDB now accepts ``degeneracy: float | None`` with ``gt=0`` on
    # the bundle-context kinetics schema. The adapter forwards an
    # explicit positive source value and otherwise omits — never
    # defaults to 1, never infers from stoichiometry, never re-routes
    # into note/parameters_json.
    def test_kinetics_degeneracy_emitted_when_positive(self):
        rxn = _reaction_record()
        rxn["kinetics"]["degeneracy"] = 2
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["degeneracy"], 2.0)

    def test_kinetics_degeneracy_emitted_as_float(self):
        # Source values may arrive as int, float, or numeric string.
        # Adapter coerces to float for the wire.
        for raw in (2, 2.0, "2.0", "2"):
            with self.subTest(raw=raw):
                rxn = _reaction_record()
                rxn["kinetics"]["degeneracy"] = raw
                _, _, payload = self._submit(reaction=rxn)
                self.assertEqual(payload["kinetics"][0]["degeneracy"], 2.0)
                self.assertIsInstance(
                    payload["kinetics"][0]["degeneracy"], float,
                )

    def test_kinetics_degeneracy_omitted_when_absent(self):
        # Default fixture has no degeneracy; payload must too. Schema
        # treats missing as NULL — *not* a defaulted 1.0.
        _, _, payload = self._submit()
        self.assertNotIn("degeneracy", payload["kinetics"][0])

    def test_kinetics_degeneracy_omitted_when_none_or_empty(self):
        # Explicit None / empty-string from a hand-edited output.yml
        # must NOT default to a number — omit and let the server
        # column stay NULL.
        for raw in (None, "", "   "):
            with self.subTest(raw=repr(raw)):
                rxn = _reaction_record()
                rxn["kinetics"]["degeneracy"] = raw
                _, _, payload = self._submit(reaction=rxn)
                self.assertNotIn("degeneracy", payload["kinetics"][0])

    def test_kinetics_degeneracy_omitted_when_uncoercible(self):
        # Non-numeric strings / mappings / lists → omit rather than
        # crash. Don't crash the whole upload over a malformed
        # qualifier.
        for raw in ("not-a-number", [2.0], {"value": 2.0}):
            with self.subTest(raw=raw):
                rxn = _reaction_record()
                rxn["kinetics"]["degeneracy"] = raw
                _, _, payload = self._submit(reaction=rxn)
                self.assertNotIn("degeneracy", payload["kinetics"][0])

    def test_kinetics_degeneracy_omitted_when_zero_or_negative(self):
        # ``BundleKineticsIn.degeneracy`` is ``gt=0`` — zero is
        # physically meaningless for reaction-path degeneracy and the
        # server rejects it. Producer must omit instead of shipping.
        for raw in (0, 0.0, -1, -2.5):
            with self.subTest(raw=raw):
                rxn = _reaction_record()
                rxn["kinetics"]["degeneracy"] = raw
                _, _, payload = self._submit(reaction=rxn)
                self.assertNotIn("degeneracy", payload["kinetics"][0])

    def test_kinetics_degeneracy_omitted_when_non_finite(self):
        # ``output.yml``'s JSON schema forbids unknown ``kinetics`` keys,
        # but that schema is never enforced at runtime (the adapter
        # ``yaml.load``s the file directly and only checks
        # ``schema_version`` — see evidence.py), and ARC doesn't validate
        # against it on write either. A hand-written/older/third-party
        # output.yml can carry a non-finite degeneracy. ``> 0`` alone
        # already rejects NaN and -inf (both compare False against 0);
        # +inf is the one value that used to slip through ``> 0`` and
        # would otherwise round-trip into invalid ``Infinity`` JSON.
        for raw in (float("nan"), float("-inf"), float("inf")):
            with self.subTest(raw=raw):
                rxn = _reaction_record()
                rxn["kinetics"]["degeneracy"] = raw
                _, _, payload = self._submit(reaction=rxn)
                self.assertNotIn("degeneracy", payload["kinetics"][0])

    def test_kinetics_degeneracy_validates_against_live_schema(self):
        # End-to-end: a positive degeneracy lands on the wire and the
        # full computed-reaction payload satisfies the live validator.
        rxn = _reaction_record()
        rxn["kinetics"]["degeneracy"] = 2.0
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["degeneracy"], 2.0)
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- note from long_kinetic_description
    def test_kinetics_note_from_long_kinetic_description(self):
        # ARCReaction.long_kinetic_description lives at the reaction
        # level in ARC but maps to ``KineticsCreate.note`` in TCKDB.
        rxn = _reaction_record()
        rxn["long_kinetic_description"] = "Refit at CCSD(T)-F12/cc-pVTZ-F12"
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(
            payload["kinetics"][0]["note"],
            "Refit at CCSD(T)-F12/cc-pVTZ-F12",
        )

    def test_kinetics_note_from_kinetics_record_note(self):
        # An explicit ``note`` on the kinetics record (future-proofing)
        # is also accepted and surfaces verbatim.
        rxn = _reaction_record()
        rxn["kinetics"]["note"] = "Hand-tuned barrier"
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["note"], "Hand-tuned barrier")

    def test_kinetics_note_kinetics_record_wins_over_long_description(self):
        # If both sources are present, the explicit kinetics-level note
        # takes precedence — it's the more direct signal.
        rxn = _reaction_record()
        rxn["kinetics"]["note"] = "explicit"
        rxn["long_kinetic_description"] = "fallback"
        _, _, payload = self._submit(reaction=rxn)
        self.assertEqual(payload["kinetics"][0]["note"], "explicit")

    def test_kinetics_note_omitted_when_absent(self):
        _, _, payload = self._submit()
        self.assertNotIn("note", payload["kinetics"][0])

    def test_kinetics_note_empty_string_omits(self):
        # Empty / whitespace-only descriptions produce no note rather
        # than an empty-string row.
        rxn = _reaction_record()
        rxn["long_kinetic_description"] = "   "
        _, _, payload = self._submit(reaction=rxn)
        self.assertNotIn("note", payload["kinetics"][0])

    # ---------------- 6: dA → multiplicative a_uncertainty, dn → n_uncertainty,
    #                     dEa → d_reported_ea
    def test_kinetics_uncertainty_mapping_policy(self):
        _, _, payload = self._submit()
        kin = payload["kinetics"][0]
        # dA is preserved verbatim as a multiplicative factor (NOT
        # converted to A * (dA - 1) or any additive band).
        self.assertAlmostEqual(kin["a_uncertainty"], 1.48466)
        self.assertEqual(kin["a_uncertainty_kind"], "multiplicative")
        # dn carries cleanly to n_uncertainty.
        self.assertAlmostEqual(kin["n_uncertainty"], 0.0514735)
        # dEa with same units as Ea → d_reported_ea (same units).
        self.assertAlmostEqual(kin["d_reported_ea"], 0.294363)

    def test_kinetics_da_not_converted_to_additive(self):
        # Guardrail against the old "additive band" misinterpretation:
        # a_uncertainty must equal dA exactly, not A * (dA - 1).
        _, _, payload = self._submit()
        kin = payload["kinetics"][0]
        bogus_additive = 0.204298 * (1.48466 - 1)  # ≈ 0.099
        self.assertNotAlmostEqual(kin["a_uncertainty"], bogus_additive, places=4)
        self.assertAlmostEqual(kin["a_uncertainty"], 1.48466)

    def test_kinetics_missing_da_omits_uncertainty(self):
        rxn = _reaction_record()
        rxn["kinetics"].pop("dA")
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        # When dA is absent the schema requires both fields to be absent
        # (or both present); the producer omits.
        self.assertNotIn("a_uncertainty", kin)
        self.assertNotIn("a_uncertainty_kind", kin)

    def test_kinetics_da_below_one_omitted(self):
        # Schema rejects multiplicative factors < 1.0; producer omits
        # rather than upcasting or sending an invalid value.
        rxn = _reaction_record()
        rxn["kinetics"]["dA"] = 0.7
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        self.assertNotIn("a_uncertainty", kin)
        self.assertNotIn("a_uncertainty_kind", kin)

    def test_kinetics_dea_units_mismatch_omits_d_reported_ea(self):
        rxn = _reaction_record()
        rxn["kinetics"]["dEa_units"] = "kcal/mol"  # Ea_units is kJ/mol
        _, _, payload = self._submit(reaction=rxn)
        kin = payload["kinetics"][0]
        self.assertNotIn("d_reported_ea", kin)

    def test_kinetics_dea_both_unitless_omits_d_reported_ea(self):
        # D2 (adversarial review): Ea/Ea_units/dEa_units all None with a
        # numeric dEa used to fall into a "best-effort: both unitless;
        # pass through" branch that emitted ``d_reported_ea`` with no
        # unit anywhere in the record -- exactly what C-4 exists to
        # prevent, just 30 lines further down. Reproduce verbatim and
        # assert it no longer ships.
        rxn = _reaction_record()
        rxn["kinetics"]["Ea"] = None
        rxn["kinetics"]["Ea_units"] = None
        rxn["kinetics"]["dEa"] = 0.294363
        rxn["kinetics"]["dEa_units"] = None
        with self.assertLogs("tckdb_arc", level="WARNING") as log_ctx:
            _, _, payload = self._submit(reaction=rxn)
        self.assertTrue(any("dEa" in m for m in log_ctx.output))
        kin = payload["kinetics"][0]
        self.assertNotIn("d_reported_ea", kin)
        self.assertNotIn("reported_ea", kin)
        self.assertNotIn("reported_ea_units", kin)
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- 7: kinetics.source_calculations populated by local keys
    def test_kinetics_source_calculations_explicit(self):
        _, _, payload = self._submit()
        kin = payload["kinetics"][0]
        sources = kin["source_calculations"]
        by_role: dict[str, list[str]] = {}
        for entry in sources:
            by_role.setdefault(entry["role"], []).append(entry["calculation_key"])
        self.assertEqual(sorted(by_role["reactant_energy"]), ["r0_sp", "r1_sp"])
        self.assertEqual(sorted(by_role["product_energy"]), ["p0_sp", "p1_sp"])
        self.assertEqual(by_role["ts_energy"], ["ts_sp"])
        # In v0, kinetics 'freq' role means the TS frequency.
        self.assertEqual(by_role["freq"], ["ts_freq"])
        # No reactant/product freq calc should be linked under role=freq.
        for entry in sources:
            if entry["role"] == "freq":
                self.assertTrue(entry["calculation_key"].startswith("ts_"))

    def test_kinetics_source_calculations_omits_missing(self):
        # Drop sp on one reactant — its source link should be omitted,
        # not faked, and the kinetics block should still build.
        doc = _reaction_output_doc()
        cho = next(s for s in doc["species"] if s["label"] == "CHO")
        cho["sp_energy_hartree"] = None
        _, _, payload = self._submit(output_doc=doc)
        kin = payload["kinetics"][0]
        sources = kin["source_calculations"]
        sp_keys = {entry["calculation_key"] for entry in sources}
        self.assertNotIn("r0_sp", sp_keys)
        self.assertIn("r1_sp", sp_keys)

    def test_kinetics_irc_source_only_when_irc_calc_exists(self):
        # Default fixture has no IRC → no irc source link
        _, _, payload = self._submit()
        roles = {e["role"] for e in payload["kinetics"][0]["source_calculations"]}
        self.assertNotIn("irc", roles)
        # With IRC present → irc source link emitted
        doc = _reaction_output_doc(with_irc=True)
        _, _, payload2 = self._submit(output_doc=doc)
        roles2 = [e for e in payload2["kinetics"][0]["source_calculations"]
                  if e["role"] == "irc"]
        self.assertEqual(len(roles2), 1)
        self.assertEqual(roles2[0]["calculation_key"], "ts_irc")

    # ---------------- IRC: structured result + output geometries
    def _irc_fixture_with_logs_on_disk(self):
        """Stage forward+reverse log files on disk and return (doc, project_dir).

        The TS record's ``irc_logs`` are populated with project-relative
        paths to real (empty) log files; the parser is mocked separately
        to return fake trajectories. The on-disk files exist only so
        the adapter's ``is_file()`` gate passes — their contents are
        irrelevant once the parser is patched.
        """
        proj = tempfile.mkdtemp(prefix="arc-tckdb-irc-")
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        f_log = pathlib.Path(proj) / "TS0_irc_f.log"
        r_log = pathlib.Path(proj) / "TS0_irc_r.log"
        f_log.write_text("dummy")
        r_log.write_text("dummy")
        doc = _reaction_output_doc(with_irc=True)
        ts = doc["transition_states"][0]
        ts["irc_logs"] = ["TS0_irc_f.log", "TS0_irc_r.log"]
        return doc, proj

    @staticmethod
    def _fake_irc_points(label):
        """Build two distinct ARC xyz_dicts so forward != reverse endpoint."""
        a = float({"f": 0.0, "r": 1.0}[label[0]])
        return [
            {"symbols": ("C", "H"), "isotopes": (12, 1),
             "coords": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0 + a))},
            {"symbols": ("C", "H"), "isotopes": (12, 1),
             "coords": ((0.0, 0.0, 0.05), (1.05, 0.0, 0.05 + a))},
        ]

    def _patch_parse_irc(self, *, forward=True, reverse=True, fail=False):
        """Patch ``arc.parser.parser.parse_irc_traj`` for a test.

        Returns forward-direction points for ``_irc_f`` paths, reverse
        for ``_irc_r``, ``None`` otherwise (or always when ``fail``).
        """
        def _stub(log_file_path, raise_error=False):
            if fail:
                return None
            name = pathlib.Path(log_file_path).name
            if "_irc_f" in name and forward:
                return self._fake_irc_points("forward")
            if "_irc_r" in name and reverse:
                return self._fake_irc_points("reverse")
            return None
        return mock.patch("tckdb_arc._arc_optional.parse_irc_traj", side_effect=_stub)

    def test_irc_depends_on_irc_start_from_ts_opt(self):
        # Spec: ``ts_opt → ts_irc`` with ``role=irc_start``. NOT freq_on.
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        ts = payload["transition_state"]
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        self.assertEqual(
            irc_calc["depends_on"],
            [{"parent_calculation_key": "ts_opt", "role": "irc_start"}],
        )

    def test_irc_kinetics_source_link(self):
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        sources = payload["kinetics"][0]["source_calculations"]
        irc_links = [s for s in sources if s["role"] == "irc"]
        self.assertEqual(irc_links, [{"calculation_key": "ts_irc", "role": "irc"}])

    def test_irc_structured_result_emitted_when_parsed(self):
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        ts = payload["transition_state"]
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        self.assertIn("irc_result", irc_calc)
        result = irc_calc["irc_result"]
        self.assertEqual(result["direction"], "both")
        self.assertTrue(result["has_forward"])
        self.assertTrue(result["has_reverse"])
        # 2 forward + 2 reverse + 1 synthesized TS marker (the IRC seed
        # saddle from ts_record.xyz + opt_final_energy_hartree).
        self.assertEqual(result["point_count"], 5)
        self.assertEqual(len(result["points"]), 5)
        # The TS marker lives at the end of the dense sequence and is
        # locatable via ``ts_point_index`` on the result.
        self.assertEqual(result["ts_point_index"], 4)

    def test_irc_level_assumed_opt_level_is_reported_on_every_irc_calc(self):
        # ARC runs IRC at irc_level, which output.yml does not export; the
        # calc is labelled with opt_level and that assumption is reported.
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            outcome, _, payload = self._submit(output_doc=doc, project_directory=proj)
        irc_calc = next(c for c in payload["transition_state"]["calculations"]
                        if c["key"] == "ts_irc")
        self.assertEqual(irc_calc["level_of_theory"]["method"], doc["opt_level"]["method"])
        [warning] = [w for w in outcome.warnings if w["code"] == "irc_level_assumed_opt_level"]
        self.assertEqual(warning["field"], "transition_state.irc.level_of_theory")
        self.assertEqual(warning["context"], {"source": "tckdb_arc_self_check",
                                              "action": "level_of_theory_assumed"})
        self.assertIn("does not export irc_level", warning["message"])
        # No IRC calculation, no warning.
        outcome, _, _ = self._submit()
        self.assertNotIn("irc_level_assumed_opt_level", {w["code"] for w in outcome.warnings})

    def test_irc_points_preserve_direction_and_geometry(self):
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        ts = payload["transition_state"]
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        points = irc_calc["irc_result"]["points"]
        # Indices unique and zero-based across both branches plus the
        # appended TS marker (index 4).
        self.assertEqual([p["point_index"] for p in points], [0, 1, 2, 3, 4])
        # Trajectory points (indices 0..3) carry forward/reverse;
        # the TS marker (index 4) deliberately omits ``direction``.
        traj_directions = [p.get("direction") for p in points[:4]]
        self.assertEqual(traj_directions.count("forward"), 2)
        self.assertEqual(traj_directions.count("reverse"), 2)
        self.assertNotIn("direction", points[4])
        for p in points:
            self.assertIn("geometry", p)
            self.assertIn("xyz_text", p["geometry"])
        # Producer must NOT label points as reactant/product.
        for p in points:
            self.assertNotIn("role", p)

    def test_irc_result_refused_when_any_log_leaves_its_direction_unstated(self):
        # IRCResultPayload.direction is required. Logs whose direction ARC
        # did not record (no irc_log_directions entry, no forward/reverse in
        # the filename, geometry-only points) do not say which branch they
        # hold, so the result is omitted with a warning instead of claiming
        # ``both`` (or filing them under the one stated branch); the irc
        # calculation itself is kept. ARC runs predating f6af510b pad
        # irc_log_directions with None.
        for directions in (None, ["reverse", None], [None, None]):
            with self.subTest(irc_log_directions=directions):
                proj = tempfile.mkdtemp(prefix="arc-tckdb-irc-")
                self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
                for name in ("TS0_irc_a.log", "TS0_irc_b.log"):
                    (pathlib.Path(proj) / name).write_text("dummy")
                doc = _reaction_output_doc(with_irc=True)
                ts = doc["transition_states"][0]
                ts["irc_logs"] = ["TS0_irc_a.log", "TS0_irc_b.log"]
                if directions is None:
                    ts.pop("irc_log_directions", None)
                else:
                    ts["irc_log_directions"] = directions
                points = self._fake_irc_points("forward")
                with mock.patch("tckdb_arc._arc_optional.parse_irc_traj", return_value=points):
                    outcome, _, payload = self._submit(output_doc=doc, project_directory=proj)
                irc_calc = next(c for c in payload["transition_state"]["calculations"]
                                if c["key"] == "ts_irc")
                self.assertNotIn("irc_result", irc_calc)
                [warning] = [w for w in outcome.warnings
                             if w["code"] == "irc_direction_not_stated"]
                self.assertEqual(warning["field"], "transition_state.irc_result")
                self.assertEqual(warning["context"], {"source": "tckdb_arc_self_check",
                                                      "action": "irc_result_omitted"})
                self.assertEqual(
                    json.loads(outcome.sidecar_path.read_text())["warnings"], outcome.warnings)

    def test_irc_no_explicit_output_geometries_when_irc_result_present(self):
        # Regression: the producer must NOT emit explicit output_geometries
        # for the IRC calc when irc_result is attached. Server-side
        # ``_persist_irc_result`` already creates calculation_output_geometry
        # rows (role=irc_forward / irc_reverse) for every directional point;
        # producer-explicit endpoints would double-claim those geometries
        # and trip the unique (calculation_id, geometry_id) constraint in
        # attach_calculation_output_geometries. The bug manifested as
        # `output_geometries declares the same geometry more than once`
        # 422s on every replay.
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        ts = payload["transition_state"]
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        # irc_result IS present (server will derive output_geometries from points)…
        self.assertIn("irc_result", irc_calc)
        # …and explicit output_geometries are suppressed (or empty).
        self.assertEqual(irc_calc.get("output_geometries", []), [])

    def test_irc_partial_fallback_when_parsing_fails(self):
        # Spec: log present but no parsable points →
        #   - keep type=irc calc
        #   - keep depends_on(role=irc_start)
        #   - keep kinetics.source_calculations(role=irc)
        #   - omit irc_result (don't fabricate incomplete data)
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc(fail=True):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        ts = payload["transition_state"]
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        self.assertEqual(irc_calc["type"], "irc")
        self.assertEqual(
            irc_calc["depends_on"],
            [{"parent_calculation_key": "ts_opt", "role": "irc_start"}],
        )
        self.assertNotIn("irc_result", irc_calc)
        # kinetics source link survives.
        sources = payload["kinetics"][0]["source_calculations"]
        irc_links = [s for s in sources if s["role"] == "irc"]
        self.assertEqual(len(irc_links), 1)

    def test_no_irc_emits_nothing_irc_related(self):
        # Default fixture has no irc_logs → no ts_irc calc, no irc_start
        # dependency, no kinetics source link.
        _, _, payload = self._submit()
        ts = payload["transition_state"]
        ts_calc_keys = {c["key"] for c in ts["calculations"]}
        self.assertNotIn("ts_irc", ts_calc_keys)
        for c in ts["calculations"]:
            for dep in c.get("depends_on") or []:
                self.assertNotEqual(dep.get("role"), "irc_start")
        roles = {e["role"] for e in payload["kinetics"][0]["source_calculations"]}
        self.assertNotIn("irc", roles)

    # ---------------- IRC: per-point direction labelling
    def _irc_fixture_production_shape(self, *, directions=("forward", "reverse")):
        """Stage logs whose filenames don't carry direction (production case).

        Real ARC IRC logs land at ``calcs/.../irc_<server_id>/output.log``;
        the filename has no forward/reverse infix. Direction lives only on
        ``irc_log_directions``, which the scheduler captures from
        ``job.irc_direction``. This fixture mirrors that shape.
        """
        proj = tempfile.mkdtemp(prefix="arc-tckdb-irc-prod-")
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        log_paths = []
        for i, _ in enumerate(directions):
            d = pathlib.Path(proj) / f"irc_{i}"
            d.mkdir()
            log = d / "output.log"
            log.write_text("dummy")
            log_paths.append(f"irc_{i}/output.log")
        doc = _reaction_output_doc(with_irc=True)
        ts = doc["transition_states"][0]
        ts["irc_logs"] = log_paths
        ts["irc_log_directions"] = list(directions)
        return doc, proj

    def _patch_parse_irc_by_index(self, points_per_log):
        """Like ``_patch_parse_irc`` but keyed off log path, not filename token.

        ``points_per_log[i]`` is the parsed-point list returned for the
        ``i``-th unique log path the adapter feeds in. Lets tests stage
        production-shape (direction-less) filenames without coupling the
        stub to filename pattern matching.
        """
        seen: dict[str, int] = {}

        def _stub(log_file_path, raise_error=False):
            if log_file_path not in seen:
                seen[log_file_path] = len(seen)
            return points_per_log[seen[log_file_path]]
        return mock.patch("tckdb_arc._arc_optional.parse_irc_traj", side_effect=_stub)

    def test_explicit_irc_log_directions_label_points_when_filename_lacks_direction(self):
        # Reproduces the live-run bug: filename detection alone yielded
        # all-NULL directions. With irc_log_directions populated by the
        # scheduler the trajectory points carry forward/reverse. The
        # synthesized TS marker (appended last) deliberately omits
        # direction — that null is expected, not a regression.
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_irc_points("forward")
        rev = self._fake_irc_points("reverse")
        with self._patch_parse_irc_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        traj_directions = [p.get("direction") for p in result["points"]
                           if not p.get("is_ts")]
        self.assertEqual(traj_directions.count("forward"), len(fwd))
        self.assertEqual(traj_directions.count("reverse"), len(rev))
        self.assertNotIn(None, traj_directions)

    def test_irc_result_flags_for_forward_only(self):
        doc, proj = self._irc_fixture_production_shape(directions=("forward",))
        with self._patch_parse_irc_by_index([self._fake_irc_points("forward")]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertEqual(result["direction"], "forward")
        self.assertTrue(result["has_forward"])
        self.assertFalse(result["has_reverse"])
        # Trajectory points carry ``forward``; the appended TS marker
        # deliberately omits ``direction`` (per IRCPointPayload's
        # nullable direction for TS).
        traj_dirs = {p.get("direction") for p in result["points"]
                     if not p.get("is_ts")}
        self.assertEqual(traj_dirs, {"forward"})
        # TS marker stays directionless.
        ts_pt = next(p for p in result["points"] if p.get("is_ts"))
        self.assertNotIn("direction", ts_pt)

    def test_irc_result_flags_for_reverse_only(self):
        doc, proj = self._irc_fixture_production_shape(directions=("reverse",))
        with self._patch_parse_irc_by_index([self._fake_irc_points("reverse")]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertEqual(result["direction"], "reverse")
        self.assertFalse(result["has_forward"])
        self.assertTrue(result["has_reverse"])
        traj_dirs = {p.get("direction") for p in result["points"]
                     if not p.get("is_ts")}
        self.assertEqual(traj_dirs, {"reverse"})
        ts_pt = next(p for p in result["points"] if p.get("is_ts"))
        self.assertNotIn("direction", ts_pt)

    def test_irc_trajectory_points_never_self_mark_as_ts(self):
        # ARC's parse_irc_traj surfaces only geometries on each
        # trajectory point — no per-point energies/gradients to
        # identify the saddle from within the trajectory. So no
        # trajectory point may carry ``is_ts=True``. (The TS marker
        # itself is a SEPARATE, synthesized point built from the IRC
        # seed-saddle — see test_irc_ts_marker_appended_when_seed_known.)
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_irc_points("forward")
        rev = self._fake_irc_points("reverse")
        with self._patch_parse_irc_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        # Trajectory points (everything except the synthesized TS
        # marker) carry no is_ts flag.
        traj_points = [p for p in result["points"]
                       if p.get("direction") in ("forward", "reverse")]
        for p in traj_points:
            self.assertFalse(p.get("is_ts", False))
        # And every is_ts point comes from the synthesized marker, not
        # the trajectory parser (i.e. it never has a direction label).
        is_ts_points = [p for p in result["points"] if p.get("is_ts")]
        for p in is_ts_points:
            self.assertNotIn("direction", p)

    def test_irc_ts_marker_appended_when_seed_known(self):
        # ARC's IRC is seeded from a fully optimized TS saddle, so
        # ts_record.xyz + opt_final_energy_hartree are always known when
        # an IRC ran. The adapter must surface that as a synthesized TS
        # marker point, distinct from the trajectory's per-step data.
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_irc_points("forward")
        rev = self._fake_irc_points("reverse")
        with self._patch_parse_irc_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        # Exactly one is_ts point in the result.
        ts_pts = [p for p in result["points"] if p.get("is_ts")]
        self.assertEqual(len(ts_pts), 1)
        ts_pt = ts_pts[0]
        # ts_point_index on the result links to the marker's index.
        self.assertEqual(result["ts_point_index"], ts_pt["point_index"])
        # The marker is appended at the END of the dense sequence, after
        # all trajectory points. Order matters for replay-stable
        # indexing: ARC's ESS-emitted trajectory order is preserved and
        # the marker is the highest index.
        self.assertEqual(ts_pt["point_index"], len(result["points"]) - 1)
        # Marker semantics: TS sits at reaction_coordinate=0 by
        # definition, carries a geometry, no direction.
        self.assertEqual(ts_pt["reaction_coordinate"], 0.0)
        self.assertIn("geometry", ts_pt)
        self.assertIn("xyz_text", ts_pt["geometry"])
        self.assertNotIn("direction", ts_pt)

    def test_irc_ts_marker_energy_consistent_with_zero_reference(self):
        # When the IRC has a zero_energy_reference_hartree, the TS
        # marker's relative_energy_kj_mol equals exactly 0 (the TS *is*
        # the zero reference). The trajectory points' relative energies
        # are computed against the same reference, so the marker and
        # trajectory share a consistent zero.
        doc, proj = self._irc_fixture_production_shape(directions=("forward",))
        doc["sp_level"] = dict(doc["opt_level"])
        ts = doc["transition_states"][0]
        ts["sp_energy_hartree"] = -303.6
        ts["opt_final_energy_hartree"] = -303.5
        fwd = self._fake_rich_irc_points("forward", base_energy=-303.5779)
        with self._patch_parse_irc_path_by_index([fwd]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        ts_pt = next(p for p in result["points"] if p.get("is_ts"))
        # Marker's absolute energy equals the zero reference (sp_energy
        # at matching level).
        self.assertAlmostEqual(ts_pt["electronic_energy_hartree"], -303.6)
        # Hence relative energy is exactly 0 kJ/mol.
        self.assertAlmostEqual(ts_pt["relative_energy_kj_mol"], 0.0, places=10)

    def test_irc_ts_marker_omits_energy_when_no_zero_reference(self):
        # No opt_final_energy and SP level mismatched → zero_ref is
        # null. The TS marker is STILL emitted (geometry alone is
        # useful), but without any energy fields. ts_point_index is
        # still set so consumers can locate the TS in the dense list.
        doc, proj = self._irc_fixture_production_shape(directions=("forward",))
        doc["sp_level"] = {"method": "ccsd(t)-f12a",
                           "basis": "cc-pvtz-f12", "software": "molpro"}
        ts = doc["transition_states"][0]
        ts["sp_energy_hartree"] = -303.99   # higher level: ignored
        ts["opt_final_energy_hartree"] = None
        fwd = self._fake_rich_irc_points("forward", base_energy=-303.5)
        with self._patch_parse_irc_path_by_index([fwd]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertNotIn("zero_energy_reference_hartree", result)
        ts_pt = next(p for p in result["points"] if p.get("is_ts"))
        # Energy fields omitted — no fabrication.
        self.assertNotIn("electronic_energy_hartree", ts_pt)
        self.assertNotIn("relative_energy_kj_mol", ts_pt)
        # Geometry + locator still present.
        self.assertIn("geometry", ts_pt)
        self.assertIn("ts_point_index", result)

    def test_irc_ts_marker_indices_unique_across_full_list(self):
        # Defensive: the schema validator requires unique point_index
        # across all points. The TS marker must use len(trajectory) as
        # its index, never collide with a trajectory point.
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_irc_points("forward")
        rev = self._fake_irc_points("reverse")
        with self._patch_parse_irc_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        indices = [p["point_index"] for p in result["points"]]
        self.assertEqual(len(set(indices)), len(indices))
        self.assertEqual(indices, list(range(len(indices))))

    def test_irc_no_reactant_product_inference_in_payload(self):
        # Spec is explicit: forward/reverse are IRC path-direction
        # labels, NOT reactant/product designators. The payload must
        # carry no fields that would imply that mapping.
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_irc_points("forward")
        rev = self._fake_irc_points("reverse")
        with self._patch_parse_irc_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        ts = payload["transition_state"]
        irc_calc = next(c for c in ts["calculations"] if c["key"] == "ts_irc")
        text = json.dumps(irc_calc)
        for forbidden in ("reactant_side", "product_side", "reactants_branch", "products_branch"):
            self.assertNotIn(forbidden, text)

    def test_irc_payload_validates_against_tckdb_schema(self):
        # Live-schema smoke: skipped when the TCKDB pydantic schema is
        # not importable in the active env.
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_irc_points("forward")
        rev = self._fake_irc_points("reverse")
        with self._patch_parse_irc_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- IRC double-emit regression
    def test_irc_calc_attaches_irc_result_only_no_explicit_output_geometries(self):
        # Regression: producer must NOT emit explicit ``output_geometries``
        # for the IRC calc when ``irc_result.points`` is attached.
        # Server-side ``_persist_irc_result`` (calculation_resolution.py)
        # writes ``calculation_output_geometry`` rows for every
        # forward/reverse point with role irc_forward / irc_reverse;
        # producer-explicit endpoints would then double-claim those
        # geometries and trip the unique (calculation_id, geometry_id)
        # constraint in ``attach_calculation_output_geometries``,
        # producing the user-visible 422
        #   "output_geometries declares the same geometry more than once".
        # The trajectory points still carry both directions — the server
        # owns the output-geometry derivation.
        doc, proj = self._irc_fixture_production_shape()
        with self._patch_parse_irc_by_index([
            self._fake_irc_points("forward"),
            self._fake_irc_points("reverse"),
        ]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        irc_calc = next(
            c for c in payload["transition_state"]["calculations"] if c["key"] == "ts_irc"
        )
        # irc_result IS attached…
        self.assertIn("irc_result", irc_calc)
        result = irc_calc["irc_result"]
        directions = [p.get("direction") for p in result["points"]]
        self.assertIn("forward", directions)
        self.assertIn("reverse", directions)
        self.assertTrue(result["has_forward"])
        self.assertTrue(result["has_reverse"])
        # …but explicit output_geometries is suppressed (or empty).
        self.assertEqual(irc_calc.get("output_geometries", []), [])

    # ---------------- IRC: rich parser path (energies, RC, gradients)
    @staticmethod
    def _fake_rich_irc_points(direction, *, base_energy):
        """Two synthetic rich-parser points with energies/RC/grads.

        Mirrors what :func:`arc.parser.parser.parse_irc_path` returns for
        Gaussian: per-point ``electronic_energy_hartree``,
        ``reaction_coordinate``, ``max_gradient``, ``rms_gradient``,
        ``direction``, and ``xyz``.
        """
        return [
            {
                "point_number": 1,
                "direction": direction,
                "electronic_energy_hartree": base_energy + 0.001,
                "reaction_coordinate": 0.07236,
                "max_gradient": 0.0073,
                "rms_gradient": 0.0025,
                "xyz": {
                    "symbols": ("C", "H"), "isotopes": (12, 1),
                    "coords": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
                },
            },
            {
                "point_number": 2,
                "direction": direction,
                "electronic_energy_hartree": base_energy + 0.002,
                "reaction_coordinate": 0.14470,
                "max_gradient": 0.0140,
                "rms_gradient": 0.0048,
                "xyz": {
                    "symbols": ("C", "H"), "isotopes": (12, 1),
                    "coords": ((0.0, 0.0, 0.05), (1.05, 0.0, 0.05)),
                },
            },
        ]

    def _patch_parse_irc_path_by_index(self, points_per_log):
        """Patch ``parse_irc_path`` per-log; ``parse_irc_traj`` is left as-is.

        Pairs with ``_patch_parse_irc_by_index`` to drive the rich-vs-
        geometry-only branches of ``_parse_irc_trajectories``.
        """
        seen: dict[str, int] = {}

        def _stub(log_file_path, raise_error=False):
            if log_file_path not in seen:
                seen[log_file_path] = len(seen)
            return points_per_log[seen[log_file_path]]
        return mock.patch("tckdb_arc._arc_optional.parse_irc_path", side_effect=_stub)

    def test_irc_rich_points_carry_energy_rc_and_gradients(self):
        # Rich parser returns per-point energies/RC/grads → the payload
        # surfaces them on every IRC trajectory point. The synthesized
        # TS marker (appended last) carries energy + reaction_coordinate
        # (=0) but no gradients (it's a static seed, not an SCF step).
        doc, proj = self._irc_fixture_production_shape()
        fwd = self._fake_rich_irc_points("forward", base_energy=-303.5779)
        rev = self._fake_rich_irc_points("reverse", base_energy=-303.5779)
        with self._patch_parse_irc_path_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        traj_points = [p for p in result["points"] if not p.get("is_ts")]
        for p in traj_points:
            self.assertIn("electronic_energy_hartree", p)
            self.assertIn("reaction_coordinate", p)
            self.assertIn("max_gradient", p)
            self.assertIn("rms_gradient", p)
            self.assertIn("geometry", p)
        # Forward + reverse direction labels propagate from the rich data.
        traj_directions = [p["direction"] for p in traj_points]
        self.assertEqual(traj_directions.count("forward"), 2)
        self.assertEqual(traj_directions.count("reverse"), 2)
        # TS marker: energy + RC=0, no gradients.
        ts_pt = next(p for p in result["points"] if p.get("is_ts"))
        self.assertIn("electronic_energy_hartree", ts_pt)
        self.assertEqual(ts_pt["reaction_coordinate"], 0.0)
        self.assertNotIn("max_gradient", ts_pt)
        self.assertNotIn("rms_gradient", ts_pt)

    def test_irc_zero_energy_reference_uses_ts_sp_when_levels_match(self):
        # opt_level == sp_level → ts_sp.electronic_energy is the reference.
        # relative_energy_kj_mol is computed against it.
        doc, proj = self._irc_fixture_production_shape()
        # Force a matching SP level in the fixture (opt_level is set in
        # _fake_output_doc; mirror it here so _level_keys_match returns True).
        doc["sp_level"] = dict(doc["opt_level"])
        ts = doc["transition_states"][0]
        ts["sp_energy_hartree"] = -303.6
        ts["opt_final_energy_hartree"] = -303.5
        fwd = self._fake_rich_irc_points("forward", base_energy=-303.5779)
        rev = self._fake_rich_irc_points("reverse", base_energy=-303.5779)
        with self._patch_parse_irc_path_by_index([fwd, rev]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertAlmostEqual(result["zero_energy_reference_hartree"], -303.6)
        # relative_energy_kj_mol = (E - E_ref) * E_h_kJmol. Uses the same
        # vendored constant as path_search_result (PHASE_C_PLAN.md C-3) —
        # not the old inline ``2625.4996`` literal — so this doubles as
        # the regression test for the two fields agreeing bit-for-bit.
        from tckdb_arc._vendor import E_h_kJmol
        for p in result["points"]:
            expected = (
                p["electronic_energy_hartree"] - (-303.6)
            ) * E_h_kJmol
            self.assertAlmostEqual(p["relative_energy_kj_mol"], expected, places=9)

    def test_irc_zero_energy_reference_falls_back_to_opt_when_sp_level_differs(self):
        # sp_level distinct from opt_level → adapter must NOT use the
        # higher-level SP energy as the IRC zero reference; falls back to
        # the TS opt's final energy (always at opt level by construction).
        doc, proj = self._irc_fixture_production_shape(directions=("forward",))
        doc["sp_level"] = {"method": "ccsd(t)-f12a", "basis": "cc-pvtz-f12",
                           "software": "molpro"}
        ts = doc["transition_states"][0]
        ts["sp_energy_hartree"] = -303.99   # high-level, MUST NOT be picked
        ts["opt_final_energy_hartree"] = -303.50  # opt-level, picked
        fwd = self._fake_rich_irc_points("forward", base_energy=-303.5)
        with self._patch_parse_irc_path_by_index([fwd]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertAlmostEqual(result["zero_energy_reference_hartree"], -303.50)

    def test_irc_zero_energy_reference_omitted_when_no_match_and_no_opt_energy(self):
        # No opt_final_energy_hartree on the TS, sp_level differs → null
        # reference. Per-point relative_energy_kj_mol is also omitted (the
        # spec forbids fabrication).
        doc, proj = self._irc_fixture_production_shape(directions=("forward",))
        doc["sp_level"] = {"method": "ccsd(t)-f12a", "basis": "cc-pvtz-f12",
                           "software": "molpro"}
        ts = doc["transition_states"][0]
        ts["sp_energy_hartree"] = -303.99   # high-level: ignored
        ts["opt_final_energy_hartree"] = None
        fwd = self._fake_rich_irc_points("forward", base_energy=-303.5)
        with self._patch_parse_irc_path_by_index([fwd]):
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertNotIn("zero_energy_reference_hartree", result)
        for p in result["points"]:
            self.assertNotIn("relative_energy_kj_mol", p)

    def test_irc_falls_back_to_geometry_only_when_rich_parser_fails(self):
        # parse_irc_path returns None → the adapter falls back to
        # parse_irc_traj. Trajectory points still carry geometry +
        # directions but no per-point energies/RC/grads. The synthesized
        # TS marker is appended regardless (4 trajectory + 1 marker = 5),
        # and it carries energy from the TS opt fallback even though the
        # trajectory itself is energy-less.
        doc, proj = self._irc_fixture_production_shape()
        with mock.patch("tckdb_arc._arc_optional.parse_irc_path",
                        side_effect=lambda log_file_path, raise_error=False: None):
            with self._patch_parse_irc_by_index([
                self._fake_irc_points("forward"),
                self._fake_irc_points("reverse"),
            ]):
                _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        self.assertEqual(result["point_count"], 5)
        traj_points = [p for p in result["points"] if not p.get("is_ts")]
        self.assertEqual(len(traj_points), 4)
        for p in traj_points:
            self.assertIn("geometry", p)
            self.assertNotIn("electronic_energy_hartree", p)
            self.assertNotIn("reaction_coordinate", p)
            self.assertNotIn("max_gradient", p)

    def test_irc_path_rich_parser_consumed_end_to_end_on_real_fixture(self):
        pytest.importorskip("arc")  # arc-gated: real ARC parse_irc_path, no stub
        # Integration: stage the real Gaussian IRC fixtures as the TS's
        # irc_logs and confirm the rich parser flows through the adapter
        # without any ESS-stub patching. This is the strongest signal that
        # the parse_irc_path → adapter → payload chain works on production
        # log shapes.
        proj = tempfile.mkdtemp(prefix="arc-tckdb-irc-real-")
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        # Copy fixtures into the project tree so adapter path resolution
        # produces an absolute path the parsers can read.
        src_dir = pathlib.Path(__file__).resolve().parents[1] / "testing" / "irc"
        f_log = pathlib.Path(proj) / "TS0_irc_f.log"
        r_log = pathlib.Path(proj) / "TS0_irc_r.log"
        shutil.copy(src_dir / "rxn_1_irc_1.out", f_log)
        shutil.copy(src_dir / "rxn_1_irc_2.out", r_log)
        doc = _reaction_output_doc(with_irc=True)
        ts = doc["transition_states"][0]
        ts["irc_logs"] = ["TS0_irc_f.log", "TS0_irc_r.log"]
        ts["irc_log_directions"] = ["forward", "reverse"]
        # Match the IRC fixtures' Gaussian level so the SP-level path
        # gets exercised. The actual Hartree values are fixture-dependent
        # so we only assert the shape/wiring.
        doc["sp_level"] = dict(doc["opt_level"])
        ts["sp_energy_hartree"] = -303.6
        _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        result = next(
            c for c in payload["transition_state"]["calculations"]
            if c["key"] == "ts_irc"
        )["irc_result"]
        # Each fixture has 50 stepped points; both branches plus the
        # synthesized TS marker → 100 trajectory + 1 marker = 101.
        self.assertEqual(result["point_count"], 101)
        self.assertTrue(result["has_forward"])
        self.assertTrue(result["has_reverse"])
        self.assertIn("zero_energy_reference_hartree", result)
        self.assertEqual(result["ts_point_index"], 100)
        # Spot-check one forward + one reverse point have the rich fields.
        fwd_pt = next(p for p in result["points"] if p.get("direction") == "forward")
        rev_pt = next(p for p in result["points"] if p.get("direction") == "reverse")
        for p in (fwd_pt, rev_pt):
            self.assertIn("electronic_energy_hartree", p)
            self.assertIn("reaction_coordinate", p)
            self.assertIn("max_gradient", p)
            self.assertIn("rms_gradient", p)
            self.assertIn("relative_energy_kj_mol", p)
            self.assertIn("geometry", p)
        # TS marker: zero relative energy (matches the zero_ref it was
        # built from), reaction_coordinate=0, is_ts=True, no direction.
        ts_pt = next(p for p in result["points"] if p.get("is_ts"))
        self.assertAlmostEqual(ts_pt["relative_energy_kj_mol"], 0.0)
        self.assertEqual(ts_pt["reaction_coordinate"], 0.0)
        self.assertNotIn("direction", ts_pt)

    def test_output_yml_does_not_contain_irc_path(self):
        pytest.importorskip("arc")  # arc-gated: needs ARC on PYTHONPATH
        # Spec invariant: output.yml must NOT carry the full IRC path.
        # arc/output.py's _spc_to_dict serializes only irc_logs /
        # irc_log_directions / irc_converged for IRC — never the
        # per-point energy/RC/grad arrays. We check the actual set of
        # emitted dict keys (``d['<field>'] = ...``) against an allowlist
        # of IRC-related fields and a forbidden list of rich IRC fields.
        # Guards the boundary so future helpers that touch the ts_record
        # post-parse can't leak rich IRC data into the human-readable
        # summary.
        from arc.output import _spc_to_dict
        import inspect, re
        src = inspect.getsource(_spc_to_dict)
        emitted_keys = set(re.findall(r"d\[['\"]([^'\"]+)['\"]\]\s*=", src))
        irc_emitted = {k for k in emitted_keys if "irc" in k}
        # Allowlist mirrors the existing _spc_to_dict implementation —
        # tighten this set if new IRC fields are intentionally added.
        self.assertEqual(
            irc_emitted,
            {"irc_logs", "irc_log_directions", "irc_converged"},
            f"unexpected IRC fields emitted to output.yml: {irc_emitted}",
        )
        # Belt-and-suspenders: rich-parser per-point field names must
        # never appear as emitted keys.
        forbidden_fields = {
            "irc_path", "irc_points", "irc_result",
            "irc_path_points", "irc_relative_energies",
        }
        self.assertFalse(
            emitted_keys & forbidden_fields,
            f"rich IRC fields leaked into output.yml: "
            f"{emitted_keys & forbidden_fields}",
        )

    # ---------------- 8: species and TS calc provenance preserved
    def test_calc_provenance_input_geometries_threaded(self):
        _, _, payload = self._submit()
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        for calc in r0["calculations"]:
            # freq + sp explicitly carry the conformer's optimized xyz
            self.assertIn("input_geometries", calc)
            self.assertEqual(len(calc["input_geometries"]), 1)

    def test_calc_provenance_depends_on_edges(self):
        _, _, payload = self._submit()
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        freq_calc = next(c for c in r0["calculations"] if c["key"] == "r0_freq")
        sp_calc = next(c for c in r0["calculations"] if c["key"] == "r0_sp")
        self.assertEqual(
            freq_calc["depends_on"],
            [{"parent_calculation_key": "r0_opt", "role": "freq_on"}],
        )
        self.assertEqual(
            sp_calc["depends_on"],
            [{"parent_calculation_key": "r0_opt", "role": "single_point_on"}],
        )

    def test_ts_calc_provenance_depends_on_edges(self):
        _, _, payload = self._submit()
        ts = payload["transition_state"]
        ts_freq = next(c for c in ts["calculations"] if c["key"] == "ts_freq")
        ts_sp = next(c for c in ts["calculations"] if c["key"] == "ts_sp")
        self.assertEqual(
            ts_freq["depends_on"],
            [{"parent_calculation_key": "ts_opt", "role": "freq_on"}],
        )
        self.assertEqual(
            ts_sp["depends_on"],
            [{"parent_calculation_key": "ts_opt", "role": "single_point_on"}],
        )

    # ---------------- 9: artifact inclusion when enabled
    def test_artifacts_inlined_under_correct_calculations(self):
        # Set up artifact upload + actual files on disk
        proj = tempfile.mkdtemp(prefix="arc-tckdb-rxn-art-")
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        opt_log = pathlib.Path(proj) / "r0_opt.log"
        opt_log.write_text("dummy log content")
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
            artifacts=TCKDBArtifactConfig(upload=True, kinds=("output_log",)),
        )
        doc = _reaction_output_doc()
        cho = next(s for s in doc["species"] if s["label"] == "CHO")
        cho["opt_log"] = "r0_opt.log"
        _, _, payload = self._submit(output_doc=doc, cfg=cfg, project_directory=proj)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        primary = r0["conformers"][0]["calculation"]
        self.assertIn("artifacts", primary)
        self.assertEqual(primary["artifacts"][0]["kind"], "output_log")
        self.assertEqual(primary["artifacts"][0]["filename"], "r0_opt.log")

    # ---------------- 10: deterministic idempotency
    def test_payload_and_idempotency_are_deterministic(self):
        outcome1, _, payload1 = self._submit()
        outcome2, _, payload2 = self._submit()
        self.assertEqual(payload1, payload2)
        self.assertEqual(outcome1.idempotency_key, outcome2.idempotency_key)

    def test_idempotency_changes_with_kinetics_change(self):
        outcome1, _, _ = self._submit()
        rxn = _reaction_record()
        rxn["kinetics"]["A"] = 0.5
        outcome2, _, _ = self._submit(reaction=rxn)
        self.assertNotEqual(outcome1.idempotency_key, outcome2.idempotency_key)

    # ---------------- 11: sidecar shape (payload_kind + endpoint)
    def test_sidecar_payload_kind_and_endpoint(self):
        outcome, _, _ = self._submit()
        sidecar = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sidecar["payload_kind"], COMPUTED_REACTION_KIND)
        self.assertEqual(sidecar["endpoint"], COMPUTED_REACTION_ENDPOINT)
        # status updates from "pending" → "uploaded" via the stub client
        self.assertEqual(sidecar["status"], "uploaded")
        self.assertIn("payload_file", sidecar)

    def test_upload_response_public_refs_written_to_sidecar(self):
        client = _StubClient(response=_StubResponse({
            "reaction_ref": "rxe_1",
            "reaction_entry_ref": "rxee_1",
            "transition_state_ref": "tse_1",
            "transition_state_entry_ref": "tsee_1",
            "calculations": [
                {"calculation_ref": "calc_ts_opt"},
                {"calculation_ref": "calc_ts_freq"},
            ],
            "kinetics": {"kinetics_ref": "kinetics_1"},
            "request": {"reaction_ref": "rxe_request_echo"},
        }, headers={"X-Request-ID": "req-reaction-upload"}))
        outcome, _, _ = self._submit(client=client)
        sidecar = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sidecar["public_refs"]["reaction_refs"], ["rxe_1"])
        self.assertEqual(sidecar["public_refs"]["reaction_entry_refs"], ["rxee_1"])
        self.assertEqual(sidecar["public_refs"]["transition_state_refs"], ["tse_1"])
        self.assertEqual(
            sidecar["public_refs"]["transition_state_entry_refs"], ["tsee_1"]
        )
        self.assertEqual(
            sidecar["public_refs"]["calculation_refs"],
            ["calc_ts_opt", "calc_ts_freq"],
        )
        self.assertEqual(sidecar["public_refs"]["kinetics_refs"], ["kinetics_1"])
        self.assertEqual(sidecar["request_ids"][-1]["request_id"], "req-reaction-upload")

    def test_offline_skipped_status_when_upload_false(self):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            upload=False,
            project_label="proj-A",
            upload_mode="computed_reaction",
        )
        outcome, client, _ = self._submit(cfg=cfg)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(client.calls, [])
        sidecar = json.loads(outcome.sidecar_path.read_text())
        self.assertEqual(sidecar["payload_kind"], COMPUTED_REACTION_KIND)
        self.assertEqual(sidecar["status"], "skipped")

    # ---------------- 12: no DB IDs in payload
    def test_payload_contains_no_db_ids(self):
        _, _, payload = self._submit()
        text = json.dumps(payload)
        for token in ("species_entry_id", "calculation_id", "thermo_id",
                      "conformer_observation_id", "reaction_id"):
            self.assertNotIn(token, text, f"unexpected DB-id field {token!r} in payload")

    # ---------------- 13: missing reactant raises with clear error
    def test_missing_reactant_label_raises(self):
        rxn = _reaction_record()
        rxn["reactant_labels"] = ["CHO", "DOES_NOT_EXIST"]
        adapter = self._adapter(_StubClient())
        with self.assertRaises(ValueError) as ctx:
            adapter._build_computed_reaction_payload(
                output_doc=_reaction_output_doc(), reaction_record=rxn,
            )
        self.assertIn("DOES_NOT_EXIST", str(ctx.exception))

    # ---------------- 14: kinetics with no Arrhenius fields → no kinetics list
    def test_no_kinetics_omits_kinetics_field(self):
        rxn = _reaction_record(with_kinetics=False)
        _, _, payload = self._submit(reaction=rxn)
        self.assertNotIn("kinetics", payload)

    # ---------------- 15: server-side schema validation (smoke; skipped when unavailable)
    def test_payload_validates_against_tckdb_schema(self):
        # The adapter targets the live TCKDB ``ComputedReactionUploadRequest``
        # schema. When the backend pydantic schema is importable in the
        # current env (it usually isn't from ARC's env), validate the
        # payload through it. Otherwise skip — this is a smoke test, not
        # a hard requirement. Uses the structured-IRC fixture so the new
        # ``irc_result`` / ``output_geometries(role=irc_*)`` /
        # ``depends_on(role=irc_start)`` shapes are all exercised by
        # ``ComputedReactionUploadRequest.model_validate`` when run.
        doc, proj = self._irc_fixture_with_logs_on_disk()
        with self._patch_parse_irc():
            _, _, payload = self._submit(output_doc=doc, project_directory=proj)
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- AEC/BAC routing (species + TS)
    def _doc_with_corrections(self, *, attach_to_ts=True):
        """Reaction output.yml fixture with applied_energy_corrections on
        every reactant/product and (optionally) on the TS. Lets each test
        focus on the routing assertion without re-assembling the doc."""
        doc = _reaction_output_doc()
        for sp in doc["species"]:
            sp["applied_energy_corrections"] = [_aec_record(), _pbac_record()]
        if attach_to_ts:
            ts = doc["transition_states"][0]
            ts["applied_energy_corrections"] = [_aec_record(), _mbac_record()]
        return doc

    def _species_block(self, payload, key):
        return next(s for s in payload["species"] if s["key"] == key)

    def test_reactant_species_aec_bac_in_species_block(self):
        # Both reactants carry AEC + BAC; the producer must put them on
        # the BundleSpeciesIn (not at top-level, not on the TS).
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        for key in ("r0_CHO", "r1_CH4"):
            sp = self._species_block(payload, key)
            self.assertIn("applied_energy_corrections", sp)
            roles = [e["application_role"] for e in sp["applied_energy_corrections"]]
            self.assertEqual(roles, ["aec_total", "bac_total"])

    def test_product_species_aec_bac_in_species_block(self):
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        for key in ("p0_CH2O", "p1_CH3"):
            sp = self._species_block(payload, key)
            self.assertIn("applied_energy_corrections", sp)
            roles = [e["application_role"] for e in sp["applied_energy_corrections"]]
            self.assertEqual(roles, ["aec_total", "bac_total"])

    def test_ts_aec_bac_in_transition_state_block(self):
        # TS-side corrections must land on the TS block (server routes them
        # to target_transition_state_entry_id), NOT on a species block and
        # NOT on a top-level field.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        ts = payload["transition_state"]
        self.assertIn("applied_energy_corrections", ts)
        self.assertEqual(
            [e["application_role"] for e in ts["applied_energy_corrections"]],
            ["aec_total", "bac_total"],
        )
        # Bundle has no top-level applied_energy_corrections field for reactions.
        self.assertNotIn("applied_energy_corrections", payload)

    def test_reactant_correction_uses_own_sp_key_r0_sp(self):
        # Each species's corrections must anchor to that species's own SP
        # calc — never a sibling's, never the TS's.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        sp = self._species_block(payload, "r0_CHO")
        for entry in sp["applied_energy_corrections"]:
            self.assertEqual(entry["source_calculation_key"], "r0_sp")

    def test_product_correction_uses_own_sp_key_p1_sp(self):
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        sp = self._species_block(payload, "p1_CH3")
        for entry in sp["applied_energy_corrections"]:
            self.assertEqual(entry["source_calculation_key"], "p1_sp")

    def test_ts_correction_uses_ts_sp_key(self):
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        ts = payload["transition_state"]
        for entry in ts["applied_energy_corrections"]:
            self.assertEqual(entry["source_calculation_key"], "ts_sp")

    def test_species_corrections_never_use_ts_sp(self):
        # Defensive: if a future bug let TS routing leak into the species
        # path, the server would 422 (cross-owner reference). Pin it here
        # so the regression is caught at the producer.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        for sp in payload["species"]:
            for entry in sp.get("applied_energy_corrections") or []:
                self.assertNotEqual(entry.get("source_calculation_key"), "ts_sp")

    def test_ts_corrections_never_use_species_sp_keys(self):
        # And the inverse: TS must not pick up a species's SP key.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        ts = payload["transition_state"]
        species_sp_keys = {"r0_sp", "r1_sp", "p0_sp", "p1_sp"}
        for entry in ts.get("applied_energy_corrections") or []:
            self.assertNotIn(
                entry.get("source_calculation_key"), species_sp_keys,
            )

    def test_components_preserved_for_aec_and_pbac(self):
        # AEC components (atom × parameter contribution) and PBAC components
        # (bond × parameter contribution) survive the producer→bundle
        # translation; the producer doesn't dedupe or recompute them.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        sp = self._species_block(payload, "r0_CHO")
        aec = next(e for e in sp["applied_energy_corrections"]
                   if e["application_role"] == "aec_total")
        self.assertEqual(len(aec["components"]), 2)
        self.assertEqual([c["key"] for c in aec["components"]], ["C", "H"])
        bac = next(e for e in sp["applied_energy_corrections"]
                   if e["application_role"] == "bac_total")
        self.assertEqual(len(bac["components"]), 1)

    def test_parameter_unit_stripped_from_components(self):
        # output.yml carries parameter_unit on each component for clarity;
        # TCKDB's AppliedCorrectionComponentPayload rejects unknown fields.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        for sp in payload["species"]:
            for entry in sp.get("applied_energy_corrections") or []:
                for c in entry["components"]:
                    self.assertNotIn("parameter_unit", c)
        ts = payload["transition_state"]
        for entry in ts.get("applied_energy_corrections") or []:
            for c in entry["components"]:
                self.assertNotIn("parameter_unit", c)

    def test_mbac_total_only_no_components_on_ts(self):
        # Melius BAC is a pairwise atom-pair function with a multiplicity
        # correction; per-bond decomposition isn't safe, so the producer
        # ships total-only.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        ts = payload["transition_state"]
        bac = next(e for e in ts["applied_energy_corrections"]
                   if e["application_role"] == "bac_total")
        self.assertEqual(bac["scheme"]["kind"], "bac_melius")
        self.assertEqual(bac["components"], [])

    def test_existing_payload_unchanged_when_no_corrections(self):
        # Backward compat: docs without applied_energy_corrections on
        # species or TS produce a bundle with no applied_energy_corrections
        # field. Older output.yml consumers continue to work.
        _, _, payload = self._submit()  # default fixture has no corrections
        for sp in payload["species"]:
            self.assertNotIn("applied_energy_corrections", sp)
        ts = payload["transition_state"]
        self.assertNotIn("applied_energy_corrections", ts)

    def test_correction_payload_validates_against_live_schema(self):
        # End-to-end pydantic validation when the live schema is reachable.
        _, _, payload = self._submit(output_doc=self._doc_with_corrections())
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_scheme_atom_and_bond_params_reach_bundle(self):
        # Integration: if output.yml carries scheme.atom_params /
        # scheme.bond_params, the bundle's BundleSpeciesIn /
        # BundleTransitionStateIn applied_energy_corrections preserve
        # them. This is the regression test for the original empty
        # ``energy_correction_scheme_atom_param`` /
        # ``energy_correction_scheme_bond_param`` tables.
        doc = self._doc_with_corrections()
        for sp in doc["species"]:
            for entry in sp["applied_energy_corrections"]:
                if entry["scheme"]["kind"] == "atom_energy":
                    entry["scheme"]["atom_params"] = [
                        {"element": "C", "value": -37.84706},
                        {"element": "H", "value": -0.50066},
                    ]
                elif entry["scheme"]["kind"] == "bac_petersson":
                    entry["scheme"]["bond_params"] = [
                        {"bond_key": "C-H", "value": -0.17350},
                    ]
        ts = doc["transition_states"][0]
        for entry in ts["applied_energy_corrections"]:
            if entry["scheme"]["kind"] == "atom_energy":
                entry["scheme"]["atom_params"] = [
                    {"element": "C", "value": -37.84706},
                    {"element": "H", "value": -0.50066},
                ]
        _, _, payload = self._submit(output_doc=doc)
        # Pick one reactant + the TS as the canonical assertion;
        # routing tests already cover all 4 species.
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        aec = next(e for e in r0["applied_energy_corrections"]
                   if e["scheme"]["kind"] == "atom_energy")
        self.assertEqual(
            aec["scheme"]["atom_params"],
            [{"element": "C", "value": -37.84706},
             {"element": "H", "value": -0.50066}],
        )
        bac = next(e for e in r0["applied_energy_corrections"]
                   if e["scheme"]["kind"] == "bac_petersson")
        self.assertEqual(
            bac["scheme"]["bond_params"],
            [{"bond_key": "C-H", "value": -0.17350}],
        )
        ts_aec = next(
            e for e in payload["transition_state"]["applied_energy_corrections"]
            if e["scheme"]["kind"] == "atom_energy"
        )
        self.assertEqual(len(ts_aec["scheme"]["atom_params"]), 2)

    def test_scheme_params_payload_validates_against_live_schema(self):
        # Live-schema validation specifically for the scheme-params path:
        # SchemeAtomParamPayload / SchemeBondParamPayload are accepted on
        # EnergyCorrectionSchemeRef and must not be rejected as extras.
        doc = self._doc_with_corrections()
        for sp in doc["species"]:
            for entry in sp["applied_energy_corrections"]:
                if entry["scheme"]["kind"] == "atom_energy":
                    entry["scheme"]["atom_params"] = [
                        {"element": "C", "value": -37.84706},
                        {"element": "H", "value": -0.50066},
                    ]
                elif entry["scheme"]["kind"] == "bac_petersson":
                    entry["scheme"]["bond_params"] = [
                        {"bond_key": "C-H", "value": -0.17350},
                    ]
        _, _, payload = self._submit(output_doc=doc)
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- frequency-scale-factor on reaction species blocks
    def _doc_with_fsf(self, *, value=0.988,
                      source="J. Phys. Chem. A 2007, 111, 11683",
                      include_freq_level=True):
        """Reaction output doc enriched with FSF metadata at the run level.

        Mirrors :class:`TestComputedSpeciesStatmechFreqScaleFactor`'s
        ``_doc_with_fsf`` so the same producer fields drive both modes.
        """
        doc = _reaction_output_doc()
        doc["freq_scale_factor"] = value
        doc["freq_scale_factor_source"] = source
        if include_freq_level:
            doc["freq_level"] = {
                "method": "wb97xd", "basis": "def2-tzvp", "software": "gaussian",
            }
        return doc

    def test_each_species_block_carries_statmech_with_fsf(self):
        # Every reactant + product BundleSpeciesIn must get its own
        # statmech.frequency_scale_factor when ARC has FSF metadata.
        # Without this, the server's frequency_scale_factor table never
        # populates even though the run-level data is in output.yml.
        _, _, payload = self._submit(output_doc=self._doc_with_fsf())
        for sp in payload["species"]:
            self.assertIn("statmech", sp)
            sm = sp["statmech"]
            self.assertIn("freq_scale_factor", sm)
            fsf = sm["freq_scale_factor"]
            self.assertAlmostEqual(fsf["value"], 0.988)
            self.assertNotIn("scale_kind", fsf)
            self.assertEqual(fsf["level_of_theory"]["method"], "wb97xd")

    def test_species_statmech_emits_scoped_source_calculations(self):
        # Schema expansion: ``BundleStatmechIn`` now accepts
        # ``source_calculations``. Each reactant/product must reference
        # only its own scoped calc keys (r0_*, r1_*, p0_*, p1_*) — the
        # workflow's owner-consistency check rejects cross-species and
        # TS references.
        _, _, payload = self._submit(output_doc=self._doc_with_fsf())
        for sp in payload["species"]:
            self.assertIn("statmech", sp)
            sources = sp["statmech"].get("source_calculations") or []
            # Default fixture carries opt + freq + sp on every species.
            roles = [sc["role"] for sc in sources]
            self.assertEqual(sorted(roles), ["freq", "opt", "sp"])
            own_prefix = sp["key"].split("_", 1)[0] + "_"
            for sc in sources:
                self.assertTrue(
                    sc["calculation_key"].startswith(own_prefix),
                    f"{sp['key']} statmech references foreign calc "
                    f"{sc['calculation_key']!r} (expected prefix "
                    f"{own_prefix!r})",
                )
                self.assertFalse(sc["calculation_key"].startswith("ts_"))

    def test_bare_citation_maps_to_note_not_literature(self):
        # Same policy as computed-species: a free-text citation is a
        # provenance breadcrumb, not a structured Literature row. The
        # producer must never invent a Literature entity from a string.
        citation = "J. Phys. Chem. A 2007, 111, 11683"
        _, _, payload = self._submit(
            output_doc=self._doc_with_fsf(source=citation),
        )
        for sp in payload["species"]:
            fsf = sp["statmech"]["freq_scale_factor"]
            self.assertEqual(fsf["note"], citation)
            self.assertNotIn("source_literature", fsf)

    def test_missing_fsf_omits_statmech_block(self):
        # Strict: when no freq_scale_factor is in the doc, every species
        # block omits ``statmech`` entirely. Empty containers create
        # useless server-side rows and would be misleading.
        _, _, payload = self._submit()  # default fixture has no FSF
        for sp in payload["species"]:
            self.assertNotIn("statmech", sp)

    def test_existing_payload_unchanged_when_fsf_absent(self):
        # Backward compat: the same payload that today's fixture
        # produces (no FSF) must remain structurally identical to before
        # this change. Concrete invariants the rest of the suite relies on:
        # species blocks have key/species_entry/conformers/calculations
        # but no statmech (and no surprise extra fields).
        _, _, payload = self._submit()
        for sp in payload["species"]:
            self.assertIn("key", sp)
            self.assertIn("species_entry", sp)
            self.assertIn("conformers", sp)
            self.assertIn("calculations", sp)
            self.assertNotIn("statmech", sp)

    def test_fsf_payload_validates_against_live_schema(self):
        # Live-schema smoke for the FSF path. Skipped when pydantic /
        # backend isn't reachable in the active env.
        _, _, payload = self._submit(output_doc=self._doc_with_fsf())
        contract_validate(ComputedReactionUploadRequest, payload)

    # ---------------- 16: endpoint is /uploads/computed-reaction
    def test_post_target_endpoint(self):
        _, client, _ = self._submit()
        calls = _upload_calls(client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], COMPUTED_REACTION_ENDPOINT)


class TestComputedReactionProvenanceFields(unittest.TestCase):
    """Reversible flag and analysis_software_release on the reaction bundle.

    Exercises the explicit pass-through of ``reaction_record['reversible']``
    and the Arkane ``analysis_software_release`` builder sourced from
    ``output_doc['arkane_git_commit']``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-rxn-prov-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
        )

    def _submit(self, *, output_doc=None, reaction=None):
        client = _StubClient(response=_StubResponse({"reaction_id": 42}))
        adapter = TCKDBAdapter(
            self.cfg, client_factory=lambda c, k: client,
        )
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=output_doc or _reaction_output_doc(),
                reaction_record=reaction or _reaction_record(),
            )
        return json.loads(outcome.payload_path.read_text())

    # -- reversible ----------------------------------------------------------

    def test_reversible_true_emitted_explicitly(self):
        rxn = _reaction_record()
        rxn["reversible"] = True
        payload = self._submit(reaction=rxn)
        self.assertIs(payload["reversible"], True)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_reversible_false_emitted_explicitly(self):
        rxn = _reaction_record()
        rxn["reversible"] = False
        payload = self._submit(reaction=rxn)
        self.assertIs(payload["reversible"], False)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_reversible_omitted_when_absent(self):
        # ARCReaction has no .reversible attribute today, so the producer
        # leaves the key off the reaction record. The bundle must omit
        # the field too — letting the schema default (True) apply rather
        # than asserting a value the producer never specified.
        rxn = _reaction_record()
        self.assertNotIn("reversible", rxn)
        payload = self._submit(reaction=rxn)
        self.assertNotIn("reversible", payload)
        # The default still validates as True.
        validated = contract_validate(ComputedReactionUploadRequest, payload)
        self.assertIs(validated.reversible, True)

    def test_reversible_none_treated_as_absent(self):
        rxn = _reaction_record()
        rxn["reversible"] = None
        payload = self._submit(reaction=rxn)
        self.assertNotIn("reversible", payload)
        contract_validate(ComputedReactionUploadRequest, payload)

    # -- analysis_software_release ------------------------------------------

    def test_analysis_software_release_emitted_when_arkane_commit_present(self):
        doc = _reaction_output_doc()
        doc["arkane_git_commit"] = "feedface" * 5  # 40 hex chars, realistic shape
        payload = self._submit(output_doc=doc)
        self.assertIn("analysis_software_release", payload)
        self.assertEqual(payload["analysis_software_release"]["name"], "Arkane")
        self.assertEqual(
            payload["analysis_software_release"]["revision"], "feedface" * 5,
        )
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_analysis_software_release_omitted_when_arkane_commit_absent(self):
        doc = _reaction_output_doc()
        doc.pop("arkane_git_commit", None)
        payload = self._submit(output_doc=doc)
        self.assertNotIn("analysis_software_release", payload)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_analysis_software_release_omitted_when_arkane_commit_empty(self):
        doc = _reaction_output_doc()
        doc["arkane_git_commit"] = ""
        payload = self._submit(output_doc=doc)
        self.assertNotIn("analysis_software_release", payload)
        contract_validate(ComputedReactionUploadRequest, payload)


# ---------------------------------------------------------------------------
# Per-species applied_energy_corrections (output.yml -> bundle)
# ---------------------------------------------------------------------------


def _aec_record(value=-0.02345, *, with_components=True, parameter_unit="hartree"):
    """A representative output.yml AEC entry produced by ARC's correction script."""
    rec = {
        "application_role": "aec_total",
        "value": value,
        "value_unit": "hartree",
        "scheme": {
            "kind": "atom_energy",
            "name": "atom_energy",
            "level_of_theory": {"method": "wb97xd3", "basis": "def2tzvp", "software": "qchem"},
            "units": "hartree",
            "version": None,
            "source_literature": None,
            "note": "Per-species AEC computed by Arkane.",
        },
        "components": [],
    }
    if with_components:
        rec["components"] = [
            {"component_kind": "atom", "key": "C", "multiplicity": 1,
             "parameter_value": -37.84993993, "parameter_unit": parameter_unit,
             "contribution_value": -0.0153},
            {"component_kind": "atom", "key": "H", "multiplicity": 4,
             "parameter_value": -0.49991749, "parameter_unit": parameter_unit,
             "contribution_value": -0.00815},
        ]
    return rec


def _pbac_record(value=-0.694):
    return {
        "application_role": "bac_total",
        "value": value,
        "value_unit": "kcal_mol",
        "scheme": {
            "kind": "bac_petersson",
            "name": "bac_petersson",
            "level_of_theory": {"method": "wb97xd3", "basis": "def2tzvp", "software": "qchem"},
            "units": "kcal_mol",
            "version": None,
            "source_literature": None,
            "note": "Per-species BAC computed by Arkane (bac_type=p).",
        },
        "components": [
            {"component_kind": "bond", "key": "C-H", "multiplicity": 4,
             "parameter_value": -0.1735, "parameter_unit": "kcal_mol",
             "contribution_value": -0.694},
        ],
    }


def _mbac_record(value=-0.056):
    return {
        "application_role": "bac_total",
        "value": value,
        "value_unit": "kcal_mol",
        "scheme": {
            "kind": "bac_melius",
            "name": "bac_melius",
            "level_of_theory": {"method": "wb97xd3", "basis": "def2tzvp", "software": "qchem"},
            "units": "kcal_mol",
            "version": None,
            "source_literature": None,
            "note": "Per-species BAC computed by Arkane (bac_type=m).",
        },
        "components": [],
    }


class TestBuildAppliedEnergyCorrectionsHelper(unittest.TestCase):
    """Direct unit tests for `_build_applied_energy_corrections`."""

    def setUp(self):
        from tckdb_arc.adapter import _build_applied_energy_corrections
        self._build = _build_applied_energy_corrections

    def test_aec_passthrough_with_components(self):
        out = self._build([_aec_record()], source_calculation_key="sp")
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["application_role"], "aec_total")
        self.assertAlmostEqual(entry["value"], -0.02345)
        self.assertEqual(entry["value_unit"], "hartree")
        self.assertEqual(entry["scheme"]["kind"], "atom_energy")
        self.assertEqual(entry["scheme"]["name"], "atom_energy")
        self.assertEqual(len(entry["components"]), 2)
        # parameter_unit must be stripped — not in TCKDB component schema
        for c in entry["components"]:
            self.assertNotIn("parameter_unit", c)
            self.assertIn("component_kind", c)
            self.assertIn("contribution_value", c)
        self.assertEqual(entry["source_calculation_key"], "sp")

    def test_pbac_passthrough(self):
        out = self._build([_pbac_record()], source_calculation_key="sp")
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["application_role"], "bac_total")
        self.assertEqual(entry["scheme"]["kind"], "bac_petersson")
        self.assertEqual(entry["value_unit"], "kcal_mol")
        self.assertEqual(len(entry["components"]), 1)

    def test_mbac_total_only_no_components(self):
        out = self._build([_mbac_record()], source_calculation_key="sp")
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["scheme"]["kind"], "bac_melius")
        self.assertEqual(entry["components"], [])

    def test_omits_source_calculation_key_when_none_passed(self):
        out = self._build([_aec_record()], source_calculation_key=None)
        self.assertEqual(len(out), 1)
        self.assertNotIn("source_calculation_key", out[0])

    def test_drops_components_with_null_parameter_value(self):
        rec = _aec_record()
        rec["components"][0]["parameter_value"] = None
        out = self._build([rec], source_calculation_key="sp")
        self.assertEqual(len(out[0]["components"]), 1)  # one dropped
        self.assertEqual(out[0]["components"][0]["key"], "H")

    def test_skips_record_with_null_value(self):
        rec = _aec_record()
        rec["value"] = None
        out = self._build([rec], source_calculation_key="sp")
        self.assertEqual(out, [])

    def test_skips_record_without_scheme(self):
        rec = _aec_record()
        rec["scheme"] = None
        out = self._build([rec], source_calculation_key="sp")
        self.assertEqual(out, [])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(self._build([], source_calculation_key="sp"), [])
        self.assertEqual(self._build(None, source_calculation_key="sp"), [])

    def test_aec_and_bac_both_emitted(self):
        out = self._build(
            [_aec_record(), _pbac_record()],
            source_calculation_key="sp",
        )
        roles = [e["application_role"] for e in out]
        self.assertEqual(roles, ["aec_total", "bac_total"])

    def test_strips_software_from_scheme_level_of_theory(self):
        """TCKDB ``LevelOfTheoryRef`` does not accept ``software`` — record
        software via per-calc software_release elsewhere. The adapter must
        project the ARC LoT dict onto method/basis/aux_basis/cabs_basis."""
        out = self._build([_aec_record()], source_calculation_key="sp")
        lot = out[0]["scheme"]["level_of_theory"]
        self.assertEqual(lot, {"method": "wb97xd3", "basis": "def2tzvp"})
        self.assertNotIn("software", lot)

    def test_scoped_source_calculation_key_passthrough(self):
        """Reaction-mode callers pass scoped keys (r0_sp / p1_sp / ts_sp).
        The helper must stamp them verbatim — it does not know about modes."""
        for key in ("r0_sp", "p0_sp", "p1_sp", "ts_sp"):
            out = self._build([_aec_record()], source_calculation_key=key)
            self.assertEqual(out[0]["source_calculation_key"], key)

    def test_scheme_atom_params_pass_through(self):
        # output.yml ships the AEC parameter table as scheme.atom_params;
        # the adapter must preserve it so TCKDB persists
        # energy_correction_scheme_atom_param rows. Without this, the
        # applied correction lands but the scheme has no parameters
        # backing it.
        rec = _aec_record()
        rec["scheme"]["atom_params"] = [
            {"element": "C", "value": -37.84706},
            {"element": "H", "value": -0.50066},
        ]
        out = self._build([rec], source_calculation_key="sp")
        self.assertEqual(
            out[0]["scheme"]["atom_params"],
            [{"element": "C", "value": -37.84706},
             {"element": "H", "value": -0.50066}],
        )

    def test_scheme_bond_params_pass_through(self):
        rec = _pbac_record()
        rec["scheme"]["bond_params"] = [
            {"bond_key": "C-H", "value": -0.17350},
            {"bond_key": "C=O", "value": -2.63454},
        ]
        out = self._build([rec], source_calculation_key="sp")
        self.assertEqual(
            out[0]["scheme"]["bond_params"],
            [{"bond_key": "C-H", "value": -0.17350},
             {"bond_key": "C=O", "value": -2.63454}],
        )

    def test_scheme_params_absent_means_no_field_in_payload(self):
        # Backward compat: schemes without parameter tables continue to
        # produce a payload without those fields — TCKDB defaults the
        # respective param lists to [] via the schema's default_factory.
        out = self._build([_aec_record()], source_calculation_key="sp")
        scheme = out[0]["scheme"]
        for k in ("atom_params", "bond_params", "component_params"):
            self.assertNotIn(k, scheme)


class TestComputedSpeciesAppliedCorrectionsBundle(unittest.TestCase):
    """End-to-end: output.yml -> computed-species bundle preserves corrections."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-applied-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )

    def _adapter(self, client):
        return TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)

    def _submit_with_corrections(self, applied):
        record = _full_record()
        record["applied_energy_corrections"] = applied
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 7,
            "conformers": [{
                "key": "conf0",
                "conformer_group_id": 3,
                "conformer_observation_id": 11,
                "primary_calculation": {"key": "opt", "calculation_id": 100, "type": "opt", "role": "primary"},
                "additional_calculations": [
                    {"key": "freq", "calculation_id": 101, "type": "freq", "role": "additional"},
                    {"key": "sp", "calculation_id": 102, "type": "sp", "role": "additional"},
                ],
            }],
            "thermo": {"thermo_id": 9},
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=record,
            )
        return json.loads(outcome.payload_path.read_text())

    def test_bundle_carries_applied_energy_corrections(self):
        payload = self._submit_with_corrections([_aec_record(), _pbac_record()])
        self.assertIn("applied_energy_corrections", payload)
        self.assertEqual(len(payload["applied_energy_corrections"]), 2)
        roles = [e["application_role"] for e in payload["applied_energy_corrections"]]
        self.assertEqual(roles, ["aec_total", "bac_total"])

    def test_bundle_omits_block_when_empty(self):
        payload = self._submit_with_corrections([])
        self.assertNotIn("applied_energy_corrections", payload)

    def test_bundle_omits_block_when_record_lacks_field(self):
        # Backwards-compat with output.yml versions that don't emit the field.
        record = _full_record()
        record.pop("applied_energy_corrections", None)
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 1,
            "conformers": [{"key": "conf0", "primary_calculation": {"key": "opt", "calculation_id": 100, "type": "opt", "role": "primary"}, "additional_calculations": []}],
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=record,
            )
        payload = json.loads(outcome.payload_path.read_text())
        self.assertNotIn("applied_energy_corrections", payload)

    def test_payload_validates_against_live_schema(self):
        """If the TCKDB backend is importable, the produced payload must
        validate against the live ComputedSpeciesUploadRequest schema —
        guarding against silent shape drift in the output.yml -> bundle
        translation."""
        payload = self._submit_with_corrections(
            [_aec_record(), _pbac_record()]
        )
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_mbac_payload_validates_against_live_schema(self):
        payload = self._submit_with_corrections(
            [_aec_record(), _mbac_record()]
        )
        contract_validate(ComputedSpeciesUploadRequest, payload)


class TestCalculationConstraints(unittest.TestCase):
    """Held-fixed coordinate constraint emission into TCKDB calc payloads.

    Covers the wiring between ARC's parser-shaped constraint dicts and the
    TCKDB ``CalculationWithResultsPayload.constraints`` field on both the
    primary opt and additional (freq/sp/scan) calcs of a computed-species
    bundle. Reaction bundles share the same ``_build_calc_in_bundle``
    plumbing, so this exercise covers the reaction path indirectly.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-constraints-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_species",
        )

    def _adapter(self, client):
        return TCKDBAdapter(
            self.cfg,
            project_directory=None,
            client_factory=lambda c, k: client,
        )

    def _submit(self, record):
        client = _StubClient(response=_StubResponse({
            "species_entry_id": 7,
            "conformers": [{
                "key": "conf0",
                "conformer_group_id": 3,
                "conformer_observation_id": 11,
                "primary_calculation": {
                    "key": "opt", "calculation_id": 100,
                    "type": "opt", "role": "primary",
                },
                "additional_calculations": [
                    {"key": "freq", "calculation_id": 101,
                     "type": "freq", "role": "additional"},
                    {"key": "sp", "calculation_id": 102,
                     "type": "sp", "role": "additional"},
                ],
            }],
            "thermo": {"thermo_id": 9},
        }))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=record,
            )
        return json.loads(outcome.payload_path.read_text())

    def test_opt_constraints_appear_on_primary_calculation(self):
        record = _full_record()
        # index_base is required on every dict-shaped constraint (C-1c) —
        # these are already 1-based, matching Gaussian's native base.
        record["opt_constraints"] = [
            {"constraint_kind": "bond", "atoms": [1, 2], "target_value": 1.45,
             "index_base": 1},
            {"constraint_kind": "angle", "atoms": [1, 2, 3], "target_value": None,
             "index_base": 1},
        ]
        payload = self._submit(record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertIn("constraints", primary)
        # constraint_index starts at 1 and is deterministic
        self.assertEqual(primary["constraints"][0]["constraint_index"], 1)
        self.assertEqual(primary["constraints"][1]["constraint_index"], 2)
        # bond carries a target_value, angle omits it (target_value=None)
        self.assertEqual(primary["constraints"][0]["constraint_kind"], "bond")
        self.assertEqual(primary["constraints"][0]["atom1_index"], 1)
        self.assertEqual(primary["constraints"][0]["atom2_index"], 2)
        self.assertNotIn("atom3_index", primary["constraints"][0])
        self.assertAlmostEqual(primary["constraints"][0]["target_value"], 1.45)
        self.assertEqual(primary["constraints"][1]["constraint_kind"], "angle")
        self.assertNotIn("target_value", primary["constraints"][1])
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_constraints_omitted_when_record_field_absent(self):
        record = _full_record()
        # No opt_constraints / freq_constraints / sp_constraints set.
        payload = self._submit(record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("constraints", primary)
        for additional in payload["conformers"][0]["additional_calculations"]:
            self.assertNotIn("constraints", additional)

    def test_freq_and_sp_constraints_are_isolated_per_calc(self):
        record = _full_record()
        record["freq_constraints"] = [
            {"constraint_kind": "dihedral", "atoms": [1, 2, 3, 4],
             "target_value": 180.0, "index_base": 1},
        ]
        record["sp_constraints"] = [
            {"constraint_kind": "cartesian_atom", "atoms": [5],
             "target_value": None, "index_base": 1},
        ]
        payload = self._submit(record)
        by_key = {c["key"]: c for c in payload["conformers"][0]["additional_calculations"]}
        self.assertEqual(by_key["freq"]["constraints"][0]["constraint_kind"], "dihedral")
        self.assertEqual(by_key["freq"]["constraints"][0]["atom4_index"], 4)
        self.assertEqual(by_key["sp"]["constraints"][0]["constraint_kind"], "cartesian_atom")
        self.assertEqual(by_key["sp"]["constraints"][0]["atom1_index"], 5)
        self.assertNotIn("atom2_index", by_key["sp"]["constraints"][0])
        # No cross-talk back onto the opt calc.
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("constraints", primary)
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_scan_calc_constraints_attached_inline_via_additional_calculations(self):
        # The scan loop uses ``source_constraints`` from the per-entry
        # ``constraints`` field, so a scan can hold frozen coordinates
        # alongside its active scan coordinate without duplication.
        record = _full_record()
        record["additional_calculations"] = [{
            "key": "scan_rotor_0",
            "type": "scan",
            "scan_result": {
                "dimension": 1,
                "is_relaxed": True,
                "coordinates": [{
                    "coordinate_index": 1,
                    "coordinate_kind": "dihedral",
                    "atom1_index": 1, "atom2_index": 2,
                    "atom3_index": 3, "atom4_index": 4,
                    "step_count": 36, "value_unit": "degree",
                }],
                "points": [],
            },
            "constraints": [
                {"constraint_kind": "bond", "atoms": [5, 6],
                 "target_value": 1.20, "index_base": 1},
            ],
        }]
        payload = self._submit(record)
        scans = [c for c in payload["conformers"][0]["additional_calculations"]
                 if c.get("type") == "scan"]
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["constraints"][0]["constraint_kind"], "bond")
        self.assertEqual(scans[0]["constraints"][0]["atom1_index"], 5)
        # The scanned dihedral lives in scan_result.coordinates[], NOT in
        # the constraints list.
        self.assertNotIn(
            "dihedral",
            [c["constraint_kind"] for c in scans[0]["constraints"]],
        )
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_invalid_constraints_filtered_silently(self):
        # Best-effort contract: a malformed entry must be dropped, not
        # explode the whole payload.
        record = _full_record()
        record["opt_constraints"] = [
            {"constraint_kind": "bond", "atoms": [1], "target_value": None,
             "index_base": 1},          # arity wrong
            {"constraint_kind": "wat", "atoms": [1, 2], "target_value": None,
             "index_base": 1},          # bad kind
            {"constraint_kind": "angle", "atoms": [1, 2, 3], "target_value": None,
             "index_base": 1},          # OK
        ]
        payload = self._submit(record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertEqual(len(primary["constraints"]), 1)
        self.assertEqual(primary["constraints"][0]["constraint_kind"], "angle")
        self.assertEqual(primary["constraints"][0]["constraint_index"], 1)
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_constraints_missing_index_base_dropped_not_assumed(self):
        # C-1c: a dict-shaped constraint with no declared index_base must
        # be refused, not silently treated as already 1-based. ARC's
        # Gaussian (1-based) and ORCA (0-based) constraint parsers always
        # attach index_base (C-1a); real ARC output never reaches this
        # path. This guards a hand-written/older/third-party
        # ``output.yml`` — the adapter accepts those (evidence.py only
        # checks schema_version) and an unshifted ORCA-sourced atom list
        # would otherwise point at the wrong atoms with no signal.
        record = _full_record()
        record["opt_constraints"] = [
            {"constraint_kind": "bond", "atoms": [1, 2], "target_value": 1.45},
        ]
        payload = self._submit(record)
        primary = payload["conformers"][0]["primary_calculation"]
        self.assertNotIn("constraints", primary)
        contract_validate(ComputedSpeciesUploadRequest, payload)

    def test_rotor_scan_end_to_end_through_real_submit_pipeline(self):
        # C-2/C-5, end to end (not just the _neutral_scan_result_to_tckdb
        # unit tests in TestNeutralArcResultTranslation): a real
        # ``rotor_scans[]`` entry on a species record, run through the
        # actual submit pipeline, must both rebase atom indices per its
        # index_base and only stamp resolution_degrees for a degree-unit
        # coordinate -- and the resulting bundle must satisfy the real
        # validator.
        record = _full_record()
        record["rotor_scans"] = [{
            "key": "scan_rotor_0",
            "result": {
                "dimension": 1,
                "relaxed": True,
                "coordinate": {
                    "coordinate_type": "dihedral",
                    "atom_indices": [0, 1, 2, 3],  # 0-based (ORCA-style)
                    "index_base": 0,
                    "unit": "degree",
                    "requested_step_size": 15.0,
                },
                "samples": [
                    {"source_index": 0, "angle_degrees": 0.0},
                    {"source_index": 1, "angle_degrees": 15.0},
                ],
            },
        }]
        payload = self._submit(record)
        scans = [c for c in payload["conformers"][0]["additional_calculations"]
                 if c.get("type") == "scan"]
        self.assertEqual(len(scans), 1)
        coord = scans[0]["scan_result"]["coordinates"][0]
        # 0-based [0,1,2,3] -> 1-based [1,2,3,4].
        self.assertEqual(
            [coord["atom1_index"], coord["atom2_index"],
             coord["atom3_index"], coord["atom4_index"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(coord["resolution_degrees"], 15.0)
        contract_validate(ComputedSpeciesUploadRequest, payload)


class TestCalculationConstraintsSerializer(unittest.TestCase):
    """Direct tests for tckdb_arc.constraints.serialize_constraints."""

    def test_indices_start_at_one_and_are_deterministic(self):
        from tckdb_arc.constraints import serialize_constraints
        items = [
            {"constraint_kind": "bond", "atoms": [1, 2], "index_base": 1},
            {"constraint_kind": "dihedral", "atoms": [3, 4, 5, 6], "index_base": 1},
            {"constraint_kind": "cartesian_atom", "atoms": [7], "index_base": 1},
        ]
        result = serialize_constraints(items)
        self.assertEqual([r["constraint_index"] for r in result], [1, 2, 3])

    def test_dataclass_input_accepted(self):
        from tckdb_arc.constraints import (
            TCKDBCalculationConstraint,
            serialize_constraints,
        )
        c = TCKDBCalculationConstraint(
            constraint_kind="bond", atom1_index=1, atom2_index=2,
            target_value=1.5,
        )
        out = serialize_constraints([c])
        self.assertEqual(out[0]["atom1_index"], 1)
        self.assertEqual(out[0]["atom2_index"], 2)
        self.assertAlmostEqual(out[0]["target_value"], 1.5)

    def test_empty_input_yields_empty_list(self):
        from tckdb_arc.constraints import serialize_constraints
        self.assertEqual(serialize_constraints([]), [])
        self.assertEqual(serialize_constraints(None or []), [])

    # ---------------- C-1c: index_base is mandatory on every dict shape
    def test_legacy_dict_missing_index_base_dropped(self):
        # The pre-fix behavior silently treated a bare {'constraint_kind',
        # 'atoms'} dict as already 1-based. That's unsafe: ARC's ORCA
        # constraint parser emits 0-based atoms with the same shape
        # (modulo index_base), so an ORCA-sourced dict missing index_base
        # would have been accepted pointing at the wrong atom.
        from tckdb_arc.constraints import serialize_constraints
        self.assertEqual(
            serialize_constraints([{"constraint_kind": "bond", "atoms": [1, 2]}]),
            [],
        )

    def test_legacy_dict_with_index_base_zero_is_rebased(self):
        # ORCA-sourced legacy-shaped constraint: atoms [0, 1] at
        # index_base=0 must land on TCKDB atoms [1, 2], exactly like the
        # neutral coordinate_type shape already does.
        from tckdb_arc.constraints import serialize_constraints
        result = serialize_constraints([
            {"constraint_kind": "bond", "atoms": [0, 1], "index_base": 0,
             "target_value": 1.4},
        ])
        self.assertEqual(result, [{
            "constraint_index": 1,
            "constraint_kind": "bond",
            "atom1_index": 1,
            "atom2_index": 2,
            "target_value": 1.4,
        }])

    def test_legacy_dict_with_index_base_one_is_unshifted(self):
        from tckdb_arc.constraints import serialize_constraints
        result = serialize_constraints([
            {"constraint_kind": "bond", "atoms": [1, 2], "index_base": 1},
        ])
        self.assertEqual(result[0]["atom1_index"], 1)
        self.assertEqual(result[0]["atom2_index"], 2)

    def test_legacy_dict_invalid_index_base_dropped(self):
        from tckdb_arc.constraints import serialize_constraints
        self.assertEqual(serialize_constraints([
            {"constraint_kind": "bond", "atoms": [1, 2], "index_base": 2},
        ]), [])
        self.assertEqual(serialize_constraints([
            {"constraint_kind": "bond", "atoms": [1, 2], "index_base": "nope"},
        ]), [])

    def test_dataclass_input_exempt_from_index_base_requirement(self):
        # A TCKDBCalculationConstraint instance is never produced by an
        # ARC parser — only by a caller that already normalized its own
        # indices — so it is not subject to the mapping-only index_base
        # requirement.
        from tckdb_arc.constraints import (
            TCKDBCalculationConstraint,
            serialize_constraints,
        )
        c = TCKDBCalculationConstraint(constraint_kind="bond", atom1_index=1,
                                       atom2_index=2)
        result = serialize_constraints([c])
        self.assertEqual(len(result), 1)


class TestArcArgsToKeywords(unittest.TestCase):
    """Determinism guarantees for ``_arc_args_to_keywords``.

    The output participates in TCKDB's ``lot_hash``, so two args dicts
    that differ only in insertion order must serialize identically — a
    regression here would fragment the LoT row across runs.
    """

    def setUp(self):
        from tckdb_arc.adapter import _arc_args_to_keywords
        self.flatten = _arc_args_to_keywords

    def test_none_returns_none(self):
        self.assertIsNone(self.flatten(None))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(self.flatten({}))

    def test_dict_with_only_empty_category_returns_none(self):
        self.assertIsNone(self.flatten({"keyword": {}}))

    def test_non_dict_returns_none(self):
        self.assertIsNone(self.flatten("dlpno=tight"))
        self.assertIsNone(self.flatten(42))
        self.assertIsNone(self.flatten(["dlpno"]))

    def test_non_mapping_category_value_skipped(self):
        # Defensive: a list under a category shouldn't crash; it just
        # doesn't contribute entries.
        self.assertIsNone(self.flatten({"keyword": ["TightPNO"]}))

    def test_includes_keyword_entries(self):
        out = self.flatten({"keyword": {"rijcosx": "RIJCOSX",
                                        "grid": "DEFGRID3"}})
        self.assertEqual(
            out,
            'keyword:grid="DEFGRID3"; keyword:rijcosx="RIJCOSX"',
        )

    def test_includes_block_entries(self):
        out = self.flatten({"block": {"scf": "MaxIter 500\nDIIS true"}})
        self.assertEqual(out, 'block:scf="MaxIter 500\\nDIIS true"')

    def test_includes_both_keyword_and_block_categories_sorted(self):
        out = self.flatten({
            "keyword": {"dlpno_threshold": "TightPNO", "rijcosx": "RIJCOSX",
                        "grid": "DEFGRID3", "uno": "UNO"},
            "block": {"scf": "MaxIter 500\nDIIS true"},
        })
        self.assertEqual(
            out,
            'block:scf="MaxIter 500\\nDIIS true"; '
            'keyword:dlpno_threshold="TightPNO"; '
            'keyword:grid="DEFGRID3"; '
            'keyword:rijcosx="RIJCOSX"; '
            'keyword:uno="UNO"',
        )

    def test_skips_none_values(self):
        out = self.flatten({"keyword": {"a": "x", "b": None, "c": "y"}})
        self.assertEqual(out, 'keyword:a="x"; keyword:c="y"')

    def test_deterministic_independent_of_input_order(self):
        a = {"keyword": {"a": 1, "b": 2, "c": 3},
             "block": {"x": "X", "y": "Y"}}
        b = {"block": {"y": "Y", "x": "X"},
             "keyword": {"c": 3, "a": 1, "b": 2}}
        self.assertEqual(self.flatten(a), self.flatten(b))

    def test_serializes_nested_list_and_bool_deterministically(self):
        # Dict-valued args are JSON-dumped with sort_keys=True so that
        # nested structure also dedups across insertion orders.
        a = self.flatten({"keyword": {
            "iters": [1, 2, 3],
            "use_uno": True,
            "thresholds": {"e": 1e-9, "d": 1e-6},
        }})
        b = self.flatten({"keyword": {
            "thresholds": {"d": 1e-6, "e": 1e-9},
            "use_uno": True,
            "iters": [1, 2, 3],
        }})
        self.assertEqual(a, b)
        self.assertIn("keyword:iters=[1,2,3]", a)
        self.assertIn("keyword:use_uno=true", a)
        self.assertIn('keyword:thresholds={"d":1e-06,"e":1e-09}', a)


class TestArcLevelToTckdbLot(unittest.TestCase):
    """Field-name translation from ARC's Level dict to TCKDB's
    ``LevelOfTheoryRef``. ARC writes ``auxiliary_basis``/``cabs``/
    ``solvation_method``; TCKDB consumes ``aux_basis``/``cabs_basis``/
    ``solvent_model`` — the projection must rename, not pass through."""

    def setUp(self):
        from tckdb_arc.adapter import _arc_level_to_tckdb_lot
        self.project = _arc_level_to_tckdb_lot

    def test_none_or_no_method_returns_none(self):
        self.assertIsNone(self.project(None))
        self.assertIsNone(self.project({}))
        self.assertIsNone(self.project({"basis": "cc-pvtz-f12"}))

    def test_renames_arc_field_names_to_tckdb(self):
        out = self.project({
            "method": "DLPNO-CCSD(T)-F12",
            "basis": "cc-pVTZ-F12",
            "auxiliary_basis": "aug-cc-pVTZ/C",
            "cabs": "cc-pVTZ-F12-CABS",
            "solvation_method": "smd",
            "solvent": "water",
            "dispersion": "gd3bj",
            "software": "orca",        # dropped — lives on software_release
            "software_version": "5.0", # dropped — lives on software_release
            "method_type": "wavefunction",  # no TCKDB counterpart
            "year": 2024,                   # no TCKDB counterpart
        })
        self.assertEqual(out, {
            "method": "DLPNO-CCSD(T)-F12",
            "basis": "cc-pVTZ-F12",
            "aux_basis": "aug-cc-pVTZ/C",
            "cabs_basis": "cc-pVTZ-F12-CABS",
            "solvent_model": "smd",
            "solvent": "water",
            "dispersion": "gd3bj",
        })

    def test_includes_keywords_when_args_present(self):
        out = self.project({
            "method": "dlpno-ccsd(t)-f12",
            "basis": "cc-pvtz-f12",
            "args": {"keyword": {"dlpno_threshold": "TightPNO"}},
        })
        self.assertEqual(out["keywords"],
                         'keyword:dlpno_threshold="TightPNO"')

    def test_omits_keywords_when_args_empty(self):
        out = self.project({
            "method": "wb97xd",
            "basis": "def2tzvp",
            "args": {},
        })
        self.assertNotIn("keywords", out)


class TestComputedReactionDependencyEdges(unittest.TestCase):
    """``optimized_from`` dependency edges in computed-reaction bundles.

    Exercises two gaps the audit found:
      - Part A: reactant/product opt → opt_coarse (parity with the
        species-side path, which previously hardcoded depends_on=None).
      - Part B: ts_opt → ts_guess (path_search: NEB / GSM) — geometry-only
        TS guesses (heuristics, AutoTST, user-supplied) stay edge-less.

    The reaction-bundle flatten step (``_flatten_all_reaction_calcs``)
    pops wrapped ``opt_result``/etc. but leaves ``depends_on`` at the
    calculation-object level, so these tests assert against the
    post-flatten payload that actually gets uploaded.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-rxn-deps-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
        )

    def _adapter(self, client):
        return TCKDBAdapter(
            self.cfg,
            client_factory=lambda c, k: client,
        )

    def _submit(self, *, output_doc=None, reaction=None):
        client = _StubClient(response=_StubResponse({"reaction_id": 42}))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=output_doc or _reaction_output_doc(),
                reaction_record=reaction or _reaction_record(),
            )
        return outcome, client, json.loads(outcome.payload_path.read_text())

    @staticmethod
    def _add_coarse_to_species(doc, label):
        spc = next(s for s in doc["species"] if s["label"] == label)
        spc["coarse_opt_log"] = f"calcs/.../{label}/coarse/input.log"
        spc["coarse_opt_n_steps"] = 7
        spc["coarse_opt_final_energy_hartree"] = -100.05
        spc["coarse_opt_input_xyz"] = "C 9.999 9.999 9.999\nH 8.888 8.888 8.888"
        spc["coarse_opt_output_xyz"] = "C 1.111 2.222 3.333\nH 4.444 5.555 6.666"
        spc["opt_input_xyz"] = spc["coarse_opt_output_xyz"]
        return spc

    # ------------------------------------------------------------------
    # Part A: reaction-side coarse → fine opt parity
    # ------------------------------------------------------------------

    def test_reactant_with_coarse_emits_namespaced_opt_coarse(self):
        doc = _reaction_output_doc()
        self._add_coarse_to_species(doc, "CHO")
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        keys = sorted(c["key"] for c in r0["calculations"])
        self.assertIn("r0_opt_coarse", keys)
        opt_coarse = next(c for c in r0["calculations"] if c["key"] == "r0_opt_coarse")
        # Same calc type ("opt") as the species-side path; only the
        # bundle-local key is namespaced.
        self.assertEqual(opt_coarse["type"], "opt")

    def test_reactant_fine_opt_depends_on_namespaced_opt_coarse(self):
        doc = _reaction_output_doc()
        self._add_coarse_to_species(doc, "CHO")
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        primary = r0["conformers"][0]["calculation"]
        self.assertEqual(primary["key"], "r0_opt")
        self.assertEqual(
            primary["depends_on"],
            [{"parent_calculation_key": "r0_opt_coarse", "role": "optimized_from"}],
        )
        # opt_coarse is chain head — no upstream calc edge.
        opt_coarse = next(c for c in r0["calculations"] if c["key"] == "r0_opt_coarse")
        self.assertNotIn("depends_on", opt_coarse)

    def test_product_with_coarse_emits_namespaced_edge(self):
        doc = _reaction_output_doc()
        self._add_coarse_to_species(doc, "CH3")  # product side
        _, _, payload = self._submit(output_doc=doc)
        p1 = next(s for s in payload["species"] if s["key"] == "p1_CH3")
        keys = [c["key"] for c in p1["calculations"]]
        self.assertIn("p1_opt_coarse", keys)
        primary = p1["conformers"][0]["calculation"]
        self.assertEqual(
            primary["depends_on"],
            [{"parent_calculation_key": "p1_opt_coarse", "role": "optimized_from"}],
        )

    def test_missing_coarse_log_emits_no_opt_coarse_or_edge(self):
        doc = _reaction_output_doc()
        # No coarse_opt_log at all on the reactant — single-stage opt.
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        keys = [c["key"] for c in r0["calculations"]]
        self.assertNotIn("r0_opt_coarse", keys)
        primary = r0["conformers"][0]["calculation"]
        self.assertNotIn("depends_on", primary)

    def test_unparseable_coarse_geometry_falls_back_no_edge(self):
        doc = _reaction_output_doc()
        spc = self._add_coarse_to_species(doc, "CHO")
        spc["coarse_opt_output_xyz"] = None  # parse failure
        # opt_input_xyz reverts to species's truly-initial xyz.
        spc["opt_input_xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0"
        _, _, payload = self._submit(output_doc=doc)
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        keys = [c["key"] for c in r0["calculations"]]
        self.assertNotIn("r0_opt_coarse", keys)
        primary = r0["conformers"][0]["calculation"]
        self.assertNotIn("depends_on", primary)

    def test_reaction_coarse_namespaces_are_unique_across_species(self):
        # Both a reactant and a product carry coarse — keys must not collide.
        doc = _reaction_output_doc()
        self._add_coarse_to_species(doc, "CHO")  # → r0_opt_coarse
        self._add_coarse_to_species(doc, "CH3")  # → p1_opt_coarse
        _, _, payload = self._submit(output_doc=doc)
        all_keys = []
        for sp in payload["species"]:
            all_keys.extend(c["key"] for c in sp["calculations"])
            for conf in sp["conformers"]:
                all_keys.append(conf["calculation"]["key"])
        self.assertEqual(len(set(all_keys)), len(all_keys),
                         msg=f"duplicate calc keys: {all_keys}")
        self.assertIn("r0_opt_coarse", all_keys)
        self.assertIn("p1_opt_coarse", all_keys)

    # ------------------------------------------------------------------
    # Part B: ts_opt → ts_guess (path_search: NEB / GSM)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_neb_ts(doc):
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "orca_neb"
        ts["neb_log"] = "calcs/.../TS0/neb/input.log"
        # path_search_result.points requires min_length=1; fallback
        # uses opt_input_xyz when the log isn't on disk to parse.
        ts["opt_input_xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.5 0.5 0.0"
        # What ARC exports for an ORCA NEB run: ``neb_level`` (from
        # orca_neb_settings) and the ESS observed for the NEB log, distinct
        # from the run's opt level/program.
        doc["neb_level"] = dict(NEB_LEVEL)
        ts["ess_software"] = {"opt": "gaussian", "neb": "orca"}
        ts["ess_versions"] = {"opt": "Gaussian 16, Revision A.03",
                              "neb": "ORCA 5.0.4"}
        return ts

    @staticmethod
    def _make_gsm_ts(doc):
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "xtb_gsm"
        ts["gsm_log"] = "calcs/.../TS0/gsm/stringfile.xyz0000"
        ts["opt_input_xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.5 0.5 0.0"
        return ts

    def test_neb_chosen_ts_emits_path_search_ts_guess_calc(self):
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        _, _, payload = self._submit(output_doc=doc)
        ts = payload["transition_state"]
        keys = sorted(c["key"] for c in ts["calculations"])
        self.assertIn("ts_guess", keys)
        ts_guess = next(c for c in ts["calculations"] if c["key"] == "ts_guess")
        self.assertEqual(ts_guess["type"], "path_search")
        psr = ts_guess.get("path_search_result")
        self.assertEqual(psr.get("method"), "neb")
        # Single-point fallback (no on-disk NEB log to parse): one
        # ts_guess point keyed off opt_input_xyz, marked is_ts_guess.
        self.assertEqual(len(psr.get("points", [])), 1)
        self.assertTrue(psr["points"][0]["is_ts_guess"])
        # Chain head: no parent calculation.
        self.assertNotIn("depends_on", ts_guess)

    def test_neb_path_search_carries_the_exported_neb_level_and_software(self):
        # ARC exports ``neb_level`` and the observed NEB program/banner. The
        # path-search calculation states those, not the opt level and the opt
        # program (BRIDGE_ROADMAP A3).
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        doc["opt_level"] = {"method": "b3lyp", "basis": "6-31g(d)",
                            "software": "gaussian"}
        _, _, payload = self._submit(output_doc=doc)
        calcs = payload["transition_state"]["calculations"]
        ts_guess = next(c for c in calcs if c["key"] == "ts_guess")
        self.assertEqual(ts_guess["level_of_theory"]["method"], "wb97xd")
        self.assertEqual(ts_guess["level_of_theory"]["basis"], "def2tzvp")
        self.assertEqual(ts_guess["software_release"]["name"], "orca")
        self.assertEqual(ts_guess["software_release"]["version"], "5.0.4")
        ts_opt = payload["transition_state"]["calculation"]
        self.assertEqual(ts_opt["level_of_theory"]["method"], "b3lyp")
        self.assertEqual(ts_opt["software_release"]["name"], "gaussian")

    def test_neb_without_an_exported_neb_level_is_omitted_not_filed_at_opt_level(self):
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        del doc["neb_level"]
        outcome, _, payload = self._submit(output_doc=doc)
        ts = payload["transition_state"]
        self.assertNotIn("ts_guess", [c["key"] for c in ts["calculations"]])
        self.assertNotIn("depends_on", ts["calculation"])
        self.assertNotIn("path_search", json.dumps(payload))
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "ts_guess_level_not_stated"]
        self.assertEqual(warning["context"]["method"], "neb")
        self.assertIn("neb_level", warning["message"])

    def test_neb_without_an_observed_neb_program_is_omitted(self):
        # ``neb_level`` is a Level dict whose software ARC deduces from the
        # method (wb97xd/def2tzvp -> gaussian), not from what ran; only
        # ``ess_software.neb`` says which program produced the NEB log.
        doc = _reaction_output_doc()
        ts = self._make_neb_ts(doc)
        ts["ess_software"] = {"opt": "gaussian"}  # no "neb"
        doc["neb_level"] = {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian"}
        outcome, _, payload = self._submit(output_doc=doc)
        self.assertNotIn("path_search", json.dumps(payload))
        self.assertNotIn("depends_on", payload["transition_state"]["calculation"])
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "ts_guess_software_not_stated"]
        self.assertEqual(warning["context"]["method"], "neb")
        self.assertNotIn("ts_guess_level_not_stated", {w["code"] for w in outcome.warnings})

    def test_neb_level_is_never_borrowed_from_the_opt_level_when_only_opt_is_set(self):
        # A doc carrying opt/freq/sp levels but no neb_level must not get a
        # path-search calculation from any of them.
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        doc.pop("neb_level")
        doc["freq_level"] = dict(doc["opt_level"])
        doc["sp_level"] = dict(doc["opt_level"])
        _, _, payload = self._submit(output_doc=doc)
        types = [c["type"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("path_search", types)

    def test_gsm_chosen_ts_omits_the_path_search_calc_and_warns(self):
        # ARC exports no level for the xtb-gsm path search (only ``neb_level``
        # for ORCA NEB), and TCKDB requires a level on every calculation. The
        # calculation is omitted rather than filed at opt_level, and the
        # omission is reported.
        doc = _reaction_output_doc()
        self._make_gsm_ts(doc)
        outcome, _, payload = self._submit(output_doc=doc)
        ts = payload["transition_state"]
        self.assertNotIn("ts_guess", [c["key"] for c in ts["calculations"]])
        self.assertNotIn("path_search", json.dumps(payload))
        self.assertNotIn("depends_on", ts["calculation"])
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "ts_guess_level_not_stated"]
        self.assertEqual(warning["context"]["method"], "gsm")
        self.assertEqual(warning["context"]["action"],
                         "path_search_calculation_omitted")

    def test_gsm_is_omitted_even_when_a_neb_level_is_exported(self):
        # ``neb_level`` is the ORCA NEB level; a GSM guess never inherits it.
        doc = _reaction_output_doc()
        self._make_gsm_ts(doc)
        doc["neb_level"] = dict(NEB_LEVEL)
        outcome, _, payload = self._submit(output_doc=doc)
        self.assertNotIn("path_search", json.dumps(payload))
        self.assertIn("ts_guess_level_not_stated",
                      {w["code"] for w in outcome.warnings})

    def test_gsm_method_alias_dash_form_matches(self):
        doc = _reaction_output_doc()
        ts = self._make_gsm_ts(doc)
        ts["chosen_ts_method"] = "xTB-GSM"  # producer typing variation
        outcome, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "ts_guess_level_not_stated"]
        self.assertEqual(warning["context"]["method"], "gsm")

    def test_gsm_chosen_ts_without_gsm_log_emits_no_parent_calc(self):
        # Mirror of NEB-without-log: chosen method is GSM but the
        # producer didn't expose the log path. Conservative: no parent.
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "xtb_gsm"
        ts["gsm_log"] = None
        # Even a stray neb_log shouldn't cross-pollinate methods.
        ts["neb_log"] = "calcs/.../TS0/neb/input.log"
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)

    def test_dedup_merged_gsm_source_emits_path_search_ts_guess_calc(self):
        # Benchmark reaction_06: GCN won equivalent-guess dedup
        # (chosen_ts_method='gcn', a geometry-only method) but xtb-gsm
        # merged into the chosen guess. The path_search calc must still
        # emit, gated off the chosen guess's method_sources plus the
        # populated gsm_log — not off the single primary method. (Since 0.6.4
        # the GSM calculation is then omitted for lack of an exported level.)
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "gcn"
        ts["gsm_log"] = "calcs/.../TS0/gsm/stringfile.xyz0000"
        ts["opt_input_xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.5 0.5 0.0"
        ts["ts_guesses"] = [
            {"index": 0, "method": "gcn",
             "method_sources": ["gcn", "xtb-gsm"], "chosen": True},
        ]
        outcome, _, payload = self._submit(output_doc=doc)
        ts_out = payload["transition_state"]
        keys = [c["key"] for c in ts_out["calculations"]]
        # The gate still recognises the merged GSM source; with no GSM level
        # exported the calculation is omitted and reported.
        self.assertNotIn("ts_guess", keys)
        [warning] = [w for w in outcome.warnings
                     if w["code"] == "ts_guess_level_not_stated"]
        self.assertEqual(warning["context"]["method"], "gsm")

    def test_dedup_merged_prefers_populated_log_field(self):
        # Chosen guess merged BOTH orca_neb and xtb-gsm, but the producer
        # preserved only the NEB log (neb_log populated, gsm_log absent).
        # The gate must select the method whose log field is populated so
        # the gate and the subsequent log lookup agree.
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "gcn"
        ts["neb_log"] = "calcs/.../TS0/neb/input.log"
        ts["gsm_log"] = None
        ts["opt_input_xyz"] = "C 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.5 0.5 0.0"
        doc["neb_level"] = dict(NEB_LEVEL)
        ts["ess_software"] = {"opt": "gaussian", "neb": "orca"}
        ts["ts_guesses"] = [
            {"index": 0, "method": "gcn",
             "method_sources": ["gcn", "xtb-gsm", "orca_neb"], "chosen": True},
        ]
        _, _, payload = self._submit(output_doc=doc)
        ts_guess = next(c for c in payload["transition_state"]["calculations"]
                        if c["key"] == "ts_guess")
        self.assertEqual(ts_guess["path_search_result"].get("method"), "neb")

    def test_dedup_merged_geometry_only_sources_no_parent_calc(self):
        # GCN won and only geometry-only methods merged in — a stray
        # gsm_log must NOT trigger a path_search calc without a
        # path-search source in method_sources.
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "gcn"
        ts["gsm_log"] = "calcs/.../TS0/gsm/stringfile.xyz0000"
        ts["ts_guesses"] = [
            {"index": 0, "method": "gcn",
             "method_sources": ["gcn", "heuristics"], "chosen": True},
        ]
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)

    def test_no_payload_carries_legacy_neb_type(self):
        # Regression guard: nothing in the bundle may emit the old
        # NEB-specific calculation type or wrapped result key.
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        _, _, payload = self._submit(output_doc=doc)
        blob = json.dumps(payload)
        self.assertNotIn('"type": "neb"', blob)
        self.assertNotIn('"neb_result"', blob)

    def test_ts_opt_depends_on_ts_guess_with_optimized_from(self):
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        _, _, payload = self._submit(output_doc=doc)
        primary = payload["transition_state"]["calculation"]
        self.assertEqual(primary["key"], "ts_opt")
        self.assertEqual(
            primary["depends_on"],
            [{"parent_calculation_key": "ts_guess", "role": "optimized_from"}],
        )

    def test_neb_method_match_is_case_insensitive(self):
        doc = _reaction_output_doc()
        ts = self._make_neb_ts(doc)
        ts["chosen_ts_method"] = "ORCA_NEB"  # producer typing variation
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertIn("ts_guess", keys)

    def test_missing_neb_log_emits_no_ts_guess_or_edge(self):
        # Conservative gate: chosen_ts_method == NEB but no log path means
        # we have no provenance to anchor the parent calc — fall back to
        # geometry-only ts_opt.
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "orca_neb"
        ts["neb_log"] = None
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)
        primary = payload["transition_state"]["calculation"]
        self.assertNotIn("depends_on", primary)

    def test_heuristic_ts_guess_emits_no_parent_calc(self):
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "Heuristics"
        ts["neb_log"] = None
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)
        self.assertNotIn(
            "depends_on", payload["transition_state"]["calculation"],
        )

    def test_autotst_ts_guess_emits_no_parent_calc(self):
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "AutoTST"
        # Even if the producer left a stray neb_log path, the method
        # gate must reject — AutoTST is not a NEB calc.
        ts["neb_log"] = "calcs/.../TS0/neb/input.log"
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)

    def test_user_guess_emits_no_parent_calc(self):
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "user guess 0"
        ts["neb_log"] = None
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)

    def test_no_chosen_ts_method_emits_no_parent_calc(self):
        # Older runs / records that never set chosen_ts_method.
        doc = _reaction_output_doc()
        # Default fixture has no chosen_ts_method on the ts.
        _, _, payload = self._submit(output_doc=doc)
        keys = [c["key"] for c in payload["transition_state"]["calculations"]]
        self.assertNotIn("ts_guess", keys)

    # ---------------- selected TS-guess: workflow narrative, not uploaded
    # ARC's selected TS-guess method (heuristics / AutoTST / KinBot /
    # GCN / user / orca_neb / xtb_gsm / …) is workflow narrative —
    # TCKDB has no schema-reviewed slot for "what generated the guess
    # that fed ts_opt". The optimized TS itself (geometry, freq imag
    # mode, IRC connectivity to reactant/product) is the scientific
    # evidence the database stores; the upstream guess source is
    # ARC-internal context that lives in ARC's artifacts.
    #
    # NEB/GSM are the *exception*: they ship actual scientific path-
    # search data (per-image energies, geometries) on a real
    # ``path_search_result`` accepted by the schema. That's not a
    # workflow label — it's a calc result.
    def test_chosen_ts_method_never_emits_origin_on_ts_opt(self):
        # Every method family ARC can select must result in NO
        # ``selected_ts_guess`` marker on ts_opt's parameters_json.
        for method in ("Heuristics", "AutoTST", "KinBot", "GCN",
                       "user guess 0", "orca_neb", "xtb_gsm"):
            with self.subTest(method=method):
                doc = _reaction_output_doc()
                doc["transition_states"][0]["chosen_ts_method"] = method
                _, _, payload = self._submit(output_doc=doc)
                ts_opt = payload["transition_state"]["calculation"]
                pj = ts_opt.get("parameters_json", {}) or {}
                origin = pj.get("tckdb_origin")
                if origin is not None:
                    self.assertNotEqual(origin.get("origin_kind"),
                                        "selected_ts_guess")

    def test_neb_path_search_result_is_kept_as_scientific_data(self):
        # Carve-out: NEB/GSM still emits a ts_guess calc carrying
        # ``path_search_result`` because that's actual scientific
        # path-search data (per-image energies + geometries), not just
        # a method label. Removing this would lose real scientific
        # content.
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        _, _, payload = self._submit(output_doc=doc)
        ts_block = payload["transition_state"]
        ts_guess = next(c for c in ts_block["calculations"]
                        if c["key"] == "ts_guess")
        self.assertEqual(ts_guess["type"], "path_search")
        self.assertIn("path_search_result", ts_guess)
        # And ts_opt itself does NOT pick up a redundant
        # selected_ts_guess marker even when NEB ran.
        ts_opt = ts_block["calculation"]
        pj = ts_opt.get("parameters_json", {}) or {}
        if pj.get("tckdb_origin") is not None:
            self.assertNotEqual(pj["tckdb_origin"].get("origin_kind"),
                                "selected_ts_guess")

    def test_arc_workflow_narrative_keys_never_appear_in_payload(self):
        # Single comprehensive guardrail: regardless of which method
        # ARC selected and which staged history the source record
        # carries, the payload must NOT contain any of these keys
        # anywhere — they're ARC-internal narrative, not generalized
        # scientific data. (Operational/runtime fields are covered by
        # the matching guardrail in the computed-species suite.)
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "AutoTST"
        ts["successful_methods"] = ["AutoTST", "Heuristics"]
        ts["unsuccessful_methods"] = ["KinBot", "GCN"]
        ts["ts_report"] = ("This is a long ARC TS report describing every "
                           "attempted method, its score, why it was selected "
                           "or rejected, etc.")
        ts["chosen_ts_list"] = [3, 5, 7]
        # Operational/runtime fields the producer must never propagate.
        ts["server"] = "node42.cluster"
        ts["queue"] = "long"
        ts["job_id"] = "12345"
        ts["run_time"] = 3600
        ts["walltime"] = 86400
        _, _, payload = self._submit(output_doc=doc)
        forbidden_keys = {
            # TS-search history
            "successful_methods", "unsuccessful_methods",
            "ts_report", "chosen_ts_list",
            # Operational
            "server", "queue", "job_id", "run_time", "walltime",
            "attempted_queues", "ess_trsh_methods",
            "restart", "restart_count", "scratch_path", "local_path",
            "username", "token", "api_key", "password",
        }
        forbidden_substrings = {
            # selected_ts_guess marker (no longer emitted)
            "selected_ts_guess",
            # Conformer-screen narrative (covered by computed-species
            # guardrail too — defense in depth)
            "relative_e0_kj_mol",
        }

        def _walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(
                        k, forbidden_keys,
                        f"workflow-narrative key {k!r} leaked at {path}",
                    )
                    _walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _walk(item, f"{path}[{i}]")
        _walk(payload)
        text = json.dumps(payload)
        for needle in forbidden_substrings:
            self.assertNotIn(needle, text,
                             f"workflow-narrative substring {needle!r} leaked")

    # ---------------- unmapped_smiles on reaction participant species
    def test_reaction_participant_emits_unmapped_smiles_when_distinct(self):
        # Same passthrough behavior as standalone species: when a
        # reactant/product record has unmapped_smiles distinct from
        # smiles, it lands on the bundle's species[*].species_entry.
        doc = _reaction_output_doc()
        target = next(s for s in doc["species"] if s["label"] == "CHO")
        target["smiles"] = "[CH:1]=[O:2]"           # mapped
        target["unmapped_smiles"] = "[CH]=O"        # canonical
        _, _, payload = self._submit(output_doc=doc)
        cho_block = next(s for s in payload["species"]
                         if s["key"].startswith("r0"))
        self.assertEqual(cho_block["species_entry"]["smiles"],
                         "[CH:1]=[O:2]")
        self.assertEqual(cho_block["species_entry"]["unmapped_smiles"],
                         "[CH]=O")

    def test_reaction_participant_omits_unmapped_smiles_when_absent(self):
        # Default reaction fixture has no unmapped_smiles on
        # participants → field stays absent on every species_entry.
        _, _, payload = self._submit()
        for spc in payload["species"]:
            self.assertNotIn("unmapped_smiles", spc["species_entry"])

    def test_atom_map_and_mapping_history_never_leak_to_payload(self):
        # ARC carries ``reaction.atom_map`` (an integer permutation
        # array used internally for geometry alignment), and may have
        # mapping/template metadata on the reaction record. None of
        # these belong in TCKDB. Stage every plausible key on the
        # input record and assert the wire payload contains none of
        # them, at any depth.
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        # Stage atom-map / mapping-history / template metadata anywhere
        # the producer might be tempted to copy them.
        ts["atom_map"] = [3, 0, 1, 2]
        ts["mapping_history"] = [{"step": 1, "method": "naive"}]
        ts["reaction_template"] = "H_Abstraction/H_to_H"
        ts["template_labels"] = ["X1", "X2"]
        for s in doc["species"]:
            s["atom_map"] = [0, 1]
        from test_adapter import _reaction_record  # local import
        rxn = _reaction_record()
        rxn["atom_map"] = [3, 0, 1, 2]
        rxn["reaction_template"] = "H_Abstraction/H_to_H"
        _, _, payload = self._submit(output_doc=doc, reaction=rxn)
        forbidden_keys = {
            "atom_map", "mapping_history",
            "reaction_template", "template_labels",
            # Defense in depth: previously-removed narrative
            "successful_methods", "unsuccessful_methods",
            "chosen_ts_list", "ts_report",
        }
        forbidden_substrings = {"relative_e0_kj_mol", "selected_ts_guess"}

        def _walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(
                        k, forbidden_keys,
                        f"forbidden key {k!r} leaked at {path}",
                    )
                    _walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _walk(item, f"{path}[{i}]")
        _walk(payload)
        text = json.dumps(payload)
        for needle in forbidden_substrings:
            self.assertNotIn(needle, text,
                             f"forbidden substring {needle!r} leaked")

    def test_unmapped_smiles_payload_validates_against_live_schema(self):
        # End-to-end: unmapped_smiles on both a reactant and the TS
        # block lands cleanly through the live bundle validator.
        doc = _reaction_output_doc()
        target = next(s for s in doc["species"] if s["label"] == "CHO")
        target["smiles"] = "[CH:1]=[O:2]"
        target["unmapped_smiles"] = "[CH]=O"
        _, _, payload = self._submit(output_doc=doc)
        contract_validate(ComputedReactionUploadRequest, payload)
        # Sanity: confirm the field actually surfaces on the right
        # block, not just that the payload validates without it.
        cho = next(s for s in payload["species"]
                   if s["key"].startswith("r0"))
        self.assertEqual(cho["species_entry"]["unmapped_smiles"], "[CH]=O")

    def test_workflow_tool_release_still_identifies_arc(self):
        # Generalized provenance is fine: TCKDB should still see
        # ``ARC`` as the workflow tool (with version + git commit).
        # That's identification, not narrative.
        _, _, payload = self._submit()
        ts_opt = payload["transition_state"]["calculation"]
        wt = ts_opt["workflow_tool_release"]
        self.assertEqual(wt["name"], "ARC")
        self.assertIn("version", wt)

    def test_payload_validates_against_live_schema_after_workflow_cleanup(self):
        # End-to-end: removing the selected_ts_guess marker leaves the
        # payload structurally valid. NEB+heuristics+history-stuffed
        # input all converge to a clean payload.
        doc = _reaction_output_doc()
        ts = doc["transition_states"][0]
        ts["chosen_ts_method"] = "Heuristics"
        ts["successful_methods"] = ["AutoTST"]
        ts["ts_report"] = "narrative"
        _, _, payload = self._submit(output_doc=doc)
        contract_validate(ComputedReactionUploadRequest, payload)

    def test_dependency_survives_reaction_bundle_flatten(self):
        # ``_flatten_all_reaction_calcs`` pops opt_result into flat
        # fields; depends_on lives at the calc-object level (not nested
        # in opt_result), so the edge must be intact in the final
        # post-flatten payload.
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        self._add_coarse_to_species(doc, "CHO")
        _, _, payload = self._submit(output_doc=doc)
        # Sanity: flatten promoted r0_opt's converged-flag to the top level.
        r0 = next(s for s in payload["species"] if s["key"] == "r0_CHO")
        r0_primary = r0["conformers"][0]["calculation"]
        self.assertNotIn("opt_result", r0_primary)
        self.assertIn("opt_converged", r0_primary)
        # Both edges still present.
        self.assertEqual(
            r0_primary["depends_on"],
            [{"parent_calculation_key": "r0_opt_coarse", "role": "optimized_from"}],
        )
        ts_primary = payload["transition_state"]["calculation"]
        self.assertEqual(
            ts_primary["depends_on"],
            [{"parent_calculation_key": "ts_guess", "role": "optimized_from"}],
        )

    def test_neb_payload_validates_against_live_reaction_schema(self):
        # End-to-end: with both Part-A and Part-B edges in play, the
        # full payload still passes the live pydantic validator.
        doc = _reaction_output_doc()
        self._make_neb_ts(doc)
        self._add_coarse_to_species(doc, "CHO")
        _, _, payload = self._submit(output_doc=doc)
        contract_validate(ComputedReactionUploadRequest, payload)


class TestPathSearchResultBuilder(unittest.TestCase):
    """Direct unit tests for ``_build_path_search_result_payload``.

    Verifies the GSM-stringfile parser branch produces multi-point
    payloads, the NEB / unparseable branch falls back to a single
    TS-guess point, and the all-empty branch returns ``None`` so the
    caller can refuse to emit a path_search calc rather than violate
    the backend's ``points: min_length=1`` invariant.
    """

    def setUp(self):
        from tckdb_arc.adapter import _build_path_search_result_payload
        self.build = _build_path_search_result_payload

    def test_gsm_with_real_stringfile_parses_into_points(self):
        # Use the real fixture stringfile (known multi-frame XYZ).
        # Skip cleanly if the fixture env var is unset or absent.
        path = _GSM_STRINGFILE_FIXTURE
        if not path or not os.path.isfile(path):
            self.skipTest(_GSM_FIXTURE_SKIP_MSG)
        result = self.build(method='gsm', log_path=path,
                            fallback_xyz_text=None)
        self.assertIsNotNone(result)
        self.assertEqual(result['method'], 'gsm')
        self.assertGreaterEqual(len(result['points']), 2)
        # Indices unique and zero-based.
        indices = [p['point_index'] for p in result['points']]
        self.assertEqual(indices, sorted(set(indices)))
        self.assertEqual(indices[0], 0)
        # Each point carries a non-empty geometry.
        for p in result['points']:
            self.assertIn('geometry', p)
            self.assertIsInstance(p['geometry']['xyz_text'], str)
            self.assertGreater(len(p['geometry']['xyz_text']), 0)
        # selected_ts_point_index points to one of the points (the
        # middle-ish one, mirroring xtb_gsm.process_run's choice).
        selected = result.get('selected_ts_point_index')
        self.assertIsNotNone(selected)
        self.assertIn(selected, indices)
        ts_pts = [p for p in result['points'] if p.get('is_ts_guess')]
        self.assertEqual(len(ts_pts), 1)
        self.assertEqual(ts_pts[0]['point_index'], selected)

    def test_gsm_unparseable_log_falls_back_to_single_point(self):
        result = self.build(method='gsm',
                            log_path='/no/such/stringfile.xyz0000',
                            fallback_xyz_text='C 0 0 0\nH 1 0 0')
        self.assertIsNotNone(result)
        self.assertEqual(result['method'], 'gsm')
        self.assertEqual(len(result['points']), 1)
        self.assertEqual(result['points'][0]['point_index'], 0)
        self.assertTrue(result['points'][0]['is_ts_guess'])
        self.assertEqual(result['selected_ts_point_index'], 0)

    def test_neb_falls_back_to_single_point(self):
        # NEB path: no log parser yet → single-point fallback from the
        # chosen guess's geometry.
        result = self.build(method='neb',
                            log_path='/no/such/neb/input.log',
                            fallback_xyz_text='C 0 0 0\nH 1 0 0')
        self.assertIsNotNone(result)
        self.assertEqual(result['method'], 'neb')
        self.assertEqual(len(result['points']), 1)
        self.assertTrue(result['points'][0]['is_ts_guess'])

    def test_no_log_no_fallback_returns_none(self):
        # Conservative: no parseable log AND no fallback xyz → None,
        # caller refuses to emit the calc rather than fake provenance.
        result = self.build(method='gsm', log_path=None,
                            fallback_xyz_text=None)
        self.assertIsNone(result)
        result = self.build(method='neb', log_path=None,
                            fallback_xyz_text='')
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Reliable static metadata (audit-driven; see audit notes in
    # adapter.py near _PATH_SEARCH_METHOD_PROPERTIES).
    # ------------------------------------------------------------------

    def test_gsm_result_carries_reliable_metadata(self):
        # GSM with parsed stringfile from the real fixture:
        # is_double_ended and source_endpoint_count are set from
        # method-static knowledge. ``converged`` is not: ARC records no
        # convergence verdict (TSGuess.success means the output file
        # exists), so the optional field is omitted.
        path = _GSM_STRINGFILE_FIXTURE
        if not path or not os.path.isfile(path):
            # Fall back to the single-point branch via a synthetic xyz
            # so the metadata still gets exercised when the fixture is
            # absent.
            result = self.build(method='gsm', log_path=None,
                                fallback_xyz_text='C 0 0 0\nH 1 0 0')
        else:
            result = self.build(method='gsm', log_path=path,
                                fallback_xyz_text=None)
        self.assertNotIn('converged', result)
        self.assertTrue(result['is_double_ended'])
        self.assertEqual(result['source_endpoint_count'], 2)

    def test_neb_fallback_carries_reliable_metadata(self):
        # NEB single-point fallback: same reliable metadata applies —
        # both ARC-supported path-search methods are double-ended with
        # two endpoints.
        result = self.build(method='neb',
                            log_path='/no/such/neb/input.log',
                            fallback_xyz_text='C 0 0 0\nH 1 0 0')
        self.assertNotIn('converged', result)
        self.assertTrue(result['is_double_ended'])
        self.assertEqual(result['source_endpoint_count'], 2)

    def test_gsm_points_have_cumulative_arc_length_path_coordinate(self):
        # path_coordinate is the physical cumulative Cartesian arc length
        # (Angstrom) along the string — the analog of ORCA NEB's Dist.(Ang.)
        # that the backend's path_coordinate column stores. It starts at 0.0
        # at node 0 and increases monotonically (NOT a normalized 0-1 fraction),
        # so its total is a real distance rather than always 1.0.
        path = _GSM_STRINGFILE_FIXTURE
        if not path or not os.path.isfile(path):
            self.skipTest(_GSM_FIXTURE_SKIP_MSG)
        result = self.build(method='gsm', log_path=path,
                            fallback_xyz_text=None)
        n = len(result['points'])
        self.assertGreaterEqual(n, 2)
        coords = [p['path_coordinate'] for p in result['points']]
        for c in coords:
            self.assertIsInstance(c, float)
        self.assertAlmostEqual(coords[0], 0.0)
        for prev, cur in zip(coords, coords[1:]):
            self.assertGreaterEqual(cur, prev)  # monotonically non-decreasing
        self.assertGreater(coords[-1], 0.0)  # non-trivial total arc length

    def test_gsm_points_omit_energy_and_force_fields(self):
        # No on-disk source for per-node energies/forces in xtb_gsm
        # output (audit confirmed); the adapter must not invent them.
        # The schema treats these as Optional with default None — we
        # rely on omission so the server stores NULL.
        path = _GSM_STRINGFILE_FIXTURE
        if not path or not os.path.isfile(path):
            self.skipTest(_GSM_FIXTURE_SKIP_MSG)
        result = self.build(method='gsm', log_path=path,
                            fallback_xyz_text=None)
        forbidden = (
            'electronic_energy_hartree',
            'relative_energy_kj_mol',
            'max_force', 'rms_force',
            'max_gradient', 'rms_gradient',
        )
        for p in result['points']:
            for field in forbidden:
                self.assertNotIn(field, p,
                                 msg=f'{field} unexpectedly populated on {p}')


class TestXtbTurbomoleFileParsers(unittest.TestCase):
    """Parsers for the xTB-generated, Turbomole-format ``energy`` /
    ``gradient`` files that ``xtb --grad`` writes per node and the
    patched ``ograd`` wrapper preserves into ``gsm_node_outputs/``.

    Provenance is xTB; ``Turbomole`` here names the on-disk file
    layout xTB uses with ``--grad``, not the software that ran. The
    TCKDB ``software_release`` for the resulting calculation stays
    xTB / xTB-GSM.
    """

    def setUp(self):
        from tckdb_arc.adapter import (
            _parse_xtb_turbomole_energy_file,
            _parse_xtb_turbomole_gradient_file,
        )
        self.parse_energy = _parse_xtb_turbomole_energy_file
        self.parse_gradient = _parse_xtb_turbomole_gradient_file
        self.tmp = tempfile.mkdtemp(prefix='tm-parsers-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, name, body):
        p = os.path.join(self.tmp, name)
        with open(p, 'w') as f:
            f.write(body)
        return p

    def test_energy_file_extracts_hartree(self):
        path = self._write('energy', (
            '$energy      SCF              SCFKIN            SCFPOT\n'
            '     1   -28.123456789012   -28.5  0.0\n'
            '$end\n'
        ))
        self.assertAlmostEqual(self.parse_energy(path), -28.123456789012)

    def test_energy_file_missing_returns_none(self):
        self.assertIsNone(self.parse_energy('/no/such/file/energy'))

    def test_energy_file_malformed_returns_none(self):
        path = self._write('energy_bad', 'totally not turbomole\n')
        self.assertIsNone(self.parse_energy(path))

    def test_gradient_file_extracts_max_and_rms(self):
        # 3 atoms; gradient components such that max=0.4, rms=sqrt(mean
        # of squared components)= sqrt((0.01+0.04+0.09+0.16+0.04+0.01+
        # 0.0004+0.0009+0.0016)/9). xTB-generated Turbomole-format
        # gradient: 2 header lines ($grad + cycle line), then N coord
        # lines, then N grad lines.
        body = (
            '$grad          cartesian gradients\n'
            '  cycle =      1   SCF energy = -28.0   |dE/dxyz| = 0.123\n'
            '   0.0   0.0   0.0     C\n'
            '   1.0   0.0   0.0     H\n'
            '   0.0   1.0   0.0     H\n'
            '   0.1   0.2   0.3\n'
            '   0.4   0.2   0.1\n'
            '   0.02  0.03  0.04\n'
            '$end\n'
        )
        path = self._write('gradient', body)
        max_g, rms_g = self.parse_gradient(path)
        self.assertAlmostEqual(max_g, 0.4)
        # 9 components total
        import math as _math
        comps = [0.1, 0.2, 0.3, 0.4, 0.2, 0.1, 0.02, 0.03, 0.04]
        expected_rms = _math.sqrt(sum(c * c for c in comps) / 9)
        self.assertAlmostEqual(rms_g, expected_rms)

    def test_gradient_file_handles_d_exponent(self):
        # xTB's Turbomole-format gradient sometimes writes 1.234D-04
        # instead of 1.234E-04; tm2orca.py already does the same
        # replace, so the parser must match its convention.
        body = (
            '$grad          cartesian gradients\n'
            '  cycle =      1\n'
            '   0.0   0.0   0.0     C\n'
            '   1.234D-04   0.0   0.0\n'
            '$end\n'
        )
        path = self._write('gradient_d', body)
        max_g, _ = self.parse_gradient(path)
        self.assertAlmostEqual(max_g, 1.234e-4)

    def test_gradient_file_missing_returns_none_pair(self):
        self.assertEqual(self.parse_gradient('/no/such/gradient'),
                         (None, None))


class TestParseXtbOutEnergy(unittest.TestCase):
    """``_parse_xtb_xtbout_energy`` recovers the total energy (Hartree)
    from an xTB stdout (``.xtbout``) file. This is the reliable per-node
    energy source for xTB-GSM: the patched ``ograd`` copies ``.xtbout``
    unconditionally, whereas the cleaner Turbomole ``.energy`` file depends
    on a ``tm2orca.py`` rename that frequently does not land, so
    ``.xtbout`` is often the only surviving record of a node's energy.
    """

    def setUp(self):
        from tckdb_arc.adapter import _parse_xtb_xtbout_energy
        self.parse = _parse_xtb_xtbout_energy
        self.tmp = tempfile.mkdtemp(prefix='xtbout-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, name, body):
        p = os.path.join(self.tmp, name)
        with open(p, 'w') as f:
            f.write(body)
        return p

    # A trimmed-but-faithful xTB ``--grad`` tail: xTB prints the total
    # energy twice, in two differently-framed summary blocks, with the
    # same value (matching real reaction_06 ``.xtbout`` files).
    _REAL_TAIL = (
        " :::::::::::::::::::::::::::::::::::::::::::::::::::::::\n"
        " ::                     SUMMARY                     ::\n"
        " :::::::::::::::::::::::::::::::::::::::::::::::::::::::\n"
        " :: total energy              -9.441927748544 Eh    ::\n"
        " :: gradient norm              0.012597676605 Eh/a0 ::\n"
        " :: HOMO-LUMO gap              4.123456789012 eV     ::\n"
        " :::::::::::::::::::::::::::::::::::::::::::::::::::::::\n"
        "           ------------------------------------------------- \n"
        "          | TOTAL ENERGY               -9.441927748544 Eh   |\n"
        "          | GRADIENT NORM               0.012597676605 Eh/α |\n"
        "           ------------------------------------------------- \n"
    )

    def test_parses_total_energy_hartree(self):
        path = self._write('0000.01.xtbout', self._REAL_TAIL)
        self.assertAlmostEqual(self.parse(path), -9.441927748544)

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.parse(os.path.join(self.tmp, 'nope.xtbout')))

    def test_garbled_file_returns_none(self):
        path = self._write('bad.xtbout',
                           'xtb aborted: SCF did not converge\nno energy here\n')
        self.assertIsNone(self.parse(path))

    def test_case_insensitive_lowercase_only_block(self):
        # Some xTB builds/verbosities emit only the ':: total energy'
        # block, not the boxed 'TOTAL ENERGY' banner.
        body = " :: total energy              -12.500000000000 Eh    ::\n"
        path = self._write('c.xtbout', body)
        self.assertAlmostEqual(self.parse(path), -12.5)

    def test_takes_last_match(self):
        # A stray earlier 'total energy ... Eh' line must not shadow the
        # converged value in the final summary.
        body = (" :: total energy              -1.000000000000 Eh    ::\n"
                " ... more iterations ...\n"
                " :: total energy              -9.999999999999 Eh    ::\n")
        path = self._write('m.xtbout', body)
        self.assertAlmostEqual(self.parse(path), -9.999999999999)

    def test_ignores_total_energy_without_eh_unit(self):
        # A 'total energy' figure not tagged 'Eh' is not a Hartree and
        # must not be misread.
        body = " total energy reported as 5.0 kcal/mol elsewhere\n"
        path = self._write('u.xtbout', body)
        self.assertIsNone(self.parse(path))

    def test_non_utf8_bytes_do_not_crash(self):
        # Real xTB banners carry non-ASCII glyphs (e.g. 'Eh/α'); a file
        # with bytes that aren't valid UTF-8 must degrade to a parsed
        # value (via errors='replace'), never raise UnicodeDecodeError
        # and abort the bundle build.
        p = os.path.join(self.tmp, 'latin.xtbout')
        with open(p, 'wb') as f:
            f.write(b' :: total energy              -7.250000000000 Eh    ::\n')
            f.write(b'          | GRADIENT NORM   0.01 Eh/\xe1 |\n')  # stray byte
        self.assertAlmostEqual(self.parse(p), -7.25)


class TestReadGsmNodeOutputs(unittest.TestCase):
    """Walks ``gsm_node_outputs/`` and returns parsed per-node dicts."""

    def setUp(self):
        from tckdb_arc.adapter import _read_gsm_node_outputs
        self.read = _read_gsm_node_outputs
        self.tmp = tempfile.mkdtemp(prefix='gsm-node-out-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, name, body):
        with open(os.path.join(self.tmp, name), 'w') as f:
            f.write(body)

    def _energy_body(self, e_h):
        return (
            '$energy      SCF              SCFKIN            SCFPOT\n'
            f'     1   {e_h:.12f}   0.0  0.0\n'
            '$end\n'
        )

    def _gradient_body(self, components):
        # 1 atom, 3 components. xTB-generated Turbomole-format
        # gradient: 2 header lines ($grad + cycle line).
        coord = '   0.0   0.0   0.0     C\n'
        grad = '   ' + '   '.join(f'{c}' for c in components) + '\n'
        return ('$grad          cartesian gradients\n'
                '  cycle =      1\n'
                f'{coord}{grad}$end\n')

    def test_returns_empty_for_missing_dir(self):
        self.assertEqual(self.read('/no/such/dir'), {})

    def test_returns_empty_for_empty_dir(self):
        self.assertEqual(self.read(self.tmp), {})

    def test_collects_energies_keyed_by_node_label(self):
        self._write('0000.01.energy', self._energy_body(-28.1))
        self._write('0000.02.energy', self._energy_body(-28.2))
        self._write('0000.03.energy', self._energy_body(-28.05))
        out = self.read(self.tmp)
        self.assertEqual(set(out.keys()), {1, 2, 3})
        self.assertAlmostEqual(out[1]['electronic_energy_hartree'], -28.1)
        self.assertAlmostEqual(out[3]['electronic_energy_hartree'], -28.05)
        # No gradient files → gradient fields absent.
        self.assertNotIn('max_gradient', out[1])

    def test_attaches_gradient_metrics_when_present(self):
        self._write('0000.05.energy', self._energy_body(-28.0))
        self._write('0000.05.gradient', self._gradient_body([0.1, -0.2, 0.05]))
        out = self.read(self.tmp)
        self.assertIn(5, out)
        self.assertAlmostEqual(out[5]['max_gradient'], 0.2)

    def test_skips_node_when_energy_unparseable(self):
        # No energy → node is omitted entirely (gradient alone isn't
        # enough to anchor a node — energy is the primary parse).
        self._write('0000.07.energy', 'garbage\n')
        self._write('0000.07.gradient', self._gradient_body([0.3, 0, 0]))
        out = self.read(self.tmp)
        self.assertNotIn(7, out)

    def _xtbout_body(self, e_h):
        return (
            f' :: total energy              {e_h:.12f} Eh    ::\n'
            f'          | TOTAL ENERGY               {e_h:.12f} Eh   |\n'
        )

    def test_reads_energy_from_xtbout_when_no_energy_file(self):
        # The real xTB-GSM shape: ograd copied only ``.xtbout`` (the
        # ``.energy`` rename didn't land). Energies must still be
        # recovered from the xTB stdout.
        self._write('0000.01.xtbout', self._xtbout_body(-9.44))
        self._write('0000.02.xtbout', self._xtbout_body(-9.02))
        out = self.read(self.tmp)
        self.assertEqual(set(out.keys()), {1, 2})
        self.assertAlmostEqual(out[1]['electronic_energy_hartree'], -9.44)
        self.assertAlmostEqual(out[2]['electronic_energy_hartree'], -9.02)
        # xtbout branch carries energy only, no gradient metrics.
        self.assertNotIn('max_gradient', out[1])

    def test_energy_file_preferred_over_xtbout(self):
        # When both exist for a node, the cleaner ``.energy`` wins (it can
        # also carry gradient metrics the ``.xtbout`` branch does not).
        self._write('0000.03.energy', self._energy_body(-28.05))
        self._write('0000.03.xtbout', self._xtbout_body(-99.9))
        out = self.read(self.tmp)
        self.assertAlmostEqual(out[3]['electronic_energy_hartree'], -28.05)

    def test_mixed_energy_and_xtbout_nodes(self):
        # A node with only ``.energy`` and a node with only ``.xtbout``
        # are both recovered.
        self._write('0000.01.energy', self._energy_body(-28.1))
        self._write('0000.02.xtbout', self._xtbout_body(-28.2))
        out = self.read(self.tmp)
        self.assertEqual(set(out.keys()), {1, 2})
        self.assertAlmostEqual(out[1]['electronic_energy_hartree'], -28.1)
        self.assertAlmostEqual(out[2]['electronic_energy_hartree'], -28.2)

    def test_garbled_xtbout_omitted(self):
        self._write('0000.09.xtbout', 'no energy printed here\n')
        out = self.read(self.tmp)
        self.assertNotIn(9, out)


class TestPathSearchPointsWithNodeMetadata(unittest.TestCase):
    """Raw GSM paths retain geometry without inventing invocation/frame matches."""

    def setUp(self):
        # arc-gated: these exercise ARC's real GSM stringfile parser
        # (arc.parser.parser.parse_trajectory / parse_gsm_stringfile_energies).
        pytest.importorskip("arc")
        from tckdb_arc.adapter import _build_path_search_result_payload
        self.build = _build_path_search_result_payload
        self.tmp = tempfile.mkdtemp(prefix='gsm-pts-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _seed_node_outputs(self, energies_by_label):
        """Create energy files for each (label, e_h) pair under
        ``self.tmp/gsm_node_outputs/``. Returns the dir path."""
        d = os.path.join(self.tmp, 'gsm_node_outputs')
        os.makedirs(d, exist_ok=True)
        for label, e_h in energies_by_label.items():
            with open(os.path.join(d, f'0000.{label:02d}.energy'), 'w') as f:
                f.write(
                    '$energy      SCF              SCFKIN            SCFPOT\n'
                    f'     1   {e_h:.12f}   0.0  0.0\n'
                    '$end\n'
                )
        return d

    def _stringfile_for(self, n_frames):
        # Synthesize a minimal multi-frame XYZ. The comment line must not
        # be a bare digit — parse_trajectory uses single-digit detection
        # to identify atom-count headers, so a numeric comment is
        # misread as the start of a new frame.
        # Two atoms with a stretching C-H bond (0.1 A / frame): a real internal
        # geometry change so the Kabsch-aligned inter-node distance (the arc
        # length) is non-zero — a single atom would be pure translation, which
        # alignment collapses to 0.
        body = ''
        for i in range(n_frames):
            body += f'2\nframe {i}\nC 0.0 0.0 0.0\nH 0.0 0.0 {1.0 + i*0.1}\n'
        p = os.path.join(self.tmp, 'stringfile.xyz0000')
        with open(p, 'w') as f:
            f.write(body)
        return p

    def test_full_energies_without_geometry_matches_are_omitted(self):
        sf = self._stringfile_for(n_frames=4)
        outputs_dir = self._seed_node_outputs({1: -28.5, 2: -28.9})
        result = self.build(method='gsm', log_path=sf, fallback_xyz_text=None,
                            node_outputs_dir=outputs_dir)
        self.assertEqual(len(result['points']), 4)
        self.assertNotIn('zero_energy_reference_hartree', result)
        for point in result['points']:
            for key in ('electronic_energy_hartree', 'relative_energy_kj_mol', 'max_gradient', 'rms_gradient'):
                self.assertNotIn(key, point)

    def test_partial_energies_without_geometry_matches_are_omitted(self):
        sf = self._stringfile_for(n_frames=4)
        outputs_dir = self._seed_node_outputs({1: -28.5, 2: -28.9})
        result = self.build(method='gsm', log_path=sf, fallback_xyz_text=None,
                            node_outputs_dir=outputs_dir)
        self.assertEqual(len(result['points']), 4)
        self.assertNotIn('zero_energy_reference_hartree', result)
        for point in result['points']:
            for key in ('electronic_energy_hartree', 'relative_energy_kj_mol', 'max_gradient', 'rms_gradient'):
                self.assertNotIn(key, point)

    def _seed_xtbout_node_outputs(self, energies_by_label):
        """Create ``.xtbout`` (xTB-stdout) files for each (label, e_h)
        pair under ``self.tmp/gsm_node_outputs/`` — the real xTB-GSM
        on-disk shape where only ``.xtbout`` survived. Returns the dir."""
        d = os.path.join(self.tmp, 'gsm_node_outputs')
        os.makedirs(d, exist_ok=True)
        for label, e_h in energies_by_label.items():
            with open(os.path.join(d, f'0000.{label:02d}.xtbout'), 'w') as f:
                f.write(
                    f' :: total energy              {e_h:.12f} Eh    ::\n'
                    f'          | TOTAL ENERGY               {e_h:.12f} Eh   |\n'
                )
        return d

    def test_real_shape_xtbout_only_orphan_endpoint_frame(self):
        sf = self._stringfile_for(n_frames=4)
        outputs_dir = self._seed_xtbout_node_outputs({1: -28.5, 2: -28.9})
        result = self.build(method='gsm', log_path=sf, fallback_xyz_text=None,
                            node_outputs_dir=outputs_dir)
        self.assertEqual(len(result['points']), 4)
        self.assertNotIn('zero_energy_reference_hartree', result)
        for point in result['points']:
            for key in ('electronic_energy_hartree', 'relative_energy_kj_mol', 'max_gradient', 'rms_gradient'):
                self.assertNotIn(key, point)

    def test_no_node_outputs_dir_keeps_geometry_only_behavior(self):
        # Backwards compat: existing geometry-only fixtures (no
        # gsm_node_outputs/ alongside the stringfile) still work.
        sf = self._stringfile_for(n_frames=4)
        result = self.build(method='gsm', log_path=sf,
                            fallback_xyz_text=None,
                            node_outputs_dir=None)
        self.assertIsNotNone(result)
        for p in result['points']:
            self.assertNotIn('electronic_energy_hartree', p)
            self.assertNotIn('relative_energy_kj_mol', p)
            self.assertNotIn('max_gradient', p)

    def _energetic_stringfile(self, rel_energies_kcal):
        # A molecularGSM-style stringfile whose comment line holds each
        # node's relative energy (kcal/mol, first node 0).
        p = os.path.join(self.tmp, 'stringfile.xyz0000')
        with open(p, 'w') as f:
            for e in rel_energies_kcal:
                f.write(f' 1\n {e:.6f}\n C 0.0 0.0 0.0\n')
        return p

    def test_points_carry_cumulative_arc_length_path_coordinate(self):
        # path_coordinate is the Kabsch-aligned cumulative arc length (Angstrom),
        # independent of any energy source. The synthetic frames stretch a C-H
        # bond 0.1 A/frame (a real internal change alignment can't remove), so
        # the aligned inter-node distance is a constant positive step and
        # path_coordinate increases monotonically from 0.0.
        sf = self._stringfile_for(n_frames=5)
        result = self.build(method='gsm', log_path=sf,
                            fallback_xyz_text=None)
        coords = [p['path_coordinate'] for p in result['points']]
        self.assertAlmostEqual(coords[0], 0.0)
        for prev, cur in zip(coords, coords[1:]):
            self.assertGreater(cur, prev)  # strictly increasing arc length
        steps = [b - a for a, b in zip(coords, coords[1:])]
        self.assertTrue(all(abs(s - steps[0]) < 1e-6 for s in steps))  # even spacing

    def test_stringfile_relative_energies_populate_when_node_outputs_absent(self):
        # No node outputs → fall back to the stringfile comment column
        # (relative kcal/mol). Every node gets relative_energy_kj_mol
        # (converted, first node 0) and absolute-Hartree fields stay null.
        # No node is flagged is_climbing_image: the contract defines it
        # for NEB-CI images, and a string's peak is not evidence of one.
        rel_kcal = [0.0, 3.0, 12.5, 7.0, 1.0]
        sf = self._energetic_stringfile(rel_kcal)
        result = self.build(method='gsm', log_path=sf,
                            fallback_xyz_text=None,
                            node_outputs_dir=None)
        self.assertNotIn('zero_energy_reference_hartree', result)
        base = min(rel_kcal)
        for p, e in zip(result['points'], rel_kcal):
            self.assertAlmostEqual(p['relative_energy_kj_mol'],
                                   (e - base) * 4.184)
            self.assertNotIn('electronic_energy_hartree', p)
        # First node is the relative-zero.
        self.assertAlmostEqual(result['points'][0]['relative_energy_kj_mol'], 0.0)
        self.assertFalse(any('is_climbing_image' in p for p in result['points']))

    def test_all_zero_stringfile_is_sentinel_no_relative_energies(self):
        # ARC's molecularGSM build writes 0.000000 for every comment
        # line; that all-zero column is the "no energy emitted" sentinel,
        # not a real flat profile — relative energies stay null.
        sf = self._energetic_stringfile([0.0, 0.0, 0.0, 0.0])
        result = self.build(method='gsm', log_path=sf,
                            fallback_xyz_text=None,
                            node_outputs_dir=None)
        for p in result['points']:
            self.assertNotIn('relative_energy_kj_mol', p)
            self.assertNotIn('is_climbing_image', p)

    def test_node_output_hartrees_take_precedence_over_stringfile(self):
        pytest.importorskip("arc")  # arc-gated: needs ARC on PYTHONPATH
        # Raw invocation ids cannot identify frames. Stringfile relative
        # energies remain valid even when archived absolute values exist.
        rel_kcal = [0.0, 5.0, 20.0, 2.0]
        sf = self._energetic_stringfile(rel_kcal)
        from arc.parser.parser import parse_trajectory
        n_frames = len(parse_trajectory(sf))
        energies = {i: -28.0 - 0.01 * (i if i != 2 else 9) for i in range(n_frames)}
        outputs_dir = self._seed_node_outputs(energies)
        result = self.build(method='gsm', log_path=sf,
                            fallback_xyz_text=None,
                            node_outputs_dir=outputs_dir)
        self.assertNotIn('zero_energy_reference_hartree', result)
        for point, energy in zip(result['points'], rel_kcal):
            self.assertNotIn('electronic_energy_hartree', point)
            self.assertAlmostEqual(point['relative_energy_kj_mol'], energy * 4.184)
        self.assertFalse(any('is_climbing_image' in point for point in result['points']))


class TestArtifactFilenameCoercion(unittest.TestCase):
    """``_coerce_artifact_filename`` adapts non-conforming output_log
    filenames so the backend's KIND_ALLOWED_EXTENSIONS allowlist passes.
    """

    def setUp(self):
        from tckdb_arc.adapter import _coerce_artifact_filename
        self.coerce = _coerce_artifact_filename

    def test_log_extension_passes_through(self):
        self.assertEqual(self.coerce('input.log', 'output_log'), 'input.log')
        self.assertEqual(self.coerce('output.out', 'output_log'), 'output.out')
        self.assertEqual(self.coerce('orca.orca', 'output_log'), 'orca.orca')

    def test_extension_check_is_case_insensitive(self):
        self.assertEqual(self.coerce('INPUT.LOG', 'output_log'), 'INPUT.LOG')

    def test_gsm_stringfile_gets_log_suffix(self):
        # Backend rejects '.xyz0000' for output_log; the coercion
        # appends '.log' so the original basename is preserved as the
        # prefix and the file passes the allowlist.
        self.assertEqual(self.coerce('stringfile.xyz0000', 'output_log'),
                         'stringfile.xyz0000.log')

    def test_other_kinds_pass_through_unchanged(self):
        # Coercion is scoped to output_log only — input/checkpoint
        # have their own allowlists upstream.
        self.assertEqual(self.coerce('input.gjf', 'input'), 'input.gjf')
        self.assertEqual(self.coerce('check.chk', 'checkpoint'), 'check.chk')
        self.assertEqual(self.coerce('weird.xyz', 'input'), 'weird.xyz')


class TestTsGuessPathSearchGate(unittest.TestCase):
    """Direct unit tests for ``_resolve_ts_guess_path_search``."""

    def setUp(self):
        from tckdb_arc.adapter import _resolve_ts_guess_path_search
        self.resolve = _resolve_ts_guess_path_search

    def test_orca_neb_resolves_to_neb(self):
        self.assertEqual(self.resolve("orca_neb"), "neb")

    def test_xtb_gsm_underscore_resolves_to_gsm(self):
        self.assertEqual(self.resolve("xtb_gsm"), "gsm")

    def test_xtb_gsm_dash_form_resolves_to_gsm(self):
        self.assertEqual(self.resolve("xTB-GSM"), "gsm")
        self.assertEqual(self.resolve("xtb-gsm"), "gsm")

    def test_match_is_case_and_whitespace_tolerant(self):
        self.assertEqual(self.resolve("ORCA_NEB"), "neb")
        self.assertEqual(self.resolve("  orca_neb  "), "neb")
        self.assertEqual(self.resolve("XTB_GSM"), "gsm")

    def test_geometry_only_methods_reject(self):
        for m in ("Heuristics", "AutoTST", "KinBot", "GCN",
                  "user guess 0", "user guess 1"):
            self.assertIsNone(self.resolve(m), msg=f"unexpected match: {m}")

    def test_non_string_inputs_reject(self):
        self.assertIsNone(self.resolve(None))
        self.assertIsNone(self.resolve(42))
        self.assertIsNone(self.resolve({"method": "orca_neb"}))


class TestComputedReactionPartial(unittest.TestCase):
    """Phase-1 partial computed-reaction sidecars.

    A partial bundle is what the producer writes when reactants/products
    converged but the TS search did not. Phase-1 policy: write to disk,
    mark ``is_partial=true``, name the file with a ``.partial.`` infix,
    drop ``transition_state`` and ``kinetics`` from the payload, and
    never live-POST regardless of ``upload``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-rxn-partial-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="proj-A",
            upload_mode="computed_reaction",
            upload=True,
        )

    def _submit_partial(self, *, doc=None, reaction=None, client=None):
        client = client or _StubClient(response=_StubResponse({"reaction_id": 99}))
        adapter = TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)
        # The sweep is responsible for stripping ts_label + kinetics
        # before calling the adapter; the adapter test mirrors what the
        # sweep would pass.
        rxn = reaction if reaction is not None else _reaction_record(with_kinetics=True)
        rxn = dict(rxn)
        rxn["ts_label"] = None
        rxn["kinetics"] = None
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc or _reaction_output_doc(),
                reaction_record=rxn,
                is_partial=True,
            )
        return outcome, client

    def test_partial_outcome_status_is_skipped(self):
        # Phase-1 reuses the "skipped" status — the sidecar's
        # is_partial=true is the load-bearing disambiguator.
        outcome, _ = self._submit_partial()
        self.assertEqual(outcome.status, "skipped")

    def test_partial_does_not_call_client(self):
        outcome, client = self._submit_partial()
        self.assertEqual(client.calls, [])
        self.assertIsNotNone(outcome)

    def test_partial_filename_has_partial_infix(self):
        outcome, _ = self._submit_partial()
        self.assertTrue(outcome.payload_path.name.endswith(".partial.payload.json"))
        self.assertTrue(outcome.sidecar_path.name.endswith(".partial.meta.json"))

    def test_partial_sidecar_metadata_marks_is_partial(self):
        outcome, _ = self._submit_partial()
        with open(outcome.sidecar_path) as fh:
            sc = json.load(fh)
        self.assertTrue(sc["is_partial"])
        self.assertEqual(sc["status"], "skipped")
        self.assertEqual(sc["payload_kind"], COMPUTED_REACTION_KIND)

    def test_partial_payload_omits_transition_state_and_kinetics(self):
        outcome, _ = self._submit_partial()
        payload = json.loads(outcome.payload_path.read_text())
        self.assertNotIn("transition_state", payload)
        self.assertNotIn("kinetics", payload)
        # Reactants and products survive — that's the whole point.
        self.assertEqual(payload["reactant_keys"], ["r0_CHO", "r1_CH4"])
        self.assertEqual(payload["product_keys"], ["p0_CH2O", "p1_CH3"])

    def test_partial_idempotency_uses_noTS_slot(self):
        # The reaction's idempotency key carries "noTS" in the conformer
        # slot when ts_label is null, so a complete rerun (with TS) and
        # the prior partial don't collide on the server side.
        outcome, _ = self._submit_partial()
        self.assertIn(":noTS:", outcome.idempotency_key)

    def test_complete_reaction_unaffected(self):
        # Smoke check: when is_partial is left at its default False,
        # the reaction submit still POSTs and produces a normal sidecar
        # with no .partial. infix.
        client = _StubClient(response=_StubResponse({"reaction_id": 7}))
        adapter = TCKDBAdapter(self.cfg, client_factory=lambda c, k: client)
        # Mark TS converged so the sweep wouldn't have stripped it; the
        # adapter is invoked the same way regardless.
        doc = _reaction_output_doc()
        for ts in doc["transition_states"]:
            ts["converged"] = True
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=_reaction_record(),
            )
        self.assertEqual(outcome.status, "uploaded")
        self.assertNotIn(".partial.", outcome.payload_path.name)
        with open(outcome.sidecar_path) as fh:
            sc = json.load(fh)
        self.assertFalse(sc["is_partial"])


class TestReactionSweepPartialGating(unittest.TestCase):
    """``_run_reaction_sweep`` partial gating: TS-converged check + flag."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-tckdb-sweep-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _doc_with_unconverged_ts(self):
        doc = _reaction_output_doc()
        for ts in doc["transition_states"]:
            ts["converged"] = False
        for s in doc["species"]:
            s["converged"] = True
        return doc

    def _doc_with_converged_ts(self):
        doc = _reaction_output_doc()
        for ts in doc["transition_states"]:
            ts["converged"] = True
        for s in doc["species"]:
            s["converged"] = True
        return doc

    class _RecordingAdapter:
        """Adapter stand-in that records what the sweep called."""

        def __init__(self):
            self.calls: list[dict] = []

        def submit_computed_reaction_from_output(
            self, *, output_doc, reaction_record, is_partial=False,
        ):
            self.calls.append({
                "ts_label": reaction_record.get("ts_label"),
                "kinetics": reaction_record.get("kinetics"),
                "is_partial": is_partial,
                "label": reaction_record.get("label"),
            })
            return None

    def _run(self, *, doc, allow_partial):
        from tckdb_arc.sweep import _run_reaction_sweep
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            upload_mode="computed_reaction",
            allow_partial_uploads=allow_partial,
        )
        adapter = self._RecordingAdapter()
        _run_reaction_sweep(adapter=adapter, output_doc=doc, tckdb_config=cfg)
        return adapter

    def test_partial_disabled_skips_unconverged_ts_reactions(self):
        adapter = self._run(
            doc=self._doc_with_unconverged_ts(), allow_partial=False,
        )
        # The sweep must not call the adapter at all for the partial
        # reaction — silent server-side validation failures are exactly
        # what the gate is preventing.
        self.assertEqual(adapter.calls, [])

    def test_partial_enabled_strips_ts_and_kinetics(self):
        adapter = self._run(
            doc=self._doc_with_unconverged_ts(), allow_partial=True,
        )
        self.assertEqual(len(adapter.calls), 1)
        call = adapter.calls[0]
        self.assertTrue(call["is_partial"])
        self.assertIsNone(call["ts_label"])
        self.assertIsNone(call["kinetics"])

    def test_missing_ts_label_is_partial(self):
        doc = self._doc_with_converged_ts()
        doc["reactions"][0]["ts_label"] = None

        adapter = self._run(doc=doc, allow_partial=True)

        self.assertEqual(len(adapter.calls), 1)
        call = adapter.calls[0]
        self.assertTrue(call["is_partial"])
        self.assertIsNone(call["ts_label"])
        self.assertIsNone(call["kinetics"])

    def test_partial_enabled_does_not_mutate_input_record(self):
        # Other consumers of output_doc must keep seeing the real
        # ts_label/kinetics — the sweep deepcopies before stripping.
        doc = self._doc_with_unconverged_ts()
        original_ts = doc["reactions"][0]["ts_label"]
        original_kin = doc["reactions"][0]["kinetics"]
        self._run(doc=doc, allow_partial=True)
        self.assertEqual(doc["reactions"][0]["ts_label"], original_ts)
        self.assertEqual(doc["reactions"][0]["kinetics"], original_kin)

    def test_complete_reaction_calls_adapter_normally(self):
        adapter = self._run(
            doc=self._doc_with_converged_ts(), allow_partial=True,
        )
        self.assertEqual(len(adapter.calls), 1)
        call = adapter.calls[0]
        self.assertFalse(call["is_partial"])
        self.assertEqual(call["ts_label"], "TS0")
        # Kinetics survive a complete run untouched.
        self.assertIsNotNone(call["kinetics"])

    def test_missing_reactant_label_is_not_treated_as_partial(self):
        # Identity-malformed reactions (missing reactant species record)
        # belong on the existing failure path, not the partial path.
        # The sweep should still hand them to the adapter; the adapter
        # builder raises ValueError, which the sweep catches as a
        # failure. This test asserts the sweep does not silently turn
        # a malformed reaction into a partial one.
        from tckdb_arc.sweep import _run_reaction_sweep

        class _RaisingAdapter:
            def __init__(self):
                self.calls = 0

            def submit_computed_reaction_from_output(
                self, *, output_doc, reaction_record, is_partial=False,
            ):
                self.calls += 1
                self.last_is_partial = is_partial
                raise ValueError("missing reactant")

        doc = self._doc_with_converged_ts()
        # Drop one reactant species entirely — identity is malformed.
        doc["species"] = [s for s in doc["species"] if s["label"] != "CH4"]

        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://x",
            payload_dir=self.tmp,
            upload_mode="computed_reaction",
            allow_partial_uploads=True,
        )
        adapter = _RaisingAdapter()
        _run_reaction_sweep(adapter=adapter, output_doc=doc, tckdb_config=cfg)
        self.assertEqual(adapter.calls, 1)
        self.assertFalse(adapter.last_is_partial)


class TestPhase3EvidenceParity(unittest.TestCase):
    """Sidecar-normalized and legacy-parser-normalized results stay identical."""

    @staticmethod
    def _canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def test_species_and_ts_hessian_sidecar_matches_fallback(self):
        from tckdb_arc._vendor import xyz_to_str
        from tckdb_arc.adapter import _normalize_xyz_text
        frame_xyz = {"symbols": ("H", "H"), "coords": ((0., 0., 0.), (0., 0., .7))}
        geometry = _normalize_xyz_text(xyz_to_str(frame_xyz), "H2")
        triangle = [float(i) / 100 for i in range(21)]
        for is_ts in (False, True):
            with self.subTest(is_ts=is_ts), tempfile.TemporaryDirectory() as root:
                pathlib.Path(root, "freq.log").write_text("fixture")
                record = {"label": "TS0" if is_ts else "H2", "is_ts": is_ts,
                          "freq_log": "freq.log"}
                adapter = TCKDBAdapter(
                    TCKDBConfig(enabled=True, base_url="http://x", upload=False),
                    project_directory=root,
                )
                parser = mock.Mock()
                parser.parse_cartesian_hessian_lower_triangle.return_value = triangle
                parser.parse_cartesian_hessian_geometry.return_value = (frame_xyz, "gaussian_input_orientation")
                geometry = _normalize_xyz_text(xyz_to_str(frame_xyz), record["label"])
                with mock.patch("tckdb_arc._arc_optional.determine_ess", return_value="gaussian"), \
                     mock.patch("tckdb_arc._arc_optional.ess_factory", return_value=parser):
                    fallback = adapter._build_freq_hessian_payload(
                        output_doc={"schema_version": "1.0"}, species_record=record,
                        geometry_xyz_text=geometry,
                    )
                evidence_value = {
                    "source_log": "freq.log", "geometry_xyz_text": geometry,
                    "atom_count": 2, "matrix_dimension": 6,
                    "packing": "lower_triangle_row_major_including_diagonal",
                    "units": "hartree_per_bohr_squared", "source": "parsed_log",
                    "parser_version": "arc-hessian-1", "lower_triangle": triangle,
                }
                sidecar = {
                    "geometry": {"xyz_text": evidence_value["geometry_xyz_text"]},
                    "lower_triangle_hartree_bohr2": triangle,
                    "source": "parsed_log", "parser_version": "arc-hessian-1",
                }
                self.assertEqual(fallback, sidecar)
                self.assertEqual(self._canonical(fallback), self._canonical(sidecar))
                contract_validate(HessianPayload, sidecar)

    def test_irc_sidecar_matches_rich_fallback(self):
        from tckdb_arc.adapter import _build_irc_result_payload, _normalize_xyz_text
        from tckdb_arc._vendor import xyz_to_str

        xyz = {"symbols": ("H", "H"), "isotopes": (1, 1),
               "coords": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.7))}
        xyz_text = _normalize_xyz_text(xyz_to_str(xyz_dict=xyz), None)
        fallback_trajectories = [{
            "direction": "forward", "geom_points": None,
            "rich_points": [{
                "point_number": 1, "direction": "forward", "xyz": xyz,
                "electronic_energy_hartree": -1.2, "reaction_coordinate": 0.4,
                "max_gradient": 0.01, "rms_gradient": 0.005,
            }],
        }]
        sidecar_trajectories = [{
            "direction": "forward", "geom_points": None,
            "rich_points": [{
                "point_number": 1, "direction": "forward",
                "geometry_xyz_text": xyz_text,
                "electronic_energy_hartree": -1.2, "reaction_coordinate": 0.4,
                "max_gradient": 0.01, "rms_gradient": 0.005,
            }],
        }]
        marker = {"xyz_text": xyz_text, "electronic_energy_hartree": -1.1}
        fallback = _build_irc_result_payload(fallback_trajectories, -1.1, marker)
        sidecar = _build_irc_result_payload(sidecar_trajectories, -1.1, marker)
        self.assertEqual(fallback, sidecar)
        self.assertEqual(self._canonical(fallback), self._canonical(sidecar))
        contract_validate(IRCResultPayload, sidecar)

    def test_irc_builder_refuses_a_result_with_no_stated_direction(self):
        from tckdb_arc.adapter import _build_irc_result_payload
        xyz = {"symbols": ("H", "H"), "isotopes": (1, 1),
               "coords": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.74))}
        trajectories = [{"direction": None, "rich_points": None, "geom_points": [xyz, xyz]}]
        warnings = []
        self.assertIsNone(_build_irc_result_payload(trajectories, warnings=warnings))
        self.assertEqual([w["code"] for w in warnings], ["irc_direction_not_stated"])
        # A stated branch next to an unstated one is still a guess: the
        # unlabelled points would be filed under the stated branch and the
        # run mode would claim one direction. Refused as well.
        partial = [
            {"direction": "reverse", "rich_points": None, "geom_points": [xyz, xyz]},
            {"direction": None, "rich_points": None, "geom_points": [xyz, xyz]},
        ]
        warnings = []
        self.assertIsNone(_build_irc_result_payload(partial, warnings=warnings))
        self.assertEqual([w["code"] for w in warnings], ["irc_direction_not_stated"])
        self.assertIn("2 of 4 IRC point(s)", warnings[0]["message"])
        # A per-point direction from the parser labels the point even when
        # the log's own entry is unknown.
        rich = [{"direction": None, "geom_points": None, "rich_points": [
            {"xyz": xyz, "direction": "forward"}, {"xyz": xyz, "direction": "forward"}]}]
        result = _build_irc_result_payload(rich, warnings=warnings)
        self.assertEqual(result["direction"], "forward")
        # One stated direction on every point: the run mode is that branch.
        trajectories[0]["direction"] = "reverse"
        warnings = []
        result = _build_irc_result_payload(trajectories, warnings=warnings)
        self.assertEqual((result["direction"], result["has_forward"], result["has_reverse"]),
                         ("reverse", False, True))
        self.assertEqual(warnings, [])
        contract_validate(IRCResultPayload, result)

    def test_gsm_absolute_energies_require_evidence_not_raw_node_indices(self):
        from tckdb_arc.adapter import _build_path_search_result_payload, _normalize_xyz_text
        from tckdb_arc._vendor import xyz_to_str

        frames = [
            {"symbols": ("H", "H"), "isotopes": (1, 1),
             "coords": ((0.0, 0.0, 0.0), (0.0, 0.0, z))}
            for z in (0.7, 0.8, 0.9)
        ]
        metadata = {1: {"electronic_energy_hartree": -1.2},
                    2: {"electronic_energy_hartree": -1.0,
                        "max_gradient": 0.03, "rms_gradient": 0.01}}
        with mock.patch("tckdb_arc._arc_optional.parse_trajectory", return_value=frames), \
             mock.patch("tckdb_arc._arc_optional.kabsch", return_value=0.1), \
             mock.patch("tckdb_arc.adapter._read_gsm_node_outputs", return_value=metadata), \
             mock.patch("tckdb_arc._arc_optional.parse_gsm_stringfile_energies",
                        return_value=[0.0, 0.0, 0.0]):
            fallback = _build_path_search_result_payload(
                method="gsm", log_path="stringfile.xyz0000",
                fallback_xyz_text=None, node_outputs_dir="nodes",
            )
        evidence_points = []
        for index, frame in enumerate(frames):
            point = {
                "source_point_index": index,
                "node_label": index or None,
                "geometry_xyz_text": _normalize_xyz_text(
                    xyz_to_str(xyz_dict=frame), f"gsm_point_{index}",
                ),
                "path_coordinate_angstrom": index * 0.1,
                "stringfile_relative_energy_kcal_mol": 0.0,
            }
            if index in metadata:
                point.update({
                    "electronic_energy_hartree": metadata[index]["electronic_energy_hartree"],
                    **({"max_gradient_hartree_per_bohr": metadata[index]["max_gradient"],
                        "rms_gradient_hartree_per_bohr": metadata[index]["rms_gradient"]}
                       if "max_gradient" in metadata[index] else {}),
                })
            evidence_points.append(point)
        with mock.patch("tckdb_arc._arc_optional.parse_trajectory",
                        side_effect=AssertionError("fallback parser used")):
            sidecar = _build_path_search_result_payload(
                method="gsm", log_path=None, fallback_xyz_text=None,
                gsm_evidence={"selected_source_point_index": 2, "points": evidence_points},
            )
        self.assertNotIn("zero_energy_reference_hartree", fallback)
        self.assertTrue(all("electronic_energy_hartree" not in p for p in fallback["points"]))
        self.assertAlmostEqual(sidecar["zero_energy_reference_hartree"], -1.2)
        self.assertEqual(sidecar["points"][2]["max_gradient"], 0.03)
        for actual, expected in zip(fallback["points"], sidecar["points"]):
            self.assertEqual(actual["geometry"], expected["geometry"])
        contract_validate(PathSearchResultPayload, sidecar)

    # Scaffolding for the live-ARC-producer tests below: the golden project's
    # logs, mocked ESS parsers, and distinct bent H3 stringfile frames, so the
    # real ``kabsch`` gives a non-trivial path coordinate.
    _PRODUCER_XYZ = {"symbols": ("H", "H", "H"), "isotopes": (1, 1, 1),
                     "coords": ((0.0, 0.0, -0.8), (0.0, 0.0, 0.0), (0.0, 0.0, 0.8))}
    _PRODUCER_H2_XYZ = {"symbols": ("H", "H"), "isotopes": (1, 1),
                        "coords": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.74))}
    _PRODUCER_FRAMES = [
        {"symbols": ("H", "H", "H"), "isotopes": (1, 1, 1),
         "coords": ((0.0, 0.0, -d), (0.0, 0.0, 0.0), (0.3, 0.0, d))}
        for d in (0.7, 0.8, 0.9)
    ]
    _PRODUCER_STRINGFILE_ENERGIES = [0.0, 3.0, 1.0]

    @classmethod
    def _producer_parser_for(cls, path, _ess):
        is_ts = "TS0" in path
        parser = mock.Mock()
        parser.parse_cartesian_hessian_lower_triangle.return_value = [0.01] * (45 if is_ts else 21)
        parser.parse_cartesian_hessian_geometry.return_value = (
            cls._PRODUCER_XYZ if is_ts else cls._PRODUCER_H2_XYZ, "gaussian_input_orientation",
        )
        return parser

    @classmethod
    def _producer_irc_points(cls, log_file_path, **_kwargs):
        direction = "forward" if "forward" in log_file_path else "reverse"
        return [{"point_number": 1, "direction": direction, "xyz": cls._PRODUCER_XYZ,
                 "electronic_energy_hartree": -1.62}]

    def _run_actual_arc_producer(self, root, gradients):
        """Write the golden project under ``root`` and run ARC's real producer.

        ``gradients`` maps each archived ``ograd`` invocation id to its
        ``(energy_hartree, H3 coordinates in bohr)``. Returns ``(base, evidence)``
        after writing the sidecar with ``write_parser_evidence_atomic``.
        """
        import yaml
        from arc.parser_evidence import (
            EVIDENCE_FILENAME,
            OUTPUT_SCHEMA_VERSION,
            build_parser_evidence,
            write_parser_evidence_atomic,
        )

        fixture = pathlib.Path(__file__).parent / "fixtures" / "golden" / "phase3_output.yml"
        base = yaml.safe_load(fixture.read_text())
        # The producer stamps its own output schema version into the sidecar,
        # and the adapter requires the document's to equal it: follow
        # whichever ARC is installed (1.1 on main, 1.2 from a10e8ae0).
        base["schema_version"] = OUTPUT_SCHEMA_VERSION
        for record in (base["species"][0], base["transition_states"][0]):
            for field in ("freq_log", "gsm_log"):
                if record.get(field):
                    path = pathlib.Path(root, record[field]); path.parent.mkdir(parents=True, exist_ok=True); path.write_text("x")
                    if field == "gsm_log":
                        node_outputs = path.parent / "gsm_node_outputs"
                        node_outputs.mkdir()
                        for invocation_id, (energy, coords_bohr) in gradients.items():
                            (node_outputs / f"{invocation_id}.energy").write_text(
                                f"$energy\n 1 {energy} 0 0\n$end\n"
                            )
                            atoms = "".join(f"   {x!r}  {y!r}  {z!r}   h\n" for x, y, z in coords_bohr)
                            (node_outputs / f"{invocation_id}.gradient").write_text(
                                f"$grad\n  cycle =      1    SCF energy =    {energy}\n{atoms}"
                                "   0.0  0.0  0.02\n   0.0  0.0  0.0\n   0.0  0.0  -0.02\n$end\n"
                            )
            for relative in record.get("irc_logs") or []:
                path = pathlib.Path(root, relative); path.parent.mkdir(parents=True, exist_ok=True); path.write_text("x")

        document_id = "0123456789abcdef0123456789abcdef"
        with mock.patch("arc.parser_evidence.determine_ess", return_value="gaussian"), \
             mock.patch("arc.parser_evidence.ess_factory", side_effect=self._producer_parser_for), \
             mock.patch("arc.parser_evidence.parse_irc_path", side_effect=self._producer_irc_points), \
             mock.patch("arc.parser_evidence.parse_trajectory", return_value=self._PRODUCER_FRAMES), \
             mock.patch("arc.parser_evidence.parse_gsm_stringfile_energies",
                        return_value=self._PRODUCER_STRINGFILE_ENERGIES):
            evidence = build_parser_evidence(
                output_doc=base, project_directory=root, document_id=document_id,
            )
        written = write_parser_evidence_atomic(
            evidence_doc=evidence, output_directory=pathlib.Path(root, "output"),
        )
        self.assertEqual(written, pathlib.Path(root, "output", EVIDENCE_FILENAME))
        return base, evidence

    def _sidecar_reaction_payload(self, root, base, evidence):
        """Build the reaction payload from the written sidecar, forbidding raw parsers.

        Returns ``(sidecar_doc, sidecar_adapter, payload)``.
        """
        from arc.parser_evidence import (
            EVIDENCE_FILENAME,
            EVIDENCE_SCHEMA_NAME,
            EVIDENCE_SCHEMA_VERSION,
        )

        sidecar_doc = copy.deepcopy(base)
        sidecar_doc.pop("tckdb_evidence", None)
        sidecar_doc["parser_evidence"] = {
            "path": EVIDENCE_FILENAME, "schema_name": EVIDENCE_SCHEMA_NAME,
            "schema_version": EVIDENCE_SCHEMA_VERSION, "document_id": evidence["document_id"],
        }
        cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
        sidecar_adapter = TCKDBAdapter(cfg, project_directory=root)
        fallback_parser_used = AssertionError("raw fallback parser used on the sidecar path")
        with mock.patch("tckdb_arc._arc_optional.determine_ess", side_effect=fallback_parser_used), \
             mock.patch("tckdb_arc._arc_optional.ess_factory", side_effect=fallback_parser_used), \
             mock.patch("tckdb_arc._arc_optional.parse_irc_path", side_effect=fallback_parser_used), \
             mock.patch("tckdb_arc._arc_optional.parse_irc_traj", side_effect=fallback_parser_used), \
             mock.patch("tckdb_arc._arc_optional.parse_trajectory", side_effect=fallback_parser_used), \
             mock.patch("tckdb_arc._arc_optional.parse_gsm_stringfile_energies",
                        side_effect=fallback_parser_used):
            payload = sidecar_adapter._build_computed_reaction_payload(
                output_doc=sidecar_doc, reaction_record=sidecar_doc["reactions"][0],
            )
        return sidecar_doc, sidecar_adapter, payload

    def test_actual_arc_producer_to_evidence_store_matches_fallback_payload(self):
        """Exercise ARC builders, JSON writer, EvidenceStore, and final composer together.

        The producer is ARC's ``arc.parser_evidence`` (successor of the removed
        ``arc.tckdb_evidence``). ``arc/output.py`` writes its sidecar to
        ``output/parser_evidence.json`` and records it under the
        ``parser_evidence`` descriptor, which is what the sidecar document
        carries here.

        The GSM run archives two ``ograd`` invocations whose gradient-file
        geometries match no stringfile frame. The producer lists them under
        ``ograd_invocations`` but attaches them to no point, which is the only
        GSM shape the raw fallback can match: the fallback never index-attaches
        archived absolute energies (see
        ``test_gsm_absolute_energies_require_evidence_not_raw_node_indices``).
        The two geometries differ, so neither can be left unattached merely
        because both contest the same frame.
        """
        pytest.importorskip("arc")

        with tempfile.TemporaryDirectory() as root:
            # Bent H3 at 2.5 and 3.0 bohr (~1.32 and ~1.59 A) spacing: not any frame.
            base, evidence = self._run_actual_arc_producer(root, {
                "0000.01": (-1.7, ((0.0, 0.0, -2.5), (0.0, 0.0, 0.0), (0.6, 0.0, 2.5))),
                "0000.02": (-1.55, ((0.0, 0.0, -3.0), (0.0, 0.0, 0.0), (0.9, 0.0, 3.0))),
            })

            # Every attempted entry must be available; an unavailable or absent
            # entry would let both sides drop the same sub-payload and still agree.
            statuses = {
                (record["label"], kind): record[kind]["status"]
                for record in evidence["records"]
                for kind in ("freq_hessian", "irc", "gsm") if kind in record
            }
            self.assertEqual(statuses, {
                ("H2", "freq_hessian"): "available",
                ("TS0", "freq_hessian"): "available",
                ("TS0", "irc"): "available",
                ("TS0", "gsm"): "available",
            })
            gsm_value = next(r for r in evidence["records"] if r["label"] == "TS0")["gsm"]["value"]
            self.assertEqual([item["invocation_id"] for item in gsm_value["ograd_invocations"]],
                             ["0000.01", "0000.02"])
            for point in gsm_value["points"]:
                self.assertNotIn("geometry_matched_ograd_invocation_id", point)
                self.assertNotIn("electronic_energy_hartree", point)

            sidecar_doc, sidecar_adapter, sidecar_payload = self._sidecar_reaction_payload(
                root, base, evidence,
            )
            for kind, label, evidence_kind in (
                ("species", "H2", "freq_hessian"),
                ("transition_state", "TS0", "freq_hessian"),
                ("transition_state", "TS0", "irc"),
                ("transition_state", "TS0", "gsm"),
            ):
                self.assertEqual(
                    sidecar_adapter._evidence.lookup(sidecar_doc, kind, label, evidence_kind).state,
                    "available", (kind, label, evidence_kind),
                )

            cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
            fallback_doc = copy.deepcopy(base)
            fallback_doc["schema_version"] = "1.0"
            fallback_doc.pop("tckdb_evidence", None)
            with mock.patch("tckdb_arc._arc_optional.determine_ess", return_value="gaussian"), \
                 mock.patch("tckdb_arc._arc_optional.ess_factory", side_effect=self._producer_parser_for), \
                 mock.patch("tckdb_arc._arc_optional.parse_irc_path", side_effect=self._producer_irc_points), \
                 mock.patch("tckdb_arc._arc_optional.parse_trajectory", return_value=self._PRODUCER_FRAMES), \
                 mock.patch("tckdb_arc._arc_optional.parse_gsm_stringfile_energies",
                            return_value=self._PRODUCER_STRINGFILE_ENERGIES):
                fallback_payload = TCKDBAdapter(cfg, project_directory=root)._build_computed_reaction_payload(
                    output_doc=fallback_doc, reaction_record=fallback_doc["reactions"][0],
                )
            # Both sides must actually carry each evidence-backed sub-payload.
            ts_calcs = {calc["type"]: calc for calc in sidecar_payload["transition_state"]["calculations"]}
            self.assertIn("hessian", ts_calcs["freq"])
            self.assertEqual(ts_calcs["irc"]["irc_result"]["point_count"], 3)
            # The golden TS's guess is a GSM path search, whose level ARC does
            # not export: the calculation is omitted on both sides (the GSM
            # evidence itself is still produced and readable, checked above,
            # and rendered by ``_build_path_search_result_payload`` directly
            # in ``test_actual_arc_producer_geometry_matched_gsm_values_reach_payload``).
            self.assertNotIn("path_search", ts_calcs)
            species_hessians = [
                calc for species in sidecar_payload["species"]
                for calc in species["calculations"] if "hessian" in calc
            ]
            self.assertEqual(len(species_hessians), 2)  # H2 as reactant and as product
            self.assertEqual(sidecar_payload, fallback_payload)
            self.assertEqual(self._canonical(sidecar_payload), self._canonical(fallback_payload))
            contract_validate(ComputedReactionUploadRequest, sidecar_payload)

    def test_actual_arc_producer_geometry_matched_gsm_values_reach_payload(self):
        """A gradient file whose geometry is exactly one frame carries its values to that point.

        Not a parity test: the raw fallback never attaches archived energies,
        so only the sidecar path can deliver them. The one archived invocation
        was evaluated at frame 1's geometry (written in bohr, as xTB does).
        """
        pytest.importorskip("arc")
        from arc.constants import bohr_to_angstrom
        from tckdb_arc.adapter import _build_path_search_result_payload

        matched_index = 1
        frame_bohr = tuple(
            tuple(c / bohr_to_angstrom for c in atom)
            for atom in self._PRODUCER_FRAMES[matched_index]["coords"]
        )
        with tempfile.TemporaryDirectory() as root:
            base, evidence = self._run_actual_arc_producer(root, {"0000.01": (-1.7, frame_bohr)})
            gsm_value = next(r for r in evidence["records"] if r["label"] == "TS0")["gsm"]["value"]
            [invocation] = gsm_value["ograd_invocations"]
            attached = [p for p in gsm_value["points"] if "geometry_matched_ograd_invocation_id" in p]
            self.assertEqual([p["source_point_index"] for p in attached], [matched_index])
            producer_point = attached[0]
            self.assertEqual(producer_point["geometry_matched_ograd_invocation_id"], "0000.01")
            self.assertEqual(producer_point["electronic_energy_hartree"],
                             invocation["electronic_energy_hartree"])
            self.assertEqual(producer_point["max_gradient_hartree_per_bohr"],
                             invocation["max_gradient_hartree_per_bohr"])
            self.assertEqual(producer_point["rms_gradient_hartree_per_bohr"],
                             invocation["rms_gradient_hartree_per_bohr"])
            # The literal values the helper wrote: the energy file's -1.7 and
            # the gradient block (0, 0, +-0.02 on the end atoms).
            self.assertEqual(producer_point["electronic_energy_hartree"], -1.7)
            self.assertEqual(producer_point["max_gradient_hartree_per_bohr"], 0.02)
            self.assertAlmostEqual(producer_point["rms_gradient_hartree_per_bohr"],
                                   math.sqrt(2 * 0.02 ** 2 / 9))

            sidecar_doc, sidecar_adapter, payload = self._sidecar_reaction_payload(root, base, evidence)
            lookup = sidecar_adapter._evidence.lookup(sidecar_doc, "transition_state", "TS0", "gsm")
            self.assertEqual(lookup.state, "available")
            store_point = lookup.value["points"][matched_index]
            self.assertEqual(store_point["geometry_matched_ograd_invocation_id"], "0000.01")

        # The reaction payload omits the GSM path-search calculation (ARC
        # exports no GSM level), so render the evidence the store served
        # through the path-search builder directly.
        ts_types = [calc["type"] for calc in payload["transition_state"]["calculations"]]
        self.assertNotIn("path_search", ts_types)
        result = _build_path_search_result_payload(
            method="gsm", log_path=None, fallback_xyz_text=None,
            gsm_evidence=lookup.value,
        )
        points = {point["point_index"]: point for point in result["points"]}
        self.assertEqual(points[matched_index]["electronic_energy_hartree"],
                         producer_point["electronic_energy_hartree"])
        self.assertEqual(points[matched_index]["max_gradient"],
                         producer_point["max_gradient_hartree_per_bohr"])
        self.assertEqual(points[matched_index]["rms_gradient"],
                         producer_point["rms_gradient_hartree_per_bohr"])
        self.assertEqual(points[matched_index]["electronic_energy_hartree"], -1.7)
        self.assertEqual(points[matched_index]["max_gradient"], 0.02)
        self.assertAlmostEqual(points[matched_index]["rms_gradient"], math.sqrt(2 * 0.02 ** 2 / 9))
        self.assertEqual(result["zero_energy_reference_hartree"],
                         producer_point["electronic_energy_hartree"])
        self.assertEqual(result["zero_energy_reference_hartree"], -1.7)
        self.assertEqual(points[matched_index]["relative_energy_kj_mol"], 0.0)
        for index, point in points.items():
            if index != matched_index:
                self.assertNotIn("electronic_energy_hartree", point)
        contract_validate(PathSearchResultPayload, result)
        contract_validate(ComputedReactionUploadRequest, payload)


class TestSingleHartreeToKjMolConstant(unittest.TestCase):
    """C-3: one Hartree->kJ/mol constant, not two independently-rounded ones.

    ``irc_result.points[].relative_energy_kj_mol`` and
    ``path_search_result.points[].relative_energy_kj_mol`` are identically
    named/united fields; before this fix they were computed from two
    different constants (an inline ``2625.4996`` vs. vendored
    ``_vendor.E_h_kJmol == 2625.4998583629967``, relative difference
    ~9.84e-8). Guard against either a reintroduced duplicate or a
    re-diverged value.
    """

    def test_no_duplicate_hartree_constant_on_adapter_module(self):
        # D5 (adversarial review): the original version of this test only
        # checked ``not hasattr(adapter_module, "_HARTREE_TO_KJ_MOL")`` --
        # a name-based guard a reintroduced *function-local* constant
        # (e.g. a fresh ``2625.4996`` literal inside
        # ``_build_irc_result_payload``, never bound to a module
        # attribute) would sail straight past. Parse adapter.py's own
        # source with ``ast`` instead and scan every numeric literal in
        # the whole module -- module-level, function-local, nested,
        # anywhere -- for one that lands suspiciously close to the
        # Hartree->kJ/mol constant. This catches a reintroduced duplicate
        # regardless of scope or binding.
        import ast
        import inspect
        import tckdb_arc.adapter as adapter_module
        from tckdb_arc._vendor import E_h_kJmol

        self.assertFalse(
            hasattr(adapter_module, "_HARTREE_TO_KJ_MOL"),
            "adapter.py must not define its own Hartree->kJ/mol constant; "
            "use tckdb_arc._vendor.E_h_kJmol so IRC and path-search "
            "relative_energy_kj_mol agree bit-for-bit.",
        )

        source = inspect.getsource(adapter_module)
        tree = ast.parse(source, filename=adapter_module.__file__)
        suspects = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                # "Near 2625.49": close enough to be a hand-typed or
                # differently-rounded Hartree->kJ/mol constant, but not
                # so wide a band it flags unrelated energy literals.
                if abs(node.value - 2625.49) < 0.01:
                    suspects.append((node.lineno, node.value))
        self.assertEqual(
            [], suspects,
            f"adapter.py contains a numeric literal near the Hartree->kJ/mol "
            f"constant ({E_h_kJmol!r}) outside _vendor.py: {suspects!r}. "
            f"Import tckdb_arc._vendor.E_h_kJmol instead of hand-typing the "
            f"value, at module level or locally.",
        )

    def test_vendored_constant_matches_arc_constants_definition(self):
        # Pins the vendored value against its own documented derivation
        # (E_h * Na / 1000) so a future edit to E_h/Na is caught here
        # rather than silently drifting the two consuming fields apart
        # again. Na is deliberately the pre-2019 CODATA value (see
        # _vendor.py's module docstring) — this test is not asking for
        # scientific accuracy, only for the two call sites to agree.
        from tckdb_arc._vendor import E_h, E_h_kJmol, Na
        self.assertAlmostEqual(E_h_kJmol, E_h * Na / 1000, places=12)


class TestNeutralArcResultTranslation(unittest.TestCase):
    """ARC records scientific facts; this package owns TCKDB projection."""

    def test_native_constraint_indices_are_normalized_at_adapter_boundary(self):
        from tckdb_arc.constraints import serialize_constraints

        self.assertEqual(serialize_constraints([{
            "coordinate_type": "distance",
            "atom_indices": [0, 1],
            "index_base": 0,
            "target_value": 1.4,
        }]), [{
            "constraint_index": 1,
            "constraint_kind": "bond",
            "atom1_index": 1,
            "atom2_index": 2,
            "target_value": 1.4,
        }])

    def test_neutral_rotor_scan_maps_to_scan_payload(self):
        from tckdb_arc.adapter import _scan_entries_from_record

        entries = _scan_entries_from_record({"rotor_scans": [{
            "key": "scan_rotor_0",
            "result": {
                "dimension": 1,
                "relaxed": True,
                "coordinate": {
                    "coordinate_type": "dihedral",
                    "atom_indices": [1, 2, 3, 4],
                    "index_base": 1,
                    "unit": "degree",
                    "requested_step_size": 120.0,
                },
                "samples": [
                    {"source_index": 0, "angle_degrees": 0.0,
                     "relative_energy_kj_mol": 0.0,
                     "geometry_xyz": "H 0 0 0\nH 0 0 1"},
                    {"source_index": 1, "angle_degrees": 120.0,
                     "relative_energy_kj_mol": 2.0},
                ],
            },
        }]})
        scan = entries[0]["scan_result"]
        self.assertEqual(scan["coordinates"][0]["atom1_index"], 1)
        self.assertEqual(scan["coordinates"][0]["step_size"], 120.0)
        self.assertEqual(scan["points"][0]["point_index"], 1)
        self.assertTrue(scan["points"][0]["geometry"]["xyz_text"].startswith("2\n"))

    def test_neutral_rotor_scan_index_base_omitted_defaults_to_one(self):
        # C-2: the only reachable producer (ARC's rotor-scan writer)
        # always emits index_base explicitly (schema-pinned `const: 1`),
        # so this covers a hand-written/older document that omits the
        # key. Safe on this path because rotor-scan atom indices are
        # otherwise indistinguishable from already-1-based.
        from tckdb_arc.adapter import _scan_entries_from_record

        entries = _scan_entries_from_record({"rotor_scans": [{
            "key": "scan_rotor_0",
            "result": {
                "dimension": 1,
                "relaxed": True,
                "coordinate": {
                    "coordinate_type": "dihedral",
                    "atom_indices": [1, 2, 3, 4],
                    # index_base intentionally omitted.
                    "unit": "degree",
                },
                "samples": [
                    {"source_index": 0, "angle_degrees": 0.0},
                ],
            },
        }]})
        self.assertEqual(len(entries), 1)
        coord = entries[0]["scan_result"]["coordinates"][0]
        self.assertEqual(coord["atom1_index"], 1)
        self.assertEqual(coord["atom4_index"], 4)

    def test_neutral_rotor_scan_out_of_range_index_base_omits_scan(self):
        # C-2: unlike the omitted-key default (safe today), an explicit
        # out-of-range index_base must not be applied blindly — it would
        # produce non-positive atom indices. Guarded the same way
        # constraints.py guards constraints[] (index_base in (0, 1)).
        from tckdb_arc.adapter import _scan_entries_from_record

        entries = _scan_entries_from_record({"rotor_scans": [{
            "key": "scan_rotor_0",
            "result": {
                "dimension": 1,
                "relaxed": True,
                "coordinate": {
                    "coordinate_type": "dihedral",
                    "atom_indices": [1, 2, 3, 4],
                    "index_base": 7,
                    "unit": "degree",
                },
                "samples": [
                    {"source_index": 0, "angle_degrees": 0.0},
                ],
            },
        }]})
        self.assertEqual(entries, [])

    def test_neutral_scan_resolution_degrees_set_for_degree_unit(self):
        # Control case: a real (dihedral/degree) rotor scan still gets
        # resolution_degrees, matching the pre-C-5 behavior.
        from tckdb_arc.adapter import _neutral_scan_result_to_tckdb

        scan = _neutral_scan_result_to_tckdb({
            "dimension": 1,
            "relaxed": True,
            "coordinate": {
                "coordinate_type": "dihedral",
                "atom_indices": [1, 2, 3, 4],
                "index_base": 1,
                "unit": "degree",
                "requested_step_size": 10.0,
            },
            "samples": [{"source_index": 0, "angle_degrees": 0.0}],
        })
        coord = scan["coordinates"][0]
        self.assertEqual(coord["step_size"], 10.0)
        self.assertEqual(coord["resolution_degrees"], 10.0)

    def test_neutral_scan_resolution_degrees_omitted_for_non_degree_unit(self):
        # C-5: a distance-kind scan's step is in Angstrom, not degrees.
        # ``step_size`` (generic, paired with ``value_unit``) still
        # carries it; ``resolution_degrees`` — a degree-specific field —
        # must not. Unreachable from real ARC output (its rotor-scan
        # writer only emits dihedral/degree scans, schema-pinned), but
        # this function also serves hand-written/third-party
        # output.yml, which evidence.py doesn't validate against ARC's
        # schema.
        from tckdb_arc.adapter import _neutral_scan_result_to_tckdb

        scan = _neutral_scan_result_to_tckdb({
            "dimension": 1,
            "relaxed": True,
            "coordinate": {
                "coordinate_type": "distance",
                "atom_indices": [1, 2],
                "index_base": 1,
                "unit": "angstrom",
                "requested_step_size": 0.05,
            },
            "samples": [{"source_index": 0, "angle_degrees": 0.0}],
        })
        coord = scan["coordinates"][0]
        self.assertEqual(coord["value_unit"], "angstrom")
        self.assertEqual(coord["step_size"], 0.05)
        self.assertNotIn("resolution_degrees", coord)

    def test_neutral_corrections_map_to_tckdb_schemes(self):
        from tckdb_arc.adapter import _correction_records_from_record

        records = _correction_records_from_record({"energy_corrections": [{
            "correction_type": "atom_energy",
            "model": "arkane_atom_energy",
            "level_of_theory": {"method": "wb97xd"},
            "total": {"value": -0.02, "unit": "hartree"},
            "components": [],
            "reference_atom_energies": {
                "unit": "hartree", "applied_as": "subtracted",
                "values": {"H": -0.5, "C": -37.8},
            },
        }]})
        self.assertEqual(records[0]["application_role"], "aec_total")
        self.assertEqual(records[0]["scheme"]["kind"], "atom_energy")
        self.assertEqual(records[0]["scheme"]["atom_params"], [
            {"element": "C", "value": -37.8},
            {"element": "H", "value": -0.5},
        ])
        self.assertEqual(records[0]["scheme"]["units"], "hartree")
        self.assertEqual(records[0]["value_unit"], "hartree")

    def test_atom_params_units_are_the_tables_own_not_the_totals(self):
        # ``scheme.units`` is the unit of the scheme's parameter values; the
        # applied total keeps its own ``value_unit``.
        from tckdb_arc.adapter import _correction_records_from_record

        records = _correction_records_from_record({"energy_corrections": [{
            "correction_type": "atom_energy", "model": "arkane_atom_energy",
            "total": {"value": -52.5, "unit": "kcal_mol"},
            "reference_atom_energies": {"unit": "kj_mol", "values": {"H": -1312.75}},
        }]})
        self.assertEqual(records[0]["value_unit"], "kcal_mol")
        self.assertEqual(records[0]["scheme"]["units"], "kj_mol")
        self.assertEqual(records[0]["scheme"]["atom_params"],
                         [{"element": "H", "value": -1312.75}])

    def test_atom_params_omitted_when_table_unit_missing_or_values_unusable(self):
        from tckdb_arc.adapter import _correction_records_from_record

        bad_tables = [
            {"values": {"H": -0.5}},                              # no unit
            {"unit": "ha", "values": {"H": -0.5}},                # unknown unit
            {"unit": "hartree", "values": {"H": None}},           # null value
            {"unit": "hartree", "values": {"H": float("nan")}},   # non-finite
            {"unit": "hartree", "values": {"H": -0.5, "Xxxx": 1.0}},  # bad element
            {"unit": "hartree", "values": {}},
        ]
        for table in bad_tables:
            with self.subTest(table=table):
                records = _correction_records_from_record({"energy_corrections": [{
                    "correction_type": "atom_energy", "model": "arkane_atom_energy",
                    "total": {"value": -0.02, "unit": "hartree"},
                    "reference_atom_energies": table,
                }]})
                self.assertNotIn("atom_params", records[0]["scheme"])
                # The correction itself is still sent, in its own unit.
                self.assertEqual(records[0]["scheme"]["units"], "hartree")

    def test_atom_energy_parameter_table_is_not_read(self):
        # ARC writes ``parameter_table`` only on the Petersson record; an
        # atom_energy record carries ``reference_atom_energies``.
        from tckdb_arc.adapter import _correction_records_from_record

        records = _correction_records_from_record({"energy_corrections": [{
            "correction_type": "atom_energy", "model": "arkane_atom_energy",
            "total": {"value": -0.02, "unit": "hartree"},
            "parameter_table": {"unit": "hartree", "values": {"H": -0.5}},
        }]})
        self.assertNotIn("atom_params", records[0]["scheme"])

    # ---------------- C-6: unit is read, never guessed
    def test_neutral_correction_missing_unit_is_omitted_not_defaulted(self):
        # Real ARC output always sets total.unit (ARC's correction script
        # hardcodes 'hartree'/'kcal_mol' — see
        # arc/scripts/get_species_corrections.py); this covers a
        # hand-written/third-party output.yml that omits it. Before the
        # fix, an atom_energy correction with no unit silently defaulted
        # to 'hartree' and a bond_additivity correction to 'kcal_mol' —
        # right most of the time by ARC convention, but never verified
        # against what this particular record actually reported.
        from tckdb_arc.adapter import _correction_records_from_record

        records = _correction_records_from_record({"energy_corrections": [
            {
                "correction_type": "atom_energy",
                "model": "arkane_atom_energy",
                "total": {"value": -0.02},  # no "unit" key
                "components": [],
            },
            {
                "correction_type": "bond_additivity",
                "model": "petersson",
                "total": {"value": -0.69, "unit": ""},  # falsy unit
                "components": [],
            },
        ]})
        self.assertEqual(records, [])

    def test_neutral_correction_bac_unit_read_from_total_not_guessed(self):
        # Control: when the unit IS present, it's read verbatim rather
        # than reconstructed from correction_type — guards against a
        # regression that goes back to guessing whenever the reported
        # unit happens to match the guess.
        from tckdb_arc.adapter import _correction_records_from_record

        records = _correction_records_from_record({"energy_corrections": [{
            "correction_type": "bond_additivity",
            "model": "melius",
            "total": {"value": -0.056, "unit": "kcal_mol"},
            "components": [],
        }]})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["value_unit"], "kcal_mol")
        self.assertEqual(records[0]["scheme"]["units"], "kcal_mol")


if __name__ == "__main__":
    unittest.main()
