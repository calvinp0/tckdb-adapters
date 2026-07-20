#!/usr/bin/env python3
# encoding: utf-8

"""Golden-corpus contract test (plan §3).

Builds every ``TCKDBAdapter.submit_*_from_output`` payload **offline**
(``upload=False``) over a frozen input document, then ``model_validate`` each
built payload against the *real* published ``tckdb_schemas`` models. This is the
regression that catches a shared-schema bump breaking the ARC↔TCKDB wire shape
in the adapter repo — instead of months later in an ARC run.

TODO: freeze a real ``arcbench`` ``output.yml`` (+ its ``tckdb_evidence``
sidecar once it exists) into ``tests/fixtures/golden/`` and drive it here. Until
then the corpus is built from the existing synthetic doc/record helpers in
``test_adapter`` / ``test_ts_upload`` (``_reaction_output_doc``,
``_reaction_record``, ``_fake_output_doc``, ``_full_record``, ``_compose``),
which already mirror ``arc/output.py``'s emitted shapes.

Also asserts the forbidden-key boundary: ARC-internal keys the wire must never
carry (``atom_map``, ``ts_report``, ``successful_methods``, ``server``,
``job_id``, ``relative_e0_kj_mol``) never appear anywhere in a built payload.
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
