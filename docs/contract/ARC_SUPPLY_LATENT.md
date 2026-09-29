# A3 — ARC latent supply (refreshed for tckdb-schemas 0.51)

Repo audited: `/home/calvin/code/ARC` at **main `db0934d5`** (output schema 1.1), plus
the open PR branch `feature_export_atom_corrections_applied` (`a10e8ae0`, `1977e53b`,
"ARC PR #1059", output schema 1.2). The PR branch was read with `git show` only. Anything
main or the PR exports is **not** latent and has no row here.

Companion data: [`ARC_SUPPLY_LATENT.yml`](ARC_SUPPLY_LATENT.yml), **128 rows**. Paths are
keyed against the v2 shared 0.51 walk (`paths_0_51.tsv`, 2991 paths). Every non-`arc_only`
path appears there verbatim, and every row cites an ARC `file:line` checked against main.

| `export_effort` | rows | | `reachable_from_export` | rows |
|---|---:|---|---|---:|
| trivial | 74 | | true | 99 |
| moderate | 50 | | false | 29 |
| hard | 4 | | | |

| `tckdb_value` | trivial | moderate | hard | total |
|---|---:|---:|---:|---:|
| high | 13 | 12 | 0 | 25 |
| medium | 23 | 23 | 2 | 48 |
| low | 38 | 15 | 2 | 55 |

| root | rows | | `value_kind` | rows |
|---|---:|---|---|---:|
| `species_upload` | 39 | | observed | 54 |
| `reaction_upload` | 27 | | derived | 52 |
| `ts_upload` | 4 | | requested | 22 |
| `conformer_upload` | 4 | | | |
| `transport_upload` | 5 | | | |
| `arc_only` | 49 | | | |

### Columns beyond FIELD_KEY's A3 schema

- `obtained_from`: one of `parsed_log`, `computed`, `input_setting`, `scheduler_state`,
  `arkane_output`, `restart_yml` or `settings`.
- `value_kind` separates **observed** values (read from what actually ran) from
  **requested** ones (an input or setting) and **derived** ones (computed by ARC). A
  requested row is never evidence of what ran. For example, the 22 `requested` rows
  include the scan, IRC and conformer levels, which are the levels ARC *asked* for.
  Where the level that actually ran can be recovered (from the input deck beside the
  log), the row says so.
- `reliability` and `tckdb_value` are rated high, medium or low.
- `export_effort` keeps FIELD_KEY's `trivial | moderate | hard` labels, because
  `tools/join_inventories.py` keys its cost column on them. The brief's
  small/medium/large map to these one-to-one.

## Reachability, restated for main `db0934d5`

`arc/main.py:673` calls `write_output_yml` with:

- `scheduler.species_dict`, `scheduler.rxn_list` and `self.output`;
- the opt, freq, sp and neb levels, `composite_method`, `freq_scale_factor`, `bac_type`,
  `compute_thermo`, `arkane_level_of_theory`, `irc_requested` and `t0`.

All `ARCSpecies` and `ARCReaction` attributes are therefore live in `arc/output.py`.

The call does **not** pass:

- `scheduler.job_dict`, which holds the JobAdapters;
- `adaptive_levels`;
- the scan, irc, conformer_opt, conformer_sp, ts_guess and orbitals levels;
- run settings;
- the ArkaneAdapter objects.

Most of the 29 `false` rows are one extra keyword argument away, not a data problem.
There are three exceptions:

1. **`job_dict` is lossy across restarts.** `restart.yml` restores running jobs only
   (`arc/scheduler.py:5241`). After a restart, per-job facts for jobs that were already
   complete survive only in `~/.arc/completed_jobs.csv`. That file is host-global, shared
   across projects, and rotated at 10000 lines.
2. **Values computed inside checks and then thrown away.** These are the IRC endpoint to
   participant mapping (`arc/checks/ts.py:581,670`) and the reaction-zone atoms
   (`arc/checks/ts.py:377`). They need a return-type change or a new attribute.
3. **Arkane's on-disk outputs are not stable at write time.** The E0-only Arkane run
   reuses `calcs/statmech/thermo` with `delete_existing_subdir=True`
   (`arc/statmech/arkane.py:228-230`). A row that proposes re-reading Arkane's `output.py`
   at export time must instead capture the value during `process_arc_project`.

