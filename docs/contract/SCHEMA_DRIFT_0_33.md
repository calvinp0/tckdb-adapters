# Schema drift — tckdb-schemas 0.24.0 → 0.33.0, tckdb-client 0.35.0 → 0.42.0

Range: `TCKDB_v2` `14c5e74f` → `main` (`aee41adb`), 32 commits.
Scope: `schemas/python/tckdb-schemas/tckdb_schemas/` (excluding the stale
`build/` artifact), `clients/python/`, `backend/app/schemas/workflows/`, and
`backend/tests/api/golden/openapi.json`.

Method: `model_json_schema(mode="validation")` was dumped for all 107 (old) /
108 (new) published models at both revisions and diffed mechanically, and the
same was done for all 1,000+ component schemas in the golden OpenAPI. Because a
JSON-Schema dump cannot see a `model_validator`, the schemas source tree was
also diffed line-by-line, and every new refusal was traced to the module that
raises it. The adapter's uncommitted tree was executed against 0.33.0 in
`scratchpad/v33`; no file in the adapter tree and no TCKDB branch was modified.

---

## Verdict

**Nothing breaks.** Across nine schema versions there is not one removed or
retyped field the ARC adapter emits, not one unit change, and not one
index-base change — the adapter's uncommitted suite is 714 passed / 1 failed /
16 skipped / 37 subtests against 0.33.0, and the single failure is a
regression *demo* asserting a `ValidationError` that the server deliberately
stopped raising. What is now **redundant** is the whole reason that demo
exists: `BundleThermoIn` went from 8 fields to 14 in `61f4b256` (#151) and now
accepts `source_calculations`, so `_build_thermo_block`'s `target_model` gate
no longer gates anything for the fields the adapter emits, and
`_STATMECH_FIELDS_BY_TARGET`'s two entries are now provably identical because
the two statmech roots reached full 18/18 field parity in `c734499c` (#155).
The `target_model` plumbing should still be **kept**, not deleted — it is the
tripwire that keeps the two roots from silently re-diverging, and
`applied_energy_corrections` is still asymmetric — but it should be re-framed
in comments as a drift guard rather than as a live 422 avoider. The only
genuinely new behaviour the adapter provokes is a warn-tier
`freq_list_incomplete_for_geometry` on every transition state whose ARC record
carries no `statmech.harmonic_frequencies_cm1`; it rides on an accepted 201 and
the adapter discards response warnings, so it is invisible today. **Re-pin
target: `tckdb-schemas==0.33.0` / `tckdb-client==0.42.0`, directly, with no
code change required** — followed by the two optional cleanups in §7 and §8.

---

## 1. What actually changed on the wire

The published-model surface moved very little. The mechanical diff over 107
models found exactly **one added model**, **five removed fields**, **fourteen
added fields**, and **zero retypes of anything the adapter emits**.

### 1a. `literature_id` → `literature` (the only *removed* field in the range)

`CalculationPayload`, `CalculationWithResultsPayload`, `CalculationIn`,
`ComputedReactionCalculationIn` and `ConformerCalculationIn` all dropped the
raw-FK `literature_id: int | None` and gained an inline
`literature: LiteratureUploadRequest | None`.

- `schemas/python/tckdb-schemas/tckdb_schemas/fragments/calculation.py:116-127`
- `schemas/python/tckdb-schemas/tckdb_schemas/shared/calculation_in.py:96`
- Commits: `ed5d610c` (#172) converted `CalculationIn`; `d1de9d16` (#177)
  converted the five primitive routes.

All five roots are `extra="forbid"`, so a payload still carrying
`literature_id` would now 422 with `extra_forbidden`. **Not breaking for this
adapter**: `grep -n literature tckdb_arc/tckdb_arc/*.py` returns only prose —
`adapter.py:5381` explicitly records that `source_literature` is *always*
`None` because "the schema explicitly forbids synthesizing literature rows from
raw citation strings". The adapter has never emitted the field.

### 1b. `SCFStabilityPayload` split into content + citation

`SCFStabilityContent` is new (the only added model in the range); it is the old
`SCFStabilityPayload` minus `source_calculation_id` / `source_artifact_id`.
`SCFStabilityPayload` now subclasses it and re-adds the two FK ids for the
primitive routes only; bundle roots take the content form.

- `schemas/python/tckdb-schemas/tckdb_schemas/fragments/calculation.py:361-443`
- `schemas/python/tckdb-schemas/tckdb_schemas/shared/calculation_in.py:127`
- Commit `61f4b256` (#151).

The adapter emits no `scf_stability` block at all, so this is inert. It is
recorded because it is the shape a future ARC SCF-stability emission must take
on the bundle routes.

### 1c. Bundle-root field parity (the theme that retires the thermo-422 defect)

`BundleThermoIn` 8 → 14 fields, `BundleStatmechIn` 13 → 18 fields. Confirmed by
execution against 0.33.0:

```
ThermoInBundle(15) vs BundleThermoIn(14)
  only in ThermoInBundle: ['applied_energy_corrections']
  only in BundleThermoIn: []
StatmechInBundle(18) vs BundleStatmechIn(18)
  only in either: []
```

- `BundleThermoIn` gained `source_calculations`, `literature`,
  `software_release`, `workflow_tool_release`, `h298_uncertainty_kj_mol`,
  `s298_uncertainty_j_mol_k` — **all six in `61f4b256` (#151)**, "Refuse an
  electron with coordinates, and let both bundle roots record what produced
  their numbers". That is the commit that retired the thermo-422 defect at the
  source, and the answer to "confirm which commit did it".
  `schemas/.../workflows/computed_reaction_upload.py:284-345`
- `BundleStatmechIn` gained `rotational_constant_{a,b,c}_cm1`, `literature`,
  `software_release`, `workflow_tool_release` in **`c734499c` (#155)**.
  `schemas/.../workflows/computed_reaction_upload.py:433-513`
- `applied_energy_corrections` is the one remaining asymmetry, and it is
  deliberate: the reaction bundle declares corrections one level up on
  `BundleSpeciesIn`, and the model says so at
  `computed_reaction_upload.py:284-300`.

### 1d. Small additive fields

- `ConformerUploadRequest.conformer_key` — `75f1a114` (#171).
  `schemas/.../workflows/conformer_upload.py:239-255`.
- `ComputedReactionUploadResult.atom_map_id`, `.statmech_ids`, and
  `additionalProperties: false` on the result envelope — `ed5d610c` (#172).
  Response-side; the adapter reads only `id` from the response.
- `CalculationFreqModeSummary.imaginary_disposition`,
  `StatmechEvidenceSummary.sp_from_optimization` — read-side projections.
- Golden OpenAPI rename `StatmechSourceCalcIn` → `StatmechSourceCalculationIn`,
  which gained `existing_calculation_id` (`7089c466`, #157). Server-side
  standalone-statmech shape; the adapter does not use that route.

---

## 2. Drift table

Per `FIELD_KEY.md` §A5. `breaking_for_producer` = "would a payload the ARC
adapter builds **today** now be rejected or silently mean something different?"

| path | change | from | to | breaking_for_producer | commit | source |
|---|---|---|---|---|---|---|
| `calculation_payload.literature_id` | removed | `int \| None` | — | false (never emitted) | `d1de9d16` | `fragments/calculation.py:116` |
| `calculation_payload.literature` | added | — | `LiteratureUploadRequest \| None` | false | `d1de9d16` | `fragments/calculation.py:127` |
| `calculation_in.literature_id` | removed | `int \| None` | — | false (never emitted) | `ed5d610c` | `shared/calculation_in.py:96` |
| `calculation_in.literature` | added | — | `LiteratureUploadRequest \| None` | false | `ed5d610c` | `shared/calculation_in.py:96` |
| `computed_reaction_calculation_in.literature_id` | removed | `int \| None` | — | false (never emitted) | `ed5d610c` | `workflows/computed_reaction_upload.py:228` |
| `conformer_calculation_in.literature_id` | removed | `int \| None` | — | false (never emitted) | `d1de9d16` | `workflows/conformer_upload.py` |
| `calculation_in.scf_stability` | added | — | `SCFStabilityContent \| None` | false | `61f4b256` | `shared/calculation_in.py:127` |
| `calculation_in_bundle.scf_stability` | added | — | `SCFStabilityContent \| None` | false | `61f4b256` | `workflows/computed_species_upload.py:158` |
| `computed_reaction_calculation_in.scf_stability` | added | — | `SCFStabilityContent \| None` | false | `61f4b256` | `workflows/computed_reaction_upload.py` |
| `scf_stability_payload.source_calculation_id` | moved | on `SCFStabilityPayload` | subclass-only; absent from `SCFStabilityContent` | false | `61f4b256` | `fragments/calculation.py:361-443` |
| `scf_stability_payload.source_artifact_id` | moved | on `SCFStabilityPayload` | subclass-only; absent from `SCFStabilityContent` | false | `61f4b256` | `fragments/calculation.py:361-443` |
| `bundle_thermo_in.source_calculations` | added | — | `list[ThermoSourceCalcInBundle]` | false (**retires the thermo-422 defect**) | `61f4b256` | `workflows/computed_reaction_upload.py:318` |
| `bundle_thermo_in.literature` | added | — | `LiteratureUploadRequest \| None` | false | `61f4b256` | `workflows/computed_reaction_upload.py:305` |
| `bundle_thermo_in.software_release` | added | — | `SoftwareReleaseRef \| None` | false | `61f4b256` | `workflows/computed_reaction_upload.py:306` |
| `bundle_thermo_in.workflow_tool_release` | added | — | `WorkflowToolReleaseRef \| None` | false | `61f4b256` | `workflows/computed_reaction_upload.py:307` |
| `bundle_thermo_in.h298_uncertainty_kj_mol` | added | — | `float \| None`, `ge=0`, kJ/mol | false | `61f4b256` | `workflows/computed_reaction_upload.py:311` |
| `bundle_thermo_in.s298_uncertainty_j_mol_k` | added | — | `float \| None`, `ge=0`, J/(mol*K) | false | `61f4b256` | `workflows/computed_reaction_upload.py:312` |
| `bundle_statmech_in.rotational_constant_a_cm1` | added | — | `float \| None`, `gt=0`, cm^-1 | false | `c734499c` | `workflows/computed_reaction_upload.py:507` |
| `bundle_statmech_in.rotational_constant_b_cm1` | added | — | `float \| None`, `gt=0`, cm^-1 | false | `c734499c` | `workflows/computed_reaction_upload.py:508` |
| `bundle_statmech_in.rotational_constant_c_cm1` | added | — | `float \| None`, `gt=0`, cm^-1 | false | `c734499c` | `workflows/computed_reaction_upload.py:509` |
| `bundle_statmech_in.literature` | added | — | `LiteratureUploadRequest \| None` | false | `c734499c` | `workflows/computed_reaction_upload.py:499` |
| `bundle_statmech_in.software_release` | added | — | `SoftwareReleaseRef \| None` | false | `c734499c` | `workflows/computed_reaction_upload.py:500` |
| `bundle_statmech_in.workflow_tool_release` | added | — | `WorkflowToolReleaseRef \| None` | false | `c734499c` | `workflows/computed_reaction_upload.py:501` |
| `conformer_upload_request.conformer_key` | added | — | `str \| None`, `min_length=1` | false | `75f1a114` | `workflows/conformer_upload.py:239` |
| `computed_reaction_upload_result.atom_map_id` | added | — | `int \| None` (response) | false | `ed5d610c` | golden `openapi.json` |
| `computed_reaction_upload_result.statmech_ids` | added | — | `list[int]` (response) | false | `ed5d610c` | golden `openapi.json` |
| `computed_species_upload_request.note` | semantics_changed | undocumented | documented as **accepted but not persisted** | false (adapter emits no bundle note) | `75f1a114` | `workflows/computed_species_upload.py:664` |
| `computed_species_upload_request.workflow_tool_release` | semantics_changed | undocumented | documented as the **default** thermo/statmech inherit | false (adapter already emits it at bundle level) | `75f1a114` | `workflows/computed_species_upload.py:653` |
| `*.applied_energy_corrections[*].source_conformer_key` | semantics_changed | undeclared key silently dropped | undeclared key **refused** (`applied_energy_correction_source_key_undeclared`); TS-side refused outright | false (adapter emits no corrections) | `d2904a7d`, `61f4b256` | `energy_correction.py:169-184` |
| `statmech_source_calc_in` (server) | renamed | `StatmechSourceCalcIn` | `StatmechSourceCalculationIn` (+ `existing_calculation_id`) | false (standalone-statmech route only) | `7089c466` | golden `openapi.json` |

No row in this table is `breaking_for_producer: true`.

---

## 3. New rejection codes, and which the adapter can trigger

`clients/python/src/tckdb_client/rejection_codes.py` grew from **23 to 134
members; nothing was removed.** The jump is mostly a *catalogue scope change*,
not new refusals: `cb38bfc2` (#161) redefined the file from "codes the
scientific-check register declares" to "every 4xx code the API can return", and
`backend/app/api/code_catalogue.py` (new in this range) became the generator's
source. `REJECTION_STATUSES` was added to `__all__` and to
`tckdb_client/__init__.py`.

**The ARC adapter does not import `rejection_codes` at all.** It imports only
`TCKDBClient`, `tckdb_client.errors.TCKDBError` and `make_idempotency_key`
(`adapter.py:37-38`, `idempotency.py:14`), and it never branches on
`exc.code`. So no code in this file can break the adapter by existing; the only
question that matters is which new *refusals* it can provoke.

Of the 111 added codes, grouped by the module that mints them:

- **~85 are read-side and cannot be reached by an upload at all** — 18 from
  `services/release/curation.py`, 10 from `scientific_read/common.py`
  (pagination), 8 from `scientific_read/handles.py`, 4 each from
  `calculation_paths.py` and `ml_dataset.py`, and so on. `geometry_too_large`
  is `scientific_read/geometry.py`, i.e. a read cap, not a deposit limit.
- **9 are transport-level and pre-existed the range** — `idempotency_conflict`,
  `invalid_idempotency_key`, `unique_conflict`, `reference_conflict`,
  `state_conflict`, `rate_limit_exceeded`, and the three
  `tckdb_client_version_*` (426) codes. `backend/app/api/client_version.py`
  exists at `14c5e74f` and its behaviour is unchanged, so the client-version
  gate is **not** a new hazard.
- **3 `atom_map_*` codes** come from `fragments/reaction_atom_map.py`, which is
  untouched in the range — the validators pre-existed and were only *coded*.

That leaves the codes an ARC deposit can actually reach:

| code | trigger | new behaviour, or newly-named old behaviour? | ARC-reachable? |
|---|---|---|---|
| `freq_list_exceeds_geometry_degrees_of_freedom` | deposited frequency list longer than `3N` for the geometry the freq calc binds to | **NEW refusal** (`9c332e89`, #163; check added `080aea31`, #162) | **No in practice.** ARC's harmonic list is at most `3N-5`; the ceiling is `3N`. Reachable only by attaching a spectrum to the wrong geometry, which the adapter's `geometry_key`/`input_geometries` wiring prevents. |
| `freq_list_incomplete_for_geometry` (sibling, **warn** tier) | list shorter than `3N-6` | **NEW warning** (`080aea31`, #162) | **Yes — it fires today.** See §4. |
| `calculation_geometry_composition_mismatch` | a geometry linked to a calculation is not made of the atoms of the calculation's subject | **NEW refusal** (`aee41adb`, #181) | **No new exposure.** For a TS-owned calc the reference is the *sum of the reaction's reactants*, which is exactly what an ARC TS geometry is; `validate_transition_state_composition` already refused a TS whose saddle point disagreed with that sum, so #181 adds nothing the adapter could newly trip. For species-owned calcs the geometry is the same conformer xyz the pre-existing conformer rule already checked. |
| `statmech_torsion_scan_calculation_owner_mismatch` | a torsion's `source_scan_calculation_key` names a scan owned by another species | **NEW check on `/uploads/computed-reaction`** (`968f0a8d`, #174) — that route previously resolved the key against the bundle-global map with *no owner check at all* | **No.** `_build_slim_torsions` (`adapter.py:5842-5847`) rewrites the key through a per-species `scan_key_renames` map (`r0_scan_rotor_0` etc.), so a torsion always names its own species' scan. This is the single closest call in the range. |
| `statmech_source_role_type_mismatch` | `(calculation_key, role)` where the calc cannot play the role | **NEW on the computed-reaction bundle** (`d2904a7d`, #152 — its second commit explicitly covers "the fourth statmech write path, the one ARC deposits through"), relaxed by `e14209a3` (#156) to let an `opt` serve `role='sp'` | **No.** `_build_statmech_source_calculations` (`adapter.py:5910-5931`) emits only `opt→opt-key`, `freq→freq-key`, `sp→sp-key`. #152's author checked `backend/tests/fixtures/arc_runs/`: 44 freq→freq and 49 sp→sp links, no contradictions. |
| `statmech_calculation_key_undeclared` | a statmech source key naming nothing in the request | **NEW** (`d2904a7d`, #152) | **No.** The adapter registers a role key only inside the `try` that successfully built and appended that calculation (`adapter.py:1194`, `2361`), so a skipped freq/sp leaves no dangling reference. |
| `thermo_source_role_type_mismatch`, `thermo_source_calculation_owner_mismatch`, `thermo_statmech_owner_mismatch`, `statmech_source_calculation_owner_mismatch` | ownership / role violations | **Newly named, not newly refused.** `62a1d48e` (#178) consolidated five inline copies into `services/calculation_ownership.py`; the deposits were already refused, as bare `ValueError` → `validation_error` | Same reachability as before the range, i.e. not reachable by a correctly-namespaced ARC bundle. |
| `applied_energy_correction_source_key_undeclared`, `applied_energy_correction_source_calculation_owner_mismatch` | correction naming an undeclared or foreign key | `_source_key_undeclared` is a **NEW refusal** (previously the key was silently dropped) | **No.** The adapter emits no `applied_energy_corrections` on any route. |
| `freq_n_imag_disagrees_with_modes` | `n_imag` ≠ count of imaginary modes | **Not new.** The validator existed at 0.24.0 raising a bare `ValueError`; `db4bc37f` (#160) attached a code to the same raise | Reachable in principle, but `_freq_result_payload` reconciles `n_imag` against `modes` before emitting (`adapter.py:4537-4576`, `4641`), which is precisely why. |

**Summary: no new refusal in this range is reachable by an ARC payload.** One
new *warning* is (below). The three closest calls — #174 torsion-scan scoping,
#152 statmech role/type on the reaction route, and #181 calculation-geometry
composition — are each avoided by an existing adapter invariant rather than by
luck, and each is now worth naming in the adapter's own comments so a future
change cannot quietly remove the invariant.

---

## 4. What behaves differently for the adapter's uncommitted work

Beyond the one known test failure, the adapter's suite is clean and its payloads
still validate. Executed against 0.33.0 in `scratchpad/v33` (all six golden
payloads built offline and `model_validate`d, then
`stationary_point_findings()` collected — a check the adapter's own tests do
not perform):

```
species                  validated OK, no findings
reaction                 validated OK
  warn freq_list_incomplete_for_geometry flag=True
       transition_state.calculations['ts_freq'].freq_frequencies_cm1
reaction_irc             validated OK  (same finding)
p3_species_H2            validated OK, no findings
p3_species_H             validated OK, no findings
p3_reaction_H2+H<=>H+H2  validated OK  (same finding)
```

**The one live behavioural change.** Every transition state in the corpus trips
`freq_list_incomplete_for_geometry`. The arithmetic, instrumented on the real
payloads: TS geometry has 3 atoms → floor `3N-6 = 3`, ceiling `3N = 9`; the
deposited `freq_frequencies_cm1` has **1** entry (`[-1320.5]`, `[-900.0]`).
The cause is that the sanitized ARC records carry no `statmech` block on the
TS at all, so `_freq_result_payload` has no `statmech.harmonic_frequencies_cm1`
to draw real modes from and emits only the reinserted imaginary mode
(`adapter.py:4508-4561`). The adapter's own comment at `adapter.py:4522-4531`
already documents that this is the *common* ARC shape.

Severity, established by reading the consumption path rather than assuming:

- It is **warn**, not block — `frequency_completeness.py:254-278`. The upload is
  accepted with a 201.
- Its `structural_flag=True` does **not** reach
  `calc_freq_result.imaginary_mode_structural_flag`. That column is computed
  only from `transition_state_frequency_findings(...)` and only when a reaction
  coordinate is designated — `backend/app/services/calculation_resolution.py:726-743`.
  So an ARC TS is *not* being flagged out of default queries by this.
- It surfaces as an `UploadWarning` on the 201 body via
  `stationary_point_warnings(request.stationary_point_findings())` —
  `backend/app/api/routes/uploads.py:441,484,526,571,607,690`.
- **The adapter discards response warnings entirely** (no `warnings` handling
  anywhere in `adapter.py`), so today this is completely invisible.

That is a reporting gap rather than a break, and it is the one thing in this
range worth acting on beyond a version bump: an ARC run whose TS *does* carry a
statmech block deposits `3N-6` modes and clears the floor exactly, so the
warning cleanly distinguishes "full spectrum deposited" from "imaginary mode
only" — information the adapter currently throws away.

**Everything else in the uncommitted tree is unaffected**, verified item by
item:

- `_freq_result_payload`'s `is_ts` × `freq_n_imag` reconciliation, the
  `(75, 10000)` cm⁻¹ reaction-coordinate designation, and
  `imaginary_disposition: "unassigned"` — `ImaginaryModeDisposition`,
  `FrequencyModePayload`, `FreqResultPayload` and
  `evaluate_transition_state_frequency`'s designation/ambiguity/τ logic are
  **byte-identical apart from unicode→ASCII in message strings**
  (`2d63789f`/`0815d99a`/`0815d99a`, the non-ASCII gate commits). The
  `W_TS_REACTION_COORDINATE_AMBIGUOUS` block that `unassigned` exists to lift
  is unchanged at `stationary_point.py:821-847`.
- `_classify_statmech_treatment` / `_build_slim_torsions` — `StatmechTorsionInBundle`,
  `StatmechTorsionCoordinateIn`, `StatmechTreatmentKind`, `RigidRotorKind` all
  unchanged; `torsion_index` still `ge=1`, coordinate indices still 1-based.
  The only torsion-adjacent change is #174's server-side ownership scoping,
  covered above.
- `target_model`-aware thermo/statmech builders — still correct, now partly
  redundant (§1c, §7).
- ADAPT-0's kinetics unit omission, unified Hartree constant, and per-field
  index bases — no unit or index change anywhere (§5).
- Constraints, scan results, IRC, path-search, geometry payloads — the golden
  OpenAPI diff reports **zero** changes to `ConstraintIn`, `ScanResultPayload`,
  `IRCResultPayload`, `PathSearchResultPayload` or `GeometryPayload`.

---

## 5. Units and index bases — the highest-severity check

**No unit change and no index-base change anywhere in the range.** Checked
mechanically over the full golden OpenAPI at both revisions (a broader surface
than the published package alone):

- **373 unit-bearing fields** (`*_hartree`, `*_cm1`, `*_kj_mol`, `*_j_mol_k`,
  `*_k`, `*_bar`, `*_units`, `*_kcal_mol`, `*_ev`, `*_debye`, `*_amu`,
  `*_angstrom`, `*_hz`, `*_pa`, `*_atm`, and anything containing `unit`):
  5 added, **0 changed** — no constraint change, no description change. The 5
  additions are `BundleStatmechIn.rotational_constant_{a,b,c}_cm1` (cm^-1,
  `gt=0`), `BundleThermoIn.h298_uncertainty_kj_mol` (kJ/mol, `ge=0`) and
  `BundleThermoIn.s298_uncertainty_j_mol_k` (J/(mol*K), `ge=0`) — all matching
  the units of the fields they qualify.
- **All enum schemas: 0 changed.** Every unit enum (`EnergyUnits`,
  `KineticsAUnits`, `PressureUnits`, …) is identical at both revisions.
- **112 fields with `index` in the name: 0 bound changes.** The per-field bases
  the earlier audit established are intact and re-verified:
  `IRCPointPayload.point_index` `ge=0`, `IRCResultPayload.ts_point_index`
  `ge=0`, `PathSearchPointPayload.point_index` `ge=0`,
  `PathSearchResultPayload.selected_ts_point_index` `ge=0` — **0-based**;
  `CalculationScanPointCreate.point_index` `ge=1`,
  `StatmechTorsionIn(Bundle).torsion_index` `ge=1`, and every atom index —
  **1-based**.
- **Across every field of every component schema, `minimum` / `maximum` /
  `exclusiveMinimum` / `exclusiveMaximum` / `multipleOf` / `const` /
  `enum`: 0 changed.**

---

## 6. τ — current state, and the prior finding

### The headline: #179 changed no code

`c69b21bd` ("Collapse tau to one citable constant, and correct what was
unsupported", #179) touches **exactly one file**:
`docs/adr/0012-imaginary-modes-are-judged-by-magnitude-not-counted.md`,
+61/−13. Its own body says so: *"Schema/migration impact: none. No model,
schema, migration or code change; this is a decision record and its
amendments."*

So the claim that "τ is now a single constant, 100 cm-1, for every protocol" is
**a decision recorded in the ADR and not implemented in the shipped schema
package.** This is exactly the case the brief warned about — the subject is
descriptive and still a claim.

### What the code actually does at 0.33.0

`schemas/python/tckdb-schemas/tckdb_schemas/stationary_point.py` still carries
the full five-row table, unchanged from 0.24.0 apart from unicode→ASCII in
message strings:

| basis | τ (cm⁻¹) | line |
|---|---|---|
| `analytic_tight` | 15.0 | `stationary_point.py:129` |
| `analytic_default` | 30.0 | `stationary_point.py:131` |
| `finite_difference_gradient` | 50.0 | `stationary_point.py:133` |
| `finite_difference_energy` | 80.0 | `stationary_point.py:136` |
| `protocol_not_recorded` (**fallback**) | 50.0 | `stationary_point.py:140` |

Resolution: `resolve_tau_from_parameters` (`stationary_point.py:353-378`) reads
exactly three canonical `calculation_parameter` keys, declared at
`stationary_point.py:146-150`:

```python
TAU_PARAMETER_KEYS = ("freq.hessian_method", "grid.quality", "opt.convergence")
```

`resolve_tau` (`stationary_point.py:224-350`) then branches: unrecognised or
absent `freq.hessian_method` → 50.0 / `protocol_not_recorded`;
`finite_difference_energy` → 80.0; `finite_difference_gradient` → 50.0;
analytic **and** `grid.quality ∈ _TIGHT_GRID_VALUES` (`:155-168`) **and**
`opt.convergence ∈ {"tight","very_tight","verytight"}` (`:171`) → 15.0;
analytic otherwise → 30.0.

Separately, `TS_IMAGINARY_FREQUENCY_MIN_CM1 = 100.0` (`stationary_point.py:121`)
is unchanged and is **not** τ — it is the soft-reaction-coordinate expectation,
and its own comment says "Unlike `resolve_tau`, this one really is fixed in
code". The coincidence that #179 chose 100 cm⁻¹ for the withdrawn τ table makes
this easy to misread; they are different constants for different judgements.

### The prior finding: **confirmed, with one refinement**

`structural_flag=True` is set on exactly two τ-related findings, both in
`evaluate_transition_state_frequency`:

- `W_TS_EXTRA_IMAGINARY_MODE_ABOVE_TAU` — `stationary_point.py:849-858`
  (the earlier audit cited `850-858` at the older revision; the ASCII-ification
  commits shifted it by one line, the logic is identical):
  ```python
  above_tau = [m for m in extras if m.magnitude_cm1 >= resolved_tau.tau_cm1]
  if above_tau:
      findings.append(StationaryPointFinding(tier=warn, ..., structural_flag=True, ...))
  ```
- `W_TS_EXTRA_IMAGINARY_MODES_NOT_ASSESSABLE` — `stationary_point.py:785-805`,
  when `n_imag > 1` but no frequency list was deposited.

The `elif extras:` branch, `W_TS_EXTRA_IMAGINARY_MODES_BELOW_TAU`
(`stationary_point.py:877-896`), carries **no** `structural_flag` and says so in
its own message: *"The record is accepted without a structural flag."*

So the prior audit's correction stands: **a defaulted τ does not flag a TS
record.** The flag fires only on an *extra imaginary mode at or above τ*, and
because the 50 cm⁻¹ default sits above both analytic rows, supplying real
provenance that resolves to `analytic_tight` (15) or `analytic_default` (30)
lowers the bar and flags **more** records, not fewer.

The refinement, which the prior finding did not state: this is only true for
the *analytic* branches. Recording `freq.hessian_method` such that it resolves
to `finite_difference_energy` raises τ to 80 cm⁻¹, i.e. **above** the 50 cm⁻¹
default, and flags *fewer* records. "The default is the permissive end" is
therefore true relative to the analytic rows and false relative to the
FD-energy row. The safe statement is: **the 50 cm⁻¹ default is the middle of a
15–80 cm⁻¹ range, and supplying provenance moves the flag rate in whichever
direction the recorded protocol implies.**

None of this is reachable by an ARC deposit today in any case: `structural_flag`
is only computed when a reaction coordinate is designated
(`calculation_resolution.py:726-731`), which the adapter does only for
`n_imag > 1`, and the extras it designates are all marked `unassigned` — which
lifts the ambiguity *block* but, as `adapter.py:4630-4632` correctly notes, does
not lift the above-τ flag.

**"Correct what was unsupported" changes none of this.** The four corrections in
#179 (free-rotor upper-limit claim, the 5.45/3.72 referencing error, the
"2–15 kJ/mol → factor of 55" arithmetic, and the hindered-rotor curvature-sign
claim) are all statements in the ADR's prose about partition functions and rate
factors. None of them is implemented anywhere in the schema package, and none
alters the flag condition.

---

## 7. The now-failing regression demo test

`tckdb_arc/tests/test_shared_builder_field_sets.py::TestWrongTargetModelReproducesTheOriginalBug::test_species_shaped_thermo_block_rejected_by_reaction_root`
feeds `_build_thermo_block(..., target_model="ThermoInBundle")` — which
legitimately emits `source_calculations` — into `BundleThermoIn` and asserts an
`extra_forbidden` `ValidationError`. Since `61f4b256` (#151), `BundleThermoIn`
accepts `source_calculations`, so no error is raised and the test fails at
`test_shared_builder_field_sets.py:305`.

**Recommendation: retire the test, and replace it with an asymmetry tripwire —
do not repoint it at `applied_energy_corrections`.**

Reasoning:

- Repointing at `applied_energy_corrections` *would* still raise
  `extra_forbidden` (it is the one remaining asymmetry, §1c), but it would no
  longer be a *regression demo*. The defect it demonstrated was "the shared
  builder emits a field the reaction root rejects". `_build_thermo_block` does
  not and cannot emit `applied_energy_corrections` — it is not in
  `_THERMO_FIELDS_BY_TARGET` for either target and the builder has no code path
  producing it. The repointed test would have to hand-construct a dict the
  adapter never builds, which converts a demonstration of a real producer bug
  into a demonstration of Pydantic's `extra="forbid"`. That is a worse test
  wearing the same name.
- The defect class is already guarded, better, by the subset assertions in the
  same file — `_THERMO_FIELDS_BY_TARGET[t] ⊆ <Model>.model_fields` — which fail
  loudly if the builder ever emits a field its target root lacks, for *any*
  field, without needing a hand-built fixture.
- What is genuinely worth keeping is a tripwire on the *asymmetry itself*, so
  that a future TCKDB change re-diverging the two roots is caught rather than
  discovered in production. Concretely, replace the test body with an assertion
  that `applied_energy_corrections ∈ ThermoInBundle.model_fields` and
  `∉ BundleThermoIn.model_fields`, plus a matching assertion that the two
  statmech roots' field sets are now *equal* — with a comment naming `61f4b256`
  (#151) and `c734499c` (#155) as the commits that closed the gap, and stating
  that the `target_model` parameter is retained as a drift guard rather than
  because a 422 currently depends on it.

Both `_THERMO_FIELDS_BY_TARGET` and `_STATMECH_FIELDS_BY_TARGET` should keep
their `target_model` gating. The comments above them
(`adapter.py:4933-4949` and `adapter.py:5479-5499`) are now factually wrong,
however: `adapter.py:4941-4943` states `BundleThermoIn` "has no
`source_calculations` column at all", and `adapter.py:5486-5488` lists
`literature` / `rotational_constant_{a,b,c}_cm1` / `software_release` /
`workflow_tool_release` as "species-only". Both statements were true at 0.24.0
and are false at 0.33.0. Correcting the prose is the only source change this
re-pin actually needs.

---

## 8. Client 0.35.0 → 0.42.0

Seven minor versions, and the changed-file set in `clients/python/src/` is:

```
__init__.py            builders/calculation.py   builders/sources.py
builders/statmech.py   builders/uploads.py       rejection_codes.py
```

`client.py`, `errors.py`, `idempotency.py` and the endpoint table are
**untouched**. Therefore:

- **Transport, retry, idempotency, error surfacing: no change.** Signatures
  verified by introspection at 0.42.0 and by reading the 0.35.0 source —
  `TCKDBClient.__init__(base_url, api_key=None, timeout=30.0, *, transport=None,
  retry=None)`, `upload(target, payload=<sentinel>, *, idempotency_key=None,
  warn_on_dropped_fields=False)`, `post_json(path, payload, *,
  idempotency_key=None)`, `request_json(method, path, *, json=None, params=None,
  authenticated=True, idempotency_key=None, extra_headers=None)`,
  `upload_artifacts(plan, *, idempotency_key_prefix=None,
  batch_by_calculation=False)`, `make_idempotency_key(*parts)`,
  `validate_idempotency_key(key)` — all identical.
- **`UPLOAD_ENDPOINTS` unchanged**: the same 11 keys (`computed_reaction`,
  `computed_species`, `conformer`, `kinetics`, `network`, `network_pdep`,
  `reaction`, `statmech`, `thermo`, `transition_state`, `transport`).
- **No new hard dependency.** `clients/python/pyproject.toml` diff in the range
  is the version string and nothing else.
- **Additive export**: `REJECTION_STATUSES` added to
  `tckdb_client/__init__.py` and `__all__`. It maps each `RejectionCode` to the
  frozenset of HTTP statuses it arrives at, which is the retry advice (422 →
  nothing written, resend corrected; 409 → the write reached the DB; 426 →
  upgrade; 429 → retry unchanged). The adapter does not use it.
- The remaining diffs are unicode→ASCII in message strings (`0815d99a` #166,
  `2d63789f` #169) and docstring corrections in `builders/statmech.py`.

**One client defect found, not affecting this adapter.**
`builders/uploads.py:1244` still passes `allow_source_calculations=False` when
emitting the computed-reaction thermo block, and `uploads.py:1466-1485` still
raises a `warning` diagnostic reading *"the computed-reaction `BundleThermoIn`
schema does not carry that field — the references will not be emitted on the
wire"*. That statement has been false since `61f4b256` (#151): `BundleThermoIn`
carries `source_calculations`, so **`tckdb-client 0.42.0`'s builder silently
drops thermo provenance that the server would now accept**, and tells the
producer to use `/uploads/computed-species` instead. The ARC adapter constructs
raw dicts and calls `client.request_json` / `client.upload_artifacts` directly —
it imports none of the builder classes — so it is unaffected. Worth reporting
upstream.

---

## 9. What I could not establish

- **Live-server confirmation.** Everything here is from source reading and from
  executing the published packages offline. `calculation_geometry_composition`,
  the ownership guards and the role/type checks are workflow-layer code that
  needs a database session; I could not run them, so their ARC-reachability
  assessments rest on reading the resolution paths plus the fixture evidence
  the commits themselves cite (#152: 44 freq→freq and 49 sp→sp ARC links, no
  contradictions; #143: all 31 calculation geometries in the repo's ARC-derived
  fixtures are TS-owned and are the whole reacting system). A staging deposit of
  one real ARC reaction bundle would convert those from well-supported
  inferences into observations.
- **Whether real ARC runs commonly omit the TS `statmech` block.** The
  conclusion in §4 that the new completeness warning fires broadly rests on the
  sanitized `phase3_output.yml` corpus (TS0 has no `statmech` key at all) plus
  the adapter's own comment at `adapter.py:4522-4531`. I did not survey a
  population of real ARC `output.yml` files, so I cannot state what fraction of
  production deposits will carry the warning.
- **Whether `freq_list_incomplete_for_geometry` has any read-time consequence.**
  I traced it to an `UploadWarning` on the 201 and confirmed it does *not* reach
  `calc_freq_result.imaginary_mode_structural_flag`. I did not trace whether any
  trust rubric or default-query filter consumes the warning row itself; if
  upload warnings are persisted and graded elsewhere, the practical severity
  would be higher than §4 states.
- **The remaining ~85 read-side rejection codes.** I classified them by the
  module that mints them and by whether an upload route can reach that module.
  I did not individually verify each one's trigger condition, because none is
  reachable from a deposit and the adapter performs no reads.
- **`backend/app/schemas/workflows/` internals.** `statmech_upload.py` (+139),
  `network_pdep_upload.py` (+51), `kinetics_upload.py` (+39),
  `thermo_upload.py` (+23) and `stationary_point_seam.py` (+14) changed. Their
  wire effect is fully captured by the golden-OpenAPI diff in §1 and §2, which
  is the authoritative server-side request contract, so I did not read them
  line-by-line. They govern the standalone `/uploads/statmech`,
  `/uploads/thermo`, `/uploads/kinetics` and `/uploads/networks/pdep` routes,
  none of which this adapter uses.
