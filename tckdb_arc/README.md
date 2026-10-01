# tckdb-arc

Convert ARC `output/output.yml` and portable parser evidence into TCKDB
species, reaction, and transition-state uploads. Payloads and upload metadata
are written locally before any network request, allowing inspection and replay.

Requires Python 3.11+, `tckdb-client` 0.102.x and `tckdb-schemas` 0.64.x.
For development with sibling checkouts:

```bash
python -m pip install ../TCKDB_v2/clients/python \
  ../TCKDB_v2/schemas/python/tckdb-schemas -e './tckdb_arc[test]'
python -m pytest tckdb_arc/tests -q
```

Run these commands from the `tckdb-adapters` repository root. Preview an
existing ARC project whose `input.yml` contains an enabled `tckdb` block:

```bash
tckdb-arc-upload /path/to/project/input.yml --offline
```

Omit `--offline` to use the input file's upload setting. Credentials and upload
destination come from the `tckdb` configuration.

Current ARC exports `parser_evidence.json` for Hessian, IRC, and GSM evidence;
the older `tckdb_evidence.json` contract is also supported. Keep the evidence
file beside `output.yml`. ARC itself is optional: raw-log reparsing requires
ARC on `PYTHONPATH`, whereas portable sidecars work in the base installation.

TCKDB decides what it accepts, and tckdb-schemas 0.52 ships that as a
producer contract generated from TCKDB's routes and models: read it with
`python -m tckdb_schemas.contract --print` (and `--since <version>` for what
moved) before changing a mapping. The test suite validates every payload the
adapter builds against the contract's JSON Schema for its route and pins the
tckdb-schemas line it ran against (`tests/_contract.py`).

Adapter 0.6.0 conforms to that contract where ARC does not state a value:
it omits `reference_pressure_bar` without a recorded pressure (below), omits
`path_search_result.converged` (ARC's TS-guess `success` means only that the
output file exists), GSM `is_climbing_image` (an NEB-CI flag the contract
says string methods ignore), `freq_scale_factor.scale_kind` and
`species_entry.electronic_state_kind` (ARC states neither; TCKDB applies its
own default), and refuses an IRC result when any log's direction is unstated
(`irc_direction_not_stated`, action `irc_result_omitted`; the `irc`
calculation is still sent) instead of claiming `both`. A species or TS record
without a usable integer `charge` or `multiplicity` is refused (the upload is
not built; the sweep reports it) instead of becoming charge 0 or a singlet.
ARC does not export `irc_level`, so the IRC calculation keeps `opt_level` and
every one reports `irc_level_assumed_opt_level`. Kinetics `tunneling_model` is
sent as the contract's lowercase token (`eckart`, unknown methods `other`),
the value TCKDB stored anyway.

