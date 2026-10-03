"""tckdb-schemas 0.67 ``level_of_theory.method`` guards (ADR 0021).

* ``//`` (ARC's ``energy//geometry`` shorthand) is refused by TCKDB in a method
  (``level_of_theory_method_is_compound``): no level the adapter builds may carry one.
* A correction-table name used as a method (``cbs-qb3-paraskevas``, ``cbsqb32023``) is
  sent as its stem, with the table name on the scheme.
"""

import copy
import json

import pytest

from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import _arc_level_to_tckdb_lot, _build_applied_energy_corrections

from test_arc_schema_1_3_thermo import ROUTES, _doc, _species, build, build_reaction, build_ts

COMPOUND = "ccsd(t)-f12/cc-pvtz-f12//b3lyp/def2tzvp"


# ---------------------------------------------------------------- (a) no ``//``


def test_a_compound_method_is_refused_with_a_warning_never_forwarded(caplog):
    with caplog.at_level("WARNING", logger="tckdb_arc"):
        assert _arc_level_to_tckdb_lot({"method": COMPOUND, "basis": "x"}) is None
    assert "level_method_is_compound" in caplog.text
    assert _arc_level_to_tckdb_lot({"method": "b3lyp", "basis": "def2tzvp/c"}) == {
        "method": "b3lyp", "basis": "def2tzvp/c"}   # a single '/' elsewhere is untouched


