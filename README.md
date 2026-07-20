# tckdb-adapters

Per-tool extractors that push computed-chemistry results to
[TCKDB](https://github.com/calvinp0/tckdbv2). Each adapter reads a producing
tool's output and builds validated payloads for the shared **`tckdb-client`**
transport against the shared **`tckdb-schemas`** wire-contract.

> "Extract a tool's computed-chemistry results and push them to TCKDB" is a
> *tool-agnostic* concern. This repo hosts one adapter per producer; they share
> one schema and one transport but are independently installable and versioned.

## Packages

| Package    | Path         | Status | Description |
|------------|--------------|--------|-------------|
| `tckdb-arc`| `tckdb_arc/` | active | ARC → TCKDB upload adapter (payload builder + sidecar writer + uploader). |
| `tckdb_rmg`| —            | future | RMG adapter (placeholder; not yet created). |
| `tckdb_chemtrayzer` | — | future | Chemtrayzer adapter (placeholder). |

The three-package ecosystem:

- **`tckdb-schemas`** (in `TCKDB_v2`) — canonical Pydantic wire-contract. Owns the schema.
- **`tckdb-client`** (in `TCKDB_v2`) — HTTP transport + idempotency + replay. Tool-neutral.
- **`tckdb-adapters`** (this repo) — per-tool extractors. Depend on the two above;
  each *may* optionally depend on its own tool.

The dependency direction is acyclic: `tckdb_arc → {tckdb-client, tckdb-schemas, arc(optional)}`.
Nothing in the shared layer ever points back at a producing tool.

## The shared layer (pinned)

Both shared packages are pinned to git tags on `calvinp0/tckdbv2`:

```bash
pip install \
  "tckdb-client @ git+https://github.com/calvinp0/tckdbv2.git@tckdb-client-v0.27.1#subdirectory=clients/python" \
  "tckdb-schemas @ git+https://github.com/calvinp0/tckdbv2.git@tckdb-schemas-v0.8.0#subdirectory=schemas/python/tckdb-schemas"
```

For local development against a `TCKDB_v2` checkout, install from the local path
instead (fast, no network):

```bash
pip install /path/to/TCKDB_v2/clients/python /path/to/TCKDB_v2/schemas/python/tckdb-schemas
```

## `tckdb-arc`

```bash
cd tckdb_arc
pip install -e ".[test]"        # + the shared packages above
pytest                          # base (no-ARC) suite
```

`tckdb-arc` depends on `tckdb-client` at runtime and `tckdb-schemas` in tests.
The CLI entry point is `tckdb-arc-upload`.

### The optional `[arc]` extra

Three payload paths (freq-Hessian, IRC trajectory, GSM string-file) reparse ESS
logs via ARC's `arc.parser`. **ARC is not pip-installable as `arc`** — it is a
conda/PYTHONPATH install — so the `[arc]` extra is a *documented marker*, not a
pip-resolvable dependency. When ARC is not importable those three sub-payloads
degrade gracefully (they are omitted, not errored) via
`tckdb_arc._arc_optional`. The arc-gated tests run only where ARC is already on
the path (they `pytest.importorskip("arc")` otherwise).

The forward-looking primary path for those three payloads is an ARC-emitted
`tckdb_evidence` sidecar (a later phase); until it lands, base installs omit
them and `[arc]`-on-PYTHONPATH installs reparse.
