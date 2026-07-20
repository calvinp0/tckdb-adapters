# Phase 2 brief — ARC-side dual-path shim

**Self-contained: runnable by any coding agent (e.g. Codex) with no prior conversation context.**
Everything needed is in this file. Companion docs in this folder: `tckdb_arc_rehoming_plan.md`
(the full plan), `PHASE_LOG.md` (what Phase 1 did — append your Phase 2 entry there when done).

## What Phase 2 is (and is NOT)
Make ARC **prefer the installed standalone `tckdb-arc` package** and **fall back to the in-tree
`arc/tckdb/`** if it isn't installed. This is an *additive shim only*.
- Do **NOT** delete `arc/tckdb/` or its tests — that is Phase 4.
- Do **NOT** change any payload/upload logic.
- Do **NOT** touch the `tckdb-adapters` repo (Phase 1 is done and committed).

## Background you need
Phase 1 extracted ARC's `arc/tckdb/` into the standalone repo `tckdb-adapters` (pip package
`tckdb-arc`). The package exposes the **identical public names**, so they are drop-in:
- `tckdb_arc.config.TCKDBConfig`      ⇔ `arc.tckdb.config.TCKDBConfig`
- `tckdb_arc.sweep.run_upload_sweep`  ⇔ `arc.tckdb.sweep.run_upload_sweep`
- `tckdb_arc.adapter.TCKDBAdapter`    ⇔ `arc.tckdb.adapter.TCKDBAdapter`  (also `UploadOutcome`)

ARC still contains `arc/tckdb/` unchanged; both coexist until Phase 4.

## Exact edits — ARC repo, file `ARC.py`
There are exactly **two** import sites that reference `arc.tckdb`.

**(1) Top-level imports, `ARC.py:14-15`:**
```python
from arc.tckdb.config import TCKDBConfig
from arc.tckdb.sweep import run_upload_sweep
```
Replace with:
```python
try:
    from tckdb_arc.config import TCKDBConfig
    from tckdb_arc.sweep import run_upload_sweep
except ImportError:  # in-tree fallback until Phase 4 removes arc/tckdb/
    from arc.tckdb.config import TCKDBConfig
    from arc.tckdb.sweep import run_upload_sweep
```

**(2) Inline import, `ARC.py:80` (inside `if tckdb_config is not None:`):**
```python
from arc.tckdb.adapter import TCKDBAdapter
```
Replace with:
```python
try:
    from tckdb_arc.adapter import TCKDBAdapter
except ImportError:
    from arc.tckdb.adapter import TCKDBAdapter
```
No other change to `ARC.py` — the usage (`TCKDBConfig.from_dict(...)` at :65, `TCKDBAdapter(...)`
at :81, `run_upload_sweep(...)` at :82) is unchanged because the names are identical.

## ARC git discipline — MANDATORY (this lives in ARC's `CLAUDE.md`, NOT `AGENTS.md`)
- **Branch off `main`** (this is a core ARC change; never commit it to `arcbench` directly):
  `git checkout main && git pull origin main && git checkout -b feature_tckdb_arc_dualpath_shim`
- **No `Co-Authored-By` line** in the commit message (the repo owner forbids it).
- **Never open a PR** (`gh pr create`) unless the user explicitly asks in that instance.
- Push the branch: `git push origin feature_tckdb_arc_dualpath_shim`
- **Bring it into `arcbench` by CHERRY-PICK** (never merge a main-based branch into arcbench):
  `git checkout arcbench && git cherry-pick <commit-sha>`
  then **mirror-push both remotes**: `git push origin arcbench && git push origin arcbench:crest_adapter`
- **Update the branch ledger** `~/code/arcbench/BRANCHES.md`: add the branch, its base (`main`), the
  arcbench cherry-pick SHA, deployed y/n, status=active, main-PR-able=y, theme=TCKDB-extraction.
- **Deploy to zeus:** `ssh calvin.p@zeus.technion.ac.il 'cd ~/Code/ARC && git pull origin crest_adapter'`
  and install the package into `arc_env`:
  `conda run -n arc_env pip install "tckdb-arc @ git+https://github.com/calvinp0/tckdb-adapters.git@main#subdirectory=tckdb_arc"`
  (Reminder: zeus ARC driver PBS jobs must pin `host=n170`.)

## Verify
1. **In-tree still works with the package NOT installed** (fallback path):
   `HOME=$(mktemp -d) RMG_DB_PATH=/home/calvin/code/RMG-database conda run -n arc_env python -m pytest arc/tckdb/ -o addopts="" -p no:cacheprovider -q`
2. **Installed package wins when present:** with `tckdb-arc` installed in `arc_env`,
   `conda run -n arc_env python -c "import ARC; from ARC import TCKDBConfig; print(TCKDBConfig.__module__)"`
   → must print `tckdb_arc.config` (not `arc.tckdb.config`).
3. **End-to-end:** run one `arcbench` reaction whose `input.yml` has a `tckdb:` block; confirm the
   sweep produces the same payloads/output as before (PayloadWriter output byte-identical).

## When done
Append a **Phase 2** entry to `~/code/tckdb-adapters/docs/PHASE_LOG.md`: date, the two edits, the
branch name + commit SHA, the arcbench cherry-pick SHA, zeus deploy status, and the verification
result. That file is the shared memory for the next agent (Phase 3 = emit the `tckdb_evidence`
sidecar from `arc/output.py`; Phase 4 = delete `arc/tckdb/`).
