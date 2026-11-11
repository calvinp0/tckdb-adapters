# tckdb-adapters-core

The producer-agnostic core of the TCKDB upload adapters. A producer adapter
(`tckdb_arc` today; RMG and ChemTrayzer next) reads its tool's output and builds
TCKDB payloads. Everything after the payload exists, and every helper that every
producer would write the same way, lives here.

## What belongs here

Anything that would be reused **verbatim** by another producer targeting the same
TCKDB contract, and **nothing that reads a producer's output format**.

| Module | What it holds |
|--------|---------------|
| `payload_writer` | Writes the payload JSON once and the sidecar eagerly; sidecar and replay types. |
| `uploader` | `TCKDBUploaderBase`: readiness probe with backoff, POST, sidecar bookkeeping (status, response, server warnings, request ids), strict mode, artifact batches. |
| `outcomes` | `UploadOutcome`, `ArtifactUploadOutcome`, `TCKDBReadinessError`. |
| `constants` | Route endpoints, payload kinds, the readiness retry policy, the `origin_kind` enum. |
| `idempotency` | Idempotency-key composition. The key namespace is a required argument. |
| `config` | Config fields and defaults, API-key resolution (env, key file, dotenv file). No parser. |
| `level_rules` | Replica of TCKDB's level-of-theory identity (aliases, folded dispersion, hash). |
| `constraints` | Held-fixed coordinate constraints and their serializer. |
| `keys` | Bundle-local key helpers. |
| `adapter_warnings` | `AdapterWarning` and `WarningSink`: the sidecar's warning dict shape, with the producer tag as a parameter. |
| `warning_codes` | `CodedEnum` (a code string plus a one-line description), `CoreWarning` (the codes this package emits) and `registry()`. A producer defines its own codes the same way. |
| `thermo_numerics` | `build_nasa_block`, `build_thermo_points`, `nasa_h298_kj_mol`: NASA-7 and Cp/H/S/G point shaping from neutral mappings. |
| `rmg_units` | RMG kinetics-unit strings to TCKDB's `ArrheniusAUnits` / `ActivationEnergyUnits` (`a_units_to_tckdb`, `ea_units_to_tckdb`). |
| `reaction_flatten` | `flatten_result_fields` / `flatten_all_reaction_calcs`: wrapped `opt_result`/`freq_result`/`sp_result` to the computed-reaction route's flat fields, with the completeness guard. |
| `composition` | Element symbols from an xyz string or a formula, a formula string, the single-atom test. |
| `xyz`, `physical_constants` | Element data, `xyz_from_data` / `check_xyz_dict` / `xyz_to_str`, and `E_h_kJmol` (ported from ARC; the one definition of the hartree-to-kJ/mol factor). |
| `isotopes` | The isotope rule for geometries (`species_geometry_isotope_mismatch`): substitutions, SMILES multisets, reconciliation error, the geometry block. |
| `route_level` | Route line vs level comparison (`route_method_basis`, `route_contradicts_level`). |
| `software_release` | `split_version_banner`: an ESS banner split into version, revision and build as TCKDB would. |
| `rules` | TCKDB rule pre-checks with the producer's facts as arguments: `energy_level_declaration` (`assert_role_consistency`), `kinetics_energy_level_assessment` (per-participant kinetics spin), `designate_reaction_coordinate_index` (the window rule), `designate_by_tau` (the tau rule), `describe_level`. |
| `ts_evidence` | Transition-state `validation_evidence` record shapes (`irc_record`, `energy_ordering_record`, `imaginary_mode_record`) and the numeric rule that a pass its own energies contradict is not sent. |
| `ts_evidence_rules` | `offline_evidence_errors`: an offline replica of the server's evidence rules, to run on any built request. |
| `testing` | The test kit (below). Needs `pytest` and `jsonschema`; never imported by the runtime package. |

## What does not belong here

- Reading a producer's files or keys (ARC's `output.yml`, parser evidence, restart
  files). `tests/test_core_is_producer_agnostic.py` fails if a module under
  `tckdb_core` imports `arc` or `tckdb_arc`, or names `output.yml`,
  `parser_evidence`, `restart.yml` or `arc.`.
- Chemistry mappings that depend on how a producer states its results.
- Producer defaults. A producer sets `PRODUCER_TAG`, `PRODUCER_NAME` and
  `IDEMPOTENCY_NAMESPACE` on its `TCKDBUploaderBase` subclass; the core has no
  default for the tag and namespace that would be right for anyone.

## Writing a producer

