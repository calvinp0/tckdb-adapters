Two of the ARC agent's sample documents (`arcbench/tckdb_output_1_3_samples/make_samples.py`), regenerated with
ARC's real `write_output_yml` at PR #1059 head `ebc88ec8` (schema 1.3 with `reactant_species_labels`,
`ts_atom_map`, `nmd_forced`, `conformer_ess_software` and the rest of that batch). The script was run unmodified
from a scratch copy, so nothing in arcbench changed. As in `../arc_1_3_samples`, the thermo, kinetics, correction
and switch numbers are placeholders chosen by the script; only the structure is meaningful, and the Arkane/RMG
helpers are stubbed as in ARC's `output_schema_test.py`.

Both validate against `arc/schemas/output_yml_schema.json` at that commit (checked by
`test_arc_1_3_ts_atom_map.py` when the schema is available). `species_thermo` has `bac_type: p`, a
`standard_state_pressure_pa` of `null` (the sample's thermo is a placeholder) and null conformer programs;
`reaction_kinetics` is the intramolecular `nC3H7 <=> iC3H7` with a real `ts_atom_map` (TS order follows
the reactants, `reactant_endpoint: 2`) and `nmd_forced: false`.

`legacy_restart` is not copied: the script's own third sample has empty `reactant_species_labels` /
`product_species_labels` at ebc88ec8, which the schema's `minItems: 1` refuses (it holds reactions with no
species; reported to the ARC agent).
