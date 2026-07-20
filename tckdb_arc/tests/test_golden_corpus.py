#!/usr/bin/env python3
# encoding: utf-8

"""Golden-corpus contract test (plan §3).

Builds every ``TCKDBAdapter.submit_*_from_output`` payload **offline**
(``upload=False``) over a frozen input document, then ``model_validate`` each
built payload against the *real* published ``tckdb_schemas`` models. This is the
regression that catches a shared-schema bump breaking the ARC↔TCKDB wire shape
in the adapter repo — instead of months later in an ARC run.

The Phase 3 cases use the checked-in, sanitized ARC ``output.yml`` and its
matching evidence sidecar from ``tests/fixtures/golden``.  The older synthetic
cases remain useful as a compact compatibility corpus for schema 1.0 output.

Also asserts the forbidden-key boundary: ARC-internal keys the wire must never
carry (``atom_map``, ``ts_report``, ``successful_methods``, ``server``,
``job_id``, ``relative_e0_kj_mol``) never appear anywhere in a built payload.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig

# Real published wire-contract models (test-only dependency).
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.fragments.calculation import CalculationWithResultsPayload
from tckdb_schemas.fragments.geometry import GeometryPayload
from tckdb_schemas.fragments.identity import SpeciesEntryIdentityPayload

# Synthetic corpus + stubs reused from the ported adapter tests.
from test_adapter import (
    _StubClient,
    _StubResponse,
    _fake_output_doc,
    _full_record,
    _reaction_output_doc,
    _reaction_record,
)
from test_ts_upload import TransitionStateUploadRequest, _compose


# Keys that identify ARC-internal state and MUST NOT cross the wire.
FORBIDDEN_KEYS = frozenset({
    "atom_map",
    "ts_report",
    "successful_methods",
    "server",
    "job_id",
    "relative_e0_kj_mol",
})


def _iter_keys(obj):
    """Yield every mapping key appearing anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _iter_keys(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_keys(item)


def _assert_no_forbidden_keys(testcase, payload, label):
    present = FORBIDDEN_KEYS.intersection(_iter_keys(payload))
    testcase.assertEqual(
        set(), present,
        f"forbidden ARC-internal key(s) leaked into the {label} payload: {sorted(present)}",
    )


class TestGoldenCorpus(unittest.TestCase):
    """Offline build → schema-validate for species / reaction / TS payloads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tckdb-golden-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def _adapter(self, upload_mode):
        cfg = TCKDBConfig(
            enabled=True,
            base_url="http://localhost:8000/api/v1",
            payload_dir=self.tmp,
            api_key_env="X_TCKDB_API_KEY",
            project_label="golden",
            upload_mode=upload_mode,
            upload=False,  # offline: build + write payload, no network POST
        )
        client = _StubClient(response=_StubResponse({"id": 1}))
        return TCKDBAdapter(cfg, project_directory=self.tmp,
                            client_factory=lambda c, k: client)

    def _built_payload(self, outcome):
        self.assertIsNotNone(outcome.payload_path, "adapter must write a payload to disk")
        self.assertTrue(outcome.payload_path.exists())
        return json.loads(outcome.payload_path.read_text())

    def _phase3_corpus(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "golden"
        output_doc = yaml.safe_load((fixture_dir / "phase3_output.yml").read_text())
        (Path(self.tmp) / "output").mkdir()
        shutil.copyfile(fixture_dir / "tckdb_evidence.json",
                        Path(self.tmp) / "output" / "tckdb_evidence.json")
        return output_doc

    @staticmethod
    def _canonical_sha256(payload):
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    # --- computed species -------------------------------------------------
    def test_computed_species_payload_validates(self):
        adapter = self._adapter("computed_species")
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_species_from_output(
                output_doc=_fake_output_doc(),
                species_record=_full_record(),
            )
        payload = self._built_payload(outcome)
        ComputedSpeciesUploadRequest.model_validate(payload)
        _assert_no_forbidden_keys(self, payload, "computed_species")

    # --- computed reaction ------------------------------------------------
    def test_computed_reaction_payload_validates(self):
        adapter = self._adapter("computed_reaction")
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=_reaction_output_doc(),
                reaction_record=_reaction_record(),
            )
        payload = self._built_payload(outcome)
        ComputedReactionUploadRequest.model_validate(payload)
        _assert_no_forbidden_keys(self, payload, "computed_reaction")

    def test_computed_reaction_with_irc_payload_validates(self):
        adapter = self._adapter("computed_reaction")
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=_reaction_output_doc(with_irc=True),
                reaction_record=_reaction_record(),
            )
        payload = self._built_payload(outcome)
        ComputedReactionUploadRequest.model_validate(payload)
        _assert_no_forbidden_keys(self, payload, "computed_reaction+irc")

    # --- standalone transition state --------------------------------------
    def test_transition_state_request_validates(self):
        _doc, _ts, _rxn, payload = _compose()
        TransitionStateUploadRequest(**payload)
        _assert_no_forbidden_keys(self, payload, "transition_state")

    # --- fragment-level validation (representative, from the TS payload) --
    def test_fragment_shapes_validate(self):
        _doc, _ts, _rxn, payload = _compose()

        # CalculationWithResultsPayload — the primary opt calc.
        primary_opt = copy.deepcopy(payload["primary_opt"])
        CalculationWithResultsPayload.model_validate(primary_opt)

        # GeometryPayload — pulled from the opt calc's output geometry.
        output_geometries = primary_opt.get("output_geometries") or []
        self.assertTrue(output_geometries, "primary_opt must carry output_geometries")
        geometry = output_geometries[0]["geometry"]
        GeometryPayload.model_validate(geometry)

        # SpeciesEntryIdentityPayload — a reactant's species_entry.
        species_entry = payload["reaction"]["reactants"][0]["species_entry"]
        SpeciesEntryIdentityPayload.model_validate(species_entry)

    def test_phase3_disk_corpus_builds_all_payloads_from_sidecar(self):
        output_doc = self._phase3_corpus()
        adapter = self._adapter("all")

        # The fixture deliberately contains no ARC calculation files. Poison
        # every legacy parser boundary as an additional proof that valid
        # sidecar evidence is sufficient.
        with mock.patch("tckdb_arc._arc_optional.require_arc_parser",
                        side_effect=AssertionError("legacy ARC parser used")):
            species = self._built_payload(
                adapter.submit_computed_species_from_output(
                    output_doc=output_doc, species_record=output_doc["species"][0],
                )
            )
            reaction = self._built_payload(
                adapter.submit_computed_reaction_from_output(
                    output_doc=output_doc, reaction_record=output_doc["reactions"][0],
                )
            )
            transition_state = self._built_payload(
                adapter.submit_computed_ts_from_output(
                    output_doc=output_doc,
                    ts_record=output_doc["transition_states"][0],
                    reaction_record=output_doc["reactions"][0],
                )
            )

        ComputedSpeciesUploadRequest.model_validate(species)
        ComputedReactionUploadRequest.model_validate(reaction)
        TransitionStateUploadRequest(**transition_state)
        for label, payload in (
            ("computed_species", species),
            ("computed_reaction", reaction),
            ("transition_state", transition_state),
        ):
            _assert_no_forbidden_keys(self, payload, label)

        # Canonical snapshots make any wire-shape change an explicit review.
        self.assertEqual(
            {
                "computed_species": "062e2397d885d7449535681e6870408bf35de961f98fb43d6bf29389d44bc17f",
                "computed_reaction": "6423aa45b7d610b29f392ea5a0a40e059e95ab90ec395b37f6033f96f0b57da6",
                "transition_state": "6ec8967ae44ed2c82eb6aa0f114086322a3e57121a0e4c9399d766719f00351c",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )


if __name__ == "__main__":
    unittest.main()
