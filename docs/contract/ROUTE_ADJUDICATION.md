# B2 — route adjudication

Companion narrative to `ROUTE_ADJUDICATION.yml` (222 rows: 208 sub-model concepts
covering all 529 `ROUTE_UNRESOLVED` rows, plus the 14 real `ORPHAN_MAPPING` rows).

The join could not decide these because TCKDB reuses sub-models under many parents.
A1 enumerated every route to each sub-model; A2/A3/A4 recorded each concept once, at
one representative route. Every row here answers one question: **does the supply at
the sibling route actually cover this route, or is it a different scientific object?**

## Verdict summary

| Verdict | Concepts | Demand rows | Of which do not generalise |
|---|---:|---:|---:|
| `GENERALISES` | 145 | 292 | 0 |
| `PARTIAL` | 36 | 196 | 95 (enumerated in `exceptions`) |
| `DIFFERENT_INSTANCE` | 27 | 41 | 41 |
| `UNDECIDABLE` | 0 | 0 | — |
| **subtotal** | **208** | **529** | **136** |
| `A1_MISS` (orphan) | 12 | 12 | — |
| `NEVER_EXISTED` (orphan) | 2 | 2 | — |

Net effect on the matrix: of the 529 `ROUTE_UNRESOLVED` rows, **393 should be
re-verdicted to match their sibling** (i.e. no new gap) and **136 are genuinely
unsupplied and should become `ARC_ABSENT`**. Those 136 break down as: 52
`frequency_scale_factor`, 48 transition-state `scan_result`, 30
`execution_environment`, 4 analysis-`software_release`, 2 `imaginary_disposition`.

Nothing was left undecided. But that 136/393 split rests on one judgement call
covering a *different* 136 rows (§1.5, and §6 for why I hold it weakly): if a reviewer
disagrees with it, the absent count more than doubles. Read §1.5 and §6 before acting
on the table above.

## Citation convention

`evidence` is repo-relative to `tckdb-adapters` (this repo) by default. Citations in
the other two repos are prefixed: `ARC:` for
`ARC.worktrees/feature_arc_result_export_contract`, `TCKDB:` for `TCKDB_v2`. TCKDB
model citations inside `rationale` prose are relative to
`schemas/python/tckdb-schemas/tckdb_schemas/`.

---

## 1. The families that generalise cleanly

### 1.1 Calculation-route mirroring — 171 concepts, 372 rows, the bulk of the work

(123 of these concepts come out `GENERALISES` outright; 24 are `PARTIAL` and 24
`DIFFERENT_INSTANCE` — almost all of those 48 are the transition-state rotor-scan
carve-out of §2.4, which this same analysis is what exposed, plus
`freq_result.modes[].imaginary_disposition` from §2.5.)

TCKDB reaches a calculation from six routes: `primary_calculation` and
`additional_calculations[]` in the species bundle, and `species[].calculations[]`,
`species[].conformers[].calculation`, `transition_state.calculation`,
`transition_state.calculations[]` in the reaction bundle. A1 correctly enumerated all
six; the supply inventories keyed each calc sub-block once.

Two independent facts make this family generalise, and both had to hold:

1. **TCKDB reaches the same classes from all six routes.** The two calculation
   models differ (`CalculationInBundle`, `computed_species_upload.py:121` vs
   `ComputedReactionCalculationIn`, `computed_reaction_upload.py:92`) — but the
   *sub-blocks* in question are shared imports. `irc_result`, `path_search_result`,
   `scan_result`, `hessian`, `spin_diagnostic`, `wavefunction_diagnostic`,
   `constraints`, `parameters`, `input_geometries`, `output_geometries`,
   `level_of_theory` and the top-level `software_release` / `workflow_tool_release`
   are declared on both, with identical element classes.
2. **The adapter has one parent-agnostic builder.** `_build_calc_in_bundle`
   (`adapter.py:1347`) wrapping `_calculation_payload` (`adapter.py:3196`) is called
   at `adapter.py:1093/1120/1143/1186` (species bundle), `:2254/2281/2310/2362`
   (reaction species block) and `:2559/2601/2630/2652/2684` (TS block). It takes the
   ARC record and a calc role and knows nothing about the parent.

