"""The typed method qualifiers read from ARC's ``args`` (adapter 0.15.0; TCKDB ADR 0024).

Each mapped spelling is one the group knowledge base confirms for that program (see
``tckdb_arc/level_qualifiers.py`` and docs/contract/CURRENT_ARC_INTEGRATION.md). Nothing is sent from
absence, a program default or a method name.
"""

import tempfile
import unittest

import pytest

from _level_args import build_all_payloads, iter_keys, level_args_corpus, materialize
from _contract import contract_validate
from tckdb_arc.adapter import _arc_level_to_tckdb_lot
from tckdb_arc.level_qualifiers import level_qualifiers
from tckdb_core.level_rules import level_hash
from tckdb_schemas.fragments.refs import LevelOfTheoryRef


def orca(general, block=None):
    return {"method": "dlpno-ccsd(t)", "basis": "def2-tzvp", "software": "orca",
            "args": {"keyword": {"general": general}, "block": block or {}}}


def gaussian(general):
    return {"method": "wb97xd", "basis": "def2tzvp", "software": "gaussian",
            "args": {"keyword": {"general": general}, "block": {}}}


MAPPED = [
    # (level, expected qualifier fields)
    (orca("tightpno"), {"pno_truncation": "tight"}),
    (orca("normalpno"), {"pno_truncation": "normal"}),
    (orca("loosepno"), {"pno_truncation": "loose"}),
    (orca("rijcosx"), {"ri_approximation": "rijcosx"}),
    (orca("rijk"), {"ri_approximation": "rijk"}),
    (orca("rijonx"), {"ri_approximation": "rijonx"}),
    (orca("dkh2"), {"relativistic_hamiltonian": "dkh2"}),
    (orca("dkh"), {"relativistic_hamiltonian": "dkh2"}),
    (orca("zora"), {"relativistic_hamiltonian": "zora"}),
    (orca("iora"), {"relativistic_hamiltonian": "iora"}),
    (orca("x2c"), {"relativistic_hamiltonian": "x2c"}),
    (orca("", {"method": "frozencore fc_ewin"}), {"core_treatment": "window"}),
    (orca("TightPNO RIJCOSX DKH2 TightSCF"),
     {"pno_truncation": "tight", "ri_approximation": "rijcosx", "relativistic_hamiltonian": "dkh2"}),
    (gaussian("int=dkh2"), {"relativistic_hamiltonian": "dkh2"}),
    (gaussian("Integral=DKH"), {"relativistic_hamiltonian": "dkh2"}),
    (gaussian("int=(dkh2,grid=ultrafine)"), {"relativistic_hamiltonian": "dkh2"}),
    (gaussian("int(dkh2) scf=tight"), {"relativistic_hamiltonian": "dkh2"}),
    (gaussian("int=dkh0"), {"relativistic_hamiltonian": "dkh0"}),
    (gaussian("int=dkhso"), {"relativistic_hamiltonian": "dkhso"}),
    (gaussian("integral=resc"), {"relativistic_hamiltonian": "resc"}),
]

NOTHING = [
    ("a non-qualifier SCF option", gaussian("scf=(maxcycle=200)")),
    ("an integration grid", gaussian("int=ultrafine")),
    ("a bare Gaussian dkh2 outside Int", gaussian("dkh2")),
    ("ORCA spelling on a Gaussian level", {**gaussian("tightpno rijcosx zora")}),
    ("Gaussian spelling on an ORCA level", orca("int=dkh2")),
    ("RI inside a method name", {"method": "ri-mp2", "basis": "def2-tzvp", "software": "orca",
                                 "args": {"keyword": {"general": "ri-mp2 tightscf"}, "block": {}}}),
    ("DLPNO method name alone", orca("dlpno-ccsd(t) normalscf")),
    ("DKH1 (first order has no TCKDB value)", orca("dkh1")),
    ("DLU-X2C is not X2C", orca("dlu-x2c")),
    ("RI-J spelling is not read", orca("ri-j ri")),
    ("NoRI is not read as none", orca("noris norijcosx nori nocosx")),
    ("a job-specific keyword is not level-wide", {**orca(""), "args": {"keyword": {"opt": "tightpno rijcosx"}, "block": {}}}),
    ("ewin without FC_EWIN", orca("", {"mp2": "ewin -1.5, 1.0e3"})),
    ("two relativistic values state nothing", orca("zora dkh2")),
    ("no args", {"method": "wb97xd", "basis": "def2tzvp", "software": "orca"}),
    ("null args", {"method": "wb97xd", "basis": "def2tzvp", "software": "orca", "args": None}),
    ("no software", {"method": "wb97xd", "basis": "def2tzvp", "args": {"keyword": {"general": "zora"}}}),
    ("molpro is not read", {"method": "ccsd(t)", "basis": "cc-pvtz", "software": "molpro",
                            "args": {"keyword": {"general": "dkh2 zora"}}}),
]


