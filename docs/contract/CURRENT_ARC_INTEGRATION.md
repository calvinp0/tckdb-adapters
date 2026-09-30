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
- **Adapter 0.6.2:** pins and CI move to TCKDB `4adf7ff4` (tckdb-client 0.95.1,
  tckdb-schemas 0.53.0). `--since 0.52.0` changes only the submission-supersede
  route (public refs, `new_submission_ref`), which the adapter never calls; no
  model changes.
- **Adapter 0.6.5:** pins and CI move to TCKDB `fd447fa0` (tckdb-client 0.95.1,
  tckdb-schemas 0.54.0). `--since 0.53.0` changes no wire model: the
  enthalpy-reference rule is now also reached from `POST /bundles/dry-run`
  (#577), and `dry_run_contended` (503) is a new code on that route only. The
  adapter never calls the bundles routes. The atom-energy scheme note is
  corrected to state Arkane's full formula.
- **Adapter 0.6.6:** roadmap A10, A11 and A13 (see "Energy level, SCF stability and
  conformer statmech (adapter 0.6.6)" below). The computed-species and reaction golden
  hashes change only by the declared `energy_level_of_theory`, proved by strip-and-restore.
- **Adapter 0.6.3:** roadmap A1, A5 and A12 (see "Corrections and statmech
  evidence (adapter 0.6.3)" below). The golden corpus hashes are unchanged.
- **Adapter 0.6.4:** levels the adapter states only when ARC's output supports
  them, and TS IRC evidence. See [Level attribution](#level-attribution-and-ts-irc-evidence-adapter-064).
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
| `ess_software` / `ess_versions` | Calculation software release | Observed software used per job; no borrowing another program's version. Since 0.6.1 the banner is split into `version` and `revision` exactly as TCKDB's shared `SoftwareReleaseRef` would (roadmap A7) |
| `scf_reference` | Level-of-theory spin treatment | Actual freq/SP references retained only on their respective jobs. Since 0.6.6 the declared thermo/statmech energy level carries the sp's `spin_treatment` too (A11) |
| `wavefunction_stability` | Calculation `scf_stability` | Since 0.6.6, on the primary opt, the job ARC's analysis tests (A10) |
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
| TS-guess `path_search` and IRC calculations labelled with the opt level and opt ESS | **Since 0.6.4 the TS guess is fixed** (NEB at `neb_level`; GSM omitted); the IRC uses `restart.yml`'s `irc_level` when recorded, else keeps `opt_level` with a warning. Adapter. The TS guess was always wrong: use `neb_level` / `ess_software.neb`, and omit GSM until ARC exports its level. The IRC is wrong when `irc_level` ≠ `opt_level`: ARC defaults `irc_level` to `wb97xd/def2tzvp` (`arc/main.py:1166-1176`), so take the IRC level from its log's route line. **Wrong data** | A3, B3 |
| Screened alternative conformers filed as opts at `opt_level` | **Done in 0.6.4** (filed at `restart.yml`'s `conformer_opt_level` when stated and `conf_opt` ran; otherwise not uploaded, `conformer_level_not_stated`). Adapter: until ARC exports the conformer level. This is wrong in ARC's default configuration (conformer `wb97xd/def2svp` vs opt `wb97xd/def2tzvp`, `arc/settings/settings.py:227-229`). **Wrong data** | A4, B3 |
| `statmech_treatment` inferred from ARC's torsion list, though Arkane ignores rotors when the freq log has no force-constant matrix (RMG-Py `arkane/statmech.py:647-667`; ARC's Gaussian freq always writes one, so the case is composite/ORCA/Q-Chem/Molpro) | Adapter: emit a rotor-bearing treatment only when parser evidence has the species' `freq_hessian`, otherwise omit (the field is optional in TCKDB); ARC: export what Arkane applied. **Wrong data (conditional)** | A5, B4 |
| Atoms get a primary opt that never ran: normally sent with `converged: false`, since ARC never sets `job_types['opt']` for an atom, and with `converged: true` in composite runs (`arc/scheduler.py:3441`) | TCKDB: a form without an opt; adapter: skip until then. **Wrong data** | A6, C6 |
| Current ARC rotor export has results but no scan level/software | ARC must export scan provenance. The adapter accepts an explicit `scan_level`; missing provenance is reported, not replaced with `opt_level`. See `arc/output.py::_build_rotor_scan_entry` and `arc/scheduler.py::run_scan_jobs` | B3 |
| Standalone TS endpoint has no `scan_result` slot | Use `computed_reaction` to carry TS scans with known provenance; standalone mode warns and omits them. Parity needs TCKDB work | C9 |
| TS statmech has no bundle field | Extend TCKDB `BundleTransitionStateIn` before wiring ARC's TS statmech (still true at 0.51) | C8 |
| Full tunneling and partition-function provenance | TCKDB `BundleKineticsIn` still lacks `tunneling_application`, `interpretation_assignments` and `network_kinetics_ref` at 0.51. The existing tunneling label is not replayable evidence | C8 |
| Transport | **Corrected.** ARC has no working transport path to export: no `onedmin` job adapter is registered, the processor's transport step is a `# todo` (`arc/processor.py:237`), and `transport_data` is not persisted. TCKDB *does* have homes at 0.51 (`conformer_upload.transport` and `POST /uploads/transport`). So this is new ARC capability first; the adapter follows | B11 |
| `wavefunction_stability` | **Corrected:** ARC exports it (`arc/output.py:1841-1842`, tested). **Done in 0.6.6:** mapped to `scf_stability` on the primary opt (the wavefunction ARC tests), never on freq/sp | A10 |
| `ts_checks` | **Corrected:** ARC exports the verdicts since output 1.1 (`arc/output.py:1986`, `:2779-2805`). The gaps are in the adapter and TCKDB. The adapter maps `ts_checks.IRC` (never `irc_converged`, which only means the IRC jobs finished) to `validation_evidence` (done in 0.6.4). TCKDB accepts only `kind: 'irc'`, so the E0, e_elect, freq and NMD verdicts have no home. No real-ARC fixture sets `ts_checks.IRC` | A14, C7 |
| Execution environment and effective calculation settings | ARC export is incomplete, especially beyond coarse/fine optimization settings. Do not invent runtime metadata from requested input settings | B11 |
| NEB portable evidence, alternative TS guesses, multidimensional rotor scans | Producer evidence/export work; the current portable sidecar covers Hessian/IRC/GSM, successful 1D rotors and the chosen guess | B11 |
| Deposit rights | Requires explicit depositor information/configuration; no license or consent is inferred. Silent at upload, but a dataset release refuses records without a rights basis | D2 |
| Whether Arkane applied atom-energy corrections | Exported by ARC output.yml 1.2 (`thermo.atom_corrections_applied` / `atom_corrections_level`, PR #1059 branch `feature_export_atom_corrections_applied`, not yet on ARC main) and consumed by the adapter. Pre-1.2 output (`db0934d5`, ARC main today) cannot say so. An `energy_corrections[]` `atom_energy` row does **not** prove the corrections were applied: it is keyed on the energy level alone, and Arkane's model chemistry can be `None` when the freq level is not found and no `freq_scale_factor` was given (`arc/statmech/arkane.py:1138-1157`). The row's absence proves nothing either. At `0913124`, such output gets only the ±2.0e4 kJ/mol magnitude guard, which misses H, H2, He and Li. **Decided (option d); committed as `84b1b05` on `fix_unverifiable_enthalpy_without_flags` (adapter 0.5.0), pending merge:** when the flag is absent, strip the enthalpy for light species by composition, taken from the species' xyz or else its `formula` (hydrogen-only species, He, He2, HeH, the Li atom), when the header `arkane_level_of_theory` does not match the energy level, and when either level sets a separate `dispersion` or `solvation_method`. Composition uses the xyz, else the record's `formula`, because ARC 1.0 writes `xyz: null` for monoatomics. The header `arkane_level_of_theory` is written by ARC `db0934d5` as a required, nullable level dict; a null header, or one without a method, is not checked. These interim rules are superseded by output.yml 1.2's switch (ARC PR #1059) for new runs. Residual risk: a stand-in with the same method and basis that differs by a year refit or by ARC's fuzzy key match. In the golden corpus only H2 loses its enthalpy. The `feature_tckdb_integration_gate` test `test_golden_species_calculations_thermo_and_hessian` asserts golden H2's `formation_298k` and NASA fit, so whichever branch merges second must update it | A2, B1, D1 |
| Per-species energy level | ARC must export the level each species' energies were computed at. Under `adaptive_levels` every calculation is attributed to the run-level level (**wrong data**), and the adapter's comparison with `atom_corrections_level` can pass wrongly. `output.yml` does not record adaptive runs, but the adapter can detect them from the project's `input.yml` (which the CLI already reads) or `restart.yml` (ARC saves `adaptive_levels` there, `arc/main.py:439`). The interim (adapter 0.6.4) attributes exactly from `restart.yml` where it can and otherwise refuses or strips affected calculations, until ARC exports per-species levels | A2b, B2 |
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

## Provenance passthrough (adapter 0.6.1)

A real ARC benzene run (B3LYP/def2-TZVP, output.yml 1.2; now the fixture
`tckdb_arc/tests/fixtures/benzene_b3lyp_def2tzvp`) deposited on production drew
provenance warnings for data output.yml already held. Status by roadmap item:

- **A7, done.** Each calculation's `software_release` banner (`ess_versions`,
  e.g. `Gaussian 16, Revision C.02`) is split before sending by
  `tckdb_schemas.fragments.refs.SoftwareReleaseRef.normalize_composite_version`,
  the shared rule the server itself applies (it warned
  `software_release_version_is_composite` and stored `16` / `C.02`). The adapter
  reuses that model rather than porting it, so both agree by construction: a
  leading token equal to the software name is stripped and a trailing
  `, Revision <label>` split off (ORCA `5.0.4`, Molpro `2022.3`, Psi4 `1.7`); a
  banner whose leading token names another program (`name=gaussian`,
  `ORCA 6.0.0`; `name=qchem`, `Q-Chem 5.4`) or has no recognised shape is sent
  unchanged, as the server leaves it. The server's `[auto]` note is not sent.
- **A9, done.** Computed-species `thermo.software_release` and
  `statmech.software_release` name Arkane (`version` ← `arkane_version`,
  `revision` ← `arkane_git_commit`, each when recorded), as the reaction bundle's
  `analysis_software_release` does (and now also with the version). Arkane is
  post-processing software; it never goes on a calculation, which TCKDB refuses
  (`calculation_software_is_workflow_tool`). Conformer mode names Arkane on its
  statmech too since 0.6.6 (A13); it has no thermo slot.
- **C10 / MR-2, done for the program name.** Each applied correction's
  `scheme.software` is `{name: <software>}` parsed from the record's
  `matched_arkane_key` (Arkane's database entry, e.g.
  `LevelOfTheory(method='b3lyp2023',basis='def2tzvp',software='gaussian')`),
  the program whose numerics the atom-energy and BAC parameters come from. The
  record's `level_of_theory` is ARC's own level, not the table's: ARC's matcher
  (`arc/statmech/arkane.py`) accepts a key without software for any program. So
  the field is omitted when the key is absent, unparseable or names no software,
  and omitted with `energy_correction_scheme_software_conflict` when it
  disagrees with the level's software. The contract calls a name-only release
  "a complete and honest deposit"; ARC does not record which release computed
  Arkane's tables, so no version is sent. Never Arkane or ARC.
- **Literature, known gap.** `missing_literature_provenance` remains advisory:
  ARC records no citation for its thermo, statmech or schemes.

Also seen on the fixture: the screened alternative conformer's opt was labelled
with `opt_level` and the opt banner (roadmap A4; fixed in 0.6.4, see below). The atom-energy scheme's
`atom_params` were not sent at 0.6.1 and are since 0.6.3 (A12 below).

## Corrections and statmech evidence (adapter 0.6.3)

- **A1, done.** The producer contract's `bac_total_requires_components` (422,
  `assert_bac_total_has_required_components`) refuses a `bac_petersson`
  `bac_total` without a component of kind `bond` when it targets a transition
  state or a species whose SMILES has a bond; a monatomic species is the only
  honest componentless case, and `bac_melius` is exempt. TCKDB does not check
  that components sum to the total. The adapter therefore omits a Petersson
  BAC, keeping the AEC and everything else, when there is no bond component
  (ARC drops the list when any bond lacks a parameter, `arc/output.py`
  1592-1596), when any component lacks `parameter_value` or
  `contribution_value` (the old per-component null filter could send a partial
  decomposition), or on a TS with no bond component. A componentless BAC is
  kept only for a species whose composition is a single atom; an unknown
  composition is treated as bonded. The warning is
  `bac_correction_omitted_components_incomplete`, field
  `[species[<key>].|transition_state.]applied_energy_corrections`, context
  `reason` (`no_components`, `component_unusable`, `no_bond_component`),
  `target_kind` and `species`. Every route that carries the block uses the one
  helper `_build_applied_energy_corrections` (computed-species bundle, reaction
  participants, reaction TS block); the standalone TS route sends no
  corrections. The benzene fixture's complete BAC (3 C-C, 3 C=C, 6 C-H) is
  unchanged; the componentless variants are synthetic.
- **A5, done.** A rotor-aware `statmech_treatment` (`rrho_1d`, `rrho_nd`,
  `rrho_1d_nd`) and each torsion's `treatment_kind` are sent only when
  `_freq_hessian_available` finds a freq Hessian for the species (parser
  evidence, or the log fallback), since Arkane drops every rotor without a
  force-constant matrix (RMG-Py `arkane/statmech.py` 647-667). Plain `rrho` (an
  empty rotor list) does not depend on the Hessian and is always sent, so
  monatomics keep it. Without a Hessian the rotor-aware treatment and the
  torsions' `treatment_kind` (nullable in the contract) are omitted; the torsions
  themselves (atom quartet, symmetry, scan link) are recorded facts and are
  still sent. One `statmech_treatment_not_stated` warning covers both (context
  `reason: no_freq_hessian`, `inferred_treatment`, `omitted`, `torsion_count`).
  The contract: "an absent field is honest where an invented one would not be".
- **A12, done.** For an `atom_energy` record the adapter maps ARC's
  `reference_atom_energies` (`{unit, applied_as: subtracted, values}`, hartree
  per element) to `scheme.atom_params[]` (`element`, `value`) and sets
  `scheme.units` to the table's own unit. It matches: TCKDB's
  `EnergyCorrectionSchemeAtomParam` is the "element-keyed scalar parameter
  within a correction scheme ... atom_energies", and `scheme.units` is "the
  unit `atom_params` ... are expressed in", converted before comparison. These
  are the scheme's parameters, never per-atom corrections: the total and
  components are ARC's own and are not rebuilt from them. The table is omitted
  (scheme still sent, without parameters) when its unit is missing or not
  `hartree`/`kj_mol`/`kcal_mol`, or when any element or value is unusable. The
  adapter no longer reads `parameter_table` on atom-energy records (ARC writes
  it only on the Petersson record, where it still becomes `bond_params`). The
  benzene fixture now sends 8 atom params in hartree, and the scheme `note`
  records Arkane's full convention (0.6.5; RMG-Py `get_atom_correction`): the
  atomic energies are subtracted and each atom's gas-phase formation enthalpy
  less its thermal correction (`atom_hf - atom_thermal`) is added, so a
  component's contribution is not count x atomic energy (benzene: C 6 x 37.86564
  = 227.19 vs stored 228.82 hartree). ARC records `applied_as: subtracted`. Atom-energy,
  Petersson and Melius schemes also carry `workflow_tool_release` = Arkane (`version` from
  `arkane_version`, `git_commit` from `arkane_git_commit`, each only when
  recorded and, for the commit, at most 40 characters). It is part of scheme
  identity, so a later Arkane build that changes a table value creates a new
  scheme instead of 422ing on the stored parameter; it is the contract's
  "workflow tool whose data file was the proximate source", not a calculation's
  software.
- **Limit of the Arkane stamp.** Arkane's correction tables come from
  RMG-database (`RMG_DB_PATH/input/quantum_corrections/data.py`; ARC
  `arc/statmech/arkane.py` ~817-821), but `arkane_git_commit` is the RMG-Py
  HEAD (ARC `arc/output.py` ~348-366) and ARC records no RMG-database commit.
  So (a) a database-only revision of an atom energy (or bond parameter) for the
  same key still resolves to the stored scheme row and TCKDB refuses it as a
  parameter conflict (422); (b) every new RMG-Py commit creates a new scheme row
  even when the tables are unchanged. Separately, the first 0.6.3 upload of an
  already-deposited scheme creates a new scheme row, because identity now
  includes the workflow tool; the older null-tool row remains. That is
  expected. The durable fix is ARC-side (BRIDGE_ROADMAP.md B12).
- **A1 provenance note.** Arkane skips bond types missing from its Petersson
  table and applies the rest, so an omitted BAC can still be inside the thermo
  H298. The thermo record's `note` (appended, never overwritten) follows
  output 1.2's `thermo.bond_corrections_applied`: `true` says a Petersson BAC
  was applied by Arkane but is not deposited because its bond decomposition was
  incomplete (with the reason); `false` writes no note (the BAC was not applied,
  so nothing is missing from the deposit); absent or null says only that ARC
  exported a Petersson BAC total that is not deposited for that reason,
  without asserting Arkane applied it.

Upgrading to 0.6.3 changes the payload hash (and idempotency key) of uploads
that gain `atom_params`, lose a BAC, or lose a `statmech_treatment`. The
golden corpus hashes are unchanged (its records neither carry a Petersson BAC
or atom-energy table nor lose a treatment).

## Level attribution and TS IRC evidence (adapter 0.6.4)

Four roadmap items, read against the producer contract (`python -m
tckdb_schemas.contract --print`): every calculation requires a
`level_of_theory` (`CalculationWithResultsPayload`, `ComputedReactionCalculationIn`,
`ConformerInBundle.primary_calculation`), and `TransitionStateValidationEvidenceIn`
is `{kind: "irc", passed: bool, rationale: string (length >= 1),
source_calculation_key?}`. Where ARC states no level, the calculation is omitted;
no level is filled from a neighbouring job. Levels that `output.yml` does not carry
are read from the project's **`restart.yml`** (`arc/main.py` `as_dict`, via
`tckdb_arc/adaptive.py`) when it is there: the adaptive levels, `irc_level`,
`conformer_opt_level`, `job_types` and each species' `adaptive_lot_n_heavy` and rotor
scan types. `tckdb-arc-upload` also passes its parsed `input.yml`, which can only say
which job types the adaptive levels name.

- **A2b, `adaptive_levels`: exact where possible.** ARC chooses each job's level per
  species by heavy-atom count, and output.yml records one level per run. With the adaptive
  spec and a species entry in `restart.yml` the adapter replays ARC's rule
  (`scheduler.determine_adaptive_level`, `arc/scheduler.py:5273-5299`): `n` is the
  species' `adaptive_lot_n_heavy` when set (the reaction-wide rule, including the
  `<label>_TS<i>` copies, `arc/scheduler.py:5317-5375`), else the number of non-`H` atoms
  of its geometry; the range is `lo <= n <= hi` (or `hi == 'inf'` and `n >= lo`); the level
  is the one whose key names the job type, exactly (whitespace or commas, no case folding),
  else the run's regular level. Job types: `opt`, `freq`, `sp`, `composite`, `irc`, `scan`
  (ESS rotor scans) and `directed_scan` (a rotor is directed when its `directed_scan_type`
  in `restart.yml` is not `ess`). The attributed level is the calculation's level (the
  program is still the observed one, `ess_software`), and the attributed `sp`/`composite`
  level is the energy level of the enthalpy check, so an adaptive run keeps a formation
  enthalpy when `atom_corrections_level` matches it. When the level cannot be worked out
  (no `restart.yml`, no adaptive spec or species entry there, no atom count, an uncovered
  range, a malformed spec, or only `input.yml`) the previous rule applies: a named `opt`
  refuses the upload, named `sp`, `freq`, `scan`/`directed_scan` and `irc` calculations are
  omitted with `<kind>_level_adaptive_not_attributable` (one warning per kind per upload,
  naming the labels), and a named `sp` or `composite` strips enthalpy
  (`enthalpy_adaptive_levels_unverifiable`; S298 and Cp are kept). With only `output.yml`
  an adaptive run cannot be detected and reads as an ordinary one.
