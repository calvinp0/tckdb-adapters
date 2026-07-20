# ARC main re-home audit

**Status:** decision-grade audit; no implementation authorized  
**Audit date:** 2026-07-20  
**Decision:** re-home a small set of ARC-owned result producers onto clean ARC
`main`; do **not** merge `tckdb-imp`, and do **not** restore `arc/tckdb/` on
`main`.

## 1. Executive decision

The separation is technically sound, but it is not ready for Phase 4 from a
clean ARC `main` yet.

ARC `origin/main` already owns a useful consolidated `output.yml` producer.
The standalone package owns extraction, payload construction, validation,
upload, and replay. The missing work is to bring the generally useful ARC-side
producers that the standalone consumer relies on from `origin/tckdb-imp` to a
small, reviewable series based on `origin/main`.

The target seam is:

```text
ARC scientific state + ESS artifacts
              |
              v
 output/output.yml 1.1 + output/tckdb_evidence.json 1.0
              |
              v
 standalone tckdb_arc: validate -> consume -> build payload -> upload/replay
```

The deletion test governs every decision: after deleting `arc/tckdb/`, does
the ARC change still describe, preserve, or parse a scientifically meaningful
ARC result? If yes, it belongs in ARC. If it only maps that result to TCKDB,
performs readiness/upload behavior, or writes TCKDB payload files, it belongs
in `tckdb-adapters`.

The minimum safe path is hand extraction by functional slice. A wholesale
merge of `tckdb-imp` is prohibited: the branch is 45 commits and 88 changed
paths beyond `origin/main`, mixing the adapter, result production, TS work,
scheduler/SSH work, Molpro fixes, mapping changes, and benchmark-related work.

## 2. Audited references and confidence

Existing local remote-tracking references were inspected:

| Repository/ref | Audited commit | Meaning |
|---|---:|---|
| ARC `origin/main` | `59ac3f01` | clean upstream base |
| ARC `origin/tckdb-imp` | `5093deb9` | integrated proof branch, including corrected Phase 3 producer |
| ARC merge base | `59ac3f01` | `tckdb-imp` contains all of this `main`, then 45 additional commits |
| standalone `main` / `origin/main` | `6a392fd` | PR #1 merge; contains the corrected Phase 3 consumer and documentation |
| standalone Phase 3 commits | `8adbeb8` + `94ba441` | original consumer plus required parity/isolation correction, both ancestors of main |

`git fetch origin` could not update ARC's `.git/FETCH_HEAD` in the current
read-only Git metadata environment. Therefore “current” in this document means
the remote-tracking refs above, not a newly fetched server state. Reconfirm the
SHAs immediately before opening implementation branches.

The corrected standalone Phase 3 consumer is proved and merged to standalone
`main`. The ARC producer remains proved on ARC feature/integration branches,
not yet an upstream-ready clean-main series.

## 3. Classification summary

### ALREADY IN MAIN

ARC `origin/main` already produces the core contract in `arc/output.py`:

- atomic `output/output.yml` writing and schema `1.0`;
- project, ARC/Arkane version and git provenance;
- run levels (`composite_method`, `opt_level`, `freq_level`, `sp_level`,
  `neb_level`, `arkane_level_of_theory`), frequency scale-factor provenance,
  BAC type, and run-level AEC/BAC tables;
- species, transition-state, and reaction collections;
- identity, charge/multiplicity, final geometry, convergence, SP/ZPE/frequency
  summaries, primary log paths, ESS versions, TS method/NEB/IRC provenance,
  thermo, statmech, torsions, kinetics, and labels joining reactions to species
  and TS records.

These are retained and extended. They must not be recopied from `tckdb-imp` as
one replacement file.

### MUST RE-HOME TO ARC MAIN

These are required to preserve the proven standalone contract without
`arc/tckdb/`:

1. The consumed calculation-provenance subsets of `ca7ce141`, split into
   small commits: input paths/geometries/results/final settings; constraints;
   rotor scan results; thermo points and applied energy corrections; richer
   IRC and reaction metadata.