Adapter 0.6.1 passes ARC's software provenance through. Each calculation's
ESS banner (`ess_versions`, e.g. `Gaussian 16, Revision C.02`) is split into
`version` `16` and `revision` `C.02` by TCKDB's own shared
`SoftwareReleaseRef` normaliser; a banner naming another program, or of any
other shape, is sent unchanged. Computed-species thermo and statmech name
Arkane (`arkane_version`, `arkane_git_commit`) as their `software_release`.
Each energy-correction scheme names the program its parameters come from as
`scheme.software`: the `software='<name>'` in the record's
`matched_arkane_key` (Arkane's database entry, e.g. `gaussian`), with no
release. It is omitted when the key is absent or names no software, and
omitted with an `energy_correction_scheme_software_conflict` warning when it
disagrees with the correction level's software. Literature is not sent: ARC
records no citation.

Upgrading to 0.6.1 changes the payload hash, and so the idempotency key, of
every ARC upload whose ESS banner is composite (e.g. `Gaussian 16, Revision
C.02`), and of every species upload that now carries Arkane or scheme
software. Replaying a sidecar written before 0.6.1 posts under a new key, so
TCKDB records it as a new deposit rather than replaying the old one.

Adapter 0.6.3 sends corrections and a statmech treatment only as far as the
evidence goes. A Petersson `bac_total` is sent only with a complete bond
decomposition: TCKDB refuses the whole upload for one with no `bond`
component (`bac_total_requires_components`, except on a monatomic species) and
does not check that a partial decomposition sums to the total. ARC drops every
component when any bond lacks a parameter, so a componentless or partly null
Petersson BAC is omitted, with a `bac_correction_omitted_components_incomplete`
warning (reason `no_components`, `component_unusable` or `no_bond_component`);
the AEC correction and the rest of the payload are still sent. This holds for
species, reaction participants and the reaction's TS block. A rotor-aware `statmech_treatment`
and each torsion's `treatment_kind` are sent only when a freq Hessian was found
for the species (Arkane ignores every rotor without a force-constant matrix);
otherwise they are omitted with `statmech_treatment_not_stated` (torsions are
still sent; plain `rrho` needs no Hessian). An omitted BAC is noted on the
thermo record according to `thermo.bond_corrections_applied` (true: Arkane
applied it, not deposited; false: no note; absent: the note states only that
ARC exported a Petersson BAC total that is not deposited). Atom-energy,
Petersson and Melius schemes carry Arkane as `workflow_tool_release`. Its
`git_commit` is the RMG-Py commit Arkane ran from; the correction tables come
from RMG-database, whose commit ARC does not record. So a database-only
revision of a table value for the same key still conflicts with the stored
scheme (a parameter-conflict 422), and each new RMG-Py commit makes a new
scheme row even when the tables are unchanged. The first 0.6.3 upload of an
already-deposited scheme also creates a new row (identity now includes the
workflow tool; the older null-tool row remains). An `atom_energy` scheme now carries
`scheme.atom_params` from ARC's `reference_atom_energies` (Arkane's per-element
atomic energies) with `scheme.units` set to that table's own unit; a table with
a missing or unrecognized unit, or unusable values, is not sent.

Adapter 0.7.0 reads output.yml 1.3 (ARC PR #1059): each record's `levels` (the level of its
opt, freq, sp, composite and IRC jobs, also under `adaptive_levels`) replaces the `restart.yml`
replay, programs come from `ess_software` (including `composite`, `irc` and the xtb GSM) and
each rotor scan's own program, the header `gsm_level` files the GSM path search again, screened
conformers are filed at their `conformer_levels` (force-field geometries are not calculations),
IRC endpoint species are skipped by `irc_endpoint_of`, a composite run's job is filed as the
primary calculation at the composite level and linked under the `composite` role, stated
isotopes ride on the geometries (a species whose stated isotopes contradict its SMILES is
refused), and the observed `*_route` lines become `parameters`. Pre-1.3 output behaves as
before. See `docs/contract/CURRENT_ARC_INTEGRATION.md`, "Levels, programs and isotopes".

Adapter 0.6.4 states a level only when ARC's output or the project's `restart.yml`
supports it. Under `adaptive_levels`, `restart.yml` (adaptive spec, each species'
`adaptive_lot_n_heavy`, rotor scan types) lets the adapter replay ARC's per-species
choice exactly (`opt`, `freq`, `sp`, `composite`, `irc`, `scan`, `directed_scan`, matched
case-sensitively); a species' attributed sp/composite level is the energy level its
enthalpy is checked against, so formation enthalpies survive. When `restart.yml` or the
species entry is missing, or only `input.yml` is available, an `opt` in the spec refuses
the upload, named `sp`/`freq`/`scan`/`irc` calculations are omitted
(`<kind>_level_adaptive_not_attributable`) and a named `sp` or composite level strips
thermo enthalpy (`enthalpy_adaptive_levels_unverifiable`); with only `output.yml` an
adaptive run cannot be detected. The IRC uses the `irc_level` `restart.yml` records;
without one it keeps `opt_level` under `irc_level_assumed_opt_level` (the run's settings
default, unreadable here, assumed equal). Screened alternative conformers are filed at
`restart.yml`'s `conformer_opt_level` (with its program) when `conf_opt` ran and the spec
does not name it, otherwise omitted (`conformer_level_not_stated`). The TS-guess path
search is filed only for NEB, at `neb_level` and the observed NEB program
(`ess_software.neb`); a GSM guess, NEB without `neb_level` or without an observed program
is omitted (`ts_guess_level_not_stated`, `ts_guess_software_not_stated`).
`ts_checks['IRC']`, ARC's IRC verdict, becomes TS `validation_evidence` (`passed` is that
verdict, never `irc_converged`; the rationale is `ARC ts_checks['IRC'] = <verdict>`); no
verdict sends none. Upgrading changes the payload hash, and so the idempotency key, of TS
uploads with a GSM guess and of species uploads whose level attribution changed.

