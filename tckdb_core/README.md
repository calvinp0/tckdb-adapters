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
| `warnings` | `AdapterWarning` and `WarningSink`: the sidecar's warning dict shape, with the producer tag as a parameter. |

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
```

Hooks to override when needed: `_log` (the logger the pipeline writes to),
`_sleep_between_probes`, `_project_label_for(output_doc)`, `_log_readiness_recovery`.
A producer package also calls `tckdb_core._logging.set_logger_name("<its logger>")`
once at import so the shared code logs under its own name. `set_logger_name` is
process-global (the last producer to call it wins), and core's module-level log
records bypass any patch of `tckdb_arc.adapter.logger` (L2 will route them through
the instance).

## Tests

```bash
pip install -e ".[test]"
TCKDB_BACKEND_PATH=/path/to/TCKDB/backend TCKDB_REQUIRE_BACKEND=1 pytest -q
```

Without `TCKDB_BACKEND_PATH` the backend comparison of `level_rules` skips.
