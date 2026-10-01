# A4 — what the ARC→TCKDB adapter emits (refresh for tckdb-schemas 0.51.0)

This is the narrative that goes with `ADAPTER_MAPPING.yml`. The YAML has 838 rows, one per distinct canonical path.

| | |
|---|---|
| Adapter | `tckdb-arc` 0.4.0, `tckdb_arc/tckdb_arc/*.py` at `0913124` (the audit worktree `docs_demand_audit_schemas_0_51` has the same code as `main`) |
| Schema | `tckdb-schemas` 0.51.0 (editable from `TCKDB_v2/schemas/python/tckdb-schemas`) |
| Path spellings | `scratchpad/shared/paths_0_51.tsv` **v2** (2,991 paths across 5 roots; regenerated mid-audit after the walker fixes in §3), copied verbatim. Every YAML `path` appears in it, and a check confirms this. |
| ARC, for gap checks | `/home/calvin/code/ARC` `main` `db0934d5`: `arc/output.py` and `arc/schemas/output_yml_schema.json`, read-only |

The inventory records only what the adapter does. Every row has `status_at_0_51: unknown`, and A5's drift ledger decides that value. The FIELD_KEY template still names the column `status_at_0_22_0`. I renamed it because this audit targets 0.51. `tools/join_inventories.py` does not read the column.

## 1. Method

**The empirical pass came first.** I collected payloads in two ways:

1. **Test-suite capture.** A pytest plugin (`-p capture_plugin`, kept in scratch) wrapped the four top-level builders: `_build_computed_species_payload`, `_build_computed_reaction_payload`, `_compose_transition_state_request` and `_build_payload`. It dumped every dict they returned while the full adapter suite ran (928 passed, 4 skipped). That gave 497 payloads: 198 species, 225 reaction, 28 TS and 46 conformer.
2. **Real-fixture builds.** Each fixture went through every upload mode by calling the public `submit_*` entry points with `upload=False`:
   - `golden/phase3_output.yml`, with its `tckdb_evidence.json` sidecar and again without it
   - `arc_1_2/output.yml` (thermo 1.2 corpus)
   - `current_arc/output.yml` with its `parser_evidence.json`

   That gave 24 payloads: 10 species, 10 conformer, 2 reaction and 2 TS.

   `current_arc/output.yml` cannot build any payload. It is an evidence-only fixture with no `smiles` and no `xyz`, so every mode raises `ValueError`. The golden TS in conformer mode raises for the same reason (its `smiles` is null).

I flattened each payload into FIELD_KEY paths and validated it against the real 0.51 request models (`ComputedSpeciesUploadRequest`, `ComputedReactionUploadRequest`, `TransitionStateUploadRequest`, `ConformerUploadRequest`):

- **All 24 fixture payloads validate.**
- **519 of 521 captured payloads validate.** The two failures come from deliberate negative tests (`test_no_scan_calcs_when_additional_calculations_empty` and `test_missing_scan_provenance_does_not_hide_unknown_torsion_reference`). In both, the adapter intentionally leaves a torsion's unknown `source_scan_calculation_key` for TCKDB to reject rather than repairing it silently.
- **Two things never appear in any payload:** a key outside the 0.51 schema, and an explicit `null` on a scalar. The one explicit `null` in the whole corpus is `scheme.source_literature: null`, passed through verbatim from legacy synthetic AEC records.

**The code pass came second.** I read the emission sites in `adapter.py` line by line (lines 76–305, 756–3505, 4207–7857), plus `constraints.py`, `evidence.py` and the `sweep.py` dispatcher. From that reading I built a catalog of calculation-relative fragments and per-route slot definitions. I then generated the YAML from the catalog and cross-checked it against the payloads:

- Every empirically populated path is covered by the catalog, and the generator refuses to run otherwise.
- Every path the catalog generates exists in the 0.51 path list.
- 209 rows are `code_only`. They are reachable in code, but no built payload populates them.

### Row columns (A4 schema plus extensions)

