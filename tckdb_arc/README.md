# tckdb-arc

Convert ARC `output/output.yml` and portable parser evidence into TCKDB
species, reaction, and transition-state uploads. Payloads and upload metadata
are written locally before any network request, allowing inspection and replay.

Requires Python 3.11+, `tckdb-client` 0.93.x and `tckdb-schemas` 0.51.x.
For development with sibling checkouts:

```bash
python -m pip install ../TCKDB_v2/clients/python \
  ../TCKDB_v2/schemas/python/tckdb-schemas -e './tckdb_arc[test]'
python -m pytest tckdb_arc/tests -q
```

Run these commands from the `tckdb-adapters` repository root. Preview an
existing ARC project whose `input.yml` contains an enabled `tckdb` block:

```bash
tckdb-arc-upload /path/to/project/input.yml --offline
```

Omit `--offline` to use the input file's upload setting. Credentials and upload
destination come from the `tckdb` configuration.

Current ARC exports `parser_evidence.json` for Hessian, IRC, and GSM evidence;
the older `tckdb_evidence.json` contract is also supported. Keep the evidence
file beside `output.yml`. ARC itself is optional: raw-log reparsing requires
ARC on `PYTHONPATH`, whereas portable sidecars work in the base installation.

Thermo blocks with enthalpy content declare `enthalpy_reference_kind:
formation_298k` (Arkane's H298 and NASA are formation enthalpies at
298.15 K) only when that holds. Blocks with entropy content carry
`reference_pressure_bar`: ARC's recorded `standard_state_pressure_pa` when it
is a number in Pa between 0.5 and 2 bar, otherwise 1.01325 bar, the 1 atm RMG
hard-codes.

ARC output.yml 1.2 records whether Arkane applied atom-energy corrections
(`thermo.atom_corrections_applied`) and whose atom energies it subtracted
(`thermo.atom_corrections_level`). The enthalpy (H298, the NASA fit, point H
and G) is stripped from the block, keeping S298 and point S/Cp, when:

- the switch was off (`enthalpy_atom_corrections_not_applied`);
- the correction level is not the species' energy level, `composite_method`
  or else `sp_level`, compared by method (with its dispersion folded in) and
  basis, normalized exactly as ARC's own level matching (ARC `1977e53b`
  `_normalized_method_and_basis`, ported, with a parity test): case, hyphens,
  spaces and a trailing refit year are ignored, dispersion spellings are
  canonical, `ccsd(t)` and `ccsdt` stay distinct, and software never matters
  (`enthalpy_atom_corrections_level_mismatch`);
- either level sets `dispersion` or `solvation_method`, which ARC's
  atom-energy matching ignores, so a match proves nothing
  (`enthalpy_atom_corrections_level_unverifiable`);
- a value is not finite (`enthalpy_not_finite`), or H298, a point H or NASA
  H(298.15 K) exceeds ±2.0e4 kJ/mol (`enthalpy_not_formation_magnitude`).

The magnitude check is the only one for pre-1.2 output and for thermo Arkane
loaded from its own YAML (switch `null`). It misses every species whose raw
total energy is below about 7.6 hartree: H, H2, He, the Li atom. Each strip is
reported in the sidecar and outcome `warnings` with action
`thermo_enthalpy_omitted`, or `thermo_omitted` when nothing is left. A block
the shared TCKDB enthalpy rule would refuse is omitted whole.

Known gaps: under ARC `adaptive_levels` species' SP levels vary but output.yml
records one `sp_level`, so the level comparison can pass wrongly and the
adapter cannot detect such runs (ARC `1977e53b` warns at run time); ARC should
export a per-species energy level.
ARC also matches atom energies ignoring `dispersion` and `solvation_method`,
which is why those fields make the level unverifiable.
