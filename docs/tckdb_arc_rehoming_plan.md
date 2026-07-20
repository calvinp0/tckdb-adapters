# Re-homing `arc/tckdb/` → standalone `tckdb-arc` package

**Status:** planning only. This document is decision-grade; it proposes a re-homing (not a
rewrite), because the shared layer already exists as pip packages `tckdb-schemas` (0.8.0) and
`tckdb-client` (0.27.1), and ARC's adapter already targets those (dicts on the wire, `tckdb-client`
for transport). Nothing here is code; it cites real symbols on both sides.

---

## Scope & intention (handoff — read this first)

**What this is.** A plan to extract ARC's TCKDB upload adapter (`arc/tckdb/`) into a standalone,
pip-installable package **`tckdb-arc`** that lives in a NEW multi-tool repo **`tckdb-adapters`** —
never inside ARC or `TCKDB_v2`.

**Why we're doing it.** "Extract a tool's computed-chemistry results and push them to TCKDB" is a
*tool-agnostic* concern; ARC is merely the first producer. RMG and Chemtrayzer are expected to
follow (no fixed timeline — they come as the ARC adapter proves the pattern). Leaving the adapter
inside ARC chains a general capability to ARC's release cycle and makes it un-reusable by other
tools. Extracting it lets each tool's community own its own adapter while sharing one schema + one
transport.

**The three-package ecosystem (who owns what):**
- **`tckdb-schemas`** (lives in `TCKDB_v2`, pip package, v0.8.0) — the canonical Pydantic
  *wire-contract* (this is the "CRM"; it already exists — nothing to invent). Owns the schema.
- **`tckdb-client`** (lives in `TCKDB_v2`, pip package, v0.27.1) — HTTP transport + idempotency +
  replay. Tool-neutral. Already exists.
- **`tckdb-adapters`** (NEW repo, this plan) — per-tool extractors: `tckdb_arc` first, then
  `tckdb_rmg` / `tckdb_chemtrayzer`. Each depends on the two above; each MAY *optionally* depend on
  its own tool (e.g. `tckdb-arc[arc]`).

**Decided principles (do not relitigate):**
1. **Re-home, not rewrite** — the adapter already emits plain dicts on the wire and already uses
   `tckdb-client`; it just physically lives in the wrong repo.
2. **The ARC adapter MAY depend on ARC** (optional `[arc]` extra, for the 3 parser-coupled paths);
   the *shared* layer (`tckdb-client`/`tckdb-schemas`) must NEVER depend on ARC.
3. **PUSH here, PULL in ARC** — extract+upload lives in `tckdb-adapters`; the future
   DB-JSON→`ARCSpecies` hydrator stays in ARC forever (or the dependency inverts).
4. **Sidecar-primary / reparse-fallback** for Hessian/IRC/GSM — deferrable (Phase 3); until it
   lands, the `[arc]` extra covers those three payloads.

**Dependency direction (acyclic — this is what makes the split safe):**
`tckdb_arc → {tckdb-client, tckdb-schemas, arc(optional)}`; ARC reaches `tckdb-arc` only through a
guarded optional import. Nothing in the shared layer ever points back at ARC.

**Where this sits in the wider effort.** TCKDB is *deliberately excluded* from ARC's current
PR-to-main merge order (see `~/code/arcbench/BRANCHES.md` + ARC `CLAUDE.md`). This work proceeds on
its own timeline and does **not** block the 500-reaction benchmark/paper run.

---

## Grounding facts established by reading the code (do not relitigate)

1. **The ARC adapter emits plain nested `dict`s.** `arc/tckdb/adapter.py` imports **zero** symbols
   from `tckdb_schemas`; its only third-party runtime imports are `from tckdb_client import
   TCKDBClient` (line 36) and `from tckdb_client.errors import TCKDBError` (line 37). Dict *shapes*
   are the contract, exactly like the Chemkin adapter's `payloads.py` (which also returns dicts and
   states "No backend imports — the dict shapes are the contract, verified in tests"). The task
   brief's example `from tckdb_schemas.fragments.calculation import CalculationWithResultsPayload`
   is true only of the **tests** (`adapter_test.py:36-40`, `ts_upload_test.py:30-35`), which
   `model_validate` the built dicts. This is a strength: the re-home is mechanical, and the
   golden-corpus test is the natural home for that validation (§3).
