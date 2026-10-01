`output.yml` and `parser_evidence.json` were written by ARC's real `write_output_yml` (PR
ReactionMechanismGenerator/ARC#1059 @ bc731fb4, schema 1.3) over `arc/output_schema_test.py`'s
rich species and TS, extended by `generate.py` here, and validate against
`arc/schemas/output_yml_schema.json`. The two helpers that shell out to the RMG environment, the
Arkane provenance and the RMG-database identity are patched exactly as that test file patches
them, except that NH3's point group is C3v so its geometry classifies as a symmetric top.

What it covers that `../arc_1_3_samples` does not:

- sBuOH: complete Petersson BAC, hindered and free rotors that Arkane kept (`rrho_1d`), a
  coupled-cluster sp level with `sp_t1_diagnostic`, a spin diagnostic with `s_squared_expected`
  and `s_squared_annihilated`.
- nBuOH: a partial Petersson BAC (`skipped_components` lists C-C x3, `components` the C-H bonds
  applied), Arkane dropped every rotor (`arkane_treatment: rrho`, torsion `treatment: null`).
- NH3: `rigid_rotor_kind: symmetric_top`.
- TS0 (sBuOH <=> nBuOH): two imaginary modes in ESS order, `reaction_coordinate_mode_index: 1`
  from a passed normal mode displacement check, a failed IRC, and the reaction's
  `reversible`, `kinetics.comment`, `ts_validation` and `atom_corrections_applied`.

Corrections, thermo and kinetics numbers are placeholders chosen by the generator, except that
the Petersson components sum to their total as output 1.3 promises. Regenerate with
`PYTHONPATH=<ARC checkout> python generate.py <scratch dir>` in an ARC environment.