## Top latent items, ranked by TCKDB value against export effort

| # | What | Path (representative) | Effort | Why it matters |
|---|---|---|---|---|
| 1 | Composite-method calculation is invisible | `arc_only.output_dict.paths.composite` (`arc/scheduler.py:3445`) | trivial | For a CBS-QB3 or G4 run, `parse_composite_geo` sets only `paths['composite']`. `_spc_to_dict` reads geo, freq and sp only (`arc/output.py:1836-1838`), and `_iter_ess_logs` has no composite key (`:1200`). The log that produced the geometry and the energy therefore has no path, deck, ESS version or spin diagnostic in `output.yml`. |
| 2 | Atom-correction marker on `statmech.e0_kj_mol` | `arc_only.species.statmech.e0_atom_corrections_applied` (`arc/statmech/arkane.py:414`) | trivial | PR #1059 stamps `use_aec`/`use_bac` on `spc.thermo` only. The E0 of transition states and of E0-only species (`arc/processor.py:221`) has no marker, so it could be a corrected 0 K enthalpy or an absolute energy. This also blocks `thermo.enthalpy_formation_0k_kj_mol`. |
| 3 | Atom-correction switch for kinetics runs | `arc_only.reaction.kinetics.atom_corrections_applied` (`arc/processor.py:154`) | trivial | Neither main nor the PR records it. TS entries still emit `atom_energy` records "as before", so those records do not show that the correction was applied. Good news from the same trace: kinetics runs are given `bac_type=None`, so no BAC mismatch between wells and TS can enter a barrier. |
| 4 | Per-species level under `adaptive_levels` | `species_upload.conformers[].primary_calculation.level_of_theory.method`; `statmech.energy_level_of_theory.*`; `thermo.energy_level_of_theory.*` (`arc/scheduler.py:1123,5273`) | moderate | Every calculation of an adaptive run is attributed to the run-level level. The PR's own docs admit this ("records neither the adaptive levels nor a per-species energy level"). It is the only way to check the energy level against `thermo.atom_corrections_level`. Do **not** use `spc.opt_level` for it (see Defects). |
| 5 | `scan_level`, `irc_level`, conformer levels, `ts_guess_level` | `…additional_calculations[].level_of_theory.method` (`arc/main.py:1078`); `transition_state.calculations[].level_of_theory.method` (`:1092`); `conformer_upload.calculation.level_of_theory.method` (`:1041`) | trivial | `rotor_scans[]`, `irc_logs` and `conformers`/`conformer_energies` are exported with no level of theory at all. Each needs one argument passed at `arc/main.py:673`. These are requested values; the deck beside each log confirms what ran. |
| 6 | TS frequencies in ESS order, and the reaction-mode index | `reaction_upload.transition_state.calculations[].freq_frequencies_cm1` (`arc/scheduler.py:3712`); `…freq_reaction_coordinate_mode_index` (`arc/checks/common.py:84`) | trivial | `spc.freqs` holds the full list. `output.yml` removes the TS negatives from `harmonic_frequencies_cm1` and re-sorts `imaginary_frequencies_cm1`, which loses the reaction-mode index. Recompute it as argmin + 1 and emit it only when the NMD check passed. |
| 7 | Atom map (raw map, plus the participant list around it) | `arc_only.reaction.atom_map` (`arc/reaction/reaction.py:331`); `atom_map.participants[].participant_index/side/species_key` | trivial (raw) / moderate (`atom_to_ts`) | The raw 0-based reactant→product map is one attribute read. Reshaping it to TCKDB's participant→TS form needs +1 on both sides, expansion of duplicate species with `get_species_count`, and a statement that TS atoms are in concatenated-reactant order. That last point is still UNVERIFIED for AutoTST, GCN, KinBot and GoFlow. |
| 8 | Effective ESS keywords | `…primary_calculation.parameters[].raw_key` (`arc/output.py:918,1044`) | moderate (trivial as a verbatim line) | ARC already parses the route line of the exported input deck, including grid, SCF, maxcycle and troubleshooting keywords. It uses the line only for `freq_hessian_method` and then throws it away. This is an observed input, unlike `job.args`, which is only the request and is not reachable at write time. |
| 9 | Statmech treatment Arkane actually used | `species_upload.statmech.statmech_treatment` (`arc/statmech/arkane.py:1426`) | moderate | Arkane silently drops rotors when the freq log has no Hessian, so `output.yml` can list torsions for a species that was treated as RRHO. The truth is in Arkane's `output.py`, which ARC reads only for symmetry and optical isomers. Capture it during the run (see exception 3). |
| 10 | IRC endpoint to participant mapping | `reaction_upload.transition_state.validation_evidence[].reactant_participant_mapping` / `product_…` (`arc/checks/ts.py:581,670`) | moderate, not reachable | `_perceive_irc_fragments` and `_match_fragments_to_species` compute exactly this mapping and then discard it. The `ts_checks.IRC` verdict is exported; the evidence behind it is not. |
| 11 | Atom-map provenance | `reaction_upload.atom_map.source` (`arc/reaction/reaction.py:400`) | moderate | A map the user declared, whether in the input YAML or the restart dict, cannot be told apart from one ARC inferred. Emitting the constant `inferred` would be wrong for declared maps. |
| 12 | Per-conformer energies in hartree, at a known level | `conformer_upload.calculation.opt_result.final_energy_hartree` (`arc/scheduler.py:3133`) | moderate | After the QM conformer jobs, `conformer_energies` holds **absolute** energies in kJ/mol, not relative ones as the schema documents. `conf_sp` jobs overwrite the opt energies without leaving a marker. |
| 13 | T1 diagnostic | `…primary_calculation.wavefunction_diagnostic.t1_diagnostic` (`arc/scheduler.py:4103`) | trivial | It is parsed and stored in `spc.t1`, then dropped. The parse is gated on the *run-level* `sp_level` containing `ccsd`, not on the adaptive per-species level. |
| 14 | Kinetics provenance constants and markers | `reaction_upload.kinetics[].note`, `pressure_context`, `model_kind`, `degeneracy_convention` | trivial | `kinetics['comment']` and `kinetics['ts_validation']` are in memory. The latter marks a rate computed from a TS that failed the IRC check. `high_p_limit`, `modified_arrhenius` and `already_applied` hold for every Arkane TST rate. Do **not** export `atom_map_clusters[].degeneracy` as `kinetics[].degeneracy`; Arkane applies path degeneracy through symmetry numbers. |
| 15 | Software release for scan and IRC calculations | `species_upload.conformers[].additional_calculations[].software_release.version` | trivial | `ess_versions`/`ess_software` are keyed only by opt, freq, sp and neb, although the scan and IRC logs are exported. |
| 16 | Isotopes | `species_upload.conformers[].geometry.isotopes`, `transition_state.geometry.isotopes` | trivial | Every ARC xyz dict carries isotopes, and `xyz_to_str` drops them. |

