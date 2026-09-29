# A5-delta — wire-schema drift, `tckdb-schemas` 0.22.0 → 0.23.0 (and 0.24.0)

Re-run of the A5 drift check against TCKDB `main` after the 0.22.0 snapshot the
ARC→TCKDB adapter audit was taken at.

- **Audit snapshot:** `09bf0165` (declares `tckdb-schemas` 0.22.0).
- **`main` at the start of this check:** `b101f755` (0.23.0).
- **`main` at the end of this check:** `14c5e74f` (0.24.0) — it moved mid-check.
  See §0.
- Companion to `SCHEMA_DRIFT.md` / `SCHEMA_DRIFT.yml`; rows below follow
  `FIELD_KEY.md` §A5.

---

## Verdict

**Nothing already built becomes wrong, and the re-pin target moved past the one
in the brief.** The 0.22.0→0.23.0 wire diff is *purely additive*: every model
that existed at 0.22.0 is byte-identical at 0.23.0 (0 changed, 0 removed, 13
added — proven by comparing `model_json_schema()` for every published model in
both versions), and 0.23.0's whole content is the *publication* of two request
contracts that were already live on the routes the adapter posts to, moved out
of the server verbatim. `#149` changed no upload schema at all: it is a read
projection plus a database CHECK, and the τ vocabulary it "closes" is the same
five `TauBasis` members that existed at 0.22.0 — the adapter never writes that
column, the server derives it. `#146` and `#147` touch nothing the adapter can
see. The uncommitted adapter tree's `_freq_result_payload` work — the `is_ts` /
`freq_n_imag` reconciliation, the (75, 10000) cm⁻¹ designation, the
`imaginary_disposition: "unassigned"` declaration — is neither wrong, redundant,
nor newly insufficient; `#149` makes its *consequence* (the ADR-0012 structural
flag) visible on a default read, which is a reason to keep doing exactly what it
does. The real news is `#148` / **0.24.0**, merged to `main` during this check:
it is genuinely BREAKING (`statmech.source_calculations[].calculation_id` →
`calculation_key`), but it breaks only the conformer-nested statmech block,
which this adapter has never built. Both versions were verified by execution:
the uncommitted suite is **715 passed / 16 skipped / 37 subtests** against
0.23.0 *and* against 0.24.0 — identical to its 0.22.0 baseline — and all twelve
payloads the adapter actually builds validate against both.

---

## 0. `main` moved during this check — 0.24.0 is no longer "a branch"

The brief describes four commits in `09bf0165..main` and names
`fix/statmech-upload-keys-not-row-ids` as unmerged 0.24.0 work. Both are stale:

- `09bf0165..main` is **ten** commits, not four. Five predate `#145`
  (`d5d97986` #138, `a7a7b096` #140, `34b46559` #139, `70fd9ff4` #143,
  `b91f69e9` #144); **none of them touches
  `schemas/python/tckdb-schemas/`**, which is why the four-commit framing gave
  the right wire answer anyway.
- `main` advanced from `b101f755` to `14c5e74f` *while this check was running*
  (`git reflog show main` → `14c5e74f main@{0}: merge origin/main: Fast-forward`,
  commit timestamp `2026-08-12 19:57:32 +0300`). `14c5e74f` is
  "Name the calculation, do not guess its row id (#148)" — the
  statmech-upload-keys work — and it bumps
  `schemas/python/tckdb-schemas/pyproject.toml:7` to **0.24.0**.

So the re-pin question is no longer "0.23.0 now or wait for 0.24.0". It is
"0.23.0 or 0.24.0", and both are on `main` today. §7 answers it.

---

## 1. Method

Verified by reading source *and* by execution. Nothing below is taken from a
commit subject.

1. `git diff 09bf0165..<rev>` over
   `schemas/python/tckdb-schemas/tckdb_schemas/`, `clients/python/`,
   `backend/app/schemas/workflows/`, `backend/tests/api/golden/openapi.json`
   (`schemas/.../build/` excluded; it does not exist at any rev in range).
2. Three throwaway installs of the wire package built from
   `git archive` at `09bf0165` / `b101f755` / `14c5e74f`, under
   `/tmp/claude-1000/.../scratchpad/`. **`/home/calvin/code/tckdb-adapters/.venv`
   was not touched**, no TCKDB branch was modified, and the adapter tree is
   byte-unchanged (`git status --short` identical before and after).
3. `model_json_schema(mode="validation")` dumped for **every** `BaseModel`
   subclass reachable by `pkgutil.walk_packages` in each version, then diffed —
   this is what makes "no field changed" a measurement rather than a reading.
4. Every `Enum` in the package and every `TAU_*` / `TS_*` module constant dumped
   and diffed across all three versions.
5. Twelve real payloads captured from the **uncommitted** adapter tree
   (conformer ×3, computed-species ×3, computed-reaction ×3, standalone TS ×3;
   synthetic corpus + the Phase-3 golden fixture), then `model_validate`d
   against 0.23.0 and 0.24.0.
6. The uncommitted adapter test suite run in full against 0.23.0 and 0.24.0.

---

## 2. Per-commit findings

### `da0d5815` — #145, publish the conformer and TS upload contracts → 0.23.0

**Additive-only. Nothing here breaks the adapter.** This is a verbatim move,
and the claim is testable three ways, all of which hold:

- **Source diff is imports-only.** `git diff` of
  `backend/app/schemas/workflows/conformer_upload.py` @ `09bf0165` against
  `schemas/python/tckdb-schemas/tckdb_schemas/workflows/conformer_upload.py` @
  `b101f755` is a *single hunk*, lines 1–36, entirely `from app.…` →
  `from tckdb_schemas.…`. Same for `transition_state_upload.py` (single hunk,
  lines 1–22). Not one field, default, constraint or validator differs.
- **The backend files became re-export shims of the same class objects**
  (`backend/app/schemas/workflows/conformer_upload.py:10-20`,
  `backend/app/schemas/workflows/transition_state_upload.py:10-20`).
- **The generated OpenAPI is unchanged.** Comparing
  `backend/tests/api/golden/openapi.json` at `09bf0165` and `b101f755`:
  `ConformerUploadRequest`, `TransitionStateUploadRequest`,
  `ConformerUploadStatmechPayload`, `ElectronicLevelIn`, `TSReactionUpload`,
  `TSReactionParticipantUpload`, `TransportUploadPayload`,
  `StatmechSourceCalculationCreate`, `StatmechTorsionCreate`,
  `StatmechTorsionCoordinateCreate` are all **identical**, and
  `/api/v1/uploads/conformers` and `/api/v1/uploads/transition-states` are
  identical path objects. Zero paths added, removed or changed.

What is new is reachability, not shape: `tckdb_schemas/workflows/__init__.py`
(new file, 41 lines) re-exports every published request model, and
`tckdb_schemas.statmech_bits` gains the create-side statmech payloads
(`statmech_bits.py:55-135` @ `b101f755`) that `ConformerUploadRequest.statmech`
reaches. `TransportUploadPayload` moved; `TransportUploadRequest` (the
standalone `/uploads/transport` body, which this adapter does not use) stayed
backend-side.

### `1896a855` — #146, late-atom-map spec + CREATE DATABASE encoding

**Nothing on the wire. Nothing here breaks the adapter.** Touches
`backend/docs/`, `docs/deployment/`, ops scripts, and two code files:
`backend/app/api/startup_checks.py` (new `template_encoding()` helper) and
`backend/app/api/routes/health.py:454` (`/status` gains
`"template_encoding": template_encoding(session)` beside `server_encoding`).
Purely additive to an untyped response body; the adapter's readiness probe reads
`/readyz` and only inspects `ready` / `status`
(`tckdb_arc/tckdb_arc/adapter.py:4033-4039`), so an extra key is inert. The
atom-map finding is a *documented API dead end* (no route accepts an existing
`transition_state_id`), not a schema change — it does not alter any payload the
adapter builds.

### `2da1cea4` — #147, restorable backup + a canary that can fail

**Nothing on the wire. Nothing here breaks the adapter.** Two files, both new:
`backend/scripts/ops/tckdb_backup.sh` and
`backend/tests/scripts/test_backup_restores_verifiably.py`. No schema, no route,
no model.

### `b101f755` — #149, `n_imag` does not travel alone + close the τ vocabulary

**Read-side and database-side only. No upload contract changed.** See §4 for the
full treatment against the uncommitted freq work. In summary:

- `schemas/python/tckdb-schemas/` is **untouched by this commit** — the wire
  package's net diff `09bf0165..b101f755` is exactly the eight files `#145`
  wrote.
- `backend/app/schemas/reads/scientific_calculation.py:300-326` adds five fields
  to `CalculationFreqResultSummary` (`reaction_coordinate_mode_index`,
  `imaginary_mode_tau_cm1`, `imaginary_mode_tau_basis`,
  `imaginary_mode_structural_flag`, `n_imag_at_or_above_tau`) and
  `:446-470` adds `ImaginaryModeTauContext` / `TauRankedModeEntry` /
  `TauProtocolParameterEntry`, hung off `ImaginaryModeProjectionSummary.tau_context`.
  Confirmed additive in the golden OpenAPI: 3 schemas added, 2 changed, and both
  changed ones gained properties only, with `required` unchanged.
- `backend/app/db/models/common.py:112-118` introduces
  `IMAGINARY_MODE_TAU_BASIS_VALUES` and
  `backend/app/db/models/calculation.py:555-562` a
  `CheckConstraint(... name="imaginary_mode_tau_basis_known")`. **The adapter
  cannot violate it**: it never sends a τ basis, and the column's sole writer is
  `backend/app/services/calculation_resolution.py:726`
  (`imaginary_mode_tau_basis=tau.basis.value`), which by construction emits one
  of the five members.

### `14c5e74f` — #148, name the calculation, do not guess its row id → 0.24.0

**Genuinely BREAKING for the wire package, and inert for this adapter.**
`ConformerUploadRequest.statmech.source_calculations[].calculation_id: int` →
`calculation_key: str`, and `statmech.torsions[].source_scan_calculation_id:
int | None` → `source_scan_calculation_key: str | None`
(`tckdb_schemas/statmech_bits.py:88,120` @ `14c5e74f`; changelog at
`schemas/python/tckdb-schemas/README.md:27-56`). `ConformerUploadRequest`'s
`calculation` / `additional_calculations[]` become `ConformerCalculationIn`
(`conformer_upload.py:173-190`), which is `CalculationWithResultsPayload` plus an
**optional** `key` — so an adapter payload that sets no key is unaffected.

Why it cannot touch this adapter, verified rather than assumed:

- The adapter builds **no conformer statmech block**. All three captured
  `/uploads/conformers` payloads have top-level keys
  `{additional_calculations, calculation, geometry, label, scientific_origin,
  species_entry}` — no `statmech`.
- The **bundle** statmech shape, which the adapter *does* build, is
  field-identical between 0.23.0 and 0.24.0.
  `StatmechSourceCalcInBundle` is now a `TypeAlias` for the shared
  `StatmechSourceCalcIn` with the same two fields (`calculation_key: str`
  `MinLen(1)`, `role`), and `StatmechTorsionInBundle` is unchanged field for
  field, including `source_scan_calculation_key: str | None`.
- All twelve captured payloads validate against 0.24.0, and the full suite is
  green against it.

### The five older in-range commits (#138, #140, #139, #143, #144)

`git diff --name-only 09bf0165..main -- schemas/` returns nothing attributable to
them; their backend changes are in `app/chemistry/geometry.py`,
`app/db/models/geometry.py`, `app/db/models/reaction_atom_map.py`,
`app/scientific_checks/declarations.py`, `app/services/reaction_atom_map.py` and
the read services. Out of the brief's scope and off the upload wire — noted here
only so the ten-commit range is accounted for rather than silently narrowed.

---

## 3. Drift rows (FIELD_KEY §A5)

Row granularity: the 0.23.0 publication event is recorded **per published model
root**, not per field. Recording all 73 fields of the seven newly-published
models as `added` would be false precision — none of them is new *on the wire*;
they were already the live request bodies of two non-deprecated routes at
0.22.0, verified byte-identical in the golden OpenAPI. What changed is which
package versions them. The 0.24.0 rows are per field, because there the fields
themselves changed.

`source` line numbers are **as of the commit in the row's `commit` column**, not
as of `main` — three of the 0.23.0-era classes (`StatmechSourceCalculationCreate`,
`StatmechTorsionCreate`, `StatmechTorsionCoordinateCreate`) no longer exist at
`main`, so a `main`-relative citation for them would point at nothing. This is a
deliberate departure from `SCHEMA_DRIFT.yml`'s "cite the current location"
convention, forced by a range that both adds and removes the same path.

```yaml
# ---------------------------------------------------------------------------
# 0.22.0 -> 0.23.0 (da0d5815). Publication, not mutation.
# `from` is null in the sense that the path did not exist *in tckdb-schemas*;
# every one of these was already the live request body of a published route.
# ---------------------------------------------------------------------------

- path: conformer_upload.<root>
  change: added
  from: "backend-only: app.schemas.workflows.conformer_upload.ConformerUploadRequest"
  to: "tckdb_schemas.workflows.conformer_upload.ConformerUploadRequest (10 fields, unchanged)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/conformer_upload.py:140

- path: conformer_upload.statmech
  change: added
  from: "backend-only: ConformerUploadStatmechPayload"
  to: "ConformerUploadStatmechPayload | None (17 fields, unchanged)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/conformer_upload.py:51

- path: conformer_upload.statmech.electronic_levels[]
  change: added
  from: "backend-only: ElectronicLevelIn"
  to: "ElectronicLevelIn (level_index int ge=1, 1-based; energy_cm1 float ge=0; degeneracy int ge=1)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/conformer_upload.py:38

- path: transition_state_upload.<root>
  change: added
  from: "backend-only: app.schemas.workflows.transition_state_upload.TransitionStateUploadRequest"
  to: "tckdb_schemas.workflows.transition_state_upload.TransitionStateUploadRequest (10 fields, unchanged)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/transition_state_upload.py:105

- path: transition_state_upload.reaction
  change: added
  from: "backend-only: TSReactionUpload"
  to: "TSReactionUpload (5 fields, unchanged)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/transition_state_upload.py:52

- path: transition_state_upload.reaction.reactants[]
  change: added
  from: "backend-only: TSReactionParticipantUpload"
  to: "TSReactionParticipantUpload (species_entry, note)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/transition_state_upload.py:36

- path: conformer_upload.transport
  change: added
  from: "backend-only: TransportUploadPayload"
  to: "TransportUploadPayload | None (10 fields, unchanged)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/transport_upload.py:24

- path: conformer_upload.statmech.source_calculations[]
  change: added
  from: "backend-only: app.schemas.entities.statmech.StatmechSourceCalculationCreate"
  to: "StatmechSourceCalculationCreate (calculation_id int, role StatmechCalculationRole)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/statmech_bits.py:66

- path: conformer_upload.statmech.torsions[]
  change: added
  from: "backend-only: app.schemas.entities.statmech.StatmechTorsionCreate"
  to: "StatmechTorsionCreate (9 fields, unchanged; torsion_index ge=1, 1-based)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/statmech_bits.py:131

- path: conformer_upload.statmech.torsions[].coordinates[]
  change: added
  from: "backend-only: app.schemas.entities.statmech.StatmechTorsionCoordinateCreate"
  to: "StatmechTorsionCoordinateCreate (coordinate_index + atom1..4_index, all ge=1, 1-based)"
  breaking_for_producer: false
  commit: da0d5815
  source: schemas/python/tckdb-schemas/tckdb_schemas/statmech_bits.py:99

# ---------------------------------------------------------------------------
# 0.23.0 -> 0.24.0 (14c5e74f). BREAKING, and unreachable from this adapter.
# ---------------------------------------------------------------------------

- path: conformer_upload.statmech.source_calculations[].calculation_id
  change: renamed
  from: "calculation_id: int (required, database row id)"
  to: "calculation_key: str (required, min_length=1, local key declared in the same request)"
  breaking_for_producer: false
  commit: 14c5e74f
  source: schemas/python/tckdb-schemas/tckdb_schemas/statmech_bits.py:88
  # breaking_for_producer is false for THIS adapter only: it builds no
  # conformer statmech block (verified: no `statmech` key in any captured
  # /uploads/conformers payload). For any producer that did, this is a 422.

- path: conformer_upload.statmech.torsions[].source_scan_calculation_id
  change: renamed
  from: "source_scan_calculation_id: int | None"
  to: "source_scan_calculation_key: str | None (min_length=1)"
  breaking_for_producer: false
  commit: 14c5e74f
  source: schemas/python/tckdb-schemas/tckdb_schemas/statmech_bits.py:120

- path: conformer_upload.calculation.key
  change: added
  from: null
  to: "str | None (optional local key; ConformerCalculationIn extends CalculationWithResultsPayload)"
  breaking_for_producer: false
  commit: 14c5e74f
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/conformer_upload.py:190

- path: conformer_upload.additional_calculations[].key
  change: added
  from: null
  to: "str | None (optional local key)"
  breaking_for_producer: false
  commit: 14c5e74f
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/conformer_upload.py:208

- path: computed_species_upload.conformers[].statmech.source_calculations[]
  change: renamed
  from: "StatmechSourceCalcInBundle (class defined in computed_species_upload)"
  to: "StatmechSourceCalcIn (TypeAlias; calculation_key + role, field-identical)"
  breaking_for_producer: false
  commit: 14c5e74f
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/computed_species_upload.py:410
  # Component rename only. The JSON shape of the bundle path is unchanged --
  # verified field-by-field (calculation_key: str MinLen(1), role) in both
  # versions. Affects generated-client class names, not payloads.

# ---------------------------------------------------------------------------
# Non-drift, recorded so the absence is on the record.
# ---------------------------------------------------------------------------

- path: calc_freq_result.imaginary_mode_tau_basis
  change: semantics_changed
  from: "TEXT, unconstrained; five TauBasis values by convention"
  to: "TEXT + CHECK imaginary_mode_tau_basis_known (NULL or one of the same five)"
  breaking_for_producer: false
  commit: b101f755
  source: backend/app/db/models/calculation.py:555
  # Database-side, and the adapter never writes this column: the server
  # derives it from the payload's own calculation parameters at
  # app/services/calculation_resolution.py:726. TauBasis's five members are
  # byte-identical at 0.22.0, 0.23.0 and 0.24.0.

- path: reads.calc_freq_result.n_imag_at_or_above_tau
  change: added
  from: null
  to: "int | None (derived; count of stored |omega| >= stored tau)"
  breaking_for_producer: false
  commit: b101f755
  source: backend/app/schemas/reads/scientific_calculation.py:326
  # Read projection. The adapter is write-only and reads no calculation
  # projection, so this is informational for a downstream consumer.
```

---

## 4. `#149` vs the uncommitted `n_imag` work

**Nothing in `_freq_result_payload` becomes wrong, redundant, or insufficient.**

### What `#149` actually did

It moved four already-persisted ADR-0012 fields onto the cheap `include=results`
projection and added one derived count
(`backend/app/schemas/reads/scientific_calculation.py:300-326`), and it closed
the τ-basis vocabulary **at the write side, in the database**
(`backend/app/db/models/calculation.py:555-562`, migration
`e2a7c9d4b615_close_the_tau_basis_vocabulary.py`). It changed no upload schema,
no route, no request body. `git diff 09bf0165..b101f755 --
schemas/python/tckdb-schemas/` contains not one line attributable to it.

### "Travel alone" is not a new co-presence requirement on the producer

The requirement it enforces is on **TCKDB's own read projection**: wherever the
API reports `n_imag`, it must also report the tolerance, the basis, the
structural flag and the count above τ. All four were already stored at 0.22.0,
written by the server. It demands no field the adapter omits.

### The τ basis, established exactly

Unchanged from what A5 recorded, and now re-measured:

- `TAU_PARAMETER_KEYS` is still exactly
  `("freq.hessian_method", "grid.quality", "opt.convergence")`
  (`schemas/.../stationary_point.py:146-150`), identical at 0.22.0, 0.23.0 and
  0.24.0.
- `TauBasis` still has exactly five members — `analytic_tight`,
  `analytic_default`, `finite_difference_gradient`, `finite_difference_energy`,
  `protocol_not_recorded` (`stationary_point.py:174-194`) — and the new CHECK
  mirrors them one for one (`backend/app/db/models/common.py:112-118`). Every
  `Enum` and every `TAU_*` / `TS_*` constant in the package is byte-identical
  across all three versions.
- τ is resolved **server-side**, from the payload's own
  `parameters[].canonical_key` / `canonical_value` pairs
  (`fragments/calculation.py:868-880`), and stored at
  `app/services/calculation_resolution.py:724-728`. The adapter emits **no**
  calculation parameter with any of the three canonical keys (`grep` over
  `tckdb_arc/tckdb_arc/`: zero hits), so every ARC freq calculation resolves to
  `protocol_not_recorded` @ **50.0 cm⁻¹** — confirmed by running
  `tau_resolution()` on the captured TS payloads. The adapter can still satisfy
  the τ basis, in the sense that it always produces a valid, honestly-labelled
  one.

### Correction to A5's characterisation of the structural flag

A5 reports that "a defaulted τ (50 cm⁻¹) earns a TS record a structural flag".
That is not what the code does. `structural_flag=True` is set in exactly two
places (`stationary_point.py:791`, `:858`):

1. `transition_state_extra_imaginary_modes_not_assessable` — a reaction
   coordinate was designated but **no frequency list was deposited**.
2. `transition_state_extra_imaginary_mode_above_tau` — an *extra* imaginary mode
   has `|ω| >= τ`.

The basis being `protocol_not_recorded` never flags a record on its own. Two
consequences worth having on the record:

- **The default τ is the permissive end**, not the strict one: 50 cm⁻¹ equals
  the finite-difference-from-gradients row and is *higher* than
  `analytic_default` (30) and `analytic_tight` (15). If the adapter ever starts
  emitting `freq.hessian_method=analytic` provenance, τ drops and **more**
  records get flagged, not fewer. Supplying τ provenance is a truthfulness
  improvement, not a flag-avoidance strategy — do not adopt it expecting the
  latter.
- **`n_imag == 1` — the ordinary ARC TS — is never flagged.** For all three
  captured TS payloads (`n_imag=1`, no designation needed) the stored
  `structural_flag` is `NULL` ("never judged"), with no findings at all.

### The uncommitted work, clause by clause

Measured by replaying `_freq_result_payload`'s own output through
`evaluate_transition_state_frequency` under 0.22.0, 0.23.0 and 0.24.0 —
**identical results in all three**:

| adapter-built case | finding | stored `structural_flag` |
|---|---|---|
| TS, `n_imag=2`, extra at −80 cm⁻¹, designated + `unassigned` | warn `…extra_imaginary_mode_above_tau` | `True` |
| TS, `n_imag=2`, extra at −30 cm⁻¹, designated + `unassigned` | warn `…extra_imaginary_modes_below_tau` | `False` |
| TS, `n_imag=2`, out-of-window artifact at −12000 stiffer than the −1320.5 RC | warn `…extra_imaginary_mode_above_tau` | `True` |
| TS, `n_imag=1` | none | `NULL` |

- **`is_ts` ↔ `freq_n_imag` reconciliation** — still exactly right.
  `W_N_IMAG_CONTRADICTS_MINIMUM` and `W_TS_NO_IMAGINARY_MODE` are unchanged
  blocking findings; refusing the record rather than dropping the disproving
  field remains the only honest option.
- **(75, 10000) cm⁻¹ designation** — still required and still sufficient.
  `W_TS_REACTION_COORDINATE_NOT_DESIGNATED` is unchanged and still blocking for
  `n_imag > 1`.
- **`imaginary_disposition: "unassigned"` on every non-designated imaginary
  mode** — still load-bearing, and its comment is still accurate. It lifts the
  hard `W_TS_REACTION_COORDINATE_AMBIGUOUS` block (which triggers only on
  *undeclared* extras at least as stiff as the RC) without lifting the
  structural flag. Row 3 above is exactly the case the comment describes, and it
  behaves as documented.
- **What `#149` changes about it:** only visibility, and in the adapter's
  favour. Before, an ARC multi-imaginary TS was deposited with
  `structural_flag = true` that a default read never showed; now a consumer
  filtering `n_imag == 1` sees the exclusion signal in the cheap projection. The
  adapter's payload is what makes that flag *correct* rather than absent —
  keeping `unassigned` and the designation is what earns the record an honest
  flag instead of a rejection.
- **One path the adapter provably cannot hit:**
  `…extra_imaginary_modes_not_assessable` (designated RC + no mode list), the
  other flag-setting branch. `_freq_result_payload` either emits a mode list
  consistent with `n_imag` or refuses/omits before a designation exists.

---

## 5. Do the published `#145` contracts match what the adapter targets?

**Yes — identically, and this is measured, not inferred.**

- Field-level: the published `ConformerUploadRequest` /
  `TransitionStateUploadRequest` differ from the backend models at `09bf0165`
  only in their import block (§2). **No field differs. There is no field to
  name.**
- Component-level: the golden OpenAPI components for both request bodies, and
  every model in their closure, are byte-identical at `09bf0165` and
  `b101f755`; neither route's path object changed.
- Payload-level: all three captured `/uploads/conformers` payloads and all three
  captured `/uploads/transition-states` payloads `model_validate` clean against
  the published 0.23.0 models (and against 0.24.0). The 188 mappings targeting
  these two endpoints are unaffected.

**Two side findings worth acting on, neither of them drift:**

1. `tckdb_arc/tests/test_ts_upload.py:52-118` reconstructs
   `TransitionStateUploadRequest` locally "because it is not published". It now
   *is* published, and the reconstruction is **weaker than the real contract** —
   and was already weaker at 0.22.0. Compared field for field against the
   published model, the local mirror is missing:
   - the field `validation_evidence: list[TransitionStateValidationEvidenceIn]`
     (`transition_state_upload.py:137`);
   - the model validators `validate_reaction_coordinate_contract` (**this is the
     ADR-0012 `n_imag` gate**), `validate_validation_evidence`, `normalize_text`;
   - the field validator `normalize_reaction_family` and
     `TSReactionParticipantUpload.normalize_note`.

   So the adapter's TS tests have never exercised the very
   blocking check the uncommitted `_freq_result_payload` work exists to
   pre-empt. The payloads pass it anyway — verified above against the real
   model — but the test proves less than it appears to. Replacing the
   reconstruction with `from tckdb_schemas.workflows import
   TransitionStateUploadRequest` on re-pin is a one-line strengthening.
2. The adapter never sets `validation_evidence` on the standalone TS path (no
   occurrence anywhere in `tckdb_arc/tckdb_arc/`), including for the
   `with_irc=True` fixture. That is a warn-tier
   `transition_state_missing_irc_evidence` on every standalone TS deposit. Not
   drift, and out of scope here — flagged for whoever owns the IRC-evidence gap.

---

## 6. Was A5's ledger right about what remains?

**Yes, and its strongest claim survives.**

- **61 changed fields / 17 breaking across `v0.8.0..0.22.0`** — unchanged; the
  0.22.0 baseline was not retroactively altered by anything in range.
- **No unit changes anywhere** — **still true through 0.24.0.** Measured: every
  model that existed at 0.22.0 is byte-identical at 0.23.0 (0 changed of 205+
  models), and 0.23.0→0.24.0 changes no unit-bearing field. Every unit-suffixed
  property name added in the whole range (`energy_cm1`, `dipole_debye`,
  `sigma_angstrom`, `epsilon_over_k_k`, `polarizability_angstrom3`) belongs to a
  newly *published* model, not a changed one, and none was removed or respelled.
- **No index-base changes.** Per-field bases are intact: `level_index`,
  `coordinate_index`, `atom1..4_index`, `torsion_index`, `mode_index` all carry
  `ge=1` and are documented 1-based
  (`conformer_upload.py:43`, `statmech_bits.py` coordinate/torsion docstrings);
  IRC and path-search `point_index` are untouched by every commit in range.
  The one int→str retype in the range (`calculation_id` → `calculation_key`,
  0.24.0) is an addressing change, not an index-base change, and it is a hard
  422 rather than a silent reinterpretation — the failure mode A5 worries about
  cannot occur.

**No silent-corruption finding.** The highest-severity class A5 named — a unit
or index-base change that passes validation and corrupts quietly — did not
happen.

---

## 7. Re-pin recommendation

**Re-pin to `tckdb-schemas` 0.24.0, not 0.23.0.**

Rationale:

1. 0.24.0 is on `main` today (`14c5e74f`), so pinning 0.23.0 pins something
   already superseded and buys a second re-pin.
2. The adapter is verified unaffected by 0.24.0's breaking change, by execution
   on both axes: 12/12 captured payloads validate, and the uncommitted suite is
   **715 passed / 16 skipped / 37 subtests** — bit-for-bit the same counts as
   its 0.22.0 baseline — against 0.23.0 *and* 0.24.0.
3. 0.24.0 is where `ConformerUploadRequest` and `TransitionStateUploadRequest`
   are pinnable *and* stable: the conformer statmech reshaping is done, so a
   pin at 0.24.0 is less likely to be invalidated than one at 0.23.0.
4. If policy requires pinning only tagged releases, note that **neither 0.23.0
   nor 0.24.0 is tagged** in TCKDB_v2 (`HEAD` was already untagged at the
   original A5 run). This is a version declared in `pyproject.toml`, not a
   git tag — the same caveat A5 recorded.

Two cheap follow-ups to fold into the re-pin, neither blocking:

- Swap `tests/test_ts_upload.py`'s reconstructed request classes for the
  published ones (§5.1). It strictly increases what the suite proves.
- Leave `_freq_result_payload` exactly as it is (§4).

**Deployment note, not an adapter concern:** migration `e2a7c9d4b615` refuses to
run if `calc_freq_result.imaginary_mode_tau_basis` holds a value outside the
five. Whether the live Pi satisfies that was not checked here.

---

## 8. What could not be established

- **Whether the live deployment passes `e2a7c9d4b615`'s pre-flight guard.** No
  production database was queried; the migration's own reasoning argues the
  expected finding is "five tokens or NULL", but that is an argument, not a
  measurement on that host.
- **Whether real ARC runs produce `n_imag > 1` TS records at all.** The golden
  Phase-3 fixture contains none (every captured TS is `n_imag=1`). The
  multi-imaginary designation path — the part of the uncommitted work with the
  most machinery — is exercised only by synthetic records, here and in the
  adapter's own tests. The behaviour is verified; its real-world frequency is
  not.
- **Whether `main` moves again.** It advanced from `b101f755` to `14c5e74f`
  during this check. Every finding above is pinned to a commit id for that
  reason; anything after `14c5e74f` is unexamined.
- **The five older in-range commits' read-side and geometry changes** (#138,
  #140, #139, #143, #144) were confirmed not to touch the wire package, but were
  not otherwise reviewed. If the adapter ever grows a read path, they need their
  own pass.
- **`clients/python/`** is byte-unchanged across `09bf0165..main`
  (`git diff --stat` is empty), so no drift row exists for it. Whether the
  generated client *would* change under 0.24.0's component renames was not
  regenerated or checked.