2. Hessian parser support from `ccb24732`.
3. GSM stringfile energy parsing/alignment from `6aef917f`.
4. ARC-side path-search provenance preservation from `cea70fb5`, the small
   relevant parts of `ff7a5762` and `530b9b8c`, and `e48b258c`.
5. The `ograd` executable/preservation fix from `c1a579a4` plus the narrowly
   required xtb-GSM artifact-preservation behavior found in `4ae2bf27`.
6. Spin-diagnostic parser and `output.yml` producer portions of `1901cfcd`.
7. Corrected Phase 3 producer from `b2ba7413` + `5093deb9`, rebased and
   hand-integrated after its parser prerequisites.
8. A clean-main standalone import shim in `ARC.py`, added only after the
   standalone package and clean-main output producer are installable. The
   existing `6efc4e12` shim cannot be cherry-picked unchanged because clean
   `main` has no `arc/tckdb` fallback package.

### SHOULD RE-HOME SEPARATELY

These are real ARC improvements but are not prerequisites for the separation.
They deserve independent review and must not hitchhike on the re-home:

- Arkane stderr/output recovery portions of `ca7ce141` and `1484b545`;
- remote lifecycle and SSH pooling (`a63a1fe4`);
- general TS troubleshooting and displacement work in `4ae2bf27` not needed
  for GSM evidence preservation;
- TSGuess `level` propagation and CREST (`822e8fc0`, `ff7a5762`) beyond the
  minimum provenance fields required by output/evidence;
- monoatomic geometry/E0, AutoTST, family reversal, mapping, heuristics,
  Molpro, and other chemistry/scheduler fixes on `tckdb-imp`.

### STAYS IN TCKDB-ADAPTERS

- `tckdb_arc.adapter`, `sweep`, `config`, CLI, constraints serializer,
  idempotency, payload writer, optional-ARC fallback, vendored helpers, and
  evidence loader/validator;
- all TCKDB schema mapping, enum normalization, payload DAG composition,
  artifact upload, readiness retry, partial-reaction salvage, computed-TS
  mode, idempotency, payload archival, and transport behavior;
- adapter-only portions of `3f58df6a`, `cbe69320`, `3ac593a1`, `9acbca2d`,
  `4f8d6b26`, `1c65289c`, `102465f7`, `1a2c8f27`, `e48b258c`, and
  `1901cfcd`;
- the Phase 3 evidence consumer and golden/parity tests (`8adbeb8` plus
  correction `94ba441`).

### DROP WITH `arc/tckdb`

- `c4fe12ad` (the original in-tree upload package), `ee0be100` (old in-tree
  wiring), all `arc/tckdb/**`, and ARC-side adapter tests/docs that exist only
  to describe that package;
- `arc/tckdb` dependency declarations. Do not add `tckdb-client` or
  `tckdb-schemas` to ARC. The installed standalone adapter is an optional
  integration, not an ARC scientific-core dependency.

### UNRELATED BENCHMARK WORK

Anything in the 45-commit branch not named in the required slices is excluded
unless it independently earns its own ARC PR. This includes scheduler/pipe/SSH
hardening, Molpro work, mapping fixes, AutoTST/heuristics work, processor logs,
and benchmark-specific branch integrations. “Useful” does not make it part of
this re-home.

## 4. What the standalone consumer actually reads

The following is the contract inventory from the corrected standalone feature
implementation. “Main status” refers to ARC `origin/main` at `59ac3f01`.