Next tier (medium value, moderate effort):

- rotational constants from Arkane's inertia block;
- `uses_projected_frequencies` and the frequencies Arkane actually used;
- `statmech`/`kinetics` `source_calculations[]` from the Arkane species files. When no
  freq job ran, the "freq" source is silently the sp log (`arc/statmech/arkane.py:484`);
- ND directed scans, i.e. `scan_result` with dimension > 1, from
  `rotors_dict[i]['directed_scan']`;
- dipole moment and polarizability from existing parsers;
- all TS-guess alternatives (`arc_only.species.ts_guesses`, `chosen_ts_list`,
  `unsuccessful_methods`);
- the TST k(T) table with tunneling factors (`arc_only.reaction.kinetics.tst_rate_table`).

## Items the brief asked about that are not latent

- **Exported on main, so no row:**
  - `ts_checks`, which A2 maps to `validation_evidence[].passed` and `kind`;
  - `wavefunction_stability` and `scf_reference`, which map to `scf_stability.*`;
  - the S² of the sp job;
  - 1D rotor scans;
  - rejected torsions, including their invalidation reasons;
  - GSM and IRC path evidence and Hessians, in `parser_evidence.json`;
  - `ess_versions`/`ess_software` for opt, freq, sp and neb;
  - `freq_scale_factor_key`/`freq_scale_factor_source`;
  - `kinetics.tunneling` and `kinetics.T0_k`. `T0_k` has no TCKDB home, so it is A2 surplus.
