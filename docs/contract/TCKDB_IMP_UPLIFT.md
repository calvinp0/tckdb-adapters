# `tckdb-imp` → `feature_arc_result_export_contract` uplift plan

**Status:** plan only. No code was changed in either repo.
**Repos:** ARC at `/home/calvin/code/ARC`; adapter at `/home/calvin/code/tckdb-adapters`.
**Merge-base (`tckdb-imp`/export):** `59ac3f01f9cd927c33c8dc050ea7fbde257136a0`.
**Branches:** `tckdb-imp` (head `5093deb9`, abandoned embedded-upload architecture) vs
`feature_arc_result_export_contract` (head `1f84d56b`, current neutral-contract architecture) vs
`arcbench` (head `d5ef3dc5`, the user's private working branch — see correction below).
**Method:** `git show <branch>:<path>` reads plus `git cherry-pick -n` trials in a throwaway
worktree cut from `feature_arc_result_export_contract`. The worktree was deleted; neither branch,
the main checkout, nor `/home/calvin/code/ARC.worktrees/feature_arc_result_export_contract` was
touched. Every claim in the correction below and in Table 1 was re-derived from `git show`/`git log`
against `arcbench` and `origin/main`; no branch, worktree, or working tree was modified.

---

## ⚠ Baseline correction (2026-08-12) — read this before anything else below

The original version of this plan (everything from "Table 1 — PORT" onward, before this notice)
compared `tckdb-imp` against `feature_arc_result_export_contract` and proposed porting 9 items from
the former. **That comparison used the wrong baseline for 7 of the 9 items.**

The user maintains **`arcbench`** as their private working branch — where fixes are built and PRs
are cut from, never a merge or rebase target for anything else. Measured against `origin/main`:

| branch | vs `origin/main` |
|---|---|
| `feature_arc_result_export_contract` | **5 behind, 9 ahead** — essentially current; its 9 commits *are* the TCKDB export-contract work (`50a3e77b`, `158f11f6`, `938146b3`, `24fe03be`, `9cd92238`, `2e855c24`, `6f3966b6`, `e4867bf5`, `1f84d56b`) |
| `arcbench` | **98 behind, 284 ahead** — a large body of unmerged, in-flight work |

**All 9 of the original PORT items are already fixed on `arcbench`** — 7 of them byte-identical (or
functionally identical) to `tckdb-imp`'s version, 2 of them fixed in a materially *better* form than
`tckdb-imp` has. **None of the 9 are in `origin/main` yet.** Porting any of them from `tckdb-imp` would
at best duplicate work the user has already done on `arcbench`, and at worst reintroduce an older,
less-refined version of a fix `arcbench` has since improved on.

**The user's decision this document now supports:** hold off on general ARC bug fixes in
`feature_arc_result_export_contract` and let `arcbench`'s work reach `origin/main` through its normal
PR flow; once it does, catch the export branch up with a plain 5-commit fast-forward. Only
**TCKDB-specific** work — code meaningful *only* because of the export contract, which will never
arrive via a general-purpose PR to `main` — proceeds against the export branch now.

Consequently:
- **Table 1 (PORT) is rewritten below** as a two-way split — TCKDB-specific (act now) vs. general ARC
  fix (already fixed on `arcbench`, wait for it to land on `main`) — with `arcbench` file:line evidence
  and an `origin/main` status column for each item. The old single-axis PORT verdicts are superseded.
- **A reverse-direction survey is added**, re-aimed per the same logic: not "what does `arcbench` have
  that the export branch lacks" in general (that's 284 commits of ordinary ARC development the user
  will get for free once their PRs land), but specifically *what is TCKDB-export-relevant on
  `arcbench` that would **not** naturally arrive via a general-purpose PR to `main`* — the one category
  that needs a deliberate decision from the user.
- **No rebase/merge recommendation is made.** `arcbench` is not a target to rebase or merge anything
  onto or pull code from wholesale; it is cited here purely as evidence for "is this already fixed,
  and how."
- **Everything else in this document is unaffected and stands as written**: the `scan_software` /
  Q1 provenance findings, the `index_base` double-shift warning, Table 2 (ALREADY PRESENT), Table 3
  (EXCLUDE), Table 4 (UNRELATED), and Q2–Q5. Where `arcbench` evidence adds to or resolves an item in
  those sections (e.g. the `seed_hub.py` open question), it is noted inline, marked **[arcbench
  update]**.

---

## Summary — be honest about the scale

> This section still correctly inventories *what `tckdb-imp` has that the export branch lacks on
> the data-export axis* — that inventory is unaffected by the baseline correction above. What changed
> is the **disposition**: items 1–3 below are general ARC fixes, already present on `arcbench`,
> deferred to the `main` fast-forward path (Table 1b); item 4 is the one TCKDB-specific item and
> still ships now (Table 1a). Read "PORT" below as "the gap exists" — for *where the fix comes from*,
> see the baseline correction and Table 1.

