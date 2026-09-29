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

    def test_computed_reaction_with_per_species_thermo_payload_validates(self):
        """Regression coverage for the shared-builder/two-roots bug: a
        computed-reaction upload whose reactant/product species carry
        per-species thermo (the ARC ``output.yml`` shape from
        ``arc/output.py::_thermo_to_dict``) must build and validate
        cleanly end-to-end. Before the fix, ``_build_thermo_block``
        always emitted ``source_calculations`` -- a field
        ``ThermoInBundle`` (the computed-species root) accepts but
        ``BundleThermoIn`` (this, the computed-reaction root) does not
        -- and every such upload 422'd with "Extra inputs are not
        permitted". This is exactly the gap the golden corpus previously
        had no coverage for.
        """
        doc = _reaction_output_doc()
        thermo_by_label = {
            "CHO": {
                "h298_kj_mol": 43.2, "s298_j_mol_k": 224.6,
                "tmin_k": 100.0, "tmax_k": 5000.0,
                "nasa_low": {"tmin_k": 100.0, "tmax_k": 1000.0,
                             "coeffs": [4.0, -1e-3, 2e-6, -1e-9, 4e-13, 5100.0, 1.0]},
                "nasa_high": {"tmin_k": 1000.0, "tmax_k": 5000.0,
                              "coeffs": [3.5, 1e-3, -2e-7, 1e-11, -3e-15, 5200.0, 5.0]},
            },
            "CH4": {
                "h298_kj_mol": -74.6, "s298_j_mol_k": 186.3,
                "tmin_k": 100.0, "tmax_k": 5000.0,
                "nasa_low": {"tmin_k": 100.0, "tmax_k": 1000.0,
                             "coeffs": [4.1, -1e-3, 2e-6, -1e-9, 4e-13, -9000.0, 1.0]},
                "nasa_high": {"tmin_k": 1000.0, "tmax_k": 5000.0,
                              "coeffs": [3.6, 1e-3, -2e-7, 1e-11, -3e-15, -8900.0, 5.0]},
            },
        }
        for sp in doc["species"]:
            thermo = thermo_by_label.get(sp["label"])
            if thermo is not None:
                sp["thermo"] = thermo

        adapter = self._adapter("computed_reaction")
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=doc["reactions"][0],
            )
        payload = self._built_payload(outcome)

        # Sanity: the thermo blocks actually made it into the payload,
        # not just bypassed (a vacuous validation pass would prove
        # nothing).
        thermo_species_keys = {
            sp["key"] for sp in payload["species"] if "thermo" in sp
        }
        self.assertEqual(thermo_species_keys, {"r0_CHO", "r1_CH4"})
        for sp in payload["species"]:
            if "thermo" in sp:
                sources = sp["thermo"]["source_calculations"]
                own_keys = {calc["key"] for calc in sp["calculations"]}
                own_keys.update(c["calculation"]["key"] for c in sp["conformers"])
                self.assertTrue(sources)
                self.assertTrue(all(s["calculation_key"] in own_keys for s in sources))

        # The real validation: the full payload, real model.
        ComputedReactionUploadRequest.model_validate(payload)
        _assert_no_forbidden_keys(self, payload, "computed_reaction+thermo")

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
        #
        # computed_reaction / transition_state changed (computed_species
        # did not) for a reviewed reason: the fixture's TS0 freq record
        # has freq_n_imag=1, freq_imag_freq_cm1=-900.0, and no
        # statmech.harmonic_frequencies_cm1 at all. Reconciling n_imag
        # with the deposited frequency list used to be gated entirely
        # behind a harmonic-frequencies source being present, so this
        # combination built no ``modes``/``freq_frequencies_cm1`` at all
        # even though the single imaginary mode was fully known from the
        # scalar field. Fixing D4(a) (n_imag>1 with no statmech source --
        # reachable via ARC's own ts_guesses fallback,
        # arc/output.py::_get_imaginary_freqs) required reinsertion to no
        # longer depend on a harmonic source being present, and that
        # generalization also (correctly) reconciles this n_imag==1 case:
        # the payload now additionally carries
        # ``freq_frequencies_cm1: [-900.0]``, a strict completeness gain
        # with no other field changed. See
        # ``_freq_result_payload``/D4 in the adapter for the full
        # rationale.
        #
        # computed_species / computed_reaction changed again (transition_state
        # did not) for a second, later, reviewed reason: this fixture's H2
        # species record previously had no ``thermo`` subdict at all --
        # the golden corpus had zero coverage of per-species thermo on the
        # computed-reaction route, which is exactly why a shared-builder
        # bug (``_build_thermo_block`` emitting ``source_calculations``
        # into ``BundleThermoIn``, which has no such column) shipped to
        # production instead of failing here. A ``thermo`` block was added
        # to the H2 species record (h298_kj_mol, s298_j_mol_k, tmin_k,
        # tmax_k, nasa_low/nasa_high, thermo_points -- the exact shape
        # ``arc/output.py::_thermo_to_dict`` writes). Field-by-field
        # effect on each payload:
        #   - computed_species: H2 is species[0], built standalone via
        #     ``submit_computed_species_from_output``. Its bundle gains a
        #     top-level ``thermo`` key with h298_kj_mol, s298_j_mol_k,
        #     tmin_k, tmax_k, nasa, points, AND source_calculations
        #     (referencing this bundle's own opt/freq/sp calc keys) --
        #     ``ThermoInBundle`` (the computed-species root) has a column
        #     for that provenance and the producer correctly populates it.
        #   - computed_reaction: H2 is both a reactant (key "r0_H2") and a
        #     product (key "p1_H2") of "H2 + H <=> H + H2". Both species
        #     blocks gain a ``thermo`` key with h298_kj_mol, s298_j_mol_k,
        #     tmin_k, tmax_k, nasa, points -- but NOT source_calculations.
        #     ``BundleThermoIn`` (the computed-reaction root) has no
        #     column for it at all; before the fix, the producer would
        #     have emitted it anyway and the server would have 422'd with
        #     "Extra inputs are not permitted" for
        #     ``species.0.thermo.source_calculations``. This snapshot is
        #     the proof the fix closes that gap through the real,
        #     disk-corpus-driven code path, not just a synthetic repro.
        #   - transition_state: untouched. The TS route never calls
        #     ``_build_thermo_block`` (transition states have no thermo
        #     field on ``BundleTransitionStateIn``), so adding thermo to
        #     H2 cannot affect it -- confirmed by the unchanged hash.
        #
        # computed_reaction / transition_state changed again (computed_species
        # did not) for a third reason: PHASE_C_PLAN.md C-3 unified two
        # independently-rounded Hartree->kJ/mol constants
        # (adapter.py's inline ``_HARTREE_TO_KJ_MOL = 2625.4996`` vs.
        # ``_vendor.E_h_kJmol == 2625.4998583629967``) onto the single
        # vendored one. Before the fix, ONLY ``irc_result.points[].
        # relative_energy_kj_mol`` (built by ``_build_irc_result_payload``)
        # used the inline constant; ``path_search_result.points[].
        # relative_energy_kj_mol`` (built by
        # ``_build_path_search_result_payload``) already imported
        # ``_vendor.E_h_kJmol`` directly and was never affected -- verified
        # against the pre-fix code itself (``git show
        # HEAD:tckdb_arc/tckdb_arc/adapter.py``, HEAD 5820/5943/5999 for the
        # inline constant's definition and IRC use, 6478 for path-search's
        # pre-existing use of ``_vendor.E_h_kJmol``), not inferred. This
        # fixture's TS0 has real IRC (sidecar-evidence-sourced, both
        # branches), so both the reaction route's inline transition_state
        # block and the standalone transition_state payload carry
        # ``irc_result`` points computed with the old inline constant.
        # Rebuilt against the actual pre-fix and post-fix code (same
        # fixture; the pre-fix build reconstructed by running
        # ``_build_irc_result_payload`` with ``_vendor.E_h_kJmol``
        # temporarily patched back to 2625.4996, leaving
        # ``_build_path_search_result_payload`` untouched -- matching what
        # the pre-fix code actually did): the ONLY differences anywhere in
        # either payload are four ``relative_energy_kj_mol`` leaves, all
        # under ``irc_result`` --
        # ``transition_state.calculations[3].irc_result.points[0]``,
        # ``.points[1]`` (reaction route) and the matching two under the
        # standalone TS's ``additional_calculations[3]`` -- each shifting
        # by the ~9.84e-8 relative amount the constant unification implies
        # (e.g. -315.0599520000003 -> -315.05998300355986). No
        # ``path_search_result`` leaf changed, and no other field changed.
        # computed_species has no IRC/path_search anywhere (species never
        # carry either), so it is untouched -- confirmed by the unchanged
        # hash.
        # Current schema 0.51 accepts reaction thermo source links. The
        # new hash differs only by those links; earlier commentary above
        # records the historical 0.22 behavior, not today's contract.
        previous_reaction = copy.deepcopy(reaction)
        for participant in previous_reaction["species"]:
            if "thermo" in participant:
                participant["thermo"].pop("source_calculations", None)
        self.assertEqual(
            self._canonical_sha256(previous_reaction),
            "72ae74b062dbd15825bc695e8a1f71605527cdc5115f8718e3f907769548998e",
        )
        self.assertEqual(
            {
                "computed_species": "fa388b7acdb06616b1b7701705501b12aeb20344f902796d8bc866d80d89487b",
                "computed_reaction": "f897dcea26df630484b514f92c359355b50edd158890e9dd834d8f525a2e1897",
                "transition_state": "9f9ba6edb1e88589595b95782474b9d3ad6c8b912c9c00c627b011512b8cc69b",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )

        # Both current roots retain provenance. Each source must refer to
        # the participant's own calculations, including repeated species.
        for sp_block in reaction["species"]:
            if "thermo" in sp_block:
                sources = sp_block["thermo"]["source_calculations"]
                own_keys = {calc["key"] for calc in sp_block["calculations"]}
                own_keys.update(c["calculation"]["key"] for c in sp_block["conformers"])
                self.assertTrue(sources)
                self.assertTrue(all(s["calculation_key"] in own_keys for s in sources))
        self.assertIn("thermo", species)
        self.assertIn("source_calculations", species["thermo"])


if __name__ == "__main__":
    unittest.main()
