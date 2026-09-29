# A3 — ARC latent supply

Repo audited: `/home/calvin/code/ARC.worktrees/feature_arc_result_export_contract`
(branch worktree only; no other ARC checkout was consulted).

Companion data: [`ARC_SUPPLY_LATENT.yml`](ARC_SUPPLY_LATENT.yml) — 75 rows.

| `export_effort` | rows |
|---|---|
| trivial | 50 |
| moderate | 19 |
| hard | 6 |

| `reachable_from_export` | rows |
|---|---|
| true | 67 |
| false | 8 |

## The one fact that determines everything else

`arc/main.py:649` calls:

```python
write_output_yml(
    species_dict=self.scheduler.species_dict,   # {label: ARCSpecies}
    reactions=self.scheduler.rxn_list,          # [ARCReaction]
    output_dict=self.output,                    # {label: {paths, job_types, convergence}}
    ...
)
```

The writer receives the **live object graphs**, not serialised snapshots. Every
attribute of every `ARCSpecies` and every `ARCReaction` — including lazily
computed properties such as `atom_map`, `charge`, `family` and `product_dicts`,
which will simply compute themselves on first access at write time — is in scope
inside `arc/output.py`. This is why 67 of 75 rows are `reachable_from_export:
true` and why 50 of them are `trivial`.

The complement is equally sharp. `scheduler.job_dict` — the `JobAdapter` objects
holding the actual ESS invocation, its final argument set after troubleshooting,
the server, queue, core count, memory allocation and wall time — is **not**
passed. `arc/output.py` reconstructs what it can by re-parsing log files off
disk (`_get_ess_versions`, `_parse_calc_constraints`, `_parse_opt_log`). Every
`false` row in this inventory is a Job-object row, and `arc/output.py:1204`
already names the missing piece explicitly: *"Future producers (a
JobAdapter→species-record handoff carrying the raw `self.fine` / `self.grid` /
`self.args` per job) can grow these dicts without touching the adapter wiring."*

So the uplift plan splits cleanly in two:

1. **Attribute reads in `_spc_to_dict` / `_rxn_to_dict`** — cheap, large, and
   scientifically the most valuable half.
2. **A JobAdapter→record handoff** — one piece of plumbing that unlocks
   execution parameters, execution environment and per-job resources at once.

## The atom-mapping situation, in detail

This is the motivating case and it deserves a precise answer, because the naive
version ("ARC has `arc/mapping/`, so just export it") is wrong in a way that
would corrupt every deposit.

### What ARC actually has

`ARCReaction.atom_map` (`arc/reaction/reaction.py:156`) is a lazy property. On
first access, if every reactant and product has a geometry, it calls
`arc.mapping.driver.map_reaction(rxn, backend='ARC')` and caches the result.
`map_reaction` dispatches three ways: the family-template path (`map_rxn`), the
general/isomerization path (`map_general_rxn`), or a flipped retry
(`flip_map`). The result passes `check_atom_map_and_return`
(`arc/mapping/driver.py:79`), which enforces that it is a permutation of
`0..N-1`.

The value is `list[int]`, **0-based**, and it is a **reactant→product** map:
entry `i` is the running index, in the concatenated *product* atom ordering, of
the atom that is reactant atom `i` in the concatenated *reactant* ordering.

### What TCKDB wants

`ReactionAtomMapIn` (ADR 0011) wants something structurally different: one
`ReactionAtomMapParticipantIn` per participant molecule, each carrying
`atom_to_ts: dict[int, int]` — *participant-local* index → *transition-state*
index, **both 1-based**, both counting into named geometries
(`geometry_key`, `ts_geometry_key`). Neither leg goes to the other side; both
legs run toward the saddle point.

So there are three separate transformations between what ARC holds and what
TCKDB accepts:

1. **Index base.** ARC 0-based, TCKDB 1-based, on both sides of every pair. A
   silent `+1` omission produces a map that validates cleanly and is
   scientifically wrong.