| Consumer surface | Fields consumed | ARC producer and main status |
|---|---|---|
| Document identity | `schema_version`, `project`, `arc_version`, `arc_git_commit` | `write_output_yml`; present as schema 1.0 |
| Levels/releases | `opt_level`, `freq_level`, `sp_level`, `arkane_level_of_theory`, `freq_scale_factor`, `freq_scale_factor_source`, `ess_versions` | present; preserve `Level.args` prerequisite where used |
| Record identity | `label`, `original_label`, `is_ts`, `smiles`, `unmapped_smiles`, `charge`, `multiplicity`, `xyz`, `converged` | substantially present |
| Geometry lineage | `conformers`, `conformer_energies`, `opt_input_xyz`, `coarse_opt_input_xyz`, `coarse_opt_output_xyz` | expanded semantics are in `ca7ce141`; must extract |
| Calculation files | `opt_log`, `coarse_opt_log`, `freq_log`, `sp_log`, `opt_input`, `freq_input`, `sp_input` | logs present; input-deck paths are `ca7ce141` |
| Opt results | `opt_converged`, `opt_n_steps`, `opt_final_energy_hartree`, `coarse_opt_n_steps`, `coarse_opt_final_energy_hartree` | basic fields present; robust/general parsing changes in `ca7ce141` |
| Freq/SP results | `freq_n_imag`, `imag_freq_cm1`, `zpe_hartree`, `sp_energy_hartree`, `statmech.harmonic_frequencies_cm1` | present; imaginary-mode payload fix stays standalone |
| Spin | `sp_spin_diagnostic.{s_squared, s_squared_expected, s_squared_annihilated, note}` | absent; parser/output slice of `1901cfcd` required |
| Constraints/settings | `opt_constraints`, `freq_constraints`, `sp_constraints`; `opt_final_settings`, `coarse_opt_final_settings`, `freq_final_settings`, `sp_final_settings`, `irc_final_settings` | absent; `ca7ce141` required |
| Alternate calculations | `additional_calculations[].{type,key,scan_result,constraints}` | absent/incomplete; `ca7ce141` scan slice required |
| Corrections | `applied_energy_corrections[]` and its scheme/value/components/LoT provenance | absent; `ca7ce141` correction slice required |
| Thermo/statmech | `thermo` including `thermo_points`; `statmech` including frequencies, symmetry, optical isomers, torsions | base blocks present; thermo points from `ca7ce141`; optical-isomer mapping stays standalone |
| TS attribution | `chosen_ts_method`, `ts_guesses[].{chosen,method_sources}`, `neb_log`, `gsm_log` | NEB/basic method present; normalized/preserved provenance and GSM path require the listed path commits |
| IRC | `irc_logs`, `irc_log_directions`, `irc_converged` | logs/convergence present; directions and rich parser path require `ca7ce141` |
| Reaction | `label`, `reactant_labels`, `product_labels`, `ts_label`, `multiplicity`, `reversible`, `family`, `kinetics`, `long_kinetic_description` | base reaction shape present; tunneling/source description enrichment in `ca7ce141` |
| Evidence descriptor | `tckdb_evidence.{path,schema_name,schema_version,document_id}` | absent; corrected Phase 3 producer required; bumps output to 1.1 |
| Evidence values | keyed by `(record_kind,label)`: `freq_hessian`, `irc`, `gsm` availability envelopes | absent; produced by `arc/tckdb_evidence.py` after Hessian/IRC/GSM prerequisites |

The adapter also reads artifact paths to upload logs/input decks and may use
legacy ARC parsers when evidence is absent or invalid. Those fallbacks are for
old runs; they are not permission to omit the new producer fields.

## 5. Explicit audit of `ca7ce141` (17 files)

`ca7ce141` is not cherry-pickable as a unit. Its 4,258 additions combine
contract production, new scientific parsers, Arkane behavior, scripts, tests,
and documentation. Split it as follows.

