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