| Column | Meaning |
|---|---|
| `path`, `source`, `adapter_source` | FIELD_KEY columns. `source` is the emission line, where the key lands in the dict. |
| `maps_from` | The ARC origin, prefixed so `tools/phase_c_stats.py` can bucket it: `output.yml: …`, `tckdb_evidence.json \| parser_evidence.json: …`, `on-disk: …` or `(none — literal/derived/adapter-minted)`. |
| `transform_kind` | One of `verbatim`, `coerced` (a type cast only), `renamed`, `unit_conversion`, `derived`, `constant`, `default` (a value with a fallback) or `conditional` (a passthrough gated by validity). |
| `transform` | Free text. It ends with a `[route: …]` note on calculation rows. |
| `condition` / `on_absence` | When the field is emitted, and what happens otherwise: omission, a refusal of the whole record, or an abort. |
| `warning_codes` | Sidecar warning codes. The thermo enthalpy guard is the only producer-side emitter. |
| `emission_route` | The line where the enclosing block is attached to the payload. |
| `empirical` / `empirical_counts` | `fixture`: populated in a payload built from a checked-in ARC fixture. `synthetic`: populated only by test-suite corpora. `code_only`: populated by no built payload. |
| `modes` | Upload modes that emit the row: `computed_species`, `computed_reaction`, `computed_ts` or `conformer`. |

## 2. Counts per root and per mode

| Root (mode) | Paths in 0.51 | Adapter rows | fixture | synthetic-only | code-only |
|---|---:|---:|---:|---:|---:|
| `species_upload` (computed_species) | 777 | 221 | 78 | 99 | 44 |
| `reaction_upload` (computed_reaction) | 1,241 | 425 | 160 | 159 | 106 |
| `ts_upload` (computed_ts) | 375 | 130 | 86 | 7 | 37 |
| `conformer_upload` (conformer) | 568 | 62 | 33 | 7 | 22 |
| `transport_upload` (no mode) | 30 | **0** | 0 | 0 | 0 |
| **Total** | 2,991 | **838** | 357 | 272 | 209 |

`transform_kind` totals:

| derived | verbatim | renamed | constant | coerced | default | conditional | unit_conversion |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 246 | 180 | 112 | 119 | 91 | 55 | 31 | 4 |

The four `unit_conversion` rows all compute a `relative_energy_kj_mol`:

- IRC points on each of the two TS routes, converted Hartree→kJ/mol with `_vendor.E_h_kJmol`.
- Path-search points on each of the two TS routes. These use Hartree→kJ/mol, or kcal→kJ with the factor 4.184 on the GSM stringfile branch.

No other field is unit-converted. Four behaviours need care:

- **Kinetics A.** At this snapshot (adapter 0.4.0) it was normalized for `T0` (`a = A / T0**n`, `adapter.py:6795`), a derived change of reference and not a unit conversion. Since tckdb-schemas 0.63 (adapter 0.8.0) `A` is sent as it is with `t0_k = T0_k`; see `CURRENT_ARC_INTEGRATION.md`.
- **Kinetics units.** A and Ea units are mapped as enums.
- **Unknown kinetics units** drop the value together with its unit (C-4).
- **Renamed IRC and GSM gradients.** They drop the `_hartree_per_bohr` suffix without converting.

### Code-only groups (209 rows)

These are reachable, but no fixture or test populates them:

- **Level-of-theory fields** `aux_basis`, `cabs_basis`, `dispersion`, `solvent`, `solvent_model` and `keywords`. They are code-only on every calculation route, on `freq_scale_factor.level_of_theory` and on `applied_energy_corrections[].scheme.level_of_theory`. No corpus level carries them.
- **`level_of_theory.spin_treatment`** on every freq and sp route. It comes from `scf_reference.{freq,sp}_reference` (`adapter.py:3419-3423`), and no corpus record has `scf_reference`.
- **`parameters[]`** (`freq_hessian_method`) on species, reaction-species and conformer routes. It is populated only on the TS routes (one test).
- **`spin_diagnostic.*`** on species, reaction and TS routes. `note` is code-only everywhere, and ARC's schema has no `note` key.
- **`constraints[]`** on reaction, TS-standalone and species-primary routes. So is `atom4_index`. Only species freq and sp constraints are exercised.
- **`artifacts[]`** on all reaction routes. It is exercised only on the species route.
- **`freq_result.imag_freq_cm1`** on species and conformer freq. It is copied whenever the record has it, even with `n_imag` 0 or null.
- **TS-only designation fields** `reaction_coordinate_mode_index` and `modes[].imaginary_disposition` on `ts_upload`. On the reaction TS route they are populated synthetically.
- **Reaction-route scan fragments:** `symmetry_number`, `zero_energy_reference_hartree`, point geometry and energy, and `start_value`/`end_value`.
- **`path_search_result.points[].is_climbing_image`** on `ts_upload`.
- **AEC fields:** `scheme.atom_params[].element`/`.value` and `scheme.bond_params[].bond_key`/`.value` on the species root, `scheme.bond_params[].*` on the reaction TS, and `note` on all three routes. No corpus record carries a neutral `parameter_table`.
- **`unmapped_smiles`** on the conformer and TS-reaction participants. ARC exports no `unmapped_smiles`.