The obvious counter-argument — A1's warning that the reaction root is *flat* where
the species root is *typed* — turns out not to bite here, and I checked it rather
than assuming. A4 documents that `_flatten_all_reaction_calcs` (`adapter.py:5453`)
unwraps **only** `opt_result` / `freq_result` / `sp_result`; `scan_result`,
`irc_result`, `path_search_result`, `hessian` and `spin_diagnostic` stay nested. And
A1 enumerated the reaction root's flat scalars as their own paths, so no
`freq_result.*` concept in this set even has a reaction route to worry about. The
asymmetry is real and dangerous, but it does not touch these 372 rows.

### 1.2 The species mirror — A2 said so, and the adapter agrees

TCKDB mirrors its per-species shape twice, and A2 keyed every per-species leaf under
`species_upload.*` only, stating in its narrative that "the identical ARC value
populates the `reaction_upload.species[].*` mirror" and that Phase B must apply the
routing rule rather than join naively. I did not take that on the agent's word: the
adapter consumes the same `output.yml` `species[]` record in
`_build_computed_species_payload` and `_build_reaction_species_block`
(`adapter.py:2205`) and calls the same block builders. `GENERALISES`.

The bound on the family, which does not affect any row here but constrains Phase C:
`BundleStatmechIn` is a strict subset of `StatmechInBundle` (it lacks `literature`,
`software_release`, `workflow_tool_release` and the three rotational constants), and
`BundleThermoIn` is a 7-field subset of `ThermoInBundle`. Under
`extra="forbid"` (`common.py:13-16`) the extra fields are 422s, not no-ops — see §4.

### 1.3 `applied_energy_corrections[]`, everything except the two traps

All four AEC sites resolve to the identical `AppliedEnergyCorrectionInBundle`
(`computed_species_upload.py:315`, an empty-bodied subclass of
`AppliedEnergyCorrectionUploadPayload`, `energy_correction.py:165`), and the adapter
has one builder, `_build_applied_energy_corrections` (`adapter.py:4469`). Its callers
differ only in which SP-key namespace they resolve (bundle-global `sp` vs scoped
`r0_sp` / `p0_sp` / `ts_sp`). `GENERALISES`.

### 1.4 `geometry.isotopes` — 30 rows, and the most surprising result in the set

This was the single largest concept and I expected to split it: the conformer geometry
comes from the species record, while `hessian.geometry`, `irc_result.points[].geometry`,
`scan_result.points[].geometry` and `path_search_result.points[].geometry` come from
re-parsed ESS logs, and my working hypothesis was that log-derived geometries carry
only symbol-derived natural-abundance isotopes and therefore differ scientifically
from a species geometry that records a real substitution.

**The hypothesis was half right, and the half that failed changes the verdict.** All
five sources are default-only, *including the conformer geometry*. `final_xyz` is
never the user's declaration — it is overwritten from a parsed log at
`ARC:arc/scheduler.py:2487/2611/2614/3538`, and every parse builds the dict through
`str_to_xyz`'s defaults branch (`ARC:arc/species/converter.py:112`) or
`xyz_from_data(isotopes=None)` (`:447`). Independently, `xyz_to_str` emits an isotope
label only when `isotope_format` is passed, and **no production ARC caller ever passes
it** — so every exported xyz string is bare symbols regardless. A user-declared
substitution survives only into `initial_xyz` (`converter.py:671`), and the first
successful optimisation destroys it. (It also means the QM job itself never sees the
substitution: `ARC:arc/job/adapters/gaussian.py:252` writes input decks without
isotope labels.)

So the supply status is identical at all 30 routes *and* at the sibling: `GENERALISES`,
and re-verdicting 30 rows as `ARC_ABSENT` would have manufactured 30 gaps out of one.

But the row carries a correction Phase C must act on: **A3's `export_effort: trivial`
on this concept is wrong.** A3 cites `ARCSpecies.final_xyz['isotopes']` as a one-line
export. That source exists but is not valid — exporting it would assert natural
abundance as a determined fact rather than record a substitution, which is worse than
the current silence. The real fix is a new field sourced from the species definition,
before the opt overwrites it. Effort is `hard`, and the ARC-side plumbing is a
prerequisite.