2. **`make_idempotency_key` is not called in adapter.py** — idempotency composition lives in the
   sibling `arc/tckdb/idempotency.py`, which wraps `tckdb_client.make_idempotency_key`. That module
   travels into the package.
3. **The only eager ARC coupling is `from arc.common import get_logger` (line 39).** Everything else
   from ARC is either (a) a sibling `arc.tckdb.*` module that moves with the package, or (b) a
   handful of **lazy, in-function** imports of `arc.parser` / `arc.species.converter` /
   `arc.constants` confined to the IRC / freq-Hessian / GSM-stringfile paths.
4. **The adapter already consumes `output.yml` dicts, not live ARC objects.** `TCKDBAdapter`
   entrypoints take `output_doc` + a record dict; `arc/tckdb/sweep.py` and `arc/tckdb/cli.py` read
   the YAML. This decoupling is what makes the standalone package possible with a thin ARC shim.
5. **ARC already stamps `schema_version: '1.0'`** at `arc/output.py:97` (`doc['schema_version'] =
   '1.0'`). The evidence-sidecar (`tckdb_evidence`) that the brief calls the "primary path" does
   **not exist yet** — today the optional-ARC re-parse is the *only* path for Hessian/IRC/GSM, not a
   fallback. That gap is the main phasing risk (§6, §7).

---

## Section 1 — New repo layout

New standalone repo, working name **`tckdb-adapters`** — a sibling-adapters monorepo. `tckdb_arc`
is the first tenant; `tckdb_rmg` / `tckdb_chemtrayzer` land later. It is **NOT** inside `TCKDB_v2`
and **NOT** inside ARC.

```
tckdb-adapters/                         # git repo root
├── README.md
├── pyproject.toml                      # optional: workspace/dev-tooling aggregator only
├── .github/workflows/ci.yml            # matrix over each adapter package
├── tckdb_arc/                          # ── the extracted package ──
│   ├── pyproject.toml                  # name = "tckdb-arc"  (see below)
│   ├── tckdb_arc/
│   │   ├── __init__.py                 # re-exports TCKDBAdapter, TCKDBConfig, UploadOutcome, run_upload_sweep
│   │   ├── config.py                   # ← arc/tckdb/config.py  (get_logger + InputError vendored)
│   │   ├── idempotency.py              # ← arc/tckdb/idempotency.py  (unchanged; wraps tckdb_client)
│   │   ├── constraints.py              # ← arc/tckdb/constraints.py  (get_logger vendored)
│   │   ├── payload_writer.py           # ← arc/tckdb/payload_writer.py  (already ARC-free)
│   │   ├── adapter.py                  # ← arc/tckdb/adapter.py  (the 6.5k-LOC core)
│   │   ├── sweep.py                    # ← arc/tckdb/sweep.py
│   │   ├── cli.py                      # ← arc/tckdb/cli.py  (entry point `tckdb-arc-upload`)
│   │   ├── _logging.py                 # NEW vendored get_logger shim (§2)
│   │   ├── _arc_optional.py            # NEW guarded `import arc` boundary (the [arc] extra, §2/§4)
│   │   └── _vendor.py                  # NEW small copies: E_h_kJmol, xyz_to_str, kabsch (§2)
│   └── tests/
│       ├── conftest.py                 # sys.path convenience like chemkin's conftest.py
│       ├── fixtures/
│       │   └── golden/                 # frozen real benchmark output.yml + evidence sidecars (§3)
│       ├── test_config.py              # ← config_test.py
│       ├── test_idempotency.py         # ← idempotency_test.py
│       ├── test_payload_writer.py      # ← payload_writer_test.py
│       ├── test_sweep.py               # ← sweep_test.py
│       ├── test_cli.py                 # ← cli_test.py
│       ├── test_adapter.py             # ← adapter_test.py (8.8k LOC; imports tckdb_schemas — dev dep)
│       ├── test_ts_upload.py           # ← ts_upload_test.py
│       └── test_golden_corpus.py       # NEW contract test (§3)
├── tckdb_rmg/                          # future tenant (placeholder; user's call)
└── tckdb_chemkin/                      # OPTIONAL future migration of the Chemkin adapter (open Q, §7)
```

