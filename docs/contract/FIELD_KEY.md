# Field key — the shared join vocabulary

Every inventory in `docs/contract/` is keyed by the **canonical field path** defined
here. The gap matrix is produced by joining those inventories on `path`. If two
agents spell the same field differently, the join silently drops the row and the
matrix reports a gap that does not exist. Follow this document exactly.

The authority for path spelling is **TCKDB's Pydantic models at `tckdb-schemas`
0.51.0 (`TCKDB_v2` HEAD `ad3cd706`)**, plus the backend-side
`TransportUploadRequest` for the `transport_upload` root, which is not published
in `tckdb-schemas`. ARC-side and adapter-side inventories
adopt TCKDB's spelling even when their own local name differs — that local name is
recorded in a separate column, never in `path`.

## Path grammar

```
<root>.<field>[.<field>…]
```

**Roots** — exactly five, one per upload workflow:

| Root | Model | Module | Endpoint |
|---|---|---|---|
| `species_upload` | `ComputedSpeciesUploadRequest` | `tckdb_schemas/workflows/computed_species_upload.py` | `POST /api/v1/uploads/computed-species` |
| `reaction_upload` | `ComputedReactionUploadRequest` | `tckdb_schemas/workflows/computed_reaction_upload.py` | `POST /api/v1/uploads/computed-reaction` |
| `ts_upload` | `TransitionStateUploadRequest` | `tckdb_schemas/workflows/transition_state_upload.py` | `POST /api/v1/uploads/transition-states` |
| `conformer_upload` | `ConformerUploadRequest` | `tckdb_schemas/workflows/conformer_upload.py` | `POST /api/v1/uploads/conformers` |
| `transport_upload` | `TransportUploadRequest` | `backend/app/schemas/workflows/transport_upload.py` (backend-side; extends the wire `TransportUploadPayload` in `tckdb_schemas/workflows/transport_upload.py`) | `POST /api/v1/uploads/transport` |

Module paths without a `backend/` prefix are relative to
`schemas/python/tckdb-schemas/`.

**Rules**

1. **Field names verbatim.** Use the Python attribute name exactly as declared on
   the model — `s298_j_mol_k`, not `entropy_298` or `S298`. Never normalise case,
   expand abbreviations, or strip units from the name.
2. **`[]` marks a list of objects; indices are never used.** Write
   `reaction_upload.species[].conformers[].geometry.xyz_text`. Never `species[0]`
   or `species.0`. A list of scalars is a leaf and takes no `[]` —
   `reaction_upload.reactant_keys` is a leaf.
3. **Descend into every nested model** until you reach a scalar, an enum, a list of
   scalars, or a free-form `dict`. Those four are leaves. A `dict | None` field such
   as `parameters_json` is one leaf row; do not invent paths for its contents.
4. **Optionality is not part of the path.** `foo: Bar | None` is `…foo`, and its
   nullability is recorded in the `requiredness` column.
5. **A model reached by two routes gets two paths.** `CalculationInBundle` appears
   under both `conformers[].primary_calculation` and
   `conformers[].additional_calculations[]`; enumerate both. Duplication is correct
   — the two routes can differ in whether ARC supplies them.
6. **Discriminated unions** take the discriminator value as a path segment in
   braces: `…result{opt}.…`. Only use this where the model genuinely dispatches on
   a type field.
7. **ARC-side surplus** — something ARC exports that has no TCKDB home at all — is
   keyed `arc_only.<arc's own dotted path>`. These rows never join against A1; they
   surface in the matrix as candidate demand for TCKDB to grow.

### Worked examples

```
reaction_upload.atom_map.reactants[].participant_index
reaction_upload.species[].conformers[].calculation.freq_result.hessian
reaction_upload.transition_state.conformers[].geometry.xyz_text
reaction_upload.reactant_keys
species_upload.conformers[].primary_calculation.level_of_theory
species_upload.thermo.nasa.coefficients_low
species_upload.statmech.torsions[].coordinates[].atom_indices
arc_only.output_yml.ts_guess_methods
```

## Row schema

Each inventory is a YAML file whose top level is a list of rows. **Every row carries
`path` and `source`.** `source` is `<repo-relative-file>:<line>` — a claim without a
file:line citation is not admissible, and the Phase B join will drop it.

Repo-relative means relative to that agent's own repo root, and each file states its
repo in a leading `# repo:` comment.

### A1 — TCKDB demand (`TCKDB_DEMAND.yml`)

```yaml
- path: reaction_upload.atom_map
  type: "ReactionAtomMapIn | None"
  requiredness: optional        # required | optional | conditional
  condition: "requires transition_state; both legs run toward the saddle point"
  units: null                   # physical unit, or null
  enum_values: null             # list of permitted values, or null
  semantics: >-
    Atom correspondence across the reaction. TCKDB never derives this
    (ADR 0011) — an algorithmic map would manufacture provenance.
  on_absence: "upload warning 'reaction_atom_map_absent'; upload still succeeds"
  adr_ref: "ADR 0011"
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/computed_reaction_upload.py:963
```

`requiredness` is `conditional` whenever a validator — not the type annotation —
decides it. Put the trigger in `condition`. `on_absence` records what TCKDB actually
does when the field is missing (reject / warn / silently accept); this is what makes
the eventual priority ranking defensible rather than a guess.