### 1.5 Vacuous demand: species-conformer `irc_result` / `path_search_result`

136 rows — 34 leaves across four species calculation routes — asked whether an IRC
result or a double-ended path-search result on a *species conformer's* calculation is
supplied. The adapter invokes `_build_irc_result_payload` only at `adapter.py:2729` and
`_build_path_search_result_payload` only at `adapter.py:2542`, both inside
`_build_ts_block`. So on the code, these routes are never populated.

I ruled `GENERALISES` anyway, and told Phase C not to re-verdict them absent. An IRC or
a double-ended path search on a non-saddle conformer is not an object ARC, or anyone
else, produces; TCKDB exposes the field there only because its calculation model is
uniform across calculation types. Calling it a gap would invent 136 uplift targets for
science that does not exist.

**This is a judgement about vacuous demand, not a code finding**, and it is the one
place in this file where I have overridden what the call graph literally says. §6
states the conditions under which it should be flipped.

---

## 2. The traps

### 2.1 `applied_energy_corrections[].frequency_scale_factor.*` — 52 rows

The brief named this one as the archetype and it holds up under three independent
checks:

- The string `frequency_scale_factor` **does not occur anywhere in `tckdb_arc`**. The
  adapter never emits the block. (`statmech.freq_scale_factor` is a different field
  and *is* emitted, at `adapter.py:4889`.)
- ARC exports a scale factor only at run level — `freq_scale_factor` /
  `freq_scale_factor_source`, `ARC:arc/output.py:134-137` — and never attaches one to
  a correction record. `_build_energy_corrections_for_species`
  (`ARC:arc/output.py:847-929`) emits no scale field, and
  `output_yml_schema.json`'s `$defs/energy_correction` is `additionalProperties: false`,
  so a producer cannot add one without a schema bump.
- TCKDB additionally requires `source_calculation_key` alongside an FSF-mode
  correction (`energy_correction.py:253-263`), which the adapter supplies only in
  scheme mode.

`DIFFERENT_INSTANCE` across the board. These 52 rows are real, currently-hidden gaps.

### 2.2 `execution_environment.*` — 30 rows

The join matched `execution_environment.software_release.version` against the
calculation's own top-level `software_release.version`, which is supplied. They are
not the same object and they are not even the same class.
`ExecutionEnvironmentManifestPayload` (`fragments/execution_environment.py:253`)
nests `ScientificSoftwareReleaseIdentity` (`:208`) and `WorkflowToolReleaseIdentity`
(`:234`) — narrower classes than `SoftwareReleaseRef` / `WorkflowToolReleaseRef` — and
the manifest additionally *requires* `schema_version`, a discriminated `runtime`
(container / conda / HPC-module / described) and an `executable` reference. Under
`extra="forbid"` a `SoftwareReleaseRef`-shaped dict dropped in there is a 422, so
this is not even a copy-paste away.

The string `execution_environment` does not occur anywhere in `tckdb_arc`, and A2
records the whole execution environment as structurally absent from ARC's export.
`DIFFERENT_INSTANCE`.

### 2.3 `statmech.software_release` / `thermo.software_release` — 4 rows

Matched against a calculation's ESS release. These name the **analysis** software that
produced the statmech/thermo block (Arkane), not the ESS that ran a job. ARC exports
only `arkane_git_commit` for that, with no version string
(`_arc_analysis_software_release`, `adapter.py:4858`). `DIFFERENT_INSTANCE`.

### 2.4 Transition-state rotor scans — 48 rows, a gap I did not expect to find

`scan_result.*` looked like plain calculation-route mirroring until I traced the call
sites. `_build_ts_block` never constructs a scan calculation — its only
`_build_calc_in_bundle` calls are `ts_guess` (`adapter.py:2559`), `opt` (`:2601`),
`freq` (`:2630`), `sp` (`:2652`) and `irc` (`:2684`) — and ARC's `transition_states[]`
record carries no `rotor_scans`. The species-side supply says nothing about a saddle
point's hindered rotors.

This matters scientifically: hindered internal rotation in a transition state is a
first-order term in the rate constant, TCKDB has a slot for it, and nothing currently
reports that ARC cannot fill it. 23 concepts whose only route is the TS are
`DIFFERENT_INSTANCE` (23 rows); the 25 `scan_result.*` concepts that also have species
routes are `PARTIAL`, with the TS leg as the exception (25 rows). 48 rows in total.

