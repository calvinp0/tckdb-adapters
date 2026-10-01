"""What ARC ``output.yml`` schema 1.3 states about levels, programs, routes and isotopes.

Schema 1.3 (ARC PR #1059) exports, per species and TS record, the level of each
job whose log is exported (``levels``: ``opt``, ``freq``, ``sp``, ``composite``,
``irc``), so under ``adaptive_levels`` the adapter no longer replays
``restart.yml``. Everything here is pure: it reads the record and says what ARC
stated, or ``None`` / not-decided where it stated nothing, and the caller then
falls back to the pre-1.3 behaviour (header levels, ``restart.yml``).

A level inside a record states **no** ``software`` (ARC's ``Level.software`` is
only a deduction); the program of a job is ``ess_software[<job>]`` read from the
log itself.

The module also holds the isotope rule for geometries (TCKDB's
``species_geometry_isotope_mismatch``) and the route-line helper.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The job keys of a record's ``levels`` object.
RECORD_LEVEL_KEYS = ("opt", "freq", "sp", "composite", "irc")


def levels_of(record: Any) -> Mapping[str, Any] | None:
    """The record's schema-1.3 ``levels`` object, or ``None`` (a record from output.yml 1.2 or older)."""
    levels = record.get("levels") if isinstance(record, Mapping) else None
    return levels if isinstance(levels, Mapping) else None


def has_levels(record: Any) -> bool:
    return levels_of(record) is not None


