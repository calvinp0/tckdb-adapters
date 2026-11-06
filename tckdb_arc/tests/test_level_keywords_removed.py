"""``level_of_theory.keywords`` is gone (tckdb-schemas 0.158, TCKDB ADR 0024).

TCKDB's upload schemas forbid unknown fields, so a level carrying ``keywords``, even ``null``, is refused
with a 422. ARC's ``args`` (ESS option blocks) are calculation parameters: the input deck and the log
carry them, and the adapter does not route the string anywhere else.

Every other fixture's levels lack ``args``, which is how a payload that 422'd on 0.158 passed the suite.
``_level_args.level_args_corpus`` is the golden document with ARC-shaped ``args`` on every level.
"""

import tempfile
import unittest

import yaml

from _level_args import build_all_payloads, iter_keys, iter_levels, level_args_corpus, materialize


def _level_dicts(obj):
    """Every ``level_of_theory`` / ``energy_level_of_theory`` mapping in a built payload."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("level_of_theory", "energy_level_of_theory") and isinstance(value, dict):
                yield value
            yield from _level_dicts(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _level_dicts(item)


class TestNoKeywordsAnywhere(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="tckdb-level-args-")
        cls.addClassCleanup(cls._tmp.cleanup)
        doc, sidecars = level_args_corpus()
        cls.doc = doc
        project = materialize(f"{cls._tmp.name}/project", doc, sidecars)
        cls.built = build_all_payloads(project, f"{cls._tmp.name}/payloads")

    def test_the_corpus_really_carries_args_on_every_level(self):
        levels = list(iter_levels(self.doc))
        self.assertGreaterEqual(len(levels), 4)   # opt, freq, sp and a correction scheme's
        for level in levels:
            self.assertTrue(level.get("args"), level)

    def test_every_route_was_built_and_has_levels_to_check(self):
        self.assertEqual({"conformer", "computed_species", "computed_reaction", "transition_state"}, set(self.built))
        for route, payload in self.built.items():
            self.assertGreater(len(list(_level_dicts(payload))), 0, route)

    def test_no_payload_of_any_route_carries_a_keywords_key_anywhere(self):
        for route, payload in self.built.items():
            self.assertNotIn("keywords", set(iter_keys(payload)), route)

    def test_levels_keep_their_method_and_basis_verbatim(self):
        seen = {(lv["method"], lv.get("basis")) for payload in self.built.values() for lv in _level_dicts(payload)}
        self.assertIn(("wb97xd", "def2-svp"), seen)
        self.assertIn(("dlpno-ccsd(t)", "def2-tzvp"), seen)

    def test_the_args_string_is_not_rerouted_into_any_other_field(self):
        text = yaml.safe_dump(self.built)
        for needle in ("keyword:general", "block:scf", "maxcycle=200", "maxiter 200"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
