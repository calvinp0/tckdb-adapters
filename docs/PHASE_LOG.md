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