```python
from tckdb_core.uploader import TCKDBUploaderBase

class MyToolAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "tckdb_mytool"       # sidecar warnings: source "tckdb_mytool_self_check"
    PRODUCER_NAME = "MyTool"
    IDEMPOTENCY_NAMESPACE = "mytool"    # keys: "mytool:<project>:..."
    LOGGER_NAME = "tckdb_mytool"        # the pipeline logs to logging.getLogger(LOGGER_NAME)
```

Hooks to override when needed: `_log` (the logger the pipeline writes to),
`_sleep_between_probes`, `_project_label_for(output_doc)`, `_log_readiness_recovery`.

### Logging

The logger is per producer, not process state (two producers can live in one process).
`TCKDBUploaderBase` logs through `self._log`, which is `logging.getLogger(cls.LOGGER_NAME)`
(`"tckdb_core"` by default); a producer sets `LOGGER_NAME`, or overrides the `_log` property to
return a logger object of its own (the ARC adapter returns its module's `logger`, so a test that
patches that module attribute intercepts every record the pipeline emits). Free functions here that
log (`serialize_constraints`, `build_nasa_block`, `a_units_to_tckdb`, ...) take a keyword `log=`;
a producer's wrapper passes its logger, and without one the record goes to the `tckdb_core` logger.
The 0.1.0 `set_logger_name` (process-global, last producer wins) is gone.

### Warning codes

Findings are emitted as `AdapterWarning(code, message, field, context)` (or `WarningSink.add`), which
renders the sidecar dict `{code, message, field, context: {source: "<producer>_self_check", ...}}`.
Codes are members of a `CodedEnum` registry; the core's own are `CoreWarning`. A producer registers
its own enum and generates its code table from it (the ARC adapter's is
`docs/contract/WARNING_CODES.md`).

## Test kit: `tckdb_core.testing`

Importable by any adapter's tests. `tckdb_core.testing` imports `pytest` and `jsonschema` at module level, so it needs the `[test]` extra (`pip install -e ".[test]"`). `tckdb_core/__init__` does not import it, so the runtime package works without those two.

| Module | Use |
|--------|-----|
| `contract` | `contract_validate(model, payload)`, `assert_request_is_accepted`, `REQUEST_MODELS`, `ROUTE_SCHEMAS`, and `TARGET_SCHEMAS_LINE`, the one tckdb-schemas line every suite pins (`tools/tckdb_drift.py --bump` rewrites it). Validates the published pydantic model, then the shipped JSON Schema. |
| `contract_hook` | `contract_checks_fixture(adapter_cls=..., payload_builders={"_build_x": "<schema name>", ...})`: the autouse fixture that checks every request the adapter builds; `register_markers(config)` for `payload_refused_by_contract`. An adapter's `conftest.py` names its own builders. |
| `backend_import` | `backend_modules()` / `backend_level_hash()`: import TCKDB's backend identity rules from `TCKDB_BACKEND_PATH` (`TCKDB_REQUIRE_BACKEND=1` turns "unavailable" into a failure). |
| `live` | The opt-in live gate's scaffolding: loopback and isolated-backend guards, retrying reads, settled row counts, the commit probe, and `live_tckdb_fixture()` for an integration `conftest.py` to register. |

## API and what is not

Public (kept stable across releases): the modules in the table above, except names with a leading
underscore. Underscore-prefixed names (for example `uploader._skip`, `payload_writer._utcnow_iso`,
`config._read_tckdb_api_key_from_env_file`) are shared internals of the producers in this repository;
import them from `tckdb_core`, never through a producer package (`tckdb_arc` no longer re-exports
them, and forwards the public names of its old modules with a `DeprecationWarning`).

## What changed in 0.5.2

No contract change (same pin). `route_level` documents that its parser is Gaussian-only: callers compare a route with a level only when the record states the job ran in Gaussian, because an ORCA keyword line carries auxiliary-basis tokens with a slash (`aug-cc-pvtz/c`, `def2/J`) that are not a `method/basis` pair. The functions are unchanged.

## What changed in 0.5.1

