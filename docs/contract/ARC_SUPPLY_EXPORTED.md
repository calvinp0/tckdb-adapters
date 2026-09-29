# A2 — ARC exported supply

**Repo:** `ARC.worktrees/feature_arc_result_export_contract` (branch
`feature_arc_result_export_contract`, 9 commits ahead of ARC `main`; those 9
commits *are* the export-contract work).
**Companion:** `ARC_SUPPLY_EXPORTED.yml` — 271 rows.

## Scope

This inventory covers **only what leaves ARC**: the two files written at the end
of a run into `<project>/output/`.

| File | Written by | What it is |
|---|---|---|
| `output.yml` | `arc/output.py:69` `write_output_yml` | The tool-neutral result contract. Declared by `arc/schemas/output_yml_schema.json` (1834 lines, closed via `unevaluatedProperties: false`). |
| `tckdb_evidence.json` | `arc/tckdb_evidence.py:681` `write_tckdb_evidence_atomic` | A versioned parser-evidence sidecar carrying the three bulk scientific artefacts that would bloat the YAML: the Cartesian Hessian, the IRC trajectories, and the GSM path. |

Anything ARC computes but does not write into one of those two files is A3's, not
mine. Where I mention an internal value (atom maps, rotational constants,
execution environment) it is only to record that the export path is closed, and
the row is keyed to the TCKDB field that is therefore unsupplied.

Both files are written atomically. `output.yml` goes through a temp file plus
`os.replace`; the sidecar additionally `fsync`s both the file and the parent
directory entry, so a returned path is durable across a crash. An interrupted run
leaves *no* `output.yml` rather than a partial one — absence is a meaningful
signal to a consumer.

## Shape of the export contract

### How the two files divide responsibility

`output.yml` is the **record**: identity, levels of theory, scalar results, file
paths, thermo, statmech, rotor scans, corrections, kinetics. Everything in it is
either a scalar, a short list, or a small nested dict. It is written *second*.

`tckdb_evidence.json` is the **bulk evidence**: three payloads that are large,
parser-version-sensitive, and meaningless without the geometry frame they were
computed in. It is written *first*, and only if it succeeds does `output.yml`
gain its `tckdb_evidence` descriptor (`arc/output.py:183-197`). If the sidecar
fails, the exception is caught, logged as a warning, and `output.yml` is written
without the descriptor — so `tckdb_evidence` being absent from `output.yml` is
the authoritative signal that no sidecar exists.

The two are joined by `document_id`, a uuid4 hex minted once per run and stamped
into both. A consumer must check it matches before pairing the files; a stale
sidecar from an earlier run in the same directory is otherwise
indistinguishable.

The sidecar's **only** join key to individual records is `label` — the ARC
species/TS label, echoed verbatim. There is no structural key.

### Three design decisions that shape everything else

1. **The species record is flat, on purpose.** ARC does not export a list of
   typed calculations. It exports `opt_log`, `opt_n_steps`,
   `opt_final_energy_hartree`, `freq_log`, `freq_n_imag`, `sp_log`,
   `sp_energy_hartree` … side by side on the species. `arc/output_test.py:1153`
   pins this flatness as intentional contract ("wrapped ones never are"). TCKDB's
   model is the opposite: a bundle of keyed `CalculationInBundle` objects, each
   with its own type, level of theory, software release, result payload,
   geometries, constraints and dependency edges. **Reconstructing calculations
   from ARC's field-name prefixes is the single largest structural transform any
   adapter must perform**, and ARC supplies no calculation keys for it (the only
   key it emits is `rotor_scans[].key`).

2. **Levels of theory are run-level, not calculation-level.** There is one
   `opt_level`, one `freq_level`, one `sp_level` for the entire document. If a
   species was re-run at a different level — which ARC does — that fact is not
   exported. Every calculation in a TCKDB bundle needs its own
   `level_of_theory`, so the adapter has to broadcast the run-level value and
   accept that the broadcast is sometimes wrong.

