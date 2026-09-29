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
