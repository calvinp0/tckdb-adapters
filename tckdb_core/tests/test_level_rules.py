"""``tckdb_core.level_rules``: the replica of TCKDB's level-of-theory identity (0.67-0.69).

The pinned hashes are TCKDB's own (``backend/tests/services/
test_level_of_theory_core_treatment_hash.py::_PRE_P4_HASHES``, produced by the
application before ``core_treatment`` existed): a change to any is a re-key of
every level spelled that way on the server.
"""

import pytest

from tckdb_core.level_rules import (
    dispersion_identity_key,
    level_hash,
    level_identity_keys,
    method_identity_key,
    same_level,
)

_PRE_P4_HASHES = [
    ({"method": "wb97xd", "basis": "def2tzvp"}, "9a489fb119cbc3cadc54fbdaae38959f622c6c9a5010476b70842df832c26b9e"),
    ({"method": "CCSD(T)", "basis": "cc-pCVTZ"}, "ad379e6d398f9c67417f8a7a12857ddc0bf12914b09792d8db4d768d6eca9d95"),
    (
        {"method": "CCSD(T)-F12", "basis": "cc-pVTZ-F12", "cabs_basis": "cc-pVTZ-F12-CABS"},
        "7d3f2bb0389dfad852f6d604044bb017f7b142d6cfd16d56e44634be744d3134",
    ),
    (
        {"method": "MRCI+Davidson", "basis": "aug-cc-pV(T+d)Z"},
        "b8a63b669e65e17271a260c56e0eedcc5c629869477dd18d7e9696cf7941c0d6",
    ),
    (
        {
            "method": "b3lyp",
            "basis": "def2tzvp",
            "dispersion": "d3bj",
            "solvent": "water",
            "solvent_model": "smd",
            "keywords": "int=ultrafine",
            "spin_treatment": "unrestricted",
        },
        "5466204b5cc42eb2f1e39aab136030ca0cba0b77bc5657d22acf542487d71793",
    ),
    ({"method": "CBS-QB3"}, "838e773298da11c3728cecd4da5c5aefb00c1b2ebe3a6d58964098c424abb43d"),
    ({"method": "cbsqb3"}, "838e773298da11c3728cecd4da5c5aefb00c1b2ebe3a6d58964098c424abb43d"),
]


@pytest.mark.parametrize("level, expected", _PRE_P4_HASHES)
def test_pinned_pre_p4_hashes(level, expected):
    assert level_hash(level) == expected


def test_core_treatment_joins_only_when_stated():
    base = {"method": "CCSD(T)", "basis": "cc-pCVTZ"}
    assert level_hash({**base, "core_treatment": None}) == level_hash(base)
    fc = level_hash({**base, "core_treatment": "frozen_core"})
    ae = level_hash({**base, "core_treatment": "all_electron"})
    assert len({level_hash(base), fc, ae}) == 3


@pytest.mark.parametrize("spelling, key", [
    ("cbsqb3", "cbs-qb3"), ("rocbsqb3", "rocbs-qb3"), ("cbs4m", "cbs-4m"), ("cbsapno", "cbs-apno"),
    ("g4(mp2)", "g4mp2"), ("g3(mp2)", "g3mp2"), ("g3(mp2)b3", "g3mp2b3"),
    ("CBS-QB3", "cbs-qb3"), ("  G4(MP2) ", "g4mp2"),
])
def test_composite_aliases(spelling, key):
    assert method_identity_key(spelling) == key


def test_composite_methods_that_stay_apart():
    assert not same_level({"method": "w1"}, {"method": "w1u"})
    assert not same_level({"method": "cbs-qb3"}, {"method": "rocbs-qb3"})
    # a correction-table name is never aliased
    assert not same_level({"method": "cbs-qb3-paraskevas"}, {"method": "cbs-qb3"})
    assert not same_level({"method": "cbsqb32023"}, {"method": "cbs-qb3"})


@pytest.mark.parametrize("spelling", [
    "gd3bj", "GD3BJ", "d3(bj)", "EmpiricalDispersion=GD3BJ", "empiricaldispersion = (gd3bj)",
    "EmpiricalDispersion(GD3BJ)",
])
def test_dispersion_aliases_and_route_forms_reach_d3bj(spelling):
    assert dispersion_identity_key(spelling) == "d3bj"


def test_dispersion_table_and_unrecognised_wrapped_value():
    assert dispersion_identity_key("GD3") == "d3zero"
    assert dispersion_identity_key("gd2") == "d2"
    assert dispersion_identity_key("d30") == "d3zero"
    assert dispersion_identity_key("d3") == "d3"  # bare d3 is its own key
    assert dispersion_identity_key("empiricaldispersion=pfd") == "empiricaldispersion=pfd"
    assert dispersion_identity_key("  ") is None and dispersion_identity_key(None) is None


def test_folded_dispersion_splits_off_listed_stems_only():
    assert level_identity_keys("b3lyp-d3bj", None) == ("b3lyp", "d3bj")
    assert level_identity_keys("B3LYP-D3(BJ)", None) == ("b3lyp", "d3bj")
    assert level_identity_keys("b3lyp-gd3bj", "d3bj") == ("b3lyp", "d3bj")
    assert level_identity_keys("m06-2x-d3zero", None) == ("m062x", "d3zero")
    # refit functionals keep the dispersion in the method name
    assert level_identity_keys("wb97x-d3bj", None) == ("wb97x-d3bj", None)
    assert same_level({"method": "b3lyp-d3bj"}, {"method": "b3lyp", "dispersion": "D3BJ"})


def test_folded_dispersion_contradiction_splits_nothing():
    assert level_identity_keys("b3lyp-d3bj", "d3zero") == ("b3lyp-d3bj", "d3zero")


def test_a_level_without_a_method_has_no_hash():
    with pytest.raises(ValueError):
        level_hash({"composite_scheme": {"kind": "additive"}})


# ---- the replica against the backend's own rules (TCKDB_BACKEND_PATH; CI sets it)

_METHODS = [
    "b3lyp", "B3LYP-D3BJ", "b3lyp-d3(bj)", "b3lyp-gd3bj", "wb97x-d", "wb97x-d3bj", "m06-2x", "m06-2x-d2",
    "hf-d3zero", "cbsqb3", "CBS-QB3", "rocbsqb3", "cbs4m", "cbsapno", "g4(mp2)", "g3(mp2)b3", "w1", "w1u",
    "cbs-qb3-paraskevas", "cbsqb32023", "dlpno-ccsd(t)", "pbe-d3zero", "b2plyp-d2", "revpbe-d3bj",
    "ccsd(t)-f12", "ub3lyp-d3bj",
]
_DISPERSIONS = [None, "d3bj", "D3BJ", "gd3bj", "d3(bj)", "gd3", "gd2", "d30", "d3", "d4",
                "EmpiricalDispersion=GD3BJ", "empiricaldispersion=(gd3)", "EmpiricalDispersion(GD2)",
                "empiricaldispersion=pfd", ""]


def test_keys_match_the_backend_over_a_corpus():
    from _backend_import import backend_modules

    _, dispersion_names, _, method_names = backend_modules()
    for method in _METHODS:
        assert method_identity_key(method) == method_names.method_identity_key(method), method
        for dispersion in _DISPERSIONS:
            assert level_identity_keys(method, dispersion) == dispersion_names.level_identity_keys(
                method, dispersion), (method, dispersion)
