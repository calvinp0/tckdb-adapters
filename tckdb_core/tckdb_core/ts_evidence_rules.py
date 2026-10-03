"""Offline replica of TCKDB's transition-state validation-evidence rules.

TCKDB 0.64 added two evidence kinds (``energy_ordering``, ``imaginary_mode``) and tightened
the record: "A pass that the record's own *stated* numbers contradict is refused, a field is
refused on a kind it does not describe, and only a passing ``irc`` record silences
``transition_state_missing_irc_evidence``. ``energy_ordering`` is accepted on the
computed-reaction ... bundles and refused on the standalone transition-state upload ...;
``imaginary_mode`` binds there to the single ``freq`` additional calculation ...
``imaginary_frequency_cm1`` is finite ... a pass with more than one imaginary mode needs that
result to designate the reaction coordinate."

:func:`offline_evidence_errors` checks a built request's ``validation_evidence`` against those
rules: fields belong to the kind, the participant mappings (both sides or neither, non-empty,
1-based, no repeated atom), the source binding on each route (``energy_ordering`` names a
calculation per energy, of the participant it is the energy of, and is refused standalone), one
record per kind, a pass the record's own numbers contradict, an ``imaginary_mode`` count or
frequency that disagrees with the cited frequency result (1 cm^-1), and the TS frequency result
designating its reaction coordinate.

The published pydantic model and JSON Schema also run on every built request (the contract
hook in :mod:`tckdb_core.testing.contract_hook`); the route handlers do not. The two checks that
need the stored calculation, the ownership of each energy's source and the freq-result
cross-check, are replicated here from ``app/services/transition_state_validation.py``.
Any producer's tests can run it on the requests its adapter builds.
"""

import math
import re

COMMON_FIELDS = {"kind", "passed", "rationale", "source_calculation_key"}
IRC_FIELDS = COMMON_FIELDS | {"reactant_participant_mapping", "product_participant_mapping"}
ENERGY_ORDERING_FIELDS = (COMMON_FIELDS - {"source_calculation_key"}) | {"energies"}
IMAGINARY_MODE_FIELDS = COMMON_FIELDS | {"imaginary_frequency_count", "imaginary_frequency_cm1",
                                         "mode_displacement_agrees"}
OTHER_KIND_FIELDS = {"energies", "imaginary_frequency_count", "imaginary_frequency_cm1",
                     "mode_displacement_agrees"}
KIND_FIELDS = {"irc": IRC_FIELDS, "energy_ordering": ENERGY_ORDERING_FIELDS,
               "imaginary_mode": IMAGINARY_MODE_FIELDS}
HARTREE_TO_KJ_MOL = 2625.4996394799
IMAGINARY_TOLERANCE_CM1 = 1.0     # backend: _IMAGINARY_FREQUENCY_TOLERANCE_CM1


def _calc_freq_fields(calc):
    """(n_imag, imag_freq_cm1, reaction_coordinate_mode_index) a freq calculation states, either route's shape."""
    if "freq_result" in calc:
        r = calc["freq_result"]
        return r.get("n_imag"), r.get("imag_freq_cm1"), r.get("reaction_coordinate_mode_index")
    return (calc.get("freq_n_imag"), calc.get("freq_imag_freq_cm1"),
            calc.get("freq_reaction_coordinate_mode_index"))


def _energy_ordering_errors(record, payload):
    """The bundle-route rules on one ``energy_ordering`` record (model + ownership + stated numbers)."""
    errors = []
    energies = record.get("energies") or []
    if not energies:
        return ["energy_ordering requires the energies that were compared"]
    if record.get("source_calculation_key") is not None:
        errors.append("energy_ordering takes no record-level source_calculation_key")
    slots = [(e["participant"], e["energy_kind"]) for e in energies]
    if len(set(slots)) != len(slots):
        errors.append("a participant has more than one energy of a kind")
    sides = {"reactant": payload["reactant_keys"], "product": payload["product_keys"]}
    ts = payload["transition_state"]
    owned = {"ts": {c["key"]: c["type"] for c in [ts["calculation"], *ts.get("calculations", [])]}}
    for species in payload["species"]:
        owned[species["key"]] = {c["key"]: c["type"] for c in [
            *(conf["calculation"] for conf in species["conformers"]), *species.get("calculations", [])]}
    allowed = {"electronic": {"sp", "opt"}, "e0": {"freq"}}
    for energy in energies:
        participant, kind = energy["participant"], energy["energy_kind"]
        if not re.fullmatch(r"(ts|reactant:[1-9][0-9]*|product:[1-9][0-9]*)", participant):
            errors.append(f"bad participant {participant!r}")
            continue
        if not (math.isfinite(energy["energy_hartree"]) and energy["energy_hartree"] <= 0):
            errors.append(f"{participant}: the energy must be finite and not positive")
        if participant == "ts":
            scope = owned["ts"]
        else:
            side, _, position = participant.partition(":")
            if int(position) > len(sides[side]):
                errors.append(f"{participant} is not declared by the reaction")
                continue
            scope = owned[sides[side][int(position) - 1]]
        found = scope.get(energy["source_calculation_key"])
        if found is None:
            errors.append(f"{participant}: its source calculation does not belong to it")
        elif found not in allowed[kind]:
            errors.append(f"{participant}: a {kind} energy cannot come from a {found} calculation")
    for kind in sorted({e["energy_kind"] for e in energies}):
        group = [e for e in energies if e["energy_kind"] == kind]
        names = {e["participant"] for e in group}
        if "ts" not in names or not any(n.startswith("reactant:") for n in names) or not any(
                n.startswith("product:") for n in names):
            errors.append(f"{kind}: needs ts, a reactant and a product")
        if record["passed"]:
            declared = {f"reactant:{i}" for i in range(1, len(sides["reactant"]) + 1)} | {
                f"product:{i}" for i in range(1, len(sides["product"]) + 1)}
            if declared - names:
                errors.append(f"{kind}: a passing record omits {sorted(declared - names)}")
            elif "ts" in names:
                ts_energy = next(e["energy_hartree"] for e in group if e["participant"] == "ts")
                for side in ("reactant", "product"):
                    well = sum(e["energy_hartree"] for e in group if e["participant"].startswith(side))
                    if not ts_energy > well:
                        errors.append(f"{kind}: a passing record puts the saddle point at or below the {side} side")
    return errors