@pytest.mark.parametrize("level, expected", MAPPED)
def test_a_stated_token_maps_to_its_lowercase_value(level, expected):
    assert level_qualifiers(level) == expected
    out = _arc_level_to_tckdb_lot(level)
    for field, value in expected.items():
        assert out[field] == value
    assert "keywords" not in out
    contract_validate(LevelOfTheoryRef, out)


@pytest.mark.parametrize("name, level", NOTHING)
def test_an_unstated_or_ambiguous_qualifier_stays_absent(name, level):
    assert level_qualifiers(level) == {}, name
    out = _arc_level_to_tckdb_lot(level)
    assert set(out) <= {"method", "basis"}, (name, out)


def test_an_ambiguous_field_is_left_out_but_the_others_are_kept():
    assert level_qualifiers(orca("zora dkh2 tightpno")) == {"pno_truncation": "tight"}


def test_the_value_is_sent_lowercase_whatever_the_case_of_the_token():
    assert level_qualifiers(orca("TIGHTPNO RIJCOSX DKH2")) == {
        "pno_truncation": "tight", "ri_approximation": "rijcosx", "relativistic_hamiltonian": "dkh2"}


def test_an_args_string_is_ARCs_general_keyword_line():
    assert level_qualifiers({"method": "m", "software": "orca", "args": "tightpno"}) == {"pno_truncation": "tight"}


def test_a_level_states_a_qualifier_in_the_hash_only_when_stated():
    base = {"method": "dlpno-ccsd(t)", "basis": "def2-tzvp"}
    stated = _arc_level_to_tckdb_lot(orca("tightpno"))
    assert level_hash(stated) != level_hash(base)
    assert level_hash(_arc_level_to_tckdb_lot(orca("tightscf"))) == level_hash(base)


@pytest.mark.parametrize("level, expected", MAPPED)
def test_the_hash_replica_agrees_with_the_backend_for_every_mapped_set(level, expected):
    from tckdb_core.testing.backend_import import backend_level_hash
    out = _arc_level_to_tckdb_lot(level)
    assert level_hash(out) == backend_level_hash(out), out


class TestQualifiersInBuiltPayloads(unittest.TestCase):
    """The corpus: opt/freq Gaussian ``int=dkh2``, sp ORCA ``tightpno rijcosx``; ``scf=(maxcycle=200)`` maps to nothing."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="tckdb-level-qualifiers-")
        cls.addClassCleanup(cls._tmp.cleanup)
        doc, sidecars = level_args_corpus()
        project = materialize(f"{cls._tmp.name}/project", doc, sidecars)
        cls.built = build_all_payloads(project, f"{cls._tmp.name}/payloads")

    def _levels(self, obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("level_of_theory", "energy_level_of_theory") and isinstance(value, dict):
                    yield value
                yield from self._levels(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                yield from self._levels(item)

    def test_every_level_of_every_route_carries_exactly_the_stated_qualifiers(self):
        checked = 0
        for route, payload in self.built.items():
            for level in self._levels(payload):
                extras = {k: v for k, v in level.items()
                          if k in ("relativistic_hamiltonian", "pno_truncation", "ri_approximation", "core_treatment")}
                if level["method"] == "wb97xd":
                    self.assertEqual({"relativistic_hamiltonian": "dkh2"}, extras, route)
                else:
                    self.assertEqual("dlpno-ccsd(t)", level["method"])
                    self.assertEqual({"pno_truncation": "tight", "ri_approximation": "rijcosx"}, extras, route)
                checked += 1
        self.assertGreater(checked, 20)

    def test_the_scf_option_is_a_qualifier_nowhere(self):
        self.assertNotIn("keywords", set(iter_keys(self.built)))
        self.assertNotIn("maxcycle", str(self.built))