2. **Running index → (participant, local index).** ARC's map is one flat
   permutation over the concatenated reaction; TCKDB partitions it per molecule.
   The split needs per-species atom counts *and* `get_species_count()`, not
   `len(r_species)`, because `remove_dup_species()`
   (`arc/reaction/reaction.py:652`) collapses `2 CH3` to a single `r_species`
   entry while TCKDB requires two participants with distinct
   `participant_index`.
3. **Reactant→product → participant→TS.** This is the real one. ARC's map does
   not mention the TS at all.

### Can the reactant→TS leg be recovered?

Partly, and honestly stating the limit matters more than the affirmative answer.

ARC's own TS adapters build guess geometries **in concatenated-reactant atom
order**. `arc/job/adapters/ts/linear.py:1649` and `:3508` say so in their return
contracts ("XYZ coordinate guesses in reactant atom ordering");
`arc/job/adapters/ts/rits_ts.py:27` documents that RitS requires reactant and
product XYZ in the same ordering and that ARC's `get_reactants_xyz` /
`get_products_xyz` already deliver that via `rxn.atom_map`. A subsequent ESS
optimisation preserves atom order. So for those adapters:

- reactant leg: TS atom `k` **is** reactant running atom `k` (identity, +1);
- product leg: TS atom `k` is product running atom `atom_map[k]` (invert, +1).

That is a complete `atom_to_ts` for both legs from data already in memory.

**But I could not establish that this holds universally.** AutoTST, GCN, KinBot
and GoFlow are separate TS adapters and none of them — nor the scheduler that
accepts their guesses — asserts or re-imposes the reactant ordering. `grep` for
`atom_map` and for ordering language in `arc/job/adapters/ts/autotst_ts.py` and
`gcn_ts.py` returns nothing. This is recorded as `UNVERIFIED` on the
`atom_to_ts` row. Emitting an identity reactant→TS map for a geometry produced
by an adapter that silently reordered atoms would be exactly the
"manufactured provenance" ADR 0011 forbids, dressed as a depositor declaration.

### Recommended shape of the fix

Do **not** have ARC emit `ReactionAtomMapIn` directly in the first pass. Emit,
under `arc_only`:

```yaml
atom_map:
  index_base: 0
  direction: reactant_to_product
  values: [ ... ]
  reactant_atom_counts: { label: n, ... }
  product_atom_counts:  { label: n, ... }
  algorithm: family_template | general | isomerization | flipped
  provenance: derived | user_supplied | restored_from_restart
  ts_atom_order_is_reactant_order: true | null   # null when the adapter cannot vouch
```

Everything except the last two lines is a straight attribute read. `algorithm`
needs `map_reaction` to return which branch fired instead of discarding it
(`arc/mapping/driver.py:57`). `provenance` needs a flag set at the three sites
that assign `_atom_map` (the property, the setter at `:167`, the restart
restore at `:352`) — and TCKDB *requires* `atom_map.note` to name the algorithm
whenever `source='inferred'`, so this is not optional polish. The adapter then
does the reshaping, where it can see the TS geometry alongside the map.

## Which ARC subsystems hold the most unexported value

**1. `arc/checks/` — TS validation.** The largest single blind spot. ARC runs
five independent checks on every saddle point (`E0`, `e_elect`, `IRC`, `freq`,
`NMD`) and stores the verdicts plus an accumulated warning string in
`ARCSpecies.ts_checks` (`arc/species/species.py:2241`). None of it is exported.
`output.yml` records only `irc_converged` — whether the IRC job *ran* — not
whether its endpoints matched the declared reactants and products, which is the
scientifically load-bearing claim and precisely what
`TransitionStateValidationEvidenceIn` exists to carry.

Worse (and better): `check_irc_species_and_rxn` computes the full participant
partition and throws it away. `_perceive_irc_fragments`
(`arc/checks/ts.py:588`) builds `fragment_indices` — one 0-based atom index list
per connected component of the IRC endpoint — then returns only the perceived
`Molecule` objects. `_match_fragments_to_species` (`:644`) then solves the
fragment↔species assignment by backtracking and returns a bare `bool`. Those two
discarded values, joined, **are** TCKDB's
`reactant_participant_mapping` / `product_participant_mapping`. Widening two
return types and stashing the result on the TS species converts a computed and
discarded quantity into passing IRC evidence. Note that the distance-matrix
fallback path does *not* produce a partition, and TCKDB refuses a partial map
presented as passing — so that path must emit evidence with no mappings at all.