### 2.5 `freq_result.modes[].imaginary_disposition` — a sibling that should never have matched

Two failures compounded. A3 keyed this at
`reaction_upload.transition_state.calculations[].freq_result.modes[].imaginary_disposition`,
a path A1 does not have — the reaction-root calculation is flat and exposes
`freq_imaginary_dispositions`, not `freq_result.modes[]` — so the row surfaced as
`SURPLUS` and the tail-matcher then used it as a sibling anyway. And the latent
capability A3 cites, `ARC:arc/checks/ts.py:285`, assesses imaginary modes on a
**transition state**, while the two demand routes are a species conformer's
calculations, i.e. a minimum. ARC has no code assigning a disposition to a spurious
imaginary mode on a minimum. `DIFFERENT_INSTANCE`.

This is worth flagging to whoever owns the join: `resolve_route` will happily pick a
`SURPLUS` path as a sibling, which means a mis-keyed supply row can silently
suppress a real gap.

---

## 3. The one that looked like a trap and is not

`applied_energy_corrections[].scheme.level_of_theory.*` (39 rows) reads exactly like
the `frequency_scale_factor` case: it is the level of theory at which a *correction
scheme's parameters* were fitted, which is a different object from a calculation's own
level of theory. I nearly ruled it `DIFFERENT_INSTANCE` on that reasoning.

It is supplied, by a producer that is parent-agnostic on both sides:

- ARC writes the scheme's level with **the same helper** that writes `opt_level` /
  `freq_level` / `sp_level` — `_level_to_dict` (`ARC:arc/output.py:262`), invoked for
  the run-level `arkane_level_of_theory` at `:133` and embedded per correction record
  as `energy_corrections[].level_of_theory` at `:878` and `:913`. It carries the full
  leaf set (`Level.as_dict()`, `ARC:arc/level.py:182-192`), and the JSON schema makes
  the per-entry `level_of_theory` a **required** `$ref` to the same `level_dict` `$def`.
- The adapter projects it with the same projector the calculation LoT uses:
  `_scheme_level_of_theory` (`adapter.py:4461`) delegating to `_arc_level_to_tckdb_lot`
  (`adapter.py:4438`).

A2 inventoried only the `.method` leaf and said so explicitly in that row's
`fidelity_note` ("Full level_dict leaf mapping applies"), which is exactly why the
other leaves fell through the join. `GENERALISES`.

One caveat to carry: `Level.as_dict()` omits `None`-valued keys rather than nulling
them, so the key set varies run to run and per-leaf availability is `conditional`,
never `always`.

The seven `level_of_theory.<leaf>` concepts therefore come out `PARTIAL` — scheme legs
generalise, `frequency_scale_factor` legs do not — with the four FSF paths enumerated
in `exceptions` on each.

### `level_of_theory.spin_treatment` — read the rationale, not just the verdict

This one is `PARTIAL` for consistency with its siblings, but the verdict is close to
meaningless on its own and the row says so. ARC's `Level` has no spin-treatment
attribute, `_level_to_dict` cannot emit one, and A2's row has `arc_key: null` /
`availability: rare`. All 15 routes are equally unsupplied. The sibling's own matrix
verdict of `ADAPTER_GAP` ("ARC exports it, adapter drops it") is simply wrong for a
null-supply row. Phase C should treat all 15 as **one** `ARC_ABSENT` fact, not as
fifteen gaps and not as eleven covered rows.

---

## 4. The 14 real orphan mappings

### 12 × `applied_energy_corrections[].scheme.{atom_params,bond_params}[]` → `A1_MISS`

The adapter is correct as written and needs no change. `atom_params` and `bond_params`
are live fields on `EnergyCorrectionSchemeRef` at 0.22.0 (`energy_correction.py:52`
and `:53`), with `element`/`value` at `:88-89` and `bond_key`/`value` at `:93-94`.
They were neither renamed nor removed: `energy_correction.py` is **byte-identical**
between tag `tckdb-schemas-v0.8.0` and HEAD — the diff over that path is empty and the
log over that range returns zero commits. The only commit that ever touched these
names under the package path is `9fde2742` ("extract tckdb-schemas package (PR 1)"),
which merely carved the file out of the backend.

