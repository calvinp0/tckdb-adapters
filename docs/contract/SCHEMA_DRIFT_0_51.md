# Schema drift: tckdb-schemas 0.22.0 → 0.51.0

**Range:** `TCKDB_v2` `3f929069` (0.22.0, which the previous `TCKDB_DEMAND.yml` was written against) → `ad3cd706` (HEAD, 0.51.0).
The untracked `.agents/` and `paper/` directories were ignored.

**Scope:** `schemas/python/tckdb-schemas/tckdb_schemas/`, plus the backend seams that refuse or warn on upload:

- `backend/app/workflows/`
- `backend/app/services/`
- `backend/app/api/routes/uploads.py`
- `backend/app/schemas/workflows/transport_upload.py`

**Deliverables regenerated with this note:**

- `TCKDB_DEMAND.yml` (A1)
- `SCHEMA_DRIFT.yml` (A5)
- the authority line and root table in `FIELD_KEY.md`

`SCHEMA_DRIFT.md`, `SCHEMA_DRIFT_0_23.md` and `SCHEMA_DRIFT_0_33.md` are left as history. The previous `SCHEMA_DRIFT.yml` (0.8.0 → 0.22.0, 61 rows) is superseded; it remains in git history at `0913124`. `TCKDB_DEMAND.md` is now stale: it describes the 0.22 inventory.

---

## Verdict

On the wire, 0.22 → 0.51 is almost entirely additive. Across the two roots the old audit covered:

- 298 paths were added.
- 8 were removed. The adapter never emitted them: 4 are `literature_id`, 4 are `scheme.version`, and the adapter moves the legacy scheme version into `note`.
- 6 were retyped: `FreqScaleFactorRef.software` changed from `SoftwareRef` to `SoftwareReleaseRef`, which is a superset.
- 34 changed requiredness: all are the NASA7 bounds and coefficients, now required inside a `nasa` block.

The drift that matters is **behavioural**, and most of it is invisible to a schema diff. Between 0.22 and 0.51 the backend gained:

- an enthalpy-declaration rule;
- source-calculation role/type rules and ownership rules;
- the level-of-theory R-rules on opt/sp links;
- a Petersson BAC total that must decompose into components;
- a refusal to name a workflow tool as a calculation's software;
- frequency-list completeness and linearity checks;
- a thermo state (`phase`, `reference_pressure_bar`, 0 K formation enthalpy) that is never defaulted where it used to be.

These are rows in `SCHEMA_DRIFT.yml` with `change: semantics_changed` (95 rows). They are also rows in `TCKDB_DEMAND.yml` with `row_kind: workflow_check` (508 rows).

**Breaking for today's adapter (`tckdb_arc` at `0913124`):** one class, flagged `breaking_for_producer: true`. A reaction-bundle transition-state `bac_total` from a Petersson scheme is refused with `bac_total_requires_components` when it carries no components.

- The adapter forwards ARC's `components` or `[]` (`tckdb_arc/tckdb_arc/adapter.py:303`, `:2904`).
- TCKDB's own comment at `backend/app/services/energy_correction_resolution.py:585-600` says ARC supplies no bond components for a saddle point, and that 17 such ARC rows were measured.
- Whether ARC's 1.2 output now carries TS components is a supply question for A2/A3.

Everything else the adapter builds today was checked against the new rules and already conforms. The adapter's recent commits declare `enthalpy_reference_kind` and `reference_pressure_bar`, count point `g_kj_mol` as enthalpy content, and name Arkane only as analysis software.

---

## Path-list corrections

These are corrections to the shared walker output `scratchpad/shared/paths_0_51.tsv`. The orchestrator should propagate them to every inventory.

