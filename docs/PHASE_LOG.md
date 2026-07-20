# tckdb_arc re-homing — phase log

Running log for the `arc/tckdb/` → standalone `tckdb-arc` re-homing. Newest
entries appended at the bottom. See `tckdb_arc_rehoming_plan.md` for the
authoritative plan.

---

## Phase 1 — create `tckdb-adapters` repo with `tckdb_arc` as a copy (2026-07-20)

**Status:** DONE. Base (no-ARC) test suite green; repo scaffolded; initial git
commit made (local only — no GitHub remote, no push).

### What was created (file inventory)

```
tckdb-adapters/
├── .gitignore
├── README.md
├── pyproject.toml                      # workspace/dev aggregator (NOT a package)
├── .github/workflows/ci.yml            # base leg runs; [arc] leg documented, not run
├── docs/
│   ├── tckdb_arc_rehoming_plan.md      # (pre-existing)
│   └── PHASE_LOG.md                    # this file
└── tckdb_arc/
    ├── pyproject.toml                  # name=tckdb-arc, CLI tckdb-arc-upload
    ├── tckdb_arc/                      # ── the package ──
    │   ├── __init__.py                 # re-exports TCKDBAdapter, TCKDBConfig, UploadOutcome, run_upload_sweep
    │   ├── config.py                   # ported; get_logger→_logging, local InputError(ValueError)
    │   ├── idempotency.py              # ported as-is (only wraps tckdb_client)
    │   ├── constraints.py              # ported; get_logger→_logging
    │   ├── payload_writer.py           # ported as-is (already ARC-free)
    │   ├── adapter.py                  # ported (~6.5k LOC); imports rewritten (see below)
    │   ├── sweep.py                    # ported; get_logger/read_yaml_file vendored; 1 DROP
    │   ├── cli.py                      # ported; read_yaml_file vendored
    │   ├── _logging.py                 # NEW  get_logger → logging.getLogger("tckdb_arc")
    │   ├── _vendor.py                  # NEW  E_h_kJmol, read_yaml_file, xyz_to_str (+deps), element dicts
    │   ├── _arc_optional.py            # NEW  guarded import arc boundary + lazy wrappers
    │   └── data/elements.yml           # vendored from ARC data/elements.yml (mass/isotope table)
    ├── testing/irc/                    # vendored IRC fixtures (rxn_1_irc_1.out, rxn_1_irc_2.out) for 1 arc-gated test
    └── tests/
        ├── conftest.py                 # puts tests/ on sys.path for cross-test helper imports
        ├── fixtures/golden/            # empty; TODO freeze a real arcbench output.yml here
        ├── test_config.py              ├── test_idempotency.py   ├── test_payload_writer.py
        ├── test_sweep.py               ├── test_cli.py           ├── test_adapter.py
        ├── test_ts_upload.py           └── test_golden_corpus.py # NEW contract test (plan §3)
```

8 modules + 7 test files ported from `arc/tckdb/`; 3 vendored shims + 1 golden
test + 1 data file + IRC fixtures added.

### Import-rewrite summary

- **Vendored into `_vendor.py`** (faithful copies of the real ARC defs):
  `arc.constants.E_h_kJmol`; `arc.common.read_yaml_file` (dropped the ARC-only
  `project_directory` globalization — callers only pass absolute paths);
  `arc.species.converter.xyz_to_str` + its transitive deps `check_xyz_dict`
  (dict-input branch only — string/Z-matrix branches rejected, never passed by
  the adapter), `xyz_from_data`, `get_most_common_isotope_for_element`, and the
  element dicts (`SYMBOL_BY_NUMBER`/`MASS_BY_SYMBOL`) built from the bundled
  `data/elements.yml` (copied from ARC `data/elements.yml`). Adds `numpy` +
  `PyYAML` runtime deps.
- **Vendored into `_logging.py`:** `arc.common.get_logger` → package logger
  `"tckdb_arc"` (replaces every eager `get_logger` in adapter/config/constraints/sweep).
- **Local exception:** `arc.exceptions.InputError` → `InputError(ValueError)`
  defined in `config.py` (used only for api-key/config file errors).
