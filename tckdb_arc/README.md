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
298.15 K). Blocks with entropy content carry `reference_pressure_bar`: ARC's
recorded `standard_state_pressure_pa` when it is a number in Pa between 0.5 and
2 bar, otherwise 1.01325 bar, the 1 atm RMG hard-codes. A block the shared
TCKDB enthalpy rule would refuse is omitted and reported in the sidecar and
outcome `warnings`. So is a block with an H298, point H, or NASA H(298.15 K)
beyond ±2.0e4 kJ/mol (`enthalpy_not_formation_magnitude`): that is a raw
absolute energy from Arkane run without atom-energy corrections, not a
formation enthalpy. This interim guard misses H/H2-only species; it stays until
ARC exports whether atom corrections were applied.