Tracks tckdb-schemas 0.161 / client 0.189 (TCKDB cd84fb6, #870). `rules.kinetics_energy_level_assessment` (and `kinetics_energy_level_declaration`, which returns its `level`) now mirrors TCKDB's per-participant spin rule instead of requiring every linked energy at one identical level: the links must agree on every identity field except `spin_treatment`; closed-shell links never constrain spin; the open-shell links name the declared `spin_treatment`, and open-shell links with different references declare nothing and are reported in `mixed_spin_calculations`. It needs two more facts per call, `multiplicities` (calculation key to the owning participant's multiplicity) and each calculation's own `spin_diagnostic.s_squared`. Breaking for direct callers: `multiplicities` is a required argument.

## What changed in 0.5.0

Tracks tckdb-schemas 0.158 / client 0.187 (TCKDB 531f823, #865 typed method qualifiers). The level-identity replica (`level_rules`) follows TCKDB's hash: the `keywords` key is gone (free-text keywords left `LevelOfTheoryRef`, so every level hashes differently from before, as on the server), and `relativistic_hamiltonian`, `pno_truncation` and `ri_approximation` join the payload only when stated, as `core_treatment` does (which now also accepts `window`); an explicit `none` is a stated value. `tckdb_core.testing.backend_import.backend_level_hash` builds the same payload. A level that states no qualifier hashes as the server does (pinned to TCKDB's own literals in `tests/test_level_rules.py`).

### Checking the hash against TCKDB's own function

`tckdb_arc/tests/test_energy_level_declaration.py` has two backend parity tests, and they are not the same check:

* `test_hash_matches_the_backend_payload_rules` runs wherever `TCKDB_BACKEND_PATH` names a TCKDB `backend/`. Without the backend's web dependencies (`arc_env` has no fastapi) it rebuilds the payload from the backend's own `app.chemistry` identity keys, so it is the replica against the replica for the final hashing step.
* `test_hash_matches_the_real_backend_function` imports `app.services.calculation_resolution._level_of_theory_hash` and compares the replica against it over the corpus and a few thousand method x basis x dispersion x solvent x spin x core x qualifier combinations. It never falls back: it **skips**, naming the reason, when that import fails. Run it in the TCKDB environment:

```bash
TCKDB=/path/to/TCKDB_v2   # a checkout at the pinned revision (tckdb-pin.toml)
cd tckdb_arc
TCKDB_BACKEND_PATH=$TCKDB/backend TCKDB_REQUIRE_BACKEND=1 \
PYTHONPATH=$TCKDB/backend:$TCKDB/schemas/python/tckdb-schemas \
  conda run -n tckdb_env python -m pytest tests/test_energy_level_declaration.py -k hash_matches -rs
```

Both must pass (not skip) before a pin bump is trusted.

## What changed in 0.4.3

Tracks tckdb-schemas 0.156 / client 0.186 (TCKDB 6a91b38); no behaviour change in this package.

## What changed in 0.4.2

A `/readyz` preflight that fails without an HTTP response (connect timeout, connection refused) now says why: the exception text, its type chain, the `base_url` host and the request timeout, instead of nothing after the colon; HTTP-level messages are unchanged.

## What changed in 0.4.1

Tracks tckdb-schemas 0.154 / client 0.184 (the pin of 0.4.0); no behaviour change in this package.

## What changed in 0.4.0 (batch M)

New rules written against the tckdb-schemas 0.74 to 0.134 contract (the tested line is 0.154 / client 0.184). New pre-checks in `tckdb_core.rules`: `geometry_anchor_refusals` (the
0.121 key rules, run through the shipped `bundle_source_rules` helpers), `undeclarable_input_geometries` (a replica of
`calculation_input_geometry_mismatch`) and `kinetics_energy_level_declaration` (0.99); in `tckdb_core.isotopes`:
`subject_isotope_multiset` and `unlabelled_point_geometries` (0.94). The uploader no longer posts artifacts for a
calculation without a `calc_` ref (0.116), and `CoreWarning.CALCULATION_REF_NOT_RETURNED` now says so; the
`_calculation_ref_not_returned_warning` helper is gone. `TARGET_SCHEMAS_LINE` is `(0, 154)` (set by the 0.3.0 pin bump).

## What moved in 0.2.0 (batch L2)

From the ARC adapter, with no behaviour change: the thermo numerics, the RMG unit tables, the
reaction-route result flattening, the element/formula/xyz helpers and the energy constant, the isotope
and route-line rules, the software-banner split, the TCKDB rule pre-checks (energy-level declaration,
reaction-coordinate designation, evidence shapes and their offline replica), the contract test kit and
the live-gate scaffolding, the warning-code registry type, and per-producer logging. What reads one of a
producer's own keys (ARC's `ts_checks`, `xyz_isotopes`, `thermo.*` fields, level dicts) stayed in
`tckdb_arc` and calls these with its facts as arguments.

## Tests

```bash
pip install -e ".[test]"
TCKDB_BACKEND_PATH=/path/to/TCKDB/backend TCKDB_REQUIRE_BACKEND=1 pytest -q
```

Without `TCKDB_BACKEND_PATH` the backend comparison of `level_rules` skips. See "Running the tests"
in the repository README for running both packages' suites together.
