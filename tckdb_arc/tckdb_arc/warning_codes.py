"""The registry of warning codes the ARC adapter emits.

Each member is the ``code`` string a sidecar's ``warnings`` entry carries plus a one-line
description. The adapter emits them through :func:`tckdb_arc.adapter._self_check`, which
renders an ``AdapterWarning`` into the dict the sidecar has always held::

    {"code": ..., "message": ..., "field": ..., "context": {"source": "tckdb_arc_self_check", ...}}

Codes the shared upload code emits (every producer inherits them) are
:class:`tckdb_core.warning_codes.CoreWarning`. Codes that come from tckdb-schemas' shared
rules (for example the enthalpy-declaration refusals of
``tckdb_schemas.enthalpy_reference``) pass through the adapter unchanged and are owned
there, not here.

``docs/contract/WARNING_CODES.md`` is generated from this registry and the core's:
``python tools/gen_warning_codes.py`` regenerates it, and a test fails when it is stale.
"""

from __future__ import annotations

from tckdb_core.warning_codes import CodedEnum, CoreWarning, registry


class ArcWarning(CodedEnum):
    """Codes emitted by the ARC adapter, grouped by what they report."""

    # --- species and calculation levels -------------------------------------------------
    IRC_ENDPOINT_SPECIES_SKIPPED = (
        "irc_endpoint_species_skipped",
        "A species is an IRC endpoint of a transition state, not a stationary species of the run, so it is not uploaded.",
    )
    LEVEL_CONTRADICTED_BY_ROUTE = (
        "level_contradicted_by_route",
        "The observed route line names another method or basis than the stated level, so the calculation is omitted.",
    )
    COMPOSITE_GEOMETRY_LEVEL_NOT_STATED = (
        "composite_geometry_level_not_stated",
        "The geometry came from a composite job whose level is not stated, so the placeholder optimisation is not filed.",
    )
    PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB = (
        "primary_opt_placeholder_no_opt_job",
        "An sp or freq log is exported but no optimisation job, so the primary optimisation is a placeholder at the header level.",
    )
    LEVEL_METHOD_IS_COMPOUND = (
        "level_method_is_compound",
        "A level's method contains '//' (two levels of theory), so the level is not sent and the calculation is not built.",
    )
    OPT_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE = (
        "opt_level_adaptive_not_attributable",
        "Under adaptive levels the optimisation level cannot be attributed to this record, so the calculation is omitted.",
    )
    FREQ_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE = (
        "freq_level_adaptive_not_attributable",
        "Under adaptive levels the frequency level cannot be attributed to this record, so the calculation is omitted.",
    )
    SP_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE = (
        "sp_level_adaptive_not_attributable",
        "Under adaptive levels the single-point level cannot be attributed to this record, so the calculation is omitted.",
    )
    SCAN_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE = (
        "scan_level_adaptive_not_attributable",
        "Under adaptive levels the scan level cannot be attributed to this record, so the calculation is omitted.",
    )
    IRC_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE = (
        "irc_level_adaptive_not_attributable",
        "Under adaptive levels the IRC level cannot be attributed to this record, so the calculation is omitted.",
    )

    # --- conformers and geometry --------------------------------------------------------
    CONFORMER_NOT_ESS_OPTIMIZED = (
        "conformer_geometry_not_esss_optimized",
        "Screened conformers were not uploaded: their geometries were not optimised by an ESS at a stated level.",
    )
    CONFORMER_PROGRAM_NOT_STATED = (
        "conformer_program_not_stated",
        "Screened conformers were not uploaded: the level of their optimisation is stated but not the program.",
    )
    CONFORMER_LEVEL_NOT_STATED = (
        "conformer_level_not_stated",
        "Screened conformers were not uploaded: the level they were screened at is not stated.",
    )
    GEOMETRY_ISOTOPES_NOT_STATED = (
        "geometry_isotopes_not_stated",
        "A geometry of an isotopically substituted species was left out: no isotope list matches the species' own geometry.",
    )

    # --- thermo: enthalpy declaration and standard state --------------------------------
    ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED = (
        "enthalpy_atom_corrections_not_applied",
        "The atom energy corrections were not applied to the enthalpies, so they are stripped from the thermo block.",
    )
    ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH = (
        "enthalpy_atom_corrections_level_mismatch",
        "The atom energies applied were from another level than the energy level, so the enthalpies are stripped.",
    )
    ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE = (
        "enthalpy_atom_corrections_level_unverifiable",
        "The level the atom energies were applied at cannot be checked against the energy level, so the enthalpies are stripped.",
    )
    ENTHALPY_NOT_FINITE = (
        "enthalpy_not_finite",
        "An enthalpy value is not a finite number, so the enthalpy content of the thermo block is stripped.",
    )
    ENTHALPY_NOT_FORMATION_MAGNITUDE = (
        "enthalpy_not_formation_magnitude",
        "An enthalpy is too large to be a formation enthalpy (it looks like a raw absolute energy), so it is stripped.",
    )
    ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES = (
        "enthalpy_formation_unverifiable_light_species",
        "A light species' raw energy is small enough to pass the magnitude guard, so its enthalpy cannot be verified without the flag.",
    )
    ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE = (
        "enthalpy_adaptive_levels_unverifiable",
        "Under adaptive levels the energy level behind the enthalpies cannot be verified, so they are stripped.",
    )
    THERMO_REFERENCE_PRESSURE_NOT_STATED = (
        "thermo_reference_pressure_not_stated",
        "The standard-state pressure of the entropies is not recorded or unusable, so reference_pressure_bar is omitted, never defaulted.",
    )

    # --- energy corrections -------------------------------------------------------------
    CORRECTION_TABLE_METHOD_SPLIT = (
        "correction_table_method_split",
        "An Arkane level string names a correction table, not a method; the scheme is sent with the table's stem as method.",
    )
    BAC_FREQUENCY_LEVEL_CONFLICT = (
        "bac_frequency_level_conflict",
        "A bond-additivity table's key and the stated frequency job disagree on the frequency level, so none is sent.",
    )
    ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT = (
        "energy_correction_scheme_software_conflict",
        "A correction table's Arkane key and the correction level name different software, so the scheme is omitted.",
    )
    ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML = (
        "atom_energy_record_not_deposited_aec_yml",
        "The atom energies came from ARC's own AEC.yml and cannot be deposited as a database record, so the correction is omitted.",
    )
    BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE = (
        "bac_correction_omitted_components_incomplete",
        "A Petersson bac_total was not sent: its bond decomposition is missing or does not sum to the total.",
    )
    BAC_TYPE_NOT_STATED = (
        "bac_type_not_stated",
        "Bond corrections are applied but the header bac_type is not 'p' or 'm'; the scheme is taken from the record's corrections.",
    )

    # --- statmech and torsions ----------------------------------------------------------
    FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL = (
        "freq_scale_factor_fitted_for_other_level",
        "The run-wide frequency scale factor was fitted for another level than this record's frequencies.",
    )
    STATMECH_TREATMENT_NOT_STATED = (
        "statmech_treatment_not_stated",
        "The treatment Arkane applied to the rotors cannot be told from the output, so the statmech treatment is omitted.",
    )
    TORSION_SCAN_NOT_BUILT = (
        "torsion_scan_not_built",
        "A torsion's scan was exported but could not be built, so the torsion is kept without its source scan.",
    )
    TORSION_NOT_SENT = (
        "torsion_not_sent",
        "A torsion has no usable atom indices and the upload refuses a torsion without coordinates, so it is not sent.",
    )
    TORSION_WITHOUT_COORDINATES = (
        "torsion_without_coordinates",
        "A torsion is sent without dihedral coordinates because its atom indices are missing or unusable.",
    )
    REJECTED_TORSION_NOT_SENT = (
        "rejected_torsion_not_sent",
        "A rejected rotor has no usable atom indices, so it is not sent as a torsion.",
    )

    # --- composite energies -------------------------------------------------------------
    G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03 = (
        "g4_energy_loader_shifted_label_gaussian16_a03",
        "A G4-family composite run on Gaussian 16 A.03 has its 0 K label shifted by one entry, so its energies are withheld.",
    )

    # --- transition states: guesses, IRC, reaction coordinate ---------------------------
    TS_GUESS_LEVEL_NOT_STATED = (
        "ts_guess_level_not_stated",
        "A TS guess's path-search calculation was not uploaded: no level of theory is exported for it.",
    )
    TS_GUESS_SOFTWARE_NOT_STATED = (
        "ts_guess_software_not_stated",
        "A TS guess's path-search calculation was not uploaded: no program is exported for it.",
    )
    IRC_SOFTWARE_NOT_STATED = (
        "irc_software_not_stated",
        "The IRC calculation was not uploaded: no program is stated for its logs.",
    )
    IRC_LEVEL_NOT_STATED = (
        "irc_level_not_stated",
        "The IRC calculation was not uploaded: its level is not stated and cannot be attributed.",
    )
    IRC_LEVEL_ASSUMED_OPT_LEVEL = (
        "irc_level_assumed_opt_level",
        "The IRC level is not exported and is assumed equal to the optimisation level.",
    )
    IRC_DIRECTION_NOT_STATED = (
        "irc_direction_not_stated",
        "Some IRC points have no stated direction, so they are sent without one.",
    )
    TS_REACTION_COORDINATE_NOT_DESIGNATED = (
        "ts_reaction_coordinate_not_designated",
        "A transition state's reaction coordinate can be designated neither from the output nor by TCKDB's tau, so the record is refused.",
    )

    # --- transition states: validation evidence -----------------------------------------
    TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION = (
        "ts_irc_evidence_without_irc_calculation",
        "An IRC verdict is stated but the upload has no IRC calculation to bind it to, so the evidence is not sent.",
    )
    TS_ENERGY_ORDERING_NOT_SENT = (
        "ts_energy_ordering_evidence_not_sent",
        "The energy_ordering evidence is not sent: a participant energy or calculation is missing, or the numbers contradict the verdict.",
    )
    TS_IMAGINARY_MODE_NOT_SENT = (
        "ts_imaginary_mode_evidence_not_sent",
        "The imaginary_mode evidence is not sent: its frequency calculation or designation is missing or contradicts the verdict.",
    )
    TS_NMD_FORCED_CONTRADICTS_INDEX = (
        "ts_nmd_forced_contradicts_reaction_coordinate_index",
        "A reaction-coordinate index is stated beside a forced normal-mode-displacement pass, so the index is not used.",
    )
    IRC_PARTICIPANT_MAPPING_NOT_SENT = (
        "ts_irc_participant_mapping_not_sent",
        "A stated IRC participant mapping cannot be sent as TCKDB participant mappings, so neither side is sent.",
    )

    # --- reactions ----------------------------------------------------------------------
    ATOM_MAP_TS_ORDER_NOT_STATED = (
        "reaction_atom_map_ts_order_not_stated",
        "A reactant-to-product atom map is stated but the TS atom order is not, so no atom_map is sent.",
    )
    REACTION_TS_ATOM_MAP_NOT_SENT = (
        "reaction_ts_atom_map_not_sent",
        "No atom_map was sent for a reaction's transition state; TCKDB will report reaction_atom_map_absent.",
    )
    REACTION_STOICHIOMETRY_NOT_STATED = (
        "reaction_stoichiometry_not_stated",
        "A reaction's collapsed (sorted, de-duplicated) participant labels are not atom-balanced and no stoichiometry is stated, so the reaction is not uploaded.",
    )
    REACTION_SPECIES_LABELS_CONTRADICTED = (
        "reaction_species_labels_contradicted",
        "The reaction's stated species labels contradict its reactant or product labels, so the reaction is not uploaded.",
    )


