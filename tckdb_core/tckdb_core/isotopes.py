"""The isotope rule for geometries (TCKDB's ``species_geometry_isotope_mismatch``).

TCKDB's ``GeometryPayload.isotopes`` lists only substituted atoms (every unlisted atom
is at its most abundant isotope), and the server refuses a geometry whose substitutions
differ from those the species entry's SMILES declares (``[2H]``). A producer that states a
per-atom mass-number list for a geometry applies the same rule: this module turns the list
into substitutions, compares multisets with a SMILES, and builds the geometry block.

The functions read text and lists a producer extracted; which record key holds them is
the producer's business.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

#: The most abundant natural isotope (mass number) per element; a geometry atom
#: at this mass is not a substitution. Elements without an entry cannot be judged.
MOST_ABUNDANT_ISOTOPE: Mapping[str, int] = {
    "H": 1, "He": 4, "Li": 7, "Be": 9, "B": 11, "C": 12, "N": 14, "O": 16, "F": 19,
    "Ne": 20, "Na": 23, "Mg": 24, "Al": 27, "Si": 28, "P": 31, "S": 32, "Cl": 35,
    "Ar": 40, "K": 39, "Ca": 40, "Sc": 45, "Ti": 48, "V": 51, "Cr": 52, "Mn": 55,
    "Fe": 56, "Co": 59, "Ni": 58, "Cu": 63, "Zn": 64, "Ga": 69, "Ge": 74, "As": 75,
    "Se": 80, "Br": 79, "Kr": 84,
}

_SMILES_ISOTOPE_RE = re.compile(r"\[(\d+)([A-Z][a-z]?|[bcnops])")


def _xyz_symbols(xyz_text: str | None) -> list[str] | None:
    if not isinstance(xyz_text, str):
        return None
    symbols: list[str] = []
    lines = xyz_text.strip().splitlines()
    try:
        int(lines[0].strip())
        lines = lines[2:]  # a standard XYZ header: atom count, then a comment line
    except (ValueError, IndexError):
        pass
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        symbols.append(parts[0][:1].upper() + parts[0][1:].lower())
    return symbols or None


def geometry_isotope_substitutions(
    xyz_text: str | None, isotopes: Any,
) -> dict[int, int] | None:
    """The 1-based ``{atom index: mass number}`` substitutions of a stated isotope list.

    TCKDB's ``GeometryPayload.isotopes`` lists only substituted atoms (every
    unlisted atom is at its most abundant isotope), so atoms at the standard mass
    are dropped. ``{}`` for an all-standard geometry (send no ``isotopes``);
    ``None`` when the list cannot be applied (not one integer per atom, or an
    element whose standard isotope is not tabulated).
    """
    symbols = _xyz_symbols(xyz_text)
    if symbols is None or not isinstance(isotopes, Sequence) or isinstance(isotopes, str):
        return None
    if len(isotopes) != len(symbols):
        return None
    out: dict[int, int] = {}
    for index, (symbol, mass) in enumerate(zip(symbols, isotopes), start=1):
        if isinstance(mass, bool) or not isinstance(mass, int) or mass <= 0:
            return None
        standard = MOST_ABUNDANT_ISOTOPE.get(symbol)
        if standard is None:
            return None
        if mass != standard:
            out[index] = mass
    return out


def smiles_isotope_multiset(smiles: Any) -> dict[tuple[str, int], int] | None:
    """``{(element, mass): count}`` of the non-standard isotopes a SMILES declares.

    A declared standard isotope (``[12C]``) is dropped, as TCKDB does before it
    compares. ``None`` when the SMILES is missing.
    """
    if not isinstance(smiles, str) or not smiles:
        return None
    out: dict[tuple[str, int], int] = {}
    for mass_text, symbol in _SMILES_ISOTOPE_RE.findall(smiles):
        element = symbol[:1].upper() + symbol[1:]
        mass = int(mass_text)
        if MOST_ABUNDANT_ISOTOPE.get(element) == mass:
            continue
        out[(element, mass)] = out.get((element, mass), 0) + 1
    return out


def geometry_isotope_multiset(
    xyz_text: str | None, substitutions: Mapping[int, int],
) -> dict[tuple[str, int], int]:
    symbols = _xyz_symbols(xyz_text) or []
    out: dict[tuple[str, int], int] = {}
    for index, mass in substitutions.items():
        key = (symbols[index - 1], int(mass))
        out[key] = out.get(key, 0) + 1
    return out


def isotope_reconciliation_error(
    label: Any,
    *,
    declared: Mapping[tuple[str, int], int] | None,
    substitutions: Mapping[int, int] | None,
    xyz_text: str | None,
    producer_name: str,
    isotopes_field: str,
) -> ValueError | None:
    """The refusal for a species whose stated geometry isotopes contradict its SMILES, else ``None``.

    ``declared`` is :func:`smiles_isotope_multiset` of the species' SMILES (``None``
    for no SMILES, e.g. a transition state: nothing to reconcile with);
    ``substitutions`` is :func:`geometry_isotope_substitutions` of its stated list
    (``None`` when the list is unusable). The producer sends the list as stated and never
    rewrites the SMILES to fit nor drops a stated substitution to get past the check; when
    the two disagree, or the SMILES declares a substitution the geometry's isotopes
    cannot confirm, no request is built.
    """
    if substitutions is None:
        if declared:
            return ValueError(
                f"{label!r}: the SMILES declares isotope substitution {sorted(declared)} but "
                f"{producer_name} states no usable isotope list for the geometry ({isotopes_field} is null "
                "or not one mass number per atom), so TCKDB's species_geometry_isotope_mismatch "
                "check cannot be satisfied; not building the request."
            )
        return None
    if declared is None:
        return None  # no SMILES (a TS): nothing to reconcile with
    stated = geometry_isotope_multiset(xyz_text, substitutions)
    if stated != declared:
        return ValueError(
            f"{label!r}: {producer_name} states geometry isotope substitutions {sorted(stated)} "
            f"but the SMILES declares {sorted(declared)}; sending them would be refused "
            "(species_geometry_isotope_mismatch) and the adapter does not edit either; "
            "not building the request."
        )
    return None


def geometry_payload(
    xyz_text: str,
    isotopes: Any,
    *,
    isotopes_stated: bool,
    record_xyz_text: str | None,
    record_substitutions: Mapping[int, int] | None,
    key: str | None = None,
) -> dict[str, Any] | None:
    """``{xyz_text[, key][, isotopes]}`` for a geometry of a species whose own geometry is ``record_xyz_text``.

    TCKDB's ``isotopes`` maps a 1-based atom index to a mass number for the substituted
    atoms only (an unlisted atom is at its most abundant isotope), so an all-standard
    list sends none. Nothing is sent when the producer states no isotope list at all
    (``isotopes_stated`` false). For an isotopically substituted species
    (``record_substitutions`` non-empty) the geometry must state the same
    substitutions as the species' own geometry (the species identity carries them); one
    that states none, or others, is ``None``: the caller leaves that geometry out rather
    than deposit it as an unsubstituted one.
    """
    out: dict[str, Any] = {"xyz_text": xyz_text}
    if key is not None:
        out["key"] = key
    if not isotopes_stated:
        return out
    record_subs = record_substitutions or {}
    subs = geometry_isotope_substitutions(xyz_text, isotopes)
    if record_subs:
        if subs is None or geometry_isotope_multiset(xyz_text, subs) != \
                geometry_isotope_multiset(record_xyz_text, record_subs):
            return None
    if subs:
        out["isotopes"] = {str(index): mass for index, mass in subs.items()}
    return out