**2. `arc/species/species.py` — the `ARCSpecies` attribute surface.** `_spc_to_dict`
reads roughly a dozen attributes off an object that carries several dozen. The
highest-value omission is `spc.t1` (`arc/species/species.py:351`), assigned at
`arc/scheduler.py:2960` from `parse_t1` on the single-point log. The T1
diagnostic is the standard single-reference-quality gate, TCKDB has a
first-class field for it, ARC has parsers for it in seven ESS adapters, and the
value is sitting on the object at write time. It is one dictionary entry.

Behind it: `rxn_zone_atom_indices` (ARC's identification of the reacting core,
0-based), `neg_freqs_trshed` (the spectrum is the survivor of a troubleshooting
loop), `active` (Molpro active space — electrons and orbitals, which
`LevelOfTheoryRef` cannot currently express at all), `transport_data` (dipole
moment in Debye and polarizability in Å³, both genuinely ESS-parsed, currently
surfaced only inside a Lennard-Jones comment string), `mol.to_adjacency_list()`
(the only lossless 2D representation ARC holds for birad singlets and charged
species), `symmetry_number` (the resonance-hybrid total, deliberately different
from the already-exported `external_symmetry`), and `opt_level` (the *per-species*
level, which under `adaptive_levels` differs from the single run-level block
`output.yml` emits — that one is not merely missing, it is currently misreported).

**3. `rotors_dict` — a rich structure read at about 40%.** `_get_torsions`
(`arc/output.py:1412`) reads `scan`, `pivots`, `symmetry`, `type` and
`dimensions`, and `continue`s past every rotor whose `success` is not `True`. It
never reads `top` (1-indexed atoms of the rotating top — the only field saying
*which side* rotates, and TCKDB's `top_description`), `invalidation_reason` (why
a found rotor was excluded from the statmech treatment), `max_e` (ARC's own
stored barrier, which the writer instead recomputes by re-parsing the log at
`:1475`), or `trsh_methods`. A reader of `output.yml` cannot distinguish "no
torsion here" from "torsion found and deliberately rejected because its pivots
sit in the TS reaction zone".

A hazard worth naming: `rotors_dict` stores the same four-atom quartet twice, as
`scan` (1-indexed) and `torsion` (0-indexed), in adjacent keys.
`output.yml` correctly exports only `scan`. Any future export must not emit both
unlabelled.

**4. `arc/family/` — reaction-template facts.** `ReactionFamily` is
process-cached (`arc/family/family.py:135`) and holds `reversible`
(`:159`), `own_reverse`, and `actions` — the RMG recipe's `BREAK_BOND` /
`FORM_BOND` / `GAIN_RADICAL` list. `output.yml` exports the family *label* and
nothing else. TCKDB's `reversible` **defaults to `True`**, so an irreversible
family currently uploads as reversible with no warning: a silent semantic error
fixable by constructing a cached object from a label already in scope.

Similarly, `kinetics[].degeneracy_convention` defaults in TCKDB. ARC's Arkane
input template (`arc/statmech/arkane.py:85`) declares only reactants, products,
transition state and tunneling, so the fitted `A` never has a degeneracy factor
folded in — the correct constant is `not_applied`, and defaulting it wrongly
silently rescales every rate a consumer derives. (ARC does *not* compute
reaction-path degeneracy itself; the only degeneracy handling in the tree is
`arc/scripts/rmg_kinetics.py:98`, a comparison-only helper. That row is recorded
as an explicit negative so the matrix does not read the gap as a plumbing
problem.)

