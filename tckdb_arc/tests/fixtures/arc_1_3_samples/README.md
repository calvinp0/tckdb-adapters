Three output.yml schema 1.3 documents written by ARC's real `write_output_yml`
(ARC PR #1059, head `bc731fb4`) and validated against its JSON schema, copied
unchanged from `arcbench/tckdb_output_1_3_samples/` (ARC agent, `make_samples.py`
there). Read the structure, not the numbers: thermo, kinetics, correction totals,
switches and point groups are placeholders; every level is uhf/3-21g except the
stand-in Arkane level (bmk/cbsb7). The IRC logs and IRC endpoint geometries of
`reaction_kinetics` are borrowed or constructed, not a computed IRC.

* `species_thermo.output.yml`: iC3H7, nC3H7 (conformers with force-field energies),
  CH3NH2 (coupled-cluster sp level).
* `reaction_kinetics.output.yml`: nC3H7 <=> iC3H7 through TS0, with IRC logs and
  IRC endpoint species.
* `legacy_restart.output.yml`: objects rebuilt from a restart that predates every
  1.3 attribute (`levels` all null, empty IRC lists).

No sample carries a `parser_evidence.json`; the adapter must (and does) build from
them without the sidecar. No calculation logs are shipped: the paths inside are
run-relative and the tests that read the documents do not upload artifacts.

The thermo, correction and kinetics batch tests use the same files for structure and plumbing
checks, and numeric invariants only on synthetic records.