3. **Envelopes, not omissions, in the sidecar.** Every evidence builder degrades
   to `{"status": "unavailable", "reason": …, "source_paths": […]}` rather than
   raising or omitting the key (`arc/tckdb_evidence.py:124-134`). A consumer can
   always distinguish "not attempted" (key absent) from "attempted and failed,
   for this reason". TCKDB has no envelope concept, so that distinction is
   destroyed on upload — every `unavailable` reason and every
   `omitted_source_paths` list is `arc_only` demand.

### Keying, and why the row count is 271 and not 500

TCKDB mirrors most of its species shape twice: once as
`ComputedSpeciesUploadRequest` and again as `BundleSpeciesIn` inside
`ComputedReactionUploadRequest`. A literal application of FIELD_KEY rule 5 would
double every per-species row. I did not do that. **Per-species leaves are keyed
under `species_upload.*` only; the identical ARC value populates the
`reaction_upload.species[].*` mirror.** Phase B should treat a `species_upload.X`
supply row as covering `reaction_upload.species[].X` as well.

The reaction-specific surface is keyed properly:

- TS leaves → `reaction_upload.transition_state.*` (there is no `species_upload`
  home for a TS at all).
- Reaction identity and kinetics → `reaction_upload.*`.
- Calculation routing: opt → `conformers[].primary_calculation`;
  freq / sp / scan / irc / path_search → `conformers[].additional_calculations[]`
  (species) or `transition_state.calculations[]` (TS). This mirrors how an
  adapter would actually build the bundle.

Five paths carry **two supply rows**. That is deliberate, not a duplication bug —
two genuinely different ARC exports feed the same TCKDB field:

| Path | Supply A | Supply B |
|---|---|---|
| `species_upload.conformers[].geometry.xyz_text` | `species[].xyz` (selected geometry) | `species[].conformers[]` (full screened set) |
| `species_upload.species_entry.multiplicity` | `species[].multiplicity` | `species[].statmech.spin_multiplicity` |
| `species_upload.conformers[].additional_calculations[].artifacts[].filename` | `freq_log` / `sp_log` | `rotor_scans[].source_log` |
| `reaction_upload.transition_state.calculations[].path_search_result.method` | `chosen_ts_method` (output.yml) | `gsm.value.method` (sidecar) |
| `reaction_upload.transition_state.calculations[].irc_result.points[].geometry.xyz_text` | `irc_logs[]` (path only) | sidecar IRC points (actual geometries) |

## Findings

### The unit and index-base defects

These are the rows to read first. All are silent under validation unless
otherwise noted.

**1. Constraint atom indices are software-dependent, and TCKDB has no
`index_base`.** ARC's Gaussian constraint parser emits `index_base: 1`
(`arc/parser/adapters/gaussian.py:1524`); the ORCA parser emits `index_base: 0`
(`arc/parser/adapters/orca.py:747`). Both flow unchanged into
`opt_constraints[]` / `freq_constraints[]` / `sp_constraints[]` /
`rotor_scans[].constraints[]`. TCKDB's `CalculationConstraintPayload` has no
`index_base` field and hard-requires `atom1_index >= 1`. An ORCA-sourced
constraint therefore needs `+1` on every index. An unconverted constraint on
atom 0 is *rejected*; one on atoms 1..N is *silently accepted pointing at the
wrong atoms*. ARC does the right thing here — it states the base as data — and
the information dies at TCKDB's edge.

**2. Rotor-scan sample indices are 0-based; TCKDB's are 1-based.** ARC's
`rotor_scans[].result.samples[].source_index` comes from `enumerate()`
(`arc/output.py:1876`), so it starts at 0. TCKDB's
`CalculationScanPointPayload.point_index` is `Field(ge=1)`
(`fragments/scan.py:85`). Every scan point needs `+1`. The failure is loud at
point 0 and silent thereafter.