- **Exported by ARC PR #1059:** `thermo.atom_corrections_applied`,
  `thermo.bond_corrections_applied` and `thermo.atom_corrections_level`, which inform
  `thermo.enthalpy_reference_kind`. Only the thermo block gets them. Items 2 and 3 above
  are the gaps the PR leaves.
- **NEB path:** ARC reads only the final TS geometry from the ORCA NEB log. It has no NEB
  image or energy parser, so `path_search_result.points[]` for NEB is ARC-absent, not
  latent. The only NEB/GSM rows here are *requested* image and node counts, labelled as
  such.
- **Execution environment:** nothing ARC has is an observed runtime environment.
  - The submit script next to each log (`arc/job/adapter.py:418`) is the user's template.
  - Cores, memory, queue and walltime are scheduler **requests**.
  - Only `job_id` and file mtimes are observations.
  - ARC has no parser for nprocs or elapsed time in ESS logs.
  - A conda lockfile or closure digest would be new functionality (`hard`).

  These rows sit under `execution_environment.runtime{described}` / `executable.locator`
  (medium/low value) or under `arc_only.job.*`.
- **Transport** is dead code on main.
  - No `onedmin` job adapter is registered.
  - The processor's transport step is `# todo` (`arc/processor.py:237`).
  - `transport_data` is not persisted in `as_dict`.

  `sigma_angstrom` and `epsilon_over_k_k` are therefore `hard` (a new adapter).
  `dipole_debye` and `polarizability_angstrom3` are `moderate` via existing log parsers.
  `rotational_relaxation` is a hard-coded 2 and must never be exported as a result.
- **ARC-absent, so no rows:**
  - D1 and largest-T2 amplitude;
  - reduced masses, force constants and IR/Raman intensities;
  - electronic excited levels and term symbols. ARC knows only the ground-state
    multiplicity, which is already exported;
  - `unmapped_smiles` for TSs. ARC writes null SMILES for TSs;
  - `calculation.quality`, which has no ARC concept (the row is kept low and flagged);
  - atom-map `geometry_key`/`ts_geometry_key`, which the adapter mints; ARC has nothing
    to export;
  - `ARCReaction.dh_rxn298`, which is never assigned.

## Defects in *already-exported* data found along the way (for A2 / ARC)

1. `conformer_energies`: after the QM conformer stage the values are absolute kJ/mol
   (`arc/scheduler.py:3133`), not "relative to the lowest" as
   `docs/output_yml_schema.md:361` says. `TSGuess.energy` is absolute too.
2. `statmech.torsions[].treatment` is always `hindered_rotor`.
   `rotors_dict['type']` is never set on main (`determine_rotor_type` is never called),
   and the Arkane template always writes `HinderedRotor(fit='best')`.
3. IRC endpoint species (`IRC_<ts>_N`) are exported as ordinary `species[]` entries with
   no `irc_label`, so a consumer could deposit them as wells.
4. `spc.opt_level` is stamped from the run-level `opt_level` (`arc/scheduler.py:3607`),
   not from `job.level`. It is wrong under `adaptive_levels`, and it is listed
   (`arc_only.species.opt_level`, reliability low) only so nobody exports it as the
   observed level.
5. Directed-scan energies are zeroed in place (`arc/scheduler.py:2974`), so the absolute
   energies of ND scan points are gone from memory.

## Rows retired since the 0.22 inventory

Of the 75 old rows:

- **Now exported:**
  - `validation_evidence[].passed`/`kind` and `arc_only.species.ts_checks`, via `ts_checks`;
  - `arc_only.species.rotors_dict.invalidation_reason`, via `rejected_torsions`;
  - `arc_only.species.number_of_radicals`, via `scf_reference.declared_number_of_radicals`;
  - `primary_calculation.software_release.version`, via `ess_versions`/`ess_software`;
  - `statmech.freq_scale_factor.software`, via `freq_scale_factor_key`;
  - `arc_only.species.rotors_dict.torsion`, via torsion `atom_indices`.
- **Dropped as absent or adapter-owned:**
  - `atom_map.participants[].geometry_key` and `atom_map.ts_geometry_key`;
  - `arc_only.reaction.dh_rxn298` (never set).
- **Re-keyed to 0.51 spellings:**
  - TS `freq_result.*` became the flattened `calculations[].freq_*` fields on the
    reaction route;
  - `…primary_calculation.level_of_theory`/`parameters[]`/`execution_environment` now
    descend to leaves.