1. **`transport_upload` is the backend `TransportUploadRequest`, not `TransportUploadPayload`.**
   - v1 of the shared list walked `LiteratureUploadRequest` by mistake (12 wrong paths). v2 walks the wire `TransportUploadPayload`, but `POST /api/v1/uploads/transport` validates `TransportUploadRequest` (`backend/app/api/routes/uploads.py:646`).
   - That model extends the payload with `species_entry`, `rights`, `calculations[]` and `source_calculations[]` (`backend/app/schemas/workflows/transport_upload.py:66-99`).
   - Decision: the root is `TransportUploadRequest`, and `FIELD_KEY.md` says so.
   - `TCKDB_DEMAND.yml` therefore has **183 leaf paths beyond v2**, plus their container rows:
     - `transport_upload.species_entry.*` (12)
     - `transport_upload.rights.*` (3)
     - `transport_upload.calculations[].key`, and `transport_upload.calculations[].calculation.*`, the full `CalculationWithResultsPayload` tree (166)
     - `transport_upload.source_calculations[].calculation_key` and `.role` (2)
   - Every v2 transport path is still present with the same spelling. Walking the payload alone would not be wrong, just incomplete.
2. **Forward-referenced lists are containers.** On all five correction routes, `…applied_energy_corrections[].scheme.atom_params`, `.bond_params` and `.component_params` are `list["Scheme…ParamPayload"]`. They are now spelled `…scheme.atom_params[]` with leaves `.element` and `.value`; `…bond_params[]` with `.bond_key` and `.value`; and `…component_params[]` with `.component_kind`, `.key` and `.value`.
   - This is 15 re-keyed containers and 35 leaves.
   - Rows in the **previous** 0.22 inventory used the leaf spelling `…scheme.atom_params`. The 0.22 walk had the same forward-ref bug, so any older A2/A3/A4 row keyed on it must be re-keyed.
   - v2 of the shared list already fixes this.
3. **Container, root and union-variant rows.** The walker emits leaves only. Following the existing convention (for example `reaction_upload.atom_map` and `species_upload.conformers[]`), `TCKDB_DEMAND.yml` also has:
   - one row per root (5);
   - one row per nested-model field (580 containers);
   - one row per discriminated-union member (44, the `…execution_environment.runtime{conda|container|described|hpc_module}` variants on 11 routes).
4. **No other walker defects found.**
   - There are no recursive models.
   - The only discriminated union is `ExecutionEnvironmentManifestPayload.runtime` (`runtime_kind`). It was handled correctly, and no `{ModelName}` fallback segment occurs.
   - My independent walk matches v2 exactly on every other path: 2,991 v2 leaves are all present, plus the 183 transport extras.

---

## 1. What changed on the wire (0.22 roots)

### Added (298 paths, by introducing commit)

| Commit | PR | What | Paths |
|---|---|---|---|
| `61f4b256` | #151 | `scf_stability` (`SCFStabilityContent`) on every bundle calculation. `BundleThermoIn` gains `literature`, `software_release`, `workflow_tool_release`, `source_calculations[]`, and `h298_`/`s298_uncertainty`. | 73 |
| `318a76e1` | #439 | `scheme.software` and `scheme.workflow_tool_release` on correction schemes. `software` was retyped to `SoftwareReleaseRef` in `df45eb20` (#458). | 52 |
| `ed5d610c` | #172 | Inline `literature` on `CalculationIn`, replacing `literature_id`. | 52 |
| `8e0ffeb5` | #386 | `energy_level_of_theory` on species/reaction thermo and statmech. Declared, checked, never persisted. | 40 |
| `76f47241` | #464 | `frequency_scale_factor.software` and `freq_scale_factor.software` become `SoftwareReleaseRef` (adds `version`, `revision`, `build`, `release_date`, `notes`). | 30 |
| `c734499c` | #155 | Reaction statmech gains `literature`, `software_release`, `workflow_tool_release` and `rotational_constant_{a,b,c}_cm1`. | 29 |
| `fd68f1ba` | #495 | `phase`, `reference_pressure_bar`, `enthalpy_formation_0k_kj_mol` and its uncertainty (`ThermoStateFields`). | 8 |
| `a2c34447` | #500 | `rights` (`DepositRights`) on both roots. | 8 |
| `4ae6e756` | #254 | `conformer_key` on `CalculationIn`. | 4 |
| `b338025a` | #520 | `enthalpy_reference_kind`. | 2 |