Backend checks that go beyond the annotation (ownership, role/type, level-of-theory,
enthalpy declaration, composition, idempotency, deposit rights) are extra A1 rows
with `row_kind: workflow_check`, keyed on the path they constrain and carrying
`check`, `code`, `tier` (`block | warn | silent_default | idempotency | other`),
`http_status` and `on_violation`. They follow all field rows, so a path's field row
is always its first A1 row.

### A2 — ARC exported supply (`ARC_SUPPLY_EXPORTED.yml`)

```yaml
- path: reaction_upload.species[].conformers[].geometry.xyz_text
  arc_key: "output.yml: species[].conformers[].xyz"
  availability: always          # always | conditional | rare
  condition: null               # what gates it, when not `always`
  fidelity: exact               # exact | lossy | derived
  fidelity_note: null           # what is lost or how it is derived
  fixture_evidence: arc/testing/tckdb_evidence/golden/output.yml:41
  source: arc/output.py:212
```

Ground truth is the **golden fixtures**, not docstrings. If a field is described in
a docstring but absent from the golden `output.yml` / `tckdb_evidence.json`, record
`availability: conditional` and say so in `condition` — do not record it as `always`.
Cite the fixture line in `fixture_evidence`; use `null` only when genuinely no
fixture covers it, which is itself a finding.

### A3 — ARC latent supply (`ARC_SUPPLY_LATENT.yml`)

Only fields ARC computes or can obtain but **does not currently export**.

```yaml
- path: reaction_upload.atom_map.reactants[].atom_indices
  arc_location: "arc/mapping/engine.py:map_reaction"
  in_memory_form: "list[int], 0-based, reactant→TS ordering"
  computed_when: "always for reactions with a located TS"
  reachable_from_export: true   # is it live where output.yml is written?
  export_effort: moderate       # trivial | moderate | hard
  effort_rationale: >-
    Value exists on the Reaction object at write time; needs a 0-based →
    1-based index shift and a schema addition to output.yml.
  blocker: null
  source: arc/mapping/engine.py:88
```

`reachable_from_export` is the field that separates a one-line change from a
plumbing project: if the value is not live at the point `output.yml` is written, say
so and describe the plumbing in `blocker`. `export_effort` must be justified in
`effort_rationale` — a bare label is not actionable.

### A4 — adapter mapping (`ADAPTER_MAPPING.yml`)

```yaml
- path: reaction_upload.species[].conformers[].geometry.xyz_text
  adapter_source: tckdb_arc/tckdb_arc/adapter.py:1204
  maps_from: "output.yml: species[].conformers[].xyz"
  transform: "xyz dict → xyz_text via _vendor.xyz_to_str"
  status_at_0_51: unknown     # leave `unknown`; A5's drift ledger decides
  source: tckdb_arc/tckdb_arc/adapter.py:1204
```

A4 reports **what the adapter does**, not whether it still works. Judging validity
needs A5's drift ledger, so `status_at_0_51` stays `unknown` here and Phase B
fills it. Resist the urge to guess.

### A5 — schema drift (`SCHEMA_DRIFT.yml`)

`git diff 3f929069..ad3cd706` (tckdb-schemas 0.22.0 → 0.51.0) over
`schemas/python/tckdb-schemas/tckdb_schemas/`, plus the backend workflow seams
that refuse or warn (`backend/app/workflows/`, `backend/app/services/`). The previous
ledger covered `tckdb-schemas-v0.8.0..0.22.0`.

```yaml
- path: reaction_upload.atom_map
  change: added                 # added | removed | renamed | retyped | requiredness_changed | semantics_changed
  from: null
  to: "ReactionAtomMapIn | None"
  breaking_for_producer: false  # does an 0.8.0-era payload now fail to validate?
  commit: 3f929069
  source: schemas/python/tckdb-schemas/tckdb_schemas/workflows/computed_reaction_upload.py:963
```

`breaking_for_producer` is the load-bearing column, and it is narrower than "did
this change": it is true only when a payload the adapter builds **today** would now
be rejected or silently mean something different. A new optional field is `added`
but not breaking. A renamed field, a widened requiredness, or a changed unit is.

## Conventions

- **Units** — record TCKDB's unit verbatim from the field name or its description
  (`kJ/mol`, `J/(mol*K)`, `K`, `Hz`, `cm^-1`). Where ARC's unit differs, that is a
  `fidelity: derived` row in A2 with the conversion in `fidelity_note`. Unit
  mismatches are a top-priority defect class — never paper over one by normalising.
- **Index bases** — TCKDB atom indices are 1-based unless the field says otherwise;
  ARC is frequently 0-based. Always state the base explicitly in
  `in_memory_form` / `fidelity_note`. An off-by-one in an atom map is silent and
  scientifically fatal.
- **Uncertainty** — if you cannot determine a value, write `UNVERIFIED: <what you
  could not establish>` in that column. Never guess, and never omit the row. A
  flagged unknown is useful; a confident fabrication corrupts the matrix.
- **No prose in YAML rows** beyond the designated free-text columns. Narrative
  belongs in the companion `.md`.

## Deliverables per agent

Each Phase A agent writes two files into `docs/contract/`:

1. `<NAME>.yml` — the rows, conforming to the schema above.
2. `<NAME>.md` — a short narrative: what was covered, what was deliberately excluded
   and why, which areas were hard to establish, and a coverage self-assessment.

The `.md` is where an agent reports doubt about its own completeness. That report
feeds Phase C's confidence ranking, so understating uncertainty there is worse than
a missing row.
