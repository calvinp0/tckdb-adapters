# Priority policy — how the uplift backlog gets ranked

Phase C turns the gap matrix into two ranked backlogs (ARC-side PRs, adapter-side
PRs). Ranking needs a value function. Cost is derivable from the audit — A3 reports
`export_effort`, A5 reports `breaking_for_producer`. **Scientific value is not
derivable from either repo.** It is a judgement about what TCKDB is for and who
queries it, and it has to be stated here rather than invented by an agent.

## Cost side — derived from the audit, no input needed

| Verdict | Typical cost | Why |
|---|---|---|
| `BROKEN` | forced | The adapter cannot re-pin to 0.22.0 without fixing these. Not optional, not ranked — prerequisite work. |
| `ADAPTER_GAP` | low | ARC already exports it; the adapter just doesn't map it. Usually a small patch in one repo. |
| `ARC_LATENT` + `export_effort: trivial` | low | Value is live where `output.yml` is written; add a field. |
| `ARC_LATENT` + `export_effort: moderate` | medium | Needs a schema addition and a conversion (units, index base). |
| `ARC_LATENT` + `export_effort: hard` | high | Needs plumbing — the value isn't reachable from the writer. |
| `ARC_ABSENT` | out of scope | ARC would have to compute something new. Report, don't rank. |

## Value side — set by Calvin, 2026-08-11

Consumer ordering, in the depositor's words: **kinetics lookup first, then
reproducibility, then atom mapping** — with the explicit caveat that all three matter
and none is dispensable.

Read the tiers accordingly. They are a **sequencing** decision, not a statement that a
lower tier is scientifically lesser. Everything in the matrix is intended to land
eventually; the tier decides what lands first. In practice the tier acts as a
tiebreaker against cost, so a cheap tier-2 item routinely ships before an expensive
tier-1 one.

```yaml
# Scientific value tiers. Phase C ranks by (tier, then ascending cost).

tier_1_kinetics_lookup:
  # Everything needed to answer "what is the rate of this reaction, and how good is
  # that number". The primary near-term query workload.
  - reaction_upload.kinetics.*                    # fitted rate coefficients, tunneling
  - reaction_upload.transition_state.*            # the saddle point the barrier comes from
  - reaction_upload.reactant_keys
  - reaction_upload.product_keys
  - reaction_upload.reversible
  - reaction_upload.reaction_family
  - species_upload.thermo.*                       # reverse rates and equilibrium
  - species_upload.statmech.*                     # partition functions, torsions, symmetry
  - "**.sp_result.*"                              # single-point energies → barrier heights
  - "**.freq_result.*"                            # frequencies; imaginary mode → tunneling
  - "**.hessian"
  - "**.applied_energy_corrections.*"             # the energies are wrong without these
  - species_upload.species_entry.*                # identity; a rate is useless unattributable

tier_2_reproducibility:
  # Everything needed to re-run the calculation, or to judge whether to trust it.
  - "**.level_of_theory"
  - "**.software_release"
  - reaction_upload.analysis_software_release
  - "**.workflow_tool_release"
  - "**.execution_environment.*"
  - "**.artifacts.*"                              # raw ESS logs
  - "**.parameters_json"
  - "**.parameters.*"
  - "**.input_geometries.*"
  - "**.output_geometries.*"
  - "**.constraints.*"
  - "**.depends_on.*"                             # which calc fed which
  - "**.wavefunction_diagnostic.*"                # T1/D1 — is the method even valid here
  - "**.spin_diagnostic.*"                        # spin contamination
  - species_upload.statmech.freq_scale_factor
  - "**.literature.*"

tier_3_atom_mapping:
  # Uniquely un-backfillable by TCKDB (ADR 0011), but placed third per the depositor's
  # ordering. See the backfill note below.
  - reaction_upload.atom_map.*
  - "**.irc_result.*"                             # connects the saddle to its endpoints
  - "**.path_search_result.*"
  - "**.scan_result.*"

tier_4_completeness:
  # Record if free; skip if it costs anything.
  - "**.note"
  - "**.label"
  - "**.*_source_note"
```