**3. IRC point indices are Gaussian's 1-based per-branch numbers, and they
collide.** `records[].irc.value.trajectories[].points[].source_point_index` is
Gaussian's own `Point Number`, which starts at **1** (point 0 is the TS seed and
carries no structure block, so it is never emitted) and **restarts at 1 on the
reverse branch** — the golden fixture shows both trajectories beginning at 1.
TCKDB's `IRCPointPayload.point_index` is documented zero-based, and
`IRCResultPayload.validate_points` **raises on duplicate `point_index` across the
whole result**. Merging ARC's two trajectories into one TCKDB `irc_result` is a
hard validation failure unless the adapter renumbers — and renumbering discards
the per-branch source step number. Worse, ARC's *fallback* IRC path
(`parse_irc_traj`, used when the rich parse yields nothing) uses `enumerate()`,
i.e. 0-based. **The index base is not consistent between ARC's own two IRC code
paths.**

**4. GSM point indices are 0-based and match.** The one clean case. ARC's
`enumerate()` over stringfile frames meets TCKDB's `ge=0` zero-based
`PathSearchPointPayload.point_index`. No conversion. Note this is the *opposite*
base from the IRC record in the same file.

**5. GSM relative energies are kcal/mol; TCKDB's field is kJ/mol.**
`stringfile_relative_energy_kcal_mol` → `points[].relative_energy_kj_mol` needs
`× 4.184`. But the more important fact is that **this value is identically zero
on every real xTB/GSM run** — the ORCA-format writer emits `0.000000` into every
comment line — which the golden fixture confirms
(`{0.0}` for all fifteen points, `arc/tckdb_evidence_test.py:652`). Uploading it
as a relative energy asserts a flat energy profile across a reaction path. The
real energies arrive through the ograd geometry attachment instead, on 3 of 15
points.

**6. Six unit statements live only in ARC's field names and are destroyed by
TCKDB's.** `reaction_coordinate_sqrt_amu_bohr` → `reaction_coordinate`;
`max_gradient_hartree_per_bohr` → `max_gradient`;
`rms_gradient_hartree_per_bohr` → `rms_gradient` (IRC and GSM both);
`cumulative_com_superposed_displacement_angstrom` → `path_coordinate`. In every
case ARC names the unit and TCKDB's field carries none, in its name or its
description.

