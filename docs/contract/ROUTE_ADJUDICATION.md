# B2 — route adjudication

> **0.51 refresh (2026-09-29).** Sections 1–6 are the 0.22-era rulings and still
> apply by concept tail. Section 7 records what changed for tckdb-schemas 0.51.0:
> new row kinds (applicability rules, flattened aliases, route mirrors, mapping
> reviews), 39 new concept rulings, and the three 0.22 rulings this refresh
> supersedes (§1.5 is now `NOT_APPLICABLE` rather than `GENERALISES`; the
> `execution_environment` and `frequency_scale_factor` carve-outs are pattern
> rules so they reach the new roots). The counts in the table below are 0.22 counts.

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

---

## 7. The 0.51 refresh (Phase B, 2026-09-29)

Inputs: A1 at tckdb-schemas 0.51.0 (TCKDB_v2 `ad3cd706`, five roots, 3,803 field
rows plus 508 workflow checks), and fresh A2–A5. The join (`tools/join_inventories.py`)
was rerun after the fixes below; it now reports **0 near-misses and 0
`ROUTE_UNRESOLVED`**.

### 7.1 Why the join semantics changed

A4 now enumerates every route the adapter emits, one row per path, generated from a
per-route catalog and cross-checked against 521 built payloads. So the adapter side of
a row is observed, not inferred. At 0.22 a `GENERALISES` ruling re-verdicted a row from
its sibling's whole verdict (sibling WIRED ⇒ row WIRED), because A4 then keyed each
concept once. That would now claim the adapter emits routes A4 shows it does not.
**A ruling now borrows only ARC-side supply (A2/A3) from the sibling; the adapter side
is always the row's own A4 row.** Concretely, 404 rows the old logic called `WIRED` were
re-examined; most were result blocks on the wrong calculation type or routes on the three
new roots.

Route resolution also runs now for rows the adapter maps but no supply inventory keys at
that route. At 0.22 only unmapped rows were adjudicated, so the `reaction_upload.species[]`
mirror rows the adapter does fill stayed `SOURCE_UNCONFIRMED` even though A2 had keyed
their supply one route over.

### 7.2 New row kinds in `ROUTE_ADJUDICATION.yml`

| Kind | What it does | Rows |
|---|---|---|
| `route_applicability` (AP-1…AP-11) | Regex over the demand path; applies only when no inventory has a row at that path. `VACUOUS` → `NOT_APPLICABLE`; `DIFFERENT_INSTANCE`/`GATED` → `ARC_ABSENT` with nothing borrowed. | 11 rules, 1,261 rows |
| `flattened_alias` | Relates a `ComputedReactionCalculationIn` scalar (`freq_n_imag`, `opt_converged`, `sp_electronic_energy_hartree`, …) to the nested tail it flattens (`freq_result.n_imag`, …). This is keying issue 3 from the brief: the two spellings never share a path tail, so the tail matcher could not relate them. | 10 aliases, 15 rows |
| `route_mirror` (RM-1…RM-9) | Same ARC record reached through a mirrored route: `reaction_upload.species[]` ↔ `species_upload`, `conformer_upload` ↔ `species_upload`, `ts_upload` ↔ `reaction_upload.transition_state`. Makes §1.2 mechanical. | 9 rules, 198 rows |
| `mapping_review` (MR-1, MR-2) | The adapter emits a field (or A2 proposes a supply) from an ARC key that is the wrong object; replaces the verdict. | 2 rules, 15 rows |

### 7.3 Rulings, with the reason for each

**Applicability.**

