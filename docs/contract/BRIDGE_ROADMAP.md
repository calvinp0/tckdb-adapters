# ARC → TCKDB bridge roadmap

**Question.** What else does the tckdb-arc adapter need so that ARC's results reach
TCKDB with all the information TCKDB needs or wants?

**Basis.**

- The Phase B join of five fresh inventories keyed to tckdb-schemas 0.51.0 (TCKDB_v2
  `ad3cd706`): `GAP_MATRIX.md` and `gap_matrix.yml`, ruled by `ROUTE_ADJUDICATION.yml`
  (§7 of the `.md` has the 0.51 rulings).
- ARC `main` `db0934d5`, plus the open PR #1059 branch (output schema 1.2).
- The adapter at `0913124`.
- The live integration gate (`feature_tckdb_integration_gate`, `bd2b13d`).

**Revision.** This version applies an adversarial review of the first draft (accepted
with notes). The top 10 and items A1–A6, A11 and D1 are corrected, and so are the
sourcing labels.

**Line numbers.** Line numbers refer to `tckdb_arc/tckdb_arc/*.py` at `0913124` unless a
repo prefix says otherwise (`ARC:`, `RMG-Py:`, or `TCKDB:` for `TCKDB_v2`).

**Sourcing.** Items tagged **[gate]** come from the integration gate's live run, recorded
as session findings. Of those findings, only T4 is written up in the committed gate
document. Each [gate] item is therefore re-derived from the code it cites, and the code
citation, not the gate label, is the evidence.

## The answer in one paragraph

The adapter already deposits the core of an ARC run correctly: species, conformers,
opt/freq/sp, thermo with an enthalpy declaration and standard-state pressure, AEC/BAC
totals, Hessians, IRC and GSM paths, kinetics and TS composition. The live gate
confirmed each of them persists. What is left falls into four groups.

- **One producer-breaking defect.** A componentless Petersson BAC makes TCKDB refuse the
  whole upload.
- **Wrong data on the ARC and adapter side.** In each case the adapter, or ARC, asserts
  something the evidence does not support:
  - screened conformers filed at the opt level, which is wrong in ARC's *default*
    configuration;
  - the GSM/NEB guess and the IRC level;
  - enthalpies on ARC's current 1.1 output (a fix is committed on a branch, pending merge);
  - levels under `adaptive_levels`;
  - calculations for atoms that never ran;
  - an inferred statmech treatment.
- **Wrong data or wrong state on TCKDB's side.** TCKDB commits after answering 201, and
  method aliases split one level into two rows.
- **Missing data, mostly cheap.** 409 leaves need no new ARC capability (`ADAPTER_GAP`
  plus `ARC_LATENT`). Of those, 231 are adapter gaps with a confirmed ARC export. Another
  57 are adapter gaps whose ARC supply no inventory confirms. The best-value adapter
  items are TS IRC validation evidence, SCF stability, Arkane provenance and the ESS
  revision. The rest needs ARC exports (per-species levels; scan, IRC and conformer
  levels; the atom map), TCKDB schema homes (non-IRC TS checks, T0, TS statmech), or a
  decision from you (deposit rights; which constants may make provenance claims).

## How this is ranked

1. **Producer-breaking** (TCKDB refuses the upload) first, then **wrong data** (TCKDB
   accepts something false), then **missing data** (TCKDB accepts an incomplete record).
   Wrong data outranks missing data regardless of tier (`PRIORITY_POLICY.md`,
   "Correctness overrides value").
2. Within a class: the value tier, then effort.
3. Effort: **S** is hours in one repo, plus a test. **M** is a day or two, a schema or
   fixture change, or work in two repos. **L** is new plumbing or a new capability.

Each item cites the demand row (a `path` in `TCKDB_DEMAND.yml`) and what TCKDB does
without it (`on_absence`).

## Matrix at a glance

Leaf demand fields at 0.51. Containers are excluded, and so are the 527 leaves ruled
`NOT_APPLICABLE` (for example an IRC result on a minimum). Reproduce with
`tools/phase_c_stats.py`.

| Verdict | Leaves | Share | Meaning |
|---|---:|---:|---|
| `WIRED` | 524 | 19.8% | An ARC export row and an adapter mapping row exist. **The key exists; the value is not proven right** (see A3) |
| `SOURCE_UNCONFIRMED` | 283 | 10.7% | The adapter sends it from a log reparse, a constant, or a key ARC does not export |
| `ADAPTER_GAP` | 288 | 10.9% | TCKDB has a slot and the adapter does not fill it. 231 have a confirmed ARC export; 57 are flagged `arc_supply: unconfirmed` |
| `ARC_LATENT` | 121 | 4.6% | ARC computes it but does not export it |
| `ARC_ABSENT` | 1,431 | 54.1% | ARC cannot produce it today |
| `BROKEN` | 3 containers | — | The BAC components rule (item 1) |

Where the absent leaves concentrate:

| Group | Absent leaves |
|---|---:|
| Literature | 381 |
| Execution environment | 293 |
| Correction-embedded frequency scale factors | 175 |
| Transport (`transport_upload.*` only; 197 including `conformer_upload.transport.*`, all rule AP-4) | 167 |

The groups overlap, and they are mostly schema breadth rather than ARC shortfalls.

Populated today: 807 of 2,647 applicable leaves (30.5%). By tier, after the revised
policy amendment:

| Tier | Populated leaves | Share |
|---|---:|---:|
| Kinetics lookup | 367 of 866 | 42.4% |
| Reproducibility | 328 of 1,525 | 21.5% |
| Atom mapping | 72 of 94 | 76.6% |
| Completeness | 20 of 105 | 19.0% |
| Untiered | 20 of 57 | 35.1% |

The atom-mapping share looks high because the tier is small and mostly scan, IRC and
path-search results; the atom map itself is latent.

---

## Top 10

| # | Item | Owner | Class | Effort |
|---:|---|---|---|---|
| 1 | A componentless Petersson `bac_total` gets the whole upload refused. The adapter's per-component filter can also send a partial decomposition | Adapter | **breaking** (+ wrong) | S |
| 2 | Screened alternative conformers are filed at `opt_level`. This is wrong in ARC's default configuration | Adapter; ARC exports the conformer level | **wrong** | S |
| 3 | The GSM/NEB TS guess (always) and the IRC (when `irc_level` ≠ `opt_level`) are labelled with the opt level. Take the IRC level from its log's route line | Adapter; ARC exports `irc_level` | **wrong** | S |
| 4 | Enthalpy on 1.1 output: the narrowed fail-closed rules, **decided (D1, option d)** and committed as `84b1b05` on `fix_unverifiable_enthalpy_without_flags` (adapter 0.5.0), pending merge | Adapter (committed, pending merge); ARC merges PR #1059 | **wrong** | S |
| 5 | T4: TCKDB commits after the 201, so a producer can be told an upload succeeded that never persisted | TCKDB | **wrong state** | M |
| 6 | Detect `adaptive_levels` runs from ARC's `input.yml`/`restart.yml` and refuse or strip; the ARC export follows | Adapter (interim); ARC | **wrong** | S |
| 7 | Atoms get an opt that never ran: normally `converged: false`, and `converged: true` in composite runs | Adapter + TCKDB | **wrong** | S–M |
| 8 | `statmech_treatment` claims rotors Arkane may have dropped. Gate it on Hessian evidence | Adapter; ARC exports the applied treatment | **wrong** (conditional) | S |
| 9 | Method aliases (`wb97x-d` vs `wb97xd`) split one level into two rows | TCKDB | **wrong** (duplicate identity) | M |
| 10 | Send TS IRC validation evidence from `ts_checks.IRC`, never from `irc_converged` | Adapter | missing; every TS warns | S–M |

