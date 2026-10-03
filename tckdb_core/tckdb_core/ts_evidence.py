"""Transition-state ``validation_evidence`` records: the shapes TCKDB accepts.

TCKDB 0.64 added two evidence kinds beside ``irc`` and tightened the record: a pass the
record's own *stated* numbers contradict is refused, a field is refused on a kind it does
not describe, and only a passing ``irc`` record silences ``transition_state_missing_irc_evidence``.
These builders put already-decided facts into the right record shape, in the field order a
sidecar has always carried, and hold the one numeric rule a producer should apply before
sending (an ``energy_ordering`` verdict its own energies contradict). Deciding *what* the
producer's checks mean, and wording the findings when it omits a record, stays with the
producer; so does reading its check results.

See :mod:`tckdb_core.ts_evidence_rules` for the replica of the server's rules that tests
use to check a built request.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from tckdb_core.physical_constants import E_h_kJmol

KIND_IRC = "irc"
KIND_ENERGY_ORDERING = "energy_ordering"
KIND_IMAGINARY_MODE = "imaginary_mode"

#: The only energy kind a producer states from a single-point (``sp``) energy.
ENERGY_KIND_ELECTRONIC = "electronic"


def finite_float(value: Any) -> float | None:
    """``value`` as a finite float, else ``None`` (a bool is not an energy)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------------------
# irc
# ---------------------------------------------------------------------------

def irc_record(
    passed: bool,
    *,
    source_calculation_key: str,
    rationale: str,
    participant_mappings: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """An ``irc`` record bound to the transition state's ``irc`` calculation.

    ``participant_mappings`` is ``(reactant side, product side)``: both sides or neither,
    and on a passing record only. The caller decides; this builder sends exactly what it
    is given.
    """
    evidence: dict[str, Any] = {
        "kind": KIND_IRC,
        "passed": passed,
        "rationale": rationale,
        "source_calculation_key": source_calculation_key,
    }
    if participant_mappings is not None:
        evidence["reactant_participant_mapping"] = participant_mappings[0]
        evidence["product_participant_mapping"] = participant_mappings[1]
    return evidence


# ---------------------------------------------------------------------------
# energy_ordering
# ---------------------------------------------------------------------------

def energy_entry(
    participant: str, energy_hartree: float, source_calculation_key: str,
    *, energy_kind: str = ENERGY_KIND_ELECTRONIC,
) -> dict[str, Any]:
    """One compared energy of an ``energy_ordering`` record, cited to its own calculation."""
    return {"participant": participant, "energy_kind": energy_kind,
            "energy_hartree": energy_hartree, "source_calculation_key": source_calculation_key}


def side_energy_sums(energies: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """The summed hartree energy of each side (``reactant``, ``product``) of the compared energies."""
    return {side: sum(e["energy_hartree"] for e in energies if e["participant"].startswith(f"{side}:"))
            for side in ("reactant", "product")}


def energy_ordering_contradiction(
    passed: bool, energies: Sequence[Mapping[str, Any]], *, margin_kj_mol: float,
) -> tuple[str, str | None] | None:
    """Why the stated energies contradict the verdict, as ``(reason, reason_code)``, else ``None``.

    TCKDB refuses a passing record whose stated numbers do not put the saddle point
    above each side, so a ``True`` verdict is re-derived from the stated hartree values
    with the producer's ``margin_kj_mol``; when they contradict it the record must not be
    sent. A ``False`` verdict whose stated numbers do satisfy the ordering was computed on
    stale energies and is likewise not sent (its ``reason_code`` is
    ``verdict_contradicted_by_stated_energies``). ``energies[0]`` is the transition state's.
    """
    ts_energy = energies[0]["energy_hartree"]
    wells = side_energy_sums(energies)
    if passed is True:
        for side, well in wells.items():
            if not (ts_energy - well) * E_h_kJmol > margin_kj_mol:
                return (
                    f"the stated sp energies do not put the saddle point more than "
                    f"{margin_kj_mol:g} kJ/mol above the {side} side "
                    f"({ts_energy} vs {well} hartree), so the pass is contradicted by its own numbers",
                    None)
    elif all((ts_energy - well) * E_h_kJmol > margin_kj_mol for well in wells.values()):
        # A False verdict on numbers that satisfy the ordering was computed on stale energies.
        return (
            f"the stated sp energies put the saddle point more than {margin_kj_mol:g} "
            "kJ/mol above both sides, so the failure is contradicted by its own numbers (the verdict "
            "was computed on stale energies)",
            "verdict_contradicted_by_stated_energies")
    return None


def energy_ordering_record(
    passed: bool, rationale: str, energies: list[dict[str, Any]],
) -> dict[str, Any]:
    """An ``energy_ordering`` record (bundle routes only; the standalone route refuses it)."""
    return {"kind": KIND_ENERGY_ORDERING, "passed": passed, "rationale": rationale, "energies": energies}


# ---------------------------------------------------------------------------
# imaginary_mode
# ---------------------------------------------------------------------------

def freq_result_imaginary_facts(
    freq_result: Mapping[str, Any],
) -> tuple[int | None, Any, float | None]:
    """``(n_imag, designated index, frequency)`` a frequency result states, for an ``imaginary_mode`` record.

    Read from the ``freq_result`` the upload sends, so the record cannot disagree with the
    result it cites (TCKDB refuses a count that differs, or a frequency more than 1 cm^-1
    apart). The frequency is the designated reaction-coordinate mode's, or with exactly one
    imaginary mode, ``imag_freq_cm1``; a finite float or ``None`` (not yet negated).
    """
    n_imag = freq_result.get("n_imag")
    n_imag = n_imag if isinstance(n_imag, int) and not isinstance(n_imag, bool) else None
    designated = freq_result.get("reaction_coordinate_mode_index")
    value = None
    if designated is not None:
        value = next((m.get("frequency_cm1") for m in freq_result.get("modes") or []
                      if m.get("mode_index") == designated), None)
    elif n_imag == 1:
        value = freq_result.get("imag_freq_cm1")
    return n_imag, designated, finite_float(value)


def imaginary_mode_record(
    passed: bool,
    *,
    source_calculation_key: str,
    imaginary_frequency_count: int | None,
    frequency_cm1: float | None,
    mode_displacement_agrees: bool | None,
    rationale: str,
) -> dict[str, Any]:
    """An ``imaginary_mode`` record bound to the transition state's ``freq`` calculation.

    ``frequency_cm1`` is negated to TCKDB's negative convention; a ``None`` (or zero)
    frequency and a ``None`` ``mode_displacement_agrees`` (not assessed) are left out.
    On the standalone route the caller drops the key (the record binds to the single freq).
    """
    record: dict[str, Any] = {"kind": KIND_IMAGINARY_MODE, "passed": passed,
                              "source_calculation_key": source_calculation_key}
    if imaginary_frequency_count is not None:
        record["imaginary_frequency_count"] = imaginary_frequency_count
    if frequency_cm1:
        record["imaginary_frequency_cm1"] = -abs(frequency_cm1)
    if mode_displacement_agrees is not None:
        record["mode_displacement_agrees"] = mode_displacement_agrees
    record["rationale"] = rationale
    return record