**Shared internal helpers:** the Chemkin adapter needs *nothing* from `tckdb_arc` and vice-versa;
their only common dependency is the already-published `tckdb-client` (+ `tckdb-schemas` in tests).
So **do not** introduce a shared `tckdb_adapters_common` package yet — there is no shared surface
worth the coupling. If a genuinely shared helper appears later (e.g. a common sidecar/replay
convention), promote it then. Keep each adapter a flat, independently-installable, independently-
versioned package under one repo, mirroring how Chemkin already lives as a self-contained
`pyproject.toml` under `clients/python/adapters/chemkin/`.

**`tckdb_arc/pyproject.toml`** (models the Chemkin one; adds the `[arc]` extra):

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "tckdb-arc"
version = "0.1.0"
description = "ARC → TCKDB upload adapter (payload builder + sidecar writer + uploader)."
requires-python = ">=3.11"          # matches tckdb-chemkin / tckdb-client
dependencies = [
    "tckdb-client>=0.27,<0.28",     # transport + idempotency + replay (pin, §5)
    "PyYAML>=6",                    # read_yaml_file is vendored → needs a yaml lib
]

[project.optional-dependencies]
arc  = ["arc"]                      # heavy: molecule system + arc.parser for Hessian/IRC/GSM fallback
test = ["pytest", "tckdb-schemas>=0.8,<0.9"]   # schemas is a TEST-only dep (model_validate)

[project.scripts]
tckdb-arc-upload = "tckdb_arc.cli:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["tckdb_arc*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Notes: **`tckdb-schemas` is a *test* dependency, not a runtime one** — the adapter never imports it
at runtime (fact 1). The `[arc]` extra pulls ARC only for the three parser-coupled paths (§2). CLI
entry point renamed `tckdb-arc-upload` (vs today's `python -m arc.tckdb.cli`) to sit next to
Chemkin's `tckdb-chemkin-import`.

---

## Section 2 — Module-by-module mapping

Chemkin's staged model — **M1 parser → M2 normalizer → M3 identity → M4 payloads → M5 uploader**,
"stages 1-4 pure, stage 5 the only network stage, `tckdb-client` lazy-imported inside the
uploader" — is the target *shape*. ARC's adapter does not decompose into those exact stages (it is
a producer of already-normalized ESS records, not a text parser), but it already obeys the same
**network boundary**: every `submit_*` builds a dict, writes it to disk, and only *then* touches
`tckdb-client` via `_upload` / `_make_client`. So the house-style alignment is "keep the pure
dict-builders network-free; keep `TCKDBClient` behind the `_upload`/`_make_client` seam," which the
code already does — no restructbranch needed for v1.

### 2a. Whole modules (1:1 file moves)

| Current `arc/tckdb/*` | Target `tckdb_arc/*` | Disposition | Notes |
|---|---|---|---|
| `__init__.py` | `__init__.py` | REFACTOR | re-export the same names; drop `arc.` prefixes. |
| `config.py` | `config.py` | MOVE + VENDOR | swap `from arc.common import get_logger`→`_logging`; `from arc.exceptions import InputError`→ define a local `InputError(ValueError)` in `config.py` (only used for api-key file errors). |
| `idempotency.py` | `idempotency.py` | MOVE AS-IS | already only imports `tckdb_client.make_idempotency_key`. Zero ARC coupling. |
| `constraints.py` | `constraints.py` | MOVE + VENDOR | only ARC import is `get_logger`→`_logging`. Pure dict serializer. |
| `payload_writer.py` | `payload_writer.py` | MOVE AS-IS | **already ARC-free** (stdlib only). No change. |
| `sweep.py` | `sweep.py` | MOVE + VENDOR + one DROP | `get_logger`,`read_yaml_file`→vendor; the lazy `from arc.imports import settings` at `sweep.py:544` (back-compat input-deck-name derivation for pre-`<calc>_input` output.yml) → **OPTIONAL-ARC or DROP**: gated behind `_arc_optional` or dropped since new output.yml always carries `opt_input`/`freq_input`/`sp_input`. |
| `cli.py` | `cli.py` | MOVE + VENDOR | `read_yaml_file`→vendor; imports become intra-package. |
| `adapter.py` | `adapter.py` | MOVE + VENDOR + OPTIONAL-ARC | see 2b. |
| `*_test.py` (all 6) | `tests/test_*.py` | MOVE | `adapter_test.py`/`ts_upload_test.py` keep their `tckdb_schemas` imports (now a declared test dep). |

**Vendored shims (NEW, tiny):**
- `_logging.py` — `get_logger()` returning `logging.getLogger("tckdb_arc")`. Replaces every
  `arc.common.get_logger` (adapter.py:39/96, config.py, constraints.py, sweep.py). Trivial.
