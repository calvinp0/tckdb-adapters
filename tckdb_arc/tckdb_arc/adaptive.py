"""Read what ARC's project files say about levels that ``output.yml`` cannot.

``output.yml`` records one level per run (``opt_level``, ``freq_level``,
``sp_level``, ``composite_method``) and does not record ``adaptive_levels``
(BRIDGE_ROADMAP A2b, B2), nor the IRC or conformer levels (A3, A4, B3). Two
project files do:

* ``<project>/restart.yml``, written by ARC (``arc/main.py`` ``as_dict``):
  ``adaptive_levels`` as a list of ``{'atom_range': [lo, hi | 'inf'],
  'levels': {'<job types>': <Level dict>}}``; ``irc_level`` (only when it
  differs from the settings default); ``conformer_opt_level``; ``job_types``;
  and ``species`` (each with its ``label``, and ``adaptive_lot_n_heavy`` when
  ARC overrode the heavy-atom count that keys its level choice).
* the user's ``input.yml``: ``adaptive_levels`` with levels usually spelled as
  strings, and no per-species state.

``restart.yml`` allows an exact answer, because ARC's own rule can be
replayed: ``Scheduler.determine_adaptive_level`` (``arc/scheduler.py``) takes
the species' heavy-atom count ``n`` (``adaptive_lot_n_heavy`` when set, else
the number of non-``H`` atoms), picks the range with ``lo <= n <= hi`` (or
``hi == 'inf'`` and ``n >= lo``), and returns the level of the entry whose job
types contain the job type, exactly (no case folding), else the run's regular
level. ``input.yml`` alone can only say which job types the adaptive levels
name.
"""

from __future__ import annotations

import copy
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tckdb_arc._vendor import read_yaml_file

logger = logging.getLogger(__name__)

RESTART_FILENAME = "restart.yml"
INPUT_FILENAME = "input.yml"

#: ``attribute_level`` outcomes besides a level dict.
RUN_LEVEL = "run_level"  # the job type is not named in the species' range
UNDETERMINABLE = "undeterminable"  # the species' level cannot be worked out