- **Behind `_arc_optional.py` (the `[arc]` extra):** the three parser-coupled
  paths, via lazy wrappers that raise `OptionalArcUnavailable(ImportError)` at
  *call* time (caught by each caller's existing try/except → sub-payload omitted):
  - freq-Hessian: `arc.parser.parser.determine_ess` + `arc.parser.factory.ess_factory`
  - IRC: `arc.parser.parser.parse_irc_path` / `parse_irc_traj`
  - GSM: `arc.parser.parser.parse_trajectory` / `parse_gsm_stringfile_energies`
    + `arc.species.converter.kabsch` (routed here, NOT vendored — it pulls ARC's
    full center-of-mass/atomic-mass + scipy machinery, and is only used on the
    GSM path that already degrades through `_arc_optional`; plan Risk #5).
  - One added guard: the GSM `parse_gsm_stringfile_energies` call in
    `_build_path_search_result_payload` was previously unguarded; wrapped in
    try/except so a no-ARC base install degrades instead of raising.
- **DROP** — `sweep.py`'s back-compat `from arc.imports import settings` branch
  (derived the input-deck filename from `settings['input_filenames']` for
  pre-`<calc>_input` output.yml). Replaced with `return None` + comment. Current
  output.yml always emits `opt_input`/`freq_input`/`sp_input`, so no live run
  relies on it.
- **Grep verification:** the ONLY remaining `from arc`/`import arc` sites are in
  `_arc_optional.py` (lines 51-52, inside `require_arc_parser`'s try). Every other
  module is ARC-free.

### Dependency install

- Local dev/CI-equivalent used for Phase 1:
  `pip install /home/calvin/code/TCKDB_v2/clients/python /home/calvin/code/TCKDB_v2/schemas/python/tckdb-schemas`
  then `pip install -e "tckdb_arc[test]"`. Resolved: tckdb-client 0.27.1,
  tckdb-schemas 0.8.0, numpy 2.5.1, PyYAML 6.0.3, pydantic 2.13.4, httpx 0.28.1.
- CI + README pin the shared layer to the git tags on `calvinp0/tckdbv2`:
  `tckdb-client-v0.27.1` (subdir `clients/python`) and
  `tckdb-schemas-v0.8.0` (subdir `schemas/python/tckdb-schemas`).
- Wheel build verified (`python -m build --wheel`): all 11 `.py` modules +
  `data/elements.yml` are packaged.

### Test results

- **Base (no-ARC) suite, Python 3.12, clean venv:**
  **615 passed, 15 skipped, 0 failed** (+32 subtests passed).
- Suite ran with the shared packages installed from the local `TCKDB_v2`
  checkout; ARC deliberately NOT installed, so arc-coupled tests self-skip.
- **arc-gated skips: 12** (`pytest.importorskip("arc")`): 4 methods in
  `test_adapter.py` (2 Hessian, 1 `arc.output._spc_to_dict` introspection, 1
  real-IRC-fixture) + the entire `TestPathSearchPointsWithNodeMetadata` class
  (8 tests, gated in `setUp` — exercises ARC's real GSM `parse_trajectory`).
- **Other skips: 3** — pre-existing conditional skips in the original suite
  (`ARC_GSM_STRINGFILE_FIXTURE` env not set); not introduced by the re-home.
- **Golden-corpus test:** 5 passing — builds computed-species / computed-reaction
  (+IRC) / standalone-TS payloads offline (`upload=False`) from the synthetic
  `_reaction_output_doc`/`_reaction_record`/`_fake_output_doc`/`_full_record`
  helpers and `model_validate`s them against the real `tckdb_schemas` models
  (`ComputedSpeciesUploadRequest`, `ComputedReactionUploadRequest`,
  `CalculationWithResultsPayload`, `GeometryPayload`, `SpeciesEntryIdentityPayload`)
  plus the local `TransitionStateUploadRequest` mirror; asserts the forbidden-key
  boundary (`atom_map`, `ts_report`, `successful_methods`, `server`, `job_id`,
  `relative_e0_kj_mol`) never appears in any built payload.

### Decisions

1. **`[arc]` extra is a documented marker, not a pip dep.** ARC is not
   pip-installable as `arc` (conda/PYTHONPATH install), so the `arc` extra is
   declared as `arc = []` (empty) with a comment; CI never tries `pip install
   arc`. The arc-coupled tests run only where ARC is already on the path;
   everywhere else they `importorskip("arc")`. (Plan §1/§4 + brief constraint #3.)
2. **Golden-corpus fixture source = synthetic helpers, not a frozen output.yml.**
   No real benchmark `output.yml` was frozen locally for Phase 1; the test builds
   the corpus from the existing synthetic doc/record helpers. A
   `TODO: freeze a real arcbench output.yml` is recorded in the test and in
   `tests/fixtures/golden/` (empty dir). (Plan §3.)
3. **`kabsch` routed through `_arc_optional`, not vendored** (see rewrite summary;
   plan Risk #5 explicitly permits gating the GSM path_coordinate alignment
   behind `[arc]`).
4. **`InputError(ValueError)`** per plan §2a (ARC's is `Exception`-based; the
   subclass choice is behavior-neutral for the `except`/`assertRaises` sites).
5. **Test string-reference rewrites:** `mock.patch("arc.tckdb.…")` →
   `"tckdb_arc.…"`; `mock.patch("arc.parser.parser.parse_irc_*")` →
   `"tckdb_arc._arc_optional.parse_irc_*"` (the adapter now imports those wrappers
   from `_arc_optional`); `assertLogs("arc")` / `logger_name = "arc"` →
   `"tckdb_arc"`; cross-test `from arc.tckdb.adapter_test import …` →
   `from test_adapter import …` (resolved via `conftest.py` sys.path insert).

### Deviations from the plan

- `_vendor.py` builds the element dicts from a bundled `data/elements.yml`
  (copied from ARC) rather than hand-copying a mass table — cleaner and exact.
- `check_xyz_dict` vendored as dict-branch-only (documented in `_vendor.py`).
- Added the missing try/except around GSM `parse_gsm_stringfile_energies`
  (new failure mode introduced only by extraction — ARC never raised ImportError).
- Vendored two IRC ESS-log fixtures into `tckdb_arc/testing/irc/` so the
  real-fixture IRC test has its inputs (still arc-gated on the parser).

### Deferred

- Freeze a real `arcbench` `output.yml` (+ future `tckdb_evidence` sidecar) into
  `tests/fixtures/golden/` and drive `test_golden_corpus.py` from it.
- No `arc/tckdb/` change in ARC — the in-tree copy still exists and both coexist
  (as Phase 1 intends). No GitHub remote created; commit is local only.
- Publishing the shared layer to a real index (PyPI/private) — the git-tag pin
  is sufficient for now.

### Entry point for Phase 2 (ARC-side dual-path shim — NOT done in Phase 1)

In ARC's `ARC.py`, make the TCKDB connector prefer the installed `tckdb_arc`
and fall back to the in-tree `arc.tckdb`, so nothing breaks if the package
isn't installed on zeus yet:

```python
try:
    from tckdb_arc.sweep import run_upload_sweep
    from tckdb_arc.config import TCKDBConfig
except ImportError:
    from arc.tckdb.sweep import run_upload_sweep
    from arc.tckdb.config import TCKDBConfig
```

Then `pip install -e tckdb_arc[test]` into zeus's `arc_env` and run one arcbench
reaction through it to confirm the sweep output is byte-identical to the in-tree
path. This is an ARC-side change (arcbench branch first, then a main-based
branch) and MUST be logged in `~/code/arcbench/BRANCHES.md`. Do NOT delete
`arc/tckdb/` until Phase 4.

---

## Phase 2 — ARC-side dual-path shim (2026-07-20)

**Status:** DONE. ARC prefers the installed standalone `tckdb-arc` package and
retains the in-tree `arc/tckdb/` fallback. The code is integrated but is not
deployed on Zeus; no benchmark job was submitted or rerun.

### ARC edits

Only `ARC.py` changed:

1. The top-level `TCKDBConfig` and `run_upload_sweep` imports now try
   `tckdb_arc` first and catch `ImportError` to import the same names from
   `arc.tckdb` until Phase 4 removes the in-tree package.
2. The end-of-run inline `TCKDBAdapter` import likewise tries
   `tckdb_arc.adapter` first and falls back to `arc.tckdb.adapter`.

No payload/upload behavior changed, and `arc/tckdb/` plus its tests remain in
place.

### Branches and commits

- Source branch: `feature_tckdb_arc_dualpath_shim`
- Source commit: `f2d460d6` (`Prefer standalone tckdb-arc package`), pushed to
  `origin/feature_tckdb_arc_dualpath_shim`
- Source base: `origin/tckdb-imp` at `d76208f4`
- Arcbench cherry-pick: `74f962a0`, pushed to `origin/arcbench` and mirrored to
  `origin/crest_adapter`
- tckdb-imp cherry-pick: `6efc4e12`, pushed to `origin/tckdb-imp`
- Arcbench ledger: local-only `mindless` commit `d2a5a35` in
  `~/code/arcbench/BRANCHES.md` (not pushed)

**Documented base deviation:** the brief required a branch from current `main`,
but current `origin/main` (`59ac3f01`) has neither the TCKDB wiring in `ARC.py`
nor `arc/tckdb/`. A main-based two-import shim would therefore have a missing
fallback and break ARC when `tckdb-arc` is absent; making it functional would
require importing the entire historical TCKDB implementation, outside Phase 2's
additive-shim-only scope. Per ARC's documented last-resort exception for
genuinely TCKDB-only code, the isolated source commit was based on `tckdb-imp`.
It is recorded in the branch ledger as Base=`tckdb-imp`, PR=`n`.

### Verification

- Fallback path with `tckdb-arc` absent from local `arc_env`:
  `622 passed, 3 skipped, 32 subtests passed` for `arc/tckdb/`.
- Preferred path after installing the local Phase 1 package into `arc_env`:
  importing `TCKDBConfig` through `ARC.py` prints `tckdb_arc.config`.
- Adjacent ARC dispatcher regression: `ARC_test.py` — `15 passed`.
- Offline end-to-end equivalence: copied the completed Zeus
  `reaction_05_h_abstraction/output/output.yml` to `/tmp`, ran both the in-tree
  and standalone computed-reaction sweeps with `upload=False`, and compared all
  five emitted payload JSON files using `cmp`. The partial reaction payload and
  four salvaged computed-species payloads were byte-identical. Metadata
  sidecars were not compared byte-for-byte because they intentionally contain
  the distinct temporary project paths. This reused a completed reaction; no
  ARC/PBS job or network upload was started.

### Zeus deployment rollback

- An initial deployment fast-forwarded `~/Code/ARC` to `74f962a0` and installed
  `tckdb-arc` 0.1.0 from standalone commit `dff080b` into `arc_env`.
- This was outside the user's intended Phase 2 scope and was rolled back on
  request. `~/Code/ARC` was restored to its exact pre-deployment commit
  `d9fb8546`, and `tckdb-arc` was uninstalled from `arc_env`.
- Post-rollback verification: the checkout reports `d9fb8546`, the package is
  absent, and `ARC.TCKDBConfig` resolves to `arc.tckdb.config`. The pre-existing
  untracked `.codex` path was preserved. No ARC/PBS job was submitted.

---

## Phase 3 — versioned ARC evidence sidecar (2026-07-20)

**Status:** DONE. New ARC output emits a matching parser-neutral evidence
sidecar, and standalone `tckdb-arc` consumes it without importing ARC or
retaining the original calculation artifacts. Work and verification were
strictly local/offline: no Zeus/HPC access, deployment, scheduler submission,
environment installation, benchmark run, live TCKDB upload, or network call
was performed.

### Branches and commits

- Standalone source branch: `feature_phase3_tckdb_evidence_consumer`, based on
  `origin/main` at `30154e1`; implementation commit `8adbeb8`.
- ARC producer source branch: `feature_tckdb_evidence_sidecar`, based on
  `origin/tckdb-imp` at `6efc4e12`; source commit `d5906ad5`, pushed.
- ARC maintained integrations: `tckdb-imp` `b2ba7413`; `arcbench` `c67a1ff3`;
  `crest_adapter` mirrors `c67a1ff3`. All three were pushed. The arcbench
  conflict resolution retained its newer output cost/TS-guess tests and the
  Phase 3 writer tests.
- ARC branch-ledger entry: local-only `mindless` commit `99d0636` in
  `~/code/arcbench/BRANCHES.md`; only the new Phase 3 row was staged from the
  pre-existing dirty worktree.
- Deployment is recorded as `n`. `AGENTS.md` was not changed.

### Producer contract and files

ARC adds `arc/tckdb_evidence.py` with
`build_tckdb_evidence(...)`, `write_tckdb_evidence_atomic(...)`, per-kind
Hessian/IRC/GSM builders, strict finite-JSON helpers, and parser-version
constants. `arc/output.py::write_output_yml` now:

1. builds output schema `1.1` in memory;
2. creates one lowercase UUID hex `document_id`;
3. builds each evidence kind independently, representing attempted failures as
   authoritative `unavailable` envelopes;
4. atomically writes `output/tckdb_evidence.json` first using deterministic
   UTF-8 JSON (`indent=2`, sorted keys, trailing newline, `allow_nan=False`,
   flush/fsync/replace);
5. adds the matching descriptor and atomically replaces `output.yml` second;
6. still writes output schema 1.1 without a descriptor if the evidence document
   itself cannot be built or written.

The final evidence identity is schema name `arc-tckdb-evidence`, version `1.0`.
Hessian values retain hartree/bohr² lower triangles, IRC retains rich native
coordinates/energies/reaction coordinates/gradients with geometry-only
fallback and omitted-source reporting, and GSM retains frames, source indices,
node labels, cumulative Kabsch distances, relative comment energies, and
absolute node energy/gradient precedence. The producer imports none of
`tckdb_arc`, `tckdb_client`, or `tckdb_schemas` and lives outside `arc/tckdb/`
so it survives Phase 4.

### Consumer contract and files

Standalone adds `tckdb_arc.evidence` with `EvidenceIssue`, `EvidenceLookup`,
`EvidenceStore`, `validate_output_schema`, and strict document/envelope/value
validators. The store lazily reads `<project>/output/tckdb_evidence.json` once,
bounds it to 256 MiB, rejects duplicate JSON keys, unsafe paths, generation or
schema mismatches, unknown keys, invalid identities/enums/indices/XYZ/shapes,
and non-finite scientific values, then caches either its index or failure.
Output schemas `1.0` and `1.1` are supported; sweeps fail clearly on any other
version.

Adapter precedence is implemented independently for Hessian, IRC, and GSM:

- valid `available`: use the sidecar only and do not touch `_arc_optional` or
  source artifacts;
- valid `unavailable`: omit the optional result authoritatively and do not
  reparse;
- absent, mismatched, malformed, unsupported, incomplete, or individually
  invalid evidence: bounded warning and the existing legacy parser fallback.

Hessian keys are translated into the existing payload shape. IRC evidence and
fallback converge on the same trajectory/result composer, including global
point indices and the synthesized TS marker. GSM evidence and fallback converge
on the same path-search composer, preserving absolute-energy precedence,
relative-comment fallback, and the flat-zero sentinel. `_arc_optional.py` and
the in-tree ARC implementation remain intact for old/partial runs.

### Golden fixture and parity

`tckdb_arc/tests/fixtures/golden/phase3_output.yml` and
`tckdb_evidence.json` form the shared cross-repository contract. They derive
from the `tckdb-imp` contract at `6efc4e12` plus the Phase 3 producer schema and
were reduced to a sanitized H/H2 exchange: run-relative paths only, no user,
server, job, credential, timestamp, or irrelevant log content. The fixture
retains species and TS Hessians, forward/reverse IRC evidence, and a three-frame
GSM path with an energy-less endpoint and absolute interior-node energies.

ARC producer tests assemble and compare against this exact JSON document.
Standalone golden tests load both files from disk, remove all source-artifact
requirements, poison the ARC parser boundary, build computed-species,
computed-reaction, and standalone-TS payloads offline, validate the pinned
schemas, enforce the forbidden-key walker, and freeze canonical JSON hashes.
Additional parity tests prove deep and canonical equality between sidecar and
legacy-fallback Hessian/IRC/GSM result dictionaries, including both species and
TS Hessian identities.

### Verification

- ARC source branch, focused producer/writer:
  `HOME=<temp> RMG_DB_PATH=/home/calvin/code/RMG-database conda run -n arc_env python -m pytest arc/tckdb_evidence_test.py arc/output_test.py -o addopts="" -p no:cacheprovider -q`
  — **200 passed, 0 skipped, 0 failed**. ARC importable.
- ARC integrated `arcbench`, same command after conflict resolution —
  **217 passed, 0 skipped, 0 failed**. ARC importable.
- Standalone focused:
  `conda run -n arc_env pytest -q tckdb_arc/tests/test_evidence.py tckdb_arc/tests/test_adapter.py tckdb_arc/tests/test_golden_corpus.py -o addopts="" -p no:cacheprovider`
  — **513 passed, 3 skipped, 0 failed, 32 subtests passed**. ARC importable.
- Standalone full repository:
  `conda run -n arc_env pytest -q -o addopts="" -p no:cacheprovider`
  — **654 passed, 3 skipped, 0 failed, 34 subtests passed**. ARC importable.
- Standalone base/no-ARC leg used a test-only `sitecustomize` import blocker and
  `PYTHONPATH=/tmp/phase3-noarc` with the full suite — **642 passed, 15 skipped,
  0 failed, 34 subtests passed**. ARC deliberately unimportable; all Phase 3
  golden payloads passed and only legacy ARC-gated tests skipped.
- `py_compile`, `git diff --check` for implementation files, and import-boundary
  `rg` scans passed. The only `diff --check` findings in the complete commit
  were two intentional Markdown hard-break spaces already present in the
  user-supplied `PHASE_3_BRIEF.md`.

### Deviations and deferred work

There is no schema or behavioral deviation from `PHASE_3_BRIEF.md`. The only
adjacent cleanup was importing the already-used `TSGuess` class in the
`tckdb-imp` `arc/output_test.py`, fixing two pre-existing NameErrors so the
complete adjacent output suite is green.

Phase 4 must not begin until a separately approved integration smoke test has
confirmed an actual new ARC-produced output/evidence pair through the
standalone package. Phase 4 may then remove `arc/tckdb/` and the dual-path
fallback; until that approval and smoke result, both rollback boundaries remain
independent and intact.