- `_vendor.py` — copies of `arc.constants.E_h_kJmol` (a scalar), `arc.species.converter.xyz_to_str`
  (dict→atom-line formatter over ARC's xyz-dict shape), and `arc.species.converter.kabsch`
  (self-contained NumPy Kabsch RMSD). All three are small, pure, and copyable; the coverage audit
  and adapter inventory both class them VENDOR. Add `numpy` to runtime deps if `kabsch` is vendored
  (it already transitively ships via tckdb-client? — verify; else add `numpy>=1.23`).
- A vendored `read_yaml_file` (used by `sweep.py`, `cli.py`) — thin `yaml.safe_load` wrapper;
  hence `PyYAML` in runtime deps.

### 2b. `adapter.py` broken into functional clusters (real names from inventory)

| Cluster (line range) | Representative functions | Target module | Disposition |
|---|---|---|---|
| Units/filename coercion (375-475) | `_normalize_unit_key`, `arc_to_tckdb_a_units`, `arc_to_tckdb_ea_units`, `_coerce_artifact_filename` | `adapter.py` (or split to `_units.py`) | MOVE AS-IS — pure. |
| Dataclasses + exception (475-541) | `UploadOutcome`, `ArtifactUploadOutcome`, `_PreparedArtifactUpload`, `TCKDBReadinessError` | `adapter.py` | MOVE AS-IS. |
| Entrypoints + species/conformer bundle builders (581-1565) | `submit_from_output`, `submit_computed_species_from_output`, `_build_computed_species_payload`, `_build_conformer_block`, `_build_calc_in_bundle`, `_inline_artifacts_for_calc` | `adapter.py` | MOVE AS-IS — pure dict work over `output_doc`. |
| Freq-Hessian + IRC-traj (1574-1739) | `_build_freq_hessian_payload` (uses `determine_ess`+`ess_factory`), `_parse_irc_trajectories` (uses `parse_irc_path`,`parse_irc_traj`) | `adapter.py`, calling `_arc_optional` | **OPTIONAL-ARC FALLBACK** — route the lazy imports through `_arc_optional.require_arc_parser()`; on `ImportError` return `None` (already best-effort). Primary path becomes the evidence sidecar (§3/§6). |
| Reaction/TS bundle builders (1740-2848) | `submit_computed_reaction_from_output`, `submit_computed_ts_from_output`, `_build_computed_reaction_payload`, `_build_ts_block`, `_compose_transition_state_request`, `_build_ts_reaction_upload` | `adapter.py` | MOVE AS-IS. |
| Legacy conformer-v1 (2849-3088) | `_species_entry_payload`, `_build_payload`, `_build_calculations`, `_calculation_payload` | `adapter.py` | MOVE AS-IS (keep — still the `/uploads/conformers` path used by `submit_from_output`). Candidate for later DROP if conformer mode is retired. |
| Network / upload / readiness (3089-3560 in-class; 3583-3768 helpers) | `_upload`, `_make_client`, `_ensure_ready`, `_prepare_artifact_upload`, `_upload_artifact_batch`, `_readyz_body_is_ready`, `_build_readiness_error` | `adapter.py` (or `_transport.py`) | MOVE AS-IS — this is the M5 boundary; `TCKDBClient`/`TCKDBError` already isolated here. |
| Field-extraction result payloads (3770-4131) | `_opt_result_payload`, `_freq_result_payload`, `_sp_result_payload`, `_spin_diagnostic_payload`, `_reused_origin`, `_screened_conformer_origin`, `_final_settings_for_calc` | `adapter.py` | MOVE AS-IS — pure record→dict. |
| Level-of-theory / software / corrections (4187-4803) | `_arc_level_to_tckdb_lot`, `_build_applied_energy_corrections`, `_arc_workflow_tool_release`, `_build_freq_scale_factor_ref` | `adapter.py` | MOVE AS-IS. |
| Thermo / NASA (4333-4553) | `_build_thermo_block`, `_build_nasa_block`, `_build_thermo_points` | `adapter.py` | MOVE AS-IS. |
| Key helpers / indexers (4554-4626) | `_safe_key_part`, `_local_key_for_actor`, `_index_species`, `_index_transition_states` | `adapter.py` | MOVE AS-IS. |
| Statmech / torsions (4804-5217) | `_build_statmech_block_for_species`, `_build_slim_torsions`, `_coerce_torsion_coordinates`, `_build_statmech_source_calculations` | `adapter.py` | MOVE AS-IS. |
| Flatten / shape-fixup / coercion (5222-5330) | `_flatten_result_fields`, `_flatten_all_reaction_calcs` (the wrapped-vs-flat calc divergence handler), `_coerce_optional_float`, `_stringify_tunneling_model` | `adapter.py` | MOVE AS-IS. |
| Kinetics (5331-5583) | `_build_kinetics_block`, `_build_kinetics_source_calculations` | `adapter.py` | MOVE AS-IS. |
| IRC / path-search / xtb-GSM (5584-6334) | `_build_irc_result_payload` (uses `xyz_to_str`→vendor), `_build_path_search_result_payload` (uses `E_h_kJmol`,`kabsch`,`xyz_to_str`→vendor **plus** `parse_trajectory`,`parse_gsm_stringfile_energies`→OPTIONAL-ARC), `_parse_xtb_turbomole_energy_file`, `_parse_xtb_xtbout_energy`, `_read_gsm_node_outputs` | `adapter.py` | **MIXED**: xtb/turbomole file parsers (5833-6047) are **already self-contained → MOVE AS-IS**; the two `arc.parser` trajectory calls → **OPTIONAL-ARC FALLBACK**; the converter/constants → **VENDOR**. |
| XYZ-normalize + misc (6335-6532) | `_normalize_xyz_text`, `_require_xyz_text`, `_summarize_response_body`, `_extract_tckdb_public_refs`, `_extract_calc_refs`, `_close_quietly` | `adapter.py` | MOVE AS-IS. |
| `_spc_to_dict`, `_xyz_dict_to_tckdb_xyz_text` (in `arc/output.py`, NOT adapter) | — | **STAYS IN ARC** | these *produce* output.yml; they are the ARC side of the contract. Never move. |

**ARC-coupling summary (the whole external surface):** exactly three parser-coupled paths —
freq-Hessian (`arc.parser.parser.determine_ess`, `arc.parser.factory.ess_factory`), IRC-traj
(`parse_irc_path`, `parse_irc_traj`), GSM-stringfile (`parse_trajectory`,
`parse_gsm_stringfile_energies`) — plus vendorable `xyz_to_str`/`kabsch`/`E_h_kJmol`, plus the sole
eager `get_logger`. That is the entire re-home cost. The `_arc_optional.py` boundary is ~20 lines:

```python
# _arc_optional.py
def require_arc_parser():
    try:
        from arc.parser import parser, factory   # noqa
        from arc.species import converter          # noqa
    except ImportError as exc:
        raise OptionalArcUnavailable(
            "This payload path (Hessian/IRC/GSM re-parse) needs the [arc] extra: "
            "pip install tckdb-arc[arc]. Prefer emitting a tckdb_evidence sidecar from ARC."
        ) from exc
    return parser, factory, converter
```
Callers already `try/except` and degrade to `None`, so `OptionalArcUnavailable` is caught the same
way — a base install silently omits Hessian/IRC/GSM sub-payloads rather than crashing.

---

## Section 3 — The ARC ↔ adapter contract

**The wire between ARC and `tckdb_arc` is `output.yml` plus (future) a validated `tckdb_evidence`
sidecar.** The adapter reads `<project>/output/output.yml` (via `sweep.run_upload_sweep`) and
builds payloads from record dicts. No live-object coupling.

1. **`schema_version` stamp (ARC side, ~1 line, already present).** `arc/output.py:97` already
   writes `doc['schema_version'] = '1.0'`. The contract action is: (a) have `tckdb_arc` **read and
   assert** that stamp at sweep entry (`sweep.run_upload_sweep`, right after `read_yaml_file`), and
   (b) declare a `SUPPORTED_OUTPUT_SCHEMA_VERSIONS = {"1.0"}` set in `tckdb_arc` so an output.yml
   from a newer/older ARC fails loud instead of mis-parsing. When ARC changes output.yml shape it
   bumps `'1.0'` → `'1.1'` at `arc/output.py:97` and `tckdb_arc` widens the supported set. This is
   the single coordination point; keep it ~2 lines on each side.

2. **The `tckdb_evidence` sidecar (NEW, ARC side; the primary path).** For the three parser-coupled
   paths (Hessian lower-triangle, IRC trajectory points, GSM stringfile energies/geoms), ARC —
   which *has* `arc.parser` in-process at end-of-run — should emit a small validated JSON sidecar
   next to output.yml (e.g. `<project>/output/tckdb_evidence.yml`) carrying the already-parsed
   numbers. Then `tckdb_arc._build_freq_hessian_payload` / `_build_irc_result_payload` /
   `_build_path_search_result_payload` read the sidecar **first**, and only fall back to the
   `[arc]`-extra re-parse when the sidecar is absent (old runs). This does not exist yet (fact 5) —
   it is a follow-up ARC change (produce it in `arc/output.py` alongside `write_output_yml`), and
   is the reason the `[arc]` extra stays a *fallback*, not the default. Until it lands, base
   installs simply omit those three sub-payloads.

3. **Golden-corpus contract test (`tests/test_golden_corpus.py`, NEW).** Freeze a real benchmark
   `output.yml` (+ its `tckdb_evidence` once it exists) from an `arcbench` reaction into
   `tests/fixtures/golden/`. The test: run each `TCKDBAdapter.submit_*_from_output` in
   `upload=False` (offline) mode over the frozen doc, then `model_validate` every built payload dict
   against the real `tckdb_schemas` models — `ComputedSpeciesUploadRequest`,
   `ComputedReactionUploadRequest`, `CalculationWithResultsPayload`, `TransitionStateUploadRequest`,
   `HessianPayload`, `GeometryPayload`, `SpeciesEntryIdentityPayload`. This is the same
   `model_validate` guard the current `adapter_test.py` uses (16+ call sites per the backend audit),
   promoted to a frozen-input regression. It runs in **`tckdb-adapters` CI** (the new repo), pinned
   against the declared `tckdb-schemas` version, so a schema bump that breaks the wire shape is
   caught in the adapter repo — not months later in an ARC run. Also mirror the boundary walkers
   (`tckdb_upload_boundary.md`): assert forbidden keys (`atom_map`, `ts_report`,
   `successful_methods`, `server`, `job_id`, `relative_e0_kj_mol`, …) never appear in any built
   payload.

---

## Section 4 — What stays in ARC

1. **The connector/shim in `ARC.py`.** Today `ARC.py:14-15` hard-imports `TCKDBConfig` +
   `run_upload_sweep`, and `ARC.py:65-86` parses the `tckdb:` block and, post-`execute()`,
   constructs `TCKDBAdapter` and calls `run_upload_sweep`. After extraction this becomes a
   **guarded optional import** so ARC runs fine without `tckdb-arc` installed:

   ```python
   # ARC.py  (replacing the eager top-level imports)
   try:
       from tckdb_arc.config import TCKDBConfig
       from tckdb_arc.sweep import run_upload_sweep
       from tckdb_arc.adapter import TCKDBAdapter
       _TCKDB_AVAILABLE = True
   except ImportError:
       _TCKDB_AVAILABLE = False

   # in main(), after arc_object.execute():
   raw_tckdb = input_dict.pop('tckdb', None)
   if raw_tckdb and not _TCKDB_AVAILABLE:
       logger.warning("input.yml has a tckdb: block but tckdb-arc is not installed "
                      "(pip install tckdb-arc); skipping upload.")
   elif _TCKDB_AVAILABLE:
       cfg = TCKDBConfig.from_dict(raw_tckdb)
       if cfg is not None:
           run_upload_sweep(adapter=TCKDBAdapter(cfg, project_directory=arc_object.project_directory),
                            project_directory=arc_object.project_directory, tckdb_config=cfg)
   ```
   The whole ARC-side surface shrinks to this ~15-line shim. `arc/tckdb/` is deleted (its tests
   move too). No other ARC module imports `arc.tckdb.*` (verified: only `ARC.py` and the tests do).

2. **`arc/output.py` producer stays** — `_spc_to_dict`, `_xyz_dict_to_tckdb_xyz_text`, the
   `schema_version` stamp, and (future) the `tckdb_evidence` emitter. output.yml is authored in ARC;
   that never inverts.

3. **The future DB-JSON → `ARCSpecies` PULL hydrator stays in ARC forever.** It needs ARC's molecule
   system to build live objects; putting it in the adapter repo would invert the dependency
   (adapter→ARC becomes mandatory). It belongs in `arc/species/` or a new `arc/tckdb_pull.py`,
   depending on `tckdb-client` reads only. Explicitly out of scope for `tckdb-adapters`.

---

## Section 5 — Publishing prerequisite (the one hard gate)

`tckdb_arc` runtime-depends on **`tckdb-client`** and test-depends on **`tckdb-schemas`**. The new
repo cannot build/install/CI until both are resolvable from an index it can reach. Today they live
as editable installs inside `TCKDB_v2` (`clients/python`, `schemas/python/tckdb-schemas`) — fine for
ARC-on-one-dev-box, **not** for a separate repo's CI.

- **Current versions to pin:** `tckdb-schemas==0.8.0` (was 0.1.0 at the May audit — moving target,
  hence a pin), `tckdb-client==0.27.1`. Pin ranges: `tckdb-client>=0.27,<0.28`,
  `tckdb-schemas>=0.8,<0.9`.
- **Gate:** publish both to an index the new repo can install from — public **PyPI** (both already
  carry full `pyproject.toml` with proper metadata, classifiers, `py.typed`, and console scripts, so
  they are publish-ready), or a **private index** (Gemfury/CodeArtifact/self-hosted) if the schema
  is not to be public yet.
- **Interim (recommended — no publishing required to start):** install both straight from the
  `TCKDB_v2` repo `main` using pip's git+subdirectory syntax, so `tckdb-adapters` builds and CIs
  *before* anything reaches an index. In `tckdb_arc/pyproject.toml` they stay ordinary named deps
  (`tckdb-client`, `tckdb-schemas`); the git source is supplied at install time (CI + dev):
  ```bash
  pip install \
    "tckdb-client @ git+https://github.com/calvinp0/tckdbv2.git@main#subdirectory=clients/python" \
    "tckdb-schemas @ git+https://github.com/calvinp0/tckdbv2.git@main#subdirectory=schemas/python/tckdb-schemas"
  ```
  For local dev against the private remote use `git+ssh://git@github.com/calvinp0/tckdbv2.git@main#subdirectory=...`.
  Caveats: `@main` is a moving target — pin to a tag/SHA (e.g. `@v0.27.1`) once the shared layer
  stabilises; a private repo's CI needs a deploy key / PAT. This is enough to unblock Phases 1–2;
  swap to a real index (PyPI/private) before external contributors need it.
- This is the **only** hard external prerequisite — and the git-install interim defers even that.
  Everything else in this plan is a code move.

---

## Section 6 — Cutover / phasing (each step independently landable)

TCKDB is **deliberately excluded from ARC's current PR-to-main merge order** (per the branch ledger
/ CLAUDE.md), so this can proceed on its own timeline without blocking the paper run.

