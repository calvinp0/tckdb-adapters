"""Correction schemes (tckdb-schemas 0.66-0.67): the frequency half of a BAC key, and method guards.

Arkane keys a Petersson / Melius BAC on ``CompositeLevelOfTheory(freq=..., energy=...)``;
``scheme.frequency_level_of_theory`` carries the frequency half. It is sent only when ARC states
it, never on an atom-energy scheme, and it joins the scheme's identity (one new scheme row per BAC
scheme). The adapter never sends ``application_role: composite_delta``.
"""

import copy
import pathlib

import pytest

from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import _bac_frequency_level

from test_arc_schema_1_3_thermo import ROUTES, _correction, _doc, _schemes, _species, build, build_reaction

FREQ = {"basis": "def2tzvp", "method": "wb97xd"}
EXPECTED = {"method": "wb97xd", "basis": "def2tzvp"}


COMPOSITE_KEY = ("CompositeLevelOfTheory(freq=LevelOfTheory(method='wb97xd',basis='def2tzvp',software='gaussian'),"
                 "energy=LevelOfTheory(method='cbsqb3',software='gaussian'))")


def _with_bac_key(doc, key, bac_type="p"):
    _correction(_species(doc, "sBuOH"), "bond_additivity")["matched_arkane_key"] = key
    return doc


@pytest.mark.parametrize("route", ROUTES)
def test_a_single_level_keyed_bac_scheme_sends_no_freq_level(tmp_path, route):
    # a single-level key such as the ebc88ec8 sample's LevelOfTheory(method='bmk', basis='cbsb7'):
    # ARC states a freq-job level, but Arkane does not key this table on it (contract 0.66)
    doc = _with_bac_key(_doc(), "LevelOfTheory(method='bmk',basis='cbsb7')")
    schemes = _schemes(build(tmp_path, route, doc, "sBuOH"))
    assert "frequency_level_of_theory" not in schemes["bac_petersson"]
    assert schemes["bac_petersson"]["level_of_theory"]["method"] == "cbs-qb3"


@pytest.mark.parametrize("route", ROUTES)
def test_a_composite_keyed_bac_scheme_carries_the_keys_freq_half(tmp_path, route):
    doc = _with_bac_key(_doc(), COMPOSITE_KEY)
    schemes = _schemes(build(tmp_path, route, doc, "sBuOH"))
    assert schemes["bac_petersson"]["frequency_level_of_theory"] == EXPECTED
    assert schemes["bac_petersson"]["level_of_theory"]["method"] == "cbs-qb3"
    assert "frequency_level_of_theory" not in schemes["atom_energy"]


def test_a_composite_key_sends_its_freq_half_even_when_arc_states_no_freq_job(tmp_path):
    doc = _with_bac_key(_doc(), COMPOSITE_KEY)
    _species(doc, "sBuOH")["levels"]["freq"] = None
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert _schemes(built)["bac_petersson"]["frequency_level_of_theory"] == EXPECTED


def test_a_composite_key_that_disagrees_with_arcs_freq_job_sends_none_with_a_warning(tmp_path, caplog):
    key = COMPOSITE_KEY.replace("method='wb97xd'", "method='b3lyp'")
    doc = _with_bac_key(_doc(), key)
    with caplog.at_level("WARNING", logger="tckdb_arc"):
        built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "frequency_level_of_theory" not in _schemes(built)["bac_petersson"]
    assert "bac_frequency_level_conflict" in caplog.text


@pytest.mark.parametrize("key", [None, "garbage", "CompositeLevelOfTheory(freq=LevelOfTheory(method='b3lyp'))",
                                 "LevelOfTheory(method='wb97xd',basis='def2tzvp')"])
def test_no_key_or_an_unparseable_or_single_level_key_sends_none(tmp_path, key):
    doc = _with_bac_key(_doc(), key)
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "frequency_level_of_theory" not in _schemes(built)["bac_petersson"]


@pytest.mark.parametrize("route", ROUTES)
def test_an_atom_energy_scheme_never_carries_one(tmp_path, route):
    schemes = _schemes(build(tmp_path, route, _doc(), "sBuOH"))
    assert "frequency_level_of_theory" not in schemes["atom_energy"]


@pytest.mark.parametrize("route", ROUTES)
def test_a_melius_scheme_carries_it_too(tmp_path, route):
    doc = _doc()
    doc["bac_type"] = "m"
    bac = _correction(_species(doc, "sBuOH"), "bond_additivity")
    bac["model"] = "melius"
    bac.pop("parameter_table", None)
    bac["skipped_components"] = None
    bac["matched_arkane_key"] = COMPOSITE_KEY
    schemes = _schemes(build(tmp_path, route, doc, "sBuOH"))
    assert schemes["bac_melius"]["frequency_level_of_theory"] == EXPECTED


def test_a_composite_runs_own_frequencies_name_no_freq_level():
    record = {"levels": {"composite": {"method": "cbs-qb3"}, "freq": None},
              "composite_log": "c.log", "freq_log": "c.log"}
    assert _bac_frequency_level({"freq_level": FREQ}, record) is None


def test_the_header_freq_level_is_used_only_for_a_pre_1_3_record_the_adapter_attributes_it_to():
    record = {"label": "x"}                       # no ``levels``: output 1.2 or older
    assert _bac_frequency_level({"freq_level": FREQ}, record) == FREQ
    assert _bac_frequency_level({"freq_level": None, "opt_level": FREQ}, record) is None   # no inference from opt
    assert _bac_frequency_level({"freq_level": {"basis": "b"}}, record) is None            # no method


def test_under_adaptive_levels_a_pre_1_3_record_gets_none():
    marked = {"freq_level": FREQ, adapter_module._ADAPTIVE_LEVELS_KEY: {"named": {"opt", "freq"}, "omitted": {}}}
    assert _bac_frequency_level(marked, {"label": "x"}) is None


def test_a_pre_1_3_document_sends_none_without_a_composite_key(tmp_path):
    doc = _doc("1.2")
    for record in (*doc["species"], *doc["transition_states"]):
        record.pop("levels", None)
    built = build(tmp_path, "computed_species", doc, "sBuOH")
    assert "frequency_level_of_theory" not in _schemes(built)["bac_petersson"]


def test_the_ts_blocks_atom_energy_scheme_carries_none(tmp_path):
    payload, _ = build_reaction(tmp_path, _doc())
    corrections = payload["transition_state"]["applied_energy_corrections"]
    assert corrections and all(c["scheme"]["kind"] == "atom_energy" for c in corrections)
    assert all("frequency_level_of_theory" not in c["scheme"] for c in corrections)


# ---- composite_delta is never sent (tckdb-schemas 0.66 warns on it)


@pytest.mark.parametrize("route", ROUTES)
def test_application_roles_are_only_aec_total_and_bac_total(tmp_path, route):
    built = build(tmp_path, route, _doc(), "sBuOH")
    assert built.corrections
    assert {c["application_role"] for c in built.corrections} <= {"aec_total", "bac_total"}


def test_the_adapter_source_never_names_composite_delta():
    source = pathlib.Path(adapter_module.__file__).read_text()
    assert "composite_delta" not in source
