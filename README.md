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

See the [current ARC integration audit](docs/contract/CURRENT_ARC_INTEGRATION.md)
for verified mappings, validation results, and remaining producer/server gaps.

## The shared layer (pinned)

The tested contract is `tckdb-client` 0.102.x with `tckdb-schemas` 0.64.x
(the producer contract ships from 0.52,
`python -m tckdb_schemas.contract`). CI pins both to the same tested TCKDB
source revision (`tckdb-pin.toml`):

```bash
pip install \
  "tckdb-client @ git+https://github.com/calvinp0/tckdbv2.git@f22d3a8072273a9b3c4c1c31f0d9d1dd2c5d86f2#subdirectory=clients/python" \
  "tckdb-schemas @ git+https://github.com/calvinp0/tckdbv2.git@f22d3a8072273a9b3c4c1c31f0d9d1dd2c5d86f2#subdirectory=schemas/python/tckdb-schemas"
```

For local development against a `TCKDB_v2` checkout, install from the local path
instead (fast, no network):

```bash
pip install /path/to/TCKDB_v2/clients/python /path/to/TCKDB_v2/schemas/python/tckdb-schemas
```

## Tracking TCKDB releases

The TCKDB commit CI installs from lives in `tckdb-pin.toml` (repo, `sha`, package
subdirectories), which `ci.yml` reads at runtime. No workflow file holds a SHA, so an
automated bump never edits `.github/workflows/` and `GITHUB_TOKEN` can push it.

`.github/workflows/tckdb-drift.yml` runs daily (and on demand). Its `test` job has a
read-only token: it compares the adapter's pinned `tckdb-schemas` / `tckdb-client`
minor lines with the mirror's `main`; on a newer line it bumps the pins in the
runner, installs both packages from that commit, and runs the full suite. Its
`publish` job (the only one with write access, `tools/tckdb_drift_publish.sh`) then:

- **tests pass:** pushes `chore/tckdb-drift-schemas-<X.Y>-client-<A.B>` and opens (or
  updates) the PR "Track TCKDB: schemas X.Y.Z / client A.B.C (TCKDB <sha7>)", then
  starts `ci.yml` on the branch. The bot has already run the suite and moved
  `TARGET_SCHEMAS_LINE`; the PR body carries `python -m tckdb_schemas.contract
  --since <old pin>`, and **your job is to read that changelog and decide**, not to
  trust the green run.
- **tests fail:** opens (or updates) the issue "TCKDB drift: schemas X.Y / client A.B
  breaks the adapter" with the failing tests; an install failure is titled "... install
  failed".
- **tests pass but no PR could be made** (push or PR creation failed): the issue
  "TCKDB drift: schemas X.Y / client A.B is ready to adopt" names the branch or the
  `--bump` command.

Items are keyed by the line pair (X.Y / A.B), so a patch release updates the existing
PR or issue instead of adding one. An issue is commented on only when the versions or
the failing tests change. A drift issue is closed automatically when a later run finds
no drift or the PR path succeeds; an older bot PR is commented as superseded when a
newer line appears (never closed).

**Branch safety.** The bot never overwrites work on its branch. If the remote branch
already has the new tree, nothing is pushed (the PR is only refreshed and CI is not
re-dispatched). If it holds any commit not authored by `github-actions[bot]`, nothing is
pushed and the PR gets a comment with the `--bump` command. Otherwise the branch is
updated with `--force-with-lease` against the commit the run saw.

Run the same check locally (stdlib only; JSON on stdout, one summary line on stderr;
exit 0 = no drift, 10 = drift, anything else = error):

```bash
python tools/tckdb_drift.py --check
python tools/tckdb_drift.py --check --repo /path/to/or/url --ref some-branch
python tools/tckdb_drift.py --bump --sha <40-hex> --schemas X.Y.Z --client A.B.C
python tools/tckdb_drift.py --since-output --from <old X.Y.0>   # needs the new tckdb-schemas installed
```

`--bump` moves the `pyproject.toml` bounds, `TARGET_SCHEMAS_LINE`, the `sha` in
`tckdb-pin.toml`, the install lines and "tested contract" sentence here, the version
sentence and a changelog line in `tckdb_arc/README.md`, and the adapter patch version.
Running it twice changes nothing; it refuses to move a line backwards unless given
`--allow-downgrade`. After a bump, pass `--from` the pre-bump line to `--since-output`.

When the PR or issue appears, the contract-reading rule in `CLAUDE.md` still applies:
read the `--since` output (and `python -m tckdb_schemas.contract --print`) before merging
or fixing. A green suite only shows the payloads the tests build still validate; it does
not show that a new required field or convention is handled correctly.

Setup and caveats:

- Enable Settings > Actions > General > "Allow GitHub Actions to create and approve
  pull requests". That is the only required setting; optionally create a `tckdb-drift`
  label (applied when present).
- `workflow_dispatch:` in `ci.yml` must be on `main` before the first dispatch of CI on a
  bot branch works (this change adds it), and the drift workflow itself only runs from
  the default branch.
- GitHub disables scheduled workflows after 60 days without repository activity; re-enable
  it under Actions if the daily run stops.

## `tckdb-arc`

```bash
cd tckdb_arc
pip install -e ".[test]"        # + the shared packages above
pytest                          # base (no-ARC) suite
```

`tckdb-arc` bounds both shared packages at runtime so fresh installs use
the contract exercised by the tests.
The CLI entry point is `tckdb-arc-upload`.

### The optional `[arc]` extra

Three payload paths (freq-Hessian, IRC trajectory, GSM string-file) can reparse
ESS logs via ARC's `arc.parser` when portable evidence is absent. **ARC is not pip-installable as `arc`** — it is a
conda/PYTHONPATH install — so the `[arc]` extra is a *documented marker*, not a
pip-resolvable dependency. When ARC is not importable those three sub-payloads
degrade gracefully (they are omitted, not errored) via
`tckdb_arc._arc_optional`. The arc-gated tests run only where ARC is already on
the path (they `pytest.importorskip("arc")` otherwise).

The primary path is ARC's `parser_evidence` descriptor and matching
`parser_evidence.json` beside `output.yml`. The legacy `tckdb_evidence`
descriptor/file remains supported. Valid sidecars supply all three evidence
types without installing ARC or retaining raw calculation logs.

### Live integration gate

`tckdb_arc/tests/integration/` uploads the offline corpora to a TCKDB backend
running on this machine and checks what it stored. It is skipped unless
`TCKDB_INTEGRATION_URL` is set, and refuses any host other than loopback. See
[docs/contract/INTEGRATION_GATE.md](docs/contract/INTEGRATION_GATE.md).