- **Carried forward, re-verified, in section 18:** the remaining `arc_only.*` species and
  reaction attributes.

## Path-spelling problems in the shared 0.51 walk

1. **Fixed in v2:** the `transport_upload` root was walked from `LiteratureUploadRequest`.
   The walker matched `*UploadRequest`, but the transport model is
   `TransportUploadPayload`. Transport rows are now keyed `transport_upload.*`; the same
   payload also embeds at `conformer_upload.transport.*`.
2. **Fixed in v2:** `…applied_energy_corrections[].scheme.atom_params`, `bond_params` and
   `component_params` were leaves typed `list[SchemeAtomParamPayload]` and so on. They
   are forward-reference strings, which the walker did not descend, breaking FIELD_KEY
   rule 3. No A3 row uses them.
3. **Still open (a join hazard, not a walker bug):**
   - `ComputedReactionCalculationIn`, reached as `reaction_upload.species[].calculations[]`
     and `reaction_upload.transition_state.calculation(s)`, **flattens** its frequency and
     opt results into `freq_frequencies_cm1`, `freq_reaction_coordinate_mode_index`,
     `freq_imaginary_dispositions`, `opt_converged` and so on.
   - These never share a tail with the nested `freq_result.*` / `opt_result.*` routes, so
     `resolve_route` cannot relate them. A3 keys the TS mode rows on both spellings: the
     reaction route and `ts_upload.additional_calculations[].freq_result.*`.
4. **Still open:** `conformer_upload.calculation` has no `artifacts[]` or `depends_on[]`.
   That is the schema's real shape, but routes differ, so no row may assume it.
5. **Resolved during this audit:** `FIELD_KEY.md` now names five roots at 0.51.0. It
   defines `transport_upload` as the backend `TransportUploadRequest`, which extends the
   wire `TransportUploadPayload`. The v2 walk enumerates it from `TransportUploadPayload`,
   so any extra field the backend request adds is not in `paths_0_51.tsv`. A3's five
   transport rows use only payload fields.
6. **Join caveat:** in this worktree `TCKDB_DEMAND.yml` is still the 0.22 A1. A scratch
   join run reports 66 A3 rows as `SURPLUS`, 17 of them non-`arc_only` 0.51-only paths
   such as `transport_upload.*`, `species_upload.thermo.phase` and
   `ts_upload.validation_evidence[]…`. These resolve once A1 is regenerated at 0.51.
   **A3 produced no near-misses.**

## Coverage self-assessment

**Method.** Three parallel passes covered:

- execution and settings;
- statmech, Arkane, kinetics and transport;
- TS, reaction, conformers and identity.

A merge step then removed two duplicate rows, carried the old `arc_only` rows forward
after re-verifying them, and dropped one non-latent row (D1). Every cited line was
checked mechanically for existence, and heuristically against the row's `arc_location`.

**Confident:**

- the reachability column;
- the exported/latent boundary, which was read from `docs/output_yml_schema.md` and the
  PR diff;
- the energy-correction and levels-of-theory findings (items 1 to 5), whose citations I
  re-read directly.

**Less certain:**

- The Arkane-side claims (statmech treatment, projected frequencies, inertia blocks,
  `model_kind`) were checked against a local RMG-Py checkout (`62eb728c0`), not the
  RMG-Py installed in `rmg_env`. `model_kind` carries an UNVERIFIED note to that effect.
- Seven rows carry `UNVERIFIED`, among them:
  - the atom-order preservation of the non-linear TS adapters;
  - whether `equivalent_map_count` equals the cluster count;
  - whether `conf_sp` reliably overwrote the energies;
  - Orca/QChem polarizability parsing;
  - whether ARC ever auto-sets fragments;
  - whether TCKDB wants producers to set `quality`.
- `arc/job/trsh.py` and the `linear.py` TS-strategy internals were again only sampled.
  The troubleshooting *sequence* per job, and TS-guess strategy diagnostics, may hold
  more recordable provenance than the `ess_trsh_methods`/`trsh_methods` rows capture.

**`trivial` means** one to three lines in `arc/output.py`, plus the matching
`arc/schemas/output_yml_schema.json`, `docs/output_yml_schema.md` and golden-fixture
updates, plus an `output.yml` schema version bump. It does not mean "no work".