- **A3, TS guess.** ARC exports `neb_level` only when `orca_neb` is in the `ts_adapters`
  the *user* listed (`arc/output.py:180`, `main.py:670`; see BRIDGE_ROADMAP B14 for the
  default-config bug), plus `ess_software.neb` and `ess_versions.neb`. The NEB
  path-search calculation carries `neb_level` and the observed NEB program; it never falls
  back to `opt_level`, and never to `neb_level.software`, which ARC deduces from the method
  (wb97xd/def2tzvp gives gaussian, not ORCA). Without `ess_software.neb` it is omitted with
  `ts_guess_software_not_stated`. A GSM (xtb-gsm) guess has no exported level, and a NEB
  guess without `neb_level` has none either: the calculation and the `optimized_from` edge
  to it are omitted and `ts_guess_level_not_stated` is reported. The golden TS0's GSM path
  search is therefore gone from both TS routes (strip-and-restore proof in
  `test_golden_corpus.py`).
- **A3, IRC level.** ARC runs the IRC at `irc_level` and `restart.yml` records it whenever
  it differs from the settings default (`arc/main.py:480-483`): that level is used exactly,
  with the program ARC's own rule gives (`arc/level.py:413-418`, called from
  `Scheduler.deduce_job_adapter`, `arc/scheduler.py:1255`: an IRC is deduced as
  Gaussian whatever software the level dict names; there is no `ess_software.irc`):
  `{name: gaussian}` with no version, since none is observed and the opt banner is not
  borrowed. The rule is not unconditional (UMA returns early with `ase`,
  `arc/level.py:386-388`; torchani/xtb/gfn methods are assigned after it, `:421-426`), so
  for those the IRC is not filed (`irc_software_not_stated`). `irc_level_assumed_opt_level`
  is not emitted; the TS reference energy for the IRC's
  relative energies is then taken only at that level (levels match on method, basis, aux
  and cabs basis, and now also `dispersion` and `solvation_method`, so an SMD single point
  is never the reference of a gas-phase IRC). When the adaptive levels name `irc`,
  the species' adaptive level wins. With no recorded `irc_level` the IRC ran at
  `default_levels_of_theory['irc']`, which the adapter cannot read; it keeps `opt_level` and
  reports `irc_level_assumed_opt_level`, whose text now says that. ARC's parsers expose no
  level from an IRC log.
