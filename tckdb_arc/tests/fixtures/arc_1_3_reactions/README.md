Written by ARC's own `write_output_yml` (PR #1059, commit `bc731fb45d560f5afe6383a7da0c8c3f68c678fd`)
for `OH + CH4 <=> H2O + CH3`, with the Arkane/RMG-environment helpers stubbed as in
`arc/output_schema_test.py`. `output.yml` validates against `arc/schemas/output_yml_schema.json` at that
commit. The reaction's `atom_map` is ARC's own (`arc.mapping.driver.map_reaction`, `inferred`), and the TS
record's `ts_checks.IRC` and `irc_participant_mapping` come from ARC's IRC check
(`arc.checks.ts.check_irc_species_and_rxn`) run on endpoint geometries in the TS atom order. The
reactants are listed `OH, CH4` for ARC's map and `CH4, OH` (sorted) in `reactant_labels`. Calculation
logs are not included (the IRC logs are ARC's `rxn_1_irc_*.out`, which `parser_evidence.json` was built from).
