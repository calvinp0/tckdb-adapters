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

from _contract import contract_validate
from tckdb_arc.adapter import TCKDBAdapter, _build_nasa_block
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
        contract_validate(ComputedSpeciesUploadRequest, payload)
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
        contract_validate(ComputedReactionUploadRequest, payload)
        _assert_no_forbidden_keys(self, payload, "computed_reaction")

    def test_computed_reaction_with_irc_payload_validates(self):
        adapter = self._adapter("computed_reaction")
        with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
            outcome = adapter.submit_computed_reaction_from_output(
                output_doc=_reaction_output_doc(with_irc=True),
                reaction_record=_reaction_record(),
            )
        payload = self._built_payload(outcome)
        contract_validate(ComputedReactionUploadRequest, payload)
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
        contract_validate(ComputedReactionUploadRequest, payload)
        _assert_no_forbidden_keys(self, payload, "computed_reaction+thermo")

    # --- standalone transition state --------------------------------------
    def test_transition_state_request_validates(self):
        _doc, _ts, _rxn, payload = _compose()
        contract_validate(TransitionStateUploadRequest, payload)
        _assert_no_forbidden_keys(self, payload, "transition_state")

    # --- fragment-level validation (representative, from the TS payload) --
    def test_fragment_shapes_validate(self):
        _doc, _ts, _rxn, payload = _compose()

        # CalculationWithResultsPayload — the primary opt calc.
        primary_opt = copy.deepcopy(payload["primary_opt"])
        contract_validate(CalculationWithResultsPayload, primary_opt)

        # GeometryPayload — pulled from the opt calc's output geometry.
        output_geometries = primary_opt.get("output_geometries") or []
        self.assertTrue(output_geometries, "primary_opt must carry output_geometries")
        geometry = output_geometries[0]["geometry"]
        contract_validate(GeometryPayload, geometry)

        # SpeciesEntryIdentityPayload — a reactant's species_entry.
        species_entry = payload["reaction"]["reactants"][0]["species_entry"]
        contract_validate(SpeciesEntryIdentityPayload, species_entry)

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
            reaction_outcome = adapter.submit_computed_reaction_from_output(
                output_doc=output_doc, reaction_record=output_doc["reactions"][0],
            )
            reaction = self._built_payload(reaction_outcome)
            ts_outcome = adapter.submit_computed_ts_from_output(
                output_doc=output_doc,
                ts_record=output_doc["transition_states"][0],
                reaction_record=output_doc["reactions"][0],
            )
            transition_state = self._built_payload(ts_outcome)

        # TS0 has an IRC; ARC does not export irc_level, so both TS routes
        # report that the IRC calculation's level is assumed to be opt_level.
        for outcome in (reaction_outcome, ts_outcome):
            codes = [w["code"] for w in outcome.warnings]
            self.assertEqual(codes.count("irc_level_assumed_opt_level"), 1)
            # TS0's guess is an xtb-gsm path search with no exported level.
            self.assertEqual(codes.count("ts_guess_level_not_stated"), 1)
            self.assertEqual(
                json.loads(outcome.sidecar_path.read_text())["warnings"], outcome.warnings)

        contract_validate(ComputedSpeciesUploadRequest, species)
        contract_validate(ComputedReactionUploadRequest, reaction)
        contract_validate(TransitionStateUploadRequest, transition_state)
        for label, payload in (
            ("computed_species", species),
            ("computed_reaction", reaction),
            ("transition_state", transition_state),
        ):
            _assert_no_forbidden_keys(self, payload, label)

        # Canonical snapshots make any wire-shape change an explicit review.
        #
        # computed_reaction / transition_state changed (computed_species did
        # not) for adapter 0.6.4, for one reason: TS0's chosen guess is an
        # xtb-gsm path search, and ARC exports no level for it (only the ORCA
        # NEB level, ``neb_level``), so the GSM ``path_search`` calculation is
        # no longer filed. It used to be labelled with opt_level and the opt
        # program, which is not what the path search ran at. Along with the
        # calculation, the reaction route's ``ts_opt`` loses its
        # ``optimized_from`` edge to it (the standalone route strips
        # dependencies). Putting exactly that calculation back at the front of
        # the TS's calculations (the 0.6.2 payload, kept as a fixture), and the
        # edge on the reaction route's ts_opt, reproduces the previous
        # snapshots checked next, so no other leaf changed.
        #
        # computed_reaction changed (computed_species and transition_state
        # did not) for adapter 0.6.7, for two reasons (A15 and A16). First,
        # each reaction species' conformer now carries ``label`` (the species
        # label, as the computed-species route already sends); removing that
        # one key from every reaction conformer reproduces the intermediate
        # snapshot checked next. Second (A16): this reaction is
        # the degenerate ``H2 + H <=> H + H2``, and each side used to declare
        # its own species blocks, so H2 and H were each deposited twice (two
        # conformer observations, two sp calculations, two identical thermo
        # rows). One block per ARC species label is declared now and the
        # second side references it: species[] is [r0_H2, r1_H] and
        # product_keys is [r1_H, r0_H2]. Re-declaring the second occurrence
        # of each (a deep copy of the kept block with its ``r0``/``r1`` key
        # prefix renamed to ``p1``/``p0``) and pointing product_keys at the
        # copies reproduces the previous snapshot checked right after.
        #
        # computed_species / computed_reaction changed again (transition_state
        # did not) for adapter 0.6.6: thermo and statmech now declare
        # ``energy_level_of_theory``, the linked sp's own level. Removing
        # exactly those declarations (and any calculation ``scf_stability``,
        # none in this corpus) reproduces the previous snapshots checked next,
        # so no other leaf changed.
        self.assertEqual(
            {
                "computed_species": "9e0749f3fe9d6476c63006e029401618edaabce8e14e919f720bd4adc793b908",
                "computed_reaction": "a0a566b2dcbbc6d911d5f341f8745d101bc38fff1fa3b8b5211b2abd43e8927d",
                "transition_state": "9bc66ae3b6894e9776df427601f8d8377f72c94c50abc4a8042aee092d7bd679",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )
        def without_reaction_conformer_labels(payload):
            stripped = copy.deepcopy(payload)
            for block in stripped["species"]:
                for conformer in block["conformers"]:
                    self.assertEqual(conformer.pop("label"), block["key"].split("_", 1)[1])
            return stripped

        def with_duplicate_species_restored(payload):
            restored = copy.deepcopy(payload)
            blocks = {block["key"]: block for block in restored["species"]}
            self.assertEqual(list(blocks), ["r0_H2", "r1_H"])
            self.assertEqual(restored["reactant_keys"], ["r0_H2", "r1_H"])
            self.assertEqual(restored["product_keys"], ["r1_H", "r0_H2"])

            def renamed(obj, old, new):
                if isinstance(obj, dict):
                    return {k: renamed(v, old, new) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [renamed(v, old, new) for v in obj]
                if isinstance(obj, str) and obj.startswith(old + "_"):
                    return new + obj[len(old):]
                return obj

            restored["species"] = restored["species"] + [
                renamed(blocks["r1_H"], "r1", "p0"),
                renamed(blocks["r0_H2"], "r0", "p1"),
            ]
            restored["product_keys"] = ["p0_H", "p1_H2"]
            return restored

        reaction = with_duplicate_species_restored(without_reaction_conformer_labels(reaction))
        self.assertEqual(
            {
                "computed_species": "9e0749f3fe9d6476c63006e029401618edaabce8e14e919f720bd4adc793b908",
                "computed_reaction": "6a338f5b4473db8f1506662746aa4084be9c092ed0abdc221492c010d74c1d74",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
            },
        )

        def without_energy_level_declarations(payload):
            stripped = copy.deepcopy(payload)
            removed = {"declarations": 0, "scf_stability": 0}

            def strip(obj, parent_key=None):
                if isinstance(obj, dict):
                    if parent_key in ("thermo", "statmech") and "energy_level_of_theory" in obj:
                        del obj["energy_level_of_theory"]
                        removed["declarations"] += 1
                    if "scf_stability" in obj:
                        del obj["scf_stability"]
                        removed["scf_stability"] += 1
                    for key, value in obj.items():
                        strip(value, key)
                elif isinstance(obj, list):
                    for item in obj:
                        strip(item, parent_key)

            strip(stripped)
            return stripped, removed

        species, removed_species = without_energy_level_declarations(species)
        reaction, removed_reaction = without_energy_level_declarations(reaction)
        # The corpus species has a thermo block and no statmech; the reaction
        # participants have thermo blocks.
        self.assertEqual(removed_species, {"declarations": 1, "scf_stability": 0})
        self.assertGreaterEqual(removed_reaction["declarations"], 1)
        self.assertEqual(removed_reaction["scf_stability"], 0)
        self.assertEqual(
            {
                "computed_species": "51313aab968f6693d4feaea32087029fab8aef4135cb72f223dd2957408d48e9",
                "computed_reaction": "de228c19393b31714c36dbb3321ee2839d883967fd3cc1a4bfb2df116ab54f90",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
            },
        )
        removed = json.loads(
            (Path(__file__).parent / "fixtures" / "golden"
             / "gsm_path_search_0_6_2.json").read_text())

        reaction_ts = reaction["transition_state"]
        self.assertNotIn("path_search", [c["type"] for c in reaction_ts["calculations"]])
        self.assertNotIn("depends_on", reaction_ts["calculation"])
        reaction = copy.deepcopy(reaction)
        reaction["transition_state"]["calculations"].insert(0, removed["computed_reaction"])
        reaction["transition_state"]["calculation"]["depends_on"] = [
            {"parent_calculation_key": "ts_guess", "role": "optimized_from"}]
        self.assertNotIn("path_search", [c["type"] for c in transition_state["additional_calculations"]])
        transition_state = copy.deepcopy(transition_state)
        transition_state["additional_calculations"].insert(0, removed["transition_state"])

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
        #
        # computed_species / computed_reaction changed again (transition_state
        # did not, it carries no thermo) for TCKDB #520/#529: every thermo
        # block with enthalpy content now declares
        # ``enthalpy_reference_kind="formation_298k"`` and every block with
        # entropy content carries ``reference_pressure_bar=1.01325`` (RMG's
        # hard-coded 1 atm; this fixture records no pressure). Stripping
        # exactly those two fields reproduces the previous snapshots, so no
        # other leaf changed.
        #
        # computed_species / computed_reaction changed again (transition_state
        # did not, it carries no thermo) for adapter 0.5.0: this schema-1.1
        # fixture records no ``atom_corrections_applied`` flag, and H2's raw
        # total energy (about 1.0 hartree) sits inside the magnitude guard's
        # blind spot, so its enthalpy cannot be verified as formation_298k.
        # All three H2 thermo blocks (the species bundle, r0_H2, p1_H2) lose
        # h298_kj_mol, nasa, every point h_kj_mol/g_kj_mol and
        # enthalpy_reference_kind, keeping S298, point S/Cp, bounds, provenance
        # and reference_pressure_bar. H carries no thermo. Restoring exactly
        # those fields from the fixture reproduces the previous snapshots below,
        # so no other leaf changed.
        #
        # All three changed for adapter 0.6.1 for one reason: ARC's ESS banners
        # are split into version/revision the way TCKDB's shared
        # ``SoftwareReleaseRef.normalize_composite_version`` would. This
        # fixture records ``Gaussian 16`` (no revision), so every calculation
        # software_release named gaussian goes from version "Gaussian 16" to
        # "16". The fixture carries no ``arkane_version``/``arkane_git_commit``
        # and no energy-correction records, so the Arkane release and scheme
        # software changes do not reach it. Putting "Gaussian 16" back on
        # exactly those releases reproduces the 0.6.0 snapshots checked next.
        self.assertEqual(
            {
                "computed_species": "51313aab968f6693d4feaea32087029fab8aef4135cb72f223dd2957408d48e9",
                "computed_reaction": "055c29dadcd70c8f84f58b84c2ceb8eeb3464156024e3568e8ddda0d8d4f634e",
                "transition_state": "5ab6d4a92ba569c5614353385aeaa38512320dbbadcabdc4980d08ac0d5c4b18",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )

        def with_banner_versions_restored(payload, *, releases):
            restored = copy.deepcopy(payload)
            found = []

            def collect(obj):
                if isinstance(obj, dict):
                    release = obj.get("software_release")
                    if isinstance(release, dict) and "level_of_theory" in obj:
                        found.append(release)
                    for value in obj.values():
                        collect(value)
                elif isinstance(obj, list):
                    for item in obj:
                        collect(item)

            collect(restored)
            # Calculations without an ESS banner (no ess_versions entry for
            # their job) carry no version, before and after.
            versioned = [release for release in found if "version" in release]
            self.assertEqual(len(versioned), releases)
            for release in found:
                if release in versioned:
                    self.assertEqual(release, {"name": "gaussian", "version": "16"})
                    release["version"] = "Gaussian 16"
                else:
                    self.assertEqual(set(release), {"name"})
            return restored

        species = with_banner_versions_restored(species, releases=3)
        reaction = with_banner_versions_restored(reaction, releases=13)
        transition_state = with_banner_versions_restored(transition_state, releases=5)

        # Adapter 0.6.0 (tckdb-schemas 0.52, the producer
        # contract). First, by the maintainer's decision, identity/provenance
        # defaults ARC does not state are no longer sent:
        # ``species_entry.electronic_state_kind="ground"`` (every species
        # entry, including the standalone TS payload's reaction participants)
        # and ``freq_scale_factor.scale_kind="fundamental"`` (every
        # frequency-scale-factor reference; this fixture has none, so only
        # the species entries move). Putting exactly those back
        # reproduces the intermediate snapshots checked next.
        self.assertEqual(
            {
                "computed_species": "09c2c0d739d49c0964e9aa7ffe492cb6a3803f2ea6c5290955e26bf37365112b",
                "computed_reaction": "bd6912d7437b8cd93b927320e4d848b71d7f28458d19a56320523e2f95db06c0",
                "transition_state": "dd1d10f4f00a844cc37f1731b89d4d5e1d59d0059792a239762ffff7249d6ce3",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )

        def with_identity_defaults_restored(payload, *, species_entries, scale_factors):
            restored = copy.deepcopy(payload)
            entries, factors = [], []

            def collect(obj):
                if isinstance(obj, dict):
                    if isinstance(obj.get("species_entry"), dict):
                        entries.append(obj["species_entry"])
                    if isinstance(obj.get("freq_scale_factor"), dict):
                        factors.append(obj["freq_scale_factor"])
                    for value in obj.values():
                        collect(value)
                elif isinstance(obj, list):
                    for item in obj:
                        collect(item)

            collect(restored)
            self.assertEqual((len(entries), len(factors)), (species_entries, scale_factors))
            for entry in entries:
                self.assertNotIn("electronic_state_kind", entry)
                entry["electronic_state_kind"] = "ground"
            for factor in factors:
                self.assertNotIn("scale_kind", factor)
                factor["scale_kind"] = "fundamental"
            return restored

        species = with_identity_defaults_restored(species, species_entries=1, scale_factors=0)
        reaction = with_identity_defaults_restored(reaction, species_entries=4, scale_factors=0)
        transition_state = with_identity_defaults_restored(
            transition_state, species_entries=4, scale_factors=0)

        # Second, for two reasons and no others:
        #   - this fixture records no ``standard_state_pressure_pa``, so the
        #     three H2 thermo blocks (species bundle, r0_H2, p1_H2) no longer
        #     carry ``reference_pressure_bar``: it is omitted, never
        #     defaulted to RMG's 1 atm, and reported as
        #     ``thermo_reference_pressure_not_stated`` (transition_state has
        #     no thermo, so this part does not touch it);
        #   - ``path_search_result.converged`` is omitted: ARC's TS-guess
        #     ``success`` means only that the output file exists, not that
        #     the string converged. TS0's GSM path search appears on the
        #     reaction route's TS block and in the standalone TS payload,
        #     not in computed_species.
        # Putting back exactly ``reference_pressure_bar=1.01325`` on each H2
        # block and ``converged=True`` on each path-search result reproduces
        # the previous snapshots, so no other leaf changed.
        self.assertEqual(
            {
                "computed_species": "19a51cab7db268f8b5fcef78411c89df77ad8cbe7d9547318fd9865002ba0525",
                "computed_reaction": "6eef7980088c2075f7cb5f7fc6491c13921c0aa07329b8e8b1a9dfa05d60aad1",
                "transition_state": "5da87ad131e86d99b028fe06661fa323322c15b86bb91c98c36c4bf6850e2336",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )

        def with_0_5_claims_restored(payload, *, thermo_blocks, path_searches):
            restored = copy.deepcopy(payload)
            blocks = [restored.get("thermo")] + [
                sp.get("thermo") for sp in restored.get("species") or []
            ]
            blocks = [block for block in blocks if block is not None]
            self.assertEqual(len(blocks), thermo_blocks)
            for block in blocks:
                self.assertNotIn("reference_pressure_bar", block)
                self.assertIn("s298_j_mol_k", block)
                block["reference_pressure_bar"] = 1.01325
            results = []

            def collect(obj):
                if isinstance(obj, dict):
                    if isinstance(obj.get("path_search_result"), dict):
                        results.append(obj["path_search_result"])
                    for value in obj.values():
                        collect(value)
                elif isinstance(obj, list):
                    for item in obj:
                        collect(item)

            collect(restored)
            self.assertEqual(len(results), path_searches)
            for result in results:
                self.assertNotIn("converged", result)
                result["converged"] = True
            return restored

        species = with_0_5_claims_restored(species, thermo_blocks=1, path_searches=0)
        reaction = with_0_5_claims_restored(reaction, thermo_blocks=2, path_searches=1)
        transition_state = with_0_5_claims_restored(
            transition_state, thermo_blocks=0, path_searches=1)
        self.assertEqual(
            {
                "computed_species": "194f02a5e8a6069ec06c8ef0f4424878aa98e86de11f29f3d862db258c13cfb7",
                "computed_reaction": "d769b48380b3a82b1b0245985aea64f4966289460f1cc278badb8b2f97aae78b",
                "transition_state": "9f9ba6edb1e88589595b95782474b9d3ad6c8b912c9c00c627b011512b8cc69b",
            },
            {
                "computed_species": self._canonical_sha256(species),
                "computed_reaction": self._canonical_sha256(reaction),
                "transition_state": self._canonical_sha256(transition_state),
            },
        )
        h2_thermo = output_doc["species"][0]["thermo"]
        self.assertEqual(output_doc["species"][0]["label"], "H2")

        def with_h2_enthalpy_restored(payload):
            restored = copy.deepcopy(payload)
            blocks = [restored.get("thermo")] + [
                sp.get("thermo") for sp in restored.get("species") or []
            ]
            blocks = [block for block in blocks if block is not None]
            self.assertTrue(blocks)
            for block in blocks:
                self.assertFalse({"h298_kj_mol", "nasa", "enthalpy_reference_kind"} & set(block))
                self.assertEqual(block["s298_j_mol_k"], h2_thermo["s298_j_mol_k"])
                block["h298_kj_mol"] = float(h2_thermo["h298_kj_mol"])
                block["nasa"] = _build_nasa_block(h2_thermo["nasa_low"], h2_thermo["nasa_high"])
                block["enthalpy_reference_kind"] = "formation_298k"
                self.assertEqual(len(block["points"]), len(h2_thermo["thermo_points"]))
                for point, record in zip(block["points"], h2_thermo["thermo_points"]):
                    self.assertEqual(point["temperature_k"], record["temperature_k"])
                    self.assertFalse({"h_kj_mol", "g_kj_mol"} & set(point))
                    point["h_kj_mol"] = record["h_kj_mol"]
                    point["g_kj_mol"] = record["g_kj_mol"]
            return restored

        species = with_h2_enthalpy_restored(species)
        reaction = with_h2_enthalpy_restored(reaction)

        def without_thermo_state(payload):
            stripped = copy.deepcopy(payload)
            blocks = [stripped.get("thermo")] + [
                sp.get("thermo") for sp in stripped.get("species") or []
            ]
            for block in blocks:
                if block is not None:
                    self.assertEqual(block["enthalpy_reference_kind"], "formation_298k")
                    self.assertEqual(block["reference_pressure_bar"], 1.01325)
                    block.pop("enthalpy_reference_kind")
                    block.pop("reference_pressure_bar")
            return stripped

        self.assertEqual(
            {
                "computed_species": "fa388b7acdb06616b1b7701705501b12aeb20344f902796d8bc866d80d89487b",
                "computed_reaction": "f897dcea26df630484b514f92c359355b50edd158890e9dd834d8f525a2e1897",
            },
            {
                "computed_species": self._canonical_sha256(without_thermo_state(species)),
                "computed_reaction": self._canonical_sha256(without_thermo_state(reaction)),
            },
        )
        previous_reaction = without_thermo_state(reaction)
        for participant in previous_reaction["species"]:
            if "thermo" in participant:
                participant["thermo"].pop("source_calculations", None)
        self.assertEqual(
            self._canonical_sha256(previous_reaction),
            "72ae74b062dbd15825bc695e8a1f71605527cdc5115f8718e3f907769548998e",
        )
        self.assertEqual(
            {
                "computed_species": "a36d0882d0d36748d8711fd42868976b4411e25a3307b4693891bc5a35f11731",
                "computed_reaction": "343ff4cd6cf8572820525b878b7b2f08c7741b35c39513dd8e02f3e9daa796d0",
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