- **A4, screened conformers.** ARC screens conformers at `conformer_opt_level`, default
  `wb97xd/def2svp` (`arc/settings/settings.py`), and `write_output_yml` exports no
  conformer level. When `restart.yml` records `conformer_opt_level` with a program,
  `job_types['conf_opt']` is true (otherwise the geometries are force-field ones) and the
  adaptive levels do not name `conf_opt` for the species' range, the alternative conformers
  whose `conformer_energies[i]` is not null are filed as bare opts at that level and program
  (no banner version is borrowed from the selected conformer's opt). A null energy means
  that conformer's `conf_opt` never finished, so `conformers[i]` is still the force-field
  geometry (`arc/scheduler.py:3133-3140`, exported whole by `arc/output.py:1748-1751`); a
  missing or misaligned energies list leaves all unverified. Known gap: when no conformer
  converged ARC re-runs all of them at a troubleshooting level
  (`arc/scheduler.py:5050-5078`, `ess_trsh_methods` `'conf_opt: <level>'`) while
  `restart.yml`'s `conformer_opt_level` stays unchanged; that troubleshooting lives only in
  the job objects (kept in `restart.yml` only while running) and is recorded nowhere durable,
  so the adapter cannot tell and the level may then be wrong. Otherwise they are omitted and each species with distinct
  ones reports `conformer_level_not_stated` (`omitted_count`).
- **A14, IRC validation evidence.** `ts_checks['IRC']` (`arc/output.py`
  `_ts_checks_to_dict`) is the verdict; `irc_converged` only means the IRC jobs finished
  and is never used. A bool becomes `validation_evidence` with `passed` = the verdict and
  the rationale exactly `ARC ts_checks['IRC'] = <verdict>`: `ts_checks['warnings']` come
  only from the e_elect and NMD checks (`arc/checks/ts.py:175`, `arc/checks/nmd.py:93-131`),
  never the IRC. `None` or no `ts_checks` sends nothing, so
  `transition_state_missing_irc_evidence` stays. The reaction bundle binds the evidence to
  the `ts_irc` calculation; the standalone route omits the key. A verdict with no IRC
  calculation in the upload is not sent (`ts_irc_evidence_without_irc_calculation`).
  Participant mappings stay omitted (ARC discards them, roadmap B10).

Upgrading changes the payload hash, and so the idempotency key, of every TS upload
with a GSM guess and of species uploads whose screened-conformer or level attribution changed.

## Energy level, SCF stability and conformer statmech (adapter 0.6.6)

- **A11, the declared energy level.** Thermo and statmech (computed-species, reaction
  participants and, for statmech, conformer mode) send `energy_level_of_theory`. It is the
  `level_of_theory` of the linked energy calculation exactly as the adapter sends it (the
  sp, or the opt when no sp was built), `spin_treatment` included, and only when ARC's
  stated energy level (`_thermo_energy_level`: composite method, else `sp_level`, else
  `opt_level`, attributed per species under `adaptive_levels`) is that calculation's level
  apart from `spin_treatment`. TCKDB hashes every `LevelOfTheoryRef` field into the level
  identity, with a NULL `spin_treatment` folding to `unknown`
  (`calculation_resolution._level_of_theory_hash`), and checks a declared level against the
  linked sp, else the linked opts (`calculation_levels.assert_role_consistency`); the adapter
  stamps `spin_treatment` on the freq/sp level from `scf_reference`, so declaring ARC's bare
  level would be `thermo_/statmech_energy_level_contradiction`, a 422. Nothing is declared
  for a composite method (no composite calculation is sent), when the level ARC states is
  another than the linked calculation's, when an adaptive run left it unattributable, or when
  no sp/opt is linked. The unit tests replay the backend's identity hash and link rule
  (`tests/_backend_level_rules.py`, checked against the backend when `TCKDB_BACKEND_PATH`
  names its `backend/`); the live gate has a case, not run here.
- **A10, `scf_stability`.** ARC runs the stability analysis once per species, from the
  optimization job: at the opt level, on the converged geometry, with the opt's own orbitals so
  its SCF reproduces the wavefunction under test (`arc/scheduler.py::run_stability_job`). The
  block therefore goes on the primary opt calculation, and only there: a freq or sp job runs
  its own SCF, which may land on another solution, so the analysis is not an observation of
  theirs (nor of a coarse opt, scan or IRC). It is not sent when ARC's record shows the opt
  sent is not the tested one: `scf_reference.source == 'derived'` (ARC re-optimized at another
  reference) or `measured_on_ts_guess`. `stable` needs ARC's `stable` verdict;
  `internal_instability`, `external_instability` and `unattributed_instability` are
  `unstable` (the first two with `instability_type` `internal` / `external`); `unknown` (an
  analysis ran, no readable verdict) is `inconclusive`. ARC's `lowest_eigenvalue` is sent when
  numeric. Not sent: an instability count (ARC exports none), `stabilized` for ORCA's
  `followed_to_stable` (the tested wavefunction stayed unstable; unconfirmed with ARC) and
  `reoptimized_wavefunction`.
- **A13, conformer mode.** `ConformerUploadRequest` now carries `statmech` and
  `applied_energy_corrections`, built by the computed-species route's builders: Hessian-gated
  `statmech_treatment` and torsion `treatment_kind`, BAC omission, `atom_params`, the Arkane
  release on the statmech and the schemes, the declared energy level, and ARC as the
  statmech's `workflow_tool_release` (the bundle names ARC once at its root; this request has
  no such slot). The calculations get local keys `opt`, `freq`, `sp`, which the statmech links
  and the corrections' `source_calculation_key` name. Rotor scans are not sent: the route
  accepts only `freq` and `sp` as additional calculations, so each torsion drops its scan link
  (`torsion_scan_not_built`). The conformer route's `StatmechTorsionIn` refuses a torsion
  without dihedral coordinates, so a treated rotor without usable `atom_indices` is not sent
  (`torsion_not_sent`); the bundle routes, which accept one, now report it (`torsion_without_coordinates`). ARC's `statmech.rejected_torsions` (`success is False` rotors only)
  become torsions with `invalidated_reason` (ARC's text verbatim, or "ARC rejected this rotor
  and recorded no reason" for an empty one), the coordinates, `dimension`, ARC's `rotor_index`
  in `note`, and a `torsion_index` after the treated rotors'; no treatment or symmetry number
  (ARC states none), and a rejected rotor without usable atoms is not sent
  (`rejected_torsion_not_sent`). Only conformer mode has a home for them (the bundle torsion
  models have no `invalidated_reason`), and a rejected rotor never counts toward
  `statmech_treatment`. There is no thermo slot on this route.

Upgrading changes the payload hash, and so the idempotency key, of every conformer upload
with statmech or corrections, and of computed-species and reaction uploads with thermo or
statmech.
