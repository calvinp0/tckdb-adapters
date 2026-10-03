"""TCKDB's level-of-theory identity, replicated so the adapter can pre-check it.

TCKDB hashes a level of theory over normalised keys, not over the strings a
producer wrote. This module is a pure copy of those rules, so the adapter can
tell at build time whether two levels it is about to send are one level on the
server (the energy-level declaration rules compare hashes) without a round trip.
It sends nothing of its own: payloads keep every name verbatim.

Replicated from TCKDB ``main`` (checked against the backend by
``tests/test_energy_level_declaration.py::test_hash_matches_the_backend`` when
``TCKDB_BACKEND_PATH`` points at its ``backend/``):

* ``app/services/calculation_resolution.py::_level_of_theory_hash`` (and
  ``_level_of_theory_payload``): method, basis, aux_basis, cabs_basis,
  dispersion, solvent, solvent_model, keywords and ``spin_treatment``
  (NULL folds to ``"unknown"``, DR-0034), plus ``core_treatment`` **only when
  stated** (ADR 0021; never a null placeholder, which would re-key every row).
* ``app/chemistry/basis_set_names.py::basis_identity_key``.
* ``app/chemistry/lot_component_names.py::component_identity_key``.
* ``app/chemistry/method_names.py::method_identity_key``: strip and lower-case,
  the curated whole-name aliases (``wb97x-d``, ``m06-2x``, the four hyphen-free
  composite spellings ``cbsqb3``/``rocbsqb3``/``cbs4m``/``cbsapno`` and the
  parenthesised ``g4(mp2)``/``g3(mp2)``/``g3(mp2)b3``) and the trailing
  ``-d3(bj)`` / ``-gd3bj`` suffix aliases.
* ``app/chemistry/dispersion_names.py``: the dispersion alias table with its
  Gaussian route forms (``EmpiricalDispersion=X``, ``=(X)``, ``(X)``), and the
  split of a recognised dispersion folded into the method name
  (``b3lyp-d3bj`` keys as ``("b3lyp", "d3bj")``) only off the listed stems and
  suffixes, and only when the dispersion column does not contradict it.

The replica is conservative in the same direction the server is: it only joins
what the server joins. A divergence would make the adapter's pre-check disagree
with the server, which is what the backend comparison test guards.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------- basis / components

_HYPHEN_RULES = (
    (re.compile(r"(?<![^-])def2(?=[a-z])"), "def2-"),
    (re.compile(r"(?<![^-])ccp(?=(?:w?c)?v)"), "cc-p"),
)


def basis_identity_key(name: str | None) -> str | None:
    """Lower-cased basis name with the ``def2``/``cc-p`` family hyphens restored."""
    if name is None:
        return None
    key = name.strip().lower()
    if not key:
        return None
    for pattern, replacement in _HYPHEN_RULES:
        key = pattern.sub(replacement, key)
    return key


def component_identity_key(name: str | None) -> str | None:
    """Stripped, lower-cased dispersion / solvent / solvent-model name; blank is ``None``."""
    if name is None:
        return None
    return name.strip().lower() or None


# ---------------------------------------------------------------- method key

#: Whole-name aliases (alias -> key), matched after strip and lower-case.
NAME_ALIASES: dict[str, str] = {
    "wb97x-d": "wb97xd",
    "m06-2x": "m062x",
    "cbsqb3": "cbs-qb3",
    "rocbsqb3": "rocbs-qb3",
    "cbs4m": "cbs-4m",
    "cbsapno": "cbs-apno",
    "g4(mp2)": "g4mp2",
    "g3(mp2)": "g3mp2",
    "g3(mp2)b3": "g3mp2b3",
}

#: Trailing ``-<alias>`` spellings of D3 with Becke-Johnson damping (alias -> key).
SUFFIX_ALIASES: dict[str, str] = {
    "d3(bj)": "d3bj",
    "gd3bj": "d3bj",
}

_SUFFIX_COMPILED = tuple(
    (re.compile("^(.+)-" + re.escape(alias) + "$", re.DOTALL), "\\1-" + canonical)
    for alias, canonical in SUFFIX_ALIASES.items()
)


def method_identity_key(name: str) -> str:
    """Identity key of a method name (idempotent)."""
    key = name.strip().lower()
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    for pattern, replacement in _SUFFIX_COMPILED:
        key = pattern.sub(replacement, key)
    return key


# ---------------------------------------------------------------- dispersion key

#: Dispersion column aliases (alias -> key), each also recognised wrapped in
#: Gaussian's route forms.
DISPERSION_ALIASES: dict[str, str] = {
    "gd3bj": "d3bj",
    "d3(bj)": "d3bj",
    "gd3": "d3zero",
    "gd2": "d2",
    "d30": "d3zero",
}

_DISPERSION_COMPILED = tuple(
    (
        re.compile(
            "^(?:"
            + re.escape(alias)
            + r"|empiricaldispersion\s*(?:=\s*"
            + re.escape(alias)
            + r"|=\s*\(\s*"
            + re.escape(alias)
            + r"\s*\)|\(\s*"
            + re.escape(alias)
            + r"\s*\)))$"
        ),
        canonical,
    )
    for alias, canonical in DISPERSION_ALIASES.items()
)

#: Dispersion keys a method may carry as a trailing ``-<key>``.
FOLDED_SUFFIXES: tuple[str, ...] = ("d3bj", "d3zero", "d2")

#: Functionals published without a dispersion-specific refit, so the dispersion
#: is an add-on. ``wb97x-d3bj``, ``wb97m-d3bj``, ``b97-d3bj`` and the refit
#: double hybrids stay folded in their method key.
FOLDED_STEMS: tuple[str, ...] = (
    "b3lyp", "cam-b3lyp", "pbe", "pbe0", "tpss", "tpss0", "bp86", "blyp",
    "b2plyp", "revpbe", "b3pw91", "bhlyp", "hf", "m06-2x", "m062x",
)

_FOLDED_COMPILED = re.compile(
    "^("
    + "|".join(re.escape(s) for s in sorted(FOLDED_STEMS, key=len, reverse=True))
    + ")-("
    + "|".join(re.escape(s) for s in FOLDED_SUFFIXES)
    + ")$"
)


def dispersion_identity_key(name: str | None) -> str | None:
    """Identity key of the dispersion column (case rule, then the curated aliases)."""
    key = component_identity_key(name)
    if key is None:
        return None
    for pattern, replacement in _DISPERSION_COMPILED:
        key = pattern.sub(replacement, key)
    return key


def level_identity_keys(method: str, dispersion: str | None) -> tuple[str, str | None]:
    """The ``(method, dispersion)`` keys the hash is taken over.

    A recognised dispersion folded into the method moves to the dispersion key,
    unless the column states a different one (then the level contradicts itself
    and nothing is split).
    """
    method_key = method_identity_key(method)
    dispersion_key = dispersion_identity_key(dispersion)
    hit = _FOLDED_COMPILED.match(method_key)
    if hit is not None and dispersion_key in (None, hit.group(2)):
        return method_identity_key(hit.group(1)), hit.group(2)
    return method_key, dispersion_key


# ---------------------------------------------------------------- the hash


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def level_payload(ref: Mapping[str, Any]) -> dict[str, object]:
    """The canonical payload TCKDB hashes for a level of theory given as a mapping.

    :raises ValueError: for a level with no ``method`` (a ``composite_scheme``
        level hashes the scheme's definition on the server, which this replica
        does not model).
    """
    method = ref.get("method")
    if ref.get("composite_scheme") is not None or method is None:
        raise ValueError("a level of theory without a method has no method hash")
    method_key, dispersion_key = level_identity_keys(method, ref.get("dispersion"))
    payload: dict[str, object] = {
        "method": method_key,
        "basis": basis_identity_key(ref.get("basis")),
        "aux_basis": basis_identity_key(ref.get("aux_basis")),
        "cabs_basis": basis_identity_key(ref.get("cabs_basis")),
        "dispersion": dispersion_key,
        "solvent": component_identity_key(ref.get("solvent")),
        "solvent_model": component_identity_key(ref.get("solvent_model")),
        "keywords": ref.get("keywords"),
        "spin_treatment": _enum_value(ref.get("spin_treatment")) or "unknown",
    }
    # Identity only when stated: a missing key is not a null placeholder.
    core_treatment = _enum_value(ref.get("core_treatment"))
    if core_treatment is not None:
        payload["core_treatment"] = core_treatment
    return payload


def level_hash(ref: Mapping[str, Any]) -> str:
    """SHA-256 of the level's canonical identity payload (TCKDB's ``lot_hash``)."""
    return hashlib.sha256(
        json.dumps(level_payload(ref), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def same_level(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether the server resolves the two level dicts to one level of theory."""
    return level_hash(a) == level_hash(b)