## 3. Problems with path spelling in `paths_0_51.tsv`

The first version of the shared list (v1, kept at `paths_0_51.v1.tsv`) had two walker bugs. This audit found both independently, and the coordinator regenerated the list (v2) while the audit was running. The YAML is keyed to **v2**.

1. **`transport_upload` was bound to the wrong model (fixed in v2).** `walk_paths.py` picked "the only class ending in `UploadRequest`" in `tckdb_schemas.workflows.transport_upload`. That class was the *imported* `LiteratureUploadRequest`, so the 12 v1 `transport_upload.*` rows were literature fields. v2 roots the list at `TransportUploadPayload`, which gives 30 leaves. The updated `FIELD_KEY.md` defines the root as the backend-side `TransportUploadRequest` (`TCKDB_v2/backend/app/schemas/workflows/transport_upload.py:66`, `POST /api/v1/uploads/transport`). That class extends the payload with `species_entry`, inline `calculations` and `source_calculations` (lines 79-88), and the v2 list does not walk those three fields. **This is a residual spelling gap**: A1 should check whether `transport_upload.species_entry.*`, `.calculations[].*` and `.source_calculations[].*` belong in the list. The same payload also travels inline at `conformer_upload.transport.*`.
2. **Fifteen list-of-model fields were not descended in v1 (fixed in v2).** They were `…applied_energy_corrections[].scheme.{atom_params,bond_params,component_params}` on each of these routes:

   | Route |
   |---|
   | `species_upload` |
   | `species_upload.thermo` |
   | `reaction_upload.species[]` |
   | `reaction_upload.transition_state` |
   | `conformer_upload` |

   The schema annotates them with forward-reference strings (`list["SchemeAtomParamPayload"]`, `energy_correction.py:92`), and the walker did not resolve `ForwardRef`. v2 descends them to `atom_params[].element`/`.value`, `bond_params[].bond_key`/`.value` and `component_params[].{component_kind,key,value}`. The A4 rows use that spelling. The adapter never builds `component_params`: Melius BAC tables are not carried, and only a legacy verbatim scheme could place them on the wire.
3. **`FIELD_KEY.md` root table (now updated by another agent).** When this audit started it declared two roots and "0.22.0 authority". It now declares five roots against 0.51.0, and `ts_upload` and `conformer_upload` have published models. This makes the old A4's invented roots `transition_state_upload.*` and `conformer_upload.*` joinable. The first is renamed here to `ts_upload.*`. The A4 status column is renamed from `status_at_0_22_0` to `status_at_0_51`. The concurrently updated `FIELD_KEY.md` fixed the root table but its A4 template still says `status_at_0_22_0`; the join does not read the column.

In v2, no A4 path has a spelling the TSV lacks, and no emitted payload key falls outside the TSV.

## 4. Adapter gaps: ARC exports it, TCKDB has a home, the adapter drops it

These are ranked by scientific weight. None of them is a YAML row, because a row means "adapter maps it".