def _level(levels: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = levels.get(key)
    return value if isinstance(value, Mapping) and value.get("method") else None


@dataclass(frozen=True)
class RecordedLevel:
    """A level ARC stated for ``kind`` and the ESS job key whose program and banner belong to it."""

    level: Mapping[str, Any] | None
    job_key: str | None


def is_composite_run(record: Any) -> bool:
    """Whether the record's geometry and energy come from a composite-method job (1.3 records only)."""
    return has_levels(record) and bool(record.get("composite_log"))


def recorded_level(record: Any, kind: str) -> RecordedLevel | None:
    """The level ARC's 1.3 ``levels`` states for the job behind ``kind`` (``opt``, ``freq``, ``sp``, ``composite``).

    ``None`` means "not decided here": the record has no ``levels`` object, or the
    job's log is exported but its level was not recorded (a restart that predates
    level recording), and the caller keeps its pre-1.3 resolution. A returned
    ``RecordedLevel`` with ``level=None`` is ARC's statement that no job of that
    kind is exported (no log), or, for a composite run's frequencies, that the level
    is not named; the caller must not fill it from a header level.

    * ``opt``: ``levels.opt``; a composite run (no opt log, a composite log) takes
      ``levels.composite``, the composite job being what produced the geometry
      (``composite_log`` is "the job log from which a composite run's geometry and
      energy are read"); a monoatomic species has no opt job and its single-point
      log is its ``opt_log``, so ``levels.sp``.
    * ``freq``: ``levels.freq``; ``null`` also when no freq job ran, or for a
      composite job, whose own geometry-level frequencies the composite level does
      not name; a ``freq_log`` that is the composite log pairs with
      ``levels.composite``.
    * ``sp``: ``levels.sp``; with no sp log (or one that is the opt log) the energy
      is read from the opt log (``levels.opt``) or, for a composite run, from the
      composite log (``levels.composite``).
    """
    levels = levels_of(record)
    if levels is None:
        return None
    composite_log = record.get("composite_log")
    composite = _level(levels, "composite")
    if kind == "composite":
        return RecordedLevel(composite, "composite") if composite else None
    if kind == "opt":
        level = _level(levels, "opt")
        if level:
            return RecordedLevel(level, "opt")
        opt_log = record.get("opt_log")
        if composite_log and (not opt_log or opt_log == composite_log):
            return RecordedLevel(composite, "composite" if composite else None)
        sp = _level(levels, "sp")
        if opt_log and record.get("sp_log") == opt_log and sp:
            return RecordedLevel(sp, "sp")  # a monoatomic: no opt job, its sp log is its opt_log
        return None if opt_log else RecordedLevel(None, None)
    if kind == "freq":
        level = _level(levels, "freq")
        if level:
            return RecordedLevel(level, "freq")
        freq_log = record.get("freq_log")
        if freq_log and composite_log and freq_log == composite_log:
            return RecordedLevel(composite, "composite" if composite else None)
        # No freq log: no freq job. A composite job's own frequencies are at a level
        # the composite level does not name (levels.freq is null by design). Either
        # way no header level may stand in.
        return None if freq_log else RecordedLevel(None, None)
    if kind == "sp":
        level = _level(levels, "sp")
        if level:
            return RecordedLevel(level, "sp")
        sp_log = record.get("sp_log")
        if sp_log and composite_log and sp_log == composite_log:
            return RecordedLevel(composite, "composite" if composite else None)
        source = sp_energy_source(record)
        if source == "opt":
            opt = _level(levels, "opt")
            return RecordedLevel(opt, "opt") if opt else None
        if source == "composite":
            return RecordedLevel(composite, "composite" if composite else None)
        return None if sp_log else RecordedLevel(None, None)
    return None


def sp_energy_source(record: Any) -> str | None:
    """Where a 1.3 record's single-point energy comes from when no sp job of its own ran.

    ``"opt"``: read from the optimization log (no ``sp_log``, or an ``sp_log`` that is
    the opt log, which ARC writes when the sp level equals the opt level);
    ``"composite"``: read from the composite log; ``None``: an sp job's own log, or
    no energy source. A monoatomic species, whose sp log is its ``opt_log`` with no
    opt job (``levels.opt`` null), is an sp job's own log. Only meaningful for a
    record with ``levels``.
    """
    levels = levels_of(record)
    if levels is None:
        return None
    sp_log, opt_log, composite_log = record.get("sp_log"), record.get("opt_log"), record.get("composite_log")
    if sp_log:
        if opt_log and sp_log == opt_log and _level(levels, "opt"):
            return "opt"
        if composite_log and sp_log == composite_log:
            return "composite"
        return None
    if opt_log:
        return "opt"
    if composite_log:
        return "composite"
    return None


def software_job_key(record: Any, kind: str) -> str:
    """The ``ess_software`` / ``ess_versions`` key behind calculation ``kind`` (``opt``/``freq``/``sp``).

    Differs from ``kind`` only on a 1.3 record whose level came from another job
    (a composite run's geometry and energy, a monoatomic's sp log).
    """
    if kind in ("opt", "freq", "sp"):
        recorded = recorded_level(record, kind)
        if recorded is not None and recorded.job_key:
            return recorded.job_key
    return kind


def distinct_levels(levels: Sequence[Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for level in levels:
        if isinstance(level, Mapping) and level and level not in out:
            out.append(level)
    return out


def irc_recorded_level(ts_record: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any] | None]:
    """``(stated, level)`` of a TS's IRC jobs from a 1.3 record.

    ``levels.irc``, else the one level every IRC log's ``irc_log_levels`` entry
    shares. ``stated=True, level=None`` means the logs ran at different levels, so
    no single IRC calculation can be labelled; ``stated=False`` means ARC recorded
    nothing for the IRC jobs.
    """
    levels = levels_of(ts_record)
    if levels is None:
        return False, None
    level = _level(levels, "irc")
    if level:
        return True, level
    per_log = ts_record.get("irc_log_levels")
    if isinstance(per_log, Sequence) and not isinstance(per_log, str):
        stated = distinct_levels(per_log)
        if len(stated) == 1 and all(isinstance(x, Mapping) and x for x in per_log):
            return True, stated[0]
        if len(stated) >= 1:
            return True, None
    return False, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

#: Route-line field per calculation kind (1.3 ``*_route``).
ROUTE_FIELDS = {"opt": "opt_route", "freq": "freq_route", "sp": "sp_route"}


def route_for_job(record: Mapping[str, Any], kind: str) -> str | None:
    """The observed keyword line ARC states for calculation ``kind``, or ``None``.

    On a composite run the opt, freq and sp calculations are the composite job
    (``composite_route``). ``sp_route`` is the opt job's line when the sp result
    was taken from the opt log, so it states the opt job's route either way.
    """
    if not has_levels(record):
        return None
    key = software_job_key(record, kind)
    field = "composite_route" if key == "composite" else ROUTE_FIELDS.get(key)
    value = record.get(field) if field else None
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------
# Isotopes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Placeholder primary opt
# ---------------------------------------------------------------------------

def primary_opt_placeholder(record: Any) -> str | None:
    """Why a 1.3 record's primary ``opt`` calculation is a placeholder, else ``None``.

    TCKDB requires every conformer's primary calculation to be an ``opt``.

    * ``"composite"``: the geometry came from a composite job (Gaussian optimizes
      the geometry at an internal level, B3LYP/CBSB7 for CBS-QB3, that ARC does
      not export); the opt is filed at the composite level ARC states.
    * ``"no_opt_job"``: ARC exports an sp or freq log but no opt job (no
      ``opt_log``, no ``levels.opt``, no composite log); the opt is filed at the
      header opt level.

    A monoatomic (its sp log is its ``opt_log``) is not a placeholder case: since
    tckdb-schemas 0.59 its primary is its real ``sp`` and no opt is filed for it
    (the adapter checks the conformer's own XYZ first, so a composite-run atom
    never reaches this).
    """
    levels = levels_of(record)
    if levels is None:
        return None
    recorded = recorded_level(record, "opt")
    if recorded is not None and recorded.job_key == "composite":
        return "composite"
    if (recorded is not None and recorded.level is None and not record.get("opt_log")
            and not record.get("composite_log") and (record.get("sp_log") or record.get("freq_log"))):
        return "no_opt_job"
    return None


# ---------------------------------------------------------------------------
# Route vs level
# ---------------------------------------------------------------------------

_ROUTE_PAIR_RE = re.compile(r"^([^/=\s]+)/([^/=\s]+)$")


def _norm_method(text: str) -> str:
    return text.lower().replace("-", "").replace("_", "").replace(" ", "")


def _norm_basis(text: str) -> str:
    out = text.lower().replace("(d,p)", "**").replace("(d)", "*")
    return out.replace("-", "").replace("_", "").replace(" ", "")


def route_method_basis(route: Any) -> tuple[str, str] | None:
    """The one ``method/basis`` token of a Gaussian-style route line, or ``None``.

    Only an unambiguous line counts: exactly one whitespace-delimited token of
    the form ``a/b`` with no ``=``, and the line is not a composite route
    (``#CBS-QB3 opt freq``) or an Orca ``!`` line (which have no such token).
    """
    if not isinstance(route, str):
        return None
    pairs = []
    for token in route.split():
        match = _ROUTE_PAIR_RE.match(token)
        if match:
            pairs.append(match.groups())
    return pairs[0] if len(pairs) == 1 else None


def route_contradicts_level(route: Any, level: Mapping[str, Any] | None) -> str | None:
    """A description when ``route`` clearly names another method or basis than ``level``, else ``None``.

    The route is what ran (observed); the level is what was requested. A Gaussian
    ``u`` or ``ro`` prefix on the route's method is the spin treatment, not the
    method (``ub3lyp`` is ``b3lyp``). Only a level with a method and a basis is
    compared; a dispersion carried in the level's method, or a route that ARC
    could not read, is never flagged.
    """
    pair = route_method_basis(route)
    if pair is None or not isinstance(level, Mapping):
        return None
    method, basis = level.get("method"), level.get("basis")
    if not method or not basis:
        return None
    r_method, r_basis = _norm_method(pair[0]), _norm_basis(pair[1])
    l_method, l_basis = _norm_method(str(method)), _norm_basis(str(basis))
    candidates = {r_method}
    for prefix in ("ro", "u", "r"):
        if r_method.startswith(prefix):
            candidates.add(r_method[len(prefix):])
    if l_method not in candidates or r_basis != l_basis:
        return f"route {pair[0]}/{pair[1]} vs level {method}/{basis}"
    return None
