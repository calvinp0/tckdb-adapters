#!/usr/bin/env python3
# encoding: utf-8

"""Unit tests for tckdb_arc.idempotency."""

import re
import unittest

from tckdb_arc.idempotency import (
    ArtifactIdempotencyInputs,
    IdempotencyInputs,
    build_artifact_idempotency_key,
    build_idempotency_key,
)


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{16,200}$")


def _inputs(**overrides):
    base = dict(
        project_label="projA",
        species_label="ethanol",
        conformer_label="conf0",
        payload_kind="conformer_calculation",
        payload={"hello": "world"},
    )
    base.update(overrides)
    return IdempotencyInputs.from_payload(**base)


class TestIdempotency(unittest.TestCase):

    def test_key_matches_server_pattern(self):
        key = build_idempotency_key(_inputs())
        self.assertRegex(key, _KEY_PATTERN)

    def test_key_stable_across_calls(self):
        a = build_idempotency_key(_inputs())
        b = build_idempotency_key(_inputs())
        self.assertEqual(a, b)

    def test_key_changes_on_payload_change(self):
        a = build_idempotency_key(_inputs(payload={"v": 1}))
        b = build_idempotency_key(_inputs(payload={"v": 2}))
        self.assertNotEqual(a, b)

    def test_key_changes_on_species_change(self):
        a = build_idempotency_key(_inputs(species_label="ethanol"))
        b = build_idempotency_key(_inputs(species_label="methanol"))
        self.assertNotEqual(a, b)

    def test_key_changes_on_project_change(self):
        a = build_idempotency_key(_inputs(project_label="projA"))
        b = build_idempotency_key(_inputs(project_label="projB"))
        self.assertNotEqual(a, b)

    def test_no_project_label_still_works(self):
        key = build_idempotency_key(_inputs(project_label=None))
        self.assertRegex(key, _KEY_PATTERN)
        self.assertTrue(key.startswith("arc:"))

    def test_payload_dict_ordering_does_not_change_key(self):
        a = build_idempotency_key(_inputs(payload={"a": 1, "b": 2}))
        b = build_idempotency_key(_inputs(payload={"b": 2, "a": 1}))
        self.assertEqual(a, b)


def _artifact_inputs(**overrides):
    base = dict(
        project_label="projA",
        species_label="ethanol",
        calculation_id=42,
        artifact_kind="output_log",
        artifact_sha256="0" * 64,
    )
    base.update(overrides)
    return ArtifactIdempotencyInputs(**base)


class TestArtifactIdempotency(unittest.TestCase):

    def test_key_matches_server_pattern(self):
        key = build_artifact_idempotency_key(_artifact_inputs())
        self.assertRegex(key, _KEY_PATTERN)

    def test_key_stable_across_calls(self):
        a = build_artifact_idempotency_key(_artifact_inputs())
        b = build_artifact_idempotency_key(_artifact_inputs())
        self.assertEqual(a, b)

    def test_key_distinct_across_calc_id(self):
        a = build_artifact_idempotency_key(_artifact_inputs(calculation_id=1))
        b = build_artifact_idempotency_key(_artifact_inputs(calculation_id=2))
        self.assertNotEqual(a, b)

    def test_key_is_built_from_the_calculation_ref(self):
        key = build_artifact_idempotency_key(
            _artifact_inputs(calculation_id=42, calculation_ref="calc_abcd2345"))
        self.assertIn("calc_abcd2345", key)
        self.assertNotIn(":42:", key)
        # the integer id no longer matters once a ref is stated
        self.assertEqual(key, build_artifact_idempotency_key(
            _artifact_inputs(calculation_id=7, calculation_ref="calc_abcd2345")))
        self.assertEqual(key, build_artifact_idempotency_key(
            _artifact_inputs(calculation_id=None, calculation_ref="calc_abcd2345")))

    def test_pre_0_10_integer_key_differs_from_the_ref_key(self):
        # Sidecars written before 0.10 keyed on the integer id, so the keys never match; the
        # adapter skips an artifact such a sidecar records as uploaded
        # (test_artifact_batch_response.py) rather than relying on server replay.
        self.assertNotEqual(
            build_artifact_idempotency_key(_artifact_inputs(calculation_id=42)),
            build_artifact_idempotency_key(_artifact_inputs(calculation_id=42, calculation_ref="calc_abcd2345")))

    def test_key_needs_a_handle(self):
        with self.assertRaises(ValueError):
            build_artifact_idempotency_key(_artifact_inputs(calculation_id=None))

    def test_key_distinct_across_kind(self):
        a = build_artifact_idempotency_key(_artifact_inputs(artifact_kind="output_log"))
        b = build_artifact_idempotency_key(_artifact_inputs(artifact_kind="input"))
        self.assertNotEqual(a, b)

    def test_key_distinct_across_sha(self):
        a = build_artifact_idempotency_key(_artifact_inputs(artifact_sha256="a" * 64))
        b = build_artifact_idempotency_key(_artifact_inputs(artifact_sha256="b" * 64))
        self.assertNotEqual(a, b)

    def test_key_distinct_across_species(self):
        a = build_artifact_idempotency_key(_artifact_inputs(species_label="ethanol"))
        b = build_artifact_idempotency_key(_artifact_inputs(species_label="methanol"))
        self.assertNotEqual(a, b)

    def test_key_distinct_across_project(self):
        a = build_artifact_idempotency_key(_artifact_inputs(project_label="A"))
        b = build_artifact_idempotency_key(_artifact_inputs(project_label="B"))
        self.assertNotEqual(a, b)

    def test_artifact_key_distinct_from_conformer_key(self):
        # Identical species/project; artifact key includes "artifact"
        # marker plus calc_id+kind+sha — should never collide with the
        # conformer key shape.
        artifact_key = build_artifact_idempotency_key(_artifact_inputs())
        conformer_key = build_idempotency_key(_inputs())
        self.assertNotEqual(artifact_key, conformer_key)
        self.assertIn(":artifact:", artifact_key)

    def test_key_handles_no_project_label(self):
        key = build_artifact_idempotency_key(_artifact_inputs(project_label=None))
        self.assertRegex(key, _KEY_PATTERN)


if __name__ == "__main__":
    unittest.main()