| # | TCKDB home (0.51) | ARC source (`db0934d5`) | Adapter behaviour |
|---|---|---|---|
| 1 | `reaction_upload.transition_state.validation_evidence[]`, `ts_upload.validation_evidence[]` (`kind: 'irc'`, `passed`, `rationale`, `source_calculation_key`) | `transition_states[].ts_checks.IRC` verdict plus `ts_checks.warnings` and `irc_converged` (`arc/output.py:1983-1986`, `_ts_checks_to_dict` `:2779-2805`). A2 independently confirms that ARC exports this. | Never built: there is no `validation_evidence` anywhere in the adapter, and the TS block (`adapter.py:2888-2909`) and standalone request (`:3088-3105`) omit it. TCKDB then warns `transition_state_missing_irc_evidence` on **every** TS the adapter deposits, even when ARC ran and passed the IRC check. The two participant mappings would still need an atom map, which A3 covers. `passed`, `rationale` and `source_calculation_key` (`ts_irc`) are available now. |
| 2 | `species_upload.thermo.energy_level_of_theory`, `…statmech.energy_level_of_theory`, and the same under `reaction_upload.species[]` and `conformer_upload.statmech` | `composite_method` / `sp_level` / `opt_level` (top level) | The adapter **already computes** this level, as `_thermo_energy_level` (`adapter.py:5177`), to run the enthalpy guard, then throws it away. The thermo builder's allow-list (`adapter.py:5070-5081`) has no slot for it. |
| 3 | **Mis-attribution, not a drop.** `level_of_theory` and `software_release` on the `ts_guess` `path_search` calc | `neb_level` (`arc/output.py:180-181`), `ess_software.neb` and `ess_versions.neb` (`:1924`, key map `:1200`) | The path-search calc is built with `level_kind="opt"` and `ess_job_key="opt"` (`adapter.py:2670-2671`). A GSM (xTB) or ORCA-NEB guess is therefore labelled with the opt DFT level and the opt ESS name and version. The comment at `:2668` says "No ts_guess_level in output_doc today", but ARC does export `neb_level`. This is wrong provenance on a populated field (fixture-populated in both golden TS payloads). |
| 4 | `…calculations[].scf_stability.{status, lowest_eigenvalue, instability_count, instability_type, reoptimized_wavefunction}` on sp and freq calcs | `species[]/transition_states[].wavefunction_stability` (verdict, internal and external instability, `lowest_eigenvalue`, `followed_to_stable`, …; `arc/output.py:1842`, parser `:586-633`). A2 independently confirms that ARC exports this. | Ignored. Only `scf_reference` is read, for `spin_treatment` (`adapter.py:3419-3423`). |
| 5 | `reaction_upload.analysis_software_release.version` | `arkane_version` (`arc/output.py:168`) | **Wired in adapter 0.6.1** (`_arc_analysis_software_release`): `version` ← `arkane_version`, `revision` ← `arkane_git_commit`, each when recorded; the same release now also goes on computed-species `thermo.software_release` and `statmech.software_release`. |
| 6 | Scan-calc `artifacts[]` (`output_log`) | `rotor_scans[].source_log` (`arc/output.py:2381`) | `_LOG_FIELD_BY_CALC_KEY` (`adapter.py:388`) has no scan role, so scan calcs never carry their log, even with `artifacts.upload=true`. |
| 7 | `conformer_upload.statmech.*` (including `freq_scale_factor`, `torsions[]` and `torsions[].invalidated_reason`) and `conformer_upload.applied_energy_corrections[]` | `species[].statmech`, `freq_scale_factor`, `energy_corrections`, and `statmech.rejected_torsions[].invalidation_reason` (`arc/output.py:2122`, `:2172`) | Conformer mode (`_build_payload`, `adapter.py:3277-3298`) emits only identity, geometry and opt/freq/sp. It has no statmech, AEC or scans, although the 0.51 conformer root accepts all three. `invalidated_reason` is the only 0.51 home for ARC's rejected rotors, and no mode reaches it. |
| 8 | `reaction_upload.species[].conformers[].label` | `species[].label` | Set on species-bundle conformers (`adapter.py:1287`) but not on reaction conformers (`:2493-2497`). |
| 9 | `…scan_result.constraints[]` (a 0.51 slot inside the scan result) | `rotor_scans[].constraints[]` | The constraints **are** emitted, but at calc level `constraints[]` (`adapter.py:1526-1533`), not in `scan_result.constraints[]`. A5 or TCKDB should say which home is intended. The data is not lost. |
| 10 | `path_search_result.climbing_image_index` | Derivable (the adapter already flags `points[].is_climbing_image`) | Never set (`adapter.py:7737-7740`). |

Other non-mappings are deliberate. They are recorded, not ranked:

- **Dropped on purpose:** `conformer_energies` (`adapter.py:1318-1329`); torsion `pivot_atoms` and `barrier_kj_mol` (no 0.51 column); constraint `target_value_units` (no unit slot); and TS applied energy corrections on `ts_upload` (no 0.51 slot, debug-logged at `adapter.py:3066`).
- **Dropped on the standalone TS route:** TS scans (`ts_upload` has no `scan_result` slot in 0.51, warned at `adapter.py:3079-3085`) and artifacts (no slot, one-time warning at `:3037`).
- **ARC data with no TCKDB home:** `statmech.e0_kj_mol`, `spin_multiplicity`, `inchi`/`inchi_key`/`formula`, `kinetics.n_data_points`, run-level `atom_energy_corrections`/`bond_additivity_corrections`/`bac_type`, `freq_scale_factor_key`, `datetime_*`, and `energy_corrections[].matched_arkane_key`/`reference_atom_energies`.
- **ARC's `statmech.torsions[].dimension` is ignored.** The adapter derives the dimension from the shape of `atom_indices` instead (`adapter.py:6256`).

**Candidate justified constant, not emitted:** `thermo.phase`. The 0.51 default is `None`. ARC and Arkane thermo is ideal-gas by construction; the reference-pressure fallback (`adapter.py:5132-5137`) already relies on RMG's `IdealGasTranslation`. So the adapter could assert `'gas'`, but today it asserts nothing.

## 5. Constants and defaults: provenance claims that need justification

**Hard literals**, meaning the adapter asserts them and does not read them from ARC:

| Field(s) | Value | Site |
|---|---|---|
| `species_entry.molecule_kind` | `'molecule'` | `adapter.py:3251` |
| `species_entry.species_entry_kind` | `'minimum'`. **This changed** since the 0.22 audit: it is no longer derived from `is_ts`, and an `is_ts=True` record is now refused. | `adapter.py:3209-3257` |
| `species_entry.electronic_state_kind` | `'ground'` | `adapter.py:3258` |
| Calculation `quality` | `'raw'` | `adapter.py:3447` |
| Calculation `workflow_tool_release.name` | `'ARC'`, used everywhere the ARC release is claimed | `adapter.py:3455`, `:5734`, `:2290` |
| `output_geometries[].role` | `'final'` | `adapter.py:1568`, `:1397` |
| `depends_on[].role` | `optimized_from`, `freq_on`, `single_point_on`, `scan_parent` or `irc_start`. These edges are asserted from ARC's workflow invariant, not recorded by ARC. | `adapter.py:1158-1161`, `:1199`, `:1222`, `:1265`, `:2687`, `:2797` |
| `input_geometries` for freq, sp and irc | The conformer's *optimized* xyz, asserted by invariant rather than recorded as an input | `adapter.py:1614-1621` |
| `opt_result.converged` on `opt_coarse` | `True`. ARC exports no coarse convergence flag. | `adapter.py:4435` |
| `path_search_result.converged` | `True`, justified only by the emission gate | `adapter.py:7749` |
| `path_search_result.is_double_ended` | `True` for neb and gsm, from a static table | `adapter.py:486-489`, applied at `:7758` |
| `path_search_result.source_endpoint_count` | `2` for neb and gsm, from the same table | `adapter.py:486-489`, applied at `:7758` |
| IRC TS-marker point | `is_ts=True`, `reaction_coordinate=0.0`, relative energy 0. This point is synthesized: it is ARC's seed geometry, not a parsed point. | `adapter.py:7222-7244` |
| `irc_result.direction` | `'both'` when no direction resolves | `adapter.py:7205-7212` |
| `freq_result.modes[]` imaginary entries | Re-inserted as `-abs(v)` from `imaginary_frequencies_cm1` or `imag_freq_cm1` | `adapter.py:4660-4684` |
| `modes[].imaginary_disposition` | `'unassigned'`, and designation of the reaction coordinate by ARC's (75, 10000) cm⁻¹ window | `adapter.py:4462-4503`, `:4758` |
| `thermo.enthalpy_reference_kind` | `'formation_298k'`, whenever enthalpy survives the guard | `adapter.py:5470` |
| `statmech.freq_scale_factor.scale_kind` | `'fundamental'` | `adapter.py:5843` |
| `statmech.freq_scale_factor.workflow_tool_release` | The ARC release, claimed only when `freq_scale_factor_source` is set | `adapter.py:5858-5869` |
| `kinetics[].model_kind` | `'modified_arrhenius'` | `adapter.py:6742` |
| `kinetics[].a_uncertainty_kind` | `'multiplicative'` | `adapter.py:6928` |
| `reaction_family_source_note` | `'ARC-reported family'` on both reaction routes | `adapter.py:2285`, `:3192` |
| `ts_upload.reaction.reversible` | `True`. The standalone schema requires the field, and ARC exports no `reversible`, so this is **always** the constant. The reaction route omits the field instead. | `adapter.py:3175` |
| `conformer_upload.scientific_origin` | `'computed'` | `adapter.py:3291` |
| `analysis_software_release.name` | `'Arkane'` (also `species_upload.thermo`/`statmech.software_release.name` since 0.6.1) | `_arc_analysis_software_release` |
| `hessian.parser_version` (reparse fallback) | `'arc-hessian-1'` | `adapter.py:372` |
| `parameters_json.tckdb_origin` markers | `origin_kind` `reused_result` or `derived`; `origin_detail` `screened_conformer`; `independent_ess_job: false`; `producer: 'ARC'` | `adapter.py:4247-4266`, `:4349-4380` |
| `parameters[]` (freq Hessian method) | Keys `freq_hessian_method`/`freq.hessian_method`, section `freq`, value type `string` | `adapter.py:3469-3476` |
| Scan coordinate indices | `coordinate_index` and `coordinate_values[].coordinate_index` = 1 | `adapter.py:171`, `:210` |
| Adapter-minted local keys | `conf0`, `alt<i>`, `r<i>_<label>`, `ts_geom`, `<actor>_geom`, `<actor>_conf0`, and calc keys | `adapter.py:977`, `:1355`, `:5664-5686`, `:2596-2603` |