Adapter 0.6.6: thermo and statmech declare `energy_level_of_theory`, the linked sp's own
level with its `spin_treatment` (TCKDB hashes it into the level identity, so ARC's bare level
would be refused); it is omitted when ARC's energy level is not that calculation's (a composite
method, an unattributable adaptive level). ARC's `wavefunction_stability` becomes the
primary opt's `scf_stability`, the job ARC's analysis tests (not when ARC re-optimized at a
derived reference; `stable` only for ARC's `stable` verdict; never on freq/sp). Conformer mode now also sends `statmech` and
`applied_energy_corrections`, built by the computed-species builders, with ARC's rejected
rotors as torsions carrying `invalidated_reason`; it sends no rotor scan (the route accepts
only freq and sp as additional calculations). See `docs/contract/CURRENT_ARC_INTEGRATION.md`.

Adapter 0.6.5 targets tckdb-schemas 0.54 (no wire model change; only the
bundles dry-run route, which the adapter never calls, gained rules) and states
Arkane's full atom-energy convention in the `atom_energy` scheme note: the atomic
energies are subtracted and each atom's gas-phase formation enthalpy less its
thermal correction is added.

Adapter 0.6.7:

- A species on both sides of a reaction (or repeated on one side) is deposited once; the
  computed-reaction bundle repeats its key in `reactant_keys` / `product_keys`, and the
  kinetics `reactant_energy` / `product_energy` links point at its sp once per role.
- A single-atom species is uploaded with the placeholder primary opt both routes require
  and a `monatomic_species_primary_opt_placeholder` warning (ARC runs no optimisation for
  an atom; TCKDB #600 asks for an sp primary).
- Each calculation's artifact batch is sent through `TCKDBClient.request_json`, so the
  artifact sidecar (and `ArtifactUploadOutcome.warnings`) carries the server's warnings,
  the HTTP status, the request id and the replay flag.
- A rotor scan calculation carries its log (`rotor_scans[].source_log`) as an
  `output_log` artifact, and a reaction species' conformer carries `label`.
- IRC endpoint species (`IRC_<ts>_<n>`) are skipped, with `irc_endpoint_species_skipped`,
  when the project's `restart.yml` records them (`irc_label`); without `restart.yml` they
  are uploaded as before.
- The adapter no longer reads keys ARC does not export (`unmapped_smiles`,
  `reactions[].reversible`, `kinetics.degeneracy`, `kinetics.note`, `irc_final_settings`,
  `sp_spin_diagnostic.note`, `electronic_energy_hartree`), except that for output.yml 1.0
  documents `thermo.cp_data` and the atom-energy `parameter_table` are still read.

Upgrading changes the payload hash, and so the idempotency key, of reaction uploads that
had a repeated species or gained conformer labels or scan logs.

Tracked TCKDB releases (added by `tools/tckdb_drift.py --bump`):

<!-- tckdb-drift:changelog -->
- Adapter 0.6.9: tracked TCKDB f22d3a8 (schemas 0.64.0, client 0.102.0).
- Adapter 0.6.8: tracked TCKDB a515fb9 (schemas 0.58.0, client 0.98.0).

Thermo blocks with enthalpy content declare `enthalpy_reference_kind:
formation_298k` (Arkane's H298 and NASA are formation enthalpies at
298.15 K) only when that holds. Blocks with entropy content carry
`reference_pressure_bar` only when ARC recorded the standard-state pressure:
`thermo.standard_state_pressure_pa` converted to bar, when it is a number in
Pa between 0.5 and 2 bar. Otherwise the field is omitted, never defaulted
(TCKDB stores the pressure as not stated), and the omission is reported as
`thermo_reference_pressure_not_stated` with action
`reference_pressure_omitted`; the rest of the block is still sent. Before
adapter 0.6.0 a missing pressure was filled with 1.01325 bar.

ARC output.yml 1.2 records whether Arkane applied atom-energy corrections
(`thermo.atom_corrections_applied`) and whose atom energies it subtracted
(`thermo.atom_corrections_level`). The enthalpy (H298, the NASA fit, point H
and G) is stripped from the block, keeping S298 and point S/Cp, when:

- the switch was off (`enthalpy_atom_corrections_not_applied`);
- the correction level is not the species' energy level, `composite_method`
  or else `sp_level`, compared by method (with its dispersion folded in) and
  basis, normalized exactly as ARC's own level matching (ARC `1977e53b`
  `_normalized_method_and_basis`, ported, with a parity test): case, hyphens,
  spaces and a trailing refit year are ignored, dispersion spellings are
  canonical, `ccsd(t)` and `ccsdt` stay distinct, and software never matters
  (`enthalpy_atom_corrections_level_mismatch`);
- either level sets `dispersion` or `solvation_method`, which ARC's
  atom-energy matching ignores, so a match proves nothing
  (`enthalpy_atom_corrections_level_unverifiable`);
- a value is not finite (`enthalpy_not_finite`), or H298, a point H or NASA
  H(298.15 K) exceeds ±2.0e4 kJ/mol (`enthalpy_not_formation_magnitude`).

Pre-1.2 output and thermo Arkane loaded from its own YAML record no switch
(`null` or absent). Declaring `formation_298k` on such output is a
deliberate, maintainer-approved interpretation ("option (d)"), not a filled-in
default: Arkane's corrected H298 is a formation enthalpy by construction. The
magnitude check and the checks below are heuristics that make an uncorrected
or wrong-level enthalpy very unlikely to pass; they are not proof that the
corrections were applied at the right level. Since adapter 0.5.0 their enthalpy is stripped, after the non-finite
and magnitude checks and in this order, when:

- the header `arkane_level_of_theory` (the level ARC ran Arkane's atom
  corrections at) is not the energy level, compared as above; a null header
  or one without a method is not checked
  (`enthalpy_atom_corrections_level_mismatch`);
- the energy level or that header level sets `dispersion` or
  `solvation_method` (`enthalpy_atom_corrections_level_unverifiable`);
- the species is light enough that its raw total energy, estimated from its
  composition (the xyz, else the record's `formula`, which ARC 1.0 keeps for
  monoatomics written with `xyz: null`; H 0.50, He 2.90, Li 7.43 hartree per
  atom, and any heavier atom exceeds the bound), is within ±2.0e4 kJ/mol,
  where the magnitude check cannot tell an uncorrected enthalpy from a
  formation one: hydrogen-only species up to H15, He, He2, HeH, the Li atom
  (`enthalpy_formation_unverifiable_light_species`).

These warnings' context adds `atom_corrections_applied: not_recorded` and
`corrections_level_source` (`output_header.arkane_level_of_theory`, or
`not_recorded` when the header gave no level). They are interim: output.yml
1.2's recorded switch and level (ARC PR #1059) supersede them for new runs.
Each strip is reported in the sidecar and outcome `warnings` with action
`thermo_enthalpy_omitted`, or `thermo_omitted` when nothing is left. A block
the shared TCKDB enthalpy rule would refuse is omitted whole.

Known gaps: under ARC `adaptive_levels` species' SP levels vary but output.yml
records one `sp_level`, so the level comparison can pass wrongly and the
adapter cannot detect such runs (ARC `1977e53b` warns at run time); ARC should
export a per-species energy level.
ARC also matches atom energies ignoring `dispersion` and `solvation_method`,
which is why those fields make the level unverifiable.
For 1.2 species whose thermo Arkane loaded from its own YAML (switch `null`),
the header `arkane_level_of_theory` and the energy level describe this run,
not where the YAML came from, so the header check cannot detect a foreign
level.