I had hypothesised a consolidation into `component_params`, and that is refuted:
`component_params` (`:54`) coexists with both and is the Melius-BAC-specific table.
Its `component_kind` is a closed four-member enum (`atom_corr`, `bond_corr_length`,
`bond_corr_neighbor`, `mol_corr`, `enums.py:376-380`) and it carries its own separate
`key: str` field, so it is not a superset spelling of an element symbol or a bond key.

Action: add the four leaves to `TCKDB_DEMAND.yml` under all three parents. Twelve
false orphans disappear.

### 2 × `reaction_upload.species[].thermo.source_calculations[].{calculation_key,role}` → `NEVER_EXISTED`

**None of the three offered verdicts fits, so I coined a fourth rather than pick a
wrong one.** The field was not renamed, not removed, and A1 did not miss it — it has
never existed on this model.

`ComputedReactionUploadRequest.species[].thermo` is `BundleThermoIn`
(`computed_reaction_upload.py:277`), whose complete field set is seven leaves:
`scientific_origin` (290), `h298_kj_mol` (291), `s298_j_mol_k` (292), `tmin_k` (293),
`tmax_k` (294), `nasa` (295), `points` (296), `note` (297). The class body was checked
at **all 19 commits** that ever touched the file, across its three historical paths,
back to `4e514ecc` ("Large update") which introduced the model: the
`source_calculations` count is 0 at every revision. There is no removal SHA to cite.

The species-root counterpart `ThermoInBundle` (`computed_species_upload.py:325`) *does*
declare `source_calculations` (`:348`) of `ThermoSourceCalcInBundle` (`:303`), where
`calculation_key` (310) and `role` (311) live. The adapter is writing the species-root
thermo shape into the reaction-root slot (`adapter.py:4620`, actor-scoped by the caller
at `:2401`).

**This is the one genuine latent upload failure in the set, and it is a hard 422, not a
silently ignored key.** Every model inherits `SchemaBase = ConfigDict(extra="forbid")`
(`common.py:13-16`), so any computed-reaction upload carrying per-species thermo is
rejected outright. There is no thermo→calculation link mechanism on the reaction bundle
to migrate to — and the absence looks deliberate rather than accidental, since the
sibling links `BundleStatmechIn.source_calculations` (`:401`) and
`BundleKineticsIn.source_calculations` (`:803`) do exist. The adapter must drop the key
on that path.

Note the blast radius is bounded by A4's own coverage report: the golden corpus has no
`thermo` key on any record, so no checked-in fixture exercises this path. It fails in
production, not in CI.

---

## 5. What Phase C should do with this file

1. **393 rows** re-verdict to their sibling's verdict — no new gap. These are the 292
   `GENERALISES` rows plus the 101 non-exception legs of the `PARTIAL` concepts.
2. **136 rows** become `ARC_ABSENT`: 52 `frequency_scale_factor`, 48 transition-state
   `scan_result` (23 TS-only concepts + 25 TS legs of `PARTIAL` concepts), 30
   `execution_environment`, 4 analysis-`software_release`, 2 `imaginary_disposition`.
   Every one of them is either an `exceptions` entry on a `PARTIAL` row or the whole
   of a `DIFFERENT_INSTANCE` row, so the list is machine-derivable from the YAML.
3. **Do not** re-verdict `geometry.isotopes` (30 rows) as absent — but **do** correct
   A3's `export_effort` on the sibling from `trivial` to `hard`.
4. **Do not** re-verdict the species-conformer `irc_result` / `path_search_result`
   routes as absent — see §6, this is my main judgement call.
5. Add the 4 AEC scheme param leaves to A1 under 3 parents (12 orphans vanish).
6. Open a defect for the `BundleThermoIn` 422.
7. Fix the mis-routing found along the way: the adapter writes scan constraints to the
   calc's **top-level** `constraints` (`adapter.py:1461-1463`) rather than into
   `scan_result.constraints`. TCKDB validates `constraint_index` uniqueness across the
   union of both lists within one calc (`computed_species_upload.py:232-257`), so this
   is silent today and collides the moment a calc carries both.