| File | Functional slice | Decision |
|---|---|---|
| `arc/output.py` | job input paths, input/output geometries, parsed opt result, constraints, final settings, GSM/IRC provenance, scan calculations, corrections, thermo/reaction enrichment | **MUST**, hand-extract by slice after rebasing against current main |
| `arc/output_test.py` | producer tests for the above | **MUST**, split with each slice |
| `arc/parser/adapter.py` | absolute-Hartree scan parser interface | **MUST** for scan-result production |
| `arc/parser/adapters/gaussian.py` | shared scan walk, absolute scan energies, rich IRC, Gaussian held constraints | **MUST**, preferably two review units: scan/IRC and constraints |
| `arc/parser/adapters/orca.py` | ORCA held constraints | **MUST** with constraints slice |
| `arc/parser/constraints_test.py` | Gaussian/ORCA constraint coverage | **MUST** with constraints slice |
| `arc/parser/parser.py` | `parse_irc_path`, `parse_1d_scan_full_result`, trajectory normalization | **MUST** with respective parser slices |
| `arc/parser/parser_test.py` | parser-facade/full-scan tests | **MUST** with parser slices |
| `arc/reaction/reaction_test.py` | reaction serialization/tunneling expectations | **MUST only for retained reaction fields**; hand-extract tests |
| `arc/scripts/get_species_corrections.py` | Arkane-backed per-species AEC/BAC totals | **MUST** if current payload parity is an acceptance criterion; otherwise a separately gated optional slice |
| `arc/scripts/save_arkane_thermo.py` | complete per-temperature thermo points | **MUST** for current payload parity |
| `arc/scripts_test.py` | script contract tests | **MUST** for extracted correction/thermo behavior |
| `arc/species/species.py` | `ThermoData.thermo_points` plus an unrelated monoatomic initializer change | **MUST** thermo-points portion; **SHOULD separately** monoatomic portion |
| `arc/species/species_test.py` | tests for both mixed changes | split: thermo **MUST**, monoatomic **SHOULD separately** |
| `arc/statmech/arkane.py` | correction/tunneling/thermo propagation mixed with stderr recovery | correction/tunneling/thermo **MUST**; stderr policy **SHOULD separately** |
| `arc/statmech/arkane_test.py` | mixed Arkane tests | split with the owning slices |
| `docs/output_yml_schema.md` | documents expanded general ARC result contract | **MUST**, update again for output 1.1/evidence descriptor |

The correction script is the heaviest judgment call. It passes the deletion
test because it records corrections ARC actually applied, but it adds an
Arkane subprocess path. If maintainers decline it, mark applied correction
detail unavailable and explicitly weaken parity; do not silently fabricate it
in `tckdb_arc`.

## 6. Focused commit audits

### Hessian — `ccb24732`

This adds Gaussian and ORCA Hessian parsing, tests, ORCA fixture files, and
facade integration. Hessians are general scientific results and pass the
deletion test. Hand-extract the parser methods, facade registration, tests,
and minimal fixtures. Do not include any adapter code. Land before the Phase 3
producer, whose `_build_hessian` calls this surface.

### GSM energies/alignment — `6aef917f`

The parser function `parse_gsm_stringfile_energies` and its tests belong in
ARC. The adapter-side path-search conversion remains standalone. Extract the
parser addition and any trajectory alignment correction needed for stable
source indices; verify against endpoint and energy-less-node cases.

### Path provenance — `e48b258c` and prerequisites

The ARC-side requirement is to retain method sources when a geometry-only
duplicate wins and to serialize the chosen method/log honestly. This spans
`arc/species/species.py`, `arc/scheduler.py`, and `arc/output.py` with tests.
The `arc/tckdb` changes in `e48b258c` stay standalone.

Prerequisites are entangled:

- `cea70fb5`: normalize `TSGuess.method` sources — small and suitable to
  cherry-pick or recreate.
- `530b9b8c`: scheduler attribution and ORCA-NEB level correction — extract
  narrowly.
- `ff7a5762`: adds `TSGuess.level` but also a 522-line CREST adapter. Do not
  cherry-pick. Extract only the data-model/serialization and participating
  adapter assignments actually required by provenance.

### Preserved GSM artifacts — `4ae2bf27`, `c1a579a4`, `1c65289c`

- `c1a579a4` is a small ARC runtime fix: preserve executable mode for `ograd`
  and ensure xtb-GSM can invoke it. Cherry-pick is plausible only after checking
  the file mode and current xtb-GSM context; otherwise recreate the three-line
  change and test.
- `4ae2bf27` contains the relevant xtb-GSM `ograd`/artifact troubleshooting
  changes amid 900 lines of unrelated troubleshooting. Hand-extract only what
  is necessary to leave source node outputs/stringfile available to the
  evidence producer.
- `1c65289c` is adapter-only parsing of preserved `.xtbout` node files. It
  stays in standalone; it is not proof that ARC itself preserved those files.

Acceptance must exercise a real xtb-GSM output directory and prove the
producer can see the stringfile and node outputs after job completion.

### Spin diagnostic — `1901cfcd`

