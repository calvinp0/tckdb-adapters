# Phase C — the uplift plan

Turns `GAP_MATRIX.md` into two ranked, executable backlogs. Ranking follows
`PRIORITY_POLICY.md`: **silent-corruption risk above every tier**, then value tier,
then ascending cost. Cost comes from A3's `export_effort`, from A5's
`breaking_for_producer`, or from code read for this document — never invented.

Citations are `file:line`. Repo prefixes: no prefix = `tckdb-adapters`;
`ARC:` = `ARC.worktrees/feature_arc_result_export_contract`; `TCKDB:` = `TCKDB_v2`.
Line numbers were re-derived against the current working trees and differ from the
Phase A inventories where the code has moved since.

---

## 0. Read this first — five corrections to the matrix

The matrix is sound as a join but four of its verdict *populations* do not mean what
the verdict name says, and one finding carried into this phase turns out to be wrong.
Acting on the raw counts would send the backlog to the wrong places, so the counts
below are the ones the rest of this document uses.

### 0.0 Transition-state rotor scans are **not** an ARC gap — they are an adapter gap

`ROUTE_ADJUDICATION.md` §2.4 reported 48 rows of TS `scan_result` as a gap "no
inventory surfaced", held at medium confidence because its author could not establish
"whether ARC *can* run a rotor scan on a TS and simply does not export it". **Settled,
and the answer is neither of the offered options: ARC runs them and already exports
them.**

* `ARC:arc/scheduler.py:1544` `run_scan_jobs`, docstring at `:1546` — "Spawn rotor
  scan jobs using 'final_xyz' for species (**or TS**)". There is no `is_ts` gate
  anywhere in `:1544-1636`.
* TS-specific rotor machinery presupposes it:
  `invalidate_rotors_with_both_pivots_in_a_reactive_zone`
  (`ARC:arc/checks/ts.py:334`) calls `ts_species.determine_rotors()` (`:349`) and
  invalidates **only** rotors with *both* pivots in the reaction zone (`:352-356`,
  marker `pivTS`) — the rest stay eligible.
* `ARC:arc/scheduler.py:2900-2904` resets `rotors_dict` on a TS-guess switch "so they
  are re-determined from the new TS geometry" — meaningless if TS rotors were never
  scanned.
* The one `is_ts` × `scan` interaction (`ARC:arc/scheduler.py:3331`) is in
  `check_all_done` and *exempts* scans from blocking TS convergence; it does not
  prevent them being spawned.
* **And the export carries them:** `ARC:arc/output.py:1302` `d['rotor_scans'] =
  rotor_scans` sits in the shared `_spc_to_dict` (def `:987`) *after* the `if
  spc.is_ts:` block closes, populated at `:1290`. TS records are routed from that same
  dict at `ARC:arc/output.py:163-171`. The schema agrees: `$defs.ts_record` is
  `allOf: [common_species_fields]` (`ARC:arc/schemas/output_yml_schema.json:1621-1626`)
  and `common_species_fields` lists `rotor_scans` in **`required`** (`:1290`).

So `_build_ts_block` not constructing a scan calculation is an **adapter** omission
against data already on the wire. The item moves from ARC-5 (effort unknown, possibly
a scheduler feature) to **ADAPT-2 (low, tier 1)**. This is the single largest change
between the Phase B picture and this plan.

### 0.1 `ADAPTER_GAP` is 87 rows. Twelve are adapter gaps.

`ADAPTER_GAP` means "ARC exports it, the adapter drops it". Resolving each row
through its `SUPPLIED_AT:` sibling and reading the sibling's `exported.arc_key`:

| | rows |
|---|---:|
| real adapter gaps (`arc_key` is non-null — ARC genuinely exports it) | **12** |
| A2 negative rows (`arc_key: null`, `availability: rare`) — ARC does **not** export it | **75** |

The 75 are ARC-side facts wearing an adapter-side label. They cluster:
31 `geometry.isotopes`, 12 `level_of_theory.spin_treatment`, 6
`calculation.parameters[].raw_key`, 6 `path_search_result.points[].max_force`,
3 `wavefunction_diagnostic.t1_diagnostic`, and singletons.
`ROUTE_ADJUDICATION.md` §3 called this out for `spin_treatment` alone ("the
sibling's own matrix verdict of `ADAPTER_GAP` … is simply wrong for a null-supply
row"); it generalises to the whole population. **No adapter ticket should be opened
from an `ADAPTER_GAP` row without checking `exported.arc_key` first.**

The 12 real ones are listed in ADAPT-4 below.

### 0.2 `SOURCE_UNCONFIRMED` is 397 rows and contains **zero** gaps

Bucketing all 397 by the adapter's own `maps_from`:

| bucket | rows | what it means |
|---|---:|---|
| `output.yml: …` | **291** | The adapter reads the export contract directly. A2 has no row because A2 inventoried 271 leaves against the adapter's 775 — a **coverage** gap in A2, not a supply gap. |
| adapter-asserted literal / synthesized / derived | **75** | Producer-invented values. Not a gap — an *audit* population (ADAPT-3). |
| on-disk reparse (artifact bytes/sha256/size) | **18** | Real, and evidence-first. |
| `tckdb_evidence.json` sidecar (Hessian) | **8** | Real, and evidence-first. |
| other derived/computed | **5** | |

The framing that this population is dominated by `arc.parser` reparse is wrong:
reparse and sidecar together are 26 rows. The dominant cause (291/397) is that A2
keyed each calculation sub-block **once**, at one representative route, and keyed
level-of-theory leaves only at
`species_upload.conformers[].primary_calculation.level_of_theory.*`
(`ARC_SUPPLY_EXPORTED.yml:205-293`). Every other calculation route therefore has an
adapter row and no supply row. `ROUTE_ADJUDICATION.md` §1.1 already adjudicated that
family `GENERALISES` across all six routes; the join simply could not carry the
adjudication through a sibling that was itself `SOURCE_UNCONFIRMED`.

**Triage outcome: 317 of 397 are `WIRED` in substance. 75 are the fabrication-audit
list. 5 need a second look. None is an uplift target.**

### 0.3 The sibling resolver mislabels 92 adjudicated-`GENERALISES` rows as `ARC_ABSENT`

`ROUTE_ADJUDICATION.md` §2.5 warned that `resolve_route` "will happily pick a
`SURPLUS` path as a sibling, which means a mis-keyed supply row can silently suppress
a real gap." The inverse also happens: 92 rows carry `ADJUDICATED:GENERALISES` and
still read `ARC_ABSENT`, because their resolved sibling is itself `SOURCE_UNCONFIRMED`
(61 rows) or an `ORPHAN_MAPPING` under the invented `transition_state_upload.*` root
(31 rows). Example:
`species_upload.conformers[].additional_calculations[].irc_result.points[].point_index`
resolves to `transition_state_upload.…point_index` (orphan) rather than to
`reaction_upload.transition_state.calculations[].irc_result.points[].point_index`,
which is `WIRED`. These 92 are not gaps.

### 0.4 336 of the 2085 "demand fields" are container nodes, not fields

A1 emits a row per nested model as well as per leaf (`FIELD_KEY.md` rule 3 governs
leaves; the walk also records the parents). **336** rows are a strict path-prefix of
another row (the heading and this count now agree; an earlier draft's body text said
342, self-contradicting its own heading — 336 is the mechanically verified figure, per
`tools/phase_c_stats.py`) — `species_upload.conformers[].primary_calculation`,
`…freq_result`, `…hessian`, and the two roots themselves. 306 of them sit in
`ARC_ABSENT`, because nothing supplies a container. **Leaf demand fields: 1749.**

### 0.5 ARC-0's headline sub-item was mis-ranked: real defect, zero corrupted rows

An earlier draft ranked `_scan_constraint_parser_for`'s Gaussian default (below, "ARC-0
sub-item 1") above every value tier as a silent-corruption risk. **The harm does not
occur.** `parse_gaussian_constraints` (`ARC:arc/parser/adapters/gaussian.py:1358-1393`)
only emits a record when it finds a Gaussian ModRedundant `F`-coordinate line
(`_parse_gaussian_constraint_line`, `:1449-1461`) — syntax that exists only in a
Gaussian input deck or the `"The following ModRedundant input section has been read:"`
log block. ORCA logs never contain either. `parse_orca_constraints` itself documents
that ARC's ORCA adapter "does not currently emit `%geom Constraints` blocks (only
`%geom Scan`)" (`ARC:arc/parser/adapters/orca.py:660-663`).

Verified by execution, not just by reading: both parsers were run over every real ORCA
artifact under `ARC:arc/testing` (21 files — freq logs, the opt log, all ten `trsh/orca`
troubleshooting logs, and the two real ORCA rotor-scan logs `arc/testing/rotor_scans/orca/cc.txt`
and `dft.txt`, 8.6MB and 5.2MB of real log text). **Every file, both parsers: `[]`.**
`_parse_scan_constraints` (`ARC:arc/output.py:1677-1712`) therefore always returns `[]`
for ORCA rotor scans regardless of which parser `_scan_constraint_parser_for` picks, and
`if scan_constraints:` (`:1671-1672`) means `entry['constraints']` is never set at all in
that case — no `index_base` is emitted, wrong or otherwise. Zero corrupted rows exist.

It is also dead code on this branch: `scan_software` is written exactly once anywhere in
`arc/` — `arc/species/species.py:1378`, `'scan_software': ''`, inside the ND
(multi-dimensional directed) rotor constructor only — and `find_internal_rotors`
(`ARC:arc/species/conformers.py:1675-1728`, the ordinary 1D rotor path) never sets it.
`scheduler.py` never assigns it either (`grep -n scan_software arc/scheduler.py` is
empty). So on the ordinary 1D rotor-scan path the key is **always** missing, the
docstring's own claim that it is "set by the scheduler when the scan job completes"
(`ARC:arc/output.py:1681`) is false on this branch, and the Gaussian-fallback branch of
`_scan_constraint_parser_for` fires on every ORCA rotor scan ARC has ever produced —
and still emits nothing, because the underlying parser has nothing to parse.

The rank was also mis-justified against `PRIORITY_POLICY.md`'s own wording: the
above-every-tier override is scoped to "anything **A2 or A5 flags** as a silent-corruption
risk", not to a finding made during Phase C itself, and the earlier draft's own text
for this item said the corruption carried "no inventory recorded" — i.e. it was not an
A2/A5 flag to begin with.

**Consequence:** this sub-item demotes from ARC-0's correctness tier to an ordinary
documentation/robustness item (folded into C-10 and ARC-3's housekeeping, §2 and §3
below) — require `scan_software` or state plainly that it is unset, and fix the stale
docstring. ARC-0's other two sub-changes (torsion `index_base`, the two-IRC-path
reconciliation) are unaffected and re-verified on their own evidence below.

---

## 1. Executive picture

### 1.1 What ARC satisfies today

**Every number in this section is mechanically derived from `gap_matrix.yml` by
`tools/phase_c_stats.py`.** Run `python tools/phase_c_stats.py` from the repo root to
reproduce it; the script's output is reproduced in full at the end of this section. This
replaces an earlier version of this section whose numerators could not be reproduced from
the matrix and were inflated by a combined 230 leaves against a mechanical recount.

Two definitions drive every number below, stated explicitly because both are judgement
calls a reader could make differently:

* **Leaf.** Of the 2085 rows carrying a `demand` block (i.e. excluding `SURPLUS` and
  `ORPHAN_MAPPING`, which the matrix's join already resolved as not-demand), a row is a
  *leaf* when no other demand row's path is a strict prefix of it. This reproduces §0.4's
  336 containers / 1749 leaves exactly.
* **Populated.** `verdict ∈ {WIRED, SOURCE_UNCONFIRMED}`. Per §0.2, `SOURCE_UNCONFIRMED`
  is 100% real supply once bucketed by the adapter's own `maps_from` — none of its 397
  rows (377 of them leaves) is a gap. **This choice drives the headline percentage:** a
  reader who does not accept §0.2's triage should instead read "populated" as `WIRED`
  alone — 390/1749 = 22.3% — and treat the rest of this table as an upper bound.

Over the **1749 leaf demand fields**:

| | leaves | share |
|---|---:|---:|
| populated from ARC (`WIRED`, or `SOURCE_UNCONFIRMED` read directly from `output.yml`, on-disk reparse, or the `tckdb_evidence.json` sidecar) | 687 | 39.3% |
| populated by adapter-asserted constants (`SOURCE_UNCONFIRMED`, literal/synthesized — §0.2's audit population) | 75 | 4.3% |
| populated, other derived (`SOURCE_UNCONFIRMED`, unclassified) | 5 | 0.3% |
| **populated, total** | **767** | **43.9%** |
| ARC-absent | 863 | 49.3% |
| ARC-latent (computed, not exported) | 29 | 1.7% |
| adapter gap (ARC exports, adapter drops) — raw leaf verdict count; §0.1's semantic split into 12 real gaps / 75 A2-negative rows is computed over all 87 `ADAPTER_GAP` rows (leaf **and** container), a different population from this one | 82 | 4.7% |
| broken against 0.22.0 | 8 | 0.5% |

By tier (leaves, same populated definition): tier 1 (kinetics lookup) **40.4%** populated
(333/825); tier 2 (reproducibility) **30.8%** (98/318); tier 3 (atom mapping) **59.5%**
(150/252 — high because IRC and path-search *are* wired on the TS route, and the atom map
itself is only 9 fields); tier 4 **13.1%** (11/84).

The number to hold onto is still **tier 2 — now 31%, not 41%**. Reproducibility is the
tier TCKDB spent the whole 0.8.0→0.22.0 range building out (`SCHEMA_DRIFT.md` §1,
movement 1), and it is the tier ARC feeds worst — worse than the uncorrected figure said.

<details>
<summary><code>python tools/phase_c_stats.py</code> output (abridged to the tables used above)</summary>

```
§1.1 -- leaf verdict distribution
verdict                   leaves     share
WIRED                        390     22.3%
SOURCE_UNCONFIRMED           377     21.6%
ARC_ABSENT                   863     49.3%
ARC_LATENT                    29      1.7%
ADAPTER_GAP                   82      4.7%
BROKEN                         8      0.5%
POPULATED (WIRED + SOURCE_UNCONFIRMED)     767     43.9%

SOURCE_UNCONFIRMED leaf sub-buckets:
   271  output.yml (direct export contract read)
    75  adapter-asserted literal/synthesized
    18  on-disk reparse
     8  tckdb_evidence.json sidecar
     5  other derived/computed

§1.1 -- by tier
tier 1 (kinetics lookup)      333 / 825   40.4%
tier 2 (reproducibility)       98 / 318   30.8%
tier 3 (atom mapping)         150 / 252   59.5%
tier 4 (completeness)          11 / 84    13.1%
```
</summary>
</details>

### 1.2 The realistic ceiling

Of the absent leaves, these are outside ARC's capability as a matter of what ARC
is, not of what it has been asked to export (leaf counts re-derived by path-fragment
match against `gap_matrix.yml`, restricted to `ARC_ABSENT`, via the same script):

| block | leaves | why |
|---|---:|---|
| `literature.*` at every route | 64 | ARC computes; it does not cite. A depositor or curator supplies these. Recorded as structurally absent by A2 ("Structurally absent by design", `ARC_SUPPLY_EXPORTED.md` §"Export paths with no fixture coverage"). |
| `applied_energy_corrections[].frequency_scale_factor.*` | 120 | ARC attaches no scale factor to a correction record, and `ARC:arc/schemas/output_yml_schema.json`'s `$defs/energy_correction` is `additionalProperties: false`, so a producer cannot add one without a schema bump. Adjudicated `DIFFERENT_INSTANCE` with three independent confirmations (`ROUTE_ADJUDICATION.md` §2.1). |
| release metadata detail (`build`, `revision`, `release_date`, `notes` on software/workflow-tool releases) | ~54 | ARC holds a git commit and a banner-parsed version string, not a release record. Not re-verified in this pass — carried forward as an approximation, unlike the two rows above. |
| `freq_result.modes[]` detail — IR intensity, Raman activity, force constant, symmetry label, reduced mass | 12 | "No parser reads them from any ESS adapter" (`ARC_SUPPLY_LATENT.md`, deliberate exclusions). |
| `D1` diagnostic, rotational constants | ~6 | Same: no parser, and `grep` for `inertia\|rotational_constant` across `ARC:arc/` returns nothing outside tests. Also not re-verified in this pass. |
| **subtotal genuinely out of reach** | **~256** | **14.6% of the leaf surface** |

A further **34 leaves** are the vacuous non-TS `irc_result` / `path_search_result` routes
on species conformers (§0.3 and `ROUTE_ADJUDICATION.md` §1.5 — 136 rows there, 34 leaves ×
four species-calculation routes; `scan_result` is not part of that ruling and is not
included here) — demand TCKDB exposes only because its calculation model is uniform, not
demand anyone intends to fill.

Everything else is reachable. **The realistic ceiling is ~83% of the leaf surface
(~1459/1749 = 1749 − 256 out-of-reach − 34 vacuous), and the single largest step toward
it is one piece of ARC plumbing — the JobAdapter→record handoff, worth 216 leaves on its
own (ARC-7, §1.3 and §3) — up from an earlier, unreproducible "198 leaves" figure.**

*(An earlier version of this section carried a footnote deriving a "~670 absent / ~1005
populated" adjustment from §0.0's TS-rotor-scan reclassification. That adjustment does not
survive a mechanical, verdict-based "populated" definition — reclassifying a row from
`ARC_ABSENT`/`ARC_LATENT` to `ADAPTER_GAP`, which is what §0.0 does, does not make it
populated under the definition above. Dropped rather than re-derived; §0.0 and ADAPT-2
already say everything load-bearing about that reclassification.)*

### 1.3 The four things that move the needle most

1. **Fix the index-base hazards before depositing anything else** (§2). They are
   cheap and they outrank every tier by policy. The live one is constraints: ARC's
   Gaussian parser emits `index_base: 1` and its ORCA parser emits `index_base: 0`
   (both confirmed) on `opt_constraints[]` / `freq_constraints[]` / `sp_constraints[]`,
   and the adapter's rebase is skipped entirely on the legacy dict shape
   (`constraints.py:193`) — an ORCA-sourced legacy constraint on that path is silently
   accepted pointing at the wrong atoms. (A related-looking hazard on the *rotor-scan*
   constraint route — ARC defaulting an unknown `scan_software` to the Gaussian parser,
   `ARC:arc/output.py:1725-1726` — turns out not to corrupt anything: see §0.5. It is
   real dead code and a stale docstring, not a live index-base risk.) All of the live
   defect passes validation today.
2. **Wire the transition-state rotor scans the adapter already has** (ADAPT-2, §0.0).
   Hindered internal rotation in a saddle point is first-order in the rate constant,
   ARC exports it under `transition_states[].rotor_scans`, and the adapter never looks.
   Low cost, tier 1, and nothing anywhere reports it as missing.
3. **The JobAdapter→record handoff in ARC** (ARC-7). 216 leaves — 144 of them tier 2
   (210 of the 216 are `ARC_ABSENT`, the other 6 are `ADAPTER_GAP` `raw_key` rows already
   counted elsewhere; within the `ARC_ABSENT` 210, 140 are tier 2) — behind one plumbing
   change that `ARC:arc/output.py` already names as the missing piece. It is the only item
   in either backlog with three-figure leverage. (An earlier, unreproducible "198 leaves —
   124 tier 2" figure understated it; the leverage was understated, not overstated.)
4. **Atom mapping, and its deposit-order consequence** (ARC-5 → ARC-6 → ADAPT-7). §6
   settles the open question: a reaction deposited without a map **cannot be
   enriched**, and re-depositing creates a *second* `reaction_entry` with nothing
   linking it to the first. Tier-3 placement is fine; **bulk reaction deposition
   before the map lands is not**.

Close behind: **the unversioned-endpoint exposure** (ADAPT-1). 188 of the adapter's
775 mappings (24.3%) target request contracts that live in TCKDB's
`backend/app/schemas/workflows/` and are absent from the published `tckdb-schemas`
package — and the endpoint behind 58 of them is the adapter's **default** upload mode
(`config.py:169`). It is a decision more than a task, which is why it is not in the
top four, but it should be decided before the re-pin.

---

## 2. Correctness items — these outrank every value tier

`PRIORITY_POLICY.md`: "anything A2 or A5 flags as a **silent-corruption risk** … is
ranked above every tier. These pass validation and write wrong science into the
database." Each item below was re-verified against source for this document rather
than relayed.

**Citation baseline (re-derived after the earlier version of this section drifted —
line numbers below had gone stale against every tree they might have been read
against).** Unless an item says otherwise, every `adapter.py:NNNN` /
`tests/test_adapter.py:NNNN` / `constraints.py:NNNN` citation in this section is
given against **`tckdb-adapters` `HEAD` (commit `fedfe6b`, "Record final re-home
validation")** — the pinned pre-ADAPT-0 baseline this whole phase's fixes are
diffed against. Most of §2 describes defects, several of which (C-3, C-4, C-5, C-6,
C-7) have since been fixed in the ADAPT-0 working tree; citing the tree where the
defect is actually visible, rather than a constantly-moving working copy, is what
keeps these line numbers meaningful over the life of this document. **C-9 is the
one exception**: it describes defensive guard code that does not exist at `HEAD` at
all (it was added during ADAPT-0's own work), so its citations are given against
the ADAPT-0 working tree instead — marked explicitly at that item. `ARC:*` and
`TCKDB:*` citations point into their own repositories' worktrees as checked out
alongside this one and are unaffected by either baseline.

**A5 established there are no unit changes in the 0.8.0→0.22.0 range**
(`SCHEMA_DRIFT.md` §3.7 — `EnergyUnit`, `ArrheniusAUnits`, `ActivationEnergyUnits`,
`CoordinateUnit` untouched; every `*_hartree` / `*_cm1` / `*_kj_mol` / `*_k` / `*_bar`
field kept its name, type and unit). **Index bases and semantics are the live
classes.** Unit defects that do exist are the adapter's own, not drift.

**Index bases are per-field, not uniform.** From A1 (`TCKDB_DEMAND.md`, "Index base is
not uniform"): `IRCPointPayload.point_index` and `PathSearchPointPayload.point_index`
are **0-based**; `CalculationScanPointPayload.point_index`, every atom index, every
`mode_index`, `constraint_index`, `coordinate_index`, `torsion_index` and
`participant_index` are **1-based**. The adapter already matches this
(`adapter.py:5920` writes IRC `point_index` from `len(points)` starting at 0;
`adapter.py:172` writes scan `point_index` from `enumerate(samples, start=1)`).
**A blanket rebase would corrupt IRC.** Any fix must be per-field.

### C-1 — Constraint atom indices: a mixed base, a skipped rebase, and a wrong default · **live silent off-by-one** · trivial (two repos)

Three findings compound into one defect. Each was verified independently.

**(a) ARC's constraint index base genuinely varies by ESS.** The Gaussian parser emits
`'index_base': 1` (`ARC:arc/parser/adapters/gaussian.py:1524`, docstring `:1373`); the
ORCA parser emits `'index_base': 0` (`ARC:arc/parser/adapters/orca.py:747`, docstring
`:659` — "ORCA atom indices remain in their native 0-based convention"). Both flow
verbatim into `opt_constraints[]` (`ARC:arc/output.py:1174`), `freq_constraints[]`
(`:1178`), `sp_constraints[]` (`:1214`) and `rotor_scans[].constraints[]` (`:1671-1673`).
ARC's schema records this honestly as a **mixed enum**, not a const:
`"index_base": {"enum": [0, 1]}` (`ARC:arc/schemas/output_yml_schema.json:411-416`),
described at `:387` as "1 for Gaussian, 0 for Orca". ARC does the right thing; the
information has to survive the adapter.

**(b) ARC defaults a missing `scan_software` to the Gaussian parser — real, but it does
not corrupt this route.** `_scan_constraint_parser_for` (`ARC:arc/output.py:1715`)
**defaults a missing or empty `scan_software` to the Gaussian parser** (`:1725-1726`),
and `scan_software` is missing on every ordinary (1D) rotor scan ARC produces (§0.5). But
`parse_gaussian_constraints` only emits a record from Gaussian ModRedundant `F`-line
syntax, which no ORCA log ever contains, and ARC's own ORCA parser documents that ARC
never emits the one ORCA syntax it does recognise (`%geom Constraints`) either
(`ARC:arc/parser/adapters/orca.py:660-663`) — confirmed by running both parsers over
every real ORCA artifact in `ARC:arc/testing` (21 files, including both real ORCA
rotor-scan logs): every result is `[]`. So the wrong-parser default never actually
stamps a wrong `index_base` on a rotor-scan constraint; it silently emits nothing,
same as a correct dispatch would on today's ORCA output. Real defect (§0.5), not a
live corruption; downgraded out of ARC-0's correctness tier.

**(c) The adapter skips the rebase entirely on the legacy dict shape.**
`constraints.py:193` enters the rebase block only `if raw.get('coordinate_type') is not
None`. The legacy branch reads `atoms` at `:169`, builds `atom_ints` at `:188`, and
passes them to `from_atoms` at `:211-213` **unshifted**, assuming they are already
1-based. An ORCA-sourced legacy constraint on atoms 1..N is silently accepted pointing
at the wrong atoms; on atom 0 it is loudly rejected. (The neutral branch is correct and
strict: it drops the constraint when `index_base` is missing or not in `(0, 1)`,
`constraints.py:197-201`.)

**Fix:** ARC-side, the live part is (a)+(c) — nothing to fix in ARC itself beyond what
the adapter does with the mixed enum it already receives honestly. (b)'s fix — make
`scan_software` required or fix the stale docstring — is real but demoted to
documentation/robustness (C-10), not correctness-tier. Adapter-side, refuse the legacy
shape rather than assume its base. Do not silently normalise on either side.

### C-2 — Scan-coordinate `index_base` defaults to 1 · **latent, not live** · trivial

`adapter.py:139`: `index_base = int(coordinate.get("index_base", 1))`, applied at `:140`
as `int(atom) - index_base + 1`, with no validity check beyond `len <= 4` at `:149` and
— unlike `constraints.py` — no `index_base in (0, 1)` guard, so an out-of-range value is
applied blindly and can produce non-positive indices.

A4 flagged this `UNVERIFIED`. **It is now settled and the news is good:**
`rotor_scans[].result.coordinate` always carries `index_base`, it is emitted as an
unconditional literal `1` (`ARC:arc/output.py:1901`), it is in the coordinate def's
`required` list (`ARC:arc/schemas/output_yml_schema.json:771`), and it is pinned as
`{"const": 1}` (`:789-791`). **The default is safe on the only path that reaches it
today.**

It is still worth closing, because the guard is one schema revision away from being
load-bearing and because the same default would be **unsafe** if ever applied to
`constraints[]`, where 0 is legal and reachable (C-1). Downgrade from "fix now" to
"fix with C-1, in the same PR". The only test exercises an explicit `index_base: 1`
(`tests/test_adapter.py:9075`); add the omitted-key case.

### C-3 — Two Hartree→kJ/mol constants for two same-named fields · trivial

`adapter.py:5820` defines `_HARTREE_TO_KJ_MOL = 2625.4996` inline, applied at
`:5943` and `:5999` to `irc_result.points[].relative_energy_kj_mol`.
`_vendor.py:43` computes `E_h_kJmol = E_h * Na / 1000 = 2625.4998583629967`
(from `E_h` at `_vendor.py:39` and `Na` at `:41`), applied at `adapter.py:6478` to
`path_search_result.points[].relative_energy_kj_mol`. Relative difference ~9.8e-8 —
scientifically negligible, but two identically-named, identically-united fields are
computed with different constants, and the inline constant's comment
(`adapter.py:5817-5819`) claims CODATA-2018 agreement while `_vendor.py` carries older
values. One-line fix; include it because it is free and because a reviewer who finds
it later will distrust everything around it.

**Status: fixed in the ADAPT-0 working tree** (both fields now consume
`_vendor.E_h_kJmol`; `_HARTREE_TO_KJ_MOL` is gone — see
`tests/test_adapter.py::TestSingleHartreeToKjMolConstant`). Rebuilding the golden
fixture through the pre-fix and post-fix code confirms the fix's actual effect: four
`relative_energy_kj_mol` leaves change, all under `irc_result` (two in the reaction
route, two in the matching standalone TS payload); `path_search_result` was already
on the vendored constant and does not move (see `tests/test_golden_corpus.py`'s
`test_phase3_disk_corpus_builds_all_payloads_from_sidecar` for the measured diff).

### C-4 — An unrecognised Arrhenius unit string ships the number without the unit · trivial, tier-1 blast radius

`arc_to_tckdb_a_units` (`adapter.py:597`) returns `None` on a miss and logs at
`logger.debug` (`:610-613`). The emission site writes `block["a"] = float(a)`
unconditionally at `adapter.py:5599` and gates `a_units` at `:5603-5604`. The `Ea`
path is identical (`arc_to_tckdb_ea_units`, `adapter.py:617`; debug log `:627-630`;
`reported_ea` at `:5617`, `reported_ea_units` gated at `:5620-5621`).

A2 flagged this as its highest-priority unverified kinetics risk and could not settle
it: "**UNVERIFIED: whether every RMG Arrhenius A-unit string is a member of
`ArrheniusAUnits`.** If one is not, it is a hard upload rejection" — except it is not
a rejection, it is a *unitless rate constant*, because both fields are optional.
A rate coefficient with no unit is not recoverable downstream.

Fix: raise, or emit at WARNING and skip the kinetics block. Never ship the magnitude
without the unit. This is the cheapest tier-1 correctness fix in either backlog.

**Status: fixed in the ADAPT-0 working tree, then corrected again in adversarial
review.** The first pass over-corrected: it raised `ValueError` on an unresolvable
`A_units`/`Ea_units`, which is worse than the original defect on ARC's own
documented output — `arc/schemas/output_yml_schema.json` requires `A_units` but
allows it to be `null`, and `arc/output.py` deliberately emits `null` whenever `A`
isn't a `(value, unit)` tuple, so a routine, valid ARC record would abort the
*entire* reaction bundle (species, TS, geometry, IRC, path-search — the call site in
`_build_computed_reaction_payload` is unguarded) over one optional field. Current
behavior: omit `a`/`a_units` (or `reported_ea`/`reported_ea_units`) and log at
WARNING; the rest of the kinetics block and the whole bundle still build. See
`tests/test_adapter.py::TestComputedReactionBundle::
test_kinetics_a_units_null_reaction_still_uploads_species_ts_geometry`.

### C-5 — `resolution_degrees` is written from a step whose unit was not checked · trivial

`adapter.py:168-169` writes `coord_out["resolution_degrees"] =
coordinate["requested_step_size"]`. The same function reads `coordinate.get("unit")`
thirteen lines earlier for `value_unit` (`:155`) and again per point (`:180`), so the
unit is in hand and simply not consulted. A `distance`-kind scan (kind mapping at
`:143`) with an Ångström step lands its step size in a field named degrees.

**Status: fixed in the ADAPT-0 working tree** — `resolution_degrees` is now only
stamped when `coordinate.get("unit")` is literally `"degree"` (`adapter.py:186-200`).

### C-6 — Applied-energy-correction units are defaulted, not read · trivial

`adapter.py:226`:
`unit = str(total.get("unit") or ("hartree" if correction_type == "atom_energy" else "kcal_mol"))`,
feeding both `scheme["units"]` (`:231`) and `value_unit` (`:249`). A2 confirms the
guess is *usually* right — the run-level `atom_energy_corrections` table is hartree and
`bond_additivity_corrections` is kcal/mol — and also records that ARC's own docs
previously stated kJ/mol for the BAC table and were wrong
(`ARC:docs/output_yml_schema.md:258`). A wrong energy-correction unit is a direct
scientific error on a tier-1 quantity.

Fix: omit the correction rather than default the unit, and open the ARC-side item to
state the unit as data on the run-level tables.

**Status: fixed in the ADAPT-0 working tree** — a missing `total.get("unit")` now
omits the whole correction with a WARNING (`adapter.py:269-276`) instead of guessing
hartree/kcal_mol.

### C-7 — `torsion_index` counts emitted torsions, not ARC's rotor ordinals · low, semantic

`adapter.py:5224` sets `next_index = 1`; the unrecognised-treatment skip is a bare
`continue` at `:5229-5230` *before* `next_index` is used; assignment at `:5232`,
increment at `:5265`. Dropping rotor #2 renumbers rotor #3 to `2`. The warning at
`:5246-5250` reports the emitted ordinal, so the log does not reveal the shift either.
The deposited torsion set is internally consistent but no longer joinable to the
*emission-order-vs-list-position* distinction this item is about — a curator
re-deriving a barrier from `output.yml`'s own `statmech.torsions[]` list loses the
ability to point at "entry N" once a skip has silently renumbered it.

**Status: fixed in the ADAPT-0 working tree** — `torsion_index` is now the entry's
1-based position in `output.yml`'s own `statmech.torsions[]` list (`adapter.py`'s
`_build_slim_torsions`), not an emission counter, so a skip leaves a gap instead of
renumbering later entries. **Correction (adversarial review):** this restores a
stable reference into the record's *own* list, not "ARC's `rotors_dict` ordering" as
an earlier draft of this fix's docstring claimed — `_get_torsions`
(`ARC:arc/output.py:1435-1438`) already skips any rotor whose `success is not True`
when building `statmech.torsions[]`, so that list is itself compacted relative to
`rotors_dict` before the adapter ever sees it, and ARC exports no raw rotor ordinal
to restore joinability to in the first place. See `_build_slim_torsions`'s docstring
for the corrected claim.

### C-8 — `statmech.torsions[].coordinates[].atomN_index` is 1-based by assumption only

`_coerce_torsion_coordinates` (`adapter.py:5269`) validates shape, `>= 1`, distinctness
and `len == 4` (`:5293-5299`, `:5307-5317`) and copies indices through verbatim, on a
docstring assumption at `adapter.py:5182`. A2 confirms ARC's side is in fact 1-based
(`rotors_dict['scan']`, asserted at `ARC:arc/output.py:1437`) **but records no
index-base field on the torsion record**, unlike `rotor_scans` which carries an
explicit one. Correct today, unverifiable by either side, and one ARC refactor away
from being silently wrong. The fix belongs in ARC (ARC-0).

### C-9 — Semantic shifts from the drift range that are *not* live

Recorded so nobody re-opens them. **The two `adapter.py` citations below are the one
exception to this section's `HEAD`-baseline convention** (see the note at the top of
§2): both describe defensive guard code added during ADAPT-0's own work, which does
not exist at `HEAD` at all, so they are cited against the ADAPT-0 working tree
instead.

* **`species_entry_kind: "transition_state"`** — `StationaryPointKind` has exactly two
  members, `minimum` and `vdw_complex`, at `TCKDB:schemas/python/tckdb-schemas/tckdb_schemas/enums.py:49-51`,
  and the enum body is **identical at tag `tckdb-schemas-v0.8.0`**. It was never valid,
  so A5's `BROKEN`/`SEMANTIC_SHIFT` verdict is right about the value and wrong about it
  being new drift. The adapter emits `"minimum"` (working tree `adapter.py:3189`) and
  `_species_entry_payload` refuses any `is_ts=True` record before that line, so the path
  is unreachable.
* **`freq_n_imag` × `species_entry_kind`** — now defended at working tree
  `adapter.py:4482-4493` and `:4495-4506` (the two raises, for the species and TS
  directions respectively), with the reasoning in `_freq_result_payload`'s docstring at
  `:4415-4441`. The docstring itself records that no path inside ARC's pipeline is
  known to produce the contradiction. The guard is right to exist and is not a
  backlog item.
* **`kinetics[].degeneracy`** — cannot be populated from ARC at all: ARC does not compute
  reaction-path degeneracy (`ARC_SUPPLY_LATENT.md` §4; the only degeneracy handling in the
  tree is `ARC:arc/scripts/rmg_kinetics.py:98`, a comparison-only helper), and
  `ARC:arc/schemas/output_yml_schema.json` forbids the key. It is reachable only through a
  hand-written `output.yml`. A5's own confidence note (`SCHEMA_DRIFT.md` §6, item 1)
  already downgrades this row to a *conditional* break.
* **`statmech_treatment`** — the break needs a rotor-aware treatment ARC cannot emit.
  Consistency work already landed in the working tree.

**A5 was reliable on what the schema says and consistently over-called what breaks.**
Of its 8 `BROKEN` rows, 6 are unreachable from an ARC-produced payload.

### C-10 — `scan_software` is never stamped, and the docstring claiming otherwise is stale · documentation/robustness · trivial

Demoted here from an earlier draft's correctness-tier ranking; see §0.5 for the full
disproof. `scan_software` is written exactly once anywhere in `arc/`
(`ARC:arc/species/species.py:1378`, ND-directed-rotor construction only) and never by
`find_internal_rotors` (`ARC:arc/species/conformers.py:1675-1728`, the ordinary 1D path)
or by `scheduler.py`. So on the path that produces the overwhelming majority of rotor
scans, `_scan_constraint_parser_for` (`ARC:arc/output.py:1715`) always takes its
"missing `scan_software`" branch and falls back to the Gaussian constraint parser
(`:1725-1726`) — and the docstring at `:1681` claiming the field is "set by the
scheduler when the scan job completes" is false on this branch. **This does not corrupt
data today** (C-1b, §0.5): the Gaussian parser only matches ModRedundant `F`-line syntax
that no ORCA log contains, so the fallback silently emits `[]`, identical to a correct
dispatch. It is real dead code and a stale comment, not a live index-base risk.

**Fix:** either make `scan_software` required on every rotor dict that reaches
`write_output_yml` (closing the gap honestly) or correct the docstring to state that the
field is unset on the ordinary rotor path and the Gaussian fallback is best-effort only.
Ships as ordinary ARC-side documentation/robustness work (folds into ARC-3's
housekeeping, §3), not ahead of any value tier.

### C-11 — `smiles` now carries isotopic identity, and ARC destroys isotopes

`SCHEMA_DRIFT.md` §3.3: `isotopologue_label` is gone; isotopic substitution is expressed
*only* through atom-resolved SMILES isotope notation. ARC's exported geometry text is
bare symbols at every site (`ARC_SUPPLY_EXPORTED.md`, "Isotopes are destroyed by every
geometry in `output.yml`"). Consequence: an isotopologue and its parent deposit as the
**same species entry**. This is an identity collision, not a rounding loss.

It is *not* fixed by exporting `final_xyz['isotopes']` — see ARC-8. Until then, the
honest position is that **ARC cannot deposit isotopologues**, and that should be stated
somewhere a depositor will read it rather than discovered from a merged record.

---

## 3. ARC-side backlog

All items extend `feature_arc_result_export_contract` (depositor's decision). Every
one needs a matching `arc/schemas/output_yml_schema.json` addition — the schema is
closed (`unevaluatedProperties: false`), so "add a key to the writer" is never the
whole change. A3 says so directly: read `trivial` as "one to three lines in the writer,
plus schema/fixture housekeeping", not "no work" (`ARC_SUPPLY_LATENT.md`, coverage
self-assessment).

**Landed already, excluded from the ranking:** `statmech.rejected_torsions` —
uncommitted, verified `+98/−0` on `ARC:arc/output.py` (`_get_rejected_torsions` at
`:1514`, wired in at `:1409`), with `+45` on `ARC:arc/schemas/output_yml_schema.json`
(new `$defs.rejected_torsion` at `:975`, `statmech.properties.rejected_torsions` at
`:1086-1091`) and `+322` across `output_test.py` / `output_schema_test.py`. One loose
end is carried into ARC-3.

Ordering is (correctness) → (tier, then ascending cost), with the policy's explicit
allowance that a cheap tier-2 item beats an expensive tier-1 one.

### ARC-0 — Stop guessing an index base · **correctness** · trivial–moderate

Two sub-changes, both in the export contract rather than in science. (An earlier draft
carried a third sub-change here — refusing to default `_scan_constraint_parser_for` to
Gaussian — ranked above every value tier. §0.5 disproves the harm: both parsers return
`[]` on every real ORCA artifact tested, so the default never actually stamps a wrong
`index_base` on anything. It is demoted to C-10, an ordinary documentation/robustness
item, and dropped from this correctness-tier list.)

1. **Put an explicit `index_base: 1` on `statmech.torsions[]`**, matching what
   `rotor_scans[].result.coordinate` already does
   (`ARC:arc/schemas/output_yml_schema.json:789-791`, `const: 1`, required at `:771`).
   The torsion def (`:922`ff) — and the new `rejected_torsion` def — have no such
   field, so the 1-based-ness rests on a comment (`ARC:arc/output.py:1438`) backed by
   `ARC:arc/species/species.py:71`. Closes C-8 at the source.
2. **Reconcile ARC's own two IRC paths.** The rich path emits Gaussian's 1-based
   per-branch `Point Number` (`ARC:arc/tckdb_evidence.py:264`, sourced from
   `ARC:arc/parser/adapters/gaussian.py:807`, regex at `:767`); the fallback uses
   `enumerate()` and is 0-based (`ARC:arc/tckdb_evidence.py:284-285`). The fallback
   fires whenever `parse_irc_path` returns falsy (`:257`), so both are reachable for
   the same ESS. **It is worse than a base mismatch:** `parse_irc_traj`
   (`ARC:arc/parser/adapters/gaussian.py:709-730`) *includes* the `Point Number: 0`
   seed geometry that `parse_irc_path` deliberately skips
   (`ARC:arc/parser/adapters/gaussian.py:738-740`), so for the *same log* the two
   paths differ in origin **and in point count**. Separately, ARC spawns one
   single-direction IRC job per branch (`ARC:arc/job/adapters/gaussian.py:374`), so
   each branch is numbered from 1 in its own trajectory and the indices collide across
   the two — which `IRCResultPayload.validate_points` rejects on the TCKDB side.

**Unlocks:** no new TCKDB fields. **Value:** prevents wrong atoms and mismatched
trajectory points being deposited under a passing validation. **Risk:** none — it adds
a statement, it does not change a number.

### ARC-1 — Transition-state validation evidence · tier 1 · trivial + moderate

**What to export.** `populate_ts_checks` (`ARC:arc/species/species.py:2241`) builds
`ts_checks` with the five saddle-point verdicts and a warning string (`:2244-2246`):
`{'E0', 'e_elect', 'IRC', 'freq', 'NMD'}` plus `'warnings'`. `arc/output.py` never
references it (grep: zero hits). The TS record's only IRC-adjacent field is
`irc_converged` (`ARC:arc/output.py:1279/1281`), sourced from
`entry['job_types']['irc']` — **a job-completion flag, not `ts_checks['IRC']`**, which
is what `ARC:arc/checks/ts.py:517/536/554` actually sets. So `output.yml` records that
the IRC *ran*, never that its endpoints matched the declared reactants and products —
the scientifically load-bearing claim, and exactly what
`TransitionStateValidationEvidenceIn` exists to carry.

Two halves, and they should ship in this order:

* **1a (trivial, `reachable_from_export: true`):** `kind`, `passed`, `rationale` from
  `ts_checks` + `ts_report`. Live at the write site.
* **1b (moderate, `reachable_from_export: false`):** the participant mappings.
  `_perceive_irc_fragments` (`ARC:arc/checks/ts.py:555`) builds `fragment_indices` at
  `:588` — one 0-based atom index list per connected component of the IRC endpoint —
  uses it only to slice per-fragment xyz at `:606-610`, and returns just the perceived
  `Molecule` objects (`:613-615`, `:640`). `_match_fragments_to_species` (`:644`) is
  annotated `-> bool` and returns bare booleans throughout (`:658-667`, `:683`). Both
  are called from `check_irc_species_and_rxn` (`:497`) at `:527-533`, whose only output
  is `ts_checks['IRC'] = True/False`. Those two discarded values, joined, *are*
  `reactant_participant_mapping` / `product_participant_mapping`. The fix is two
  return-type widenings and a stash on the TS species.

**Unlocks:** 6 leaves (`validation_evidence[].*`), all tier 1. Small row count, large
consequence: absent evidence produces `transition_state_missing_irc_evidence`
(`TCKDB:backend/app/services/transition_state_validation.py:41`).

**Correctness risk — high, and it decides the sequencing.** The mappings are 1-based on
both sides (`SCHEMA_DRIFT.md` §3.1) against ARC's 0-based `fragment_indices`. TCKDB
checks coverage as a *set* against `1..natoms`, so a uniformly shifted list fails
loudly but a correctly-covering, wrongly-assigned one passes. And **TCKDB refuses
incomplete evidence presented as passing.** Verified: the distance-matrix fallback
(`ARC:arc/checks/ts.py:541-555`) compares whole-system bond lists via
`get_bonds_from_dmat` over the full `dmat` (`:547-549`) and **never splits atoms into
components at all** — no fragments, no partition, just global bond-set equality. That
path must therefore emit evidence with *no* mappings rather than partial ones. Ship 1a
alone if 1b cannot be done carefully.

### ARC-2 — Kinetics honesty: `degeneracy_convention` and `reversible` · tier 1 · trivial

Two fields, two silent semantic errors, both fixed by reading something already in
scope:

* **`degeneracy_convention`.** TCKDB defaults it to `unknown` "for legacy producers"
  (`SCHEMA_DRIFT.md` §1). Verified: ARC's Arkane `reaction()` input template
  (`ARC:arc/statmech/arkane.py:84-92`) declares exactly five items — `label`,
  `reactants`, `products`, `transitionState`, `tunneling` — and `degeneracy` appears
  nowhere in the file. No reaction-path degeneracy factor is folded into the fitted
  `A`, so the correct constant is `not_applied`, and every ARC record currently
  deposits "we don't know" (`SCHEMA_DRIFT.md` §3.4).
* **`reversible`.** `ReactionFamily.__init__` (`ARC:arc/family/family.py:151`) sets
  `self.reversible = is_reversible(...)` at `:159` and `self.own_reverse` at `:160`.
  `ARC:arc/output.py:2035` writes `'family': rxn.family` — a plain string label
  (property at `ARC:arc/reaction/reaction.py:217-223`) — and nothing else. Neither
  `reversible` nor `family_own_reverse` reaches `output.yml`, even though
  `ARCReaction.family_own_reverse` exists (`:233-238`) and *is* serialised into ARC's
  restart dict (`:293-294`). TCKDB's `reversible` **defaults to `True`**, so an
  irreversible family currently uploads as reversible with no warning.

**Unlocks:** 2 leaves. **Value:** disproportionate — both are defaults that are wrong
rather than absent, which is the failure mode `PRIORITY_POLICY.md` ranks hardest.
**Risk:** `not_applied` describes the template. If a future ARC path invokes Arkane
differently, the assertion becomes a lie; gate it on the template actually used rather
than hardcoding it. Asserting the wrong convention is worse than `unknown`.

### ARC-3 — Rotor and statmech completeness · tier 1 · trivial–moderate

`_get_torsions` (`ARC:arc/output.py:1413`) reads `success` (`:1436`, the `continue`
gate), `scan` (`:1438`), `pivots` (`:1439`), `symmetry` (`:1440`), `type` (`:1441`),
`dimensions` (`:1443`), and `scan_path` indirectly through `_get_rotor_barrier`
(`:1451` → `_resolve_scan_path` `:1562`) — from a structure documented at
`ARC:arc/species/species.py:69-88` as carrying considerably more. Add:

* `top` (`ARC:arc/species/species.py:70`, "1-indexed") → TCKDB's `top_description`, the
  only field saying *which side* rotates. Trivial.
* `invalidation_reason` (`ARC:arc/species/species.py:76`) — why a found rotor was
  excluded. Trivial. Pairs with the already-landed `rejected_torsions` work; without it
  a reader cannot distinguish "no torsion here" from "torsion found and deliberately
  rejected because its pivots sit in the TS reaction zone".
* `skip_rotors` → an honest `statmech_treatment` / `uses_projected_frequencies`.
  **Moderate, and blocked:** `skip_rotors` is a `process_arc_project` argument that never
  reaches `write_output_yml`. Deriving the treatment from "are there any successful
  rotors" conflates *none found* with *deliberately skipped*, so the plumbing is the
  work.

**Do not add `max_e`, contrary to A3.** A3 recommends exporting
`rotors_dict[i]['max_e']` as "ARC's own stored barrier" that the writer wastefully
recomputes. **The field is dead in production.** Its only assignment anywhere in `arc/`
is `ARC:arc/species/conformers.py:1727` — `rotor['max_e'] = None` — and although
`determine_rotor_symmetry` returns `(symmetry, max_e, …)`, both scheduler call sites
subscript `[0]` and discard the barrier (`ARC:arc/scheduler.py:3167-3170` and
`:3274-3276`). It is an unfilled slot, not latent data. If the barrier is wanted, the
work is wiring `determine_rotor_symmetry`'s second return value at those two sites —
a different, larger task than an export.

**Unlocks:** 9 leaves (6 tier 1). **Risk:** `rotors_dict` stores the same four-atom
quartet twice — as `scan` (1-indexed) and `torsion` (0-indexed) — in adjacent keys
(`ARC_SUPPLY_LATENT.md` §3). `output.yml` correctly exports only `scan`. **Any new
export must not emit both unlabelled.**

**Housekeeping on the landed work:** `rejected_torsions` is emitted unconditionally by
`_statmech_to_dict` (`ARC:arc/output.py:1409`) but was **not** added to `statmech`'s
`required` list in `ARC:arc/schemas/output_yml_schema.json`. Producer and schema
disagree about whether a consumer may rely on the key. Either add it to `required` or
make the adapter treat it as optional; do not leave the two disagreeing.

**Housekeeping carried from C-10:** stamp `scan_software` on every 1D rotor dict (from
the scheduler at scan-completion time, or from `find_internal_rotors` at
rotor-discovery time) and fix the stale docstring at `ARC:arc/output.py:1681`. Trivial,
documentation/robustness — see C-10 and §0.5 for why this is not correctness-tier work.

### ARC-4 — Per-species attribute reads: T1, per-species `opt_level` · tier 1/2 · trivial

* **`spc.t1`** (`ARC:arc/species/species.py:351`), assigned in `post_sp_actions` at
  `ARC:arc/scheduler.py:2960` from `parser.parse_t1(...paths['sp'])`. The standard
  single-reference-quality gate, a first-class TCKDB field, one dictionary entry.
  Unlocks `wavefunction_diagnostic.t1_diagnostic` on every calculation route.
  **Availability is `conditional`, not `always`:** the assignment is guarded by
  `if self.sp_level is not None and 'ccsd' in self.sp_level.method`, so `t1` is `None`
  for every DFT-only run. Export it as absent rather than as a null the consumer might
  read as "measured and zero".
* **`spc.opt_level`** (`ARC:arc/species/species.py:430`). `output.yml` exports one
  run-level `opt_level` for the whole document; under `adaptive_levels` the per-species
  level differs. This one is not merely missing — **it is currently misreported**, and it
  compounds with the adapter's own `_resolve_level` fallback (ADAPT-7).

**Unlocks:** 30 leaves across tiers 1/2/4 (the `wavefunction_diagnostic` subtree resolves
across all six calculation routes). Note that only `t1_diagnostic` has an ARC source —
`d1_diagnostic` has no parser anywhere in ARC and stays absent.
**Risk:** low. **Caveat carried from A3:** the claim that a given ESS yields T1 rests on
parse-method presence, not on a verified regex (`ARC_SUPPLY_LATENT.md`, coverage
self-assessment) — though the `'ccsd' in method` guard bounds the exposure.

### ARC-5 — Bundle-local geometry and calculation keys · tier 2 · moderate · **prerequisite**

`output.yml` has no geometry keys and no calculation keys; calculations are identified
by *field name* (`freq_log`, `sp_log`). Two consequences:

* ADR 0011 makes naming the geometry an atom map counts into **mandatory**, so the atom
  map cannot be expressed at all until a key convention exists.
* `depends_on[]` — the opt→freq→sp dependency edges are derivable from the paths dict
  already in scope, but edges need endpoints to name.

This is a schema decision, not a data-availability problem, and it is on the critical
path for the highest-value item in A3's inventory. **Ship it before ARC-6.**

**Unlocks:** 4 leaves directly; gates 7 more (ARC-6) and removes the adapter's need to
mint keys itself. Classifying §0.2's 75-row `SOURCE_UNCONFIRMED` "adapter-asserted"
bucket by field-name suffix (`.key`, `.role`, `*_index`) rather than by its
literal/synthesized/derived split: **47 of the 75** are keys, roles or indices the
adapter synthesizes because ARC does not name them (`scan_rotor_1`-style calc keys,
`depends_on[].role` literals, `geometry_key`, every `*_index` field in the bucket); the
other 28 are unrelated literal constants (`quality: "raw"`, `workflow_tool_release.name`,
`species_entry.molecule_kind`, artifact/calc-type labels). An earlier draft's "75 of the
80 … are keys, roles and indices" overstated this by conflating the whole bucket with
this narrower classification — corrected here rather than left as an unresolved
qualitative claim.

### ARC-6 — Atom map, in ARC's own coordinates · tier 3 · moderate · **depends on ARC-5**

**Do not have ARC emit `ReactionAtomMapIn` directly.** A3's analysis is the most careful
thing in the Phase A set and its conclusion should be followed exactly: emit under
`arc_only` a neutral block —

```yaml
atom_map:
  index_base: 0
  direction: reactant_to_product
  values: [...]
  reactant_atom_counts: {label: n, ...}
  product_atom_counts:  {label: n, ...}
  algorithm: family_template | general | isomerization | flipped
  provenance: derived | user_supplied | restored_from_restart
  ts_atom_order_is_reactant_order: true | null
```

— and let the adapter do the reshaping, where it can see the TS geometry alongside the
map (ADAPT-7).

**Where it lives.** `ARCReaction.atom_map` (`ARC:arc/reaction/reaction.py:156`) is a lazy
property; `map_reaction` (`ARC:arc/mapping/driver.py:57`) dispatches three ways and
**discards which branch fired** — recovering that is what `algorithm` needs, and TCKDB
*requires* `atom_map.note` to name the algorithm whenever `source='inferred'`, so it is
not optional polish. `provenance` needs a flag at the three sites that assign `_atom_map`
(the property, the setter at `:167`, the restart restore at `:352`).

**Unlocks:** 7 leaves. Small; see §6 for why the row count understates it.

**Correctness risk — the highest in either backlog.** Three independent transformations
separate ARC's value from TCKDB's: (1) 0-based → 1-based on **both** sides of every pair;
(2) flat running index → (participant, local index), which needs `get_species_count()`
rather than `len(r_species)` because `remove_dup_species()`
(`ARC:arc/reaction/reaction.py:652`) collapses `2 CH3` to one entry while TCKDB requires
two participants; (3) reactant→product → participant→TS, and **ARC's map does not mention
the TS at all**.

The last one is only partly recoverable and A3 says so honestly: ARC's `linear` and
`rits_ts` adapters document that their guesses are in concatenated-reactant atom order,
so for those the reactant leg is the identity and the product leg inverts `atom_map` —
but **AutoTST, GCN, KinBot and GoFlow assert nothing about ordering, and neither does the
scheduler that accepts their guesses**. This is recorded as `UNVERIFIED` on the
`atom_to_ts` row. `ts_atom_order_is_reactant_order: null` is the correct output when the
adapter cannot vouch; emitting an identity map for a geometry produced by an adapter that
silently reordered atoms is precisely the manufactured provenance ADR 0011 forbids,
dressed as a depositor declaration. **Settle the four unverified adapters before anyone
implements the participant→TS emission.**

### ARC-7 — The JobAdapter→record handoff · tier 2 · hard · **216 leaves**

`ARC:arc/main.py:649` passes `species_dict`, `reactions` and `output_dict` to
`write_output_yml` — live object graphs, which is why 67 of A3's 75 rows are reachable.
`scheduler.job_dict` is **not** passed, and every one of A3's 8 unreachable rows traces
to that. `ARC:arc/output.py:1204` already names the missing piece: *"Future producers (a
JobAdapter→species-record handoff carrying the raw `self.fine` / `self.grid` / `self.args`
per job) can grow these dicts without touching the adapter wiring."*

**Unlocks, on one change:**

* `calculation.parameters[]` (48 leaves, mechanically re-derived; 42 `ARC_ABSENT` and 6
  already-flagged `ADAPTER_GAP` `raw_key` rows) — the per-job final argument set,
  *including everything `ARC:arc/job/trsh.py` added after a failure*, i.e. the difference
  between what ARC asked for and what actually ran. A1 ranks this fourth by scientific
  weight, and not for bookkeeping reasons: three canonical keys (`freq.hessian_method`,
  `grid.quality`, `opt.convergence`) resolve ADR 0012's noise floor τ. With no
  parameters, τ falls back to `protocol_not_recorded` = 50 cm⁻¹ and **a transition state
  with a genuine soft extra imaginary mode gets a structural flag, which excludes the
  record from default queries and bulk TS exports** (`SCHEMA_DRIFT.md` §3.5). The upload
  succeeds; the record is invisible.
* `calculation.execution_environment` (168 leaves, mechanically re-derived, all
  `ARC_ABSENT`) — server, queue, cores, memory, wall time.

Combined: 216 leaves (144 tier 2, 72 tier 1) — up from an earlier, unreproducible
"198 leaves — 124 tier 2" figure.

**Cost is genuinely hard** and the payoff is lumpy: `ExecutionEnvironmentManifestPayload`
requires `schema_version`, a discriminated `runtime` and an `executable` reference, and
its pinned tiers demand ≥2 closure entries, an exact executable digest and an OCI
`@sha256:` image reference. But TCKDB's own design note is explicit that the `described`
tier (`module load gaussian/16`, no digests) is **fully acceptable and is not scored by
any reproducibility rubric** (`SCHEMA_DRIFT.md` §1). Target the `described` tier.

**Sequencing note.** If only part of this ships, ship the **three τ keys first** —
`freq.hessian_method`, `grid.quality`, `opt.convergence`. They are a small slice of the
parameters work and they are the difference between a queryable TS record and a
structurally-flagged one.

**Blocker to note:** `delete_check_files()` runs at `ARC:arc/main.py:614`, *before*
`write_output_yml`, so for a default run the Gaussian checkpoint an `artifacts[]` row
would embed is already deleted by export time. That is a separate, smaller fix.

### ARC-8 — Isotopes · untiered/identity · **hard** (A3's `trivial` is wrong)

**A3's `export_effort: trivial` on this concept is wrong and must not be actioned as
written.** A3 cites `ARCSpecies.final_xyz['isotopes']` as a one-line export. That source
exists but is not valid: `final_xyz` is never the user's declaration — it is overwritten
from a parsed log at `ARC:arc/scheduler.py:2487/2611/2614/3538`, and every parse builds
the dict through `str_to_xyz`'s defaults branch (`ARC:arc/species/converter.py:112`) or
`xyz_from_data(isotopes=None)` (`:447`). Independently, `xyz_to_str` emits an isotope
label only when `isotope_format` is passed and **no production ARC caller ever passes
it**. A user-declared substitution survives only into `initial_xyz`
(`ARC:arc/species/converter.py:671`) and the first successful optimisation destroys it —
which also means the QM job never saw the substitution
(`ARC:arc/job/adapters/gaussian.py:252` writes decks without isotope labels).

Exporting `final_xyz['isotopes']` would **assert natural abundance as a determined fact**
rather than record a substitution, which is worse than the current silence. The real fix
is a new field sourced from the species definition, before the opt overwrites it.
**Effort: hard. Plumbing is a prerequisite.**

Ranked last despite 39 open leaves (mechanically re-derived by path-fragment match on
`geometry.isotopes`, up from an earlier, unreproducible "37" — 31 `ADAPTER_GAP`, 6
`ARC_ABSENT`, 2 `ARC_LATENT`, none `WIRED`), because the cheap version of it is actively
harmful and the honest interim position is C-11: say plainly that ARC cannot deposit
isotopologues.

---

## 4. Adapter-side backlog

### ADAPT-0 — The correctness items · **first** · trivial

C-1 (adapter half, i.e. C-1c), C-2 through C-7 are all adapter-side and all small. They
are one PR, or at most two (index bases; units). Nothing else in this backlog should land
before them. C-8 is ARC-side (ARC-0); the adapter cannot detect it from the wire. C-1's
other half (C-1b) is also ARC-side, but demoted to documentation/robustness (C-10,
folded into ARC-3) rather than correctness — see §0.5.

**Also commit, in the same PR: the `BundleThermoIn` 422 fix.** `ROUTE_ADJUDICATION.md`
§5 (action item 6) opens this as a defect. It is not new work — it is **already fixed in
the working tree, uncommitted.** `_build_thermo_block` now takes `calc_keys_by_role` and a
`target_model` and resolves sources by role rather than by bare role literal
(`adapter.py:4913-4918`, docstring `:4919-4959` naming the prior bug directly: "A prior
version ... tested the bare role literal ... which meant a namespaced reaction call site
could never match"), with the resolution itself at `:5015-5022`. `git status --short` in
this repo shows `adapter.py` as modified, not committed. Land it with ADAPT-0 rather than
leave it sitting in the working tree indefinitely.

### ADAPT-1 — Decide what to do about the two unversioned endpoints · **architectural** · decision, then work

**188 of the adapter's 775 mappings (24.3%) target request contracts that are not in the
published `tckdb-schemas` package.** They split 130 `/uploads/transition-states` +
58 `/uploads/conformers`.

Verified: `CONFORMER_UPLOAD_ENDPOINT = "/uploads/conformers"` (`adapter.py:259`), used by
`submit_from_output` (`adapter.py:743`, POST at `:783`);
`TRANSITION_STATE_ENDPOINT = "/uploads/transition-states"` (`adapter.py:282`), used by
`submit_computed_ts_from_output` (`adapter.py:2805`, POST at `:2874`). Both are production
paths driven from `sweep.py` (`:124`, `:389`) via `VALID_UPLOAD_MODES`
(`config.py:63-68`). `grep -r deprecat tckdb_arc/tckdb_arc/` returns **zero** hits.
Their request models live in TCKDB's `backend/app/schemas/workflows/`, i.e. server-internal.

**And `conformer` is the adapter's default upload mode** (`config.py:169`:
`upload_mode: str = UPLOAD_MODE_CONFORMER`). So the default production path is bound to a
contract that can change without a schema version bump, and `tests/test_ts_upload.py`
reconstructs `TransitionStateUploadRequest` locally from fragment schemas because there is
nothing to import — a test that pins the adapter's *belief* about the contract, not the
contract.

This is not a mapping bug and it does not have an adapter-only fix. Three options, in the
order I would put them to TCKDB:

1. **Publish both request models in `tckdb-schemas`.** Restores versioning to 24% of the
   mapping surface. Cheapest for the adapter, needs TCKDB agreement.
2. **Migrate both modes onto the published bundle routes** (`computed-species` /
   `computed-reaction`) and retire the standalone paths. Largest adapter change; removes
   the exposure entirely.
3. **Accept it, and add a contract-drift canary** — a test that fetches the live
   OpenAPI schema for both routes and fails when it moves. Cheapest overall, but it
   detects the break rather than preventing it.

**Do not defer this past the re-pin.** The re-pin is the moment the adapter starts
tracking a moving server; that is precisely when an unversioned contract bites.

### ADAPT-2 — Build a rotor-scan calculation on the transition-state path · tier 1 · low

**The highest value-per-unit-cost item in either backlog, and no inventory reported it
as an adapter item.**

`_build_ts_block` (`adapter.py`, region 2456–2776 per A4's structural map) makes exactly
five `_build_calc_in_bundle` calls — `ts_guess`, `opt`, `freq`, `sp`, `irc` — and never
constructs a scan calculation. `ROUTE_ADJUDICATION.md` §2.4 found this and concluded the
data was not there to build one. §0.0 of this document shows it is: ARC runs rotor scans
on transition states (`ARC:arc/scheduler.py:1544`, no `is_ts` gate in `:1544-1636`) and
writes them to `transition_states[].rotor_scans` via the shared `_spc_to_dict`
(`ARC:arc/output.py:1302`, populated at `:1290`), with `rotor_scans` in the **required**
list of `common_species_fields` (`ARC:arc/schemas/output_yml_schema.json:1290`), which
`$defs.ts_record` includes wholesale (`:1621-1626`).

**Why it matters scientifically:** hindered internal rotation in a saddle point is
first-order in the rate constant. TCKDB has a slot for it, ARC computes it, and the
adapter drops it. That places it squarely in the depositor's tier-1 band.

**Cost: low.** The species path already does this — `_build_calc_in_bundle` is
parent-agnostic (`ROUTE_ADJUDICATION.md` §1.1 enumerates all eleven call sites) and the
scan translators (`_scan_entries_from_record`, `adapter.py:98`;
`_neutral_scan_result_to_tckdb`, `:131`) take a record, not a species. This is a new call
site plus routing, not new machinery.

**Correctness risk — inherits C-1 and C-2 wholesale.** TS rotor scans carry the same
`constraints[]` with the same mixed `index_base`. The reaction-zone-pivot exclusion
(`ARC:arc/checks/ts.py:352-356`, marker `pivTS`) is real and does mean a TS's rotor set
can be narrower than a minimum's when pivots sit in the reaction zone — but across 34 TS
rotor entries in the 12 completed runs sampled for this document
(`/home/calvin/code/arc_rotor_scan/*/restart.yml`: 19 succeeded, 11 failed, 4 still
pending/never attempted), **zero of the 11 failures cite the `pivTS` rule**; every one is
an ordinary scan-quality rejection (initial/final or consecutive-point energy
inconsistency). The coupling to `statmech.rejected_torsions` is real and should still
ship alongside (a reader otherwise cannot tell "no torsion here" from "torsion found and
excluded"), but the motivating pivTS-exclusion scenario is not what was observed —
soften the rationale accordingly. **Land ADAPT-0 first.**

**Golden-fixture prerequisite.** Neither golden fixture used to pin the adapter's output
contains a `rotor_scans` key at all — confirmed by direct search, not inference:
`grep -c rotor_scans tckdb_arc/tests/fixtures/golden/phase3_output.yml` is 0 (the fixture
does include a TS record, `is_ts: true`, just no rotor scan on it), and
`grep -c rotor_scans ARC:arc/testing/tckdb_evidence/golden/output.yml` is also 0, for
either species or TS. A new golden fixture built from a real TS rotor scan is
prerequisite work for this item, not an afterthought for its test suite — there is
currently nothing to regression-test the new call site against. Note separately that
`rotor_scans` being in `common_species_fields`'s schema `required` list (§0.0) is not
honoured by either of ARC's own goldens today; that is a pre-existing gap in ARC's test
fixtures, independent of this item.

**Consistency note, not a defect: where a scan calc's constraints live.**
`ROUTE_ADJUDICATION.md` §5 (action item 7) raised this: `_scan_entries_from_record`
(`adapter.py:98-128`) writes each translated scan calc's constraints to the calc's
**top-level** `constraints` (`:122-127` → `_build_calc_in_bundle`'s write at
`:1472-1474`; `ROUTE_ADJUDICATION.md`'s own citation of `:1461-1463` is off by ~11 lines
and lands on the `artifacts` block, not this write), not into `scan_result.constraints`
— a field `CalculationScanResultCreate` also defines. Both destinations are valid and
share one persistence path: TCKDB's own validator says so directly, *"Top-level
`constraints` and `scan_result.constraints` share the same `calculation_constraint` table
at persistence time, so `constraint_index` must be unique across the union of both lists
within one calculation"*
(`TCKDB:schemas/python/tckdb-schemas/tckdb_schemas/workflows/computed_species_upload.py:236-238`).
Since the adapter only ever populates one of the two lists — including on the fifth and
sixth routes (`transition_state.calculation`, `.calculations[]`) this item adds, built
from the same call — the union-uniqueness check has nothing to collide against. Not a
prerequisite of anything; worth a one-line comment at the write site if this item touches
that code anyway.

**Unlocks:** 10 open leaves directly, 48 matrix rows across the routed forms.

### ADAPT-3 — Audit the 80 adapter-asserted values · tier 1 exposure · low

The §0.2 triage produced this list rather than a gap list, and the 80 is two of §0.2's
`SOURCE_UNCONFIRMED` buckets combined, not a new count: 45 hardcoded literals, 28
synthesized values, 2 derived (75, §0.2's "adapter-asserted literal / synthesized /
derived" row) plus §0.2's separate 5-row "other derived/computed" bucket — 80 fields the
database receives as fact from the adapter, not from ARC. Most are benign (`quality:
"raw"`, `workflow_tool_release.name: "ARC"`, calculation keys, coordinate indices). These
are not:

* `path_search_result.converged = True`, hardcoded on every emitted path-search calc
  (A4 §4). No ARC convergence flag is consulted.
* `opt_result.converged = True` for `opt_coarse` — the docstring concedes a real field
  "would be slightly more honest".
* **Synthesized imaginary mode** — ARC's `statmech.harmonic_frequencies_cm1` lists only
  real modes, so the adapter *inserts* `{frequency_cm1: -abs(imag_freq_cm1),
  is_imaginary: True}` at position 0 to make `count(is_imaginary) == n_imag` hold. Given
  that `7511b690` now lets TCKDB compute imaginary-mode projections from the stored
  Hessian and report disagreement as a first-class finding (`SCHEMA_DRIFT.md` §3.6), a
  synthesized mode is a claim TCKDB can now publicly dispute.
* **Synthesized IRC TS-marker point** with `reaction_coordinate = 0.0` hardcoded — it is
  ARC's IRC *seed* geometry, not a parsed trajectory point.
* `irc_result.direction` defaulting to `"both"` — with a single unlabelled log the
  payload claims a two-branch IRC.
* `transition_state_upload.reaction.reversible` defaults to `True` (`adapter.py:3057` —
  `"reversible": bool(reversible) if reversible is not None else True`; `:3034` is the tail
  of an unrelated `raise ValueError` and was mis-cited) while the computed-reaction path
  *omits* the same missing value (`:2170-2178` — comment at `:2170` explaining the fields
  is emitted only when the producer set it explicitly, `bundle["reversible"] = bool(reversible)`
  at `:2178`; `:2165` alone lands mid-comment). The two paths disagree, and ARC-2 makes the
  correct value available to both.

**Deliverable:** not a rewrite. A single documented table of every asserted value, its
justification, and whether TCKDB can contradict it — plus fixes for the three that are
falsifiable claims rather than conventions (`converged`, the synthesized mode, `direction`).

### ADAPT-4 — The twelve real adapter gaps · mixed tier · low

The complete list, route-collapsed to twelve:

| TCKDB field (route-collapsed) | ARC source | tier |
|---|---|---|
| `statmech.freq_scale_factor.source_literature` (×2 routes) | `output.yml: freq_scale_factor_source` | 1 |
| `applied_energy_corrections[].scheme.level_of_theory.method` (×4 routes) | `output.yml: arkane_level_of_theory.method` | 1 |
| `…scan_result.constraints[].constraint_kind` (×4 routes) | `output.yml: species[].rotor_scans[].constraints[]` | 2 |
| `transition_state.validation_evidence[].passed` | `output.yml: transition_states[].irc_converged` | 1 |
| `reaction_upload.software_release.name` | `sp_level.software` / `opt_level.software` / `species[].ess_versions` | — |

2 + 4 + 4 + 1 + 1 = twelve rows.

Four notes. (a) `validation_evidence[].passed` from `irc_converged` alone is
`fidelity: derived` and A2 says so — `irc_converged` means the job ran, not that the
endpoints matched. Deposit it only alongside ARC-1a's real verdict, or not at all; a
`passed: true` sourced from "the job finished" is a false claim.
(b) `reaction_upload.software_release` is optional (`TCKDB:…/computed_reaction_upload.py`),
and the datum exists but **the selection rule does not** — a bundle-level field forces the
producer to pick one release when a run used several ESSs (`ROUTE_ADJUDICATION.md` §6,
medium confidence). Decide the rule before emitting.
(c) A1 is missing the four AEC `scheme.{atom_params,bond_params}[]` leaves under three
parents; adding them to `TCKDB_DEMAND.yml` makes 12 false orphans disappear
(`ROUTE_ADJUDICATION.md` §4). Inventory hygiene, not adapter work.
(d) The stated methodology — "each verified to have a non-null `exported.arc_key`" —
holds cleanly for only one of the four `scan_result.constraints[].constraint_kind` routes
today (`species_upload.conformers[].primary_calculation`, per `gap_matrix.yml`); the other
three (`reaction_upload.species[].calculations[]`,
`reaction_upload.species[].conformers[].calculation`,
`species_upload.conformers[].additional_calculations[]`) carry a null `arc_key` on that row
and rest on the same `GENERALISES`-across-routes reasoning used elsewhere (§1.1 of
`ROUTE_ADJUDICATION.md`, applied by analogy), not on an independently-verified sibling. Not
a reason to drop them — the reasoning is sound — but the "each verified" framing overstates
how it was established for three of the twelve.

### ADAPT-5 — Free tier-1 fields the adapter simply omits · tier 1 · trivial

Not gaps in the matrix's sense (nothing supplies them), but the adapter can assert them
correctly and does not:

* `thermo.scientific_origin` / `statmech.scientific_origin` / `kinetics[].scientific_origin`
  — all default to `'computed'` server-side, which happens to be right, so this is
  belt-and-braces against a default changing.
* `reaction_upload.workflow_tool_release` at bundle level — the adapter already builds
  the identical object per calculation (`adapter.py:3305`) and emits
  `analysis_software_release` at bundle level (`adapter.py:2210-2212`).

Do these in the same PR as ADAPT-4.

### ADAPT-6 — Stop broadcasting the opt level onto freq and sp · tier 2 · low

`_resolve_level` (`adapter.py:4211-4231`) falls back `freq_level`/`sp_level` → `opt_level`
when the job-specific level is null (`:4230-4231`), and common ARC runs declare only
`opt_level`. Callers: `adapter.py:3207` (freq), `:3228` (sp). The docstring justifies it
(ARC writes `freq_level: null` when the user declares only `opt_level`) and the
justification is real — but the result is that a freq calculation is *labelled* with a
level of theory that may not be the one it ran at, and TCKDB has no way to tell.

This compounds with A2's structural finding that levels of theory in `output.yml` are
run-level, not calculation-level, so "the adapter has to broadcast the run-level value and
accept that the broadcast is sometimes wrong". ARC-4's per-species `opt_level` narrows it;
it does not close it.

**Options:** omit the level rather than guess (loses a required-ish field), or emit it
with a `parameters_json.tckdb_origin` marker recording that it was inherited. The second
matches what the adapter already does for reused SP energies (`_reused_origin`,
`adapter.py:4020`).

### ADAPT-7 — Consume ARC's atom map · tier 3 · moderate · **depends on ARC-5 + ARC-6**

The reshaping half of §ARC-6: neutral `arc_only.atom_map` block → `ReactionAtomMapIn`.
The adapter is the right place because it can see the TS geometry alongside the map.

Note what has to be *removed* as well as added: `test_golden_corpus.py`'s
`FORBIDDEN_KEYS` check currently **asserts `atom_map` is absent from every built payload**
(A4 §7). That assertion is correct today and becomes wrong here.

**Do not ship this in the same change as the re-pin** (`SCHEMA_DRIFT.md` §5.4): it is a
1-based index surface whose failure mode is silent, and mixing it into a change whose
other fields fail loudly means the loud failures absorb all the review attention.

### ADAPT-8 — Close the three silent reparse data-loss surfaces · tier 2 · low

When ARC is not importable **and** no sidecar exists, three sub-payloads vanish with no
signal at INFO or above (A4 §3): the whole `hessian` block, the whole `irc_result` block
(while the `type=irc` calc node is still emitted with its `depends_on` edge, so
`kinetics.source_calculations(role=irc)` still resolves against a calc with no scientific
content), and — worst — the GSM path **substitutes** a single-point fallback for an
N-point path, which reads downstream as "ARC computed one image", not as "the parser was
unavailable".

The real-deployment risk is lower than A4 assumed: the adapter is pip-installed *into*
ARC's env and imported in-process by `ARC.py`, so `arc.parser` is importable in
production. But `_vendor.py` deliberately does not vendor `kabsch`, and the sidecar is
absent for every pre-1.1 ARC run. The fix is not to restore the data — it is to **stop
substituting silently**: raise or emit at WARNING, and never fabricate `n_points: 1`.

### ADAPT-9 — Re-pin to `tckdb-client` 0.35.0 + `tckdb-schemas` 0.22.0 · **atomic** · see §5

---

## 5. Sequencing and the re-pin

### 5.1 The pins, and what "atomic" means concretely

Six sites, twelve lines — not the four/six an earlier draft counted. Two historical
planning docs carry the identical pins and were missed:

| site | current |
|---|---|
| `tckdb_arc/pyproject.toml:13` | `"tckdb-client>=0.27,<0.28"` — **runtime** dep |
| `tckdb_arc/pyproject.toml:25` | `test = [..., "tckdb-schemas>=0.8,<0.9"]` — **test-only** dep |
| `.github/workflows/ci.yml:30-31` | `git+…@tckdb-client-v0.27.1`, `git+…@tckdb-schemas-v0.8.0` |
| `README.md:36-37` | the same two git refs |
| `docs/tckdb_arc_rehoming_plan.md:142,148,342-343` | the same pin ranges and git refs (4 lines), verbatim from the pyproject/README text above |
| `docs/PHASE_LOG.md:95-96` | `tckdb-client-v0.27.1` / `tckdb-schemas-v0.8.0` (2 lines) |

Not excluded as "historical planning docs don't need updating": both are live enough to
be cited elsewhere in this same document (`docs/PHASE_LOG.md` records the phase history
this plan continues; `docs/tckdb_arc_rehoming_plan.md` is the plan the adapter's current
layout implements) and a reader who greps for the pin string to find every site that
needs to move will find these two whether or not they are "planning" documents. Update
all twelve lines in the same PR as the four functional sites above, or state explicitly
in that PR why the two docs are left stale.

**A borderline seventh candidate, left out by the same rule that included the first
two:** `docs/contract/SCHEMA_DRIFT.md:348-349` quotes the identical two git refs verbatim
in a fenced code block. It is excluded here because it is this document set's own audit
record of what the pins *were* at the time A5 ran, not a live declaration a build reads —
the same status as the rehoming-plan and `PHASE_LOG.md` prose lines describing rather than
declaring the pins, which this document does *not* exclude. The line between "update it"
and "it's a historical record" is genuinely judgement-dependent; six sites / twelve lines
is the count if "lives in `docs/` and states the pin as fact for a reader to act on" is the
inclusion test, which is the test applied above.

`TCKDB:clients/python/pyproject.toml:41-44` at HEAD:

```toml
dependencies = [
    "httpx>=0.27",
    "tckdb-schemas>=0.10.0",
]
```

— because `types.py:7` and `builders/calculation.py:21` now import
`tckdb_schemas.fragments.execution_environment`. Confirmed by reading, not relayed.

**So the re-pin is not just "move two version numbers".** `tckdb-schemas` changes from a
test-only dependency to a **runtime transitive** one, installed into ARC's conda
environment alongside the adapter. That is a real footprint change and it belongs in the
PR description.

**HEAD is untagged.** `git tag` in `TCKDB_v2` returns exactly two relevant tags:
`tckdb-client-v0.27.1` and `tckdb-schemas-v0.8.0`. The working tree declares `0.35.0`
(`clients/python/pyproject.toml:7`) and `0.22.0`
(`schemas/python/tckdb-schemas/pyproject.toml:7`), and neither exists as a tag. A5's
recommendation stands and is the **first action**: ask `TCKDB_v2` to cut
`tckdb-schemas-v0.22.0` and `tckdb-client-v0.35.0`. Pin to a SHA only if refused; never
to a branch — the wire contract acquired six new blocking validators in this range.

**Good news, narrowly scoped:** TCKDB HEAD has moved since A5's snapshot (`09bf0165` →
`34b46559`, three commits) and `git diff 09bf0165..HEAD -- schemas/python/tckdb-schemas/tckdb_schemas/ clients/python/`
is **empty**. A5's drift ledger — the *wire-package* diff — is still current. Re-run that
diff before pinning; if it is still empty, the published-package contract has not moved.

**That is not the whole exposure, and "no re-audit is needed" was too strong.** The same
commit range also touched the **server surface** that ADAPT-1 already flags as unversioned:

```
$ git diff 09bf0165..HEAD -- backend/app/ --stat
 backend/app/chemistry/geometry.py                                 |  31 +-
 backend/app/db/models/geometry.py                                 |  33 +-
 backend/app/db/models/reaction_atom_map.py                        | 109 ++++----
 backend/app/schemas/reads/scientific_calculation.py                |  80 +++++-
 backend/app/scientific_checks/declarations.py                      |  55 +++--
 backend/app/services/reaction_atom_map.py                          |  80 +++---
 backend/app/services/scientific_read/calculations.py               |  32 ++
 backend/app/services/scientific_read/imaginary_mode_projection.py  | 236 ++++++++++++++-
 8 files changed, 541 insertions(+), 115 deletions(-)

$ git diff 09bf0165..HEAD -- backend/tests/api/golden/openapi.json --stat
 backend/tests/api/golden/openapi.json | 161 +++++++++++++++++++++++++++++--
 1 file changed, 160 insertions(+), 1 deletion(-)
```

ADAPT-1 is this document's own finding that 24.3% of the adapter's mappings target
`backend/app/schemas/workflows/` request models with no published-package equivalent —
exactly the tree that moved. Concluding "no re-audit is needed" from a package-only diff
ignores precisely the exposure ADAPT-1 identifies: the wire package holding still, and the
server the adapter actually talks to on 24.3% of its mappings changing underneath it, are
two different facts. **Before pinning, re-diff both:** the wire-package diff above, and
`backend/app/schemas/workflows/`, the upload routes (`backend/app/api/routes/*upload*`,
`bundles.py`), and the golden OpenAPI (`backend/tests/api/golden/openapi.json`). If the
server-surface diff is non-empty against what the adapter's `TransitionStateUploadRequest`
/ `ConformerUploadRequest` test doubles assume, that is a required re-audit, not an optional
one — and it is exactly the exposure ADAPT-1 says should be decided before the re-pin.

### 5.2 What must land *before* the re-pin

1. **ADAPT-0** (the correctness items, C-1…C-7). They are independent of the schema
   version, they are cheap, and every one of them writes wrong science today. Landing
   them first also means the re-pin's test churn does not hide them.
2. **A tag request to `TCKDB_v2`.** Lead time, not work.
3. **Re-run both diffs** (§5.1): the wire-package diff, to confirm A5's ledger still
   holds, **and** the server-surface diff (`backend/app/schemas/workflows/`, the upload
   routes, the golden OpenAPI) against what ADAPT-1's two unversioned endpoints assume.
   The package diff being empty does not stand in for the second one.

### 5.3 What must land *with* the re-pin

The caught breaks — the ones that will actually fire on an ARC payload
(`SCHEMA_DRIFT.md` §5.3, filtered against what the adapter emits):

* **Drop `isotopologue_label`.** Hard reject under `extra="forbid"`. Already handled:
  `adapter.py:3125-3131` documents it as intentionally left null.
* **`vdw_complex` re-declaration.** `freq_n_imag` is now cross-checked against the
  declared kind and blocks. The adapter emits `species_entry_kind: "minimum"`
  unconditionally (`adapter.py:3139`) and now raises rather than deposits a contradiction
  (`adapter.py:4431-4444`). **Landed.** What is *not* handled is the case where ARC's
  species genuinely is a van der Waals complex — the adapter has no way to know, so this
  becomes a loud failure the operator must resolve by hand. Document it.
* **`statmech_treatment` without `torsions`.** Consistency work landed in the working tree.
* **`freq_reaction_coordinate_mode_index` + `freq_imaginary_dispositions` on any TS with
  `n_imag > 1`.** This is the one caught break with no landed fix. ARC has
  `get_index_of_abs_largest_neg_freq` (`ARC:arc/checks/ts.py:409`) computed against live
  `JobAdapter` objects and never stored, **but A3 identifies a cheap escape**: `spc.freqs`
  is in scope at write time, so the index can be recomputed as an argmin over the negative
  entries rather than plumbed — remembering `+1`, since `FrequencyModePayload.mode_index`
  is `ge=1`. For the dispositions, `unassigned` is explicitly a real answer and not a
  placeholder; declaring a guess produces a record TCKDB can now contradict from the
  Hessian (`SCHEMA_DRIFT.md` §3.6). **Declare `unassigned`.**

  **Repair prerequisite.** `GAP_MATRIX.md` still prints `!! 1 near-miss path(s) — see
  GAP_MATRIX.md` at load time, and the one row is this exact field: A3's inventory spells
  it `reaction_upload.transition_state.calculations[].freq_result.reaction_coordinate_mode_index`,
  the demand model's real path is
  `reaction_upload.transition_state.calculations[].freq_reaction_coordinate_mode_index`
  (similarity 0.96, `tools/join_inventories.py` near-miss detector), and the two never
  joined. Fix `ARC_SUPPLY_LATENT.yml`'s path before trusting this bullet or any other
  matrix row keyed through that field — a near-miss reads as `ARC_ABSENT` until repaired,
  which could be silently overstating this exact break.
* **`rejection_codes.py`** — if the adapter starts matching on `TCKDBHTTPError.code`, use
  `rejection_code(exc.code)`, never `RejectionCode(exc.code)`; the server will routinely be
  newer than the pinned client and an unknown code must not raise `ValueError`.
  Also note `.code is None` no longer means "unstructured error": the client now recovers a
  code from a legacy `detail` prefix.

Plus the mechanical bits: bump the two version ranges, the two git refs in CI, the two in
the README, the four lines in `docs/tckdb_arc_rehoming_plan.md` and the two in
`docs/PHASE_LOG.md` (§5.1 — six sites, twelve lines total), and move `tckdb-schemas` out
of the `test` extra into a comment noting it arrives transitively.

### 5.4 What follows, and in what order across the two repos

The dependency direction is **ARC exports → adapter maps**, with one exception (ADAPT-0,
which is adapter-only) and one two-way item (ARC-0 ↔ C-1/C-8, where ARC states a base and
the adapter stops defaulting it).

```
    ADAPT-0 (correctness) ──┐
    tag request ────────────┼──► ADAPT-9 (re-pin, atomic) ──┬──► ADAPT-3 (assertion audit)
    drift re-diff ──────────┘                               ├──► ADAPT-4/5 (12 gaps + free fields)
                                                            ├──► ADAPT-6 (level broadcast)
    ARC-0 (index bases) ────────────────────────────────────┴──► ADAPT-8 (reparse silence)

    ADAPT-0 ──► ADAPT-2 (TS rotor scans; ARC already exports the data)

    ARC-1a (TS evidence, verdicts) ──► ADAPT-4 (validation_evidence[].passed, honestly)
    ARC-1b (IRC partition) ─────────► adapter maps participant mappings
    ARC-2 (degeneracy_convention, reversible) ──► adapter passthrough (near-free)
    ARC-3 (rotors) ─────────────────► adapter maps top_description / treatment
    ARC-4 (T1, per-species level) ──► adapter maps t1_diagnostic; narrows ADAPT-6

    ARC-5 (keys) ──► ARC-6 (neutral atom map) ──► ADAPT-7 (reshape to ReactionAtomMapIn)
    ARC-7 (JobAdapter handoff) ─────► adapter maps parameters[] + execution_environment
                 └─ ship the three τ keys first
    ARC-8 (isotopes) — hard, and blocked on sourcing the species declaration
```

**Two ordering constraints that are not obvious** (an earlier draft's ARC-side entry
here — refusing to default `_scan_constraint_parser_for` to Gaussian before landing
ADAPT-0's C-1 fix — is dropped: §0.5 disproves the harm it was protecting against, both
parsers return `[]` on every real ORCA rotor-scan log tested, so there is no live
index-base risk to sequence against. Demoted to C-10, and nothing in either backlog
depends on it landing in any particular order):

1. **ARC-5 strictly before ARC-6.** ADR 0011 makes naming the geometry mandatory; the map
   cannot be expressed without keys.
2. **ADAPT-7 strictly after the re-pin is green**, and in its own change
   (`SCHEMA_DRIFT.md` §5.4). Then audit the index bases by hand against a golden fixture
   with a real multi-atom reaction. Do not rely on a 200.

**One thing that should be re-sequenced against the tiers, per the policy's explicit
invitation:** ARC-2 is tier 1 and trivial; ARC-7 is tier 2 and hard but worth 216 leaves.
The policy says value tier first, then cost — but it also says "a cheap tier-2 item
routinely ships before an expensive tier-1 one". ARC-7's τ-key slice is the case where the
inverse holds: **three parameter keys, a small fraction of ARC-7's cost, decide whether
every ARC transition state is visible in TCKDB's default read surface.** Pull that slice
forward, ahead of the rest of tier 1.

---

## 6. The open question, answered: **re-deposit, and it duplicates**

`PRIORITY_POLICY.md` left this open: "can a deposited reaction be enriched with an atom
map later, or does it need re-depositing? If enrichment is cheap, tier 3 costs nothing. If
it requires re-deposit, every reaction uploaded in the meantime accrues rework."

**Answer: enrichment is impossible through any exposed route, and re-deposit is not
idempotent — it creates a second, parallel reaction record.** Evidence, all from
`TCKDB_v2`:

**No update route exists.** The entire API has three non-POST mutating routes and none
touches reactions: `backend/app/api/routes/record_reviews.py:86` (PATCH review status),
`backend/app/api/routes/admin.py:59` (PATCH user role),
`backend/app/api/routes/auth.py:298` (DELETE api-key). Every reaction and TS route is
read-only (`backend/app/api/routes/reactions.py:27,61`;
`backend/app/api/routes/transition_states.py:21,52,60`).

**The request schema has no hook for it.** `ComputedReactionUploadRequest`
(`TCKDB:schemas/python/tckdb-schemas/tckdb_schemas/workflows/computed_reaction_upload.py:929`
— the backend path re-imports the same class as a shim, it is not a second definition)
carries `species`, `reactant_keys`, `product_keys`, `transition_state`, `kinetics`,
`atom_map` — no `existing_reaction_entry_id`, no reaction ref. Every bundle is
self-contained. `ContributionBundleV0`
(`TCKDB:backend/app/schemas/workflows/contribution_bundle.py:182` — the class definition;
`backend/app/api/routes/bundles.py:26,47` are the route decorators that consume it, not the
model) has no `atom_map` field at all.

**The workflow mints the reaction fresh.** `backend/app/workflows/computed_reaction.py:404-411`
resolves `chem_reaction` by `stoichiometry_hash` (get-or-create, unique column at
`backend/app/db/models/reaction.py:44-46`) — **that is the only dedup**. Line `:414-418`
constructs `ReactionEntry(...)` with no lookup, and `ReactionEntry`
(`backend/app/db/models/reaction.py:109-143`) carries **no unique constraint whatsoever**.
`TransitionState` / `TransitionStateEntry` are likewise new every call (`:452-469`).

**It is structurally enrichable, and deliberately fenced off.** `reaction_atom_map` is its
own table with an FK to `reaction_entry` (`backend/app/db/models/reaction_atom_map.py:160`)
and to `transition_state_entry` (`:171`) — so a later INSERT is physically possible. It is
closed at two levels. Migration
`backend/alembic/versions/b6c1f4a8e703_freeze_declared_atom_maps.py` brings the table under
the accepted-science immutability regime, and its own docstring says: *"`tckdb_guard_accepted_child`
fires on INSERT as well as UPDATE and DELETE, so a map cannot be added to an already-accepted
TS entry… **ADR 0011 puts retroactive mapping explicitly out of scope**."* And
`docs/adr/0011-atom-mapping-is-declared-not-inferred.md:4` states retroactive mapping is out
of scope, `:61` that "the reactions that matter can be **re-uploaded** with maps".
`backend/app/services/reaction_atom_map.py:815-819` says it plainly: *"no path can attach a
map to a saddle point deposited earlier because every transition-state entry is created fresh
by the deposit that writes it."*

**No backfill exists.** There is no `backend/app/cli` package; `atom_map` returns zero hits
across `backend/scripts/` (including `bulk_load_reactions.py` and `bulk_load_arc.py` — there
is no top-level `scripts/`), `curation.py` and `admin.py`. The only `ReactionAtomMap(...)`
construction sites are `backend/app/services/reaction_atom_map.py:196` and three sites in
one test file, `backend/tests/db/test_atom_map_immutability.py:146,250,326`.

**What a second upload does.** With the same `Idempotency-Key`, `uploads.py:652-653` replays
the stored response and the workflow never runs. Without a key (or with a new one) the
workflow re-executes end to end — `backend/app/api/idempotency.py:109-110` states it
outright: *"with no receipt on file that retry re-executes the upload and duplicates the
science."* Concretely: `chem_reaction`, species, species entries and geometries are **reused**
(hash dedup); `reaction_entry`, participants, `transition_state`, `transition_state_entry`,
calculations, kinetics and the atom map are all **new**. Result: **201 Created, and two
micro-reaction records for one reaction — one mapped, one not, with nothing linking them.**

**Consequence for the plan.** The depositor's tier-3 placement of atom mapping stands as a
*sequencing* decision, but it is not free. It costs:

* **Every reaction deposited before ARC-6 lands is permanently unmapped**, and the sanctioned
  remedy is a full re-deposit that leaves a duplicate behind.
* The advertised "correction path" — a replacement `transition_state_entry` plus a
  supersession edge — is **only half-exposed**. `POST /curation/scientific-record-supersessions`
  exists and is curator/admin-gated (`backend/app/api/routes/curation.py:66-75`), but there is
  **no route that creates a replacement `transition_state_entry` under an existing
  `transition_state`**; the one test of that path (`backend/tests/db/test_atom_map_immutability.py:304-389`)
  performs that step with raw ORM writes at `:322-348`. So the supersession edge is not even the
  same shape as what a public re-deposit produces.

**Recommendation:** keep atom mapping in tier 3, but treat **ARC-5 → ARC-6 → ADAPT-7 as a
gate on bulk reaction deposition**, not on deposition as such. Depositing a handful of
reactions now and re-depositing them later is cheap. Depositing a campaign now is not.
If the depositor intends a bulk load before the map lands, that is a decision worth making
explicitly, with this paragraph in front of them.

**Two qualifications on the mechanism, not the conclusion.**

*The DB-layer immutability guard is not what blocks enrichment.*
`tckdb_record_is_accepted` (`backend/alembic/versions/e2c9a4f7b163_record_accepted_science_repairs.py:318-330`)
tests `first_approved_at IS NOT NULL` against `record_review`, and a fresh deposit is
created `status=under_review`
(`backend/app/services/upload_submission.py:158-162`) — so the guard has not even armed
at deposit time. Enrichment is impossible because **no route or service ever reaches the
insert** (`backend/app/services/reaction_atom_map.py:196` is the only non-test
`ReactionAtomMap(...)` construction site, reachable only from the fresh-bundle workflow),
not because the database forbids the write pre-acceptance. The conclusion is unchanged;
the reason is code absence, not a constraint that would otherwise fire.

*An `Idempotency-Key` does not prevent the duplicate — it makes the failure mode worse.*
A corrected re-deposit carries a different payload than the original. Replayed under the
**same** key, `idempotency.py:195-198` raises `IdempotencyConflict` → 409 with nothing
written (`backend/tests/api/test_api_upload_idempotency.py:161-180` exercises exactly
this). Replayed under a **new** key, the workflow re-executes end to end and writes the
duplicate `reaction_entry` described above. And the stored receipt itself expires:
`IDEMPOTENCY_TTL = timedelta(days=30)` (`idempotency.py:41`), so even a same-key,
same-payload retry stops being a safe no-op after 30 days.

---

## 7. Confidence, and what I could not establish

Carried forward rather than laundered into confidence.

**The single most load-bearing judgement in the whole audit, unchanged.**
`ROUTE_ADJUDICATION.md` §6 flags its own §1.5 ruling — 136 rows (34 leaves × 4 species
calculation routes) of `irc_result` / `path_search_result` on *species conformers* — as
"a judgement about vacuous demand, not a code finding", and "the largest block of rows
resting on judgement rather than evidence". Its author is explicit that if a reviewer
disagrees, the absent count **more than doubles**, from 136 to 272. I have followed the
ruling (§0.3 treats these as non-gaps) because the reasoning is sound — an IRC on a
non-saddle conformer is not an object anyone produces — but **this plan inherits that
sensitivity in full**, and it is the one place where a second reader could move the
headline numbers materially. It does not change any backlog item's rank: nothing in
either backlog targets those routes.

**Settled during Phase C — two of the audit's flagged unknowns are now closed:**

* **TS rotor scans** (`ROUTE_ADJUDICATION.md` §6, medium confidence — "what I did not
  establish is whether ARC *can* run a rotor scan on a TS and simply does not export
  it"). **Settled: ARC runs them and exports them.** §0.0 and ADAPT-2. The item moved
  repos.
* **Whether ARC's `rotor_scans[].result.coordinate` carries `index_base`** (A4's
  `UNVERIFIED`). **Settled: yes** — unconditional literal at `ARC:arc/output.py:1901`,
  `required` at `ARC:arc/schemas/output_yml_schema.json:771`, `const: 1` at `:789-791`.
  The adapter's default is safe on that path (C-2). It is **not** safe for
  `constraints[]`, where the schema legitimately allows 0 (C-1).

**Could not establish:**

1. **Whether the AutoTST / GCN / KinBot / GoFlow TS adapters preserve concatenated-reactant
   atom ordering.** A3 flagged it `UNVERIFIED` and it is load-bearing for ARC-6 —
   an identity reactant→TS map for a geometry produced by an adapter that silently reordered
   atoms is manufactured provenance. `ts_atom_order_is_reactant_order: null` is the correct
   output until it is settled.
2. **Whether every RMG Arrhenius A-unit string is a member of `ArrheniusAUnits`.** A2 flagged
   it; C-4 makes the *consequence* safe (refuse rather than ship unitless) without needing the
   answer, but the enumeration is still worth doing.
3. **RMG's `NASAPolynomial` coefficient order.** A2 mapped `coeffs` positionally to
   `a1..a7` / `b1..b7` without verifying the ordering, and neither side validates it — "a
   reversed or rotated list would be silently accepted". Not in either backlog because it
   is a one-off verification, not a code change; **do it before the next thermo deposit.**
4. **Whether the deployed instance at `tckdb.homecalvin.com` runs this HEAD.** Every schema
   statement here is about the `TCKDB_v2` working tree. A server running an older revision
   enforces fewer of these rules — and, worse for the re-pin, client 0.35.0 unconditionally
   emits `degeneracy_convention` (`SCHEMA_DRIFT.md` §4.6), which a 0.8.0-era server rejects
   under `extra="forbid"`. **Check the deployed revision before the re-pin, not after.**

**Confidence in the numbers.** §0's re-classification is mechanical over `gap_matrix.yml`
and reproducible from it. §1's coverage figures depend on the container/leaf split (§0.4)
and on treating `SOURCE_UNCONFIRMED`-from-`output.yml` as populated (§0.2); both are stated
so they can be disagreed with. §2's items were each re-verified against current source with
current line numbers, in both repos. §6 is the strongest-evidenced section in the document — the
mechanism is stated in TCKDB's own migration docstring and ADR.

**Current state, for the record.** Adapter: **706 tests** collected
(`pytest tckdb_arc/tests --collect-only -q` under `arc_env`). Corrected: a plain
`.venv/bin/python -m pytest tckdb_arc/tests -q` in this checkout does **not** collect
nothing — it collects and runs the same suite (690 passed, 16 skipped, 37 subtests,
690+16=706), matching the `arc_env` figure. The earlier "collects nothing" claim was
misleading; the venv already has `tckdb_client` and `tckdb_schemas` on its path.
`tckdb_arc/tests/test_shared_builder_field_sets.py` exists and is untracked.
Both previously-found defects are fixed in the working tree: `_build_thermo_block` now takes
`calc_keys_by_role` and resolves by role (signature `adapter.py:4913-4918`, docstring
`:4919-4959`, resolution `:5015-5022`), and `_flatten_result_fields` now raises on an
unknown field rather than dropping it (`adapter.py:6023-6034`, per-mode guard at
`:6042-6057`).