#: The adaptive-level code by calculation kind (``opt``, ``freq``, ``sp``, ``scan``, ``irc``).
ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE = {
    "opt": ArcWarning.OPT_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE.value,
    "freq": ArcWarning.FREQ_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE.value,
    "sp": ArcWarning.SP_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE.value,
    "scan": ArcWarning.SCAN_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE.value,
    "irc": ArcWarning.IRC_LEVEL_ADAPTIVE_NOT_ATTRIBUTABLE.value,
}


# ---------------------------------------------------------------------------
# docs/contract/WARNING_CODES.md
# ---------------------------------------------------------------------------

DOC_RELATIVE_PATH = "docs/contract/WARNING_CODES.md"


def render_markdown() -> str:
    """``docs/contract/WARNING_CODES.md``, generated from the core and ARC registries."""
    lines = [
        "# Warning codes",
        "",
        "<!-- generated by `python tools/gen_warning_codes.py`; do not edit by hand -->",
        "",
        "Every self-check finding the ARC adapter writes to a sidecar's `warnings` list has the",
        "server's shape, `{code, message, field, context}`, with `context.source` set to",
        "`tckdb_arc_self_check`. The `code` is registered below: the ARC adapter's own codes",
        "(`tckdb_arc/warning_codes.py`, `ArcWarning`) and the codes the shared upload code emits",
        "(`tckdb_core/warning_codes.py`, `CoreWarning`; the `source` is then the producer's).",
        "",
        "The adapter is not the only emitter of warnings: the thermo enthalpy guard also passes",
        "through the refusal codes of `tckdb_schemas.enthalpy_reference` (owned by tckdb-schemas),",
        "and the sidecar carries the server's own warnings beside these.",
        "",
    ]
    for title, enum_cls in (("ARC adapter (`ArcWarning`)", ArcWarning),
                            ("Shared upload code (`CoreWarning`)", CoreWarning)):
        lines += [f"## {title}", "", "| Code | Meaning |", "|------|---------|"]
        lines += [f"| `{code}` | {description} |" for code, description, _ in registry(enum_cls)]
        lines.append("")
    return "\n".join(lines)
