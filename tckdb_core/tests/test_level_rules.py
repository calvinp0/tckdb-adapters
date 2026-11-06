"""``tckdb_core.level_rules``: the replica of TCKDB's level-of-theory identity (0.67-0.69).

The pinned hashes are TCKDB's own (``backend/tests/services/
test_level_of_theory_core_treatment_hash.py::_NO_QUALIFIER_HASHES``, as the application
computes them for a level stating no method qualifier, after Stage 2 removed the
free-text ``keywords`` key from the payload, ADR 0024): a change to any is a re-key of
every level spelled that way on the server.
"""

import pytest

from tckdb_core.level_rules import (
    dispersion_identity_key,
    level_hash,
    level_identity_keys,
    level_payload,
    method_identity_key,
    same_level,
)

_NO_QUALIFIER_HASHES = [
    ({"method": "wb97xd", "basis": "def2tzvp"}, "1ba2d643d5757c406f5c28afe2e5f6314ca850a02833b6fbeda4f3347c7baa0d"),
    ({"method": "CCSD(T)", "basis": "cc-pCVTZ"}, "6fa4d7f048b76844d934505442b982faed2dde442ff347ae1013a94d83e3691b"),
    (
        {"method": "CCSD(T)-F12", "basis": "cc-pVTZ-F12", "cabs_basis": "cc-pVTZ-F12-CABS"},
        "70e98893e288a3ee2e4a204435db2c0641a9e2258a612ae6b1af9d54390f5b6f",
    ),
    (
        {"method": "MRCI+Davidson", "basis": "aug-cc-pV(T+d)Z"},
        "4955157382ef4be0539748ea19418864fc2ec006023520a04715cf60b5e74c44",
    ),
    (
        {
            "method": "b3lyp",
            "basis": "def2tzvp",
            "dispersion": "d3bj",
            "solvent": "water",
            "solvent_model": "smd",
            "spin_treatment": "unrestricted",
        },
        "0ca9b853d518fedc3b32b703138f69772913522db035f17adf03af723cde5666",
    ),
    ({"method": "CBS-QB3"}, "112ef295e50169ffd82d88aee89e30359e4462c5131a7270f45698d0b6c76495"),
    ({"method": "cbsqb3"}, "112ef295e50169ffd82d88aee89e30359e4462c5131a7270f45698d0b6c76495"),
]


@pytest.mark.parametrize("level, expected", _NO_QUALIFIER_HASHES)
def test_pinned_no_qualifier_hashes(level, expected):
    assert level_hash(level) == expected


_QUALIFIER_VALUES = {
    "relativistic_hamiltonian": ["none", "dkh0", "dkh2", "dkhso", "resc", "x2c", "zora", "iora"],
    "pno_truncation": ["loose", "normal", "tight"],
    "ri_approximation": ["none", "rij", "rijcosx", "rijk", "rijonx"],
    "core_treatment": ["frozen_core", "all_electron", "window"],
}
_DLPNO = {"method": "DLPNO-CCSD(T)", "basis": "cc-pVTZ"}


def test_there_is_no_keywords_key_in_the_payload():
    assert "keywords" not in level_payload({**_DLPNO, "keywords": "ignored"})
    assert level_hash({**_DLPNO, "keywords": "int=ultrafine"}) == level_hash(_DLPNO)


@pytest.mark.parametrize("field", sorted(_QUALIFIER_VALUES))
def test_an_unstated_qualifier_adds_no_key_and_does_not_change_the_hash(field):
    assert field not in level_payload(_DLPNO)
    assert level_hash({**_DLPNO, field: None}) == level_hash(_DLPNO)
    assert field not in level_payload({**_DLPNO, field: None})


@pytest.mark.parametrize(
    "field, value", [(f, v) for f, values in sorted(_QUALIFIER_VALUES.items()) for v in values],
)
def test_each_stated_qualifier_is_identity_and_the_stated_values_are_distinct(field, value):
    stated = {**_DLPNO, field: value}
    assert level_payload(stated)[field] == value
    assert level_hash(stated) != level_hash(_DLPNO)
    others = [level_hash({**_DLPNO, field: v}) for v in _QUALIFIER_VALUES[field] if v != value]
    assert level_hash(stated) not in others


@pytest.mark.parametrize("field", ["relativistic_hamiltonian", "ri_approximation"])
def test_an_explicit_none_is_a_stated_value_not_the_absent_one(field):
    assert level_hash({**_DLPNO, field: "none"}) != level_hash(_DLPNO)
    assert level_payload({**_DLPNO, field: "none"})[field] == "none"


def test_qualifiers_are_independent_of_each_other():
    both = {**_DLPNO, "pno_truncation": "tight", "ri_approximation": "rijcosx"}
    assert level_hash(both) not in {
        level_hash({**_DLPNO, "pno_truncation": "tight"}),
        level_hash({**_DLPNO, "ri_approximation": "rijcosx"}),
        level_hash(_DLPNO),
    }
    assert level_hash(both) == level_hash({"ri_approximation": "rijcosx", "pno_truncation": "tight", **_DLPNO})


def test_core_window_is_a_third_core_treatment():
    assert len({level_hash({**_DLPNO, "core_treatment": v}) for v in ("frozen_core", "all_electron", "window")}) == 3


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
    from tckdb_core.testing.backend_import import backend_modules

    _, dispersion_names, _, method_names = backend_modules()
    for method in _METHODS:
        assert method_identity_key(method) == method_names.method_identity_key(method), method
        for dispersion in _DISPERSIONS:
            assert level_identity_keys(method, dispersion) == dispersion_names.level_identity_keys(
                method, dispersion), (method, dispersion)
