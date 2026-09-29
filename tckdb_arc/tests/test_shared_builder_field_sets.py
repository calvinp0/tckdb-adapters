#!/usr/bin/env python3
# encoding: utf-8

"""Guard shared builders against drift between current upload roots.

Both thermo roots now accept source calculations. Keep explicit target
checks and exercise each root so future differences fail at this seam.
"""

import unittest

from pydantic import ValidationError as PydanticValidationError

from tckdb_arc.adapter import (
    _REACTION_FLAT_RESULT_FIELDS,
    _REACTION_MODE_FIELDS_HANDLED,
    _REACTION_MODE_FIELDS_NOT_CARRIED,
    _REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE,
    _REACTION_RESULT_FIELDS_NOT_CARRIED,
    _STATMECH_FIELDS_BY_TARGET,
    _THERMO_FIELDS_BY_TARGET,
    _build_slim_torsions,
    _build_statmech_block_for_species,
    _build_thermo_block,
    _flatten_result_fields,
    _freq_result_payload,
    _opt_result_payload,
    _sp_result_payload,
)

# Real published wire-contract models (test-only dependency, same as
# test_golden_corpus.py).
from tckdb_schemas.fragments.calculation import (
    FreqResultPayload,
    FrequencyModePayload,
    OptResultPayload,
    SPResultPayload,
)
from tckdb_schemas.workflows.computed_species_upload import (
    StatmechInBundle,
    StatmechTorsionInBundle,
    ThermoInBundle,
)
from tckdb_schemas.workflows.computed_reaction_upload import (
    BundleStatmechIn,
    BundleStatmechTorsionIn,
    BundleThermoIn,
    ComputedReactionCalculationIn,
)

from test_adapter import _fake_output_doc


# ---------------------------------------------------------------------
# Realistic ARC-shaped fixtures (verified against arc/output.py):
#   - thermo record shape: arc/output.py::_thermo_to_dict
#   - statmech record shape: arc/output.py::_statmech_to_dict /
#     _get_torsions (mirrors the fixtures already exercised in
#     TestComputedSpeciesStatmechBaseFields / TestComputedReactionStatmechBaseFields
#     in test_adapter.py).
# Deliberately maximal: every optional field the builder knows how to
# translate is populated, so the builder's *actual* output surface is
# exercised, not just a hand-picked subset.
# ---------------------------------------------------------------------

_THERMO_RECORD = {
    "h298_kj_mol": -235.1,
    "s298_j_mol_k": 282.6,
    "tmin_k": 100.0,
    "tmax_k": 5000.0,
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
    ],
}

