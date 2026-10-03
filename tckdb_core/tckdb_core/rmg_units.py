"""RMG kinetics-unit strings mapped to TCKDB's unit enums.

Producers that read RMG-family kinetics (ARC, RMG itself, ChemTrayzer's RMG-style
exports) state an Arrhenius prefactor and an activation energy as RMG unit strings.
The enum values are TCKDB's ``ArrheniusAUnits`` and ``ActivationEnergyUnits``
(``backend/app/db/models/common.py``). The strings are normalised (lower case,
trimmed) before lookup so cosmetic variations do not miss.

A null, empty or unrecognised string maps to ``None``: the caller decides whether to
omit the field or refuse. An unrecognised string logs a debug line so a typo can be
spotted without a warning per reaction.
"""

from __future__ import annotations

from tckdb_core._logging import resolve_log

#: RMG ``A`` unit string (normalised) -> ``ArrheniusAUnits`` value.
RMG_TO_TCKDB_A_UNITS: dict[str, str] = {
    "s^-1": "per_s",
    "1/s": "per_s",
    "cm^3/(mol*s)": "cm3_mol_s",
    "cm^3/(molecule*s)": "cm3_molecule_s",
    "m^3/(mol*s)": "m3_mol_s",
    "cm^6/(mol^2*s)": "cm6_mol2_s",
    "cm^6/(molecule^2*s)": "cm6_molecule2_s",
    "m^6/(mol^2*s)": "m6_mol2_s",
}

#: RMG ``Ea`` unit string (normalised) -> ``ActivationEnergyUnits`` value.
RMG_TO_TCKDB_EA_UNITS: dict[str, str] = {
    "j/mol": "j_mol",
    "kj/mol": "kj_mol",
    "cal/mol": "cal_mol",
    "kcal/mol": "kcal_mol",
}


def normalize_unit_key(text: str | None) -> str | None:
    """Lowercase + strip whitespace so ``" kJ/mol "`` matches ``"kj/mol"``.

    RMG unit strings are not perfectly consistent in case/whitespace; the TCKDB
    enums are. This is the boundary helper.
    """
    if text is None:
        return None
    s = str(text).strip().lower()
    return s or None


def a_units_to_tckdb(units: str | None, *, log=None) -> str | None:
    """Map an RMG ``A_units`` string to a TCKDB ``ArrheniusAUnits`` enum value.

    Returns ``None`` for null/empty input or unrecognized strings; the caller decides
    whether to omit the field or raise. Unrecognized units log a debug line (to
    ``log``, else the package logger).
    """
    key = normalize_unit_key(units)
    if key is None:
        return None
    enum_value = RMG_TO_TCKDB_A_UNITS.get(key)
    if enum_value is None:
        resolve_log(log).debug(
            "TCKDB kinetics: unrecognized A_units %r; field will be omitted.",
            units,
        )
    return enum_value


def ea_units_to_tckdb(units: str | None, *, log=None) -> str | None:
    """Map an RMG ``Ea_units`` (or ``dEa_units``) string to a TCKDB ``ActivationEnergyUnits`` value.

    Same null-or-unknown -> ``None`` policy as :func:`a_units_to_tckdb`.
    """
    key = normalize_unit_key(units)
    if key is None:
        return None
    enum_value = RMG_TO_TCKDB_EA_UNITS.get(key)
    if enum_value is None:
        resolve_log(log).debug(
            "TCKDB kinetics: unrecognized Ea_units %r; field will be omitted.",
            units,
        )
    return enum_value