**5. Geometry isotopes.** `xyz_to_str` accepts an `isotope_format` argument
(`arc/species/converter.py:138`) and `arc/output.py:1041` calls it without one.
Every ARC xyz dict carries an `isotopes` tuple — `check_xyz_dict` enforces the
key — so the labelling exists and is silently dropped at serialisation. The
consequence is that an isotopologue and its parent export byte-identical
geometry text, which for TCKDB is an *identity collision*, not a rounding loss.
TCKDB's `isotopes: dict[int, int]` is 1-based-keyed against ARC's positional
0-based tuple.

**6. TS-guess provenance.** `output.yml` deliberately emits only the *chosen*
guess and only four of its fields (`arc/output.py:1251`), with a comment
explaining that `TSGuess.as_dict()` also carries absolute paths and diagnostic
state. That restraint is right about paths and wrong about coverage: the
non-chosen guesses are the entire record of what was tried and rejected, and
`spc.unsuccessful_methods` / `ts_guesses_exhausted` / `chosen_ts_list` are
dropped with them. A consumer cannot distinguish "AutoTST was not tried" from
"AutoTST was tried and failed" — the same distinction the evidence sidecar's
`unavailable` envelope was built to preserve.

## Plumbing obstacles

Three, in descending order of leverage.

**The JobAdapter handoff.** All eight `reachable_from_export: false` rows trace
to it, and it is the gate on `calculation.parameters[]` (per-job final args,
including everything `arc/job/trsh.py` added after a failure — the difference
between "what ARC asked for" and "what actually ran") and on
`calculation.execution_environment`. A per-job record persisted onto the species
or into `output_dict` at completion time would unlock the whole class.

**Missing bundle-local keys.** `output.yml` has no geometry keys and no
calculation keys; calculations are identified by *field name* (`freq_log`,
`sp_log`). ADR 0011 makes naming the geometry an atom map counts into mandatory,
so the atom map cannot be expressed at all until a key convention exists. The
same gap blocks `depends_on[]` — the opt→freq→sp dependency edges are derivable
from the paths dict already in scope, but edges need endpoints to name. This is
a schema decision, not a data-availability problem, and it is on the critical
path for the highest-value row in the inventory.

**Values computed inside checks and discarded.** The IRC participant partition
(above) and the reaction-coordinate mode index
(`get_index_of_abs_largest_neg_freq`, `arc/checks/ts.py:409`) are both computed
against live `JobAdapter` objects and never stored. The mode index has a cheap
escape — `spc.freqs` *is* in scope, so it can be recomputed at write time as an
argmin over the negative entries rather than plumbed (remembering `+1`, since
`FrequencyModePayload.mode_index` is `ge=1`). The IRC partition has no such
escape and needs the return-type widening.

Two smaller ones worth naming: `skip_rotors` is a `process_arc_project`
argument and never reaches `write_output_yml`, which blocks an honest
`statmech.statmech_treatment` / `uses_projected_frequencies` (deriving it from
"are there any successful rotors" conflates *none found* with *deliberately
skipped*). And `delete_check_files()` runs at `arc/main.py:614`, *before*
`write_output_yml`, so for a default run the Gaussian checkpoint an
`artifacts[]` row would embed is already deleted by export time.

## Deliberate exclusions

- **Anything A2 covers.** `arc/output.py` and `arc/tckdb_evidence.py` were read
  in full precisely to exclude their contents. Hessians, IRC paths and GSM
  string-file records are in the evidence sidecar and are not repeated here even
  where the internal form is richer.
- **Level-of-theory sub-fields.** `_level_to_dict` calls `Level.as_dict()`,
  which returns *all* non-`None` attributes, so `dispersion`, `auxiliary_basis`,
  `cabs`, `solvation_method`, `solvent`, `solvation_scheme_level` and `args`
  are already exported at run level. Only the *per-species* level
  (`spc.opt_level`) and the freq-scale-factor `software` are latent.
- **Fields ARC does not have.** `D1` diagnostic: no parser exists. Rotational
  constants / moments of inertia: `grep` for `inertia|rotational_constant`
  across `arc/` returns nothing outside test files, so ARC does not compute
  them. IR intensities, reduced masses and force constants: no parser reads
  them from any ESS adapter. None of these are recorded as ARC supply, because a
  row claiming them would send the uplift plan down a dead end. They belong to
  A1's demand list and should surface in the matrix as genuine absences.