---

## 6. Confidence

**High confidence** (traced to code in all three repos, and the counter-hypothesis was
tested rather than assumed):

- The 372-row calculation-route mirroring family. Both legs verified: TCKDB class
  identity across the six routes, and the single adapter builder with all eleven call
  sites enumerated.
- `frequency_scale_factor` (52 rows). Three independent confirmations, one of them a
  closed JSON schema that forbids the field existing.
- `execution_environment` (30 rows). Class mismatch plus three required fields ARC has
  no analogue for.
- `geometry.isotopes` (30 rows). The `xyz_to_str` / `isotope_format` finding is
  decisive and repo-wide.
- The AEC scheme LoT (39 rows). Same producer helper on the ARC side, same projector on
  the adapter side, and A2 said as much in the row I nearly overrode.
- All 14 orphans. Both groups were settled by exhaustive git archaeology (byte-identical
  file for group 1; class body checked at all 19 revisions for group 2), not by reading
  HEAD and inferring.

**Medium confidence:**

- **TS rotor scans (48 rows).** I am confident the adapter never builds a TS scan
  calc and that ARC's TS record carries no `rotor_scans` — that is grep-solid. What I
  did not establish is whether ARC *can* run a rotor scan on a TS and simply does not
  export it, which would make these `ARC_LATENT` rather than `ARC_ABSENT`. Settling it
  needs a read of ARC's scheduler rotor path for `is_ts=True` species. The verdict
  (does not generalise) is unaffected either way; only the re-verdict label moves.
- **`reaction_upload.software_release.version`.** I ruled it generalising from the
  calc-level ESS release via `ess_versions`. But a bundle-level field forces the
  producer to pick one release when a run used several ESSs, and the adapter emits no
  bundle-level `software_release` at all today. The datum exists; the selection rule
  does not.

**Low confidence — the decisions most worth a second reader:**

- **The species-conformer `irc_result` / `path_search_result` routes (136 rows: 34
  leaves × 4 species calculation routes — `species_upload.conformers[].primary_calculation`,
  `…additional_calculations[]`, `reaction_upload.species[].calculations[]`,
  `reaction_upload.species[].conformers[].calculation`).** I
  ruled `GENERALISES` and explicitly told Phase C *not* to re-verdict them absent. The
  reasoning: the adapter invokes these builders only on the TS path
  (`adapter.py:2729`, `:2542`), so the species routes are never populated — but an IRC
  or a double-ended path search on a non-saddle conformer is not an object anyone
  produces. TCKDB exposes the field there only because its calculation model is uniform
  across calc types. **This is a judgement about vacuous demand, not a code finding**,
  and it is the largest block of rows resting on judgement rather than evidence. If a
  reviewer thinks TCKDB genuinely wants these populated, flip them to
  `DIFFERENT_INSTANCE` and 136 rows become gaps — which would move the headline count
  from 136 absent to 272, i.e. it would more than double it. That sensitivity is the
  single most important thing on this page.
- **`level_of_theory.spin_treatment` (15 rows).** The `PARTIAL` label is the weakest
  cell in this file. The substance is in the rationale: every route is equally
  unsupplied and the sibling's `ADAPTER_GAP` verdict is itself wrong. A mechanical
  reader that acts on the verdict and skips the prose will get this one wrong in both
  directions at once.
- **`applied_energy_corrections[].scheme.*` leaves beyond `.method`.** I generalised
  from A2's statement that "full level_dict leaf mapping applies" plus the shared
  `_level_to_dict` producer. I did not see a fixture that actually populates
  `auxiliary_basis` / `cabs` / `dispersion` / `args` on an `arkane_level_of_theory` —
  A2 itself lists the rich-level surface among its eleven never-exercised rows. The
  code path is right; the field is untested.

**Explicitly not claimed:** nothing in this file re-opens A1's, A2's, A3's or A4's
inventories beyond the four corrections named above (A3's isotope effort, A3's
mis-keyed `imaginary_disposition`, A1's missing AEC param leaves, and the sibling
verdict on `spin_treatment`). Where a sibling row's own content looked wrong but was
not load-bearing for the route question, I left it alone and said so in the rationale
rather than silently adjudicating around it.