> [!WARNING]
> **Pinned just below the cut: A11 (declaring `energy_level_of_theory`) would 422 on
> real ARC output as written.** TCKDB's level-of-theory hash includes `spin_treatment`,
> and a NULL folds to `"unknown"`. The adapter stamps `spin_treatment` on the sp
> calculation's level, but `_thermo_energy_level` returns the bare run level. Declaring it
> therefore raises `thermo_energy_level_contradiction` / `statmech_energy_level_contradiction`.
> Do not ship A11 without copying the sp calculation's `spin_treatment`, and add a gate
> case for it.

Also just below the cut:

- A7: split the ESS banner into version and revision.
- A9: Arkane provenance on species thermo and statmech.
- A10: SCF stability.
- A8: the artifact batch drops the server's warnings.

---

## A. Adapter work

Each item is one of three things:

- ARC exports the datum and the adapter does not use it;
- the defect is in the adapter's own logic;
- the item is flagged `arc_supply: unconfirmed`: the only known source is the
  adapter's own mapping on a sibling route.

### A1. Petersson `bac_total` without a bond component (**breaking**; top 10 #1)

**Status (adapter 0.6.3): done.** See CURRENT_ARC_INTEGRATION.md, "Corrections and statmech evidence".

**What to change.** Before emitting a `bac_total` from a `bac_petersson` scheme, require
at least one component of kind `bond`:

- on a TS, drop a componentless correction;
- on a species whose SMILES has a bond, drop it;
- on a monatomic species, keep it.

Record a sidecar warning either way. Never pad with an `other` placeholder; TCKDB refuses
that too.

Also drop the whole correction, not individual components, when any component lacks
`parameter_value` or `contribution_value`. Today the per-component null filter
(`adapter.py:5020-5028`) can send a *partial* decomposition whose contributions do not sum
to `value`. TCKDB does not check the sum, so that is silent wrong data. ARC itself drops
the whole list in that case (`ARC:arc/output.py:1592-1596`).

**Why it matters to TCKDB.**

- Demand path: `…applied_energy_corrections[].components[]` on the species,
  reaction-species and TS routes.
- Workflow check: `bac_total_requires_components`, block tier, 422.
- The whole species or reaction upload is refused, not only the correction.

**Evidence.**

- The adapter forwards `correction.get("components") or []` (`adapter.py:303`).
- ARC drops all components when any bond lacks a parameter, but still exports the total
  (`ARC:arc/output.py:1592-1596`).
- TCKDB's rule is at `TCKDB:backend/app/services/energy_correction_resolution.py:601-690`.

**When it fires.**

- **Species:** a bond outside Arkane's Petersson table at that level. This happens on
  current ARC main; it is conditional and no fixture covers it.
- **TS:** legacy output only. Current ARC never exports a TS BAC (`_bac_is_applied_to`,
  `ARC:arc/output.py:1402-1416`), so the TS case needs pre-`c8240195` output or a legacy
  `applied_energy_corrections` record. TCKDB measured 17 such rows.

**Matrix.** `BROKEN` on 3 containers.

**Effort.** S.

### A2. Enthalpy on output without the 1.2 flags (**wrong**; top 10 #4; decided, committed, pending merge)

**Decision.** D1 is decided as option (d). The fix is reviewed and committed as `84b1b05`
on branch `fix_unverifiable_enthalpy_without_flags` (adapter 0.5.0). It is not yet merged,
and it is not in this worktree.

**What changes.** When `thermo.atom_corrections_applied` is absent or null, the adapter
strips H298, the NASA fit, and point H and G, keeping S298 and Cp, in three cases:

- **Light species, by composition:** hydrogen-only species, He, He2, HeH and the Li atom.
  Their raw total energy is below the ±2×10⁴ kJ/mol magnitude guard
  (`adapter.py:5118-5126`). Composition comes from the species' xyz, falling back to its
  `formula`.
- **Level mismatch:** the header `arkane_level_of_theory` does not match the energy level.
- **Unverifiable level:** either level sets a separate `dispersion` or `solvation_method`
  field, which ARC's atom-energy matching ignores.