The 45 commits are **43 real commits plus 2 no-op merges** of `origin/main`
(`cbff7fff`, `f31bb299`; `git rev-list --count` confirms they contributed zero
new commits beyond the branch's own 43).

**`tckdb-imp` has almost nothing the export contract lacks on the data-export axis.** Every
TCKDB-relevant parse, derivation and emission on `tckdb-imp` — Cartesian Hessians, `<S**2>` spin
diagnostics, GSM path-search points, constraint parsing, applied energy corrections, TS-guess path
provenance, the evidence sidecar — is already present on `feature_arc_result_export_contract`, and
in most cases the export version is a *strict correctness improvement* over `tckdb-imp` (frame-matched
Hessian geometry, geometry-matched GSM energies, full `imaginary_frequencies_cm1` list, explicit
`index_base`, `matched_arkane_key` guard on BAC parameter tables). `arc/parser/parser.py` is
**byte-identical** between the branches.

What `tckdb-imp` genuinely has that the current branch lacks is a **handful of small ARC-side bug
fixes that never made the jump**, in descending order of value:

1. **`ARCSpecies._init_monoatomic_geometry()`** and **`species_is_ready_for_e0()`** — together these
   unblock reactions with atomic participants (`[H]`, `[O]`, `[Cl]` — i.e. most H-abstractions);
2. the **Arkane A+A duplicate-species thermo recovery** — without it, thermo is `None` for any
   reaction with two identical reactants, so there is nothing to export;
3. a `Level.as_dict()` round-trip fix, an ORCA NEB level-string fix, and a post-run `KeyError` guard;
4. the **`scan_software` scheduler stamp** — the only TCKDB-facing item, two lines. It is a
   correctness-by-construction fix, **not** a live bug: measured over 198 real ESS artifacts, the
   current branch's Gaussian fallback produces byte-equivalent constraint output to `tckdb-imp`'s
   stamped dispatch (see Q1, which resolves the sharpened hypothesis).

Everything else splits into: superseded by the adapter re-home (10 commits, all `arc/tckdb/`),
already present (12), or ordinary unrelated backports (12 — Molpro memory cards, SSH hardening, TS
troubleshooting, mapping log noise, cumulene heuristics).

### Counts

| Class | Commits |
|---|---|
| **PORT** | 9 (2 of them partial — only a slice of the commit ports) |
| **ALREADY PRESENT** | 12 |
| **EXCLUDE** | 10 |
| **UNRELATED** (worth backporting on own merit) | 12 |
| merge commits (no content) | 2 |
| **total** | **45** |

---

## Table 1 — PORT (revised under the baseline correction)

**This table replaces the original single-axis PORT verdicts.** The original framing — "does
`tckdb-imp` have a capability the export branch lacks, so port it" — is no longer the right question
for any of these 9 items, because for all 9 the honest first question is "has `arcbench` already
fixed this, and how." It has, for all 9. The remaining question is only *where the fix should land
and when*, which splits cleanly on one axis: **is this meaningful only because of the TCKDB export
contract (act now, directly on `feature_arc_result_export_contract`), or is it a general ARC
correctness fix with no dependency on TCKDB (already fixed on `arcbench`; it will reach this branch
for free via `arcbench`'s normal PR flow into `origin/main`, followed by a 5-commit fast-forward — do
not re-derive it from `tckdb-imp` in the meantime).**

Evidence method: `git show arcbench:<path>`, `git show origin/main:<path>`, and `diff` against the
corresponding `tckdb-imp` extract, all read-only against `/home/calvin/code/ARC`. No branch or
worktree was touched.

### 1a — TCKDB-specific: act now, directly on the export branch

Meaningful only because `output.yml`/the evidence sidecar exist — machinery `origin/main` doesn't
have in this shape, so a general PR would have nowhere to land this. `tckdb-imp` is *not* the source
for these; `arcbench` already carries them, byte-identical, so take the two-line/one-attribute diff
from `arcbench` (equivalently from `tckdb-imp`, since the two agree) directly onto the export branch.

| sha | subject | `arcbench` status | `origin/main` status | action |
|---|---|---|---|---|
| `a63a1fe4` | `scan_software` scheduler stamp | **Present, byte-identical to `tckdb-imp`.** `arcbench:arc/scheduler.py:1369` (`end_job`) and `:3467` (`check_scan_job`); both are `a63a1fe4` verbatim — that exact commit sha is a literal ancestor of `arcbench` (it entered via `6c91833b "Merge branch 'tckdb-imp' into crest_adapter"`). Its only consumer anywhere in the tree is `arc/output.py`'s `_scan_constraint_parser_for` (confirmed by `git grep`) — nothing else reads the field, which is why this is TCKDB-only. | Absent — `origin/main:arc/scheduler.py` has no `scan_software` occurrences at all, and `origin/main`'s `arc/output.py` (687 lines, the pre-export-contract shape) has no constraint-parser dispatch to feed. | **Act now.** Apply the 2-line hand patch to `feature_arc_result_export_contract:arc/scheduler.py` at the sites given in the original Port 1 (below) — sourced from `arcbench`, confirmed identical to `tckdb-imp`. |
| `530b9b8c` (attribution half only — see 1b split below) | TS-guess method attribution → `TSGuess.level` | **Present, materially better than `tckdb-imp`.** `process_completed_tsg_queue_jobs` (`arcbench:arc/species/species.py:1839`) is byte-identical to `tckdb-imp` except at two internal sites (`:1860`, `:1878`): `tsg.index = self.get_next_tsg_index()` where `tckdb-imp` has `tsg.index = len(self.ts_guesses)`. `get_next_tsg_index()` (`arcbench:arc/species/species.py:1820`, added by a later, unrelated `arcbench` commit `8dc0fae9 "Stop conflating a TS guess's identity with its position in a list"`) fixes an index-collision bug: after clustering removes some guesses while preserving survivor indices, `len()` can hand out an index already in use. Porting `tckdb-imp`'s `len()` form verbatim would reintroduce that collision. | Neither `process_completed_tsg_queue_jobs` nor `get_next_tsg_index` exist on `origin/main`. | **Act now, but take `arcbench`'s form, not `tckdb-imp`'s.** Port `process_completed_tsg_queue_jobs` with the `get_next_tsg_index()` call, and port `get_next_tsg_index()` itself (17 lines, additive) as a prerequisite. |
| `ff7a5762` minus `crest.py` | `TSGuess.level` attribute + `plain_level_dict` | **Present, functionally identical to `tckdb-imp`** — `plain_level_dict` (`arcbench:arc/level.py:457`), `TSGuess.__init__`/`as_dict`/`from_dict` level plumbing (`arcbench:arc/species/species.py` ~`2610`/`2699`–`2700`/`2767`), population in `orca_neb.py:331` and `xtb_gsm.py:401`. Only textual difference: `_normalize_method_sources`'s type hint reads `list[str] \| None` on `arcbench` vs. `Optional[List[str]]` on `tckdb-imp` — matches the PEP-604 style export already uses elsewhere (same conclusion Table 2 already reached for `cea70fb5`). The only consumer of `TSGuess.level` in production code, on every branch, is `arc/output.py:442`'s `'level': getattr(tsg, 'level', None)` inside `_ts_guesses_to_list` — confirming this is TCKDB/output.yml-only, not general ARC state. **[arcbench update]** Unlike `tckdb-imp`, `arcbench`'s `crest.py` is **not** dead code: `arc/job/adapters/ts/seed_hub.py` (351 lines) exists there and is imported successfully (`git ls-tree arcbench -- arc/job/adapters/ts/` lists both files; `register_job_adapter('crest', CrestAdapter)` runs). This resolves the "could not be established" item in the original doc — the file exists, just not on `tckdb-imp`. It changes nothing about the port itself: `crest.py`/`seed_hub.py` are still out of scope for this plan either way. | Absent. | **Act now.** Port `plain_level_dict` + the `TSGuess.level` plumbing (still excluding `crest.py`/`seed_hub.py`, now for a different reason: not dead code on `arcbench`, but CREST-adapter work is out of scope for this plan, not a TCKDB-export gap). |

### 1b — General ARC fix: already fixed on `arcbench`, wait for `main`

Real ARC bugs with no dependency on TCKDB or `output.yml`. `arcbench` already carries a fix for every
one of them. **Do not port these from `tckdb-imp`** — that would risk shipping an older, less-refined
version of a fix the user has already written and is carrying toward `main` on their own branch.

| sha | subject | `arcbench` status | `origin/main` status | action |
|---|---|---|---|---|
| `ca7ce141` (partial) | `ARCSpecies._init_monoatomic_geometry()` | **Present, byte-identical to `tckdb-imp`, including the call site.** `arcbench:arc/species/species.py:565` (definition), called at `:561` from `__init__` (`self.set_mol_list(); self._init_monoatomic_geometry()`) — the coordinator's confirmed example. `ca7ce141` is a literal ancestor commit of `arcbench` (same mechanism as the `scan_software` stamp above). | **Confirmed absent.** `git show origin/main:arc/species/species.py \| grep _init_monoatomic_geometry` returns nothing. | **No action on the export branch now.** Fixed upstream of `main` on `arcbench`; will arrive via `arcbench`'s PR flow, then a fast-forward. |
| `fe3161f9` | `species_is_ready_for_e0()` | **Present, function body byte-identical to `tckdb-imp`** — `arcbench:arc/scheduler.py:4772`, used at `:3065` and `:3195`. **But the call site retains the same less-safe pattern `tckdb-imp` has, which export already improved on**: both `tckdb-imp` and `arcbench` index `self.output[spc_label]` directly in the `all([...])` comprehension inside `check_rxn_e0_by_spc`; export's `check_rxn_e0_by_spc` (`feature_arc_result_export_contract:arc/scheduler.py:2858-2862`) iterates `self.output.items()` filtered by `if spc_label in labels` — a `KeyError`-safe form export already has that neither `tckdb-imp` nor `arcbench` has adopted for this function. | Absent — no `species_is_ready_for_e0` on `origin/main`. | **No action on the export branch now**, and when this does land (via `main`, from `arcbench`), **hand-merge required**: keep export's safe `.items()` iteration, swap the predicate to `species_is_ready_for_e0(...)` — exactly the Port 2b instruction in the original plan below, just now confirmed to apply against `arcbench`'s version too, not only `tckdb-imp`'s. |
| `1484b545` | Arkane A+A duplicate-species thermo recovery | **Present, and strictly better than `tckdb-imp`.** `_dedup_thermo_species_list`/`_propagate_duplicate_species_thermo` (`arcbench:arc/statmech/arkane.py:435`/`:477`, called `:414`/`:634`) diff empty against `tckdb-imp`. `arc/scripts/save_arkane_thermo.py` is **byte-identical in full** between `tckdb-imp` and `arcbench` (`diff` empty). Unlike `tckdb-imp`, `arcbench` **also** carries the export-only SMILES/adjacency-list singlet-carbene fix (`arcbench:arc/statmech/arkane.py:362-379`, same shape as export's `43c0adc4`) and `_summarize_arkane_stderr` (`:764`) — i.e. `arcbench` has both fixes the original plan said must coexist ("Keep both — `43c0adc4` prevents the singlet-carbene crash, dedup prevents the duplicate-entry crash"), already correctly threaded together. It also already has the optional "3c" asymmetry fix from the original plan (`if not run_arkane(statmech_dir): return` in `compute_thermo`, with the comment "matches the kinetics caller's gate") — but that one turns out to be on `tckdb-imp` too, byte-identical; the original doc's "optional 3c" framing undersold it as already-shipped. | Absent — no `_dedup_thermo_species_list` on `origin/main`. | **No action on the export branch now.** When it lands via `main`, take `arcbench`'s form (superset of `tckdb-imp`'s). |
| `822e8fc0` | `Level.as_dict()` args round-trip | **Present, byte-identical to `tckdb-imp`.** `arcbench:arc/level.py:190`: `if (val is not None and key != 'args') or (key == 'args' and any(v for v in self.args.values())):`. | **Confirmed still broken on `origin/main`** — `origin/main:arc/level.py:190` is the pristine `... or key == 'args' and all([v for v in self.args.values()])` pre-fix line, same as export currently has. | **No action on the export branch now.** One-line fix; will arrive via `main`. |
| `530b9b8c` (NEB-level-string half — see 1a for the attribution half) | ORCA NEB default level string | **Present, byte-identical to `tckdb-imp`.** `arcbench:arc/settings/settings.py:270`: `'level': 'wb97x-d3/def2tzvp',  # ORCA spelling; it does not accept Gaussian's 'wb97xd'`. | **Confirmed still broken on `origin/main`** — `origin/main:arc/settings/settings.py:289` is `'level': 'wb97xd/def2tzvp',` with no comment; ORCA rejects that keyword, so the default NEB job still errors on `main` today. | **No action on the export branch now.** This is a hard functional break independent of TCKDB (any ORCA NEB job with default settings fails); will arrive via `main`. If the user runs ORCA NEB jobs against the export branch before `arcbench`'s PR lands, this one-word fix is cheap enough to justify an exception — flagged, not decided here. |
| `aa82723c` | `KeyError` on deleted IRC species in `save_project_info_file` | **Present, byte-identical to `tckdb-imp`, comments included.** `arcbench:arc/scheduler.py:4322`: `self.species_list[:] = [spc for spc in self.species_list if spc.label != irc_label]` (in-place, not a rebind) with the same explanatory comment as `tckdb-imp`. `arcbench:arc/main.py:719`: `if species.label not in self.output: continue` guard, same comment. | Absent — no such guard on `origin/main:arc/scheduler.py` or `arc/main.py`. | **No action on the export branch now.** Crashes the final run summary after all compute is done; will arrive via `main`. |
| `b1ae2d71` | Guard the `_info.yml` species loop too | **Present, byte-identical to `tckdb-imp`, comments included.** `arcbench:arc/main.py:743`: `if not species.is_ts and species.label in self.output:`, identical to `tckdb-imp`. | Absent. | **No action on the export branch now.** Follow-up to `aa82723c`; travels with it. |

### Net effect on the ordering plan below

**All 7 general-ARC-fix items collapse to "no action now."** Of the original six numbered ports,
only **Port 1** (`scan_software`) and the level-attribution half of **Port 6** survive as work to do
against `feature_arc_result_export_contract` today; **Ports 2, 3, 4, 5, and the NEB-level half of
Port 6** are deferred to the `arcbench` → `main` → fast-forward path. The mechanical instructions for
each (target lines, conflict notes, hand-merge steps) in the "Ordered port plan" section below are
still accurate as *reference material for when that fast-forward happens* — they are not superseded,
just not urgent. They have been annotated inline.

---

## Table 2 — ALREADY PRESENT

| sha | subject | how export differs | which is better |
|---|---|---|---|
| `ca7ce141` | Expand output.yml calculation provenance (bulk) | Export's `50a3e77b`/`1f84d56b` rewrote the same ground into a tool-neutral shape: `atom_indices`+`index_base` instead of `atom1..4_index`; `result`/`samples`/`relaxed` instead of `scan_result`/`points`/`is_relaxed`; `_build_energy_corrections_for_species` (`arc/output.py:847`) instead of `_build_applied_corrections_for_species`. | **Export.** It adds `matched_arkane_key`, emits the BAC `parameter_table` only when `bac_key == aec_key`, guards nested Melius tables via `_flat_parameter_values` (`arc/output.py:823`), renames the AEC table to `reference_atom_energies` with `applied_as: subtracted` (sign/semantics correction — `tckdb-imp` invited consumers to reconstruct the correction with the wrong sign and ~10⁵ kJ/mol error), and computes `relaxed` (`_scan_is_relaxed`, `arc/output.py:1681`) instead of hard-coding `True`. |
| `ccb24732` | Pull Cartesian Hessian through to TCKDB uploads | `parse_cartesian_hessian_lower_triangle` is on both. Export *adds* `parse_cartesian_hessian_geometry` on the ESS interface (`arc/parser/adapter.py:233`, gaussian `:305`, orca `:361`) plus `_MAX_HESSIAN_DIMENSION` bounds-check (`arc/parser/adapters/orca.py:23`). | **Export.** `tckdb-imp` pairs the Hessian with the *standard*-orientation geometry, which silently reconstructs a wrong spectrum; export returns the Hessian's own frame and labels it. Export's tests include `test_standard_orientation_does_not_reproduce_frequencies`. |
| `1901cfcd` | Parse `S**2` + emit `spin_diagnostic` | `parse_s_squared` is byte-equivalent on both (gaussian `:354`, orca `:403`, qchem `:127`); `_parse_spin_diagnostic` at `tckdb-imp:arc/output.py:329` ≡ export `:371`; emitted as `d['sp_spin_diagnostic']` on both. | Tie. Only the `arc/tckdb/adapter.py` half differs, and that is out of scope. |
| `6aef917f` | GSM path-search points carry aligned arc-length `path_coordinate` | `arc/parser/parser.py` is **byte-identical** between branches. The derivation half moved into `arc/tckdb_evidence.py:567-575` on export, renamed `cumulative_com_superposed_displacement_angstrom`. | **Export.** The rename is deliberate: `path_coordinate` invited reading it as commensurable with the IRC record's mass-weighted `reaction_coordinate_sqrt_amu_bohr`. |
| `b2ba7413` | Add versioned TCKDB evidence sidecar | Export's `158f11f6` is a **direct hand-carried descendant**, not an independent implementation — same docstring, function names, control flow. `schema_version` 1.0 → 1.1; `arc-hessian-1` → `-2`; `arc-gsm-stringfile-1` → `-3`. Top-level document shape is byte-identical. | **Export.** Adds `freq_hessian.value.frame`, `gsm.value.ograd_invocations[]`, and geometry-matched energy attachment (`geometry_matched_ograd_invocation_id`, `geometry_match_displacement_angstrom`) replacing `tckdb-imp`'s broken id→frame arithmetic; plus `_fsync_directory`. |
| `5093deb9` | Harden TCKDB evidence normalization | **Every** hardening is present on export verbatim: no-trailing-`\n` canonical XYZ (`:120`), neutral comment lines (`:266`,`:287`,`:584`), per-source try/except isolation (`:252-296`), `_finite_json` per item (`:276`,`:292`), per-builder degradation (`:614-631`), `allow_nan=False` + `sort_keys=True` (`:693`). | **Export**, plus four bug fixes `tckdb-imp` lacks: final-cycle gradient slicing (`:329`), `_INVOCATION_ID_RE` validation (`:95`), `_invocation_sort_key` (`:359`), lowercase `d`→`E` in the gradient tokenizer (`:338`). Nothing to graft. |
| `e48b258c` | Preserve path-search provenance when a geometry-only TS guess wins dedup | Fully present on export — but under a **mislabelled commit subject**, `9cd92238 "species: expose spin diagnostics for result export"`, which contains no spin code. `_ts_guess_path_provenance` at export `arc/scheduler.py:89` (used `:1376`, `:2423`); `method_source_paths` plumbing at `arc/species/species.py:1688`, `:2460`, `:2570`, `:2608`; output fallback at `arc/output.py:1261-1268`. | Tie (identical bodies; export's docstrings are terser). |
| `f0cc1660` | Guard TSGuess clustering against coordinate-less guesses | Byte-identical, landed as `52eda5c5`. `arc/species/species.py:1733`, `:2675`. `git cherry-pick -n` produces only the 53-line test addition. | Tie — but the **tests are worth taking**. |
| `9be72a64` | Validate electron-count/multiplicity parity at species init | Landed as `df0584f1`, then evolved. | **Export, decisively.** It extracts `count_electrons` / `is_multiplicity_parity_valid` into `arc/common.py:546`/`:577` and routes `determine_multiplicity_from_xyz()`, `check_xyz()` and `perceive.infer_multiplicity()` through them, fixing three defects `tckdb-imp` still carries: `get_xyz(generate=True)` can trigger force-field conformer generation at `__init__` time; `check_xyz()` keeps a broken XOR; unknown symbols return `None` instead of raising. Export also has `bfff0d4e` (fixture repairs) and `6f40cc1d` → `reconcile_mol_multiplicity` (`arc/species/species.py:1499`), without which the guard rejects legitimate singlet carbenes. **Porting backwards would regress.** |
| `cea70fb5` | Normalizes TSGuess method sources | The `method_sources` feature predates the split and is on both (`_normalize_method_sources`, export `arc/species/species.py:2509`). The commit's actual content is a typing-style revert `list[str] \| None` → `Optional[List[str]]`. | **Export** — porting would move it away from the PEP-604 style used throughout the file. |
| `c1a579a4` | Fix xtb_gsm: make ograd wrapper executable | All three legs present on export: blob mode `100755`, `change_mode('+x')` at `arc/job/adapters/ts/xtb_gsm.py:264`, `make_x=True` at `:311`. | Tie. |
| `005083f7` | Fix molpro_test atomic-N fixture: multiplicity 3 → 4 | Arrived independently via `bfff0d4e`. Export `arc/job/adapters/molpro_test.py:95` has `multiplicity=4`. | Tie. |
| `6efc4e12` | Prefer standalone tckdb-arc package | Superseded by export's `938146b3`, which replaced the try/except in-tree fallback with `run_tckdb_upload()` in `ARC.py`. | **Export.** It requires an explicit `enabled: true` in the `tckdb` block before anything leaves the machine and logs the resolved destination first — `tckdb-imp` uploaded whenever a `tckdb` block existed. |

> Note: `6efc4e12` is listed here (superseded-by-better) rather than under EXCLUDE because its
> *intent* — delegate to the standalone package — is exactly the current architecture.

---

## Table 3 — EXCLUDE

Embedded-upload implementation, or superseded by the adapter re-home. All of these live wholly or
substantially under `arc/tckdb/`. **None may come back as a file port.**

| sha | subject | rationale |
|---|---|---|
| `c4fe12ad` | Add TCKDB upload package | The 18k-line `arc/tckdb/` package itself, plus its three audit docs and the `tckdb-client` / `tckdb-schemas` git dependencies in `environment.yml`/`requirements.txt`. Re-homed to `/home/calvin/code/tckdb-adapters`. |
| `ee0be100` | Wire ARC TCKDB upload sweep | Superseded by `938146b3`. **One carve-out** — see UNRELATED, `arc/imports.py`. |
| `3f58df6a` | Fix TS freq payload: represent the imaginary mode in modes | `arc/tckdb/adapter.py:4064-4085` only. It existed because `tckdb-imp`'s `output.yml` carried a *scalar* `imag_freq_cm1`, forcing the uploader to reconstruct the mode list. Export emits the full list (`arc/output.py:1136 imaginary_frequencies_cm1`), so the reconciliation is unnecessary. |
| `cbe69320` | salvage individually-converged species from partial reactions | `arc/tckdb/sweep.py`. Pure upload orchestration — decides what to POST. Its inputs are already exported (`arc/output.py:1009 'converged'`, `:1935 reactant_labels`/`product_labels`). |
| `3ac593a1` | map screened-conformer `origin_kind` to `'derived'` | Wire-level enum mapping against TCKDB's `CalculationWithResultsPayload`. Adapter-repo concern. Its inputs (`d['conformers']`, `d['conformer_energies']`) are exported at `arc/output.py:1043-1056`. |
| `9acbca2d` | retry the `/readyz` readiness probe | HTTP transport. Adapter-repo concern. |
| `4f8d6b26` | standalone transition-state upload adapter (computed_ts mode) | `arc/tckdb/{adapter,config,payload_writer,sweep}.py`. Adapter-repo concern. |
| `1c65289c` | capture per-node GSM energies from `.xtbout` | This *was* a parse-in-the-uploader smell (`arc/tckdb/adapter.py:5910-5983` globbing `gsm_node_outputs/*`). It is already re-homed **correctly**, into `arc/tckdb_evidence.py` on export (`_parse_xtbout:350`, `_node_outputs:365` → `gsm.value.ograd_invocations`), with the id-arithmetic bug fixed. Nothing left to port. |
| `102465f7` | emit `optical_isomers` on the bundle statmech block | `arc/tckdb/adapter.py:4855` copies a value `output.yml` already carried. Export emits it at `arc/output.py:1402`. |
| `1a2c8f27` | archive prior payload+sidecar on PayloadWriter re-run | `arc/tckdb/payload_writer.py`. Adapter-repo concern. |

### Idea-mining verdict (`arc/tckdb/adapter.py`, ideas only — no file port)

The brief asked whether any parse/derivation inside the embedded uploader belongs in ARC proper.
**Answer: yes, five of them did — and all five are already on the export branch**, in most cases
with a correctness fix:

| `tckdb-imp` adapter behaviour | belongs in ARC? | export status |
|---|---|---|
| `.xtbout` per-node energy parsing (`adapter.py:5910-5983`) | yes, evidence sidecar | present, geometry-matched instead of id-matched |
| cumulative Kabsch arc-length (`adapter.py:6134-6164`) | yes, evidence sidecar | present as `cumulative_com_superposed_displacement_angstrom` |
| synthetic imaginary-mode reinsertion (`adapter.py:4064`) | the *cause* did — the missing list | present as `imaginary_frequencies_cm1` |
| `<S**2>` payload build (`adapter.py:4113`) | the parse did | present as `sp_spin_diagnostic` |
| Hessian ↔ geometry binding (`adapter.py:1632`) | yes — frame determination is a parse | present with explicit `frame` |

Two derivations correctly stay adapter-side: the `is_climbing_image` peak flag
(`adapter.py:6266`) and `_resolve_irc_zero_energy_reference` (`adapter.py:6292`) — both are
re-derivable by any consumer from the absolute energies the sidecar now carries.

One optional idea worth recording: `_GSM_STRINGFILE_ENERGY_EPS = 1e-6` (`adapter.py:291`) encodes
"an all-zero comment column is molecularGSM's no-energy sentinel". The export sidecar deliberately
preserves the all-zero column (`test_gsm_all_zero_comment_energies_are_preserved`), which is right
for a neutral evidence file — but every consumer now re-derives the same span test. Consider adding a
**flag** (`stringfile_energies_are_sentinel: true`) rather than suppressing values. Low priority; the
`ograd_invocations` attachment already supplies real energies.

---

## Table 4 — UNRELATED

Incidental fixes with no TCKDB content. Worth backporting on their own merit, but as **ordinary
backports**, not as part of this uplift. Handle in a separate PR series.

| sha | subject | verdict | cherry-pick | value |
|---|---|---|---|---|
| `0b6c38d4` + `bc679fd8` + `42d54ab8` | Molpro memory card + node-capped trsh (3-commit sequence; take the **net** state) | ABSENT | **clean** — export's `arc/job/adapters/molpro.py` and `arc/job/trsh.py` are byte-identical to merge-base | **High.** Export `molpro.py:354` is still `math.ceil(job_memory_gb * 31.25)`; `tckdb-imp:molpro.py:360` is `max(1, ceil(job_memory_gb * 125.0))` with NODE-TOTAL semantics — a ~4× under-request. The trsh rewrite (`tckdb-imp:arc/job/trsh.py:1120-1170`) caps against `servers[server]['memory']` and halves MPI ranks at the cap instead of an unbounded `memory_gb * 3` loop. **Caveat: verify the 125 MW ≈ 1 GB claim against your cluster** — the three commits flip-flopped between per-process and node-total, so the number is empirical. |
| `a63a1fe4` | Harden remote job lifecycle (everything except the `scan_software` stamp) | ABSENT | `ssh.py`, `adapter.py`, `pipe_coordinator.py` are at merge-base → clean; new file `ssh_pool.py` clean | Split it. **Take now:** `ssh.load_system_host_keys(filename=self.key)` at export `arc/job/ssh.py:373` is outright API misuse — that parameter is the *known_hosts* file, not a private key (`tckdb-imp:arc/job/ssh.py:381` uses `load_system_host_keys()` + `key_filename=`); the `stdout and` guards in `submit_job` (export indexes `stdout[0]` unguarded → `IndexError` masks the real failure); `status_line.lstrip()` in the qstat parse; the `download_file` existence retry; `_filter_unavailable_ts_adapters`; the `pipe_coordinator.should_use_pipe` remote-server guard. **Defer:** `ssh_pool.py` (156 lines of process-global paramiko pooling + rework of every remote call path) deserves its own PR. Do not take `ssh.remove_dir` + `adapter.remove_remote_files` without the `scheduler.py:1201-1208` caller, or it is dead code. |
| `4ae2bf27` | Improve TS adapter troubleshooting | PARTIAL | clean for the absent parts | The `ograd` provenance and `xtb_gsm.py` `log_path` halves **are already present** (export `6f3966b6`; export went further with `gsm_node_outputs` in the archive lists). Absent and worth taking: the **orca_neb `abs_path` removal** — export `arc/job/adapters/ts/orca_neb.py:47`/`:50`/`:225` still embeds the *orchestrator's* local path in the ORCA input, which does not exist on the cluster node; and `NoOutput`-on-missing-file (`tckdb-imp:arc/job/trsh.py:82`), since export still raises bare `FileNotFoundError`. The `DispUnconverged` / `trsh_keyword_loose_disp` ladder (~150 lines) is empirically tuned to a single log — take it only with its tests and the `arc/testing/trsh/opt_disp_unconverged_a2354.log` fixture, or skip. |
| `c358e994` | Heuristics TS: H-abstraction seed for linear/cumulene acceptors | ABSENT | **clean** (file at merge-base; `SpeciesError` already imported at export `heuristics.py:35`) | Real correctness fix: without the `_perturb_collinear_zmat_angles` fallback (`tckdb-imp:arc/job/adapters/ts/heuristics.py:690`, used `:744-758`), H-abstraction from any cumulene/ketene-forming reaction yields **zero** heuristic TS guesses. Blast radius minimal — the fallback runs only after the direct build raises, and never touches returned TS coordinates. |
| `d76208f4` | Heuristics TS test teardown cleanup | ABSENT | clean | No standalone value; fold into `c358e994`. |
| `a11a96ee` | Map reactions in family discovery direction | ABSENT | **clean** (`arc/mapping/driver.py` at merge-base) | `tckdb-imp:arc/mapping/driver.py:73-75` short-circuits to `flip=True` when every product dict has `discovered_in_reverse`. The key *is* populated on export (`arc/family/family.py:725`). Saves a guaranteed-doomed forward mapping pass and its error log. Re-run `driver_test.py`/`engine_test.py` — export reworked mapping heavily in #906. |
| `ade4cc8c` | Mapping: quiet recoverable first-orientation template-order failure | ABSENT | clean | Export `arc/mapping/driver.py:287`/`:295` are `logger.error`; `tckdb-imp:290`/`:301` are `logger.debug`. Pure false-alarm noise on every reverse-discovered reaction that then maps fine. Control flow unchanged. **Take as a pair with `a11a96ee`.** |
| `81263a86` | Quiet benign linear-segment dihedral log noise | PARTIAL | `species.py` one-liner by hand; `engine.py` half **cannot** apply | Take **only** `arc/species/species.py:1462` `logger.warning` → `logger.debug` (message describes a documented no-op). The `arc/mapping/engine.py` half is superseded: `is_torsion_linear` does not exist on export, and `get_backbone_dihedral_angles` was rewritten to Rodrigues rotations on raw coord lists that never call `set_dihedral()`. |
| `f752d2fe` | processor: stop treating benign RMG log chatter as a thermo-script failure | ABSENT | **will conflict** — export rewrote the adjacent `execute_command` call to `rmg_env_command(py_args=...)` | ~10 self-contained lines (`tckdb-imp:arc/processor.py:282-288`) to re-apply by hand after export's `execute_command`. Removes a `logger.error` that fires on *every* reaction of a successful run. The `thermo_computed` gate is what makes it safe — it substitutes a deliverable check rather than just muting stderr. |
| `5954e5cb` | Enable AutoTST TS search for the Disproportionation family | ABSENT | `autotst_ts.py` clean; `common.py` **conflicts** (table rewritten by the GoFlow/RitS PR) | **Skip.** The commit's own message admits it works only against an AutoTST *fork* branch (`fix/disproportionation-support`). Registering it against upstream AutoTST routes Disproportionation to an adapter that rejects it. |
| `08bd07eb` | Register xtb_gsm + orca_neb for 5 linear-only NO_TS families | ABSENT / SUPERSEDED | **conflicts** (every line of `ts_adapters_by_rmg_family` changed) | **Low priority.** The stated motivation is addressed differently on export: GoFlow and RitS — double-ended/generative TS finders `tckdb-imp` does not have — are now registered on all five families. Adding xtb_gsm+orca_neb on top is defensible but additive configuration, not a fix. |
| `ee0be100` (carve-out) | `arc/imports.py` local-overlay guard | ABSENT | trivial | `_local_overlays_disabled()` (skip `~/.arc/{settings,submit,inputs}.py` when `'pytest' in sys.modules` or `ARC_IGNORE_LOCAL_SETTINGS=1`) exists only on `tckdb-imp`. Pure test-hygiene, zero TCKDB content — without it, a developer's personal `~/.arc/settings.py` silently changes test outcomes vs CI. Worth taking as its own tiny commit. |

---

## Ordered port plan

> **Status under the baseline correction: mostly deferred.** Per Table 1 above, only **Port 1** and
> the attribution half of **Port 6** (now split out as **Port 6a**) are live work today, against
> `feature_arc_result_export_contract`. **Ports 2, 3, 4, 5, and the NEB-level half of 6** (now
> **Port 6b**) are **already fixed on `arcbench`** and deferred to that branch's PR flow into
> `origin/main`, then a fast-forward — do not execute them from `tckdb-imp` now. The mechanical
> detail below (target lines, conflict notes, hand-merge steps) remains accurate reference material
> for whoever performs the fast-forward merge/rebase later; it has not been re-verified against
> `arcbench`'s tree (which has since diverged further in the surrounding files), only against
> `tckdb-imp`'s, as originally written.

Six commits. Ports 1–4 are the whole TCKDB-relevant + high-science-value set; 5–6 are small
fidelity fixes. Land them in this order; each is independently revertible.

> **Ordering note.** Ports are numbered by topic, not urgency. If you ship only one thing, ship
> **Port 2**; if only two, add **Port 3**. Port 1 is the cheapest and the only TCKDB-facing item,
> but it is also the one with no demonstrated behavioural effect today.

### Port 1 — `scan_software` scheduler stamp *(TCKDB-facing; ACT NOW — see Table 1a)*

- **Source:** `a63a1fe4`, 2 lines.
- **Target:** `feature_arc_result_export_contract:arc/scheduler.py:1219` (after
  `rotors_dict['scan_path'] = job.local_path_to_output_file`) and `:3184` (after the
  corresponding `rotors_dict[job.rotor_index]['scan_path'] = ...`).
- **Change:** add `rotors_dict['scan_software'] = job.job_adapter` at both sites.
- **Conflicts:** none — do **not** `git cherry-pick a63a1fe4` (it conflicts on `.gitignore`,
  `arc/job/adapter_test.py`, `arc/scheduler.py`, `arc/scheduler_test.py`). Apply the two lines by hand.
- **Tests:** `arc/output_test.py:2605-2694` already exercises every dispatch branch (they were
  carried over) but stub the rotor dict directly, so they pass today with the field unpopulated.
  Add a scheduler test asserting the stamp; `tckdb-imp:arc/output_test.py:2799` has the docstring
  that states the contract ("The scheduler stamps `scan_software` onto each rotor…").
- **Value:** makes the dispatch correct by construction and the docstring true. Prerequisite for the
  day ARC's ORCA adapter emits `%geom Constraints`, or a user supplies a deck that does. See Q1 for
  why this is not urgent.

### Port 2 — monoatomic participants (two commits, land together) *(DEFERRED — already fixed on `arcbench`, see Table 1b)*

- **2a:** `ARCSpecies._init_monoatomic_geometry()` from `ca7ce141`
  (`tckdb-imp:arc/species/species.py:552`, `:556`). Pure insertion; **applies cleanly by hand**
  (do not cherry-pick `ca7ce141` — it conflicts across 13 files).
- **2b:** `fe3161f9` in full. `git cherry-pick -n fe3161f9` reports **clean**, but the result
  **silently reverts an export-only improvement**: export's `check_rxn_e0_by_spc`
  (`arc/scheduler.py:2858-2862`) iterates `self.output.items()` filtered by `labels`, which avoids a
  `KeyError` on a label absent from `self.output`; `tckdb-imp`'s version indexes
  `self.output[spc_label]` for `spc_label in set(labels)`. **Hand-merge required:** keep export's
  iteration form, swap the predicate to `species_is_ready_for_e0(output_dict, self.species_dict[spc_label])`.
  Append `species_is_ready_for_e0` at `arc/scheduler.py:4346`. `is_monoatomic()` already exists on
  export (used at `:472`, `:1522`, `:3749`).
- **Value:** together these unblock TSG dispatch *and* the reaction E0 check for any reaction with
  an atomic participant — i.e. most H-abstractions. Highest functional impact in the plan.

### Port 3 — Arkane A+A thermo recovery *(DEFERRED — already fixed on `arcbench`, and better there; see Table 1b)*

- **Source:** `1484b545`. `git cherry-pick -n 1484b545` reports **clean**, but do not trust it
  blindly for `arc/statmech/arkane.py`.
- **3a (clean, do first):** `arc/scripts/save_arkane_thermo.py` — re-add `_iter_thermo_calls` and
  `_load_thermo_entries_from_output_py`, replace export's `return` at `:87-89` with the
  `thermo.py → output.py → return` ladder, restore `import re, sys`, add the
  `if thermo_data is None: continue` guard. Bring `arc/scripts/save_arkane_thermo_test.py` back verbatim.
- **3b (moderate conflict):** `arc/statmech/arkane.py` — `_dedup_thermo_species_list` and
  `_propagate_duplicate_species_thermo` are additive, but the **call sites collide** with
  export-only edits: `generate_arkane_input`'s species-list build was rewritten by `43c0adc4`
  (the SMILES/adjlist branch at export `arc/statmech/arkane.py:346-368`), and
  `parse_arkane_thermo_output`'s tail was reworked by the `rmg_env_command` change. Re-thread the
  dedup call around export's adjlist branch. **Keep both fixes** — `43c0adc4` prevents the
  singlet-carbene multiplicity crash, dedup prevents the duplicate-entry crash; they are
  complementary, not redundant.
- **Do not lose** export-only `_summarize_arkane_stderr` (`:649`), the `rmg_env_command` refactor,
  or `kinetics['tunneling'] = ARKANE_TUNNELING_METHOD` (`:1342`).
- **Tests:** restore `TestArkaneThermoDedupAndReload` alongside export's
  `test_lone_pair_species_uses_adjacency_list` and `TestSummarizeArkaneStderr`.
- **Optional (3c):** `tckdb-imp:arc/statmech/arkane.py:230` gates thermo parsing on
  `if not run_arkane(statmech_dir): return`; export calls `run_arkane` bare and parses regardless,
  even though its *kinetics* path does gate (`:258`). Asymmetry, not a design choice — fix it.

### Port 4 — post-run `KeyError` guard *(DEFERRED — already fixed on `arcbench`, see Table 1b)*

- **Source:** `aa82723c` + `b1ae2d71`, as one commit. Both cherry-pick **clean**
  (`b1ae2d71` conflicts only in `arc/main_test.py`; re-apply the test by hand).
- Key line: `arc/scheduler.py` must use `self.species_list[:] = [...]`, not `self.species_list = [...]` —
  the list object is shared by reference with `ARC.species`.

### Port 5 — `Level.as_dict()` args round-trip *(DEFERRED — already fixed on `arcbench`, see Table 1b)*

- **Source:** `822e8fc0`. `git cherry-pick -n` **clean** — the export line at `arc/level.py:190` is
  the pristine pre-fix line. Take the `arc/level_test.py` addition with it.

### Port 6 — TS-guess level fidelity + ORCA NEB level string *(SPLIT under the baseline correction)*

- **6a — TS-guess method attribution / `TSGuess.level` fidelity — ACT NOW (TCKDB-specific, Table 1a).**
  Source `530b9b8c`'s `process_completed_tsg_queue_jobs(path, method=...)` half, combined with
  `ff7a5762` **minus `crest.py`** (`plain_level_dict`, `TSGuess.level` plumbing). Both are confirmed
  present on `arcbench`, functionally identical to `tckdb-imp`, **except**: take `arcbench`'s
  `get_next_tsg_index()` (`arc/species/species.py:1820`, 17 lines, additive) instead of `tckdb-imp`'s
  `tsg.index = len(self.ts_guesses)` at the two call sites inside `process_completed_tsg_queue_jobs`
  — `tckdb-imp`'s form reintroduces an index collision after TS-guess clustering that `arcbench`
  fixed in a later, unrelated commit (`8dc0fae9`). `plain_level_dict` is a clean append to
  `arc/level.py`; the `TSGuess.__init__`/`as_dict`/`from_dict` hunks conflict lightly against export's
  `9cd92238`, and `xtb_gsm.py` was rewritten by export's `6f3966b6` — re-apply by hand as before.
  **Still explicitly exclude `arc/job/adapters/ts/crest.py`**: on `tckdb-imp` it is dead code (imports
  `arc.job.adapters.ts.seed_hub`, which doesn't exist there); on `arcbench` **[arcbench update]**
  `seed_hub.py` *does* exist (351 lines, `arc/job/adapters/ts/seed_hub.py`) and CREST is a live,
  registered adapter there — so the exclusion is no longer "this is unimportable dead code" but
  "the CREST adapter is a separate, out-of-scope feature," which still means: don't port it as part
  of this plan.
- **6b — ORCA NEB default level string — DEFERRED (general ARC fix, Table 1b).** The `settings.py`
  one-word fix (`wb97xd` → `wb97x-d3`) is confirmed byte-identical on `arcbench`
  (`arc/settings/settings.py:270`) and confirmed **still broken** on `origin/main`
  (`arc/settings/settings.py:289`, unfixed). `git cherry-pick -n 530b9b8c` reports clean at this site
  on `tckdb-imp` too. This is a hard functional break on export (every default ORCA NEB job errors)
  independent of TCKDB, so it is deferred to the `main` fast-forward path in principle — but it is
  cheap enough (one word) that taking it now as an exception is defensible if ORCA NEB is in active
  use against the export branch before `arcbench`'s PR lands. Flagged, not decided here.

### Not in the port plan, but scheduled separately

The 12 UNRELATED backports. Suggested order (all independent of the above):
Molpro memory → SSH/scheduler small fixes from `a63a1fe4` → cumulene heuristics (`c358e994`+`d76208f4`)
→ mapping pair (`a11a96ee`+`ade4cc8c`) → orca_neb `abs_path` + `NoOutput` from `4ae2bf27`
→ `f752d2fe` (manual) → `81263a86` species.py one-liner → `arc/imports.py` overlay guard.
Skip `5954e5cb` and `08bd07eb`.

---

## Answers to the five questions

### Q1 — Scan constraints and `index_base`

Four sub-questions were posed. Answers: **(1) yes, (2) no, (3) no, (4) yes.** The
fossil-docstring reading is **confirmed** — but it does **not** explain the user's working uploads,
and the honest conclusion is that the stamp is a cheap correctness fix, not the missing producer for
a live capability.

#### (1) Does `tckdb-imp` assign `scan_software` outside the ND branch? **Yes — twice, in the scheduler.**

| | `tckdb-imp` | `feature_arc_result_export_contract` |
|---|---|---|
| scheduler stamp — `end_job`, on scan-job completion (`job.job_type == 'scan' or job.directed_scan_type == 'ess'`, matched by pivots) | `arc/scheduler.py:1260` `rotors_dict['scan_software'] = job.job_adapter` | **absent** — `arc/scheduler.py:1219` sets `scan_path` only |
| scheduler stamp — `check_scan_job`, alongside the `scan_path` / `invalidation_reason` write after symmetry/invalidation analysis | `arc/scheduler.py:3194` `…rotors_dict[job.rotor_index]['scan_software'] = job.job_adapter` | **absent** — `arc/scheduler.py:3184` sets `scan_path` and `invalidation_reason` only |
| ND / directed-rotor dict default | `arc/species/species.py:1407` `'scan_software': ''` | `arc/species/species.py:1378` `'scan_software': ''` — **identical**, ND branch only |
| dispatcher (consumer) | `arc/output.py:1548` `_scan_constraint_parser_for` | `arc/output.py:1617` — **logically identical**, including the empty→Gaussian fallback |

Lifecycle point: both writes happen **after the scan job converges**, so the value is whatever
adapter actually produced the log. Introduced by **`a63a1fe4` "Harden remote job lifecycle"**
(2026-05-25 11:47:20) — a misleading subject; `git log -S"scan_software" -- arc/scheduler.py`
confirms it is the sole source on the branch.

#### (2) Does `tckdb-imp`'s `find_internal_rotors` include a `scan_software` key? **No — and neither does export's.**

`git show tckdb-imp:arc/species/conformers.py | grep scan_software` returns **nothing**; same on the
export branch. The 1D rotor dict is constructed without the key on **both** branches. The key comes
into existence only when the scheduler assigns it (Python dict assignment creates the key), and
`rotor.get('scan_software')` in `arc/output.py` tolerates its absence. So this is purely a
scheduler-write gap — there is no rotor-construction difference to port.

#### (3) Does `tckdb-imp` reach `parse_orca_constraints` with a non-empty result on any real artifact? **No — 0 of 198.**

Executed both parsers over every `*.log`/`*.out`/`*.txt`/`*.inp`/`*.gjf`/`*.com` under `arc/testing`
in throwaway worktrees of each branch, under `arc_env`:

| branch | files scanned | `parse_orca_constraints` non-empty | `parse_gaussian_constraints` non-empty |
|---|---|---|---|
| `tckdb-imp` | 198 | **0** | 2 |
| `feature_arc_result_export_contract` | 197 | **0** | 2 |

The two Gaussian hits are the same files on both branches —
`arc/testing/rotor_scans/CH2OOH.out` and `arc/testing/rotor_scans/scan_1d_curvilinear_error.out` —
with identical constraints, differing only in record vocabulary
(`{constraint_kind: 'bond', atoms: [2,3]}` on `tckdb-imp` vs
`{coordinate_type: 'distance', atom_indices: [2,3], index_base: 1}` on export). Both real ORCA
rotor-scan artifacts (`arc/testing/rotor_scans/orca/{cc,dft}.txt`) yield `[]` from **both** parsers on
**both** branches, because ARC's ORCA adapter emits `%geom Scan`, never `%geom Constraints`
(stated at `arc/parser/adapters/orca.py:658-663` on both branches).

#### (4) Is the `arc/output.py` docstring present on `tckdb-imp`, and true there? **Yes to both — the fossil reading is confirmed and dated.**

`git log -S"set by the scheduler when the scan"` gives:

- `tckdb-imp`: **`ca7ce141`, 2026-05-25 11:47:56** — i.e. **36 seconds after `a63a1fe4`** wrote the
  scheduler stamp. Same authoring session; the docstring was written to describe a mechanism the
  same author had just added, and it is **true** on `tckdb-imp`.
- `feature_arc_result_export_contract`: **`50a3e77b`, 2026-08-08** — 2½ months later, carrying the
  consumer and its docstring across without the producer. The docstring at
  `arc/output.py:1583` and `:1622` is therefore **false on the current branch**, exactly as
  hypothesised: *the target branch has the consumer without the producer.*

#### What this means — the honest calibration

The hypothesis is right about the mechanism and right about the provenance. It is **wrong about the
consequence being live**, and the port must be sized accordingly.

With `scan_software` never populated, `_scan_constraint_parser_for` takes the `not software` branch
and returns `parse_gaussian_constraints` for **every** scan. Effects:

- *Gaussian scans* — ARC's default — get the correct parser anyway, by accident of the fallback.
  **Identical output on both branches**, as the 2/2 match above demonstrates empirically.
- *ORCA scans* are routed to the Gaussian parser. This produces **no wrong data**: on a `.log`/`.out`
  with no `"The following ModRedundant input section has been read:"` marker, the deck-line heuristic
  is gated to `.gjf`/`.com` (`arc/parser/adapters/gaussian.py:1428-1432`) and returns `[]`. It would
  drop a `%geom Constraints … end` block that `parse_orca_constraints` would have caught — but ARC
  never emits one, so on the whole test corpus there is nothing to drop.
- *QChem / TeraChem scans* reach the Gaussian parser instead of the intended `None` → debug-log →
  `[]` path. Same empty result, wrong reason.

**So the user's working scan-constraint upload on `tckdb-imp` is explained by the Gaussian
ModRedundant path, which is byte-equivalent on both branches** — not by the `scan_software` stamp.
The rest of the chain is intact on the current branch too: `_parse_calc_constraints`
(`arc/output.py:475`, byte-identical to `tckdb-imp:arc/output.py:433`) dispatches on the
*calculation's* `software` field, which **is** populated on both branches, so opt/freq/sp constraints
are unaffected; and `/home/calvin/code/tckdb-adapters/tckdb_arc/tckdb_arc/constraints.py:193-202`
accepts both the legacy `constraint_kind`/`atoms` shape and the current
`coordinate_type`/`atom_indices`/`index_base` shape.

**Revised verdict: still PORT — two lines, zero conflict — but as a correctness-by-construction fix,
not as a bug fix.** It makes a false docstring true, stops ORCA logs being fed to the Gaussian
parser, and is a prerequisite for the day ARC's ORCA adapter starts emitting `%geom Constraints` (or
a user supplies a deck that does). It is **not** the highest-value item in this plan; Ports 2 and 3
are. If a regression is ever observed in ORCA scan-constraint export, this is the cause — but no
such regression is demonstrable on the current corpus.

#### `index_base`

**`tckdb-imp` does not have it, and should not get it.**
`git grep index_base tckdb-imp -- arc/` returns only an unrelated test-name hit in
`arc/mapping/engine_test.py`. `tckdb-imp` instead normalizes ORCA to 1-based **at the parser
boundary** (`tckdb-imp:arc/parser/adapters/orca.py:633`, `atoms = [a + 1 for a in zero_based]`) and
emits `{constraint_kind, atoms, target_value}`. The export branch emits raw native indices plus an
explicit `index_base` (`arc/parser/adapters/orca.py:747` → `0`;
`arc/parser/adapters/gaussian.py:1373`,`:1524` → `1`), de-TCKDB'd coordinate vocabulary
(`bond`→`distance`, `cartesian_atom`→`cartesian`), and a schema pin at
`arc/schemas/output_yml_schema.json:411`.

**The export form is better, and reverting it would break the adapter.** The +1 is merely relocated
downstream, and the downstream already implements it:
`/home/calvin/code/tckdb-adapters/tckdb_arc/tckdb_arc/constraints.py:195-202` and
`/home/calvin/code/tckdb-adapters/tckdb_arc/tckdb_arc/adapter.py:140-141` compute
`atom - index_base + 1` with validation that `index_base ∈ {0,1}`. Feeding them pre-normalized
1-based ORCA indices would **double-shift**. `output.yml` stays ESS-faithful, which matters for any
consumer that is not TCKDB. **Nothing to port here.**

Also verified: `tckdb-imp`'s `parse_orca_constraints` parses nothing extra. Multi-block
`re.finditer`, Cartesian `{ C i C }` freezes and brace-less hand-written variants are on **both**
branches, byte-for-byte; **neither** parses `%geom Scan` as constraints, and both say so in the
docstring.

**`index_base`: `tckdb-imp` does not have it, and should not get it.**
`git grep index_base tckdb-imp -- arc/` returns only an unrelated test-name hit in
`arc/mapping/engine_test.py`. `tckdb-imp` instead normalizes ORCA to 1-based **at the parser
boundary** (`tckdb-imp:arc/parser/adapters/orca.py:633`, `atoms = [a + 1 for a in zero_based]`) and
emits `{constraint_kind, atoms, target_value}`. The export branch emits raw native indices plus an
explicit `index_base` (`arc/parser/adapters/orca.py:747` → `0`;
`arc/parser/adapters/gaussian.py:1373`,`:1524` → `1`), de-TCKDB'd coordinate vocabulary
(`bond`→`distance`, `cartesian_atom`→`cartesian`), and a schema pin at
`arc/schemas/output_yml_schema.json:411`.

**The export form is better, and reverting it would break the adapter.** The +1 is merely relocated
downstream, and the downstream already implements it:
`/home/calvin/code/tckdb-adapters/tckdb_arc/tckdb_arc/constraints.py:195-202` and
`/home/calvin/code/tckdb-adapters/tckdb_arc/tckdb_arc/adapter.py:140-141` compute
`atom - index_base + 1` with validation that `index_base ∈ {0,1}`. Feeding them pre-normalized
1-based ORCA indices would **double-shift**. `output.yml` stays ESS-faithful, which matters for any
consumer that is not TCKDB. **Nothing to port here.**

Also verified: `tckdb-imp`'s `parse_orca_constraints` parses nothing extra. Multi-block
`re.finditer`, Cartesian `{ C i C }` freezes and brace-less hand-written variants are on **both**
branches, byte-for-byte; **neither** parses `%geom Scan` as constraints, and both say so in the
docstring.

### Q2 — `arc/output.py`: does `tckdb-imp` capture data the export contract does not?

**No — with one exception that is not about data capture (`_init_monoatomic_geometry`, in
`species.py`, already in Port 2).**

Function-set and emitted-key comparison:

- **Keys only on `tckdb-imp`:** `atom1_index`…`atom4_index`, `coordinate_index`, `coordinate_kind`,
  `coordinate_value(s)`, `scan_result`, `points`, `point_index`, `is_relaxed`, `step_count`,
  `value_unit`, `units`, `application_role`, `scheme`, `kind`, `name`, `version`,
  `source_literature`, `note`, `source_scan_calculation_key`, `type`.
  **Every one is a TCKDB-schema-shaped name, not a datum.** They map 1:1 onto export's neutral
  names (`atom_indices`+`index_base`, `source_index`, `coordinate_type`, `angle_degrees`, `result`,
  `samples`, `relaxed`, `sample_count`, `unit`, `source_scan_key`). Removing them was the point of
  the neutral contract.
- **Keys only on export:** `index_base`, `matched_arkane_key`, `applied_as`, `correction_type`,
  `model`, `total`, `values`, `samples`, `sample_count`, `source_log`, `source_index`,
  `angle_degrees`, `method_sources`, `chosen`, `relaxed`, `coordinate`, `coordinate_type`, `unit`.

Four concrete places where export captures **more or better**:

1. `_get_imaginary_freqs` (`arc/output.py:1307`) emits the full
   `imaginary_frequencies_cm1` list plus `freq_n_imag` (`:1131-1136`). `tckdb-imp`'s
   `_get_ts_imag_freq` (`:1280`) emits a single scalar with `n_imag` hard-coded to 0-or-1 — which is
   exactly why the embedded uploader needed `3f58df6a` to reconstruct the mode list.
2. `_scan_is_relaxed(rotor)` (`arc/output.py:1681`, emitted `:1890`) vs `tckdb-imp`'s hard-coded
   `'is_relaxed': True` (`:1458`, `:1820`).
3. `_build_energy_corrections_for_species` (`:847`) adds `matched_arkane_key`, gates the BAC
   `parameter_table` on `bac_key == aec_key`, guards nested Melius tables via `_flat_parameter_values`
   (`:823`), and renames the AEC table to `reference_atom_energies` with `applied_as: subtracted` and
   a comment explaining that the old naming invited a sign-flipped, ~10⁵ kJ/mol-wrong reconstruction.
4. `_evidence_status_counts` (`:231`) with `isinstance` guards, replacing `tckdb-imp`'s inline
   comprehension (`:184-193`) that raises `AttributeError` if `record[kind]` is ever not a dict.

Export also has `arc/schemas/output_yml_schema.json` and `arc/output_schema_test.py`, which
`tckdb-imp` has no analogue for.

**Verdict: `arc/output.py` is EXCLUDE / ALREADY PRESENT in full.** Do not port any of the four
`tckdb-imp` commits touching it (`ca7ce141`, `e48b258c`, `1901cfcd`, `b2ba7413`) at the `output.py` level.

### Q3 — `arc/species/species.py` (8 commits) and `arc/scheduler.py` (5)

**`species.py` — one real gap, and it is significant.**

- **`_init_monoatomic_geometry()`** (`tckdb-imp:arc/species/species.py:556`, called `:552`) — absent
  on export. Promotes/synthesizes a one-atom geometry into `final_xyz` at init, preferring
  `initial_xyz` → first conformer → `most_stable_conformer` → `cheap_conformer` before falling back
  to origin coordinates. ARC skips opt for atoms, so without it `final_xyz` stays `None` for the
  whole run, and `Reaction.check_done_opt_r_n_p` — which gates TS-search dispatch on every
  reactant/product having non-`None` `final_xyz` — silently blocks all TSG jobs for any reaction
  with an atomic reactant. **Port (Port 2a).**
- `TSGuess.level` + `plain_level_dict` (`ff7a5762`) — absent. **Port (Port 6b).**
- `process_completed_tsg_queue_jobs(path, method=…)` (`530b9b8c`) — absent; export hard-codes
  `TSGuess(method='orca_neb', …)` at `arc/species/species.py:1709`. **Port (Port 6a).**
- `logger.warning` → `logger.debug` on the linear-segment dihedral message
  (`tckdb-imp:1491` vs export `:1462`) — cosmetic. **UNRELATED.**
- Parity guard (`9be72a64`), TSGuess clustering guard (`f0cc1660`), `method_source_paths` plumbing
  (`e48b258c`), `method_sources` normalization (`cea70fb5`) — **all already present**, three of them
  in a better form (see Table 2).

**Explicitly checked and clean, per the brief:** there is **nothing** `tckdb-imp`-only about
**rotors**, **`scan_path`**, **`success` states**, **frequency handling**, or **`conformers`** in
`species.py`. Traffic runs the other way — export-only: `scan_software` in the ND rotor dict
(`:1378`), `skip_conformers` on `scissors()`/`_scissors()` (`:2051`, `:2100`), and the
`ThermoData.cp_data` → `thermo_points` rename (`:2718`). `tsg.success = False` handling is identical.

**`scheduler.py` — three gaps.**

- `scan_software` stamps at `:1260`/`:3194` (Q1). **Port 1.**
- `species_is_ready_for_e0` at `:4363`, used `:2866`/`:2996` (`fe3161f9`). **Port 2b** — hand-merge,
  because export's `check_rxn_e0_by_spc` iterates `self.output.items()` and a clean-reporting
  cherry-pick would silently revert that.
- `self.species_list[:] = […]` in-place removal at `:3919` (`aa82723c`). **Port 4.**
- `_filter_unavailable_ts_adapters`, the `JobError` catch, and `remove_remote_files` — absent, but
  **UNRELATED** (ship with the SSH backport; `remove_remote_files` is dead code without its caller).
- `_ts_guess_path_provenance`, `irc_directions`, and the `gsm`/`neb` paths-slot split are **already
  present** on export (`arc/scheduler.py:89`, `:1376`, `:2423`, `:3012`), which is why the user-visible
  path-search provenance already works there.
- Export-only in this file (do not revert): `post_freq_actions`, the `check_negative_freq` `None`/
  empty-list guards, `has_pending_pipe_work`, and the pipe-mode plumbing.

### Q4 — `arc/parser/parser.py` (3 commits)

**Nothing to port. `arc/parser/parser.py` is byte-identical between the two branches**
(`git diff --quiet tckdb-imp feature_arc_result_export_contract -- arc/parser/parser.py` succeeds).
All three commits (`ca7ce141`, `1901cfcd`, `6aef917f`) landed their parser content on the export
branch via `24fe03be` "parser: add Cartesian Hessian, structured IRC, and GSM parsing".

The per-ESS adapters run the same direction — the export branch is a **strict superset**:

| capability | `tckdb-imp` | export |
|---|---|---|
| `parse_s_squared` (gaussian/orca/qchem) | yes | yes, byte-equivalent |
| `parse_cartesian_hessian_lower_triangle` | yes, no ABC contract | yes, declared on `arc/parser/adapter.py:217` |
| `parse_cartesian_hessian_geometry` | **no** | yes — gaussian `:305` (input orientation, `frame='gaussian_input_orientation'`), orca `:361` (`$atoms` block, Bohr→Å, `frame='orca_hess_atoms'`) |
| Hessian dimension bound | **no** (unbounded `n_rows × n_rows` allocation from file content) | `_MAX_HESSIAN_DIMENSION = 30000`, `arc/parser/adapters/orca.py:23`, applied `:247-255` |
| `_read_irc_geometry_table` | **no** (inline loop; drops the `Point Number: 0` seed geometry, raises `ValueError` on a malformed row) | `arc/parser/adapters/gaussian.py:23` — emits the seed point, skips malformed rows |
| ORCA constraints (multi-block, Cartesian, brace-less) | yes | yes, identical |
| `%geom Scan` as constraints | no | no (both document it as out of scope) |

Test fixtures `arc/testing/freq/orca_hessian_h2o/{input.hess,output.out}` exist on **both**; export's
are extended (full H₂O `$atoms` block). `tckdb-imp` has one schema-shaped test; export replaces it
with six physics tests including `test_hessian_reproduces_reported_frequencies` and
`test_standard_orientation_does_not_reproduce_frequencies`.

Also checked: `arc/scripts/get_species_corrections.py` exists on both; export deletes the local
`_lot_from_string` regex helper in favour of the export-only `arc/scripts/arkane_levels.py` (AST-based
whitelisted reconstruction handling nested `CompositeLevelOfTheory`, returning `None` rather than a
wrong best-effort level). Export is ahead.

### Q5 — `docs/tckdb_upload_boundary.md`

**Yes — the reasoning is worth preserving, and it is currently only partially preserved.** The doc
(195 lines, added by `c4fe12ad`, amended by `3ac593a1`) is not implementation; it is the
**design rationale for where the ARC/TCKDB semantic line sits**, and that line is exactly what the
new architecture formalizes as `output.yml`.

Its durable content, and where it stands today in `/home/calvin/code/tckdb-adapters`:

| section | status in the adapter repo |
|---|---|
| **Core rule** — "TCKDB stores final scientific records and reproducibility-critical evidence; ARC stores ARC workflow history"; when in doubt omit | **not written down anywhere.** This is the single sentence the whole `output.yml`/`tckdb_evidence.json` split rests on. |
| **Upload / do-not-upload lists** (operational metadata, `successful_methods`, `chosen_ts_list`, `ts_report`, `atom_map`, `reaction_template`, `relative_e0_kj_mol`, RMG `longDesc`, `long_thermo_description`) | partially implicit in `docs/contract/ROUTE_ADJUDICATION.md` and `docs/contract/ADAPTER_MAPPING.md`, but as per-field verdicts, not as a stated policy |
| **Borderline-case rationales** — `kinetics.degeneracy` (keep: same A/n/Ea mean different rates at different degeneracies), `final_settings.optimization_stage`, IRC TS marker, `unmapped_smiles` (keep the identity, never the mapping that produced it) | these are the *arguments*, not the conclusions; they are what stops the next reviewer relitigating each field. `ADAPTER_MAPPING.md:306` carries one of them as an inline aside. |
| **`screened_conformer` → `origin_kind: derived` + `origin_detail`** with the HTTP-422 explanation | still directly true of the live backend; belongs in the adapter repo |
| **Guardrail tests** — full-payload walkers asserting *absence* of forbidden keys | `docs/tckdb_arc_rehoming_plan.md:286` already cites `tckdb_upload_boundary.md` as the source of the forbidden-key list, i.e. the plan **depends on a doc that does not exist in this repo** |
| **Future schema-change rule** — do not hide a value in `note`/`parameters_json`; decide ownership first; propose a typed field | fully general; nothing in this repo states it |
| references to `arc/tckdb/adapter.py`, `arc/tckdb/adapter_test.py`, `docs/tckdb-integration.md`, `docs/tckdb_arc_payload_coverage_audit.md` as the enforcement surface | **stale** — must be repointed at `tckdb_arc/` and `docs/contract/` |

**Recommendation.** Re-home it as `docs/contract/UPLOAD_BOUNDARY.md` in this repo, with two edits:
(1) repoint the enforcement-surface references from `arc/tckdb/*` to `tckdb_arc/*` and
`docs/contract/*`; (2) add a short preamble noting that under the new architecture the boundary is
enforced in **two** places, not one — ARC decides what enters `output.yml` (producer side), and the
adapter decides what enters a payload (consumer side) — so the "do not upload" list is now
primarily an *ARC export* rule, with the adapter's walkers as the backstop. Everything else
transfers unchanged. This is documentation work, **not** a code port, and it does not reintroduce
any `arc/tckdb/` code.

---

## Reverse survey — what `arcbench` has that is TCKDB-export-relevant and won't arrive via `main`

**Question asked:** not "what does `arcbench` have that the export branch lacks" (that's most of 284
commits of ordinary ARC development the user will receive for free once their PRs land on `main` and
the export branch fast-forwards) but the narrower, actionable one: **what on `arcbench` bears on the
ARC→TCKDB export path specifically, and would *not* naturally arrive that way** — because it's tied to
`output.yml`/the evidence sidecar, machinery `origin/main` doesn't have at all in this shape, so a
general-purpose PR has nowhere to land it. That is the only category requiring a deliberate decision
from the user; everything else is "wait." Investigated via `git log <merge-base>..arcbench --oneline
--no-merges` (238 non-merge commits) filtered for `output.yml`/evidence/provenance/TCKDB-flavored
subjects, then read individually. Four themes found; none of them were ported here — this is a survey,
not a further port table.

### A. Richer TS-guess provenance + a new `cost_metrics` section in `output.yml` — genuinely new, not on export

> **DECIDED 2026-08-12 — `cost_metrics` is out of scope for the export contract. Do not port it,
> and do not re-open this.**
>
> The depositor's ruling, verbatim in substance: cost metrics are *"a thing for an ARC user to look
> at — completely unrelated to TCKDB."* They describe how expensive a run was, which is a property
> of the computation's execution, not of the science it produced. TCKDB stores what a calculation
> **concluded**; wall-time and core-hours tell a reader nothing about whether a rate coefficient is
> right.
>
> The same ruling settles the `ts_guesses` fork below: the export contract keeps reporting **only
> the chosen guess**. Every guess ARC tried is run diagnostics for the person who ran it, not
> provenance for the record. Keeping the smaller shape also keeps the deposited record defensible —
> it says what the calculation determined, not what was attempted on the way there.
>
> `cost_metrics` and full `ts_guesses` remain valuable **on the ARC side** for benchmark analysis;
> nothing here argues against them living in `arcbench` or reaching `main`. They simply do not cross
> the boundary into `output.yml`'s TCKDB-facing contract.

Three commits (`383351c1` "Collect per-job cost records in the Scheduler and pass them to
output.yml", `7f9feecc` "Record pipe task costs into output.yml cost metrics", `873447d4` "Record
per-TS-guess provenance and run cost metrics in output.yml") add capability **neither `tckdb-imp` nor
`feature_arc_result_export_contract` has**:

- A run-level `cost_metrics` block: total job count, per-ESS `{job_count, summed execution time,
  core-hours}`, `wall_time_hrs`, and explicit `jobs_missing_time`/`jobs_missing_cores` counters so a
  benchmark analysis knows its data coverage rather than silently dropping incomplete records. Fed by
  a new `Scheduler.completed_job_records` list, persisted across restarts.
- A **full** `ts_guesses` list per TS species — every guess (method, `method_sources`,
  `method_index`/`direction`, success, relative energy, execution time, guess-generation level, guess
  log path, chosen flag) — not just the winning one.

**This is a real design fork, not a straightforward addition.** Export's current `ts_guesses` field
(`arc/output.py:1251`, `d['ts_guesses'] = [{...}]`) deliberately reports **only the chosen guess**,
with a narrow, explicit field set, and says why in a comment: *"Only expose stable attribution fields.
`TSGuess.as_dict()` also contains absolute paths, timestamps, geometries, and diagnostic state that do
not belong in this result-contract seam."* `arcbench`'s version takes the opposite position — report
the whole guess population, including failed/rejected guesses, for provenance and benchmarking. Both
are defensible; they are not compatible without a decision. **This needs the user's judgment, not a
port:** does TCKDB (or a benchmarking consumer downstream of `output.yml`) want full-population
TS-guess provenance and per-job cost accounting, or does the export contract deliberately stay
minimal? If the former, this is real design/schema work against the export branch, not a mechanical
port — it would need its own schema entries and its own decision about which of the "diagnostic
state" fields the narrow-contract comment was written to keep out are actually wanted.

### B. TS validation surfaced in `output.yml`, and a matching IRC-enforcement correctness fix in flight

`b9eae2d5` "Report the TS validation checks in output.yml" adds `d['ts_checks'] = dict(ts_checks)` for
TS species (`arc/output.py`) — the full `ts_checks` dict (`E0`, `energy`, `freq`, `IRC`, `warnings`,
...) is not exported today on `feature_arc_result_export_contract`. This is TCKDB-specific in the same
sense as Table 1a's items — its only purpose is enriching the export record — but it was not part of
the original 9 PORT items because it doesn't exist on `tckdb-imp` either; it's new `arcbench` work.

It gains real teeth from a companion correctness theme on `arcbench` that **is not TCKDB-specific**
and is still an open bug on `origin/main`: `c872458f` "Enforce the IRC check: reject a TS whose IRC
endpoints do not match the wells" (plus `5e9c5e58` "distinguish 'checked and failed' from 'could not
check'" and `639d00e7` "Label kinetics computed for an IRC-invalid TS instead of publishing them
silently"). The commit message documents a real published-wrong-rate incident (benchmark reaction
nitroethane ⇌ ethyl nitrite, family `intra_NO2_ONO_conversion`: IRC verdict `False`, rate off by ~9
orders of magnitude at 1000 K) caused by `ts_checks['IRC']` being computed but never read by any
enforcement path. **Confirmed still present on `origin/main`**: `git show origin/main:arc/checks/ts.py`
shows `ts_checks['IRC']` written but `origin/main`'s `check_ts()`/`ts_passed_checks()` do not gate on
it (matches the bug description in `c872458f`'s message).

**Practical consequence for TCKDB export:** once `b9eae2d5` lands (as a TCKDB-specific, act-now-style
change, since `main` will never have `output.yml` in this shape), `ts_checks.IRC` will be `False` for
some currently-published TS records — meaning a database record can now distinguish "this rate
constant's TS was IRC-validated" from "it wasn't, treat with caution," which the export contract
cannot express at all today. The IRC-enforcement fix itself (`c872458f` etc.) is general and will
arrive via `main`; the `ts_checks` surfacing (`b9eae2d5`) will not and needs its own deliberate
decision once the enforcement fix lands, so the two should be sequenced together rather than the
surfacing commit landing alone (an unenforced `ts_checks['IRC'] = False` in the export record would
be noise, not signal, until the enforcement fix makes `False` mean something acted-on).

### C. A new TS-search method (QST2) the export contract's per-method dispatch tables don't know about

`arcbench` has an entire new TS-guess adapter, `arc/job/adapters/ts/qst2.py` (Gaussian's Synchronous
Transit-Guided Quasi-Newton method), registered and exercised (`feature_qst2`/`qst2_settings` lineage;
sample commits `586f3ca6` "Register qst2 in the TS-guess paths-key map", `35d9bd36`/`3bb8192b`
"Classify failed QST2 queue jobs correctly", `84b13d55` "Align product fragments onto reactants for
NEB/QST2 endpoints"). **Confirmed absent from both `origin/main` and
`feature_arc_result_export_contract`** (`git show <branch>:arc/job/adapters/ts/qst2.py` fails on
both). This is ordinary new-adapter ARC work, not TCKDB-specific, and will arrive via `main` like any
other adapter. The reason it's worth flagging here rather than ignoring: `arc/output.py`'s
`_ts_guess_log_field_for_method`/`_TS_GUESS_METHOD_TO_LOG_FIELD` and `scheduler._ts_guess_paths_key`
are **per-method dispatch tables** that must know about every TS-search method by name to attribute a
guess's log correctly in `output.yml`. When QST2 lands via `main`, the export branch's copies of those
tables will need a `'qst2'` case added — a small, mechanical follow-up, not a design question, but one
that is easy to miss because nothing will error; a QST2-sourced guess would just silently fall through
to "geometry-only / unknown method" (`_ts_guess_log_field_for_method` returns `None`) instead of
getting its log attributed.

### D. `arcbench`'s own TCKDB integration is the *old* architecture, independently confirmed — nothing new here

**[arcbench update, confirms rather than changes Table 3]** `arcbench` carries `arc/tckdb/` (the
18k-line embedded-uploader package) and `arc/tckdb_evidence.py` at the **same commits** as `tckdb-imp`
— `git log <merge-base>..arcbench -- arc/tckdb/` and `-- arc/tckdb_evidence.py` return the identical
shas already covered in Tables 2/3 (`c4fe12ad`, `ccb24732`, `3f58df6a`, `e87e13d3`, `be39a2b5`,
`e289b9be`, `dbdac442`, `92b5b807`, `b8d31f48`, `c2a86856`, `c67a1ff3` "Add versioned TCKDB evidence
sidecar" — the older v1.0 sidecar Table 2 already describes as superseded by export's `158f11f6`, and
`5a179479` "Harden TCKDB evidence normalization", the `arcbench`-side twin of `tckdb-imp`'s
`5093deb9`). `arcbench` never received the export-contract rewrite (`50a3e77b`/`158f11f6`/`938146b3`/
`24fe03be` and siblings are absent — confirmed by the absence of `arc/schemas/` on `arcbench`, which
only the export branch has). So `arcbench`'s TCKDB layer is a **parallel continuation of the same
abandoned embedded-upload architecture `tckdb-imp` represents**, not a newer alternative to it. This
independently confirms — from a second branch, not just `tckdb-imp` — that Table 3's EXCLUDE verdict
and the "abandoned architecture" framing at the top of this document are correct, and that there is
nothing to mine from `arcbench`'s `arc/tckdb/` beyond what Table 3's idea-mining verdict already
extracted.

---

## What could not be established

- **No `arcbench` cherry-pick or hand-merge was trialled.** Every `arcbench` verdict above (Table 1,
  the reverse survey) is from `git show`/`diff`/`grep` reads and `git log -S`/`--ancestry-path`
  provenance tracing, exactly as the original `tckdb-imp` reads were, but **no throwaway worktree was
  cut from `arcbench`** (per the task constraint — `arcbench` is not to be touched, merged, or rebased
  from) and no `git cherry-pick -n` trial was run against it. Whether `arcbench`'s Port-1a items
  (`get_next_tsg_index`, `TSGuess.level` plumbing, `plain_level_dict`) apply cleanly to
  `feature_arc_result_export_contract`'s current tree, versus `tckdb-imp`'s, is unverified — only that
  their *content* is confirmed equal-or-better. Treat the "act now" items' target-line numbers as
  approximate until an actual patch is prepared.
- **The `arcbench`/`origin/main` ahead/behind counts (98 behind, 284 ahead; export 5 behind, 9 ahead)
  are a snapshot** taken via `git rev-list --left-right --count origin/main...<branch>` after
  `git fetch origin main`. Both branches continue to move; re-run before acting on any "wait for
  main" item to confirm the fix hasn't already landed.
- ~~**Whether `arcbench`'s full-`ts_guesses`-population / `cost_metrics` design (reverse-survey theme
  A) is what the user actually wants for the export contract**~~ — **RESOLVED 2026-08-12: no.** Both
  stay out of the export contract; the "chosen guess only" shape stands. See the decision box in
  reverse-survey theme A. The reasoning is that cost metrics describe how expensive a run was, which
  is a property of the execution rather than of the science, and TCKDB records what a calculation
  concluded. Left below for the record of why it was a genuine fork rather than an oversight.
  Resolving it needed the user's judgment about what a TCKDB consumer
  should see, not further code archaeology.
- **The Molpro `125 MW ≈ 1 GB` node-total factor is empirical, not verified here.** The three-commit
  sequence (`bc679fd8` → `42d54ab8` → `0b6c38d4`) flip-flops between per-process and node-total
  semantics, and the final commit message asserts NODE-TOTAL without a citation. The 4× change
  relative to export's `31.25` should be checked against an actual Molpro run on the target cluster
  before merging. No Molpro run was performed.
- **No test suite was executed.** Apart from the constraint-parser probe described in Q1, every
  verdict is from source reading plus `git cherry-pick -n` trials. The `arc_env` suite was not run
  against any proposed port, and cherry-pick cleanliness is a statement about text, not behaviour —
  Port 2b is the concrete case where a *clean* cherry-pick silently reverts an improvement.
- **No ORCA artifact carrying `%geom Constraints` exists in the test corpus**, so the one scenario
  where the `scan_software` stamp changes output could not be exercised end-to-end — only reasoned
  from source. The 198-file probe establishes that no such artifact is present, not that none can
  exist. If the user has a project directory with a hand-written ORCA scan deck carrying a
  `Constraints` block, running `_parse_scan_constraints` against it on both branches would settle
  the last of it.
- **Why the user's `tckdb-imp` uploads worked is established by elimination, not by tracing their
  actual run.** The Gaussian ModRedundant path is byte-equivalent on both branches and is the only
  path that produces non-empty constraints on any available artifact; `_parse_calc_constraints` is
  byte-identical and dispatches on a field populated on both. No `output.yml` from the user's run was
  inspected.
- **`arc/job/adapters/ts/seed_hub.py` was not located** *(on `tckdb-imp` — since resolved)*.
  `crest.py` on `tckdb-imp` imports it and it exists nowhere on that branch, so the CREST adapter is
  unimportable there. **[arcbench update]** It has since been located: `seed_hub.py` (351 lines)
  exists on `arcbench` at `arc/job/adapters/ts/seed_hub.py`, and CREST is a live, registered adapter
  there (`register_job_adapter('crest', CrestAdapter)` executes; confirmed via `git ls-tree` and
  `git grep`). This resolves the original open question — the file was never lost, it simply postdates
  `tckdb-imp`'s branch point. It remains a separate, out-of-scope feature-port question for this plan
  either way: nothing in the 9 PORT items or the reverse survey depends on it.
- **The two merge commits** (`cbff7fff`, `f31bb299`) were treated as content-free on the basis that
  `git rev-list --count` reports exactly 43 non-merge + 2 merge = 45, i.e. they introduced no
  commits absent from the export branch's ancestry. Their conflict resolutions were not inspected
  hunk-by-hunk.
- **`arc/job/adapter.py`'s `_open_or_borrow_ssh` / `_dispatch_execution` refactor** was assessed for
  presence and cherry-pick feasibility, not for correctness. It carries `except Exception:` fallbacks
  around connection reuse; that is a review question for its own PR, not something this audit settles.