@dataclass(frozen=True)
class AdaptiveLevels:
    """An adaptive-levels run: the job types its entries assign levels to.

    ``job_types`` are exact strings, as ARC matches them (case-sensitive).
    """

    job_types: frozenset[str]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class RestartInfo:
    """What ``restart.yml`` records about levels.

    ``ranges`` holds ARC's adaptive levels as ``(lo, hi, {job types: level})``
    in file order, ``hi`` being an int or ``'inf'``. ``species`` maps each
    species label to its ``adaptive_lot_n_heavy`` (or ``None``) and its rotors'
    ``directed_scan_type`` by rotor index.
    """

    ranges: tuple[tuple[int, Any, Mapping[tuple[str, ...], Mapping[str, Any]]], ...]
    species: Mapping[str, Mapping[str, Any]]
    irc_level: Mapping[str, Any] | None
    conformer_opt_level: Mapping[str, Any] | None
    job_types: Mapping[str, Any] = field(default_factory=dict)

    @property
    def adaptive_job_types(self) -> frozenset[str]:
        return frozenset(t for _, _, levels in self.ranges for key in levels for t in key)

    def species_n_heavy_override(self, label: Any) -> int | None:
        entry = self.species.get(label) if isinstance(label, str) else None
        value = entry.get("adaptive_lot_n_heavy") if entry else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def irc_endpoint_ts(self, label: Any, ts_labels: Any) -> str | None:
        """The TS label ``label`` is an IRC endpoint of, per ``restart.yml``; else ``None``.

        ARC creates each endpoint as ``ARCSpecies(label='IRC_<ts>_<n>',
        irc_label=<ts>)`` and appends the endpoint's label to the TS's own
        ``irc_label`` (``ARC:arc/scheduler.py:4167-4178``); ``irc_label`` reaches
        ``restart.yml`` through ``ARCSpecies.as_dict``. A species is an endpoint
        when its entry is not a TS, names a TS in ``ts_labels`` (output.yml's
        transition states), and, when that TS's entry is present, appears in the
        TS's ``irc_label``. No entry means no answer.
        """
        entry = self.species.get(label) if isinstance(label, str) else None
        if not entry or entry.get("is_ts") is not False:
            return None
        ts = entry.get("irc_label")
        if not isinstance(ts, str) or not ts or ts not in ts_labels:
            return None
        ts_entry = self.species.get(ts)
        if ts_entry is not None:
            endpoints = ts_entry.get("irc_label")
            if not isinstance(endpoints, str) or label not in endpoints.split():
                return None
        return ts

    def scan_job_type(self, label: Any, rotor_index: int) -> str | None:
        """``'scan'`` (ESS) or ``'directed_scan'`` for a species' rotor; ``None`` if unknown."""
        entry = self.species.get(label) if isinstance(label, str) else None
        types = entry.get("scan_types") if entry else None
        if not isinstance(types, Mapping) or rotor_index not in types:
            return None
        scan_type = types[rotor_index]
        if scan_type is None or not str(scan_type).strip() or str(scan_type).strip() == "ess":
            return "scan"
        return "directed_scan"

    def attribute_level(
        self, label: Any, job_type: str, n_heavy_from_geometry: int | None,
    ) -> Mapping[str, Any] | str:
        """The level ARC ran ``job_type`` at for the species labelled ``label``.

        A level dict when the species' range names the job type; ``RUN_LEVEL``
        when it does not (ARC then uses the run's regular level);
        ``UNDETERMINABLE`` when there is no adaptive spec, no ``species`` entry
        for the label (restart state absent), no heavy-atom count, or no range
        containing it.
        """
        if not self.ranges or not isinstance(label, str) or label not in self.species:
            return UNDETERMINABLE
        n = self.species_n_heavy_override(label)
        if n is None:
            n = n_heavy_from_geometry
        if n is None:
            return UNDETERMINABLE
        for lo, hi, levels in self.ranges:
            if (n >= lo) if hi == "inf" else (lo <= n <= hi):
                for key, level in levels.items():
                    if job_type in key:
                        return copy.deepcopy(level)
                return RUN_LEVEL
        return UNDETERMINABLE


def _job_types_in_key(key: Any) -> tuple[str, ...]:
    """ARC's split of a ``levels`` key: whitespace or commas, case preserved."""
    return tuple(str(key).replace(",", " ").split())


def _job_types_named(entries: Any) -> set[str]:
    """Job types named in the ``levels`` mapping of any well-formed entry."""
    named: set[str] = set()
    if not isinstance(entries, (list, tuple)):
        return named
    for entry in entries:
        levels = entry.get("levels") if isinstance(entry, Mapping) else None
        if not isinstance(levels, Mapping):
            continue
        for key in levels:
            named.update(_job_types_in_key(key))
    return named


def _adaptive_entries(document: Any) -> Any:
    return document.get("adaptive_levels") if isinstance(document, Mapping) else None


