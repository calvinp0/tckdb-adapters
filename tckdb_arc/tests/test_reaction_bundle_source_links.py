"""A reaction bundle never links the calculation of one species from another's block (tckdb-schemas 0.61).

Contract: "A source calculation of another species on the reaction bundle is refused with the
coded ``transport_source_calculation_owner_mismatch``" (0.61 states it for transport; thermo,
statmech and applied corrections are owner-checked at the same persistence seam, as the bundle's
field descriptions say: ``source_calculation_key`` "resolves against the bundle's global
calculation namespace; the workflow rejects 422 when the referenced calc is not owned by this
species" and a transition state's statmech and evidence name "calculations this transition state
owns"). The adapter builds every link from its own block's role map, and a species repeated in
the reaction (``H + H``, ``H2 + H <=> H + H2``) is declared once, so a link can only name its own
calculation. This test replicates the ownership rule over several built bundles; the rule runs
offline (the route handler's own check is not reachable here).
"""

import copy

import pytest
import yaml

from test_adapter import _reaction_output_doc
from test_arc_1_3_reactions import _fixture as _reactions_fixture, _reaction_payload
from test_reaction_species_dedup import _build as _build_species_dedup
from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig

TS = "<transition state>"


def _owners(payload):
    owner = {}
    for block in payload["species"]:
        for conformer in block["conformers"]:
            owner[conformer["calculation"]["key"]] = block["key"]
        for calc in block.get("calculations", []):
            owner[calc["key"]] = block["key"]
    ts = payload.get("transition_state")
    if ts:
        for calc in (ts["calculation"], *ts.get("calculations", [])):
            owner[calc["key"]] = TS
    return owner


def ownership_violations(payload):
    """Every cross-owner calculation link in a computed-reaction payload, as readable strings."""
    owner = _owners(payload)
    total = sum(len(b["conformers"]) + len(b.get("calculations", [])) for b in payload["species"])
    ts = payload.get("transition_state")
    total += (1 + len(ts.get("calculations", []))) if ts else 0
    assert len(owner) == total, "calculation keys must be globally unique"
    out = []

    def check(where, key, expected):
        if key not in owner:
            out.append(f"{where}: {key!r} is not a calculation of the bundle")
        elif owner[key] != expected:
            out.append(f"{where}: {key!r} belongs to {owner[key]!r}, not {expected!r}")

    species_keys = {b["key"] for b in payload["species"]}
    assert set(payload["reactant_keys"]) | set(payload["product_keys"]) <= species_keys
    for block in payload["species"]:
        for part in ("thermo", "statmech", "transport"):
            for link in (block.get(part) or {}).get("source_calculations", []):
                check(f"species[{block['key']}].{part}", link["calculation_key"], block["key"])
        for correction in block.get("applied_energy_corrections", []):
            if correction.get("source_calculation_key"):
                check(f"species[{block['key']}].applied_energy_corrections",
                      correction["source_calculation_key"], block["key"])
        for calc in block.get("calculations", []):
            for edge in calc.get("depends_on", []):
                check(f"calculation {calc['key']} depends_on", edge["parent_calculation_key"], block["key"])
    if ts:
        for part in ("statmech",):
            for link in (ts.get(part) or {}).get("source_calculations", []):
                check(f"transition_state.{part}", link["calculation_key"], TS)
        for correction in ts.get("applied_energy_corrections", []):
            if correction.get("source_calculation_key"):
                check("transition_state.applied_energy_corrections",
                      correction["source_calculation_key"], TS)
        for evidence in ts.get("validation_evidence", []):
            if evidence.get("source_calculation_key"):
                check("transition_state.validation_evidence", evidence["source_calculation_key"], TS)
    sides = {"reactant_energy": "reactant_keys", "product_energy": "product_keys"}
    for fit in payload.get("kinetics", []):
        for link in fit.get("source_calculations", []):
            key, role = link["calculation_key"], link["role"]
            if role in sides:
                if owner.get(key) not in fit[sides[role]]:
                    out.append(f"kinetics {role}: {key!r} belongs to {owner.get(key)!r}, "
                               f"not a participant of {fit[sides[role]]}")
            else:
                check(f"kinetics {role}", key, TS)
    return out


def _default_reaction():
    doc = _reaction_output_doc()
    cfg = TCKDBConfig(enabled=True, base_url="http://x", upload=False)
    return TCKDBAdapter(cfg)._build_computed_reaction_payload(
        output_doc=doc, reaction_record=doc["reactions"][0])


def _thermo_fixture_reaction():
    """sBuOH <=> nBuOH of the 1.3 thermo fixture: both species carry thermo, statmech and applied corrections."""
    import pathlib
    import tempfile
    from test_arc_schema_1_3_thermo import _doc, build_reaction
    return build_reaction(pathlib.Path(tempfile.mkdtemp()), _doc())[0]


BUILDS = {
    "default reaction": _default_reaction,
    "1.3 OH + CH4": lambda: _reaction_payload(_reactions_fixture())[0],
    "1.3 sBuOH <=> nBuOH": _thermo_fixture_reaction,
    "H2 + H <=> H + H2": lambda: _build_species_dedup(["H2", "H"], ["H", "H2"]),
    "H + H <=> H2": lambda: _build_species_dedup(["H", "H"], ["H2"]),
}


@pytest.mark.parametrize("name", list(BUILDS))
def test_no_link_crosses_species(name):
    payload = BUILDS[name]()
    assert payload["kinetics"][0]["source_calculations"], "the bundle must carry links for this to mean anything"
    assert ownership_violations(payload) == []


def test_the_thermo_fixture_bundle_exercises_every_species_side_link_kind():
    """Coverage of the ownership check: thermo, statmech and applied-correction links all exist there."""
    payload = BUILDS["1.3 sBuOH <=> nBuOH"]()
    for block in payload["species"]:
        assert block["thermo"]["source_calculations"] and block["statmech"]["source_calculations"]
        assert block["applied_energy_corrections"]
        assert all(c.get("source_calculation_key") for c in block["applied_energy_corrections"])
    assert len(payload["species"]) == 2


def test_a_species_repeated_in_the_reaction_is_declared_and_linked_once():
    payload = BUILDS["H + H <=> H2"]()
    assert [b["key"] for b in payload["species"]].count("r0_H") == 1
    assert payload["reactant_keys"] == ["r0_H", "r0_H"]
    reactant_links = [l for l in payload["kinetics"][0]["source_calculations"] if l["role"] == "reactant_energy"]
    assert reactant_links == [{"calculation_key": "r0_sp", "role": "reactant_energy"}]


def test_the_checker_flags_a_crossing_link():
    """Mutation check of the test itself: each kind of cross-owner link is detected."""
    for build in ("default reaction", "1.3 OH + CH4"):
        base = BUILDS[build]()
        a, b = base["species"][0], base["species"][1]
        other = b["conformers"][0]["calculation"]["key"]
        mutated = copy.deepcopy(base)
        mutated["species"][0].setdefault("statmech", {}).setdefault("source_calculations", []).append(
            {"calculation_key": other, "role": "sp"})
        assert any("belongs to" in v for v in ownership_violations(mutated))
        mutated = copy.deepcopy(base)
        mutated["kinetics"][0]["source_calculations"][0]["calculation_key"] = (
            mutated["transition_state"]["calculation"]["key"])
        assert ownership_violations(mutated)
        mutated = copy.deepcopy(base)
        mutated["species"][0].setdefault("applied_energy_corrections", []).append(
            {"source_calculation_key": other})
        assert ownership_violations(mutated)