**7. The IRC and GSM path coordinates are not commensurable, and land in
adjacent unitless fields.** The IRC's `reaction_coordinate_sqrt_amu_bohr` is
Gaussian's mass-weighted coordinate. The GSM's
`cumulative_com_superposed_displacement_angstrom` is a running sum of
centre-of-mass-superposed Frobenius norms in Angstrom — `sqrt(N_atoms)` times the
RMSD, unweighted in the norm but mass-dependent in the superposition frame, so it
*moves under isotopic substitution* and is an upper bound (6% high on the
golden's final point). ARC renamed it from `path_coordinate_angstrom` in
`arc-gsm-stringfile-2` **specifically to stop this conflation**, and TCKDB's
`path_coordinate` reintroduces it.

**8. The run-level correction parameter tables carry no unit at all.**
`atom_energy_corrections` is hartree; `bond_additivity_corrections` is kcal/mol.
Neither key states it. ARC's own docs record that an earlier revision said
kJ/mol for the BAC table and was wrong
(`docs/output_yml_schema.md:258`). The unit is stated as data only per-correction
(`energy_corrections[].total.unit`), which is a different object.

**9. Torsion atom indices are 1-based on both sides — but ARC does not say so.**
`statmech.torsions[].atom_indices` comes from `rotors_dict['scan']`, which is
1-indexed (asserted in a comment at `arc/output.py:1437` and enforced upstream),
and TCKDB requires `>= 1`. No conversion is needed. But unlike `rotor_scans`,
which carries an explicit `index_base: 1` validated as a JSON-schema `const`, the
torsion record carries **no index-base field**. The correctness depends on a
convention a consumer has to know.

### Isotopes are destroyed by every geometry in `output.yml`

`xyz_to_str` is called without `isotope_format` at every one of the six geometry
export sites in `arc/output.py`. ARC's xyz dicts carry an `isotopes` tuple; the
exported text is symbol + coordinates only. TCKDB's `GeometryPayload.isotopes`
therefore has **no ARC supply**, and a deuterated species uploads as its
natural-abundance isotopologue with nothing marking the substitution. The data
exists at write time — `arc/tckdb_evidence.py:446` reads isotopes internally for
the Kabsch superposition — it is simply not serialised.

### Two geometry text formats in one export

`output.yml` emits **header-less atom lines** (no count, no comment).
`tckdb_evidence.json` emits **canonical XYZ** (count line, comment line, atom
lines) — with the comment set to the species label for the Hessian geometry, to
`gsm_point_<i>` for GSM frames, and to the *empty string* for IRC points.
`GeometryPayload.xyz_text` is free text on TCKDB's side, so both are structurally
acceptable, but a consumer parsing one format will fail on the other.

### The Hessian frame is stated, and then lost

`_build_hessian` deliberately reads the geometry from the Hessian's *own* source
rather than the species record's exported `xyz`, because Gaussian prints force
constants in the input orientation while ARC exports the standard orientation — a
pure rotation apart, which silently reconstructs a different spectrum with
invented low-frequency modes and errors of hundreds of wavenumbers, undetectable
by any size or finiteness check. The `frame` field names the frame both the
matrix and the geometry share, and `HESSIAN_PARSER_VERSION` was bumped to
`arc-hessian-2` to mark `arc-hessian-1` records as *not* frame-consistent.

TCKDB's `HessianPayload` has `geometry`, `lower_triangle_hartree_bohr2`,
`source`, `parser_version` and `note` — **but no `frame` field**. The one fact
that makes the pair safe to recombine is discarded on upload. The `packing`
statement (`lower_triangle_row_major_including_diagonal`) goes the same way.

### Parser versions: kept for the Hessian, lost for IRC and GSM

`HessianPayload.parser_version` exists, so `arc-hessian-2` survives.
`IRCResultPayload` and `PathSearchResultPayload` have no such field, so
`arc-irc-path-1` and `arc-gsm-stringfile-3` are dropped. This matters most for
GSM, where ARC documents three *mutually incompatible* record versions
(`arc/tckdb_evidence.py:43-82`): v1 attached per-node energies by id arithmetic,
which on a real 15-frame run puts the **last** frame's energy on the **first**
point; v2 named the cumulative coordinate in a way that invited reading it as a
mass-weighted reaction coordinate. A consumer must gate on the version, and the
gate does not survive the upload.

### `reaction_upload.atom_map` has no ARC supply at all

`output.yml` has no atom-map field of any kind, and the JSON schema has no slot
for one. TCKDB will not derive one (ADR 0011), so every ARC-sourced reaction
uploads with a `reaction_atom_map_absent` warning. ARC computes maps internally —
that is A3's territory — but the export contract does not carry them. This is the
largest structural hole in the reaction surface.

### Where fixtures and docstrings disagree

**The golden `output.yml` is not a golden of the `output.yml` contract.** It is
16 lines long and exists solely as an *input* driver for the evidence builder
(`arc/tckdb_evidence_test.py:749` loads it and feeds it to
`build_tckdb_evidence`). Validated against `arc/schemas/output_yml_schema.json` it
produces **107 errors**, starting with fifteen missing required top-level keys
(`project`, `arkane_git_commit`, `datetime_started`, `datetime_completed`, all
five level fields, `freq_scale_factor`, `freq_scale_factor_source`, `bac_type`,
both correction tables, `reactions`). Nothing in the test suite asserts that the
golden `output.yml` is a valid `output.yml`, and it is not. Anyone treating
`arc/testing/tckdb_evidence/golden/output.yml` as a specimen of the export
contract will build against a fifth of it.

**The `rigid_rotor_kind` 'atom' branch is dead and schema-forbidden.**
`_statmech_to_dict` computes `rotor_kind = 'atom'` for a monoatomic species
(`arc/output.py:1388`), but the only caller gates on `not is_mono`
(`arc/output.py:1292`), so the branch is unreachable — and
`output_yml_schema.json` restricts the enum to `['linear', 'asymmetric_top']`,
with `output_schema_test.py:822` asserting `'atom'` is rejected. The code says one
thing, the schema says another, and the schema is what ships.

**`rigid_rotor_kind` under-reports even where it fires.** TCKDB has five values;
ARC emits two. Every non-linear polyatomic is labelled `asymmetric_top`
regardless of its actual rotor class, so methane (spherical top) and benzene
(symmetric top) are both misreported. This is a docstring-silent lossy path, not
a bug in the fixtures.

**`freq_final_settings` / `sp_final_settings` are pinned to `null` by the
schema.** The docstring at `arc/output.py:1183-1205` explains this as "better than
fabricating defaults", and `output_schema_test.py:827` asserts that a *populated*
`freq_final_settings` is rejected. So a future producer cannot fill them without a
schema bump — the export deliberately refuses to carry grid, SCF convergence,
cycle limits, symmetry or guess for freq and sp jobs.

**`ts_guesses[].chosen` can never be false.** Only the chosen guess is ever
emitted (`arc/output.py:1251-1256`), and the JSON schema pins the field to
`const: true`. A boolean that cannot be false carries no information, and the
rejected guesses it would discriminate against are not exported at all.

**Case normalisation is inconsistent within one TS record.**
`chosen_ts_method` is passed through verbatim — `arc/output_test.py:864` shows
`'xTB-GSM'` reaching `output.yml` with its original casing — while
`ts_guesses[].method` is stripped and lowercased (`arc/output.py:1238`). The same
method can therefore appear twice in one record with two spellings.

### Export paths with no fixture coverage

45 of 271 rows carry `fixture_evidence: null`. They fall into three groups.

*Never exercised, and populated only in configurations no test builds* (11 rows,
the ones that matter):

- `level_dict.auxiliary_basis`, `.cabs`, `.dispersion`, `.year`, `.args` — the
  full rich-level surface. `args` is the concerning one: it is a free-form dict
  that must be flattened into TCKDB's flat `keywords` string, the flattening is
  undefined, and no test ever produces one.
- `rotor_scans[].result.coordinate.requested_step_size` / `requested_start` /
  `requested_end` / (unexported) `step_count` — the entire Gaussian ModRedundant
  grid-metadata path. `parse_scan_args` is only reachable for Gaussian logs, and
  the schema test's `SCAN_LOG` fixture does not populate them. This is a whole
  code branch (`arc/output.py:1810-1873`, ~60 lines including the deliberate
  no-wrap-at-360 decision) with zero fixture coverage of its output.
- `freq_input` / `sp_input` — only `opt_input` gets a deck staged in the schema
  test (`output_schema_test.py:473`).

*Structurally absent by design* (30 rows): TCKDB fields ARC never writes —
`spin_treatment`, `statmech_treatment`, rotational constants,
`uses_projected_frequencies`, `top_description`, all `source_calculations[]`
edges, `wavefunction_diagnostic`, `execution_environment`, `parameters[]`,
`degeneracy`, `reversible`, `atom_map`, `literature`, force fields,
`is_climbing_image`, `is_ts`. `fixture_evidence: null` is correct here because
there is nothing to cover.

*Covered only by unit tests with stubbed parsers* (4 rows): the `unavailable`
envelope's `reason` and `source_paths`, and `omitted_source_paths`. The golden
deliberately contains **no** unavailable envelope —
`arc/tckdb_evidence_test.py:758` guards the golden itself against degenerating
into one, on the grounds that a fixture of failures asserts nothing. Correct
discipline, but it means the failure envelopes have no end-to-end fixture.

Also worth flagging: **NEB produces no evidence record at all.**
`_path_search_method` resolves `orca_neb` → `'neb'` (`arc/tckdb_evidence.py:510`)
and then `build_tckdb_evidence` only ever calls `_build_gsm`, gated on
`== "gsm"`. There is no `_build_neb`. So `neb_log` is exported as a path and the
NEB path itself is never parsed. Every `path_search_result` row in this inventory
is GSM-only in practice.

## Coverage self-assessment

**High confidence (verified against code and a fixture):**
- The full `output.yml` field set. I enumerated it from `arc/output.py`
  assignments and cross-checked against the closed JSON schema's `$defs`; the
  schema's `unevaluatedProperties: false` plus `output_schema_test.py`'s
  round-trip through the *real* writer means I can be confident the field set is
  complete and that nothing is silently emitted outside it.
- The full `tckdb_evidence.json` field set. The golden covers all three evidence
  kinds in their `available` form, and `test_golden_contract` regenerates it from
  real ESS logs with nothing stubbed.
- Every unit and index-base claim. Each was traced to the parser or converter
  that produces the value, not to a docstring.

**Medium confidence:**
- **The TCKDB path spellings.** I read the two workflow roots and every payload
  module they reach, but I did not read A1's inventory. Where TCKDB reaches a
  model by two routes I picked one (see "Keying") and documented the choice. If
  A1 enumerated both routes, roughly 130 `species_upload.*` rows will appear
  unsupplied on the `reaction_upload.species[].*` side. **Phase B must apply the
  routing rule, not join naively.**
- **The calculation routing.** Assigning opt to `primary_calculation` and
  freq/sp/scan to `additional_calculations[]` is how an adapter would sensibly
  build the bundle, but ARC does not say so — it exports a flat record. A4's
  actual adapter may route differently, and the join will show that as a
  disagreement rather than a gap.
- **`arc_only.*` path spellings.** FIELD_KEY says `arc_only.<arc's own dotted
  path>`. ARC's dotted paths contain `[]` list segments that I flattened
  (`arc_only.output_yml.species.statmech.torsions.barrier_kj_mol`, not
  `species[].statmech.torsions[]`). These rows join nothing by construction, so
  the spelling only matters for readability, but it is inconsistent with the
  `[]` convention used on the TCKDB side.

**Low confidence / known gaps in my own work:**
- **The Melius BAC shape.** I read the nested
  `{atom_corr, bond_corr_length, bond_corr_neighbor, mol_corr}` structure from
  the JSON schema `$defs` and from `output_schema_test.py`'s Melius test, not
  from a real Arkane Melius table. The mapping to TCKDB's
  `component_params[]` triples is my reconstruction; `mol_corr` in particular is
  a bare float that needs a synthetic `key` and I could not establish what an
  adapter should call it. **UNVERIFIED: the correct `key` for the Melius
  `mol_corr` component.**
- **NASA coefficient ordering.** I traced `coeffs` to
  `RMG NASAPolynomial.coeffs` (`arc/scripts/save_arkane_thermo.py:31`) and mapped
  positionally to TCKDB's `a1..a7` / `b1..b7`. I did **not** verify that RMG's
  ordering is the standard NASA `[a1, a2, a3, a4, a5, a6, a7]`. Nothing on either
  side validates it, and a reversed or rotated list would be silently accepted.
  **UNVERIFIED: RMG's NASAPolynomial coefficient order.**
- **`ess_versions` banner formats.** I have Gaussian's and ORCA's shapes from the
  test docstring; I could not establish what `parse_ess_version` returns for
  QChem, Molpro, TeraChem or xTB, so the "banner contains the vendor name"
  splitting claim is verified for two ESSs only.
- **The `A_units` / `Ea_units` enum-match risk.** I flagged that ARC emits RMG's
  raw unit string against TCKDB's closed enums. I did not enumerate RMG's actual
  unit spellings to determine whether any of them fall outside the enum.
  **UNVERIFIED: whether every RMG Arrhenius A-unit string is a member of
  `ArrheniusAUnits`.** If one is not, it is a hard upload rejection, and it would
  be the highest-priority defect in the kinetics path.
- **`long_kinetic_description` → `kinetics[].note`.** This is my routing choice;
  the string is a description of the *fit*, and `note` is the only free-text
  landing spot, but TCKDB has several `note` fields and I cannot prove this is
  the intended one.

**What I deliberately excluded:**
- `arc/output_yml_schema.md` (386 lines of human-facing documentation) is quoted
  only where it disagrees with code or states a unit; it is documentation, not
  export.
- Commit `938146b3` ("upload: delegate TCKDB uploads to an optional standalone
  adapter") touches ARC's *upload* path, not what it writes. That is A4's
  territory.
- `arc/scheduler.py`, `arc/species/species.py` and `arc/statmech/arkane.py`
  changes in these 9 commits are producers *feeding* the export; I cited them
  only where a unit or value origin needed establishing.