**Phase 0 — publish the shared layer (§5).** Push `tckdb-client` 0.27.1 + `tckdb-schemas` 0.8.0 to
the chosen index. Independently landable; unblocks everything.

**Phase 1 — create `tckdb-adapters` repo with `tckdb_arc` as a *copy*.** Move the 12 files, add the
3 vendored shims (`_logging`, `_vendor`, `_arc_optional`) + vendored `read_yaml_file`, rewrite
imports, add `pyproject.toml`. Port all 6 test files; add the golden-corpus test (§3). Get CI green
against pinned shared packages (base install + `[arc]` matrix leg). At this point `arc/tckdb/` still
exists in ARC unchanged — the two coexist. Independently landable (new repo, no ARC change).

**Phase 2 — ARC back-compat shim, dual-path.** In `arcbench` (and later a `main`-based branch), make
`ARC.py` prefer the installed `tckdb_arc` but **keep `arc/tckdb/` as a fallback** so nothing breaks
if the package isn't installed on zeus yet:
```python
try:    from tckdb_arc.sweep import run_upload_sweep; from tckdb_arc.config import TCKDBConfig
except ImportError:  from arc.tckdb.sweep import run_upload_sweep; from arc.tckdb.config import TCKDBConfig
```
Install `tckdb-arc` into `arc_env` on zeus. Run one `arcbench` reaction through it to confirm the
sweep output is byte-identical. This keeps the benchmark working throughout. Independently landable.