Extract `ESSAdapter`/Gaussian/ORCA/Q-Chem parser methods, parser facade,
`arc/output.py` emission, and their tests. Leave `arc/tckdb/adapter.py` and its
tests in standalone. This is a clean example of the ownership seam: ARC emits
native S² facts; standalone shapes `spin_diagnostic` for TCKDB.

### Phase 2 shim — `6efc4e12`

Do not cherry-pick. It catches `ImportError` and falls back to `arc.tckdb`,
which clean `main` does not contain. The clean-main end state is a guarded
optional import that disables the TCKDB integration with one actionable log
when `tckdb_arc` is absent. Catch only the missing optional package, not an
arbitrary transitive `ImportError` raised inside a broken installation.

### Phase 3 producer — `b2ba7413` + `5093deb9`

Treat these as one corrected logical change, but review `arc/output.py` wiring
separately from `arc/tckdb_evidence.py` and its tests. The producer lives at ARC
top level, imports no TCKDB package, writes evidence first then output, binds
the pair by `document_id`, and leaves `output.yml` valid when evidence creation
fails. `5093deb9` is mandatory: it fixes XYZ parity, per-IRC-source isolation,
per-record poisoning, and real-producer parity coverage.

`5093deb9` is mandatory for the ARC producer: it fixes XYZ parity,
per-IRC-source isolation, and unexpected per-kind builder isolation. The
separate standalone correction `94ba441` fixes per-record consumer isolation
and adds the real producer-to-consumer final-payload parity test; it is already
merged in standalone `main`.

Hand-extract the ARC producer after all parser/provenance prerequisites.
Direct cherry-pick onto main is unlikely to apply cleanly because its
`arc/output.py` parent is the fully expanded `tckdb-imp` producer.

## 7. Minimal clean-main patch sequence

Each numbered item is an independently reviewable unit. Later units depend on
earlier arrows; no unit may include `arc/tckdb/**`.

```text
1 output baseline tests/docs
  |\
  | +--> 2 constraints + inputs/results/final settings
  | +--> 3 scan + rich IRC + thermo/corrections
  | +--> 4 Hessian parsers
  | +--> 5 GSM parser + artifact preservation
  | +--> 6 TS provenance
  | +--> 7 spin diagnostics
  \-------------------------------> 8 corrected evidence producer
standalone main 6a392fd -> package/release -----> 9 optional ARC shim
2..9 + real local run ---------------------> Phase 4 gate
```

1. **Freeze the current main contract.** Files: `arc/output.py`,
   `arc/output_test.py`, `docs/output_yml_schema.md`. Add characterization tests
   before changing fields. No behavior expansion.
2. **Calculation provenance.** Hand-extract `ca7ce141` input paths, geometry
   lineage, opt results, constraints and final settings with Gaussian/ORCA
   constraint parsers/tests.
3. **Scan, thermo, correction and rich IRC production.** Prefer separate PRs if
   maintainers want smaller scientific review. Exact files are the relevant
   `ca7ce141` parser/script/species/statmech/output files listed above.
4. **Hessian parsing.** Hand-extract `ccb24732` parser/facade/tests/fixtures.
5. **GSM parsing and preservation.** Hand-extract parser portions of
   `6aef917f`, runtime preservation from `c1a579a4` and the minimal xtb-GSM
   portion of `4ae2bf27`; include an artifact-lifetime test.
6. **TS path provenance.** Recreate the minimal `cea70fb5` -> `530b9b8c` ->
   `ff7a5762` subset -> ARC portions of `e48b258c` chain.
7. **Spin diagnostics.** Hand-extract ARC portions of `1901cfcd`.
8. **Evidence producer/output 1.1.** Rebase the corrected logical Phase 3
   producer (`b2ba7413` + `5093deb9`) onto units 1-7. Files:
   `arc/tckdb_evidence.py`, its test, and narrow `arc/output.py`/test/docs
   wiring.
9. **Standalone release then ARC shim.** Phase 3 commits `8adbeb8` + `94ba441`
   are already merged in standalone `main` at `6a392fd`. Package an identified
   version from that line, then add a minimal clean-main `ARC.py`
   optional-import integration. No in-tree fallback.

