"""The adapter surface agrees with TCKDB's shipped ``classify_method`` and the backend's dispatching one."""

import pytest

from tckdb_schemas.method_families import CLASSIFICATION, classify_method

from tckdb_core.method_families import Family, method_family
from tckdb_core.testing.backend_import import backend_method_families

NAMED = ["b3lyp", "b2plyp", "g4", "cbs-qb3", "ccsd(t)-f12/ri", "dlpno-ccsd(t)-f12", "ccsd(t)-f12a",
         "mp2-f12", "caspt2", "mrci+q"]
ARC_SPELLINGS = ["dlpno-ccsd(t)-f12", "ccsd(t)-f12", "ccsd(t)", "mp2", "b3lyp", "wb97xd", "wb97x-d3", "m062x",
                 "b2plyp", "cbs-qb3", "g4", "g3", "hf", "casscf", "caspt2", "nevpt2", "mrci+q", "ccsd(t)-f12/ri"]
VARIANTS = ["HF", " Hf ", "B3LYP-D3BJ", "wB97X-D", "M06-2X", "m06-2x-d3zero", "UHF", "ub3lyp", "uccsd(t)",
            "ro-b3lyp", "b97-d3", "b97-d3bj", "b97d3", "CBSQB3", "G4(MP2)", "g3(mp2)b3", "xyg3", "hf-3c", "ci",
            "casscf(2,2)", "", "   ", "unknownmethod", "pm6", "gfn2-xtb", "uff", "dsd-blyp-d3bj", "ωb97x-d"]
CORPUS = sorted(set(CLASSIFICATION) | set(NAMED) | set(ARC_SPELLINGS) | set(VARIANTS))


def test_the_corpus_is_large():
    assert len(CORPUS) >= 60


def test_a_blank_or_missing_method_is_unclassified():
    assert method_family(None) is Family.unclassified
    assert method_family("  ") is Family.unclassified


@pytest.mark.parametrize("name,family", [
    ("dlpno-ccsd(t)-f12", Family.wavefunction), ("ccsd(t)-f12a", Family.wavefunction),
    ("mp2-f12", Family.wavefunction), ("caspt2", Family.multireference), ("mrci+q", Family.multireference),
    ("b3lyp", Family.dft), ("b2plyp", Family.dft), ("g4", Family.composite), ("cbs-qb3", Family.composite),
    ("ccsd(t)-f12/ri", Family.unclassified)])
def test_the_families_the_exemption_turns_on(name, family):
    assert method_family(name) is family


def test_families_match_the_shipped_classifier_over_a_corpus():
    for name in CORPUS:
        assert method_family(name) is classify_method(name).family, name


def test_families_match_the_backend_over_a_corpus():
    # The backend's classify_method still dispatches through its version registry over the shipped table.
    backend = backend_method_families()
    for name in CORPUS:
        assert method_family(name).value == backend.classify_method(name).family.value, name
