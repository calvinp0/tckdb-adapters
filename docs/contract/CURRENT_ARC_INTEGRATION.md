# ARC integration audit — 2026-09-29

The adapter can build current TCKDB species, reaction and transition-state
payloads, but it cannot yet deposit every scientifically useful ARC result.
This audit separates verified adapter fixes from missing producer exports and
target fields. Earlier inventories in this directory describe older branches
and schema versions; their percentages are not current coverage measurements.

## Baselines and validation

- ARC main: `db0934d5973e7dda0facd0c99f4080b4243166ae`.
- TCKDB main: `adceeff5c0470c37483d16c4d6b46a005c076dfe`, client 0.93.0,
  schemas 0.51.0. The checkout advanced during this investigation; the final
  installation and CI pin use this revision.
- Adapter: working tree including substantial pre-existing uncommitted work.
  Those changes were retained.

The original environment used client 0.35.0/schema 0.22.0 despite package and
CI pins at client 0.27/schema 0.8. Its 715 passing tests masked four current
schema rejection cases and an obsolete test expecting reaction thermo
provenance to be rejected. Package dependencies and CI now target the tested
current contract. The local environment was upgraded from local source
copies, without changing either sibling repository.

Tests build payloads offline, validate against the real shared schemas, and
exercise current ARC's checked-in evidence. They do not establish successful
database persistence or production-server compatibility. No upload, deployment,
commit, or push was performed.

Final base-suite result: **774 passed, 16 skipped, 37 subtests passed**.
The skips require the optional ARC environment. `pip check` and
`git diff --check` pass.

## Verified mapping and corrections

| ARC output | TCKDB destination | Result of this audit |
|---|---|---|
| Species identity, geometry, opt/freq/SP | Conformers and calculations | Existing mappings retained; coarse reaction optimizations explicitly linked to their conformer without claiming the final geometry |
| `parser_evidence` descriptor and JSON | Hessian, IRC, GSM result blocks | Current names and Arkane producer metadata accepted; legacy `tckdb_evidence` retained |
| Hessian matrix and frame geometry | Hessian with its own geometry | Portable evidence retains the matrix's coordinate frame instead of substituting the conformer frame |
| GSM geometry-matched invocation values | Path points and gradients | Current coordinate name supported; unmatched invocations never attached by numeric node identity; sparse indices retain the right relative energies |
| Reaction participant thermo/NASA/points | Participant `thermo` | Source-calculation links restored for the current schema, scoped to each participant |
| Correction scheme metadata | Applied energy corrections | Removed obsolete `scheme.version`; nonempty legacy version preserved in scheme note |
| `ess_software` / `ess_versions` | Calculation software release | Observed software used per job; no borrowing another program's version |
| `scf_reference` | Level-of-theory spin treatment | Actual freq/SP references retained only on their respective jobs |
| `freq_hessian_method` | Typed calculation parameter | Known analytic/finite-difference method retained |
| `rotor_scans` | Scan calculations | TS reaction-bundle mapping added; explicit scan provenance required rather than assuming optimization method |
| Arrhenius `A`, `T0_k`, `n` | Modified Arrhenius prefactor | Normalize `a = A / T0_k**n`; rate-equivalence tests span temperatures and exponents |
| Accepted-upload warnings | Sidecar, outcome, log | Structured warnings exposed explicitly; previously already present inside full JSON response bodies |

Raw-log fallback now requires the Hessian parser's matching frame geometry;
it omits matrices when that geometry is unavailable. Raw GSM fallback retains
geometries and stringfile relative energies, but does not attach archived
invocation energies/gradients by node-number arithmetic. Portable evidence is
the supported path for geometry-matched absolute values.

ARC/RMG evaluates `A*(T/T0)**n*exp(-Ea/RT)`; TCKDB evaluates
`a*T**n*exp(-Ea/RT)`. Multiplicative `dA` is unchanged by this conversion.
Missing legacy `T0_k` retains the 1 K convention; explicit invalid or null
reference temperature omits the prefactor and warns instead of depositing
a rate law with unknown normalization.

## Remaining work, by owner

| Gap | Owner / next step |
|---|---|
| Current ARC rotor export has results but no scan level/software | ARC must export scan provenance. Adapter accepts explicit `scan_level`; missing provenance is reported, not replaced with `opt_level`. See `arc/output.py::_build_rotor_scan_entry` and `arc/scheduler.py::run_scan_jobs`. |
| Standalone TS endpoint has no `scan_result` slot | Use `computed_reaction` to carry TS scans with known provenance; standalone mode warns and omits them. Target parity requires TCKDB work. |
| TS statmech has no bundle field | Extend TCKDB `BundleTransitionStateIn` before wiring ARC's TS statmech. |
| Full tunneling and partition-function provenance | TCKDB `BundleKineticsIn` lacks `tunneling_application`, `interpretation_assignments`, and `network_kinetics_ref`. The existing tunneling label is not replayable evidence. |
| Transport | ARC has internal transport data but does not export it in `output.yml`. TCKDB bundles also lack transport fields; a separate transport upload route exists. Both producer export and adapter orchestration are needed. |
| `wavefunction_stability` | Adapter gap requiring source-job binding. A stability calculation/followed solution must not be mislabeled as a result of the final SP or optimization. |
| `ts_checks` | Adapter evidence mapping remains. ARC's completed IRC jobs do not establish that its endpoint validation passed; map explicit verdicts and source calculations to TCKDB validation evidence. |
| Execution environment and effective calculation settings | ARC export is incomplete, especially beyond coarse/fine optimization settings. Do not invent runtime metadata from requested input settings. |
| NEB portable evidence, alternative TS guesses, multidimensional rotor scans | Producer evidence/export work; current portable sidecar covers Hessian/IRC/GSM, successful 1D rotors and the chosen guess. |
| Deposit rights | Requires explicit depositor information/configuration; no license or consent is inferred. |

TCKDB workflow checks extend beyond schema validation: source calculations
must belong to the right species and scientific role; SP/optimization geometry
and level relationships must agree; TS composition must match reaction
participants. A future integration gate should POST these offline corpus
payloads to an isolated current backend and inspect persisted relationships
and response warnings. Never use production deposits as this test fixture.

## Reproduce

From the repository root, install the shared packages as described in
`README.md`, then run:

```bash
python -m pytest tckdb_arc/tests -q
python -m pip check
```

`test_evidence.py` uses a frozen copy of ARC's actual current parser-evidence
fixture. `test_current_contract_mapping.py` covers current field/linkage
semantics. `test_kinetics_reference_temperature.py` checks numerical rate
equivalence. `test_golden_corpus.py` validates all three payload roots and
keeps the older sidecar format covered.