### 0.51 path-shape amendment (Phase B, 2026-09-29; revised after review)

tckdb-schemas 0.51 made three changes the globs above do not follow:

- it added three upload roots (`ts_upload`, `conformer_upload`, `transport_upload`);
- it turned several leaves into containers (`hessian`, `level_of_theory`,
  `software_release` and `workflow_tool_release` now descend to their fields);
- it flattened the reaction bundle's result blocks into scalars (`freq_n_imag`, ...).

Under the globs above that left 826 demand rows untiered. The block below maps a 0.51
path onto the tier its 0.22 counterpart had. It adds no new value judgement.

**Same field, same tier.** At 0.22, `**.level_of_theory` and `**.software_release` were
literal-ending globs, so they beat broad subtree rules such as
`reaction_upload.transition_state.*` and `species_upload.thermo.*`. Now that those fields
are containers, a `**.level_of_theory.*` glob would lose to the subtree rules, and one
field would get different tiers on different routes. The block therefore lists the
leaves explicitly: literal-ending globs keep winning, as before. For the same reason,
execution environment and literature under the TS and thermo/statmech/kinetics subtrees,
and `statmech.freq_scale_factor` under the statmech subtrees, get explicit tier-2 globs. `ts_upload` is tiered exactly like
`reaction_upload.transition_state`.

**Deliberately untiered.**

- Deposit `rights` and the transport root: decisions for the depositor
  (BRIDGE_ROADMAP.md §D), not path shapes.
- The value decisions listed after the block, which need your call.

```yaml
tier_1_kinetics_lookup:
  - ts_upload.*                                   # the standalone TS = reaction_upload.transition_state
  - reaction_upload.species.thermo.*              # species mirror of species_upload.thermo
  - reaction_upload.species.statmech.*            # species mirror of species_upload.statmech
  - conformer_upload.statmech.*                   # conformer mirror of species_upload.statmech
  - "**.species_entry.*"                          # species_upload.species_entry.* on every route
  - "**.hessian.*"                                # **.hessian was tier 1; it is a container at 0.51
  - "**.sp_electronic_energy_hartree"             # flattened sp_result
  - "**.freq_n_imag"                              # flattened freq_result
  - "**.freq_imag_freq_cm1"
  - "**.freq_zpe_hartree"
  - "**.freq_frequencies_cm1"
  - "**.freq_reaction_coordinate_mode_index"
  - "**.freq_imaginary_dispositions"

tier_2_reproducibility:
  # **.level_of_theory / **.software_release / **.workflow_tool_release were literal-ending
  # leaves at 0.22; their 0.51 fields are listed so they still beat subtree rules.
  - "**.level_of_theory.method"
  - "**.level_of_theory.basis"
  - "**.level_of_theory.aux_basis"
  - "**.level_of_theory.cabs_basis"
  - "**.level_of_theory.dispersion"
  - "**.level_of_theory.keywords"
  - "**.level_of_theory.solvent"
  - "**.level_of_theory.solvent_model"
  - "**.level_of_theory.spin_treatment"
  - "**.software_release.name"
  - "**.software_release.version"
  - "**.software_release.revision"
  - "**.software_release.build"
  - "**.software_release.release_date"
  - "**.software_release.notes"
  - "**.workflow_tool_release.name"
  - "**.workflow_tool_release.version"
  - "**.workflow_tool_release.git_commit"
  - "**.workflow_tool_release.release_date"
  - "**.workflow_tool_release.notes"
  - reaction_upload.analysis_software_release.*   # was a literal tier-2 leaf; a container at 0.51
  - "**.freq_scale_factor.*"                      # species_upload.statmech.freq_scale_factor was tier 2
  # ...and it must also beat the tier-1 statmech subtree rules, so the whole field is tier 2
  # on every route (the same device as for literature below).
  - species_upload.statmech.freq_scale_factor.*
  - reaction_upload.species.statmech.freq_scale_factor.*
  - conformer_upload.statmech.freq_scale_factor.*
  # execution_environment and literature are tier 2 on every route, including under the
  # tier-1 TS, thermo, statmech and kinetics subtrees.
  - reaction_upload.transition_state.*.execution_environment.*
  - ts_upload.*.execution_environment.*
  - reaction_upload.transition_state.*.literature.*
  - ts_upload.*.literature.*
  - species_upload.thermo.literature.*
  - species_upload.statmech.literature.*
  - reaction_upload.species.thermo.literature.*
  - reaction_upload.species.statmech.literature.*
  - conformer_upload.statmech.literature.*
  - reaction_upload.kinetics.literature.*
```