_STATMECH_INPUT = {
    "e0_kj_mol": 12.5,
    "spin_multiplicity": 1,
    "optical_isomers": 2,
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


def _statmech_output_doc():
    """A realistic output_doc carrying a frequency-scale-factor, mirroring
    ``TestComputedSpeciesStatmechFreqScaleFactor._doc_with_fsf`` in
    test_adapter.py -- needed so ``_build_statmech_block_for_species``
    also exercises ``freq_scale_factor``, the one field it derives from
    ``output_doc`` rather than the species record."""
    doc = _fake_output_doc()
    doc["freq_scale_factor"] = 0.961
    doc["freq_scale_factor_source"] = "J. Chem. Theory Comput. 2010, 6, 2872"
    doc["freq_level"] = {"method": "wb97xd", "basis": "def2-tzvp", "software": "gaussian"}
    return doc


class TestDeclaredFieldSetsAreSubsetsOfRealModels(unittest.TestCase):
    """Static check: the adapter's own allow-lists never claim more than
    the real, currently-installed TCKDB schema actually accepts.

    This is what catches a future TCKDB schema bump that *drops* a field
    the adapter still declares as emittable: ``model_fields`` is read
    from the real imported model, so it tracks schema upgrades
    automatically rather than rotting like a hand-copied list would.
    """

    def test_thermo_species_allowlist_subset_of_ThermoInBundle(self):
        self.assertLessEqual(
            _THERMO_FIELDS_BY_TARGET["ThermoInBundle"],
            set(ThermoInBundle.model_fields),
        )

    def test_thermo_reaction_allowlist_subset_of_BundleThermoIn(self):
        self.assertLessEqual(
            _THERMO_FIELDS_BY_TARGET["BundleThermoIn"],
            set(BundleThermoIn.model_fields),
        )

    def test_statmech_species_allowlist_subset_of_StatmechInBundle(self):
        self.assertLessEqual(
            _STATMECH_FIELDS_BY_TARGET["StatmechInBundle"],
            set(StatmechInBundle.model_fields),
        )

    def test_statmech_reaction_allowlist_subset_of_BundleStatmechIn(self):
        self.assertLessEqual(
            _STATMECH_FIELDS_BY_TARGET["BundleStatmechIn"],
            set(BundleStatmechIn.model_fields),
        )


class TestBuilderOutputValidatesAgainstItsOwnTargetModel(unittest.TestCase):
    """Dynamic check: actually run each shared builder for each of its two
    target models with a maximal, ARC-shaped record, and validate the
    *builder's own output* -- not just the declared allow-list -- with the
    real model's ``model_validate``.

    This is the check that would have caught the original bug even if
    the ``target_model`` plumbing had been added but wired backwards (a
    builder that claims to target one root while actually emitting the
    other's fields): the allow-list-only checks above only test the
    table entries against each other, not what the code actually does.
    """

    # ---- thermo -------------------------------------------------------

    def test_thermo_builder_for_species_root_validates_as_ThermoInBundle(self):
        block = _build_thermo_block(
            _THERMO_RECORD,
            calc_keys_by_role={"opt": "opt", "freq": "freq", "sp": "sp"},
            target_model="ThermoInBundle",
        )
        self.assertIsNotNone(block)
        self.assertIn("source_calculations", block, "fixture should exercise this field")
        ThermoInBundle.model_validate(block)  # must not raise

    def test_thermo_builder_for_reaction_root_validates_as_BundleThermoIn(self):
        # The reaction root must retain links in its participant namespace.
        block = _build_thermo_block(
            _THERMO_RECORD,
            calc_keys_by_role={"opt": "r0_opt", "freq": "r0_freq", "sp": "r0_sp"},
            target_model="BundleThermoIn",
        )
        self.assertIsNotNone(block)
        self.assertEqual(
            [entry["calculation_key"] for entry in block["source_calculations"]],
            ["r0_opt", "r0_freq", "r0_sp"],
        )
        BundleThermoIn.model_validate(block)  # must not raise

    # ---- statmech -------------------------------------------------------

    def _species_statmech_block(self, target_model):
        return _build_statmech_block_for_species(
            output_doc=_statmech_output_doc(),
            species_record={"statmech": _STATMECH_INPUT},
            calc_keys_by_role={"opt": "opt", "freq": "freq", "sp": "sp"},
            workflow_tool_release=None,
            target_model=target_model,
        )

    def test_statmech_builder_for_species_root_validates_as_StatmechInBundle(self):
        block = self._species_statmech_block("StatmechInBundle")
        self.assertIsNotNone(block)
        # Exercise the maximal surface this builder can currently produce.
        for field in _STATMECH_FIELDS_BY_TARGET["StatmechInBundle"]:
            self.assertIn(field, block, f"fixture should exercise {field!r}")
        StatmechInBundle.model_validate(block)  # must not raise

    def test_statmech_builder_for_reaction_root_validates_as_BundleStatmechIn(self):
        block = _build_statmech_block_for_species(
            output_doc=_statmech_output_doc(),
            species_record={"statmech": _STATMECH_INPUT},
            calc_keys_by_role={"opt": "r0_opt", "freq": "r0_freq", "sp": "r0_sp"},
            workflow_tool_release=None,
            target_model="BundleStatmechIn",
        )
        self.assertIsNotNone(block)
        for field in _STATMECH_FIELDS_BY_TARGET["BundleStatmechIn"]:
            self.assertIn(field, block, f"fixture should exercise {field!r}")
        BundleStatmechIn.model_validate(block)  # must not raise


class TestCurrentThermoRootParity(unittest.TestCase):
    def test_both_roots_preserve_thermo_provenance(self):
        for target, model in (("ThermoInBundle", ThermoInBundle),
                              ("BundleThermoIn", BundleThermoIn)):
            block = _build_thermo_block(
                _THERMO_RECORD,
                calc_keys_by_role={"opt": "r0_opt", "freq": "r0_freq", "sp": "r0_sp"},
                target_model=target,
            )
            result = model.model_validate(block)
            self.assertEqual(len(result.source_calculations), 3)


# ---------------------------------------------------------------------
# D1 fixtures: realistic ARC-shaped opt/freq/sp records for the
# wrapped-result -> flat-result completeness guard below. The freq
# record is deliberately a maximal TS shape (two imaginary modes, a
# designated reaction coordinate, an ``unassigned`` disposition on the
# other) so the dynamic tests exercise every field
# ``_freq_result_payload`` can currently emit -- mirroring
# ``test_ts_two_imaginary_modes_designates_reaction_coordinate`` in
# test_adapter.py, which exercises the same shape end-to-end through
# the real submit path.
# ---------------------------------------------------------------------

_OPT_RECORD = {
    "opt_converged": True,
    "opt_n_steps": 12,
    "opt_final_energy_hartree": -154.321,
}

_SP_RECORD = {
    "sp_energy_hartree": -154.4567,
}

_TS_FREQ_RECORD = {
    "label": "TS0",
    "is_ts": True,
    "freq_n_imag": 2,
    "imag_freq_cm1": -1320.5,
    "imaginary_frequencies_cm1": [-1320.5, -30.0],
    "zpe_hartree": 0.045,
    "statmech": {"harmonic_frequencies_cm1": [800.0, 1500.0]},
}


def _reaction_calc_shell(calc_type: str) -> dict:
    """The minimal ``ComputedReactionCalculationIn``-required scaffolding
    (``key``, ``type``, ``software_release``, ``level_of_theory``) a
    wrapped result dict gets merged into before/after
    :func:`_flatten_result_fields`."""
    return {
        "key": f"r0_{calc_type}",
        "type": calc_type,
        "software_release": {"name": "Gaussian"},
        "level_of_theory": {"method": "wb97xd", "basis": "def2-tzvp"},
    }


class TestReactionResultFlatteningCompletenessGuard(unittest.TestCase):
    """Structural regression test for the third shared-builder/two-root
    pair (see this module's docstring): ``_calculation_payload`` always
    builds the species-shape wrapped ``opt_result``/``freq_result``/
    ``sp_result`` dicts; ``_flatten_result_fields`` rewrites them to the
    computed-reaction route's flat fields via the hand-maintained
    ``_REACTION_FLAT_RESULT_FIELDS`` table (plus two companion tables for
    fields handled outside the generic copy loop, or deliberately not
    carried at all).

    Static half: every field the real ``OptResultPayload`` /
    ``FreqResultPayload`` / ``SPResultPayload`` / ``FrequencyModePayload``
    models can carry is accounted for by exactly one of those tables --
    so a future TCKDB schema bump that adds a field fails here instead
    of the field silently vanishing on the reaction route.

    Dynamic half: (a) direct proof the guard fires -- a wrapped result
    (or per-mode entry) carrying a field none of the tables know about
    raises ``ValueError`` instead of being dropped, reproducing the
    reviewer's break without touching production code; (b) each
    builder's actual maximal output, run through the real flatten step,
    validates against the real ``ComputedReactionCalculationIn``.
    """

    # ---- static: adapter tables vs. the real wrapped-result models ----

    def test_opt_result_top_level_fields_fully_accounted_for(self):
        known = (
            set(_REACTION_FLAT_RESULT_FIELDS["opt_result"])
            | _REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE.get("opt_result", frozenset())
            | set(_REACTION_RESULT_FIELDS_NOT_CARRIED.get("opt_result", {}))
        )
        self.assertEqual(known, set(OptResultPayload.model_fields))

    def test_freq_result_top_level_fields_fully_accounted_for(self):
        known = (
            set(_REACTION_FLAT_RESULT_FIELDS["freq_result"])
            | _REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE.get("freq_result", frozenset())
            | set(_REACTION_RESULT_FIELDS_NOT_CARRIED.get("freq_result", {}))
        )
        self.assertEqual(known, set(FreqResultPayload.model_fields))

    def test_sp_result_top_level_fields_fully_accounted_for(self):
        known = (
            set(_REACTION_FLAT_RESULT_FIELDS["sp_result"])
            | _REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE.get("sp_result", frozenset())
            | set(_REACTION_RESULT_FIELDS_NOT_CARRIED.get("sp_result", {}))
        )
        self.assertEqual(known, set(SPResultPayload.model_fields))

    def test_frequency_mode_fields_fully_accounted_for(self):
        known = set(_REACTION_MODE_FIELDS_HANDLED) | set(_REACTION_MODE_FIELDS_NOT_CARRIED)
        self.assertEqual(known, set(FrequencyModePayload.model_fields))

    # ---- dynamic: the guard actually fires, not just the tables -------

    def test_flatten_raises_on_unmapped_opt_result_field(self):
        """D1 regression demo: an ``opt_result`` field the table doesn't
        know about must raise, not silently vanish."""
        calc = _reaction_calc_shell("opt")
        calc["opt_result"] = {"converged": True, "totally_new_field": 1.0}
        with self.assertRaises(ValueError) as ctx:
            _flatten_result_fields(calc)
        self.assertIn("totally_new_field", str(ctx.exception))

    def test_flatten_raises_on_unmapped_freq_result_field(self):
        calc = _reaction_calc_shell("freq")
        calc["freq_result"] = {"n_imag": 0, "totally_new_field": "x"}
        with self.assertRaises(ValueError) as ctx:
            _flatten_result_fields(calc)
        self.assertIn("totally_new_field", str(ctx.exception))

    def test_flatten_raises_on_unmapped_sp_result_field(self):
        calc = _reaction_calc_shell("sp")
        calc["sp_result"] = {
            "electronic_energy_hartree": -1.0, "totally_new_field": "x",
        }
        with self.assertRaises(ValueError) as ctx:
            _flatten_result_fields(calc)
        self.assertIn("totally_new_field", str(ctx.exception))

    def test_flatten_raises_on_unmapped_mode_field(self):
        """Same demo, one level deeper: a per-mode field with no flat
        home on the reaction route (see
        ``_REACTION_MODE_FIELDS_NOT_CARRIED``'s docstring for why)."""
        calc = _reaction_calc_shell("freq")
        calc["freq_result"] = {
            "n_imag": 1,
            "modes": [{
                "mode_index": 1, "frequency_cm1": -100.0, "is_imaginary": True,
                "totally_new_mode_field": "x",
            }],
        }
        with self.assertRaises(ValueError) as ctx:
            _flatten_result_fields(calc)
        self.assertIn("totally_new_mode_field", str(ctx.exception))

    # ---- dynamic: builder output -> flatten -> real model_validate ----

    def test_opt_result_builder_output_flattens_and_validates(self):
        result = _opt_result_payload(_OPT_RECORD)
        self.assertIsNotNone(result)
        calc = _reaction_calc_shell("opt")
        calc["opt_result"] = dict(result)
        _flatten_result_fields(calc)
        self.assertNotIn("opt_result", calc)
        self.assertEqual(calc["opt_converged"], True)
        ComputedReactionCalculationIn.model_validate(calc)  # must not raise

    def test_sp_result_builder_output_flattens_and_validates(self):
        result = _sp_result_payload(_SP_RECORD)
        self.assertIsNotNone(result)
        calc = _reaction_calc_shell("sp")
        calc["sp_result"] = dict(result)
        _flatten_result_fields(calc)
        self.assertNotIn("sp_result", calc)
        ComputedReactionCalculationIn.model_validate(calc)  # must not raise

    def test_freq_result_builder_output_flattens_and_validates(self):
        """Maximal freq surface: two imaginary modes, a designated
        reaction coordinate, and an ``unassigned`` disposition on the
        other -- exercising both the mapped scalar fields and the
        ``modes``-derived ``freq_frequencies_cm1`` /
        ``freq_imaginary_dispositions`` pair through the real flatten
        step."""
        result = _freq_result_payload(_TS_FREQ_RECORD)
        self.assertIsNotNone(result)
        self.assertIn("reaction_coordinate_mode_index", result,
                       "fixture should exercise this field")
        self.assertTrue(
            any(m.get("imaginary_disposition") for m in result["modes"]),
            "fixture should exercise imaginary_disposition",
        )
        calc = _reaction_calc_shell("freq")
        calc["freq_result"] = dict(result)
        _flatten_result_fields(calc)
        self.assertNotIn("freq_result", calc)
        self.assertIn("freq_frequencies_cm1", calc)
        self.assertIn("freq_reaction_coordinate_mode_index", calc)
        self.assertIn("freq_imaginary_dispositions", calc)
        ComputedReactionCalculationIn.model_validate(calc)  # must not raise


class TestTorsionSubBlockSharedAcrossTargets(unittest.TestCase):
    """D5: ``_build_slim_torsions`` feeds both ``StatmechInBundle.torsions``
    (species root, entries shaped ``StatmechTorsionInBundle``) and
    ``BundleStatmechIn.torsions`` (reaction root, entries shaped
    ``BundleStatmechTorsionIn``) from one shared builder that -- unlike
    the thermo/statmech blocks above -- is not itself
    ``target_model``-gated: it emits one shape for both roots, on the
    standing assumption that ``StatmechTorsionInBundle`` and
    ``BundleStatmechTorsionIn`` stay field-for-field identical.

    They do today (``torsion_index``, ``symmetry_number``,
    ``treatment_kind``, ``dimension``, ``top_description``,
    ``source_scan_calculation_key``, ``coordinates`` -- 7 fields, both
    sides). This test exists so a future divergence between the two
    roots' torsion shapes is caught here the same way it would be for
    thermo/statmech, rather than shipping silently. No behaviour change
    is expected today.
    """

    def test_torsion_field_sets_match_across_targets(self):
        self.assertEqual(
            set(StatmechTorsionInBundle.model_fields),
            set(BundleStatmechTorsionIn.model_fields),
        )

    def test_torsion_builder_output_validates_against_both_targets(self):
        torsions = _build_slim_torsions(_STATMECH_INPUT["torsions"])
        self.assertTrue(torsions, "fixture should exercise at least one torsion")
        for entry in torsions:
            StatmechTorsionInBundle.model_validate(entry)  # must not raise
            BundleStatmechTorsionIn.model_validate(entry)  # must not raise


if __name__ == "__main__":
    unittest.main()
