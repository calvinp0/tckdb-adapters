"""TCKDB's level-of-theory identity and energy-level link check, replicated offline.

The conftest hook validates a payload against the published request models, but
the route handlers' rules run only server-side. The energy-level rules are the
ones a declaration can trip, so the tests replay them here.

Replicated from TCKDB (checked against the backend when it is importable, see
``test_energy_level_declaration.py::test_hash_matches_the_backend``):

* ``backend/app/services/calculation_resolution.py::_level_of_theory_hash``:
  a level's identity hashes method, basis (through ``basis_identity_key``),
  aux_basis, cabs_basis, dispersion, solvent, solvent_model, keywords and
  ``spin_treatment``, where NULL folds to ``"unknown"`` (DR-0034).
* ``backend/app/chemistry/basis_set_names.py::basis_identity_key``.
* ``backend/app/services/calculation_levels.py::assert_role_consistency``,
  R2' level uniformity and R4'/R5: a declared energy level must resolve to the
  linked sp calculations' shared level, or, with no sp linked, to every linked
  opt's level.
"""

from __future__ import annotations

import hashlib
import json
import re

_HYPHEN_RULES = (
    (re.compile(r"(?<![^-])def2(?=[a-z])"), "def2-"),
    (re.compile(r"(?<![^-])ccp(?=(?:w?c)?v)"), "cc-p"),
)


def basis_identity_key(name):
    if name is None:
        return None
    key = name.strip().lower()
    if not key:
        return None
    for pattern, replacement in _HYPHEN_RULES:
        key = pattern.sub(replacement, key)
    return key


def level_hash(ref) -> str:
    payload = {
        "method": ref["method"],
        "basis": basis_identity_key(ref.get("basis")),
        "aux_basis": basis_identity_key(ref.get("aux_basis")),
        "cabs_basis": basis_identity_key(ref.get("cabs_basis")),
        "dispersion": ref.get("dispersion"),
        "solvent": ref.get("solvent"),
        "solvent_model": ref.get("solvent_model"),
        "keywords": ref.get("keywords"),
        "spin_treatment": ref.get("spin_treatment") or "unknown",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