- **AP-1** (361 rows, `NOT_APPLICABLE`). freq/sp/irc/path-search/scan result blocks, and the flattened `freq_*`/`sp_*` scalars, on the primary calculation of a conformer or TS. TCKDB forces that calculation to be type `opt` and admits only the matching result block. `conformer_upload.calculation` is not forced by the schema but is the adapter's opt in conformer mode.
- **AP-2** (160, `NOT_APPLICABLE`). IRC and path-search results on minima routes. **Supersedes §1.5**, which reached the same "not a gap" conclusion but encoded it as `GENERALISES`, so these rows showed as `WIRED`.
- **AP-3** (4, `NOT_APPLICABLE`). `reaction_coordinate_mode_index` on minima; the schema says it is refused there.
- **AP-4** (197, `ARC_ABSENT`, gated). All of `transport_upload.*` and `conformer_upload.transport.*`. ARC produces no transport data, so borrowing calculation/provenance supply onto a transport record would describe a deposit nobody can make.
- **AP-5, AP-6, AP-7** (266 / 175 / 11). §2.2, §2.1 and §2.4 as patterns. The 0.22 PARTIAL rulings on `workflow_tool_release.*` and `software_release.*` enumerated only the 0.22 routes as exceptions, so the new roots' execution-environment routes had leaked into `WIRED`.
- **AP-8** (25, `ARC_ABSENT`). A correction scheme's `workflow_tool_release` is the tool whose table was the proximate source (Arkane/RMG), not ARC. The 0.22 ruling generalised it from the bundle's ARC release because the route did not exist then. Sending ARC here would be wrong provenance.
- **AP-9** (5). `scheme.level_of_theory.spin_treatment`: the calculation routes gained a supply since 0.22 (`scf_reference`), which describes ARC's own freq/sp jobs, not the level a scheme was fitted at.
- **AP-10** (2, `NOT_APPLICABLE`). `conformer_key` on TS calculations; the 0.51 TS block has no conformers.
- **AP-11** (55, `ARC_ABSENT`). Hessian, spin diagnostic and spin treatment on the primary opt. They come from the freq/sp jobs; the adapter deliberately does not propagate them (`adapter.py:3416-3418`).

**Concept rulings added (39).** All in the YAML with evidence. The ones that carry a
judgement:

- `software_release.revision` — PARTIAL. Arkane routes generalise the RMG-Py commit. On calculation routes the ESS revision is a different object, but it is **not absent**: ARC exports the full banner (`Gaussian 16, Revision C.01`) and the adapter sends it whole as `version`. So those legs carry `different_instance_verdict: ADAPTER_GAP` (a new ruling field: what is true of the route when the sibling does not apply).
- `scf_stability.*` — PARTIAL. Generalises across additional-calculation routes; not onto the primary opt (the stability job tested the sp/freq reference).
- `parameters[].*` — PARTIAL, same split (the supply is the freq Hessian method). `parameters[].section` generalises (A3's deck resource sections exist for every opt).
- `source_literature.title` — PARTIAL. FSF citation generalises across statmech routes; a correction scheme's citation is a different object.
- `freq_scale_factor.software.version` — DIFFERENT_INSTANCE. The only sibling was the scheme's software, which MR-2 rules invalid anyway.
- The rest are the species/conformer/TS mirrors and coarse-opt generalisations (ARC writes `coarse_opt_*` from the shared `_spc_to_dict` for species and TS records alike).

**Mapping reviews.**

- **MR-1** (6 rows → `ADAPTER_GAP`). The adapter fills `scheme.atom_params[]` from `parameter_table.values`, which ARC writes only on the Petersson BAC record. The atom-energy record carries its table as `reference_atom_energies` (`ARC:arc/output.py:1575-1587`), which the adapter never reads. So the rows looked `WIRED` and are never emitted on current ARC output. This is integration-gate finding C4.
- **MR-2** (9 rows → `ARC_ABSENT`). A2 maps `scheme.software` from Arkane. TCKDB defines it as the ESS release that computed the scheme's parameters. Arkane's correction database does not record that release, so ARC cannot fill it honestly; Arkane there would be wrong data inside the scheme's identity tuple.

### 7.4 Inventory fixes made for the join (no row deleted)

1. **A2 `scheme.{atom,bond,component}_params`** (9 rows, 3 routes). Re-keyed from the leaf spelling to the 0.51 container spelling `…[]`, and 21 leaf rows added from each container's `arc_key` (`atom_params[].element/.value`, `bond_params[].bond_key/.value`, `component_params[].component_kind/.key/.value`). Each carries a `phase_b_note`. A2: 535 → 556 rows.
1b. **A2 `energy_level_of_theory` leaves** (review fix). 21 rows were added for `aux_basis`, `cabs_basis`, `dispersion`, `keywords`, `solvent`, `solvent_model` and `spin_treatment` on `species_upload.thermo`, `species_upload.statmech` and `conformer_upload.statmech`. ARC exports the energy level as a whole level dict via `_level_to_dict` → `Level.as_dict` (`ARC:arc/output.py:451-465`), and the sp reference via `scf_reference.sp_reference`; A2 had keyed only `.method` and `.basis`. The reaction-species mirror picks them up through RM-1. Effect: 35 rows moved from `ARC_ABSENT` to `ADAPTER_GAP`. A2 went from 556 to 577 rows.
2. **A5 `…applied_energy_corrections[].components[]`** (4 rows). The three species routes flipped to `breaking_for_producer: true`. Current ARC drops a Petersson BAC's entire component list when any bond lacks a parameter (`ARC:arc/output.py:1592-1596`) but still exports the total, and the adapter forwards `components or []`. A bonded species then gets a 422 `bac_total_requires_components`. The TS row stays true, with a note that current ARC main never exports a TS BAC (`_bac_is_applied_to`, `ARC:arc/output.py:1402`), so the TS break needs pre-`c8240195` output or a legacy record. A5 asked Phase B to confirm exactly this.
3. **Join tool.** Container, root and union rows get `CONTAINER` with a roll-up. Breaking drift on a container whose descendants the adapter maps is `BROKEN`, which is how the one producer-breaking drift A5 found (keyed on a container) now reaches the matrix at all. The near-miss check ignores list markers and union braces, which is what catches keying issue 1. A5 `removed` rows are listed rather than reported as near-misses. The tail index excludes `arc_only.*` and non-demand paths (§2.5's warning). Workflow checks are attached to their path.
4. **Keying issue 2** (transport extras only in A1) needed no inventory change. ARC and the adapter have no transport, so there is nothing to key; AP-4 rules the whole root.
5. **Keying issue 4** (A1 container/union/root rows) is handled by the `CONTAINER` verdict above.
6. **Superseded 0.22 rulings.** The two `NEVER_EXISTED` rows (`reaction_upload.species[].thermo.source_calculations[]`) are resolved: `BundleThermoIn` gained `source_calculations[]` at 0.51 (`61f4b256`, #151), so the adapter's key is now valid. The 12 `A1_MISS` rows are resolved: A1 at 0.51 has the `atom_params[]`/`bond_params[]` leaves. The join now skips both kinds when it looks up route rulings.

### 7.4b `ADAPTER_GAP` with unconfirmed ARC supply (review fix)

A `GENERALISES` ruling whose sibling is supplied only by the adapter (an A4 row with no
A2 or A3 row) still yields `ADAPTER_GAP` when the adapter does not emit the route. That
is 57 of the 288 `ADAPTER_GAP` rows. Those rows now carry the flag
`ARC_SUPPLY_UNCONFIRMED` (`arc_supply: unconfirmed` in `gap_matrix.yml`): the adapter
could emit them only if its source at the sibling is real ARC data, and no inventory
confirms that. The other 231 have an A2 export.

Separately, a `WIRED` verdict reached through a concept, mirror or alias ruling proves the
ARC key exists, not that the value is right. `GAP_MATRIX.md` now says so.

### 7.5 Confidence

- **High.** AP-1/2/3/10 (schema validators), AP-4 (three inventories agree ARC has no transport), AP-5/6 (0.22 rulings, now reaching the new roots), MR-1 (read in both repos), the A5 species BAC flip (read in all three repos).
- **Medium.** AP-8 and MR-2 rest on TCKDB's docstring semantics for scheme `software` and `workflow_tool_release`. Whether Arkane's tables live in RMG-Py or RMG-database is UNVERIFIED. RM-8/RM-9 matched only one row.
- **Low.** Treating `conformer_upload.calculation` as the primary opt (AP-1, AP-11, the `*_opt_primary` exceptions). The schema allows another type there; the ruling holds for the adapter's conformer mode, not for every producer.