def _level_paths(node, path=()):
    """Paths of every dict in ``node`` that looks like an ARC level (``method`` beside level keys)."""
    if isinstance(node, dict):
        if isinstance(node.get("method"), str) and ({"basis", "method_type", "software", "args"} & set(node)):
            yield path
        for key, value in node.items():
            yield from _level_paths(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _level_paths(value, path + (index,))


def _at(node, path):
    for key in path:
        node = node[key]
    return node


def _walk(node, found):
    """Every method string beside a level-of-theory key in a payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("level_of_theory", "frequency_level_of_theory", "energy_level_of_theory") and isinstance(value, dict):
                found.append(value.get("method"))
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)
    return found


def _all_payloads(tmp_path, doc, label):
    """Build every route; a refusal (``ValueError``) is the adapter declining, not forwarding."""
    out = []
    for i, route in enumerate(ROUTES):
        try:
            out.append(build(tmp_path / f"r{i}", route, doc, label).payload)
        except ValueError:
            pass
    for build_fn in (build_reaction, build_ts):
        try:
            out.append(build_fn(tmp_path / build_fn.__name__, doc)[0])
        except ValueError:
            pass
    return out


def test_the_fixture_has_the_level_slots_the_sweep_below_covers():
    doc = _doc()
    paths = list(_level_paths(doc))
    names = {p[0] if len(p) == 1 else "/".join(str(x) for x in p[:1] + p[2:]) for p in paths}
    for header in ("opt_level", "freq_level", "sp_level", "composite_method", "arkane_level_of_theory"):
        assert any(p == (header,) for p in paths) or doc.get(header) is None, header
    assert any(p[0] == "species" and "levels" in p for p in paths)
    assert any(p[0] == "species" and "energy_corrections" in p for p in paths)      # scheme levels
    assert len(paths) >= 15, names


def test_no_slash_slash_reaches_any_method_the_adapter_builds(tmp_path):
    doc0 = _doc()
    paths = list(_level_paths(doc0))
    assert paths
    checked = 0
    for n, path in enumerate(paths):
        doc = copy.deepcopy(doc0)
        node = _at(doc, path)
        node["method"] = COMPOUND
        for payload in _all_payloads(tmp_path / f"p{n}", doc, "sBuOH"):
            for method in _walk(payload, []):
                assert "//" not in str(method), (path, method)
            assert "//" not in json.dumps(
                [m for m in _walk(payload, []) if m is not None]), path
            checked += 1
    assert checked > len(paths)       # the sweep built payloads, not just refusals


def test_a_clean_document_builds_with_no_slash_slash(tmp_path):
    for payload in _all_payloads(tmp_path, _doc(), "sBuOH"):
        assert all("//" not in str(m) for m in _walk(payload, []))


# ---------------------------------------------------------------- (b) correction-table names

BAC = {
    "application_role": "bac_total", "value": -1.5, "value_unit": "kcal_mol",
    "components": [{"component_kind": "bond", "key": "C-H", "multiplicity": 2,
                    "parameter_value": -0.75, "contribution_value": -1.5}],
}


def _record(kind, method, **extra):
    return {**BAC, "application_role": "aec_total" if kind == "atom_energy" else "bac_total",
            "scheme": {"kind": kind, "name": kind, "units": "kcal_mol",
                       "level_of_theory": {"method": method, "method_type": "composite", "software": "gaussian"}},
            **extra}


@pytest.mark.parametrize("arkane_method, stem", [
    ("cbs-qb3-paraskevas", "cbs-qb3"),
    ("cbsqb32023", "cbsqb3"),
    ("cbs-qb3-2023", "cbs-qb3"),
    ("g4-paraskevas", "g4"),
    ("b3lyp2023", "b3lyp"),
])
@pytest.mark.parametrize("kind", ["atom_energy", "bac_petersson", "bac_melius"])
def test_a_correction_table_name_is_sent_as_its_stem_with_the_table_on_the_scheme(arkane_method, stem, kind):
    warnings = []
    (out,) = _build_applied_energy_corrections([_record(kind, arkane_method)], warnings=warnings)
    scheme = out["scheme"]
    assert scheme["level_of_theory"]["method"] == stem
    assert scheme["name"] == arkane_method
    assert scheme["kind"] == kind
    assert [w["code"] for w in warnings] == ["correction_table_method_split"]
    assert warnings[0]["context"]["table"] == arkane_method


@pytest.mark.parametrize("method", ["cbs-qb3", "g4", "wb97xd", "b3lyp-d3bj", "ccsd(t)-f12"])
def test_a_real_method_is_left_alone(method):
    warnings = []
    (out,) = _build_applied_energy_corrections([_record("bac_petersson", method)], warnings=warnings)
    assert out["scheme"]["level_of_theory"]["method"] == method
    assert out["scheme"]["name"] == "bac_petersson"
    assert warnings == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_paraskevas_arkane_level_builds_a_contract_valid_payload(tmp_path, route):
    doc = _doc()
    table = "cbs-qb3-paraskevas"
    doc["arkane_level_of_theory"]["method"] = table
    for correction in _species(doc, "sBuOH")["energy_corrections"]:
        correction["level_of_theory"]["method"] = table
    built = build(tmp_path, route, doc, "sBuOH")      # the conftest hook validates every request
    schemes = {c["scheme"]["kind"]: c["scheme"] for c in built.corrections}
    assert set(schemes) == {"atom_energy", "bac_petersson"}
    for scheme in schemes.values():
        assert scheme["name"] == table
        assert scheme["level_of_theory"]["method"] == "cbs-qb3"
    assert "correction_table_method_split" in built.warning_codes()


# ---------------------------------------------------------------- (c) a table name as a calculation method

TABLE = "cbs-qb3-paraskevas"


def _sp_at_table(doc):
    """Every sp-level slot of the document at ``cbs-qb3-paraskevas`` (ARC runs it as CBS-QB3)."""
    doc["sp_level"] = {"method": TABLE, "method_type": "composite", "software": "gaussian"}
    for record in (*doc["species"], *doc["transition_states"]):
        levels = record.get("levels")
        if isinstance(levels, dict) and levels.get("sp"):
            levels["sp"] = {"method": TABLE}
    return doc


def test_a_calculation_at_a_table_named_method_is_sent_as_its_stem_on_every_route(tmp_path, caplog):
    from tckdb_schemas.fragments.refs import correction_table_method_stem

    payloads = []
    with caplog.at_level("WARNING", logger="tckdb_arc"):
        payloads = _all_payloads(tmp_path, _sp_at_table(_doc()), "sBuOH")
    assert len(payloads) >= 3
    methods = [m for payload in payloads for m in _walk(payload, []) if m is not None]
    assert "cbs-qb3" in methods
    assert all(correction_table_method_stem(m) is None for m in methods), sorted(set(methods))
    assert TABLE not in json.dumps(payloads)
    assert "correction_table_method_split" in caplog.text and TABLE in caplog.text


def test_the_sp_calculation_itself_carries_the_stem(tmp_path):
    payload = build(tmp_path, "computed_species", _sp_at_table(_doc()), "sBuOH").payload
    sp_methods = [c["level_of_theory"]["method"] for c in _walk_calc_dicts(payload) if c.get("type") == "sp"]
    assert sp_methods and set(sp_methods) == {"cbs-qb3"}


def _walk_calc_dicts(node):
    if isinstance(node, dict):
        if "type" in node and isinstance(node.get("level_of_theory"), dict):
            yield node
        for value in node.values():
            yield from _walk_calc_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_calc_dicts(value)


def test_a_compound_calculation_method_is_withheld_with_its_reason_code(tmp_path, caplog):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        if isinstance(record.get("levels"), dict) and record["levels"].get("sp"):
            record["levels"]["sp"] = {"method": COMPOUND}
    doc["sp_level"] = {"method": COMPOUND, "software": "orca"}
    with caplog.at_level("WARNING", logger="tckdb_arc"):
        payload = build(tmp_path, "computed_species", doc, "sBuOH").payload
    assert "level_method_is_compound" in caplog.text
    assert "//" not in json.dumps(payload)
