"""TCKDB's level-of-theory identity and energy-level link check, replicated offline.

The conftest hook validates a payload against the published request models, but
the route handlers' rules run only server-side. The energy-level rules are the
ones a declaration can trip, so the tests replay them here.

Replicated from TCKDB (checked against the backend when it is importable, see
``test_energy_level_declaration.py::test_hash_matches_the_backend``):

* ``backend/app/services/calculation_resolution.py::_level_of_theory_hash`` and
  its identity keys: now ``tckdb_arc/level_rules.py`` (see its docstring).
* ``backend/app/services/calculation_levels.py::assert_role_consistency``,
  R2' level uniformity and R4'/R5: a declared energy level must resolve to the
  linked sp calculations' shared level, or, with no sp linked, to every linked
  opt's level.
"""

from __future__ import annotations

# The hash lives in the package so the adapter can pre-check identity at build time;
# the tests import it from there (and test_level_rules pins it to TCKDB's literals).
from tckdb_arc.level_rules import basis_identity_key, level_hash  # noqa: F401


def energy_level_verdict(block, calculations_by_key):
    """``None`` when TCKDB accepts the block's declared energy level, else the refusal stem.

    ``block`` is a thermo or statmech dict; ``calculations_by_key`` maps a
    calculation's local key to its payload dict.
    """
    declared = block.get("energy_level_of_theory")
    if declared is None:
        return None
    links = [(link["role"], calculations_by_key[link["calculation_key"]])
             for link in block.get("source_calculations", [])]
    sp_hashes = {level_hash(c["level_of_theory"]) for role, c in links if role == "sp"}
    opt_hashes = [level_hash(c["level_of_theory"]) for role, c in links if role == "opt"]
    if len(sp_hashes) > 1:
        return "energy_level_ambiguous"
    if sp_hashes:
        return None if sp_hashes == {level_hash(declared)} else "energy_level_contradiction"
    if opt_hashes:
        return (None if all(h == level_hash(declared) for h in opt_hashes)
                else "energy_level_requires_sp")
    return None


def calculations_by_key(*groups):
    out = {}
    for group in groups:
        for calc in ([group] if isinstance(group, dict) else group):
            if calc.get("key"):
                out[calc["key"]] = calc
    return out
