"""Element and formula utilities over a geometry or a formula string.

They read the text a producer extracted (an ``xyz`` string, a ``formula`` string) and
say which elements it holds; picking the key out of a producer's record is the
producer's job.
"""

from __future__ import annotations

import re
from typing import Any


def xyz_element_symbols(xyz: Any) -> tuple[str, ...] | None:
    """Return the element symbol of every atom in a producer's ``xyz`` string.

    Accepts atom-only lines (the output of ``tckdb_core.xyz.xyz_to_str``) with or without an XYZ
    count/comment header. ``None`` when the geometry is missing or a line
    does not start with an element symbol.
    """
    if not isinstance(xyz, str):
        return None
    lines = [line for line in xyz.strip().splitlines() if line.strip()]
    if lines and lines[0].strip().isdigit():
        lines = [line for line in xyz.strip().splitlines()[2:] if line.strip()]
    symbols = tuple(line.split()[0].capitalize() for line in lines)
    if not symbols or not all(symbol.isalpha() for symbol in symbols):
        return None
    return symbols

FORMULA_TERM_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


def formula_element_symbols(formula: Any) -> tuple[str, ...] | None:
    """Expand a plain ``formula`` (``H``, ``H2``, ``C2H5O``) to one symbol per atom.

    ``None`` for anything else (charges, parentheses, isotopes), like
    ``xyz_element_symbols``.
    """
    if not isinstance(formula, str) or not formula:
        return None
    terms = FORMULA_TERM_RE.findall(formula)
    if "".join(symbol + count for symbol, count in terms) != formula:
        return None
    symbols: list[str] = []
    for symbol, count in terms:
        if count.startswith("0"):
            return None
        symbols.extend([symbol] * int(count or 1))
    return tuple(symbols)


def is_single_atom_geometry(xyz_text: Any) -> bool:
    """Whether a conformer's own XYZ has exactly one atom.

    The rule tckdb-schemas 0.59 states for an ``sp`` primary calculation is on the
    geometry the conformer carries (``geometry.natoms == 1``), so it is read from
    the XYZ the adapter sends, never from the formula or the SMILES.
    """
    symbols = xyz_element_symbols(xyz_text)
    return symbols is not None and len(symbols) == 1


def formula(element_symbols: Any) -> str:
    counts: dict[str, int] = {}
    for symbol in element_symbols:
        counts[symbol] = counts.get(symbol, 0) + 1
    return "".join(f"{s}{n if n > 1 else ''}" for s, n in sorted(counts.items()))
