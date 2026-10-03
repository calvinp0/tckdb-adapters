`output.yml` is **derived by `generate.py`, not written by ARC**: the real document of `../arc_1_3_reactions`
(ARC #1059 @ bc731fb4) with the keys ARC added at head `ebc88ec8` set to values that satisfy ARC's documented
rules, plus a second reaction with a repeated reactant. It validates against `arc/schemas/output_yml_schema.json`
at `ebc88ec8` (`test_arc_1_3_ts_atom_map.py` checks that when the schema is available).

* `OH + CH4 <=> H2O + CH3`: `reactant_species_labels` `[OH, CH4]` (not the sorted `reactant_labels`), a
  `ts_atom_map` whose TS order differs from the concatenated reactants (`ts_atom_order_follows_reactants:
  false`) and agrees with the TS's `irc_participant_mapping`; `nmd_forced: false` with
  `reaction_coordinate_mode_index: 1`.
* `CH3 + CH3 <=> C2H6` (`TS1`, species `C2H6` copied from the real `CH3` with replaced geometry): a repeated
  reactant in `reactant_species_labels`, a `ts_atom_map` with a TS order unlike the reactants', and a forced
  NMD pass (`nmd_forced: true`, `reaction_coordinate_mode_index: null`).
* `CH4` has four screened conformers with mixed `conformer_ess_software` / `conformer_ess_version` (Gaussian,
  ORCA, a lost log with no program, a force-field geometry) and some `null` `conformer_energies`.

The calculation logs the paths name do not exist; no test here reads them.
