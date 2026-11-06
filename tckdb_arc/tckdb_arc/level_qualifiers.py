"""The typed method qualifiers of a level of theory, read from ARC's ``args`` where they are stated.

tckdb-schemas 0.158 (TCKDB ADR 0024) replaced the free-text ``keywords`` of a level of theory with four typed
fields: ``relativistic_hamiltonian``, ``pno_truncation``, ``ri_approximation`` and ``core_treatment`` (now
also ``window``). Each is level identity only when stated. This module sends one only when the level's own
``args`` state it as a whole token of a spelling the group knowledge base confirms for that program; it
never reads a program default, an absence, or a method name (``RI-MP2``, ``DLPNO-CCSD(T)``).

Rules, all deliberately narrow:

* Only ARC's ``args.keyword.general`` (the level-wide simple-keyword line) is read for keywords, and for ORCA
  ``args.block`` values for the ``FC_EWIN`` frozen-core token. A ``keyword.opt`` / ``keyword.freq`` entry is
  job-specific, not a statement about the level, so it is not read. ``args`` given as a string is ARC's
  ``keyword.general``.
* The program is the level's own ``software``; a spelling is read only for the program that owns it.
* Whole tokens, case-insensitive; the lower-case value is sent (TCKDB refuses ``DKH2``).
* Two different values for one field in one level (``zora dkh2``) state nothing: the field is left out.
* ``none`` is never sent: no spelling here is read as the absence of a qualifier.

Table (source: rag-drg knowledge base, see docs/contract/CURRENT_ARC_INTEGRATION.md):

========  ===================================  ==========================  =====================
program   spelling                              field                       value
========  ===================================  ==========================  =====================
orca      TightPNO / NormalPNO / LoosePNO      pno_truncation              tight / normal / loose
orca      RIJCOSX / RIJK / RIJONX              ri_approximation            rijcosx / rijk / rijonx
orca      DKH2, DKH / ZORA / IORA / X2C        relativistic_hamiltonian    dkh2 / zora / iora / x2c
orca      FC_EWIN (``%method FrozenCore``)     core_treatment              window
gaussian  Int(egral)=DKH / DKH2                relativistic_hamiltonian    dkh2
gaussian  Int(egral)=DKH0 / DKHSO / RESC       relativistic_hamiltonian    dkh0 / dkhso / resc
========  ===================================  ==========================  =====================
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_ORCA_KEYWORDS = {
    "tightpno": ("pno_truncation", "tight"),
    "normalpno": ("pno_truncation", "normal"),
    "loosepno": ("pno_truncation", "loose"),
    "rijcosx": ("ri_approximation", "rijcosx"),
    "rijk": ("ri_approximation", "rijk"),
    "rijonx": ("ri_approximation", "rijonx"),
    "dkh2": ("relativistic_hamiltonian", "dkh2"),
    "dkh": ("relativistic_hamiltonian", "dkh2"),   # ORCA manual, simple keywords: "DKH2, DKH" = second order
    "zora": ("relativistic_hamiltonian", "zora"),
    "iora": ("relativistic_hamiltonian", "iora"),
    "x2c": ("relativistic_hamiltonian", "x2c"),
}
_ORCA_BLOCK_TOKENS = {"fc_ewin": ("core_treatment", "window")}

_GAUSSIAN_INTEGRAL_OPTIONS = {
    "dkh": "dkh2",    # G09 Integral: DKH = DKH2 = second order
    "dkh2": "dkh2",
    "dkh0": "dkh0",
    "dkhso": "dkhso",
    "resc": "resc",
}
# ``int=dkh2``, ``integral=(dkh2,grid=ultrafine)``, ``int(dkh2)``: the option list of an Int/Integral keyword.
_GAUSSIAN_INTEGRAL = re.compile(r"(?<![\w-])int(?:egral)?(?:\s*=\s*\(([^)]*)\)|\s*\(([^)]*)\)|\s*=\s*([^\s(]+))")


def _keyword_line(args: Any) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, Mapping):
        keyword = args.get("keyword")
        if isinstance(keyword, Mapping) and isinstance(keyword.get("general"), str):
            return keyword["general"]
        if isinstance(keyword, str):
            return keyword
    return ""


def _block_text(args: Any) -> str:
    if isinstance(args, Mapping) and isinstance(args.get("block"), Mapping):
        return " ".join(v for v in args["block"].values() if isinstance(v, str))
    return ""


def _resolve(found: list[tuple[str, str]]) -> dict[str, str]:
    """One value per field; a field stated with two different values is left out."""
    values: dict[str, set[str]] = {}
    for field, value in found:
        values.setdefault(field, set()).add(value)
    return {field: next(iter(vs)) for field, vs in values.items() if len(vs) == 1}


def level_qualifiers(level: Mapping[str, Any]) -> dict[str, str]:
    """``{field: lower-case token}`` for the method qualifiers ``level['args']`` states for ``level['software']``."""
    software = str(level.get("software") or "").strip().lower()
    args = level.get("args")
    line = _keyword_line(args).lower()
    found: list[tuple[str, str]] = []
    if software == "orca":
        tokens = re.split(r"[\s,]+", line)
        found += [_ORCA_KEYWORDS[t] for t in tokens if t in _ORCA_KEYWORDS]
        block_tokens = re.split(r"[\s,]+", _block_text(args).lower())
        found += [_ORCA_BLOCK_TOKENS[t] for t in (*tokens, *block_tokens) if t in _ORCA_BLOCK_TOKENS]
    elif software == "gaussian":
        for match in _GAUSSIAN_INTEGRAL.finditer(line):
            for option in (match.group(1) or match.group(2) or match.group(3) or "").split(","):
                value = _GAUSSIAN_INTEGRAL_OPTIONS.get(option.strip())
                if value is not None:
                    found.append(("relativistic_hamiltonian", value))
    return _resolve(found)