def _read_mapping(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        document = read_yaml_file(path=str(path))
    except Exception as exc:  # unreadable YAML must never break an upload
        logger.warning("TCKDB level detection: could not read %s: %s", path, exc)
        return None
    return document if isinstance(document, Mapping) else None


def normalize_level(level: Any) -> dict[str, Any] | None:
    """A restart.yml ``Level.as_dict()`` in the shape output.yml writes levels.

    ``arc/output.py`` ``_level_to_dict`` drops ``repr`` and ``compatible_ess``
    and converts ``solvation_scheme_level`` recursively; restart.yml keeps them.
    ``None`` for anything that is not a level with a method.
    """
    if not isinstance(level, Mapping) or not level.get("method"):
        return None
    out = {k: copy.deepcopy(v) for k, v in level.items() if k not in ("repr", "compatible_ess")}
    if "solvation_scheme_level" in out:
        nested = normalize_level(out["solvation_scheme_level"])
        if nested is None:
            out.pop("solvation_scheme_level")
        else:
            out["solvation_scheme_level"] = nested
    return out


def _parse_ranges(entries: Any) -> tuple[tuple[int, Any, dict], ...] | None:
    """ARC's processed ranges, or ``None`` when the list is not well formed.

    A malformed spec is not partially trusted: attribution then falls back to
    refusing the levels the spec names.
    """
    if not isinstance(entries, (list, tuple)) or not entries:
        return None
    ranges = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        atom_range, levels = entry.get("atom_range"), entry.get("levels")
        if (not isinstance(atom_range, (list, tuple)) or len(atom_range) != 2
                or not isinstance(levels, Mapping)):
            return None
        lo, hi = atom_range
        if isinstance(hi, float) and math.isinf(hi):
            hi = "inf"
        if (not isinstance(lo, int) or isinstance(lo, bool)
                or not (hi == "inf" or (isinstance(hi, int) and not isinstance(hi, bool)))):
            return None
        parsed = {}
        for key, level in levels.items():
            normalized = normalize_level(level)
            if normalized is None:
                return None
            parsed[_job_types_in_key(key)] = normalized
        ranges.append((lo, hi, parsed))
    return tuple(ranges)


def read_restart_info(project_directory: str | Path | None) -> RestartInfo | None:
    """``restart.yml``'s level state, or ``None`` when the file is absent or unreadable."""
    if project_directory is None:
        return None
    document = _read_mapping(Path(project_directory) / RESTART_FILENAME)
    if document is None:
        return None
    species: dict[str, dict[str, Any]] = {}
    for record in document.get("species") or []:
        if not isinstance(record, Mapping) or not isinstance(record.get("label"), str):
            continue
        rotors = record.get("rotors_dict")
        scan_types = {
            index: rotor.get("directed_scan_type")
            for index, rotor in rotors.items() if isinstance(rotor, Mapping)
        } if isinstance(rotors, Mapping) else {}
        species[record["label"]] = {
            "adaptive_lot_n_heavy": record.get("adaptive_lot_n_heavy"),
            "scan_types": scan_types,
            "irc_label": record.get("irc_label"),
            "is_ts": record.get("is_ts"),
        }
    job_types = document.get("job_types")
    return RestartInfo(
        ranges=_parse_ranges(document.get("adaptive_levels")) or (),
        species=species,
        irc_level=normalize_level(document.get("irc_level")),
        conformer_opt_level=normalize_level(document.get("conformer_opt_level")),
        job_types=job_types if isinstance(job_types, Mapping) else {},
    )


def detect_adaptive_levels(
    project_directory: str | Path | None,
    input_dict: Mapping[str, Any] | None = None,
) -> AdaptiveLevels | None:
    """Return the run's adaptive levels, or ``None`` when none are found.

    Reads ``restart.yml`` and ``input.yml`` in ``project_directory`` and the
    already-parsed ``input_dict`` (the CLI's ``input.yml``, which may live
    elsewhere). ``None`` also means "cannot tell" when no source is available;
    it is never evidence that the run was not adaptive.
    """
    documents: list[tuple[str, Any]] = []
    if project_directory is not None:
        directory = Path(project_directory)
        for name in (RESTART_FILENAME, INPUT_FILENAME):
            documents.append((name, _read_mapping(directory / name)))
    documents.append(("input_dict", input_dict))

    named: set[str] = set()
    sources: list[str] = []
    for source, document in documents:
        source_named = _job_types_named(_adaptive_entries(document))
        if source_named:
            named |= source_named
            sources.append(source)
    if not sources:
        return None
    return AdaptiveLevels(job_types=frozenset(named), sources=tuple(sources))
