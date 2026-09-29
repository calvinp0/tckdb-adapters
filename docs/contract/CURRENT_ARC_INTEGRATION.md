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
  TCKDB HEAD has since moved to `ad3cd706` (client 0.94.0, still schemas 0.51.0),
  adding backend-only rules (#565 refuses ARC/RMG as calculation software, #566)
  that the adapter already satisfies. See BRIDGE_ROADMAP.md C12.
- **Adapter 0.6.0 (2026-09-29):** pins and CI move to TCKDB `11cc43d7`
  (tckdb-client 0.95.0, tckdb-schemas 0.52.0, the first release shipping the
  producer contract). See [the 0.52 section](#tckdb-schemas-052-producer-contract-adapter-060).
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

Final result, run in `arc_env` (ARC importable): **897 passed, 4 skipped,
37 subtests passed**. Three skips need a real GSM stringfile
(`ARC_GSM_STRINGFILE_FIXTURE`); the fourth is the level-normalization parity
test, which needs ARC `1977e53b` or later and passes with the
`feature_export_atom_corrections_applied` worktree on `PYTHONPATH`. `pip check`
and `git diff --check` pass.

## Verified mapping and corrections

| ARC output | TCKDB destination | Result of this audit |
|---|---|---|
| Species identity, geometry, opt/freq/SP | Conformers and calculations | Existing mappings retained; coarse reaction optimizations explicitly linked to their conformer without claiming the final geometry |
| `parser_evidence` descriptor and JSON | Hessian, IRC, GSM result blocks | Current names and Arkane producer metadata accepted; legacy `tckdb_evidence` retained |
| Hessian matrix and frame geometry | Hessian with its own geometry | Portable evidence retains the matrix's coordinate frame instead of substituting the conformer frame |
| GSM geometry-matched invocation values | Path points and gradients | Current coordinate name supported; unmatched invocations never attached by numeric node identity; sparse indices retain the right relative energies |
| Reaction participant thermo/NASA/points | Participant `thermo` | Source-calculation links restored for the current schema, scoped to each participant |
| Thermo enthalpy basis and standard-state pressure | `enthalpy_reference_kind`, `reference_pressure_bar` on every thermo block | Enthalpy content (H298, NASA, point H or G) declares `formation_298k` (TCKDB #520). Entropy content (S298, NASA, point S or G) carries `thermo.standard_state_pressure_pa` / 1e5 when ARC recorded it as a number within 0.5–2 bar; otherwise, since adapter 0.6.0, `reference_pressure_bar` is omitted (never defaulted; TCKDB #529 and the 0.52 producer contract) and `thermo_reference_pressure_not_stated` is reported with action `reference_pressure_omitted` (0.5.0 filled RMG's 1 atm, 1.01325 bar). Cp-only blocks carry neither. Enthalpy is declared only when it is a formation enthalpy; for pre-1.2 output without the correction flags this is the maintainer-approved option (d) interpretation (Arkane's corrected H298 is a formation enthalpy by construction; the magnitude, header-level, dispersion/solvation and light-species checks below are heuristics that make an uncorrected enthalpy very unlikely to pass, not proof): from output.yml 1.2, `thermo.atom_corrections_applied` must be true and `thermo.atom_corrections_level` must equal the energy level (`composite_method`, else `sp_level`; effective method with dispersion folded in, and basis, normalized by a port of ARC `1977e53b`'s `_normalized_method_and_basis`, checked by a parity test against that ARC), and neither level may set `dispersion` or `solvation_method`. Non-finite values and enthalpies beyond ±2.0e4 kJ/mol always fail. For a null or absent switch (output.yml 1.0/1.1, YAML-loaded species) adapter 0.5.0 then also strips when the header `arkane_level_of_theory` is not the energy level (`enthalpy_atom_corrections_level_mismatch`; a null header or one without a method is not checked), when the energy level or that header sets `dispersion` or `solvation_method` (`enthalpy_atom_corrections_level_unverifiable`), and when the species' composition (xyz, else the record's `formula`, since ARC 1.0 writes `xyz: null` for monoatomics) puts its raw total energy (H 0.50, He 2.90, Li 7.43 hartree per atom) inside the magnitude bound (`enthalpy_formation_unverifiable_light_species`: hydrogen-only species, He, He2, HeH, the Li atom), in that order; their context adds `atom_corrections_applied: not_recorded` and `corrections_level_source`. These interim rules are superseded by output.yml 1.2's switch (ARC PR #1059) for new runs. Failing enthalpy (H298, NASA, point H and G) is stripped, S298 and point S/Cp kept, with warning action `thermo_enthalpy_omitted` (`thermo_omitted` if nothing remains). A block the shared `tckdb_schemas.enthalpy_reference.enthalpy_reference_error` rule refuses is omitted whole. All refusals are producer warnings in the sidecar and outcome |
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

The ranked, owner-sorted list is now **[`BRIDGE_ROADMAP.md`](BRIDGE_ROADMAP.md)**. It is
built from the 0.51 gap matrix (`GAP_MATRIX.md`) and the live integration-gate
findings. The table below is kept as a short index into it. Three rows of the earlier
version of this table were contradicted by the refreshed inventories and are corrected
here: transport, `wavefunction_stability` and `ts_checks`.

| Gap | Owner / next step | Roadmap |
|---|---|---|
| Petersson `bac_total` with no bond component: TCKDB 422s the whole upload (`bac_total_requires_components`). Current ARC drops all components when any bond lacks a parameter (`arc/output.py:1592-1596`) and the adapter forwards `components or []` | Adapter: drop the componentless correction on bonded species and TSs, with a sidecar warning. **Producer-breaking** | A1 |
| TS-guess `path_search` and IRC calculations labelled with the opt level and opt ESS | Adapter. The TS guess is always wrong: use `neb_level` / `ess_software.neb`, and omit GSM until ARC exports its level. The IRC is wrong when `irc_level` ≠ `opt_level`: ARC defaults `irc_level` to `wb97xd/def2tzvp` (`arc/main.py:1166-1176`), so take the IRC level from its log's route line. **Wrong data** | A3, B3 |
| Screened alternative conformers filed as opts at `opt_level` | Adapter: stop until ARC exports the conformer level. This is wrong in ARC's default configuration (conformer `wb97xd/def2svp` vs opt `wb97xd/def2tzvp`, `arc/settings/settings.py:227-229`). **Wrong data** | A4, B3 |
| `statmech_treatment` inferred from ARC's torsion list, though Arkane ignores rotors when the freq log has no force-constant matrix (RMG-Py `arkane/statmech.py:647-667`; ARC's Gaussian freq always writes one, so the case is composite/ORCA/Q-Chem/Molpro) | Adapter: emit a rotor-bearing treatment only when parser evidence has the species' `freq_hessian`, otherwise omit (the field is optional in TCKDB); ARC: export what Arkane applied. **Wrong data (conditional)** | A5, B4 |
| Atoms get a primary opt that never ran: normally sent with `converged: false`, since ARC never sets `job_types['opt']` for an atom, and with `converged: true` in composite runs (`arc/scheduler.py:3441`) | TCKDB: a form without an opt; adapter: skip until then. **Wrong data** | A6, C6 |
| Current ARC rotor export has results but no scan level/software | ARC must export scan provenance. The adapter accepts an explicit `scan_level`; missing provenance is reported, not replaced with `opt_level`. See `arc/output.py::_build_rotor_scan_entry` and `arc/scheduler.py::run_scan_jobs` | B3 |
| Standalone TS endpoint has no `scan_result` slot | Use `computed_reaction` to carry TS scans with known provenance; standalone mode warns and omits them. Parity needs TCKDB work | C9 |
| TS statmech has no bundle field | Extend TCKDB `BundleTransitionStateIn` before wiring ARC's TS statmech (still true at 0.51) | C8 |
| Full tunneling and partition-function provenance | TCKDB `BundleKineticsIn` still lacks `tunneling_application`, `interpretation_assignments` and `network_kinetics_ref` at 0.51. The existing tunneling label is not replayable evidence | C8 |
| Transport | **Corrected.** ARC has no working transport path to export: no `onedmin` job adapter is registered, the processor's transport step is a `# todo` (`arc/processor.py:237`), and `transport_data` is not persisted. TCKDB *does* have homes at 0.51 (`conformer_upload.transport` and `POST /uploads/transport`). So this is new ARC capability first; the adapter follows | B11 |
| `wavefunction_stability` | **Corrected:** ARC exports it (`arc/output.py:1841-1842`, tested). The gap is the adapter's: map it to `scf_stability` on the sp/freq calculation whose reference was tested, never on the final opt | A10 |
| `ts_checks` | **Corrected:** ARC exports the verdicts since output 1.1 (`arc/output.py:1986`, `:2779-2805`). The gaps are in the adapter and TCKDB. The adapter must map `ts_checks.IRC` (never `irc_converged`, which only means the IRC jobs finished) to `validation_evidence`. TCKDB accepts only `kind: 'irc'`, so the E0, e_elect, freq and NMD verdicts have no home. No real-ARC fixture sets `ts_checks.IRC` | A14, C7 |
| Execution environment and effective calculation settings | ARC export is incomplete, especially beyond coarse/fine optimization settings. Do not invent runtime metadata from requested input settings | B11 |
| NEB portable evidence, alternative TS guesses, multidimensional rotor scans | Producer evidence/export work; the current portable sidecar covers Hessian/IRC/GSM, successful 1D rotors and the chosen guess | B11 |
| Deposit rights | Requires explicit depositor information/configuration; no license or consent is inferred. Silent at upload, but a dataset release refuses records without a rights basis | D2 |
| Whether Arkane applied atom-energy corrections | Exported by ARC output.yml 1.2 (`thermo.atom_corrections_applied` / `atom_corrections_level`, PR #1059 branch `feature_export_atom_corrections_applied`, not yet on ARC main) and consumed by the adapter. Pre-1.2 output (`db0934d5`, ARC main today) cannot say so. An `energy_corrections[]` `atom_energy` row does **not** prove the corrections were applied: it is keyed on the energy level alone, and Arkane's model chemistry can be `None` when the freq level is not found and no `freq_scale_factor` was given (`arc/statmech/arkane.py:1138-1157`). The row's absence proves nothing either. At `0913124`, such output gets only the ±2.0e4 kJ/mol magnitude guard, which misses H, H2, He and Li. **Decided (option d); committed as `84b1b05` on `fix_unverifiable_enthalpy_without_flags` (adapter 0.5.0), pending merge:** when the flag is absent, strip the enthalpy for light species by composition, taken from the species' xyz or else its `formula` (hydrogen-only species, He, He2, HeH, the Li atom), when the header `arkane_level_of_theory` does not match the energy level, and when either level sets a separate `dispersion` or `solvation_method`. Composition uses the xyz, else the record's `formula`, because ARC 1.0 writes `xyz: null` for monoatomics. The header `arkane_level_of_theory` is written by ARC `db0934d5` as a required, nullable level dict; a null header, or one without a method, is not checked. These interim rules are superseded by output.yml 1.2's switch (ARC PR #1059) for new runs. Residual risk: a stand-in with the same method and basis that differs by a year refit or by ARC's fuzzy key match. In the golden corpus only H2 loses its enthalpy. The `feature_tckdb_integration_gate` test `test_golden_species_calculations_thermo_and_hessian` asserts golden H2's `formation_298k` and NASA fit, so whichever branch merges second must update it | A2, B1, D1 |
| Per-species energy level | ARC must export the level each species' energies were computed at. Under `adaptive_levels` every calculation is attributed to the run-level level (**wrong data**), and the adapter's comparison with `atom_corrections_level` can pass wrongly. `output.yml` does not record adaptive runs, but the adapter can detect them from the project's `input.yml` (which the CLI already reads) or `restart.yml` (ARC saves `adaptive_levels` there, `arc/main.py:439`). The interim is to refuse or strip affected calculations until ARC exports per-species levels | A2b, B2 |
| Atom-energy matching ignores dispersion and solvation | ARC bug. Its Arkane key match and `data/AEC.yml` lookup ignore `dispersion` and `solvation_method`: B3LYP + GD3BJ gets plain B3LYP atom energies and SMD gets gas-phase ones, yet `atom_corrections_level` equals `sp_level`. Fix: make the match refuse, or warn, when the level has dispersion or solvation the matched key lacks. ARC `1977e53b` warns at run time. Until the match is fixed, the adapter strips enthalpy when either level sets either field (`enthalpy_atom_corrections_level_unverifiable`) | B1 |
| TCKDB commits writes after the 201 is sent (`backend/app/api/deps.py:123-150`, T4) | TCKDB. Until fixed, a 201 does not prove persistence, so the adapter's sidecar and idempotency record can describe a deposit that does not exist | C1 |

TCKDB workflow checks extend beyond schema validation: source calculations
must belong to the right species and scientific role; SP/optimization geometry
and level relationships must agree; TS composition must match reaction
participants. `TCKDB_DEMAND.yml` now lists them as 508 `workflow_check` rows, and
`gap_matrix.yml` attaches each to its path. The live integration gate (branch
`feature_tckdb_integration_gate`, `docs/contract/INTEGRATION_GATE.md`, under review)
POSTs the offline corpora to an isolated backend and reads the rows back. Never
use production deposits as this test fixture.

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

## tckdb-schemas 0.52 producer contract (adapter 0.6.0)

tckdb-schemas 0.52.0 ships `python -m tckdb_schemas.contract` (the contract,
generated from TCKDB's routes, models and refusal catalogue) and one JSON
Schema per upload payload. `--since 0.51.0` lists only additive changes: the
contract module, the `producer_rule` marker, field descriptions for `phase`,
`enthalpy_reference_kind` and `reference_pressure_bar`, and request examples;
every 0.51.0 payload validates identically. The adapter changes below are
conformance with what the contract states, not reactions to new validation.

- **Tests.** `tests/conftest.py` validates every payload the adapter builds
  (conformer, computed-species, computed-reaction, transition-state, and the
  artifact batch bodies) against the shipped JSON Schema for its route;
  `contract_validate` does the same wherever a test checks a payload or
  fragment. `test_contract_pin.py` pins tckdb-schemas 0.52.x.
- **Found by the JSON Schema:** `tunneling_model` was sent as ARC's `Eckart`,
  which the lenient pydantic model normalizes but the schema's enum
  (`eckart`, ..., `other`) refuses. It is now sent as the token the server
  stores.
- **Pressure:** omitted with a warning when ARC recorded none (above).
- **`path_search_result.converged`:** omitted. ARC's TS-guess `success` is
  set when the output file exists (`arc/job/adapters/ts/xtb_gsm.py:490-496`,
  `orca_neb.py:360-364`), not on convergence.
- **GSM `is_climbing_image`:** no longer set on the stringfile's energy peak.
  The contract defines it for NEB-CI; string methods ignore it.
- **IRC `direction`:** required. When any contributing log's direction is
  unstated (no `irc_log_directions` entry, which ARC runs before `f6af510b` pad
  with None; no parsed per-point direction; no forward/reverse filename), the
  `irc_result` is refused with `irc_direction_not_stated` instead of claiming
  `both` or filing unlabelled points under the stated branch; the `irc`
  calculation stays. ARC runs every IRC job one way
  (`arc/job/adapters/common.py:702-704`) and current output records it
  (`arc/output.py:1976-1981`).
- **Test hook scope.** The conftest hook runs each route's published pydantic
  model and the shipped JSON Schema. It does not reach the route handlers'
  rules (`thermo_energy_level_*`, `calculation_geometry_composition_mismatch`,
  ...) or headers, and `format` keywords are not enforced; the live gate does.
  Two tests deliberately build a request TCKDB refuses (a dangling torsion scan
  reference the adapter forwards unrepaired) and are marked
  `payload_refused_by_contract`.

### Maintainer decisions (2026-09-29)

| Adapter behaviour | Contract | ARC | Decision |
|---|---|---|---|
| `species_entry.charge` / `.multiplicity` and the TS `charge` fell back to 0 / 1 when absent | required, no default | always written (`arc/output.py:1698-1699`) | **Refuse.** A record without a usable integer raises `ValueError` ("states no usable charge"); that upload is not built and the sweep reports the failure (`_stated_integer`) |
| IRC calculation labelled `opt_level` | `level_of_theory` required | IRC runs at `irc_level` (`arc/scheduler.py:1827-1840`), default `wb97xd/def2tzvp` (`arc/main.py:1166-1176`; `arc/settings/settings.py:229,235` make opt = irc by default), not exported | **Keep, warn.** Every IRC calculation reports `irc_level_assumed_opt_level` (action `level_of_theory_assumed`). Wrong when a user set `opt_level` without `irc_level`. Pending ARC export of `irc_level` (BRIDGE_ROADMAP B3) |
| `freq_scale_factor.scale_kind = 'fundamental'` | optional, server default | not stated; ARC's table mixes CCCBDB fundamental and ZPE×1.014 harmonic factors (`data/freq_scale_factors.yml`) | **Omit.** One-time payload hash change |
| `electronic_state_kind = 'ground'` | optional, server default | not stated; ARC computes the requested multiplicity, which need not be the ground state | **Omit.** One-time payload hash change |
| freq level falls back to `opt_level` | `level_of_theory` required | ARC sets `freq_level = opt_level` itself when unset (`arc/main.py:1119-1125`) | **Keep** |
| sp level falls back to `opt_level` | `level_of_theory` required | a null `sp_level` means no sp job ran (`arc/main.py:1135-1145`), so ARC parses `e_elect` from the opt log (`arc/scheduler.py:849-850`, `parse_opt_e_elect` at `:3484-3505`); `sp_level == opt_level` also reuses the opt output (`arc/scheduler.py:1662-1683`); output.yml's `sp_energy_hartree` is that `e_elect` (`arc/output.py:1763-1764`). The sp calc is marked `reused_result` from opt | **Keep** (verified) |
| Arrhenius `T0_k` → 1 K when the key is absent | TCKDB evaluates `a*T**n` | current output always writes `T0_k` (`arc/output.py:2824`); pre-contract exports come from Arkane's `Arrhenius().fit_to_data(...)` called without `T0` (RMG-Py `arkane/kinetics.py:190`), whose default is `T0=1` K (`rmgpy/kinetics/arrhenius.pyx:149`, stored at `:200`; `Arrhenius.__init__` default `T0=(1.0, "K")` at `:69`) | **Keep** for pre-contract output |
| `ts_upload.reaction.reversible = True` | required, no default (the computed-reaction route defaults `true`) | ARC has no reversibility attribute (`arc/output.py:2841-2849`) | **Keep.** Refusing would block every standalone TS upload. [TCKDB#583](https://github.com/TCKDB/TCKDB/issues/583) asks the TS route to default it like the computed-reaction route |