**Residual risk.** A stand-in that matches on method and basis but differs by a year
refit, or through ARC's fuzzy key match, still passes. Only ARC's 1.2 flags
(B1, PR #1059) close that.

**Why it matters to TCKDB.**

- Demand paths: `species_upload.thermo.enthalpy_reference_kind` (declares
  `formation_298k`), `…h298_kj_mol`, `…nasa.*`, and `…points[].h_kj_mol` / `g_kj_mol`.
- TCKDB cannot detect a raw absolute energy stored as a formation enthalpy; it passes
  every check.
- An `atom_energy` row in `energy_corrections` does **not** prove corrections were
  applied. It is keyed on the energy level alone, and Arkane's model chemistry can be
  `None` when the freq level is not found and no `freq_scale_factor` was given
  (`ARC:arc/statmech/arkane.py:1138-1157`).

**Effect on the golden corpus.** Only H2 loses its enthalpy.

**Merge coordination.** On the `feature_tckdb_integration_gate` branch,
`test_golden_species_calculations_thermo_and_hessian` asserts that golden H2 carries
`formation_298k` and a NASA fit. Whichever of the two branches merges second must update
that assertion.

**Effort.** S; done on the branch, merge pending.

### A2b. Interim detection of `adaptive_levels` runs (**wrong**; top 10 #6)

**Status (adapter 0.6.4): done, exact where `restart.yml` allows, else refuse or omit.**
`tckdb_arc/adaptive.py` reads the project's `restart.yml` and `input.yml` (and the CLI's
parsed `input.yml`). When `restart.yml` holds the adaptive spec (`arc/main.py:438-442`)
and an entry for the species, ARC's own rule is replayed
(`scheduler.determine_adaptive_level`, `arc/scheduler.py:5273-5299`): the heavy-atom count
is the species' `adaptive_lot_n_heavy` when set, else the non-`H` atoms of its geometry;
the range is `lo <= n <= hi` (or `hi == 'inf'` and `n >= lo`); the level is the entry whose
job types contain the job type exactly (case-sensitive), else the run's regular level. The
job types are `opt`, `freq`, `sp`, `composite`, `irc`, `scan` (ESS rotor scans) and
`directed_scan` (per rotor, from `restart.yml`'s `rotors_dict[i]['directed_scan_type']`).
The attributed level labels that species' calculation, and its sp/composite level is the
energy level the enthalpy check compares with the atom-correction level, so adaptive runs
keep their formation enthalpies. The fallback, when `restart.yml`, the species entry or a
heavy-atom count is missing, or only `input.yml` is available (string levels, no
`adaptive_lot_n_heavy`): a named `opt` refuses the upload
(`opt_level_adaptive_not_attributable`); a named `sp`, `freq`, `scan`/`directed_scan` or
`irc` omits those calculations (`<kind>_level_adaptive_not_attributable`); a named `sp` or
`composite` strips thermo enthalpy (`enthalpy_adaptive_levels_unverifiable`). With only
`output.yml` no detection is possible and the run reads as an ordinary one. Dropped when ARC
exports per-species levels (B2).

**The problem.** Under `adaptive_levels`, every calculation is attributed to the run-level
level. `…level_of_theory.*` is required on every calculation route, so it is always sent,
and here it is wrong (B2).

**Detection is possible today.** `output.yml` does not record `adaptive_levels`, but ARC
writes it to `restart.yml` (`ARC:arc/main.py:439`), and the user's `input.yml` carries it.
The adapter already reads `input.yml` for the `tckdb` block (`cli.py:1-24`), and the sweep
has the project directory.

**What to change.**

- When `adaptive_levels` is set, refuse the calculations whose level it may have changed.
- Alternatively, strip them and keep only what does not depend on the level. That is a
  policy choice under D1's principle.
- Record a sidecar warning either way.
- Drop this interim once ARC exports per-species levels (B2).

**Effort.** S.

### A3. The TS-guess and IRC calculations carry the opt level (**wrong**; top 10 #3)

**Status (adapter 0.6.4): TS guess done; IRC exact when `restart.yml` records it.** The
NEB path search uses `neb_level` and `ess_software.neb` / `ess_versions.neb`; the program is
only ever the observed one, because ARC's `Level` deduces a software from the method alone
(`wb97xd/def2tzvp` gives gaussian, not ORCA), so without `ess_software.neb` the calculation
is omitted (`ts_guess_software_not_stated`). GSM, and NEB without an exported `neb_level`,
are omitted with `ts_guess_level_not_stated`, never filed at `opt_level`. The IRC uses the
`irc_level` `restart.yml` records (ARC records it whenever it differs from the settings
default, `arc/main.py:480-483`) with the program ARC's rule gives an IRC (`arc/level.py:413-418`: Gaussian, name only; none
for UMA/torchani/xtb methods, where the IRC is omitted, `irc_software_not_stated`), or, when
the adaptive levels name `irc`, the species' adaptive level; the TS reference energy for the IRC then
stays at that level. With no recorded `irc_level` the IRC is the run's settings default,
`default_levels_of_theory['irc']`, which the adapter cannot read, and it keeps `opt_level` with
`irc_level_assumed_opt_level` (the maintainer's 0.6.0 decision, warning reworded to say so).
ARC's parsers and parser evidence expose no level from an IRC log. Durable fix: B3.

**TS guess (always wrong).** `adapter.py:2662-2671` builds the `path_search` calculation
with `level_kind="opt"` and `ess_job_key="opt"`, under the comment "No ts_guess_level in
output_doc today". A GSM (xTB) or ORCA-NEB guess is therefore filed as a DFT calculation
run by the opt program. Both golden TS payloads populate this.

- ARC exports `neb_level` (`ARC:arc/output.py:180-181`) and `ess_software.neb` /
  `ess_versions.neb`. Use them for NEB.
- For GSM, ARC exports no level. Omit the calculation (it cannot be sent without a level)
  until B3 lands, or take the level from the GSM input.

**IRC (wrong when `irc_level` ≠ `opt_level`).** The adapter builds the IRC calculation with
`level_kind="opt"`, under the comment "ARC runs IRC at the opt level"
(`adapter.py:2793-2794`). ARC actually runs IRCs at `irc_level`, which defaults to
`wb97xd/def2tzvp` whatever `opt_level` is (`ARC:arc/main.py:1166-1176`,
`ARC:arc/settings/settings.py:235`), and does not export it.

- So the label is wrong whenever the user set `opt_level` but not `irc_level`, or set the
  two differently.
- Interim fix: read the method and basis from the IRC log's route line. The adapter
  already reparses that log.
- Durable fix: B3.

**Why it matters to TCKDB.** Demand paths: `…transition_state.calculations[].level_of_theory.*`
and `.software_release.*`, and the same under `ts_upload.additional_calculations[]`.

These rows are **`WIRED`** in the matrix: the ARC key exists and the adapter emits the
field. That is exactly the case the matrix cannot judge (see `GAP_MATRIX.md`, "What
`WIRED` proves").

**Effort.** S.

### A4. Screened alternative conformers are filed at `opt_level` (**wrong**; top 10 #2)

**Status (adapter 0.6.4): done, at the `restart.yml` conformer level when it is stated.**
output.yml exports no conformer level (header, species or conformer). The screened
conformers are filed as bare opts at the `conformer_opt_level` `restart.yml` records, with
the program that level names, only when `job_types['conf_opt']` is true there (else the
geometries are force-field ones) and the adaptive levels do not name `conf_opt` for the
species' range, and only those conformers whose `conformer_energies` entry is not null
(a null one is still the force-field geometry). Known gap: ARC's conformer troubleshooting
level (`arc/scheduler.py:5050-5078`) is recorded nowhere durable. Otherwise they are omitted and each species with distinct ones reports
`conformer_level_not_stated`. The durable fix is B3.

**What happens.** `_build_alt_conformer_blocks` (`adapter.py:1290-1400`) gives every
screened conformer a primary `opt` calculation at the run's `opt_level`, with the opt
program. The `parameters_json.tckdb_origin` marker (`screened_conformer`,
`adapter.py:4349`) is only a qualifier; `level_of_theory` and `software_release` still
assert the opt level.

**Why this is wrong by default.** ARC screens conformers at the conformer level, which
by default is lower than the opt level: `wb97xd/def2svp` against `wb97xd/def2tzvp`
(`ARC:arc/settings/settings.py:227-229`). ARC exports no conformer level (A3 row 5). The
adapter's docstring assumes the screen ran "at the same level", which holds only when a
user sets the two levels equal.

**Why it matters to TCKDB.** Demand path:
`species_upload.conformers[].primary_calculation.level_of_theory.*`. A primary calculation
must have a level (422 otherwise), so there is no honest partial form.

**What to change.** Stop emitting alternative conformers until B3 exports the conformer
level, then use it.

**Effort.** S.

### A5. `statmech_treatment` can claim rotors Arkane dropped (**wrong**, conditional; top 10 #8)

**Status (adapter 0.6.3): done.** `torsions[]` are still sent. See CURRENT_ARC_INTEGRATION.md, "Corrections and statmech evidence".

**What happens.** `_classify_statmech_treatment` (`adapter.py:6020-6024`, `:6069`) derives
`rrho_1d` / `rrho_nd` from the rotors ARC lists. Arkane ignores every rotor when the freq
log has no force-constant matrix and treats the species as RRHO, logging only a warning
(`RMG-Py:arkane/statmech.py:647-667`).

**When it matters.** Gaussian freq jobs from ARC always add `IOp(7/33=1)`
(`ARC:arc/job/adapters/gaussian.py:323-326`), which writes that matrix. So the case arises
for composite, ORCA, Q-Chem and Molpro freq jobs.

**What to change.** Emit a rotor-bearing treatment only when the parser evidence contains
the species' `freq_hessian` (the lookup the adapter already performs at `adapter.py:1838`).
Otherwise omit the field.

**Why omitting is acceptable.** Demand path: `species_upload.statmech.statmech_treatment`
is optional. TCKDB's validator docstring says producers, ARC included, deposit statmech
without naming a treatment, and that "an absent field is honest where an invented one
would not be" (`TCKDB:…/computed_species_upload.py:602-605`).

**Durable fix.** B4.

**Effort.** S.

### A6. Atoms get an opt that never ran (**wrong**; [gate]; top 10 #7)

**What happens.** The computed-species bundle always builds a primary `opt`
(`adapter.py:1162-1174`), and `_opt_result_payload` copies `opt_converged` (`:4407`).

- ARC never sets `job_types['opt']` for a single atom, so the deposit is normally a
  fabricated opt with `converged: false`, a level and a program.
- In composite runs ARC sets `job_types['opt'] = True` for every composite job
  (`ARC:arc/scheduler.py:3441`), so an atom arrives with `converged: true`.

**Why it matters to TCKDB.** Demand path: `species_upload.conformers[].primary_calculation`
is required and must be type `opt`, so TCKDB's contract has no honest way to deposit an
atom through this root (C6).

**What to change.** Skip atoms in computed-species mode with a sidecar reason until C6
provides a form without an opt.

**Effort.** S to skip; M with C6.

### A7. Send the ESS version and revision separately; use `arkane_version` ([gate])

**Status (adapter 0.6.1): done.** See CURRENT_ARC_INTEGRATION.md, "Provenance passthrough".

**What to change.**

- Split ARC's `ess_versions` banner into `version` and `revision`; for example
  `Gaussian 16, Revision C.01` becomes `16` and `C.01`. Today the whole banner goes into
  `version` (`adapter.py:3437-3441`).
- Fill `analysis_software_release.version` from `arkane_version`. Only `arkane_git_commit`
  is read today (`adapter.py:5756`), and the docstring saying no version is exported is
  stale.

**Why it matters to TCKDB.**

- A composite version triggers `software_release_version_is_composite`, and TCKDB splits
  it, but only on the synchronous routes (A1 `on_absence`). Whether the async `/jobs`
  routes store the banner verbatim was not verified.
- 16 `…software_release.revision` rows are `ADAPTER_GAP` (concept ruling
  `software_release.revision`), as is `reaction_upload.analysis_software_release.version`.

**Effort.** S.

### A8. The artifact batch drops the server's warnings, status and request ID ([gate])

**What happens.** After `client.upload_artifacts(..., batch_by_calculation=True)`, every
sidecar gets the batch summary. tckdb-client 0.93 returns only the body, so per-item
warnings, the HTTP status, the request ID and the replay flag are lost
(`adapter.py:3906-3940`).

**Why it matters.** The depositor cannot see
`software_release_version_filled_from_artifact` or `…identity_corrected_from_artifact`,
the warnings that report TCKDB repairing the adapter's provenance from the log.

**Effort.** S in the adapter once the client exposes the envelope (C3).

### A9. Name Arkane on species thermo and statmech ([gate])

**Status (adapter 0.6.1): done** for the computed-species route (the reaction route
inherits the bundle's `analysis_software_release`).

**What to change.** Set `software_release` to `{name: Arkane, version: arkane_version,
revision: arkane_git_commit}` on `species_upload.thermo`, `species_upload.statmech` and
their reaction mirrors. The computed-reaction route already sets
`analysis_software_release` (`adapter.py:2297-2299`); the species route never names Arkane.

**Why it matters to TCKDB.** Without it, TCKDB warns `missing_software_release_provenance`
(`TCKDB:backend/app/services/provenance_warnings.py:143`) on every computed-species thermo.
15 rows are `ADAPTER_GAP`. This field is analysis software, never a calculation's
`software_release` (A18).

**Effort.** S.

### A10. SCF stability from `wavefunction_stability`

**What to change.** Map ARC's `wavefunction_stability` onto `scf_stability.*` of the sp or
freq calculation whose reference was tested. Send `status: stable` only when an analysis
actually ran, and never attach it to the opt.

**Why it matters to TCKDB.** Demand path: `…additional_calculations[].scf_stability`.
Without it, TCKDB stores no row and reports `not_checked`, although ARC did check. 25 rows
are `ADAPTER_GAP` with a confirmed ARC export.

**Evidence.** `ARC:arc/output.py:1841-1842`, parser at `:586-633`. The adapter reads only
`scf_reference` (`adapter.py:3416-3423`). The `followed_to_stable` → `stabilized` mapping
is A2's proposal; confirm it with ARC.

**Effort.** S–M; no real-ARC fixture covers it (E1).

### A11. Declare the thermo and statmech energy level (⚠ 422 risk as written)

**What to change.** Send `energy_level_of_theory` on thermo and statmech. The adapter
computes it already (`_thermo_energy_level`, `adapter.py:5177`) and discards it.

**Why it matters to TCKDB.** Demand path: `species_upload.thermo.energy_level_of_theory`
and its statmech, reaction and conformer mirrors: 45 `ADAPTER_GAP` rows with a confirmed
export. The leaves `aux_basis`, `cabs_basis`, `dispersion`, `keywords`, `solvent`,
`solvent_model` and `spin_treatment` were added to A2 in this revision: ARC exports the
whole level dict via `_level_to_dict` (`ARC:arc/output.py:451-465`), and the sp reference
via `scf_reference.sp_reference`. Without the field, TCKDB runs no declared-level check.

**The trap.** Declaring the level turns on TCKDB's checks against the sp links, and as
written it fails them:

- TCKDB's level-of-theory hash includes `spin_treatment`, and a NULL folds to `"unknown"`
  (`TCKDB:backend/app/services/calculation_resolution.py:138-143`).
- The adapter stamps `spin_treatment` from `scf_reference` on the sp calculation's level
  (`adapter.py:3419-3423`), but `_thermo_energy_level` returns the bare run level.
- On any output with `scf_reference`, the declared level and the sp calculation's level
  therefore resolve to different rows, and TCKDB raises
  `thermo_energy_level_contradiction` / `statmech_energy_level_contradiction`, a 422
  (`TCKDB:backend/app/services/calculation_levels.py:458-466`).

**Before shipping.** Build the declared level from the sp calculation's projected level,
including `spin_treatment`, and add a gate case. The first draft of this roadmap said the
two levels "agree"; that was wrong.

**Effort.** S, plus the gate case.

### A12. `scheme.atom_params` from `reference_atom_energies` ([gate])

**Status (adapter 0.6.3): done.** See CURRENT_ARC_INTEGRATION.md, "Corrections and statmech evidence".

**What to change.** For `atom_energy` corrections, read
`energy_corrections[].reference_atom_energies` (per-element atomic energies, in hartree).
The adapter reads `parameter_table`, which ARC writes only on the Petersson record
(`adapter.py:285-291`, `ARC:arc/output.py:1575-1587`). These values are the scheme's
parameters, **not** per-atom corrections: never reconstruct the total from them.

**Why it matters to TCKDB.** Demand path: `…scheme.atom_params[].element/.value`, used when
TCKDB creates a new scheme. 10 `ADAPTER_GAP` rows (mapping review MR-1).

**Effort.** S.

### A13. Conformer mode drops statmech, corrections, scans and rejected rotors

**What happens.** `_build_payload` (`adapter.py:3277-3298`) emits only identity, geometry
and opt/freq/sp. The 0.51 conformer root also accepts `statmech` (including `torsions[]`
and `invalidated_reason`), `applied_energy_corrections[]` and scan calculations.

**The gap in numbers.**

| Block | Confirmed ARC export | ARC supply unconfirmed |
|---|---:|---:|
| `conformer_upload.statmech.*` | 37 | 12 |
| `…applied_energy_corrections[]` | 20 | 9 |

`torsions[].invalidated_reason` is the only 0.51 home for ARC's rejected rotors
(`ARC:arc/output.py:2172`), and no mode reaches it.

**Dependencies.** A1, A5 and A9 apply here too.

**Effort.** M.

### A14. TS validation evidence from `ts_checks.IRC` (top 10 #10)

**Status (adapter 0.6.4): done on both TS routes.** A bool `ts_checks['IRC']` becomes
`validation_evidence[{kind: irc, passed, rationale}]` (`source_calculation_key` on the
reaction bundle; omitted on the standalone route). The rationale is exactly
`ARC ts_checks['IRC'] = <verdict>`: ARC's `ts_checks['warnings']` come only from the
e_elect and NMD checks (`arc/checks/ts.py:175`, `arc/checks/nmd.py:93-131`), never the IRC.
`None` or an absent `ts_checks` sends nothing. A verdict with no IRC calculation in the
upload is not sent (`ts_irc_evidence_without_irc_calculation`).

**What to build.** `validation_evidence[]` as follows:

- `kind: 'irc'`.
- `passed` from `transition_states[].ts_checks.IRC`, only when that value is a bool.
  `None` means not run and must not be sent as `passed=false`.
- `source_calculation_key` pointing at the IRC calculation.
- `rationale` from `ts_checks.warnings`. That string is often `""` while TCKDB requires
  `min_length=1`, so use a fixed factual sentence when it is empty.
- The participant mappings are latent in ARC (B10); omit them.

**Never use `irc_converged`.** It "reports that the IRC jobs completed, not that the IRC
validated anything" (`ARC:arc/output.py:2789-2791`). The adapter is right not to use it.
The 0.22 `GAP_MATRIX.md` row that mapped `passed` from `irc_converged` was an overclaim
and is gone.

**Why it matters to TCKDB.** Demand paths: `reaction_upload.transition_state.validation_evidence[]`
and `ts_upload.validation_evidence[]` (6 `ADAPTER_GAP`, 6 `ARC_LATENT`). Without them TCKDB
warns `transition_state_missing_irc_evidence` on every TS
(`TCKDB:…/services/transition_state_validation.py:41`). Only the IRC check has a TCKDB
home (C7). No real-ARC fixture sets `ts_checks.IRC` (E1).

**Effort.** S–M.

### A15. Smaller adapter gaps

Ordered by value.

| Item | Demand path | Evidence | Effort |
|---|---|---|---|
| Scan calculations never carry their log | `…additional_calculations[].artifacts[]` on scans | `_LOG_FIELD_BY_CALC_KEY` has no scan role (`adapter.py:388`); ARC exports `rotor_scans[].source_log` | S |
| Melius BAC tables not carried | `…scheme.component_params[]` (15 `ADAPTER_GAP`) | ARC's run-level Melius table (`ARC:arc/output.py:193`) | S |
| Reaction conformers lack `label` | `reaction_upload.species[].conformers[].label` | Set for species (`adapter.py:1287`), not for reactions | S |
| `path_search_result.climbing_image_index` never set | same | The adapter already flags `points[].is_climbing_image` | S |
| Bundle-level ESS `reaction_upload.software_release` | same (3 `ADAPTER_GAP`) | One release per bundle; needs a rule when several programs ran | S |
| Thermo-level applied corrections | `species_upload.thermo.applied_energy_corrections[]` (20 confirmed + 9 unconfirmed) | TCKDB intends this mainly for the FSF used for ZPE; low value | S |
| Scan constraints written at calculation level, not in `scan_result.constraints[]` | both | `adapter.py:1526-1533`; harmless today | S |

### A16. A species on both sides is deposited twice ([gate])

**What happens.** Reaction actors are keyed per index (`_local_key_for_actor`,
`adapter.py:5664-5676`). A species that is both reactant and product, and `H + H` style
duplicates, become separate `species[]` entries with their own conformers and
calculations.

**What to change.** Deposit one entry and reference it from both key lists.

**Effort.** M.

### A17. IRC endpoint species are uploaded as ordinary species

**What happens.** ARC exports `IRC_<ts>_<n>` endpoint optimizations as plain `species[]`
records with no marker (`ARC:arc/scheduler.py:4167`), and the sweep uploads every
converged record (`sweep.py:111-114`).

**What to change.** Needs B6; decision D5 covers the interim.

**Effort.** S after B6.

### A18. Guard: never name a workflow tool as a calculation's software (HEAD-only rule)

**The rule.** TCKDB #560/#565 (2026-09-28/29) refuse a calculation `software_release`
named arc, arkane, rmg or rmgpy (`calculation_software_is_workflow_tool`, block).

**Status.** The adapter already complies. It takes the name from the observed
`ess_software[job]`, falling back to the requested level's software
(`adapter.py:3394-3405`).

**What to change.** Add a one-line assertion.

**Deployment.** These rules postdate schemas 0.51.0 (`2a32f694`). HEAD's client is 0.94;
the adapter pins 0.93. Verify what the deployment runs (C12).

**Effort.** S.

### A19. Mappings that read keys ARC does not export

**What happens.** The adapter reads `unmapped_smiles`, `reactions[].reversible`,
`kinetics.degeneracy`, `kinetics.note`, `sp_spin_diagnostic.note`, `irc_final_settings`,
`thermo.cp_data`, and `electronic_energy_hartree` as an sp fallback. ARC main exports none
of them (A4 §9.3).

**Risk.** No wrong data, but they look like coverage.

**What to change.** Delete them, or mark them legacy.

**Effort.** S.

---

## B. ARC export work

### B1. Merge PR #1059 (output 1.2 atom-correction flags)

**What.** `thermo.atom_corrections_applied`, `bond_corrections_applied` and
`atom_corrections_level` (branch `feature_export_atom_corrections_applied` @ `1977e53b`).

**Why.** It closes A2's residual risk, and the adapter already consumes the flags.

**Also.** Extend the flag to `statmech.e0_kj_mol` and to kinetics runs (B5).

### B2. Per-species level under `adaptive_levels` (**wrong data**)

**What.** Export the opt, freq and sp level each species actually ran at, or at least a
run-level `adaptive_levels` marker in `output.yml`.

**Why.**

- Every calculation of an adaptive run is attributed to the run-level level
  (`ARC:arc/scheduler.py:1123`, `:5273`).
- The adapter can detect adaptive runs only indirectly, from `input.yml`/`restart.yml`
  (A2b). That is enough to refuse, not to deposit correctly.
- Do **not** use `spc.opt_level`; it is also stamped from the run-level value.

**Effort.** M.

### B3. Levels for scans, IRCs, conformers and TS guesses; versions for scans and IRCs

**What.** Pass `scan_level`, `irc_level`, the conformer levels and the TS-guess level to
`write_output_yml` (`ARC:arc/main.py:673`). Key `ess_versions` and `ess_software` for scan
and IRC jobs.

**Why.**

- Rotor scans without a level are skipped today.
- IRC and TS-guess calculations are mislabelled (A3).
- Screened conformers need their level (A4).

**Effort.** S–M. A3 rates each as trivial: one keyword argument, plus schema, docs,
fixture and a version bump.

### B4. The statmech treatment Arkane actually applied

**What.** Capture, during `process_arc_project`, whether Arkane kept or dropped the
rotors. It drops them when there is no force-constant matrix
(`RMG-Py:arkane/statmech.py:647-667`). `torsions[].treatment` is always `hindered_rotor`.

**Why.** This makes A5's field fillable without inference.

**Effort.** M. The value lives in Arkane's `output.py`, which is overwritten.

### B5. Correction markers on E0 and kinetics

**What.** An atom-correction flag on `statmech.e0_kj_mol` and on the kinetics run.

**Why.** It unblocks `thermo.enthalpy_formation_0k_kj_mol` (`ARC_LATENT`). Kinetics runs
use `bac_type=None`, so no BAC mismatch enters a barrier.

**Effort.** S.

### B6. Mark IRC endpoint species

**What.** Export an `irc_label` or a role on `IRC_<ts>_<n>` records (A17).

**Effort.** S.

### B7. Make the composite-method calculation visible

**What.** For CBS-QB3 or G4 runs only `paths['composite']` is set, and `output.yml` has no
log, deck, ESS version or spin diagnostic for it.

**Unverified.** What the adapter builds for a composite run. A6 shows at least one
consequence: atoms arrive with `converged: true`.

**Effort.** S, plus an adapter follow-up.

### B8. The atom map

**What.** The raw 0-based reactant→product map, the participant list, the `atom_to_ts`
reshaping, and provenance (declared or inferred) (`ARC:arc/reaction/reaction.py:158`,
`:331`, `:400`).

**Why.** Demand path: `reaction_upload.atom_map` (tier 3). Without it TCKDB warns
`reaction_atom_map_absent`, and it never derives a map (ADR 0011). Whether a deposited
reaction can be enriched later is the open question in `PRIORITY_POLICY.md`'s backfill
note.

**Unverified.** The TS atom order for AutoTST, GCN, KinBot and GoFlow.

**Effort.** S for the raw map; M for the participant→TS shape.

### B9. TS frequency order and the reaction-mode index

**What.** Export `spc.freqs` in ESS order, and the index of the reaction-coordinate mode.
Today the adapter re-inserts the TS negatives (`adapter.py:4660-4684`) and designates the
mode by a frequency window.

**Why.** Demand path: `…freq_reaction_coordinate_mode_index`, required when `n_imag > 1`.

**Effort.** S.

### B10. IRC endpoint→participant mapping

**What.** `ARC:arc/checks/ts.py:581` and `:670` compute exactly the
`validation_evidence[]` participant mappings, then discard them.

**Effort.** M, because the writer cannot reach the value today.

### B11. Cheaper latent exports

| Export | Demand path | Effort |
|---|---|---|
| T1 diagnostic (parsed into `spc.t1`, then dropped) | `…wavefunction_diagnostic.t1_diagnostic` | S |
| Effective ESS route keywords | `…parameters[].raw_key` | M |
| Kinetics provenance: `comment`, `ts_validation`, `pressure_context`, `degeneracy_convention` | `reaction_upload.kinetics[].*` | S |
| `reversible`, from the reaction arrow | `reaction_upload.reversible`, `ts_upload.reaction.reversible` | S |
| Rotational constants | `…statmech.rotational_constant_{a,b,c}_cm1` | M |
| Statmech and kinetics source calculations from the Arkane species files | `…statmech.source_calculations[]` | M |
| Stereo kind and label | `…species_entry.stereo_*` | M |
| Dipole and polarizability | `conformer_upload.transport.*` | M |
| Isotopes, as a new field from the species definition | `…geometry.isotopes` | L |

The execution environment accounts for 293 absent leaves. ARC observes almost nothing
about its runtime; do not export requested resources as runtime facts.

### B12. Export the RMG-database commit behind Arkane's correction tables

**What.** Export the RMG-database commit that supplied Arkane's correction tables
(`RMG_DB_PATH/input/quantum_corrections/data.py`, `ARC:arc/statmech/arkane.py`
~817-821), for example into `WorkflowToolReleaseRef.notes` or a dedicated field.

**Why.** `arkane_git_commit` is the RMG-Py HEAD (`ARC:arc/output.py` ~348-366), not the
database's. The adapter's `scheme.workflow_tool_release` (0.6.3) therefore cannot
tell a database-only table revision from no change: it collides with the stored
scheme (parameter-conflict 422), while each new RMG-Py commit needlessly creates
a new scheme row.

**Effort.** S.

### B13. Export the Petersson components Arkane did apply

**What.** When some bonds lack Petersson parameters, export the components Arkane did
apply (they sum exactly to the applied total) instead of dropping the whole list
(`ARC:arc/output.py` ~1592-1596).

**Why.** Today a partial table makes the adapter omit the BAC (A1) although a
complete, exact decomposition of the applied total exists.

**Effort.** S.

### B14. Two ARC bugs found while wiring levels

- **`neb_level` is missing in default-config runs.** `main.py:670` calls
  `resolve_neb_level(self.ts_adapters)`, which is `None` when the user did not list
  `ts_adapters`, although the scheduler falls back to `default_ts_adapters`
  (`scheduler.py:410`), which include `orca_neb` (`settings.py:122`). Default-config NEB
  guesses therefore get no `neb_level` in `output.yml`. Fix:
  `resolve_neb_level(self.scheduler.ts_adapters)`.
- **`xtb_gsm` energies are suspect for charged and open-shell species.** Its `ograd`
  runs `xtb --grad --chrg 0` with no `--gfn` or `--uhf`: the method is unstated and the
  charge is hard-coded 0, so the GSM energies of charged or open-shell species (and the
  method of any GSM path) cannot be trusted or stated.

### B15. Export the correct rigid-rotor kind

**What.** ARC exports `rigid_rotor_kind` only as `atom`, `linear` or `asymmetric_top`,
so benzene (D6h, an oblate symmetric top) is exported as `asymmetric_top`. Export the
correct kind, including symmetric tops, from the principal moments.

**Why.** The adapter forwards ARC's value, so TCKDB holds a wrong rotor kind for every
symmetric top.

**Effort.** S.

---

## C. TCKDB schema and backend work

### C1. T4: writes commit after the 201 is sent (**high; wrong state**; top 10 #5)

**What happens.** `get_write_db` commits in the teardown of a yield dependency
(`TCKDB:backend/app/api/deps.py:123-150`). Under FastAPI 0.135's request-scoped
teardown, that runs after the response is sent. So:

- an immediate GET can return 404;
- a commit failure can follow a 201 the client already has.

This was verified by 4 of 4 failing fresh-database runs, and it is written up in the
committed gate document.

**Consequence for the adapter.** It records `status: uploaded` and the idempotency record
from the 201, so it can believe, and write in its sidecar, that a deposit exists when it
does not. Later steps that use the returned calculation IDs, such as the artifact upload,
inherit that.

**Fix.** Commit before returning, or use function-scoped teardown. The committed gate
polls on read-back (`conftest.py:59-116`), so it is not affected; production producers
are.

**Effort.** M.

### C2. T2: refused uploads add a failed submission row on every retry ([gate])

**What happens.** The durable failure audit (`record_failed_upload`,
`TCKDB:backend/app/services/upload_submission.py:288`; called from `deps.py:143`) writes
one row per refused attempt, so idempotent retries grow the table.

**Effort.** S–M.

### C3. T3: the artifact route has no `submission_record_link`, and the client returns the body only ([gate])

**Client half (re-derived from code).** In batch mode, `upload_artifacts` stores only the
parsed body of each `post_json` call (`TCKDB:clients/python/src/tckdb_client/client.py:1137-1149`).
`ArtifactUploadBatchResult` carries that body verbatim (`client.py:183-198`). The HTTP
status, request ID, replay flag and per-item warnings are not exposed, which explains A8.

**Route half (live-run observation only, not re-derived from code).** The claim that
the artifact route writes no `submission_record_link` comes from the gate's live run. The
route *reads* such a link for ownership (`TCKDB:backend/app/api/routes/calculations.py:634-636`),
but I did not trace what it writes.

**What to change.** Expose the response envelope from `upload_artifacts`.

**Effort.** S.

### C4. T1: `backend/scripts/dev_login.sh:91` fails on Python 3.11–3.14 ([gate])

**What happens.** The embedded Python writes `f"{data[\"username\"]} …"`. A backslash
inside an f-string expression is a SyntaxError.

**Effort.** S.

### C5. Level-of-theory method aliases (**duplicate identity**; [gate]; top 10 #9)

**What happens.** `wb97x-d` and `wb97xd` became separate level rows. The spellings appear
in the adapter's corpora: `tckdb_arc/tests/fixtures/arc_1_2/output.yml:20-22` (a
hand-assembled fixture) against `golden/phase3_output.yml:5`. TCKDB dedupes levels by
hashing the method string as sent, with no alias table
(`TCKDB:backend/app/services/calculation_resolution.py:122-147`).

**What to change.** Canonicalize aliases on write, or refuse unknown spellings with a
hint. The adapter should keep sending ARC's string verbatim.

**Effort.** M, including a backfill of the duplicate rows.

### C6. Atoms and the computed-species primary opt ([gate])

**What happens.** The primary calculation must be type `opt`
(`TCKDB:…/computed_species_upload.py:327`).

**What to change.** Provide a form for a species that had no optimization (A6).

**Effort.** M.

### C7. TS validation evidence accepts only `kind: 'irc'`

**What happens.** `ts_validation_evidence.py:58` is `Literal["irc"]`, so ARC's E0,
e_elect, freq and NMD verdicts have no home.

**Effort.** M.

### C8. Schema homes for ARC data that has none (138 `SURPLUS` rows)

Highest value first:

- the kinetics reference temperature `T0` (the adapter normalizes `a = A/T0^n` correctly,
  but the reported T0 is lost);
- TS statmech;
- tunneling application and interpretation fields;
- `statmech.e0_kj_mol` with its correction marker;
- rejected-rotor pivots and barriers;
- constraint `target_value_units`;
- IRC and GSM gradient units.

### C9. Standalone-TS route asymmetries

`ts_upload` has three:

- no `atom_map`, so it always warns `reaction_atom_map_absent`;
- no slot for TS applied corrections;
- the async `/jobs/transition-state` route drops every warning.

Separately, `conformer_upload` runs no provenance warnings for its nested statmech.

### C10. Correction-scheme provenance for tool-table schemes ([gate])

**Status (adapter 0.6.1):** `scheme.software` now names the program in the record's
`matched_arkane_key` (`gaussian`), without a release, and is omitted when the key names
none or disagrees with ARC's level; the release and literature remain open.

**What happens.** `scheme.software` is the program release that computed a scheme's
parameters. Arkane's database does not record that release, so no ARC deposit can fill
it honestly. Every new scheme then warns `missing_energy_correction_scheme_software`, and
`ambiguous_energy_correction_scheme_without_literature` when it has no citation.

**What to decide (with D3).** Either an explicit "tool table, release unknown" state, or
identify the scheme through `workflow_tool_release` = Arkane/RMG.

**Effort.** S–M.

### C11. The bundle kinetics warning names the wrong field

**What happens.** `missing_software_release_provenance` names `software_release` but checks
`analysis_software_release`. A-units are not checked against molecularity on the bundle.

**Effort.** S.

### C12. Verify the deployed TCKDB version

**What happens.** #560 (client 0.93), #565 (client 0.94) and #566 postdate schemas 0.51.0
(`2a32f694`, 2026-09-27). The adapter pins `tckdb-client>=0.93,<0.94` and
`tckdb-schemas>=0.51,<0.52`.

**What to do.** Confirm what the Pi runs before relying on "filled from artifact" or on the
workflow-tool refusal. None of these changes the adapter's payloads today.

---

## D. Decisions for you

### D1. Enthalpy on pre-1.2 ARC output — **decided: option (d)**

| Option | What it does |
|---|---|
| (a) | Magnitude guard only (the behaviour at `0913124`) |
| (b) | Fail closed on every flagless output |
| (c) | (b), with a per-run override |
| **(d), chosen** | **Targeted fail-closed rules**: strip enthalpy when the flag is absent **and** the species is light by composition (from the xyz, else `formula`: hydrogen-only, He, He2, HeH, the Li atom), or the header `arkane_level_of_theory` mismatches the energy level, or either level sets a separate `dispersion`/`solvation_method` |

Option (d) is committed as `84b1b05` on `fix_unverifiable_enthalpy_without_flags` (adapter 0.5.0) and is pending merge (A2). In the golden corpus only H2 loses its enthalpy, so the integration gate's `test_golden_species_calculations_thermo_and_hessian` must be updated by whichever branch merges second.

**Residual risk accepted.** Stand-ins that agree on method and basis but differ by a year
refit, or through ARC's fuzzy key match, still pass as `formation_298k` until ARC's 1.2
flags (B1) are present.

### D2. Deposit rights

**What TCKDB does.** `…rights` on every root (15 leaves, all `ARC_ABSENT`) is silent at
upload. But a dataset release refuses records without a rights basis
(`TCKDB:backend/app/services/release/curation.py`), and
`depositor_attests_right_to_license` must be literally `true`.

**What you decide.** The license, the source terms, and whether the adapter may attest on
your behalf. Nothing is inferred today.

### D3. Constants that make provenance claims

| Claim | Where | Options |
|---|---|---|
| `quality: raw` on every calculation | `adapter.py:3447` | Keep, or omit |
| `species_entry_kind: minimum`, `electronic_state_kind: ground`, `molecule_kind: molecule` | `:3209-3258` | Keep (ARC deposits ground-state minima; TS records are refused) |
| `reference_pressure_bar` = 1.01325 when ARC did not record it | `:5132-5165` | Keep (RMG hard-codes 1 atm) |
| `depends_on` edges and freq/sp `input_geometries` = the optimized geometry | `:1158-1265`, `:1614-1621` | Keep; they are ARC's workflow invariant |
| `opt_coarse` `converged: true`; `path_search_result.converged`, `is_double_ended`, `source_endpoint_count: 2` | `:4435`, `:7749`, `:486-489` | Recommend omitting `converged` where ARC exported no flag |
| IRC TS marker point synthesized; `direction: both` when unresolved | `:7222-7244`, `:7205-7212` | Mark the point; omit `direction` when unknown |
| Imaginary mode designated by a (75, 10000) cm⁻¹ window | `:4462-4503` | Keep until B9 |
| `scale_kind: fundamental`; FSF `software.name` = the opt software | `:5843`, `:5851-5856` | Confirm with ARC's FSF source |
| `model_kind: modified_arrhenius`, `a_uncertainty_kind: multiplicative` | `:6742`, `:6928` | Keep |
| `ts_upload.reaction.reversible: true` (always) | `:3175` | Replace with B11 |
| Kinetics `T0 = 1 K` when absent | `:6797` | Keep |
| Freq/sp level falls back to `opt_level`; software falls back to the requested level's | `:4383-4404`, `:3400` | Keep for freq/sp; scan is already refused |
| Correction scheme `software` / `workflow_tool_release` | `software` = `{name: <matched_arkane_key's software>}` since 0.6.1; `workflow_tool_release` = Arkane (recorded version/commit) on atom-energy, Petersson and Melius schemes since 0.6.3 | See C10. **Never Arkane as `scheme.software`, never ARC as `scheme.workflow_tool_release`** (MR-2, AP-8) |

### D4. Which upload modes to run

Computed-species and conformer modes overlap; decide whether conformer mode stays (A13).
Also decide whether `allow_partial_uploads` may deposit reaction species without their TS.

### D5. IRC endpoint species

Until B6 lands, pick one: skip `IRC_*` labels (a heuristic), deposit them as wells
(today's behaviour), or deposit them with a note.

---

## E. Test and fixture gaps

### E1. No real-ARC fixture exercises most of the export surface

**What the fixtures are.**

- ARC's golden `output.yml` is 18 lines, and `current_arc` builds no payload.
- `arc_1_2` is hand-assembled.
- Only the Phase-3 golden corpus derives from a real run.

**What no real ARC output covers.** Statmech and torsions, kinetics, rotor scans and
constraints, AEC/BAC records, spin diagnostics, `scf_reference`, `wavefunction_stability`,
`ts_checks` (so `ts_checks.IRC` never appears), artifacts, coarse opt, the Melius table,
and a componentless BAC.

**What the inventories show.** 272 A4 rows are populated only synthetically, and 209 by no
payload at all.

**What is needed.** One real project per mode: a thermo run with rotors and BAC, a
kinetics run with IRC and GSM, and an adaptive-levels run.

### E2. Chemically invalid synthetic fixtures

**Known.** The synthetic ethanol shipped with a two-atom geometry, which TCKDB refuses
with a 422.

**Next.** Audit the synthetic corpora against TCKDB's composition, mode-count and
TS = Σ reactants checks. This audit did not enumerate them.

### E3. Missing negative tests

- A1: a componentless BAC on a bonded species, a TS, and a partial decomposition.
- A2: H2, a mismatched `arkane_level_of_theory`, and dispersion on 1.1 output.
- A4 and A6: alternative conformers and atoms, including composite atoms.
- A2b: an adaptive-levels project.
- A11: a declared energy level with an `scf_reference` sp.
- A18: the workflow-tool guard.

### E4. The GSM stringfile tests skip without `ARC_GSM_STRINGFILE_FIXTURE`

Commit a small real stringfile.

(The first draft's E3 and E4, which called the gate's `irc_converged` xfail mis-specified
and its read-back a sleep, are withdrawn. The committed gate keys its xfails on
`ts_checks.IRC` (`test_live_tckdb.py:399-420`) and polls (`conftest.py:59-116`).)

---

## Already correct

These results come from the live gate against an isolated TCKDB at 0.51, and from A4's
payload validation. All 24 fixture payloads validate, as do 519 of 521 captured payloads;
the two failures are deliberate negative tests.

- **Links and ownership.** Calculation owners and types; thermo, statmech and kinetics
  source-calculation roles and owners; conformer anchors.
- **Thermo state.** `formation_298k` only when the 1.2 flags prove it, and
  `reference_pressure_bar` from ARC's recorded pressure. On 1.2 output, unprovable
  enthalpies are stripped with a producer warning.
- **AEC/BAC.** Totals, units (never guessed), components and the `sp` source calculation.
  The Hessian is stored in its own frame. IRC and GSM results use geometry-matched
  evidence only.
- **Kinetics.** `a = A/T0^n` (checked for rate equivalence). Unknown units drop value and
  unit together. TS composition equals Σ reactants.
- **Idempotency.** A replayed sweep leaves every count unchanged.
- **Provenance discipline.** The ESS name is the observed per-job program, and versions are
  never borrowed across programs. The spin treatment appears only on the jobs it was
  observed on. A scan without a level is skipped. No calculation is named ARC, Arkane or
  RMG. `irc_converged` is not treated as IRC validation.
- **0.51 shape.** No payload key is outside the schema, and no scalar is an explicit null.
  The 0.22 `BundleThermoIn.source_calculations` 422 is gone.
- **Matrix.** 524 `WIRED` leaves, meaning the key exists; the value is not proven, as A3
  shows. There are also 283 `SOURCE_UNCONFIRMED` leaves, by source:
  - 153 direct `output.yml` reads;
  - 109 adapter constants (D3);
  - 19 on-disk artifact hashes;
  - 2 sidecar reads.

---

## What this roadmap could not establish

- Whether the deployed TCKDB includes #560, #565 or #566 (C12).
- A real level for a GSM (xTB) TS guess (A3), and what the adapter builds for composite
  runs beyond A6's atom case (B7).
- Whether Arkane's correction tables live in RMG-Py or RMG-database (C10, D3).
- The `followed_to_stable` → `stabilized` mapping (A10), which is A2's proposal.
- Whether TCKDB's async routes store a composite ESS version verbatim (A7).
- The full list of chemically invalid synthetic fixtures (E2).
- The 57 `ADAPTER_GAP` rows flagged `arc_supply: unconfirmed`. They are adapter gaps only
  if the adapter's source at the sibling route is real ARC data.
- The medium- and low-confidence rulings in `ROUTE_ADJUDICATION.md` §7.5, in particular
  treating `conformer_upload.calculation` as the primary opt.