def _imaginary_mode_errors(record, freq_calc):
    """Model rules plus the cross-check against the cited frequency result."""
    errors = []
    count, value = record.get("imaginary_frequency_count"), record.get("imaginary_frequency_cm1")
    if value is not None and not (math.isfinite(value) and value < 0):
        errors.append("imaginary_frequency_cm1 must be negative and finite")
    if count is not None and count < 0:
        errors.append("imaginary_frequency_count must not be negative")
    if count == 0 and value is not None:
        errors.append("imaginary_frequency_cm1 is given but the count is 0")
    if record["passed"] and count == 0:
        errors.append("a pass with no imaginary mode")
    if freq_calc is None:
        return errors
    n_imag, imag, designated = _calc_freq_fields(freq_calc)
    if count is not None and n_imag is not None and count != n_imag:
        errors.append(f"count {count} disagrees with the frequency result's {n_imag}")
    if value is not None and imag is not None and abs(abs(value) - abs(imag)) > IMAGINARY_TOLERANCE_CM1:
        errors.append(f"frequency {value} disagrees with the frequency result's {imag}")
    effective = count if count is not None else n_imag
    if record["passed"] and effective is not None and effective > 1 and designated is None:
        errors.append("a pass with several imaginary modes needs the frequency result to designate the coordinate")
    return errors


def offline_evidence_errors(payload, *, standalone):
    """The 0.64 rules a built request's ``validation_evidence`` can break, checked offline."""
    block = payload if standalone else payload["transition_state"]
    records = block.get("validation_evidence", [])
    errors = []
    kinds = [r["kind"] for r in records]
    if len(set(kinds)) != len(kinds):
        errors.append("more than one record of a kind")
    calcs = [block["primary_opt"], *block.get("additional_calculations", [])] if standalone else [
        block["calculation"], *block.get("calculations", [])]
    for record in records:
        unknown = set(record) - KIND_FIELDS[record["kind"]]
        if unknown:
            errors.append(f"{record['kind']} record carries {sorted(unknown)}")
        if record["kind"] == "irc":
            extra = OTHER_KIND_FIELDS & set(record)
            if extra:
                errors.append(f"irc record carries {sorted(extra)}")
        elif standalone and record["kind"] == "energy_ordering":
            errors.append("energy_ordering is refused on the standalone route")
        elif record["kind"] == "energy_ordering" and not standalone:
            errors.extend(_energy_ordering_errors(record, payload))
        elif record["kind"] == "imaginary_mode":
            errors.extend(_imaginary_mode_errors(
                record, next((c for c in calcs if c["type"] == "freq"
                              and (standalone or c["key"] == record.get("source_calculation_key"))), None)))
        if record.get("passed") is not True and (
                "reactant_participant_mapping" in record or "product_participant_mapping" in record):
            errors.append("a mapping is only meaningful on a passing record")
        sides = [k for k in ("reactant_participant_mapping", "product_participant_mapping") if record.get(k) is not None]
        if len(sides) == 1:
            errors.append("a one-sided participant mapping")
        for key in sides:
            mapping = record[key]
            if not mapping:
                errors.append(f"{key} is empty")
            for participant, atoms in mapping.items():
                if not re.fullmatch(r"(reactant|product):[1-9][0-9]*", participant):
                    errors.append(f"{key}: bad participant key {participant!r}")
                if not atoms or any(not isinstance(a, int) or a < 1 for a in atoms):
                    errors.append(f"{key}[{participant}]: atom indices must be 1-based integers")
                if len(set(atoms)) != len(atoms):
                    errors.append(f"{key}[{participant}] repeats an atom index")
        # the source binding
        if record["kind"] in ("irc", "imaginary_mode"):
            wanted = {"irc": "irc", "imaginary_mode": "freq"}[record["kind"]]
            of_type = [c for c in calcs if c["type"] == wanted]
            if standalone:
                if record.get("source_calculation_key") is not None:
                    errors.append("the standalone route has no key namespace: source_calculation_key must be omitted")
                if len(of_type) != 1:
                    errors.append(f"binds to the single {wanted} calculation, found {len(of_type)}")
            else:
                owned = {c["key"]: c["type"] for c in calcs}
                if owned.get(record.get("source_calculation_key")) != wanted:
                    errors.append(f"source_calculation_key must name a {wanted} calculation of this transition state")
    return errors
