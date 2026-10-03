# tckdb-adapters

Producer adapters that turn a tool's output (ARC today, `tckdb_arc/`) into TCKDB
upload payloads. TCKDB decides the contract; the adapter conforms.

## Layout

`tckdb_core/` (`tckdb-adapters-core`) holds what any producer reuses verbatim: the
payload writer, upload/sidecar/readiness pipeline, idempotency keys, level-identity
replica, config fields. It never imports `tckdb_arc` or reads a producer's output
format (a guard test enforces it). `tckdb_arc/` reads ARC's output and builds the
payloads, uploading through `tckdb_core`. Install the core first:
`pip install -e tckdb_core -e tckdb_arc`.

## Read the producer contract before changing a mapping

Before changing any mapping from a producer's output to a TCKDB payload, read the
producer contract from the installed tckdb-schemas:

```bash
python -m tckdb_schemas.contract --print
python -m tckdb_schemas.contract --since <the tckdb-schemas version this adapter last targeted>
```

The second command shows what moved. The contract is generated from TCKDB's own
routes, models and refusal-code catalogue, so it (not memory, not a guess from ARC
or RMG conventions) is what TCKDB accepts. TCKDB decides the contract and the
adapter conforms.

In the adapter's tests, validate every payload the adapter builds against the
shipped JSON Schema for its route, e.g.
`jsonschema.Draft202012Validator(tckdb_schemas.contract.json_schema("ThermoUploadRequest")).validate(payload)`,
and pin the tckdb-schemas version the tests ran against. Here that is
`tckdb_arc/tests/_contract.py`, `tckdb_arc/tests/conftest.py` and
`tckdb_arc/tests/test_contract_pin.py`. The conftest hook checks the four
whole-request builders (conformer, computed-species, computed-reaction,
transition-state) and the artifact request bodies with the route's published
pydantic model and the shipped JSON Schema; use `contract_validate` where a test
checks a payload or fragment itself. It does not run the route handlers' rules
(e.g. `thermo_energy_level_*`, `calculation_geometry_composition_mismatch`) or check
headers, and JSON Schema `format` keywords are not enforced; only the live
integration gate (`tckdb_arc/tests/integration`) reaches those.

If the source does not state a convention the contract requires (for example
`enthalpy_reference_kind` or `reference_pressure_bar`), refuse to build that block
rather than filling a plausible default.

A scheduled workflow proposes these bumps as a PR or issue; see "Tracking TCKDB
releases" in `README.md`. The reading rule above still applies to that PR.

## ARC-specific decisions

- **Pre-1.2 enthalpies (maintainer-approved option (d)).** Without output.yml 1.2's
  `atom_corrections_applied` flags, the adapter still declares
  `enthalpy_reference_kind: formation_298k` when the non-finite, magnitude,
  header-level, dispersion/solvation and light-species checks pass. This is a
  deliberate interpretation, not a default: Arkane's corrected H298 is a formation
  enthalpy by construction. The magnitude, light-species, header-level and
  dispersion/solvation checks are heuristics that make an uncorrected enthalpy very
  unlikely to pass; they are not proof that the corrections ran at the right level.
  The 1.2 flags supersede them when present.
- **`reference_pressure_bar` is omitted, never defaulted,** when ARC does not record
  `thermo.standard_state_pressure_pa` (or records an unusable value). The adapter
  reports `thermo_reference_pressure_not_stated` instead of filling 1.01325 bar.