### Removed (8)

- `…calculation.literature_id` and `…calculations[].literature_id` on the reaction bundle. Replaced by inline `literature` (`ed5d610c`).
- `…applied_energy_corrections[].scheme.version` on 4 routes. Scheme identity is now `(kind, name, level_of_theory, source_literature, software_release, workflow_tool_release)` (`df45eb20`), and `units` left the identity in `c4f2cbf2` (#462).
- Both removals are `extra="forbid"` 422s if sent. The adapter never sends either.

### Retyped and requiredness changes

- `…frequency_scale_factor.software` and `statmech.freq_scale_factor.software`: `SoftwareRef | None` became `SoftwareReleaseRef | None` (`76f47241`). This is a superset, so a `{name}`-only payload still validates.
- `…thermo.nasa.{t_low,t_mid,t_high,a1..a7,b1..b7}`: optional became **required** inside a `nasa` block (`ThermoNASACreate`, `fd68f1ba`). They must also be finite.

### New roots (new to this audit)

| Root | Model | Since |
|---|---|---|
| `ts_upload` | `TransitionStateUploadRequest` | Wire contract published in 0.23.0 (`da0d5815`). The adapter's `computed_ts` mode posts it. |
| `conformer_upload` | `ConformerUploadRequest` | 0.23.0 (`da0d5815`). The adapter's conformer mode posts it. |
| `transport_upload` | `TransportUploadRequest` (backend) | Pre-0.22 and backend-side. The adapter does not post it. |

---

## 2. Behavioural drift: new refusals and warnings

The API code catalogue (`backend/app/api/code_catalogue.py`) did not exist at 0.22. Of its 244 codes, 110 have no literal anywhere in `backend/app` or `tckdb_schemas` at `3f929069`. The upload-relevant subset is below. Every row is in `TCKDB_DEMAND.yml` as a `workflow_check` with its raise site.

### Block tier (422 unless noted)

| Code | Constrains | Commit |
|---|---|---|
| `enthalpy_declaration_absent`, `enthalpy_declaration_without_content`, `enthalpy_quantity_not_storable_here`, `enthalpy_reference_kind_unrecognized` | `…thermo.enthalpy_reference_kind` versus H298, NASA, point H and point G. Raised by the backend workflow (`backend/app/workflows/thermo.py:291`), not by the schema, so tckdb-client will not catch it. Point G was added to the enthalpy content in `2a32f694` (#555, 0.51). | `b338025a` |
| `thermo_source_role_type_mismatch`, `statmech_source_role_type_mismatch` | `…source_calculations[].role` versus `Calculation.type` (opt→opt, freq→freq, sp→sp or opt) | `e14209a3`, `d2904a7d` |
| `thermo_*` / `statmech_*`: `_role_duplicate`, `_sp_geometry_mismatch`, `_energy_level_ambiguous`, `_energy_level_requires_sp`, `_energy_level_contradiction` | opt/sp role links and the declared `energy_level_of_theory` (`backend/app/services/calculation_levels.py`) | `8e0ffeb5` |
| `thermo_/statmech_/transport_source_calculation_owner_mismatch`, `statmech_torsion_scan_calculation_owner_mismatch`, `applied_energy_correction_source_calculation_owner_mismatch` | Source keys must name calculations owned by the same species or TS entry. Some refusals existed at 0.22 without a code; they were coded in #159 and #178. | `971e1ae2`, `62a1d48e` |
| `calculation_key_undeclared`, `conformer_key_undeclared`, `geometry_key_unresolved`, `species_key_undeclared`, `statmech_calculation_key_undeclared`, `transition_state_key_undeclared`, `applied_energy_correction_source_key_undeclared` | Local-key resolution. Some were prose at 0.22 and are coded now; ADR 0017 makes the schema layer and the workflow layer answer the same way. | `d2904a7d`, `cb38bfc2` |
| `bac_total_requires_components` | `…applied_energy_corrections[].components[]` for a Petersson `bac_total`: always on a TS, and on a bonded species | `9b412250` |
| `calculation_software_is_workflow_tool` | Every calculation's `software_release.name` must not fold to arkane, arc, rmg or rmgpy | `400e2a5e` (#560), `34509c46` (#565) |
| `calculation_geometry_composition_mismatch` | Every geometry linked to a calculation must match its subject (species formula; TS = sum of the reactants) | `aee41adb` |
| `freq_list_exceeds_geometry_degrees_of_freedom` | More than 3N modes in a frequency list | `080aea31`, `9c332e89` |
| `freq_mode_index_not_unique`, `freq_n_imag_disagrees_with_modes` | Existing prose refusals that now have codes | `a7cdb560`, `db4bc37f` |
| (prose) | `reaction_upload.species[].thermo` must carry content (`validate_scientific_content`) | `fd68f1ba` |
| (prose) | Coordinates on an atomless participant, such as an electron | `61f4b256` |

### Warn tier (upload accepted)

| Code | Where | Commit |
|---|---|---|
| `freq_list_incomplete_for_geometry` | Fewer than 3N−6 modes (3N−5 when N≤2) | `080aea31` |
| `freq_list_linear_mode_count_for_bent_geometry`, `freq_list_bent_mode_count_for_linear_geometry` | Mode count versus geometry linearity. Synchronous routes only. | `38766219` |
| `software_release_version_is_composite`, `software_release_name_looks_wrong` | Every `SoftwareReleaseRef.version` in the request. Synchronous upload routes only (`collect_software_release_version_warnings`). | `54e36c37` |
| `software_release_version_filled_from_artifact` | Calculation `software_release.version` filled from the output-log banner | `34509c46` |
| `converged_opt_no_usable_energy` | A converged opt whose owner has no sp and no artifact-backed opt energy | `ba81dcb3` |
| `calculation_conformer_anchor_unresolved` | A reaction-bundle species calculation that names neither `conformer_key` nor `geometry_key` | `4ae6e756` |
| `statmech_frequency_scale_factor_software_mismatch` | The FSF's software differs from the freq source calculation's software | `82b75885` |
| `missing_energy_correction_scheme_software`, `ambiguous_energy_correction_scheme_without_literature` | A newly created correction scheme | `318a76e1` |
| `literature_title_mismatch`, `literature_year_mismatch` | Inline literature versus DOI/ISBN metadata | `fb083552` |

### Silent defaults that changed meaning

- **`…thermo.reference_pressure_bar`** is never defaulted (0.49.0, `71177e31`). It was 1.0 bar when it was introduced in 0.45. If omitted it is stored NULL and no warning is emitted. ARC's entropies are at 1 atm, so the only honest value ARC can send is 1.01325; the adapter already sends ARC's recorded pressure.
- **`…thermo.phase`**: omitting it on a computed record gives `gas`, while an explicit null is kept as unknown (`fd68f1ba`).
- **`species_upload.workflow_tool_release`** is now the default for the `thermo` and `statmech` blocks (`75f1a114`).
- **`species_upload.note`** is documented as accepted but not persisted (`75f1a114`).

---

## 3. The most important new TCKDB demands for ARC data

In priority order for the ARC→TCKDB contract:

1. **Declare the enthalpy zero.**
   - Any H298, NASA block, point H, or (since 0.51) point G needs `enthalpy_reference_kind='formation_298k'`. A declaration on an S/Cp-only record is also refused.
   - It is checked by the backend, not the schema. The adapter already derives it from ARC's `atom_corrections_applied` and `atom_corrections_level`, and strips the enthalpies when they are not formation enthalpies.
2. **State the standard-state pressure.** `reference_pressure_bar` is never defaulted. Without it the S298, Cp and G of ARC deposits have an undeclared standard state, and the consistency checks treat the pressure as unrecorded.
3. **Send a complete NASA7 fit.** All 3 bounds and 14 coefficients are required and must be finite. A partial fit is a 422.
4. **Make each source-calculation role match the calculation type.** Thermo and statmech `role` must fit `Calculation.type`, and sp accepts sp or opt. The linked calculation must be owned by the same species entry.
5. **Satisfy the level-of-theory rules on opt/sp links.**
   - Every linked opt needs an sp at its geometry once any sp is linked. Two sps on one opt are refused, and so are sps at different levels.
   - In a multi-conformer bundle, linking an opt calculation under role `sp` leaves every opt "uncovered". That triggers `*_energy_level_requires_sp`, a 422.
   - `energy_level_of_theory` can declare the sp//opt level, and it is checked against those links.
6. **Decompose a Petersson BAC total.** A `bac_total` without components is refused on a TS always, and on any bonded species. This is the one breaking item for the adapter today (see Verdict).
7. **Name the real electronic-structure program.** A calculation's `software_release` must be the ESS. ARC, Arkane and RMG are refused there (#560, merged 2026-09-29). They belong in `workflow_tool_release` or `analysis_software_release`.
8. **Send complete, signed frequency lists.** More than 3N modes is refused. Fewer than 3N−6 modes, or a linear count on a bent geometry, is flagged. A TS with n_imag > 1 needs the list to designate its reaction coordinate: without it the upload is refused with `transition_state_reaction_coordinate_not_designated`.
9. **Correction-scheme identity has changed.** It now includes the program release, the citation and the workflow tool. `scheme.version` is gone. ARC should send `scheme.software` (the Gaussian or ORCA release) for atom-energy and BAC schemes, or each new scheme warns. The FSF `software` is a `SoftwareReleaseRef` and is compared with the freq calculation's software.
10. **Reaction-bundle thermo is now a full record.** It needs content and supports `source_calculations[]`, provenance and uncertainties. Provenance falls back to the bundle-level `analysis_software_release` and `workflow_tool_release`. A missing one warns, and the kinetics warning names the field `software_release` even though it checks `analysis_software_release`.
11. **Anchor species calculations with `conformer_key`** in the reaction bundle. `geometry_key` now only names a geometry, and a calculation naming neither is stored unanchored with a warning.
12. **Declare `rights` (`DepositRights`) to make deposits releasable.** It is optional and silent at upload, but a release refuses records that have no rights basis. `depositor_attests_right_to_license` must be literally `true`.
13. **SCF stability can be deposited on the bundles** (`scf_stability`). Send `status='stable'` only when a stability analysis was actually observed.
14. **Link kinetics sources explicitly.** Without `kinetics[].source_calculations`, the reaction route auto-links the first sp of each reactant and product and adds no TS link. Also, A-units are not checked against molecularity on the bundle; they are only checked on the standalone kinetics route.
15. **Route asymmetries on the roots the adapter uses:**
    - `ts_upload` always warns `reaction_atom_map_absent`, because it has no `atom_map` field. It has no slot for applied corrections, and the async `/jobs/transition-state` route drops every warning.
    - `conformer_upload` runs no provenance or statmech-content warnings for its nested statmech or transport.
    - `label` is ignored for conformer grouping on every route: `resolve_conformer_group` never reads it.
    - `molecule_kind='pseudo'` is refused on every upload root, because species resolution accepts only molecule and electron.

---

## 4. Inventory shape and counts

`TCKDB_DEMAND.yml` has 3,803 field rows and 508 workflow-check rows (4,311 total). Every row has `path` and a `source` whose file exists and whose line is within the file (checked mechanically). The field rows' `source` is the AST line of the declaring class attribute.

| Root | Field rows | required | optional | conditional | Check rows (block / warn / other) |
|---|---|---|---|---|---|
| `species_upload` | 926 | 233 | 548 | 145 | 119 (64 / 32 / 23) |
| `reaction_upload` | 1,477 | 386 | 819 | 272 | 225 (164 / 39 / 22) |
| `ts_upload` | 460 | 129 | 253 | 78 | 67 (48 / 11 / 8) |
| `conformer_upload` | 681 | 160 | 412 | 109 | 63 (33 / 16 / 14) |
| `transport_upload` | 259 | 66 | 147 | 46 | 34 (15 / 12 / 7) |
| **Total** | **3,803** | **974** | **2,179** | **650** | **508** |

Workflow-check rows carry `row_kind: workflow_check`, `check`, `code`, `tier`, `http_status` and `on_violation`. They always come after all field rows, so the join tool's `demand_rows[0]` is the field row. The join runs cleanly on a scratch copy of `docs/contract/`.

How the field-row text was produced:

- 2,025 rows reuse the 0.22 row verbatim where the declaration is unchanged, with `source` re-anchored.
- 1,331 reuse the text of the same `(owner model, field)` on another route.
- 161 reuse text by `(field, annotation)` for fields shared across roots. Blacklisted generic names such as `note`, `value` and `key` were excluded.
- 286 are fresh.
- About 200 hand-written overrides then restate everything that changed since 0.22 or depends on a route.

---

## 5. What I could not establish (read before trusting a row)

- **Inherited 0.22 text.** `on_absence` in the 2,025 verbatim rows was re-checked only where a 0.51 workflow check touches the path, or where a keyword scan found a claim about a route (for example "no warning on this route" or "collector"). I corrected every stale claim those passes found:
  - the reaction thermo/statmech provenance warnings;
  - the `freq_scale_factor` warning on both bundles;
  - `transition_state_extra_imaginary_modes_not_assessable`, which the 0.22 text claimed and which is unreachable at 0.51;
  - conformer `label`.

  A 0.22 claim on a path no 0.51 check touches was not independently re-verified.
- **Workflow-check `source` lines.** Two read-only sub-audits (reaction/TS and species/conformer/transport) produced these and machine-checked that each line is within 2 lines of the raise, the warning construction or the code constant. I verified that every file and line exists, and machine-spot-checked 25 random ones (all 25 passed). Seventeen reaction-side items cite a shared raise site that receives its code as a parameter (`calculation_levels.assert_role_consistency`, `thermo.assert_enthalpy_reference`, `local_key_resolution`).
- **Prose-only refusals** (`code: null`). I did not check whether `validation_detail_code` promotes any of them to a named code. The client probably sees `validation_error` / `request_validation_error`.
- **`breaking_for_producer`** was judged by reading the adapter at `0913124`, not by running it against 0.51. A4 and Phase B should confirm two things:
  - the TS `bac_total` components case;
  - that ARC's multi-conformer thermo/statmech links never produce the `*_energy_level_requires_sp` pattern (opt linked as `sp`, or an sp without an input geometry among more than one linked opt).
- **Unreachable or unemitted codes:**
  - The reaction/TS routes never emit `missing_level_of_theory_provenance`, `missing_kinetics_interpretation_*`, `missing_tunneling_application_evidence` or `reaction_family_not_applied`.
  - The owner-mismatch codes are unreachable on the species, conformer and transport routes, because every key resolves to a calculation the same request just created.
  - `energy_correction_scheme_literature_not_attached` is defined but never emitted.

  These are recorded as `tier: other` rows, not as demand.
- **Tau (ADR 0012).** The upload-time tau comes from the freq calculation's own `parameters[]` canonical keys. Not recorded gives 50 cm⁻¹. The value depends on the Hessian method and grid: 80 or 50 cm⁻¹ for finite-difference Hessians, 30 cm⁻¹ for analytic ones, and 15 cm⁻¹ for analytic with a tight grid and tight opt. I did not check whether a later read-side re-judgement with the inferred Hessian method changes anything. See `SCHEMA_DRIFT_0_33.md` §6 for the prior finding.
- **Scope of the new-code list.** "New since 0.22" means that no literal for the code exists at `3f929069`. A refusal that existed as prose and gained a code is listed as such where I could tell (freq modes, ownership, key resolution). Elsewhere a newly coded old refusal may be listed as new.