After this block, 176 demand rows (140 leaves) remain untiered. They are:

- the proposed items below, on the routes the subtree rules do not reach;
- deposit rights and transport;
- local keys, calculation `type`, `scientific_origin`, and the conformer and TS
  `geometry` / `isotopes`.

**Proposed, needs your decision (left untiered).** These have no 0.22 counterpart in
the policy, so a tier would be a new value judgement:

- `**.opt_result.*` and its flattened `opt_converged` / `opt_n_steps` /
  `opt_final_energy_hartree`. Tier 1 would parallel `sp_result` and `freq_result`.
  Tier 2 would treat optimisation convergence as reproducibility evidence.
- `**.scf_stability.*`. Tier 2 would treat it like `spin_diagnostic` and
  `wavefunction_diagnostic`.
- `**.quality`, `**.parameters_extracted_at`, `**.parameters_parser_version`.
  Tier 2 would put them beside `parameters.*`.
- `**.energy_level_of_theory.*`. It sits only under thermo and statmech, so their
  tier-1 subtree rules tier it. A tier-2 glob would treat it like `level_of_theory`,
  but it is a distinct field, so that is your call.

Under the original globs, the TS routes and `species_upload.thermo` / `statmech` place
`opt_result` and `scf_stability` in tier 1 through their subtree rules; the list above
concerns the other routes.

Patterns use `**.` to match a field wherever it occurs in the tree — the same field
(`level_of_theory`, `sp_result`) is reachable by several routes and carries the same
value on each. Phase C expands these against A1's full inventory and reports any row
that matched no pattern, rather than silently defaulting it to a tier.

### Backfill note — a factual consequence, not a re-litigation

The tier-3 placement of `atom_map` stands. One consequence worth recording, since it
is asymmetric with the other tiers: TCKDB will never derive an atom map itself
(ADR 0011), and it accepts an unmapped reaction with a `reaction_atom_map_absent`
warning rather than rejecting it. So reactions deposited before ARC exports maps are
accepted and land *incomplete*. Whether that matters depends on a question still
open: **can a deposited reaction be enriched with an atom map later, or does it need
re-depositing?** If enrichment is cheap, tier 3 costs nothing. If it requires
re-deposit, every reaction uploaded in the meantime accrues rework.

Phase C will check TCKDB for an enrichment/update path and report the answer rather
than assume it. If re-deposit turns out to be the only route, that is worth a second
look at the ordering — but that is a decision for after the evidence, not before.

### Deadlines

None declared. Phase C ranks on value and cost alone; say the word if a paper or
collaborator dataset should override that.

## Correctness overrides value

Independent of the tiers above, anything A2 or A5 flags as a **silent-corruption
risk** — a unit mismatch, a 0-based/1-based atom-index mismatch, or a semantic shift
under an unchanged field name — is ranked above every tier. These pass validation and
write wrong science into the database, so a cheap fix that prevents one outranks an
expensive fix that merely adds a field. This rule is not negotiable by tier and Phase
C applies it automatically.