## Coverage self-assessment

**Read closely and with confidence:** `arc/output.py` (all 1946 lines),
`arc/tckdb_evidence.py` (header and envelope contract), `arc/reaction/reaction.py`
(class body, properties, and the bond/mapping methods), `arc/mapping/driver.py`,
`arc/checks/ts.py`, `arc/level.py`, `arc/family/family.py` (`ReactionFamily`
construction and the template predicates), `arc/main.py` around the write site,
`ARCSpecies.__init__` and its class docstring, and the TCKDB models under
`tckdb_schemas/workflows/` and `tckdb_schemas/fragments/`.

**Sampled, not exhausted — I would not claim completeness here:**

- **`arc/mapping/engine.py` (78 kB, ~50 functions).** I read the function
  inventory and `map_reaction`'s call graph but not the bodies of
  `map_two_species`, `fingerprint`, `glue_maps`, `cut_species_based_on_atom_indices`
  or the `xh2`/`xh3` hydrogen-mapping machinery. Intermediate artefacts of value
  may exist there (per-species maps, fingerprints, torsion-based hydrogen
  assignments) that I have not inventoried. `find_all_breaking_bonds` and
  `get_template_product_order` in particular look like they hold structure worth
  a second pass.
- **`arc/job/adapters/ts/linear.py` (3800+ lines).** I read the module docstring
  and grepped for ordering claims. The per-strategy diagnostics this adapter
  produces (weight grids, strategy names, near-attack folding parameters) are
  almost certainly recordable TS-search provenance and are not in this
  inventory.
- **`arc/job/trsh.py` (113 kB).** Not opened. It is where every troubleshooting
  decision is made, and the *sequence* of troubleshooting actions applied to a
  job is provenance a curator would want. I have only the downstream traces
  (`neg_freqs_trshed`, `rotors_dict['trsh_methods']`, `ess_trsh_methods`).
- **`arc/species/conformers.py` (123 kB) and `arc/species/zmat.py` (131 kB).**
  Function inventories only. The conformer pipeline's intermediate scoring and
  the z-matrix construction internals are unexamined.
- **`arc/statmech/arkane.py` (62 kB).** I read the input templates and the
  output-parsing entry points. The Arkane *output* files ARC writes and parses
  may carry more than `E0`, `optical_isomers` and `external_symmetry`
  (`_parse_conformer_statmech` reads only those three) — Arkane's conformer
  block also prints rotational constants and per-mode data that ARC currently
  does not extract. I did not verify what is present in a real Arkane output,
  so I did not record those as ARC supply.
- **`arc/scheduler.py` (4346 lines).** Grepped for the specific attribute
  assignments cited; not read end to end. The scheduler is where most
  `ARCSpecies` attributes get their values and there may be transient state
  worth capturing that I did not find.
- **`arc/parser/adapters/*` per-ESS bodies.** I confirmed which parse methods
  exist on the abstract adapter and which adapters implement them, but did not
  read the implementations. Claims about *which* ESS actually yield T1, dipole
  moment and polarizability rest on method presence, not on verified regexes.

**Specific unknowns flagged in the rows:** whether the AutoTST / GCN / KinBot /
GoFlow TS adapters preserve concatenated-reactant atom ordering (this one is
load-bearing for the atom map and should be settled before anyone implements the
participant→TS emission); and whether the atom-equivalence classes from
`arc/checks/nmd.py:480` can soundly produce TCKDB's `equivalent_map_count`
(I believe not, and the row says so — `null` is better than a wrong `1`).

**Confidence in the two decisive columns.** `reachable_from_export` I hold with
high confidence: the call site is unambiguous and the true/false split follows
mechanically from which objects cross it. `export_effort` is a judgement, and
the place it is most likely wrong is the `trivial` cluster — I rated an
attribute read as trivial when the *change to `arc/output.py`* is trivial, which
understates the cost where the field also needs a schema addition to
`arc/schemas/` and a golden-fixture update. Read `trivial` as "one to three
lines in the writer, plus schema/fixture housekeeping", not "no work".
