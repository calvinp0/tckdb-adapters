# A5 — Schema drift: `tckdb-schemas` 0.8.0 → HEAD (0.22.0)

Companion narrative to `SCHEMA_DRIFT.yml`.

**Scope.** `git diff tckdb-schemas-v0.8.0..HEAD -- schemas/python/tckdb-schemas/tckdb_schemas/`
in `/home/calvin/code/TCKDB_v2`. 14 files, +3409/−30. Thirteen commits touch the
wire package. `schemas/python/tckdb-schemas/build/` was excluded as instructed;
it does not in fact exist at HEAD, so there were no phantom diffs to filter.

**Headline numbers.** 61 changed fields. 17 are `breaking_for_producer`.
Six of those seventeen break *silently* — see §3.

---

## 1. The arc of the evolution

Read in commit order, the thirteen commits tell one story with four movements.

### Movement 1 — "stop grading provenance you cannot see" (#62, #64, #50)

`9f79826f` / `00546f60` add `ExecutionEnvironmentManifestPayload`: a typed,
secret-scrubbed record of *where a calculation ran*, accepted at two tiers.
The design note is explicit that the `described` tier (`module load gaussian/16`,
no digests) is fully acceptable and is **not** scored by any reproducibility
rubric. This is TCKDB choosing to record what a real HPC uploader can actually
supply rather than refusing a record for lacking bytes nobody has.

`7ad5cb99` adds `degeneracy_convention`. Same instinct: a stored degeneracy is
meaningless unless the record says whether it is already in the rate. The
default is `unknown` rather than `not_applied`, deliberately, "for legacy
producers" — TCKDB will record an honest absence but will not invent a
convention on your behalf.

### Movement 2 — "close the scientific integrity holes" (#66)

`ee7377f5` is the largest single semantic step:

- **`isotopologue_label` is deleted.** A free-text label could mint two species
  identities that no scientific content distinguished. Isotopic resolution now
  derives server-side from atom-resolved SMILES isotope notation (`[2H]CO`),
  and `geometry.isotopes` carries the per-atom mass numbers. "A derived key may
  never be accepted from an uploader" is the stated principle.
- **`TransitionStateValidationEvidenceIn` becomes shared.** Before, only the
  PDep bundle could deposit IRC evidence, so a TS uploaded through the
  computed-reaction path always read back as `validation: {"irc": "absent"}`
  even when the depositor had run the IRC. It is optional on every path; a
  deposit without it warns (`transition_state_missing_irc_evidence`). What is
  refused is *incomplete evidence presented as passing*.
- **`statmech_treatment` must justify itself.** A rotor-aware treatment is
  *defined* by the rotors it treats, so `rrho_1d` with no `torsions` is now
  refused. The docstring names ARC specifically as a producer that legitimately
  omits the treatment field, and declines to make it required.

### Movement 3 — "judge the science, don't count it" (#82, #107)

`ea48d36a` creates `stationary_point.py` and, for the first time, makes
frequency evidence *consequential*. At 0.8.0 the wire package contained **no**
`n_imag` consistency check at all — `freq_n_imag` was recorded and never read.
Now a species entry declared `minimum` whose own frequency evidence reports an
imaginary mode is refused, and a transition state reporting zero imaginary modes
is refused.

`fe2a3ac4` then implements ADR 0012, which is the most interesting decision in
the range. The old gate — `n_imag == 1` for a transition state — was retired
because it is not a gate on science: two correct calculations of the same saddle
point can return `n_imag == 1` and `n_imag == 3` depending on integration grid,
and its cheapest workaround is *deleting a line from the frequency list*. What
replaces it:

- at least one imaginary mode (block),
- exactly one designated `reaction_coordinate_mode_index` when `n_imag > 1` (block),
- no undeclared extra imaginary mode at or above `|ω_RC|` (block),
- extra modes judged against τ, a protocol-dependent noise floor resolved from
  the record's own `calculation_parameter` provenance, producing a warning and
  possibly a **structural flag**.

τ never decides between blocking and warning. It decides between a quiet
warning and a flagged record.

### Movement 4 — "say which atom is which" (#97, #101, #115, #137)

`3dd20db0` adds ADR 0011's `reaction_atom_map`, the single largest new surface
(705 lines). Its three governing choices:

1. **Supplied, never derived.** TCKDB runs no mapping algorithm, because several
   chemically distinct maps are usually consistent with the same reactants and
   products, and choosing one by algorithm "would manufacture provenance."
   `AtomMapSource` has no default member.
2. **Two legs, both toward the TS.** `reactants → TS` and `products → TS`. The
   saddle point is the physical pivot and is what the IRC traverses.
3. **Indices are geometry-relative.** Every participant names the geometry its
   indices count into, and the map names the TS geometry, even when there is
   only one it could be. "The failure mode of implicit indexing is a map that
   looks fine and refers to a different atom order than the depositor intended."

`f54c01b5` adds `MoleculeKind.electron` with the `[e-]` sentinel, so
`OH⁻ + H → H₂O + e⁻` deposits as the balanced reaction it is. `3f929069`
finishes that: both wire surfaces now accept an *empty* atom list, but only from
a participant the reaction declares atomless. Before this, a reaction with an
electron had to skip that participant, which silently switched off the whole
map's completeness rule.

`ec53167e` adds `CodedValidationError` so a refusal carries a machine-readable
code as an attribute rather than as a substring of English prose.

**What TCKDB is clearly trying to achieve:** move every claim that used to be
implicit — which atom, which geometry, which convention, which mode is the
barrier, where it ran — into an explicit, refusable field, while *widening*
rather than narrowing what it will accept, so that honest incompleteness warns
and only self-contradiction blocks (ADR 0008).

---

## 2. The three commits the brief singled out, and what they actually touched

Three of the four named commits **do not touch the wire schema package at all**.
This matters for the re-pin, so it is stated plainly:

| Commit | Subject | Touches `tckdb_schemas/`? |
|---|---|---|
| `5abef25b` | "Freeze the evidence behind an accepted saddle point, and say what its indices count into" | **No.** Backend only: 2 Alembic migrations, `app/db/models/transition_state.py`, read schemas, services, DDL triggers, tests. |
| `7511b690` | "Compute ADR 0012's imaginary-mode projections from the Hessian TCKDB already stores" | **No.** Backend read-path only. |
| `09bf0165` | "Declare a repair to accepted science before making it…" | **No.** Backend/DDL only. |
| `3f929069` | "Let a reaction map the electron it releases" | **Yes** — `reaction_atom_map.py`, `ts_validation_evidence.py`, `enums.py`, `identity.py`. |

### `5abef25b` is not an index-base change on the wire

The brief flagged this as a suspected index-base change. It is not, and it is
worth saying why the title misleads. The migration `f3b7d2c8a419` adds a
**server-side** column binding `transition_state_validation_evidence`'s
participant mappings to the geometry their atom indices count into — it makes
the database record what the wire contract already implied. On the wire,
`TransitionStateValidationEvidenceIn` at HEAD carries exactly six fields
(`kind`, `passed`, `rationale`, `source_calculation_key`,
`reactant_participant_mapping`, `product_participant_mapping`) and **no**
geometry field; the binding is derived by the service from the TS's own
geometry. Indices were 1-based when the fragment was introduced in `ee7377f5`
and are 1-based now. No producer-visible base changed.

The same commit adds `reaction_atom_map_absent` to the standalone
transition-state upload — a route the ARC adapter does not use.

### `7511b690` adds no field

`include=imaginary_mode_projections` is a read-time determination computed from
the stored Hessian. "Nothing is stored: no table, no column, no cache, no
migration." It has **no** upload-side consequence — but it does mean that a
depositor's declared `imaginary_disposition` can now be publicly contradicted by
a determination TCKDB computes from the Hessian. If the adapter starts declaring
dispositions, it will be graded against the matrix.

### `09bf0165` adds no field

Operator-facing DDL. Relevant to the re-pin only as evidence that TCKDB's
accepted-science records are now *immutable except under a declared repair* —
i.e. a bad upload cannot be quietly fixed in place after acceptance.

---

## 3. SILENT-CORRUPTION RISKS — the complete list

These are the changes validation **cannot** catch. A payload built by the
0.8.0-era adapter passes, and means something other than what the adapter
intended. Everything in this section must be resolved by reading, not by
running the uploader and seeing whether it 200s.

### 3.1 — Every new atom-index surface is 1-BASED; ARC is 0-based (CRITICAL)

