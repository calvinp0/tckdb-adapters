Output.yml schema 1.3 document written by ARC's real `write_output_yml` (ARC PR #1059,
head `bc731fb4`) and validated against its JSON schema, for the batch E tests (levels,
programs, conformers, IRC endpoints, composite job, isotopes, routes). `generate_fixture.py`
regenerates `output.yml`, `parser_evidence.json` and the GSM files under `calcs/` from an ARC
worktree at that commit (it needs ARC's `arc/testing` logs; see its docstring).

What the document holds:

* header `adaptive_levels` with two species at different levels (`iC3H7` b3lyp/6-31g,
  `nC3H7` wb97xd/def2tzvp with a coupled-cluster sp) while the header `opt_level` is
  uhf/3-21g;
* `nC3H7`: three screened conformers (two ESS-optimized at the header
  `conformer_opt_level`, one force-field geometry) with electronic energies;
  `nC3H7_ff`: conformers that only hold force-field energies (`MMFF94s (rdkit)`);
* `SO2OO`: a composite run (only `composite_log`, `levels.composite` cbs-qb3);
* `iC3H7_d`: an isotopically substituted species (one deuterium, SMILES `[2H]C[CH]C`);
* `sBuOH`: ARC's rich species with a rotor scan carrying its own program;
* `TS0` of `nC3H7 <=> iC3H7`: IRC jobs, an xtb GSM path search (`gsm_level` gfn2/xtb) and the
  two IRC endpoint species `IRC_TS0_1` / `IRC_TS0_2` (`irc_endpoint_of: TS0`).

Real vs fixture: the Gaussian/Orca logs behind the paths are `arc/testing` files and are not
shipped (the tests upload no artifacts), except the GSM stringfile and node outputs; the two
`.xtbout` files are written by the generator (xtb banner, `--chrg 0 --uhf 1`, GFN2-xTB) so ARC can
state `gsm_level`. Point groups, correction tables, Arkane provenance, thermo/kinetics numbers
are the writer's test fixtures; the IRC logs and endpoint geometries are borrowed or constructed.
The Gaussian logs are staged under different species labels, so levels recorded in
`levels` do not match the method in the logs; read the structure, not the numbers.