**Phase 3 — emit `tckdb_evidence` from ARC (§3.2).** Add the sidecar producer to `arc/output.py`.
Teach `tckdb_arc` builders to read it first, `[arc]`-reparse second. Now a base `tckdb-arc` (no
`[arc]` extra) produces *complete* Hessian/IRC/GSM payloads for new runs. Independently landable.

**Phase 4 — delete `arc/tckdb/` from ARC.** Once zeus + CI run purely on installed `tckdb-arc`,
remove `arc/tckdb/*.py` and the fallback branch; `ARC.py` shim becomes the guarded-import form in
§4. ARC's own test suite loses `arc/tckdb/*_test.py` (they now live in the adapter repo).
Independently landable; the last step.

Throughout: **ARC's tests keep passing** because until Phase 4 the in-tree package is still there;
after Phase 4, ARC has no `arc.tckdb` to test. **The arcbench benchmark keeps working** because
Phase 2's fallback shim means an un-upgraded zeus still uses the in-tree copy.

---

## Section 7 — Risks & open questions

1. **How much of adapter.py is ARC-shaped vs house-style?** *Answer from the read:* almost all of it
   is house-style already. The adapter builds plain dicts (like Chemkin's `payloads.py`), isolates
   the network behind `_upload`/`_make_client` (like Chemkin's `uploader.upload_payloads`), and only
   ~3 code paths (freq-Hessian, IRC, GSM) reach into `arc.parser`. Risk is **low**. The one wrinkle:
   the adapter is *one 6.5k-LOC module* rather than Chemkin's 5 small staged files. v1 keeps it
   monolithic (mechanical move, lowest risk); a later refactor could split it (units/, thermo/,
   kinetics/, statmech/, transport/) but that is cosmetic and out of scope for the re-home.
2. **The `tckdb_evidence` sidecar does not exist yet (fact 5).** Until Phase 3, base installs silently
   omit Hessian/IRC/GSM sub-payloads, and only `[arc]`-extra installs produce them (by re-parsing).
   For the paper run this is fine (zeus has ARC), but it means "primary path = sidecar" is aspirational
   at extraction time. Risk: if the sidecar schema is under-specified, the fallback re-parse and the
   sidecar could diverge — pin the sidecar shape to exactly what the three builders consume and cover
   it in the golden corpus.
3. **Should Chemkin migrate into `tckdb-adapters`?** Open — **the user's call.** Chemkin currently
   lives under `TCKDB_v2/clients/python/adapters/chemkin/`. Arguments for moving it into
   `tckdb-adapters`: one home for all adapters, uniform CI, symmetry with `tckdb_arc`/`tckdb_rmg`.
   Against: it is already stable and co-located with the client it tracks; moving it churns import
   paths and its editable-install conftest. Do **not** assume the move; leave a placeholder dir and
   decide later.
4. **Config / credentials resolution movement.** `config.py`'s `resolve_tckdb_api_key`
   (env → `api_key_file` → `api_key_env_file`, dotenv-parsed-never-executed) is self-contained and
   moves cleanly. The only ARC touch is `arc.exceptions.InputError` → replace with a local
   `InputError`. `VALID_ARTIFACT_KINDS` claims to mirror a backend enum
   (`backend/app/db/models/common.py`) by hand-copied comment — after extraction that drift risk
   grows (the adapter repo can't see the backend). Mitigation: pull the artifact-kind enum from
   `tckdb_schemas` if it exposes one, else cover it in the golden corpus / a live-schema test.
5. **`numpy` runtime dep via vendored `kabsch`.** If `numpy` is not already transitively available
   through `tckdb-client` (it is not — client only needs `httpx`), vendoring `kabsch` adds a `numpy`
   runtime dependency to `tckdb_arc`. Acceptable, but note it (or gate the GSM `path_coordinate`
   alignment behind the `[arc]` extra to keep the base install `numpy`-free).
6. **`sweep.py:544` `arc.imports.settings` back-compat.** The input-deck-filename fallback for
   pre-`<calc>_input` output.yml reads ARC's settings dict. New output.yml always carries
   `opt_input`/`freq_input`/`sp_input` (fact from `arc/output.py:1208`), so this is safe to **DROP**
   in the standalone package (or gate behind `[arc]`). Confirm no benchmark output.yml still relies
   on it before deleting.
7. **Test-suite parity under the new layout.** `adapter_test.py` is 8.8k LOC and imports
   `tckdb_schemas` — it becomes the heaviest CI leg. Ensure the pinned `tckdb-schemas` in the new
   repo matches the shapes those tests assert (the May audit was against 0.1.0; the suite has since
   moved to 0.8.0 shapes per `adapter_test.py:37-40`). A version skew here is the most likely
   Phase-1 CI failure.