Four new index surfaces landed in this range. All four are 1-based. All four
accept a 0-based map as *structurally valid* in the common case, and the
resulting record is scientifically wrong.

| Field | Base | Why validation will not save you |
|---|---|---|
| `atom_map.participants[].atom_to_ts` (keys **and** values) | 1-based | The only structural check is `>= 1` and `<= natoms`. A 0-based map shifts every atom by one; if the shifted target happens to be the same element (very common — H's cluster together in an XYZ block), the element-conservation rule passes and the map is stored, wrong. |
| `atom_map.participants[].participant_index` | 1-based | `ge=1`. A 0-based participant index on a two-reactant reaction names participant 0 → rejected; on the *second* reactant it names participant 1, which is a real slot, and the map silently attaches to the wrong molecule. |
| `validation_evidence[].{reactant,product}_participant_mapping` | keys `reactant:N`/`product:N` 1-based, values 1-based TS atom indices | Coverage is checked as a *set* against `1..natoms`. A uniformly 0-based list fails coverage loudly — but a *partially* shifted one, or one where the depositor's own ordering differs, can still cover the set exactly while assigning the wrong atoms to the wrong participants. |
| `geometry.isotopes` keys | 1-based XYZ atom index | Only `>= 1` is checked in the wire package. A 0-based isotope map labels the wrong atom. Element-awareness is deferred to `parse_xyz` server-side, so a D-for-H shift within a molecule with several H's is undetectable. |

Also 1-based, and index into a *different* array:
`freq_reaction_coordinate_mode_index` counts into `freq_frequencies_cm1`
(reaction routes) or matches `freq_result.modes[].mode_index` (species routes).
Off-by-one designates the wrong mode as the barrier — the record is accepted and
the partition function is wrong.

**Verdict:** ARC's mapping engine and geometry handling are 0-based. Nothing in
`tckdb-schemas` will detect a wholesale off-by-one on any of these five
surfaces. This is the single highest-priority defect class in the re-pin.

### 3.2 — `species_entry_kind` acquired teeth (loud, but changes the failure mode)

Not silent, listed for completeness: at 0.8.0 `freq_n_imag` was recorded and
never checked. It is now cross-checked against the declared kind and blocks.
An ARC upload of a van der Waals complex previously declared `minimum` will now
be **refused** unless it is re-declared `vdw_complex`. That is a caught break,
not a silent one — but it will fail uploads that used to succeed.

### 3.3 — `smiles` now silently carries isotopic identity

`isotopologue_label` is gone (breaking, and caught: `SchemaBase` is
`extra="forbid"`, so sending it raises). The **silent** half is the converse:
isotopic substitution is now expressed *only* through SMILES isotope notation,
and it is **atom-resolved** — `[2H]CO` and `[2H]OC` are different molecules. An
adapter that drops or normalises isotope labels out of its SMILES will now merge
two distinct species entries into one, with no error. Conversely, a SMILES that
happens to carry an isotope label where the underlying calculation had none will
fork a species entry that should not exist.

### 3.4 — `degeneracy_convention` defaults to `unknown`, not to the truth

An 0.8.0-era payload sends `degeneracy` and no convention. TCKDB records
`unknown`. That is honest and does not corrupt anything — but any downstream
consumer that has to decide whether to multiply the rate by the degeneracy now
sees "we don't know" for every ARC record. If ARC's degeneracy is in fact
already folded into the fitted rate (the usual Arkane behaviour), that fact is
being thrown away on every upload. The *field* is non-breaking; the *silence*
is a data-quality loss that compounds with every deposit.

### 3.5 — τ is resolved from provenance the adapter probably does not send

`stationary_point.py:146` reads exactly three canonical parameter keys —
`freq.hessian_method`, `grid.quality`, `opt.convergence`. When none are present,
τ resolves to `protocol_not_recorded` = 50 cm⁻¹, deliberately equal to the
finite-difference-from-gradients value. Consequence: a transition state with a
genuine soft extra imaginary mode below the analytic threshold gets a
**structural flag**, and a structurally flagged record is *excluded from default
query results and bulk transition-state exports unless explicitly opted into*.

The upload succeeds. The record is stored. It is then invisible to the default
read surface. Nothing in the response distinguishes "accepted" from "accepted
and hidden" other than the warning payload. This is not a unit or index bug, but
it is exactly the class of thing that only shows up six months later when
somebody asks why the TS corpus is short.

### 3.6 — `imaginary_disposition` is a declaration that can now be contradicted

Adding a disposition is optional and non-breaking. But `7511b690` means TCKDB
now *computes* the projection from the stored Hessian and reports any
disagreement with the declaration as a first-class finding. An adapter that
guesses a disposition to satisfy the blocking rule in §3.1's neighbour rule
(extra mode at or above `|ω_RC|`) will produce a record TCKDB publicly disputes.
Declaring `unassigned` is the honest option and is explicitly treated as a real
answer, not a placeholder.

### 3.7 — No unit changes were found

I searched the full diff for renamed unit suffixes, changed `_units` enums and
changed unit strings. **There are none.** `EnergyUnit`, `ArrheniusAUnits`,
`ActivationEnergyUnits` and `CoordinateUnit` are untouched; the new
`TemperatureUnit` / `PressureUnit` / `EnergyZeroConvention` /
`EnergyCorrectionConvention` enums are additions used by the PDep network
surface, which is not reachable from either of the two workflow roots. Every
existing unit-bearing field (`*_hartree`, `*_cm1`, `*_kj_mol`, `*_k`, `*_bar`)
kept its name, its type and its unit. This is a genuinely clean result and the
one piece of good news in this section.

---

## 4. `tckdb-client` 0.27.1 → 0.35.0

Client changes get no YAML rows (they are not schema fields), but one of them
blocks the re-pin outright.

### 4.1 — The client now depends on the schemas package (BLOCKING)

```diff
 dependencies = [
     "httpx>=0.27",
+    "tckdb-schemas>=0.10.0",
 ]
```
*(`clients/python/pyproject.toml`)*

At 0.27.1 the client had a single dependency, `httpx`, and was schema-agnostic —
"dicts on the wire", as the rehoming plan puts it. At 0.35.0 it imports
`tckdb_schemas.fragments.execution_environment` in `types.py` and
`builders/calculation.py`. **The adapter's current pin set —
`tckdb-client@v0.27.1` + `tckdb-schemas@v0.8.0` — cannot be upgraded to client
0.35.0 without also moving schemas past 0.10.0.** The two pins are no longer
independent. Pip will refuse the combination.

### 4.2 — Transport: retry policy added, opt-in

`TCKDBClient.__init__` gains `retry: RetryPolicy | None = None` (new module
`retry.py`, 222 lines). Default `None` = no retries, so existing behaviour is
unchanged. The policy is idempotency-aware: `IDEMPOTENT_WITH_KEY_METHODS =
{"POST"}` and `method_is_retryable("POST", has_idempotency_key=False)` is
`False` — a POST without an `Idempotency-Key` is never replayed. Additive and
safe.

### 4.3 — Idempotency: unchanged

`clients/python/src/tckdb_client/idempotency.py` does not appear in the diff.
`make_idempotency_key` and `validate_idempotency_key` are byte-identical, and
the header is still `Idempotency-Key`. The adapter's
`tckdb_arc/idempotency.py` wrapper needs no change.

### 4.4 — Submission API: unchanged

`client.upload(...)`, `post_json(...)` and `UPLOAD_ENDPOINTS` keep their
signatures and endpoint map. The adapter's call sites are safe.

### 4.5 — Errors: two additions, one behavioural

- New `TCKDBPaginationError`.
- `TCKDBHTTPError.code` is now also **recovered from a legacy `detail` prefix**
  when the server did not send a `code` field. An adapter matching on `.code`
  being `None` to mean "unstructured error" will now sometimes get a real code.
  Not breaking, but it changes what `.code is None` means.
- New generated module `rejection_codes.py` (`RejectionCode`, `rejection_code()`).
  Its own docstring warns: use `rejection_code(exc.code)`, never
  `RejectionCode(exc.code)`, because a server is routinely newer than the client
  and an unknown code must not raise `ValueError`. **This applies directly to
  the re-pin**: the adapter will be running against a server ahead of whatever
  it pins.

### 4.6 — Builders: one always-on wire change

`Kinetics.to_payload()` now *unconditionally* emits `degeneracy_convention`
(defaulting to `"unknown"`), where at 0.27.1 the key was absent. Against a
0.8.0-era **server** that key would be rejected by `extra="forbid"`. Against
HEAD it is fine. This is a client-forward/server-backward incompatibility: you
cannot run client 0.35.0 against a 0.8.0-era deployment.

`Calculation` gains an `execution_environment` field on the dataclass and on
`Calculation.opt/freq/sp`, validated eagerly through the schemas package. Purely
additive.

### 4.7 — Read surface: large, additive

~25 `search_*` methods, ~20 `iter_*` generators, `pagination.py`,
`scientific_types.py` (901 lines of typed records), `_parity.py`. None of it is
on the upload path.

---

## 5. Re-pin recommendation

### 5.1 — HEAD is untagged. That is the central problem.

There are exactly two tags in this repo's history for these packages:
`tckdb-schemas-v0.8.0` and `tckdb-client-v0.27.1`. HEAD's working tree declares
`0.22.0` and `0.35.0` respectively, and **neither number exists as a tag**.

The adapter installs by git ref:

```
"tckdb-client @ git+…@tckdb-client-v0.27.1#subdirectory=clients/python"
"tckdb-schemas @ git+…@tckdb-schemas-v0.8.0#subdirectory=schemas/python/tckdb-schemas"
```

There is no ref to move these to that is both current and stable. The options
are:

- **Pin to a commit SHA.** Works today, reproducible, but opaque: nothing in the
  pin says "0.22.0", and `tckdb_schemas.__version__` now reads from distribution
  metadata (`__init__.py` was rewritten in this range precisely so the literal
  cannot drift), so an installed-from-SHA package will report `0.22.0` while the
  lockfile says `4f3a…`. Workable, ugly.
- **Pin to a branch.** Unacceptable. The wire contract acquired six new blocking
  validators in this range; a floating pin means an upload path that passes CI
  on Monday fails on Thursday.
- **Ask TCKDB_v2 to cut `tckdb-schemas-v0.22.0` and `tckdb-client-v0.35.0`
  tags.** This is the right answer and it is cheap. **Recommend requesting the
  tags as the first action of the re-pin, before any adapter code changes.**

### 5.2 — The two pins must move together

Because client 0.35.0 requires `tckdb-schemas>=0.10.0` (§4.1), there is no
incremental path. You cannot take the client's retry policy and rejection codes
while staying on schemas 0.8.0, and you cannot take schemas 0.22.0's atom map
while staying on client 0.27.1 without hand-rolling the `degeneracy_convention`
key. **Treat it as a single atomic re-pin.**

### 5.3 — Sequencing

1. **Request tags** `tckdb-schemas-v0.22.0` and `tckdb-client-v0.35.0` from
   TCKDB_v2. Pin to a SHA only if that is refused.
2. **Fix the caught breaks first** — they are cheap and the uploader will tell
   you when you have missed one. Seventeen fields, of which the ones that will
   actually fire on an ARC payload are:
   - drop `isotopologue_label` (2 paths, hard reject under `extra="forbid"`);
   - re-declare van der Waals complexes as `vdw_complex` rather than `minimum`;
   - stop sending a rotor-aware `statmech_treatment` without `torsions`, or
     start sending the torsions;
   - set `freq_reaction_coordinate_mode_index` on any TS with `n_imag > 1`, and
     give the other imaginary modes a `freq_imaginary_dispositions` entry (or
     `unassigned`).
3. **Then audit the index bases** (§3.1) by hand, against a golden fixture, with
   a real multi-atom reaction. Do not rely on the uploader returning 200. This
   step is the one that decides whether the atom map is worth having at all.
4. **Then decide about the new optional surfaces.** `atom_map`,
   `validation_evidence`, `execution_environment`, `geometry.isotopes` and
   `degeneracy_convention` are all optional and all non-breaking. They are new
   *capability*, not new *obligation* — sequence them after the re-pin is green,
   using A2/A3's supply inventories to decide which ARC can actually feed.
5. **Send the three τ parameter keys** (`freq.hessian_method`, `grid.quality`,
   `opt.convergence`) if ARC's parser has them. One-line-ish, and it is the
   difference between a TS record that is queryable by default and one that is
   structurally flagged out of the default read surface (§3.5).

### 5.4 — What not to do

Do not adopt `atom_map` and `validation_evidence` in the same change as the
re-pin. Both are 1-based index surfaces (§3.1) whose failure mode is silent, and
mixing them into a change whose other 15 fields fail loudly means the loud
failures will absorb all the review attention.

---

## 6. Deliberate exclusions and coverage self-assessment

**Confident.** The two workflow roots, the atom map, the TS evidence fragment,
the ADR 0012 surface, identity, geometry and kinetics. These I read in full at
both revisions, and I verified against the 0.8.0 tree: `grep` for `n_imag` in
the 0.8.0 workflow files returns only the "not allowed for calculation type
'scan'" forbidden-field list, and nothing at all in `computed_species_upload.py`
— confirming there was no `n_imag` consistency check in the 0.8.0 wire package
to break.

**Deliberately excluded, with reasons:**

- **`ExecutionEnvironmentManifestPayload`'s ~25 leaves** (`runtime{described|
  container|conda|hpc_module}.*`, `software_release.*`, `executable.*`,
  `closure[].*`). The parent is a new optional field the 0.8.0 adapter never
  sends; enumerating its subtree across six calculation routes would add ~150
  non-breaking rows and no drift information. A1 owns the full inventory. **If
  the adapter decides to populate this field, the subtree needs its own pass** —
  its internal validators are strict (pinned tiers require ≥2 closure entries,
  an exact executable digest match, and an OCI `@sha256:` image reference).
- **`geometry.isotopes` at every `GeometryPayload` route.** `GeometryPayload`
  is reachable from both roots via `input_geometries[]`, `output_geometries[].geometry`,
  `hessian.geometry`, `irc_result.points[].geometry` and
  `path_search_result.points[].geometry`, on six calculation routes. `isotopes`
  was added to the model, so it exists on all of them. I recorded the three
  routes ARC actually populates (conformer geometry ×2 roots, TS geometry).
  The others are the same non-breaking optional addition.
- **`species_entry.charge` / `.multiplicity`.** Both gained a conditional
  validator, but it fires only when `molecule_kind == 'electron'`, which no
  0.8.0-era payload can express. Zero drift for this producer.
- **New enums with no reachable field** — `NetworkKineticsModelKind`,
  `EnergyZeroConvention`, `EnergyCorrectionConvention`, `TemperatureUnit`,
  `PressureUnit`. These serve the PDep network surface, which is not reachable
  from `species_upload` or `reaction_upload`.
- **`coded_error.py` / `software.py` / `__init__.py`.** No fields. The
  `__init__.py` change (version now read from distribution metadata) matters for
  pinning, not for the wire, and is discussed in §5.1.

**Where I am least confident:**

1. **`breaking_for_producer` on `reaction_upload.kinetics[].degeneracy`.** I
   marked it `true` per the brief's rule that a tightened constraint is
   breaking. `allow_inf_nan=False` only fires on a literal `inf`/`nan`
   degeneracy, which an Arkane-sourced value will never be. Treat this row as a
   *conditional* break, not a hard one. It is the only row in the ledger where
   I applied the rule mechanically against my own read of the likelihood.
2. **The two `statmech_treatment` rows.** Whether these fire depends entirely on
   whether the adapter sends a rotor-aware treatment kind. A2/A4 can settle it;
   I marked them breaking because if the adapter does send one without torsions,
   the upload is refused outright.
3. **`freq_imaginary_dispositions` on the TS routes.** Marked breaking because
   a TS with an undeclared extra imaginary mode at or above `|ω_RC|` is refused.
   How often ARC produces such a TS is an ARC question, not a schema question,
   and I could not establish it from this repo.
4. **Whether the adapter's four reaction calculation routes are all live.**
   I enumerated `species[].conformers[].calculation`, `species[].calculations[]`,
   `transition_state.calculation` and `transition_state.calculations[]` because
   the model has all four. Whether the adapter populates all four is A4's to say;
   rows on unused routes will simply not join.

**UNVERIFIED:** whether the deployed TCKDB instance at `tckdb.homecalvin.com`
actually runs this HEAD. Every statement here is about the HEAD of the
`TCKDB_v2` working tree; I did not query the live deployment, and a server
running an older revision would enforce fewer of these rules.