Cherry-pick candidates are limited to very small, context-independent changes
such as `cea70fb5` and possibly `c1a579a4`. Everything touching `arc/output.py`,
the parser facade, scheduler state, or mixed commits should be hand-extracted.

## 8. Tests and acceptance criteria

### Per-review-unit tests

- Parser units: unit fixtures for every supported ESS and malformed/missing
  input; finite-value, shape, and units assertions.
- Output units: exact record-field tests for species and TS, missing-artifact
  degradation, relative-path behavior, restart-restored provenance, and no
  operational secrets (`server`, queue, job id, credentials).
- Provenance: dedup winner/loser permutations, chosen method source, NEB/GSM
  log routing, restart serialization round trips.
- Evidence: deterministic strict JSON, atomic two-file protocol, generation
  mismatch, per-kind failure, per-record isolation, invalid XYZ/non-finite
  rejection, and no imports of `tckdb_arc`, `tckdb_client`, or
  `tckdb_schemas`.
- Shim: installed-package path, absent-package no-op, and broken-installed-
  package error propagation.

### End-to-end acceptance

Phase 4 is gated on all of the following:

1. Clean ARC main plus the review units passes its focused output/parser/
   scheduler/statmech suites and the normal ARC regression suite appropriate
   to those files.
2. Standalone main passes the full suite with ARC importable and with ARC
   deliberately blocked.
3. A fresh **local** ARC calculation produces `output.yml` 1.1 and a matching
   evidence 1.0 sidecar; standalone builds schema-valid species, reaction, and
   standalone-TS payloads with its ARC parser boundary poisoned.
4. Sidecar and legacy fallback payload subtrees are canonically equal for
   Hessian, IRC, and GSM, including corrected XYZ comments and node-energy
   precedence.
5. Deleting/renaming `arc/tckdb/` in a disposable test checkout does not break
   ARC result production or standalone consumption.
6. No ARC runtime or environment dependency on `tckdb-client`,
   `tckdb-schemas`, or the standalone package except the optional integration
   shim.
7. The merged standalone `main` at `6a392fd` is packaged as an identifiable,
   installable artifact/version (the merge itself is complete; release
   identification remains a Phase 4 gate).
8. One separately approved real integration smoke test is completed before
   destructive Phase 4 removal. Zeus may be a useful final deployment target,
   but it is not required to decide or implement this clean-main series and is
   not authorized by this audit.

## 9. Upstream plausibility and review boundaries

The following are plausible upstream ARC contributions because their names,
types, and tests can be explained without mentioning TCKDB: constraints,
calculation inputs/results, scan profiles, Hessians, IRC trajectories, GSM
path provenance, spin diagnostics, thermo points, and applied corrections.

The evidence filename retains `tckdb` in its name, but the module is still an
ARC-owned export of parser-native facts. Its upstream case is strongest only
after the underlying general result producers land. If ARC maintainers reject
a TCKDB-named sidecar in core, the acceptable alternative is a tool-neutral
`calculation_evidence.json` with the same parser-native contract; it is not
acceptable to move ARC parser execution back into the uploader.

Recommended review units are: output provenance, constraints, scan/IRC,
thermo/corrections, Hessian, GSM preservation/parser, TS provenance, spin,
evidence producer, and optional integration shim. This keeps scientific
parser review separate from integration policy.

## 10. Phase 4 non-goals and stop conditions

This audit does not authorize deletion, commits, pushes, PRs, deployments,
HPC access, scheduler submission, live uploads, dependency installation, or an
`AGENTS.md` change.

Stop Phase 4 if any of these remains true:

- clean ARC main cannot produce a matching output/evidence pair;
- no identified installable standalone release contains merged Phase 3;
- any golden path imports ARC when valid evidence is present;
- GSM source artifacts are not retained through job completion;
- correction/thermo parity was silently weakened;
- the clean-main shim still depends on `arc/tckdb`;
- the only proposed integration mechanism is merging `tckdb-imp` wholesale.

No new `AGENTS.md` rule is needed. The existing repository instructions cover
environment choice, focused tests, minimal diffs, and branch discipline; this
document provides the task-specific review and ownership rules.
