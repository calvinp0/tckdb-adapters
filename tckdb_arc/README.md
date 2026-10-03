# tckdb-arc

Convert ARC `output/output.yml` and portable parser evidence into TCKDB
species, reaction, and transition-state uploads. Payloads and upload metadata
are written locally before any network request, allowing inspection and replay.

Requires Python 3.11+, `tckdb-client` 0.111.x and `tckdb-schemas` 0.73.x.
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
from RMG-database, whose commit output 1.3 records (since adapter 0.8.0, batch H,
it travels as `scheme.data_revision`, which stops the Arkane build from splitting
schemes). Before output 1.3 a database-only
revision of a table value for the same key still conflicts with the stored
scheme (a parameter-conflict 422), and each new RMG-Py commit makes a new
scheme row even when the tables are unchanged. The first 0.6.3 upload of an
already-deposited scheme also creates a new row (identity now includes the
workflow tool; the older null-tool row remains). An `atom_energy` scheme now carries
`scheme.atom_params` from ARC's `reference_atom_energies` (Arkane's per-element
atomic energies) with `scheme.units` set to that table's own unit; a table with
a missing or unrecognized unit, or unusable values, is not sent.

Adapter 0.8.0 (ARC output 1.3, batch E) reads output.yml 1.3 (ARC PR #1059): each record's `levels` (the level of its
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

Adapter 0.8.0 (batch G) reads ARC output schema 1.3's reaction atom map and IRC participant mapping.
`irc_participant_mapping` becomes the IRC validation evidence's `reactant_participant_mapping`
/ `product_participant_mapping` (1-based TS atoms, participants matched by label and
occurrence), and only when ARC states that the IRC endpoints follow the TS atom order and the
sides are distinguishable; otherwise neither side is sent
(`ts_irc_participant_mapping_not_sent`). ARC's reactant-to-product `atom_map` is **not** sent:
TCKDB's `atom_map` is participant atom to TS atom and the bc731fb4 draft of ARC 1.3 states no relation to the TS atom
order, so the adapter reports `reaction_atom_map_ts_order_not_stated` for a document without `ts_atom_map` (TCKDB
then warns `reaction_atom_map_absent`); adapter 0.9.0 sends it from ARC's `ts_atom_map` (below).
See `docs/contract/CURRENT_ARC_INTEGRATION.md`.

Adapter 0.6.5 targets tckdb-schemas 0.54 (no wire model change; only the
bundles dry-run route, which the adapter never calls, gained rules) and states
Arkane's full atom-energy convention in the `atom_energy` scheme note: the atomic
energies are subtracted and each atom's gas-phase formation enthalpy less its
thermal correction is added.

Adapter 0.6.7:

- A species on both sides of a reaction (or repeated on one side) is deposited once; the
  computed-reaction bundle repeats its key in `reactant_keys` / `product_keys`, and the
  kinetics `reactant_energy` / `product_energy` links point at its sp once per role.
- A single-atom species was uploaded with a placeholder primary opt and a
  `monatomic_species_primary_opt_placeholder` warning (TCKDB#600). Superseded in batch H: an atom
  now sends its real `sp` as its primary calculation (TCKDB#610, tckdb-schemas 0.59), with no
  placeholder and no warning.
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

Adapter 0.8.0 (batch F) reads ARC output schema 1.3 (the keys are read from 1.3 documents
only, and a document before 1.3 gives the payload it gave before):

- A partial Petersson BAC is sent with the bonds Arkane applied (`components`, which must
  sum to the total) and its skipped bonds in the correction's note, instead of being omitted.
- `statmech_treatment` is `statmech.arkane_treatment`, the treatment Arkane applied, with no
  Hessian gate; a torsion's `treatment` is sent as recorded and a null one sends the torsion
  without a treatment. The rigid-rotor kind (now including symmetric and spherical tops) is
  sent as stated.
- The kinetics `note` carries Arkane's comment, the `ts_validation` text and the kinetics run's
  atom-correction switch; the reaction's `reversible` is sent as stated.
- A correction scheme names the RMG-database table Arkane loaded. Batch F did that with a
  `RMG-database` `workflow_tool_release` (commit, package version or `sha256:`); that stand-in is
  replaced in batch H by `scheme.data_revision` (tckdb-schemas 0.62), see below.
- A TS's modes are its frequencies in ESS order and its reaction-coordinate mode is the one ARC's
  normal mode displacement check validated; with none stated, TCKDB's 50 cm-1 noise floor picks the one
  mode above it (the others `unassigned`), and two or more above it refuse the record.
- `sp_t1_diagnostic` becomes the sp calculation's `wavefunction_diagnostic`. The dipole moment
  and polarizability have no home on these routes (TCKDB holds them only on the standalone
  transport record), and the E0 switches gate nothing since no E0 is deposited.

Upgrading changes the payload hash, and so the idempotency key, of 1.3 uploads that gain any of
the above. See `docs/contract/CURRENT_ARC_INTEGRATION.md`.

Adapter 0.8.0 (batch H) uses what tckdb-schemas 0.59 to 0.64 added:

- **Atoms (0.59).** A conformer whose own XYZ has exactly one atom sends `type: "sp"` as its
  primary calculation (computed-species, computed-reaction and conformer routes): the atom's own
  log, level, program and energy (`sp_energy_hartree`, else the `opt_final_energy_hartree` ARC
  parsed from the same one log), no `opt`, no `converged` (an sp result has none), no placeholder, no
  warning, no `freq`, no coarse opt, no rotor scans and no alternative conformers. Thermo and statmech
  link that one sp once (`thermo_role_duplicate` / `statmech_role_duplicate` need two on one
  geometry). An atom with neither energy is refused (`ValueError`), never relabelled as an opt. The
  placeholder opt stays only for a molecule with no opt job (`primary_opt_placeholder_no_opt_job`) or
  a composite run (`composite_geometry_level_not_stated`).
- **Scheme provenance (0.62).** `scheme.data_revision` is the RMG-database revision Arkane read:
  the git commit for `path_kind: git`, else the package version, else the `data.py` SHA-256 as a
  plain hex string. It joins the scheme's identity, so the Arkane build (still stamped as
  `scheme.workflow_tool_release`, provenance only) stops splitting schemes. The `RMG-database`
  tool-release identity set of the first batch-F draft was never deposited (it existed only on an
  unmerged branch), so no deposited scheme has it; a scheme deposited with a revision never
  matches one without, so the first upload of each table under 0.8.0 makes a new row. An
  `atom_energy` scheme with `atom_params` also sends `atom_params_applied_as` as ARC states it
  (`subtracted`); the scheme note keeps only Arkane's per-atom addend (`atom_hf - atom_thermal`),
  which that field does not cover.
- **Kinetics (0.63).** ARC's `A` is sent as it is with `t0_k` = ARC's `T0_k` (omitted when 1 K,
  the contract default), instead of `A / T0**n`; a `T0_k` that is null, invalid or outside
  `0 < t0_k <= 10000` omits `a`/`a_units`. `tunneling_application`, `interpretation_assignments`
  and `network_kinetics_ref` are not sent: every reference in them is a record deposited earlier,
  and the bundle cannot cite the transition state or statmech it is itself creating. The tunnelling
  label is still ARC's `tunneling` as `tunneling_model`.
- **Rejected rotors (0.61).** ARC's rejected rotors go out as torsions with `invalidated_reason` on the bundle
  routes too (they were conformer-route only).
- **Standalone transition-state route (0.64).** Rotor scans (with `scan_result`) and
  `applied_energy_corrections` (without source keys) are now sent; its `atom_map` slot was left unset
  until ARC stated `ts_atom_map` (adapter 0.9.0 sends it, below).
- Not built, no source in ARC or a decision needed: statmech `electronic_levels` (ARC 1.3 exports no
  electronic level, term or spin-orbit data), bundle `transport` (ARC's dipole and polarizability keys
  could feed it) and TS `statmech` on the reaction bundle. An IRC result whose
  direction ARC does not state is still withheld although 0.64 made `direction` and the flags optional.

Adapter 0.8.1 (batch I) sends the two other TS validation evidence kinds tckdb-schemas 0.64 added, only from
what ARC states (details in `docs/contract/CURRENT_ARC_INTEGRATION.md`, A14):

- **`imaginary_mode`** (both TS routes) from `ts_checks['freq']`: `imaginary_frequency_count` and
  `imaginary_frequency_cm1` (the designated reaction-coordinate mode, written negative) are read from the TS
  frequency result sent in the same upload, so TCKDB's cross-check against it passes; `mode_displacement_agrees`
  is `True` only when ARC 1.3 states `reaction_coordinate_mode_index`, `False` only for a failed
  `ts_checks['NMD']` with no index, otherwise omitted. A passing record with several imaginary modes and no
  designated coordinate is not sent (`ts_imaginary_mode_evidence_not_sent`).
- **`energy_ordering`** (reaction bundle only; the standalone route refuses it) from `ts_checks['e_elect']`,
  with every participant's own `sp_energy_hartree` cited to its `sp` calculation (`reactant:N` / `product:N` in
  the declared order, a repeated species repeated). Electronic energies only: `e0` is never sent. A `True`
  verdict the stated numbers contradict (ARC's 1 kJ/mol margin), or a participant with no usable energy or `sp`,
  leaves the record out (`ts_energy_ordering_evidence_not_sent`). ARC leaves `e_elect` unset when its `E0`
  check passed, and then nothing is sent.

Adapter 0.10.0 (batch K, tckdb-schemas 0.73 / client 0.111; details in
`docs/contract/CURRENT_ARC_INTEGRATION.md`, "tckdb-schemas 0.65 to 0.73"):

- **Public refs.** Artifacts are posted to `/calculations/{calc_ref}/artifacts`, and the sweep names a
  calculation by the response's `primary_calculation.calculation_ref`. The integer `calculation_id` is a fallback only for a
  response with no ref, with a `calculation_ref_not_returned` warning. `UploadOutcome.submission_ref` and
  `.calculation_key_refs` carry the response's `sub_` ref and (computed-reaction) key-to-`calc_` map. The adapter makes no
  rights-attestation call.
- **Artifact sidecars and idempotency.** The artifact idempotency key is built from
  `(calculation_ref, artifact_kind, artifact_sha256)` (it was the integer id before), and the sidecar records
  `calculation_ref` beside `calculation_id` and is named after the ref. **A project uploaded before 0.10 holds integer-id
  keys, so a re-run would re-post each artifact under a new key, and TCKDB keeps one `calculation_artifact` row per upload
  even for identical bytes (it would duplicate the row).** The adapter therefore looks for the pre-0.10 sidecar
  (`<species>.calc<int>.<kind>.artifact.meta.json`, the response still carries the integer id) before building a
  ref-keyed upload; if its status is `uploaded`, its sha256 is the artifact's and it was posted to the same server, the
  artifact is skipped as already uploaded. Otherwise (no sidecar, pending or failed, changed bytes) it uploads under the
  ref key. Nothing needs deleting.
- **BAC frequency level.** Only a `bac_petersson` / `bac_melius` scheme whose record's `matched_arkane_key` is a
  `CompositeLevelOfTheory(freq=..., energy=...)` carries `frequency_level_of_theory`, taken from the key's freq half
  (tckdb-schemas 0.66: a scheme keyed on one level sends none; in RMG-database only 4 of 47 Petersson keys are composite).
  A single-level key, no key or an unparseable one sends none. If ARC's stated freq-job level disagrees with the key's
  freq half the field is omitted with a `bac_frequency_level_conflict` warning. It joins scheme identity, so a composite-keyed
  BAC is one new scheme row. Atom-energy schemes send none, and the adapter never sends `application_role: composite_delta`.
- **Method guards.** No `//` ever reaches a `level_of_theory.method` (a compound level is refused with a log line
  `level_method_is_compound`). An Arkane level that is a correction-table name (`cbs-qb3-paraskevas`, `cbsqb32023`) is sent
  as its method stem with the table name as the scheme `name` (warning `correction_table_method_split`). A calculation whose
  level method is a table name (ARC runs `cbs-qb3-paraskevas` as CBS-QB3) is sent with the stem too (logged with the original
  string). A calculation level with `//` is not built; its skip reason starts with `level_method_is_compound`.
- **G4 / G4MP2 on Gaussian 16 Revision A.03.** ARC's energy for such a composite run is the shifted 298 K value, so the
  record's sp energy, the E0-derived thermo content (H298, NASA, point H and G), statmech energy links and the kinetics
  built on it are withheld, with `g4_energy_loader_shifted_label_gaussian16_a03`, until ARC exports a correct E0. S298 and
  Cp (from statmech) are kept. Only output 1.3 records carry `ess_versions.composite`, so the guard cannot fire on an
  earlier document.
- **Identity replica.** `tckdb_arc.level_rules` replicates TCKDB's level-of-theory hash (aliases, folded dispersion,
  `core_treatment` only when stated). Run `test_hash_matches_the_backend` with `TCKDB_BACKEND_PATH` set to a TCKDB
  `backend/` (CI does).
- Not in this release: the `composite` calculation type (waits on ARC B18), transport, core extraction.

Adapter 0.11.0 (core extraction, batch L1; no behaviour change):

- The producer-agnostic half moved to the new `tckdb-adapters-core` package (`tckdb_core/`, import name
  `tckdb_core`; `tckdb-arc` now depends on it, `>=0.1,<0.2`): the payload writer and sidecar types, idempotency-key
  composition (the key namespace is a parameter; ARC binds `"arc"`), constraints, the TCKDB level-identity replica, the
  config fields and API-key resolution, and the upload / sidecar / readiness / artifact-batch pipeline
  (`TCKDBUploaderBase`, which `TCKDBAdapter` inherits), with its outcome types and endpoint constants.
- Every payload, sidecar file, warning, idempotency key and log line is byte-identical to 0.10.0 (checked by building
  every route over eight fixtures, in all three upload modes, against both packages and diffing the JSON). Each name
  that moved is still importable from where it was (`tckdb_arc.adapter`, `.config`, `.payload_writer`, `.idempotency`,
  `.constraints`, `.level_rules`), so callers and tests are unchanged.
- ARC-specific code stays here: reading `output.yml`, the payload builders, `TCKDBConfig.from_dict` (a thin subclass of
  the core config) and the CLI/sweep. Install `tckdb_core` before `tckdb_arc` when working from the checkout
  (`pip install -e tckdb_core -e tckdb_arc`).

Tracked TCKDB releases (added by `tools/tckdb_drift.py --bump`):

<!-- tckdb-drift:changelog -->
- Adapter 0.10.0: tracked TCKDB 96b71b0 (schemas 0.73.0, client 0.111.0).
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

Adapter 0.9.0 (batch J) consumes ARC output schema 1.3 at PR #1059 head `ebc88ec8` (details in
`docs/contract/CURRENT_ARC_INTEGRATION.md`, "Output schema 1.3 at ebc88ec8"). Documents from the earlier 1.3 draft
take the previous paths unchanged.