**Defaults that mask absence:**

| Field | Default | Site |
|---|---|---|
| `thermo.reference_pressure_bar` | **None since adapter 0.6.0.** The value is `standard_state_pressure_pa`/1e5 when that is a number within [0.5, 2.0] bar; otherwise the field is omitted and `thermo_reference_pressure_not_stated` is reported. Before 0.6.0 it defaulted to 1.01325 bar (RMG's hard-coded P0 = 1 atm), which the producer contract forbids. | `adapter.py` `_thermo_reference_pressure_bar` |
| `species_entry.charge` / `.multiplicity` | 0 / 1. A record without a multiplicity uploads as a singlet. | `adapter.py:3253-3254` |
| TS `charge` | 0 | `adapter.py:2889` |
| TS multiplicity | The reaction multiplicity | `adapter.py:2879-2886` |
| Freq and sp level of theory | `opt_level` when `freq_level`/`sp_level` is null (`_resolve_level`). Scan has no fallback: without `scan_level` the scan calc is skipped. | `adapter.py:4383-4404` |
| `software_release.version` | `ess_versions['opt']`, but only when the software names match, and never for scan. Since 0.6.1 the banner is split into `version`/`revision` by TCKDB's own `SoftwareReleaseRef.normalize_composite_version` (`Gaussian 16, Revision C.02` → `16` / `C.02`); banners whose leading token is not the software name are sent unchanged | `adapter.py` `_split_ess_version_banner` |
| `software_release.name` | The requested level's software when no observed `ess_software[job]` exists | `adapter.py:3400` |
| `freq_scale_factor.software.name` | `opt_level.software` | `adapter.py:5851-5856` |
| Scan `dimension` / `is_relaxed` / `value_unit` / `index_base` | 1 / `True` / `'degree'` / 1 | `adapter.py:149`, `:174`, `:212`, `:223-224` |
| Kinetics `T0_k` | 1 K when absent | `adapter.py:6797` |
| Labels | `'unlabeled'` and `'unlabeled-ts'` | `adapter.py:976`, `:3034` |

## 6. Refusal and omission behaviour

**Thermo enthalpy guard** (`adapter.py:5240-5514`). H298, NASA, and point H and G are stripped, while S298, Cp and point S are kept. It triggers in these cases:

- ARC recorded `atom_corrections_applied=false`.
- `atom_corrections_level` differs from the energy level, compared by normalized method plus dispersion and basis.
- Either level sets a dispersion or solvation field that ARC's matching ignores (`…_level_unverifiable`).
- A value is non-finite.
- The **magnitude guard** fires: |H298|, point H, or NASA H(298 K) above 2×10⁴ kJ/mol.

Each refusal adds a sidecar warning with action `thermo_enthalpy_omitted`, or `thermo_omitted` if nothing is left. The finished block is then checked with the shared `enthalpy_reference_error`, and a block that rule refuses is dropped whole.

Observed on `arc_1_2`:

| Species | Warning |
|---|---|
| `CH4_standin` | `level_mismatch` |
| `CH4_uncorrected`, `H`, `H2` | `not_applied` |
| `CH4_yml` | `not_formation_magnitude` |

The `atom_corrections_*` flags exist only on ARC branch `feature_export_atom_corrections_applied` (`a10e8ae0`), with output schema 1.2. ARC `main` `db0934d5` exports output 1.1 and does not have them. There the guard is only the finiteness and magnitude checks, so H and H2 (below 2×10⁴) would pass uncorrected.

**Scan provenance omission** (`adapter.py:6008-6019`, `:1241`). A scan calc without `scan_level` is skipped, and any torsion `source_scan_calculation_key` pointing at it is removed. Unknown keys are left for TCKDB to reject, and the adapter does not repair them.

**Frequency contradictions** (`adapter.py:4604-4785`). The adapter raises `ValueError` and refuses the **whole record** (a sweep failure) in these cases:

- A minimum has `n_imag>0`, or its harmonic list contains a negative value.
- A TS has `n_imag≤0`.
- A TS has `n_imag>1` with no unique in-window designation.

When the mode count does not match `n_imag`, only `modes` is omitted, with a warning.

**Kinetics units:**

- A or Ea with an unmapped or null unit drops the value and its unit together (a warning). The rest of the kinetics block is kept.
- `dEa` is sent only when its unit matches the `reported_ea_units` on the wire.
- `dA<1`, and `Tmin`/`Tmax≤0`, are omitted.

**Energy corrections.** A correction without `total.unit` is dropped rather than given an assumed unit (C-6). Components lacking `parameter_value` or `contribution_value` are dropped, and `parameter_unit` is always stripped.

**Hessian, IRC and GSM** read ARC parser evidence first. A sidecar state of `unavailable` suppresses the sub-payload. A state of `fallback` reparses the on-disk logs through the optional-ARC boundary.

- IRC parse failure still emits a bare `type=irc` calc.
- NEB, and GSM without parseable frames, collapse to a single-point path from `opt_input_xyz`.
- GSM absolute energies and gradients come **only** from geometry-matched evidence. `_read_gsm_node_outputs` (`adapter.py:7420`) is now dead code: nothing calls it, and a warning at `:7559` says archived node energies are omitted.

**Sweep gating** (`sweep.py`):

- Only `converged` species and TSs are uploaded.
- Reactions with a missing or non-converged TS become partial bundles when `allow_partial_uploads` is set. `ts_label` and `kinetics` are stripped from these bundles, and they are never POSTed.
- Conformer mode never receives TS records.

## 7. `transport_upload`

The adapter has **no transport path at all**. No builder emits `transport`, and the only mention is a docstring in `__init__.py`. ARC `db0934d5` exports no Lennard-Jones, dipole or polarizability data (none in `arc/output.py` or the output schema). The real transport home, `conformer_upload.transport.*`, is therefore ARC-absent as well as adapter-absent. The adapter never POSTs to `/api/v1/uploads/transport` and never fills `conformer_upload.transport`. All 30 v2 `transport_upload.*` leaves, and the backend-only request fields noted in §3.1, are ARC-absent and adapter-absent. No A4 row exists for them.

## 8. Changes from the previous A4 (0.22-era, 775 rows)

- **Roots:** `transition_state_upload.*` became `ts_upload.*`. 726 paths carry over, 106 are new and 49 were dropped.
- **Container rows dropped (for example `species_upload.thermo`, `reaction_upload.species[]`).** The shared path list has leaves only.
- **Old rows that were wrong and are now removed:**
  - `ts_upload.additional_calculations[].opt_result.*` and `reaction_upload.transition_state.calculations[].opt_*`: the TS block never builds an opt as an *additional* calc.
  - `…transition_state.calculations[].output_geometries[]` / `ts_upload.additional_calculations[].output_geometries[]`: only opt and opt_coarse roles get output geometries.
  - `conformer_upload.calculation.parameters_json`: the conformer-mode opt gets neither a marker nor `final_settings`.
- **New since the 0.22 audit:**
  - `thermo.enthalpy_reference_kind` and `thermo.reference_pressure_bar`
  - `level_of_theory.spin_treatment`
  - `parameters[]` (the freq Hessian method)
  - `freq_result.reaction_coordinate_mode_index`, `modes[].imaginary_disposition`, and their flat forms `freq_reaction_coordinate_mode_index` and `freq_imaginary_dispositions`
  - `reaction_upload.species[].calculations[].conformer_key`
  - `freq_scale_factor.workflow_tool_release.*`
  - `scheme.note`, which now also carries a legacy scheme `version`
  - scan coverage on the reaction-TS route
  - the AEC `scheme.level_of_theory.*` leaves
- **Semantic changes worth re-checking downstream:**
  - `species_entry_kind` is now a constant `'minimum'`.
  - The observed `ess_software[job]` now takes precedence over the requested level's software.
  - Kinetics `a` is now normalized by `T0`.
  - `constraints` require an explicit `index_base`: legacy mappings without it are dropped, where the old behaviour assumed 1-based.
  - A thermo block now carries `source_calculations` on *both* roots.

## 9. Self-assessment of coverage

**Confident:**

- The row set is complete for every builder in `adapter.py`. The generator fails if any empirically populated path is missing from the catalog, and every catalog path exists in 0.51.
- `emission_route` and `source` lines were re-read at `0913124`.

**Less certain:**

1. **Code-only rows (209).** I reasoned about their reachability rather than observing it. I removed the routes I could show to be impossible: TS-additional output geometries, TS-additional opt results, and the conformer opt's `parameters_json`. The others rest on `_calculation_payload` being shared.
2. **Pass-through surfaces.** The legacy record-level `additional_calculations[].scan_result` (`adapter.py:106-108`) and legacy `applied_energy_corrections[].scheme` (`adapter.py:5032-5035`) are copied **verbatim**. Such a record could therefore place other `scan_result.*` or `scheme.*` keys (for example `scan_result.note`, `scheme.software.*` or `scheme.component_params`) on the wire. I added rows only for keys the adapter itself constructs, plus the pass-through keys seen in the corpora. Current ARC emits only the neutral shapes.
3. **Sources the adapter reads that ARC `main` does not export.** These rows will read as SOURCE_UNCONFIRMED in the join. That is correct: the adapter code handles them, but ARC does not supply them.

   | Adapter source | Where ARC stands |
   |---|---|
   | `unmapped_smiles`, `reactions[].reversible`, `kinetics.degeneracy`, `kinetics.note`, `sp_spin_diagnostic.note`, `irc_final_settings`, `thermo.cp_data`, and `electronic_energy_hartree` as an sp fallback | Not exported |
   | `thermo.atom_corrections_applied` / `atom_corrections_level` | ARC branch `a10e8ae0` only |

4. **Fixture realism.** "fixture" means a checked-in fixture, but only the golden `phase3` corpus derives from a real ARC run. `arc_1_2` is a hand-assembled 1.2 thermo corpus, and `current_arc` builds nothing. No fixture exercises any of these: statmech/torsions, kinetics, scans, constraints, AEC, spin diagnostics, `scf_reference`, artifacts, or coarse opt. Those rows are `synthetic` or `code_only`.
5. **Line numbers** point at the line that assigns the key. Where one assignment writes several keys (NASA `a1..a7` at `:5574`, LoT fields at `:4967`, components at `:5028`), several rows share a line.

The generator (`gen.py`), capture plugin, fixture builder and flattener lived in the A4 scratch directory and are not committed. To rebuild, run the adapter suite with a builder-wrapping pytest plugin, flatten the payloads against `paths_0_51.tsv`, and diff the result against this YAML.