- **Reaction species labels.** `reactant_species_labels` / `product_species_labels` (one entry per occurrence) are the
  order and repeats of `reactant_keys` / `product_keys` on both routes; a contradiction with `atom_map_*_labels` or
  `reactant_labels` refuses the reaction (`reaction_species_labels_contradicted`).
- **Reaction `atom_map` on both routes.** `ts_atom_map` becomes TCKDB's `atom_map` (`atom_to_ts` per participant,
  `source: inferred`, a `note` naming ARC's method and its symmetry-equivalent-atom convention) after checking block
  lengths, TS index coverage, element conservation, `atom_map` consistency and consistency with the IRC participant
  mapping that is sent; otherwise no map and `reaction_ts_atom_map_not_sent` (with `ts_atom_map_unavailable_reason`
  in the context when ARC states `null`).
- **`nmd_forced`.** A forced NMD pass gives `mode_displacement_agrees: False` (the check ran and failed), never `True`; an index stated beside it is
  reported (`ts_nmd_forced_contradicts_reaction_coordinate_index`) and not used as the NMD designation.
- **Conformer programs.** `conformer_ess_software` / `conformer_ess_version` give each screened conformer's program
  and banner; conformers with a null program are still not filed. `conformer_energies` null entries send no energy.
- **`bac_type`.** `bond_corrections_applied: true` with a null header `bac_type` warns (`bac_type_not_stated`); the
  BAC scheme is still built from the record's `energy_corrections`. `reference_pressure_bar` is still omitted, never
  defaulted, when `standard_state_pressure_pa` is null (ARC 1.3 final always states 101325).
