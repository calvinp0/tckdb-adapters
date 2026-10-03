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
- **Adapter 0.6.7:** roadmap A6, A8, A15, A16, A17 (via restart.yml) and A19. See
  [Reaction species, atoms and artifacts (adapter 0.6.7)](#reaction-species-atoms-and-artifacts-adapter-067).
- **Adapter 0.8.0 (ARC output schema 1.3), batch E:** output.yml 1.3 levels, programs, conformers, IRC endpoints,
  composite job, isotopes and routes; roadmap A2b, A3, A4, A17, B2, B3, B6 and B7 consumed. See
  [Levels, programs and isotopes (adapter 0.8.0, batch E)](#levels-programs-and-isotopes-adapter-080-batch-e).
- **Adapter 0.8.0, batch F:** corrections, statmech, kinetics provenance,
  TS frequencies and parsed properties. See
  [Output schema 1.3: corrections, statmech, kinetics, TS frequencies (adapter 0.8.0, batch F)](#output-schema-13-corrections-statmech-kinetics-ts-frequencies-adapter-080-batch-f).
- **Adapter 0.8.0, batch G:** reaction atom map and IRC participant mapping. See
  [Reaction atom map and IRC participant mapping (adapter 0.8.0, batch G)](#reaction-atom-map-and-irc-participant-mapping-adapter-080-batch-g).
- **Adapter 0.8.0, batch H (tckdb-schemas 0.59 to 0.64):** atoms send their real `sp`, scheme
  `data_revision`, kinetics `t0_k`, standalone TS scans and corrections. See
  [tckdb-schemas 0.59 to 0.64 (adapter 0.8.0, batch H)](#tckdb-schemas-059-to-064-adapter-080-batch-h).
- **Adapter 0.8.1, batch I:** TS `imaginary_mode` and `energy_ordering` validation evidence (tckdb-schemas 0.64), from ARC's
  `ts_checks['freq']` / `ts_checks['e_elect']` and the stated frequencies and energies. See A14 (below).
- **Adapter 0.9.0, batch J (ARC output 1.3 at PR #1059 head `ebc88ec8`):** per-occurrence reaction species labels, the TS
  atom map as TCKDB's `atom_map` on both routes, `nmd_forced`, observed conformer programs, `bac_type` check. See
  [Output schema 1.3 at ebc88ec8 (adapter 0.9.0, batch J)](#output-schema-13-at-ebc88ec8-adapter-090-batch-j).
- **Adapter 0.10.0, batch K (tckdb-schemas 0.73, client 0.111, TCKDB `96b71b09`):** public refs on artifact targets,
  the frequency level on BAC schemes, the `//` and correction-table method guards, a faithful replica of the
  level-of-theory identity hash, and a G4 / G4MP2 Gaussian 16 Rev A.03 stopgap. See
  [tckdb-schemas 0.65 to 0.73 (adapter 0.10.0, batch K)](#tckdb-schemas-065-to-073-adapter-0100-batch-k).
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
| Arrhenius `A`, `T0_k`, `n` | Modified Arrhenius prefactor | Normalized `a = A / T0_k**n` until batch H; since tckdb-schemas 0.63 `a = A` and `t0_k = T0_k` (see the batch H section); rate-equivalence tests span temperatures and exponents |
| Accepted-upload warnings | Sidecar, outcome, log | Structured warnings exposed explicitly; previously already present inside full JSON response bodies |

Raw-log fallback now requires the Hessian parser's matching frame geometry;
it omits matrices when that geometry is unavailable. Raw GSM fallback retains
geometries and stringfile relative energies, but does not attach archived
invocation energies/gradients by node-number arithmetic. Portable evidence is
the supported path for geometry-matched absolute values.

ARC/RMG evaluates `A*(T/T0)**n*exp(-Ea/RT)`. Until tckdb-schemas 0.63 TCKDB evaluated
`a*T**n*exp(-Ea/RT)`, so the adapter sent `a = A/T0**n` and lost the T0; since 0.63 (adapter 0.8.0,
batch H) it sends `a = A` and `t0_k = T0_k`. Multiplicative `dA` is unchanged either way.

This changes the payload, and so the idempotency key, of every rate whose T0 is not 1 K, which is
every ARC rate (T0 = 300 K). TCKDB kinetics rows are append-only, so a rate deposited by adapter
0.7.x or earlier (stored as `a = A/300**n`, `t0_k = 1`) and the same rate deposited by 0.8.0
(`a = A`, `t0_k = 300`) are two rows for one physical rate. They evaluate identically through
TCKDB's `a_at_unit_t0`, so the duplicate is harmless to values but is not merged.
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
| Atoms got a primary opt that never ran (`converged: false`, or `true` in composite runs, `arc/scheduler.py:3441`) | **Done in batch H** (tckdb-schemas 0.59, TCKDB#610): an atom's primary is its real `sp`; no placeholder, no warning | A6, C6 |
| Current ARC rotor export has results but no scan level/software | ARC must export scan provenance. The adapter accepts an explicit `scan_level`; missing provenance is reported, not replaced with `opt_level`. See `arc/output.py::_build_rotor_scan_entry` and `arc/scheduler.py::run_scan_jobs` | B3 |
| Standalone TS endpoint has no `scan_result` slot | **Done in batch H** (tckdb-schemas 0.64 accepts `scan` and `scan_result` there): the standalone request carries TS scans | C9 |
| TS statmech has no bundle field | TCKDB 0.64 added `BundleTransitionStateIn.statmech`; the adapter does not send it yet (a decision is needed on ARC's TS `statmech` record) | C8 |
| Full tunneling and partition-function provenance | TCKDB 0.63 added `tunneling_application`, `interpretation_assignments` and `network_kinetics_ref` to `BundleKineticsIn`, but every reference in them is a record deposited earlier and a bundle cannot cite the TS or statmech it creates, so the adapter cannot send them. The tunneling label is still ARC's | C8 |
| Transport | **Corrected.** ARC has no working transport path to export: no `onedmin` job adapter is registered, the processor's transport step is a `# todo` (`arc/processor.py:237`), and `transport_data` is not persisted. TCKDB *does* have homes at 0.51 (`conformer_upload.transport` and `POST /uploads/transport`). So this is new ARC capability first; the adapter follows | B11 |
| `wavefunction_stability` | **Corrected:** ARC exports it (`arc/output.py:1841-1842`, tested). **Done in 0.6.6:** mapped to `scf_stability` on the primary opt (the wavefunction ARC tests), never on freq/sp | A10 |
| `ts_checks` | **Corrected:** ARC exports the verdicts since output 1.1 (`arc/output.py:1986`, `:2779-2805`). The gaps are in the adapter and TCKDB. The adapter maps `ts_checks.IRC` (never `irc_converged`, which only means the IRC jobs finished) to `validation_evidence` (done in 0.6.4). TCKDB 0.64 added the `energy_ordering` and `imaginary_mode` kinds; adapter 0.8.1 (batch I) maps `ts_checks.freq` to `imaginary_mode` and, on the reaction bundle only, `ts_checks.e_elect` to `energy_ordering` (A14, C7). No real-ARC fixture sets `ts_checks.IRC` | A14, C7 |
| Execution environment and effective calculation settings | ARC export is incomplete, especially beyond coarse/fine optimization settings. Do not invent runtime metadata from requested input settings | B11 |
| NEB portable evidence, alternative TS guesses, multidimensional rotor scans | Producer evidence/export work; the current portable sidecar covers Hessian/IRC/GSM, successful 1D rotors and the chosen guess | B11 |
| Deposit rights | Requires explicit depositor information/configuration; no license or consent is inferred. Silent at upload, but a dataset release refuses records without a rights basis | D2 |
| Whether Arkane applied atom-energy corrections | Exported by ARC output.yml 1.2 (`thermo.atom_corrections_applied` / `atom_corrections_level`, PR #1059 branch `feature_export_atom_corrections_applied`, not yet on ARC main) and consumed by the adapter. Pre-1.2 output (`db0934d5`, ARC main today) cannot say so. An `energy_corrections[]` `atom_energy` row does **not** prove the corrections were applied: it is keyed on the energy level alone, and Arkane's model chemistry can be `None` when the freq level is not found and no `freq_scale_factor` was given (`arc/statmech/arkane.py:1138-1157`). The row's absence proves nothing either. At `0913124`, such output gets only the ±2.0e4 kJ/mol magnitude guard, which misses H, H2, He and Li. **Decided (option d); committed as `84b1b05` on `fix_unverifiable_enthalpy_without_flags` (adapter 0.5.0), pending merge:** when the flag is absent, strip the enthalpy for light species by composition, taken from the species' xyz or else its `formula` (hydrogen-only species, He, He2, HeH, the Li atom), when the header `arkane_level_of_theory` does not match the energy level, and when either level sets a separate `dispersion` or `solvation_method`. Composition uses the xyz, else the record's `formula`, because ARC 1.0 writes `xyz: null` for monoatomics. The header `arkane_level_of_theory` is written by ARC `db0934d5` as a required, nullable level dict; a null header, or one without a method, is not checked. These interim rules are superseded by output.yml 1.2's switch (ARC PR #1059) for new runs. Residual risk: a stand-in with the same method and basis that differs by a year refit or by ARC's fuzzy key match. In the golden corpus only H2 loses its enthalpy. The `feature_tckdb_integration_gate` test `test_golden_species_calculations_thermo_and_hessian` asserts golden H2's `formation_298k` and NASA fit, so whichever branch merges second must update it | A2, B1, D1 |
| Per-species energy level | ARC must export the level each species' energies were computed at. Under `adaptive_levels` every calculation is attributed to the run-level level (**wrong data**), and the adapter's comparison with `atom_corrections_level` can pass wrongly. `output.yml` does not record adaptive runs, but the adapter can detect them from the project's `input.yml` (which the CLI already reads) or `restart.yml` (ARC saves `adaptive_levels` there, `arc/main.py:439`). The interim (adapter 0.6.4) attributes exactly from `restart.yml` where it can and otherwise refuses or strips affected calculations, until ARC exports per-species levels | A2b, B2 |
| Atom-energy matching ignores dispersion and solvation | **Fixed in PR #1059 (`16eec7af`), pending merge.** ARC's Arkane key match and `data/AEC.yml` lookup used to ignore `dispersion` and `solvation_method`: B3LYP + GD3BJ got plain B3LYP atom energies and SMD got gas-phase ones, yet `atom_corrections_level` equalled `sp_level`. The PR (rebased onto ARC `d9f47ab9`) fixes the match. Until it merges, and for output from an ARC without it, the adapter strips enthalpy when either level sets either field (`enthalpy_atom_corrections_level_unverifiable`). Follow-up, not done: for 1.2 output from an ARC that has the fix, that rule could be relaxed | B1 |
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
- **A14 / C7, the two 0.64 kinds (adapter 0.8.1, batch I).** Each is sent only from what ARC states, and each
  record is left out, with a warning, rather than sent where TCKDB would refuse it.
  - `imaginary_mode`, from `ts_checks['freq']` (a bool; `None` or no `ts_checks` sends nothing) and the TS
    frequency result the same upload sends: `imaginary_frequency_count` is its `n_imag` (`freq_n_imag`);
    `imaginary_frequency_cm1` is the designated reaction-coordinate mode's frequency (1.3
    `reaction_coordinate_mode_index` into `freq_frequencies_cm1_ess_order`, else the single imaginary mode's
    `imag_freq_cm1`; written negative); `mode_displacement_agrees` is `True` only when ARC states
    `reaction_coordinate_mode_index` (ARC sets it only for a genuine, non-forced normal mode displacement
    pass), `False` when `ts_checks['NMD']` is `False` and no index is stated or when `nmd_forced` is `true` (ARC forces
    only a check that ran and failed), and omitted otherwise (an unrun or undecided check). It is read from the result, not from the raw record,
    because TCKDB refuses a count that differs from that result or a frequency more than 1 cm-1 from it, and a
    passing record with several imaginary modes unless the result designates the coordinate: with several modes
    and no designation the record is not sent (`ts_imaginary_mode_evidence_not_sent`, as it is with no freq
    calculation in the upload). Both routes; the bundle binds `ts_freq` by key, the standalone route omits the
    key and binds to its single freq calculation.
  - `energy_ordering` (bundle only: the standalone route refuses it), from `ts_checks['e_elect']`. ARC's check
    (`arc/checks/ts.py::check_rxn_e_elect`) is the TS electronic energy above both wells by more than
    1 kJ/mol, summed over the participants; it leaves `e_elect` unset whenever the `E0` check passed, and
    `passed` is required, so `None` sends nothing. Only electronic energies are sent: each participant's
    `sp_energy_hartree` (the TS and every reactant and product, `reactant:N` / `product:N` in the order of
    `reactant_keys` / `product_keys`, a repeated species repeated) cited to that participant's own `sp`
    calculation (an atom's is its sp primary). `e0` is never sent: ARC's `e0_kj_mol` carries corrections and is
    not one calculation's absolute energy; ARC's `E0` verdict appears only in `rationale`. TCKDB refuses a pass
    its stated numbers do not support (strictly above each side summed) and a positive energy, and a passing
    record needs every participant, so a `True` verdict the stated hartree values contradict (ARC's 1 kJ/mol
    margin), a participant with no finite non-positive `sp_energy_hartree`, or one with no `sp` calculation in the
    upload, leaves the record out with `ts_energy_ordering_evidence_not_sent`. A `False` verdict is re-derived the same way: one the
    stated numbers do satisfy (TS above both sides by ARC's margin) was computed on stale energies and is left out with the same
    warning (context `reason: verdict_contradicted_by_stated_energies`); otherwise it is sent as stated.
  - Neither kind silences `transition_state_missing_irc_evidence`; only a passing `irc` record does.

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
  (`tests/_backend_level_rules.py`, whose hash is now `tckdb_arc/level_rules.py` since 0.10.0, checked
  against the backend when `TCKDB_BACKEND_PATH` names its `backend/`, which CI sets); the live gate has a case, not run here.
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
## Reaction species, atoms and artifacts (adapter 0.6.7)

- **A species on both sides is deposited once (A16).** The computed-reaction bundle
  declares one species block per distinct ARC species label; a repeated label (`H + H`, or
  `H2 + H <=> H + H2`) repeats the key in `reactant_keys` / `product_keys`. The
  kinetics links `reactant_energy` / `product_energy` point at that species' sp, once per
  role. The golden `computed_reaction` hash changes; restoring the duplicate blocks
  reproduces the previous one.
- **Single-atom species kept a placeholder opt and warned (A6); superseded in batch H.** ARC
  never optimises an atom, but until tckdb-schemas 0.59 the computed-species and computed-reaction
  routes needed an opt primary, so the atom was uploaded with that opt (`converged` as ARC reports it)
  and `monatomic_species_primary_opt_placeholder` was reported (TCKDB#600). TCKDB#610 (0.59) accepts
  an `sp` primary for a one-atom conformer, and the adapter now sends that, see the batch H section.
- **Artifact batch keeps the response (A8).** Each calculation's artifact batch is sent
  through `request_json`, so the artifact sidecar and `ArtifactUploadOutcome` carry the
  server's `warnings`, the status code, the `artifact_upload` request id and the replay
  flag.
- **Smaller gaps (A15).** A scan calculation carries its log (`rotor_scans[].source_log`);
  a reaction conformer carries `label`. Skipped: Melius tables (needs an ARC export),
  `climbing_image_index` (ARC does not state it), bundle-level ESS release (needs a rule),
  thermo-level corrections (low value), scan constraints (the contract puts a scan's
  frozen coordinates at calculation level).
- **Dead reads removed (A19).** `unmapped_smiles`, `reactions[].reversible`,
  `kinetics.degeneracy`, `kinetics.note`, `sp_spin_diagnostic.note`, `irc_final_settings`,
  `thermo.cp_data` and the `electronic_energy_hartree` sp fallback are not exported by ARC
  and are no longer read.
- **IRC endpoint species are skipped via restart.yml (A17).** ARC writes `irc_label` to
  `restart.yml` only. A species whose restart entry is not a TS and names an `output.yml`
  TS (which lists it back) is not uploaded, with `irc_endpoint_species_skipped`. Without
  `restart.yml` nothing is skipped. B6 (an `output.yml` marker) is still wanted.
- **1.0 documents (A19).** `thermo.cp_data` and the atom-energy `parameter_table` are
  still read when `schema_version` is `1.0`.

## Levels, programs and isotopes (adapter 0.8.0, batch E)

Output.yml 1.3 (ARC PR #1059) states what 0.6.4-0.6.7 had to infer. Where a 1.3 record states a
value the adapter uses it; where it states nothing the 1.2 behaviour (header levels,
`restart.yml`) is the fallback, and for a 1.3 record a job whose level it leaves null is never
filled from a run-level header level when the run is adaptive or the job has no exported log.
The version gate (`evidence.SUPPORTED_OUTPUT_SCHEMA_VERSIONS`) now accepts `1.3`.

| ARC 1.3 key | TCKDB destination | Behaviour |
|---|---|---|
| `levels.{opt,freq,sp,composite,irc}` | `calculation.level_of_theory` | The record's own level, authoritative under `adaptive_levels`; replaces the `restart.yml` replay (`arc13.recorded_level`, `adapter._resolve_level`). `sp` pairs with `opt` when the energy is read from the opt log (`sp_log` absent or equal to `opt_log`; marked `reused_result`) and with `composite` on a composite run. |
| `ess_software[job]`, `ess_versions[job]` | `calculation.software_release` | The only program source (levels inside a record state no `software`). A job with no observed program is not filed at a deduced one. |
| header `scan_level` + `rotor_scans[].ess_software` / `ess_version` | scan `level_of_theory`, `software_release` | A scan with no stated program is not filed. A header `scan_level` left null by an adaptive `scan` entry omits scans (`scan_level_adaptive_not_attributable`). |
| `levels.irc` / `irc_log_levels`, header `irc_level`, `ess_software.irc` | irc calculation | See A3 in the roadmap; no `irc_level_assumed_opt_level` for 1.3. |
| `gsm_level`, `ess_software.gsm`, `ess_versions.gsm` | `path_search` (method `gsm`) | Filed again. The xtb banner `xtb version X (hash)` becomes version `X`, build `hash`. |
| `conformer_levels`, `conformer_energy_kind`, `conformer_energy_level`, `conformer_force_field`, `conformer_opt_level`, `conformer_sp_level` | alternative conformers | See A4. Force-field geometries are not calculations (omitted, `conformer_geometry_not_esss_optimized`). |
| `irc_endpoint_of`, `irc_endpoint_direction` | (species skipped) | `irc_endpoint_species_skipped`; authoritative both ways. |
| `composite_log`, `composite_input`, `composite_route`, `levels.composite`, `ess_*.composite` | primary `opt` calculation + `composite` role | TCKDB has no composite calculation type (see B7). The composite log and deck are its `output_log` and `input` artifacts. |
| `xyz_isotopes`, `conformers_isotopes`, `*_input_xyz_isotopes`, `*_output_xyz_isotopes`, sample `geometry_isotopes` | `geometry.isotopes` | Substituted atoms only (below). |
| `opt_route`, `freq_route`, `sp_route`, `composite_route`, `irc_log_routes` | `calculation.parameters[]` | `{raw_key: "route", raw_value: <line>, section: <job>, value_type: "string"}`; IRC lines with `parameter_index` when the two jobs differ. |
| `freq_scale_factor` with `levels.freq` | `statmech.freq_scale_factor` | Attached with its own (header) level and program, as Arkane applies the one run-wide factor to every species; `freq_scale_factor_fitted_for_other_level` when the record's `levels.freq` differs. |

**Isotopes.** TCKDB's `GeometryPayload.isotopes` maps a 1-based atom index to a mass number and
"only substituted atoms need an entry; every unlisted atom is taken to be at its most abundant
natural isotope"; `species_geometry_isotope_mismatch` refuses a geometry whose substitutions
differ from those the species entry's SMILES declares. The adapter sends ARC's list as stated,
dropping atoms at the standard mass, and builds nothing when ARC's stated isotopes and the
record's SMILES disagree (`ValueError`, naming `species_geometry_isotope_mismatch`); it never
rewrites the SMILES or drops a stated substitution. For a substituted species a geometry whose
isotopes ARC did not state (an input geometry from a log that states no masses, a conformer
with a `null` list, a scan sample) is left out rather than deposited as an unsubstituted one
(`geometry_isotopes_not_stated` for conformers). Not covered: geometries parsed from logs by the
adapter itself (IRC points, path-search images, the Hessian frame) carry no isotopes; whether
TCKDB applies the isotope check to them is not stated in the contract.

**Placeholders and contradictions.** A composite run's primary `opt` (composite level as ARC states
it) and the primary `opt` of a record with no opt job (header opt level) are marked
`tckdb_origin.origin_detail = placeholder_primary_opt_{composite,no_opt_job}` with warnings
`composite_geometry_level_not_stated` / `primary_opt_placeholder_no_opt_job`. A calculation whose
recorded level a Gaussian route clearly contradicts is not built (`level_contradicted_by_route`).
The conformer program is a deterministic deduction (header `conformer_opt_level.software`), interim.
The xtb banner split is a local rule pending a TCKDB normaliser change (roadmap C13).

**No home in the contract.** `ts_guess_level` (the level of the TS-guess optimization/comparison),
`conformer_sp_level` beyond the conformer single-point case, the composite job as a calculation
*type*, a per-keyword decomposition of the route lines (the observation is the whole line), and
the program of conformer jobs (taken from the header `conformer_opt_level`/`conformer_sp_level`,
which are requested levels with a deduced software).

Upgrading changes the payload hash, and so the idempotency key, of every 1.3 upload with a
route line (each calculation gains `parameters`), a stated isotope, a composite run, a GSM guess
or a changed level; pre-1.3 output is byte-identical.

## Output schema 1.3: corrections, statmech, kinetics, TS frequencies (adapter 0.8.0, batch F)

`evidence.SUPPORTED_OUTPUT_SCHEMA_VERSIONS` gains `1.3`. Every 1.3 key below is read from a
document whose `schema_version` is 1.3 or later (`_is_output_schema_1_3_or_later`), and only
there: an earlier document never states them (A19), so the same record relabelled `1.2` gives
the pre-1.3 payload. Fixtures: `tests/fixtures/arc_1_3_thermo` (written by ARC's real writer,
with a partial BAC, a null torsion treatment, a symmetric top and a two-imaginary-mode TS) and
`tests/fixtures/arc_1_3_samples` (ARC's own hand-off documents; their numbers are placeholders).
Tests: `test_arc_schema_1_3_thermo.py`, `test_arc_1_3_samples.py`.

| 1.3 key | Payload field | Rule |
|---|---|---|
| `energy_corrections[].components` (Petersson: the bonds applied), `skipped_components` | `applied_energy_corrections[].components`, correction `note` | A partial BAC is sent. The components must sum to `total` within 1e-3 kcal/mol (`components_do_not_sum`); skipped bonds are named in the correction `note`, never in the scheme. Omitted, as before, with no usable bond component, an unusable component or no component on a bonded species or a TS |
| `statmech.arkane_treatment` | `statmech.statmech_treatment` | Sent as stated, with no freq Hessian gate and no inference from the rotor list. `null` omits it (`statmech_treatment_not_stated`, reason `arkane_treatment_not_recorded`); a rotor treatment with no torsion sent is withheld (`no_torsion_sent_for_rotor_treatment`) |
| `statmech.torsions[].treatment` (nullable) | `torsions[].treatment_kind` | As recorded; a null treatment sends the torsion without one, never `hindered_rotor` |
| `statmech.rigid_rotor_kind` (adds `symmetric_top`, `spherical_top`, null) | `statmech.rigid_rotor_kind` | As stated; null is omitted |
| `statmech.e0_*_applied`, `arkane_rotors_applied` | none | No TCKDB statmech field for E0 or a rotor count, and the adapter deposits no E0 |
| `kinetics.comment`, `ts_validation`, `atom_corrections_applied` | kinetics `note` | After the reaction description, one line each: Arkane's comment verbatim, the `ts_validation` text when the comment does not already carry it, and `Arkane kinetics run atom energy corrections: applied.` / `not applied (E0 values are absolute electronic energy plus ZPE).` |
| `reactions[].reversible` | computed-reaction `reversible`; standalone TS `reaction.reversible` | A stated bool is sent. A null omits it on the bundle (the schema default, `true`) and sends `true` on the TS route (required, no default; the 0.6.0 maintainer decision) |
| `rmg_database`, `arc_aec_yml_sha256` | `scheme.data_revision` (batch H; `scheme.workflow_tool_release` in the first draft of batch F) | See below |
| TS `freq_frequencies_cm1_ess_order`, `reaction_coordinate_mode_index` | `freq_result.modes` and `reaction_coordinate_mode_index` (flat: `freq_frequencies_cm1`, `freq_reaction_coordinate_mode_index`) | See below |
| `sp_t1_diagnostic` | the sp calculation's `wavefunction_diagnostic.t1_diagnostic` | Finite and non-negative only |
| `sp_spin_diagnostic.s_squared_expected`, `s_squared_annihilated` | `spin_diagnostic` | Already mapped before 1.3; unchanged |
| `freq_hessian_method` | freq calculation `parameters` (`freq.hessian_method`) | Already mapped; unchanged |
| `opt_dipole_moment_debye`, `opt_dipole_moment_density`, `freq_polarizability_angstrom3` | none | See below |

**Correction-scheme identity (B12; superseded by `data_revision` in batch H, see the batch H section).
This paragraph describes the first draft of batch F, which put the table in a `RMG-database`
`workflow_tool_release` because tckdb-schemas 0.61 had no field for it.** The producer contract's `WorkflowToolReleaseRef` is
`{name, version, git_commit (1-40 characters), release_date, notes}`; `WorkflowToolReleaseIdentity`,
what identifies a release, is `(name, version, git_commit)` only, so `notes` never separates two
tables. With a usable `rmg_database` the scheme's release is `RMG-database` with the table's
identity in an identifying field: `git_commit` for `path_kind: git`, `version` for `package`, and
`version: sha256:<digest>` for a path of unknown origin. The digest, the path kind, the package
version or commit, the Arkane version and commit that looked the table up, and a
`matches_arc_rmg_db_path: false` remark are repeated in `notes`. This replaces the Arkane build on
atom-energy, Petersson and Melius schemes: a database revised in place now gives a new scheme
instead of a parameter-conflict 422, and an unrelated RMG-Py commit no longer forks one. A
`git`/`package`/`unknown` block with none of commit, version or a 64-hex digest, a commit over 40
characters, an absent block or a document before 1.3 keep the Arkane build (0.6.3 behaviour).
`arc_aec_yml_sha256` is not sent as a release. ARC renders Arkane's `atomEnergies` from `data/AEC.yml`
whenever `_match_aec_yml_key` matches (even if `data.py` also matches, `arkane.py:410-419`) but recomputes
the exported `atom_energy` record from `data.py` (`get_species_corrections.py:92`), so for a level both
files cover the record would describe a table other than the one applied. When the digest is non-null
the `atom_energy` record is therefore not deposited
(`atom_energy_record_not_deposited_aec_yml`); the Petersson/Melius schemes are unaffected. A
safety net today (AEC.yml holds only gfn2/torchani levels). Energies that came only from `AEC.yml`
write no record at all (ARC docs: the list "is empty when the corrections came from ARC's own
`data/AEC.yml` rather than Arkane's database").

**Scheme identity sets (history).** Deposited scheme rows have two identities for the same table
content: no tool (<= 0.6.2) and the Arkane build (0.6.3-0.6.9). The third set the first draft of batch F
described, an `RMG-database` tool release (0.8.0), was never deposited: it existed only on an unmerged
branch, so no row carries it. Adapter 0.8.0 as released identifies a scheme by `data_revision`
(tckdb-schemas 0.62), a deposit with a revision never matches one without, and each table's first
upload is a new row.

**TS frequencies and the reaction coordinate (B9).** The producer contract: `freq_result.modes[].mode_index`
is the "1-based ordering from the ESS output", and `reaction_coordinate_mode_index` is the "`mode_index`
of the mode the depositor designates the reaction coordinate"; a TS with more than one imaginary mode
"is accepted only if it says which one". On a 1.3 TS the modes are `freq_frequencies_cm1_ess_order`,
numbered by position (imaginary modes in place, no re-insertion that would shift ARC's index), and the
designated mode is ARC's `reaction_coordinate_mode_index` when it names an imaginary listed mode (sent
for one imaginary mode too: it is ARC's normal-mode-displacement verdict). Every other imaginary mode is
declared `unassigned`. With no usable index (ARC's is null whenever the normal mode displacement check did not genuinely
pass, including `skip_nmd` runs) TCKDB's own noise floor decides: `tckdb_schemas.stationary_point`
`TAU_PROTOCOL_NOT_RECORDED_CM1 = 50` cm-1 (ARC states no tau). Exactly one imaginary mode at or above it
is designated and the others, below it, are `unassigned` (the contract's `ImaginaryModeDisposition` has no
below-tau value; TCKDB warns `transition_state_extra_imaginary_modes_below_tau`). Two or more at or above
tau, or none, is refused with `TSReactionCoordinateNotDesignated` (a `ValueError` whose message starts
with `[ts_reaction_coordinate_not_designated]` and which carries the structured `.warning`; with no outcome
there is no sidecar, so the sweep's failure line is where it shows). The (75, 10000) cm-1
window and the re-insertion remain for documents before 1.3. A null index with one imaginary mode needs
no designation and is uploaded without one. ARC's null index does not say why (README open question 2: a
forced normal-mode check cannot be told from an unrecorded frequency); the adapter treats every null the
same, as "not established".

**Parsed properties.** `sp_t1_diagnostic` has a home on `CalculationInBundle` and the reaction and
standalone TS calculation models: `wavefunction_diagnostic` (`t1_diagnostic >= 0`). ARC states it only
for a coupled-cluster or QCISD sp level. The dipole moment and polarizability exist in the contract only
as `dipole_debye` and `polarizability_angstrom3` on the standalone `TransportUploadRequest`
(`POST /uploads/transport`, with Lennard-Jones, one record per species entry, calculation roles
`dipole`/`polarizability`), which the adapter does not call; on the computed-species, conformer,
computed-reaction and transition-state routes they have no home and are not sent.

**E0 and the e0 switches.** The adapter sends no `statmech.e0_kj_mol` (TCKDB's statmech has no such
field) and no `enthalpy_formation_0k_kj_mol`, so nothing decides formation-ness for an E0-only species or a
TS E0, and the switches `e0_atom_corrections_applied` / `e0_bond_corrections_applied` are not consumed.
A well's E0 can carry a BAC that its TS's does not, and a TS E0 is never a formation enthalpy; a future
0 K deposit must read them and still compare levels, which the statmech block does not state.

**Thermo flags unchanged.** `atom_corrections_applied`, `bond_corrections_applied` and
`atom_corrections_level` have the same shape in 1.3 as in 1.2 (a level dict, the cross-field rules the
JSON schema enforces), and the thermo block built from the same record is identical under both labels
(test). Energy-correction records now follow the run's switches in 1.3 (dropped when the switch is false),
so a 1.3 species whose atom corrections were off carries none; the adapter forwarded what ARC exported and
had no gate that assumed otherwise. Before 1.3 ARC kept the record of a correction its thermo run did not apply, so the adapter now drops an
`atom_energy` / `bond_additivity` record when the same species' `thermo.atom_corrections_applied` /
`bond_corrections_applied` is false.

**README open questions touching these items** (ARC hand-off notes; Q2 to Q6 were answered by ARC `ebc88ec8`, see
[batch J](#output-schema-13-at-ebc88ec8-adapter-090-batch-j), which supersedes the Q2, Q4, Q5 and Q6 rows of this
paragraph where the new keys are present): (2) a null
`reaction_coordinate_mode_index` as above; (5) `bond_corrections_applied: true` is not linked to a
requested `bac_type` by the schema, and the adapter does not use `bac_type`: the switch only words the
omitted-BAC note; (6) a null `standard_state_pressure_pa` keeps omitting `reference_pressure_bar`
(`thermo_reference_pressure_not_stated`), never a default.

Upgrading changes the payload hash, and so the idempotency key, of 1.3 uploads that gain a partial
BAC, a different treatment, torsions without a treatment, a scheme release, a kinetics note, a
stated `reversible`, ESS-ordered modes or a T1 diagnostic. Documents before 1.3 are unchanged, and the
golden corpus hashes are unchanged.

## Reaction atom map and IRC participant mapping (adapter 0.8.0, batch G)

ARC output schema 1.3 (ARC PR #1059 @ bc731fb4) exports the reaction's `atom_map`,
`atom_map_reactant_labels`, `atom_map_product_labels`, `atom_map_source` and
`atom_map_method` (`arc/output.py:_get_reaction_atom_map`), and the TS-only
`irc_participant_mapping` (`arc/output.py:_irc_participant_mapping_to_dict`). The
fixture `tckdb_arc/tests/fixtures/arc_1_3_reactions` is written by ARC's writer, with the map
from ARC's mapper and the IRC mapping from ARC's IRC check.

- **IRC participant mapping is sent (B10, A14).** On a passed IRC verdict
  (`ts_checks.IRC` true) the evidence record carries `reactant_participant_mapping` and
  `product_participant_mapping`: `{"reactant:N": [1-based TS atoms]}`. ARC's indices are
  0-based indices into each IRC endpoint geometry (add 1), and are TS atom indices only when
  `atom_order_matches_ts` is `true`. TCKDB's participant index is the slot in
  `reactant_keys`, which follows the reaction's `reactant_labels`, **sorted**; ARC counts
  `position` in `atom_map_reactant_labels` order (`r_species`), so participants are matched by
  `(label, occurrence)` and never by `position`. Neither side is sent (and
  `ts_irc_participant_mapping_not_sent` names why) when `atom_order_matches_ts` is not `true`,
  when `sides_distinguishable` is not `true` (ARC states that which endpoint is the
  reactants is then a convention), when the participants are not exactly the slots of the
  uploaded reaction (a repeated species collapsed in `reactant_labels` has fewer slots than
  ARC's per-occurrence participants), when an index is out of range, when a side does not
  cover every TS atom exactly once, or when a participant's atoms are not the elements of
  its species. A `null` or absent mapping (pre-1.3, bond-list fallback, IRC not validated)
  sends nothing and says nothing. Both TS routes.
- **The reaction atom map is not sent (B8).** *(Documents that carry no `ts_atom_map`, i.e. the bc731fb4 draft. From
  `ebc88ec8` the map is sent: see batch J.)* TCKDB's `ReactionAtomMapIn` is, per
  participant, `{participant atom: TS atom}`, 1-based, against the TS geometry key
  (`ts_geometry_key`, `participants[].geometry_key`, `participant_index`); ARC's `atom_map` is
  reactant atom to product atom, and its schema says it "says nothing about the atom order
  of the transition state". `irc_participant_mapping` records only atom-set membership, "not
  the atom-to-atom correspondence inside a participant". No ARC key relates a participant
  atom to a TS atom one by one, so `atom_to_ts` cannot be stated; writing TS atom `i` =
  reactant atom `i` would assert an order ARC does not (the fixture's TS lists CH4 before OH,
  ARC's map counts OH first). TCKDB never derives a map (ADR 0011), so the adapter reports
  `reaction_atom_map_ts_order_not_stated` (context: `atom_map_source`, `atom_map_method`)
  whenever ARC states a map and a TS exists, and TCKDB reports `reaction_atom_map_absent`. The
  standalone TS request has had an `atom_map` slot since tckdb-schemas 0.64 (it counts into
  `geometry` on each participant and the request's `geometry_key`), left unset for the same reason
  (C9). If ARC later exports the
  TS atom each reactant and product atom corresponds to: `source` is ARC's
  `atom_map_source` (`declared` or `inferred`; a `null` source is refused, TCKDB has no
  default), an `inferred` map's required `note` is ARC's `atom_map_method`
  (`atom_map_inferred_requires_note`), and `ts_geometry_key` is the TS block's `ts_geom`.
- **Consistency.** With no atom map sent, `atom_map_contradicts_irc_mapping` (TCKDB's
  partition-refinement check, `app/services/reaction_atom_map.py`) cannot arise. The
  offline-checkable rules on the mapping that is sent (every participant named, every TS atom
  once per side, `transition_state_irc_mapping_element_mismatch`) run in
  `test_arc_1_3_reactions.py` against the fixture, and the conftest contract hook checks the
  published models. The fixture's atom map and IRC partition agree participant by participant;
  that is a property of the fixture, not a gate, because ARC's map is arbitrary among
  symmetry-equivalent atoms.
- **`kinetics.ts_validation` (1.3) and `ts_checks`.** `ts_checks` has the same five verdicts
  as 1.1 (`E0`, `e_elect`, `IRC`, `freq`, `NMD`); only IRC is sent (validation kind
  `irc`), unchanged since 0.6.4. TCKDB 0.64 added homes for two more kinds (batch H section), but the
  other four verdicts are still not sent. `ts_validation`
  is ARC's text marker on a rate computed from a TS whose `ts_checks.IRC` is `false`; that
  verdict is already sent as `validation_evidence[].passed = false`, TCKDB has no per-rate
  validation field, and `BundleKineticsIn.note` carries only ARC's
  `long_kinetic_description`, so it is not sent.
- **`reversible` (1.3) is not read here.** Out of this batch.
- **Repeated species (B16).** *(From `ebc88ec8` ARC states `reactant_species_labels` / `product_species_labels` and these
  are the source; the recovery below is the fallback for the draft that lacked them. See batch J.)* ARC's `reactant_labels` / `product_labels` are sorted and
  de-duplicated, so `HO2 + HO2 <=> H2O2 + O2` would upload as `HO2 <=> H2O2 + O2` and TCKDB would
  refuse it as unbalanced. When `atom_map_*_labels` (else the IRC participants) list a repeat the
  collapsed lists lack, the expanded lists define the participants (one species block, the key
  repeated per slot); with neither stated, collapsed lists that do not balance by the species
  geometries refuse the upload with `reaction_stoichiometry_not_stated` (a missing geometry skips
  the check). The label string is not parsed.

## tckdb-schemas 0.59 to 0.64 (adapter 0.8.0, batch H)

Read first: `python -m tckdb_schemas.contract --since 0.58.0`. Each paragraph quotes what it relies on.

- **Atoms send their `sp` (0.59, TCKDB#610; A6, C6).** "A conformer whose own XYZ has exactly one atom may
  send `type: "sp"` as its primary ... Link the atom's thermo and statmech source calculations to that `sp`
  with role `sp`; an atom has no `opt` or `freq` to link ... A relabelled `opt` on an atom is still
  accepted." A conformer whose XYZ (the geometry the adapter sends) has one atom now carries its real sp
  as the primary calculation on the computed-species, computed-reaction and conformer routes: the atom's
  own log, level and program (output 1.3: `levels.sp`, `ess_software.sp`; its sp log is also its
  `opt_log`), its energy (`sp_energy_hartree`, else the `opt_final_energy_hartree` ARC parsed from the
  same one log), with no `depends_on`, no reused-result marker and no `converged` (`SPResultPayload` has
  no such field, so ARC's flag for the atom is not sent). It has no `opt`, coarse opt, `freq`, rotor
  scans or alternative conformers. Thermo, statmech and the applied corrections point at that one sp
  (key `sp`, or `<prefix>_sp` on a reaction), so no two `sp` links share a geometry
  (`thermo_role_duplicate`, `statmech_role_duplicate`). The server gives the atom's sp the conformer geometry as
  its final output (`is_single_atom_primary`), so the adapter sends none. Thermo now declares the atom's
  `energy_level_of_theory` (the linked sp's level), as for any linked sp. An atom with neither energy is
  refused with a `ValueError` (never relabelled). `monatomic_species_primary_opt_placeholder` is gone. The
  placeholder opt remains for a molecule with no opt job (`primary_opt_placeholder_no_opt_job`) and a
  composite run (`composite_geometry_level_not_stated`): 0.59 covers one-atom XYZs only. Tests:
  `test_monatomic_sp_primary.py`. The golden `computed_reaction` hash changes (the `H` block), proved by
  strip-and-restore in `test_golden_corpus.py`.
- **Scheme provenance (0.62; B12, C10).** `EnergyCorrectionSchemeRef.data_revision` is "the revision of the
  *data* that holds the parameter tables, for example the RMG-database commit that holds Arkane's
  atom-energy and BAC tables ... When present it joins the scheme's identity and the workflow-tool build
  stops being part of it ... A value of 7 to 64 hex digits is lower-cased as a git commit; any other value is
  kept as written ... Adapters: send the RMG-database commit here, keep stamping the tool release." Output
  1.3's `rmg_database` gives it: the git `HEAD` for `path_kind: git`, else the package `version`, else the
  SHA-256 of the `data.py` Arkane loaded as a plain hex string (no `sha256:` prefix; TCKDB reads 7 to 64
  hex as a commit and lower-cases it, and keeps anything else as written; at most 200 characters). It is sent
  on `atom_energy`, `bac_petersson` and `bac_melius` schemes, with the Arkane build still stamped as
  `scheme.workflow_tool_release` (provenance only now). It is omitted before 1.3, without the block, or when
  the block states no usable commit, version or digest; the scheme then keeps its pre-0.62 identity. ARC does
  not say whether the checkout is dirty or Arkane's tables were overridden, which the contract says should
  suppress the revision; the adapter cannot check it (ARC's own `data/AEC.yml` override is handled by
  `atom_energy_record_not_deposited_aec_yml`). **History.** The `RMG-database` tool-release identity set of
  the first batch-F draft was never deposited: it existed only on an unmerged branch, so deposited rows have
  at most the null-tool and the Arkane-build identities, and a deposit with a revision never matches one
  without. `atom_params_applied_as` ("How `atom_params` enter the corrected energy ... covers every entry of
  `atom_params` and nothing else ... not inferred when omitted") is sent as ARC states it in
  `reference_atom_energies.applied_as` (`subtracted`), only beside `atom_params`. The scheme note's first
  sentence (the subtraction) is now redundant and was dropped; the note keeps Arkane's per-atom addend
  (`atom_hf - atom_thermal`), which `atom_params_applied_as` does not cover.
- **Kinetics `t0_k` (0.63).** "`t0_k` ... the reference temperature T0 of the scalar rate, in K, meaning
  `k = A (T/T0)^n exp(-Ea/RT)` ... It must satisfy `0 < t0_k <= 10000` ... The server stores `a` as sent (it is
  A at T0, not A rescaled)." So ARC's `A` is sent as it is and `t0_k` is ARC's `T0_k` (left out when it is 1 K,
  the contract default and the old convention). A null, invalid or out-of-range `T0_k` omits `a` and
  `a_units` with a warning; no `T0_k` key (pre-contract output) still means 1 K. The normalisation
  `a = A / T0**n` and its need for `n` are gone. Falloff, PLOG and the other forms refuse a non-1 K `t0_k`, and
  the adapter only sends `modified_arrhenius`. `tunneling_application`, `interpretation_assignments` and
  `network_kinetics_ref` are not sent: "every reference is the public ref of a record deposited *earlier* (a
  bundle cannot cite a statmech, transition state or calculation it is itself creating)", and the adapter's
  bundle creates all of them. Tests: `test_kinetics_reference_temperature.py`.
- **Source links never cross species (0.61).** "A source calculation of another species on the reaction bundle
  is refused" (stated for transport; the thermo, statmech and applied-correction links are owner-checked at the same
  seam). Batch E and G's links are built from each block's own role map, and a repeated species (`H + H`) is one
  block, so nothing crosses; `test_reaction_bundle_source_links.py` replicates the ownership rule over five built
  bundles (with a mutation check of the checker). Not built: `transport` (ARC's `opt_dipole_moment_debye` and
  `freq_polarizability_angstrom3` could feed `dipole_debye` and `polarizability_angstrom3`, which needs a decision
  on the dipole's density and source calculation). **Rejected rotors (0.61):** "`invalidated_reason` on
  `StatmechTorsionInBundle` and on the reaction bundle's `BundleStatmechTorsionIn`, the same field the conformer
  route's `StatmechTorsionIn` has", so the computed-species and computed-reaction statmech blocks now carry ARC's
  rejected rotors too (reason and dihedral coordinates; the bundle torsion models have no `note`, so ARC's
  `rotor_index` is sent only on the conformer route).
- **Validation evidence (0.64; A14, C7).** "A pass that the record's own *stated* numbers contradict is refused, a
  field is refused on a kind it does not describe, and only a passing `irc` record silences
  `transition_state_missing_irc_evidence` ... `energy_ordering` is accepted on the computed-reaction and
  pressure-dependent bundles and refused on the standalone transition-state upload ... a pass with more than one
  imaginary mode needs that result to designate the reaction coordinate." The adapter sends `irc` evidence
  carrying only irc fields (`passed`, `rationale`, the key on the bundle, the two participant mappings with
  1-based, non-empty, unrepeated atoms and both sides or neither), never a mapping on a failed verdict, and on the
  standalone route no `source_calculation_key` with exactly one irc calculation to bind to. Batch F's
  reaction-coordinate designation (the stated index or the one mode at or above TCKDB's 50 cm-1 tau) is unchanged
  and meets the contract. Since 0.8.1 (batch I) it also sends `imaginary_mode` (both routes) and `energy_ordering`
  (bundle only; electronic energies only), see A14 above; `test_ts_validation_evidence_rules.py` replicates the
  rules offline (fields belong to the kind; one record per kind; ownership and type of each energy's source; a pass
  the stated numbers contradict; the count and frequency against the cited frequency result), and
  `test_ts_energy_ordering_evidence.py` / `test_ts_imaginary_mode_evidence.py` pin what is sent and what is left out.
- **Standalone transition-state route (0.64; C9).** "`additional_calculations` now accepts `scan`, and
  `CalculationWithResultsPayload` gains `scan_result` ... The request gains `applied_energy_corrections` (no source
  keys or frequency scale factor, since the payload has no key namespace), and `atom_map`." The adapter now sends the
  TS's rotor scans and applied corrections there (it dropped both before), minus the source keys. `atom_map` stayed
  unset (`reaction_atom_map_ts_order_not_stated`) until ARC `ebc88ec8` stated `ts_atom_map` (adapter 0.9.0, batch J, which
  sends it here from the same data as on the bundle).
- **Not sent, no source in ARC.** Statmech `electronic_levels` (0.60): ARC 1.3's statmech record has no electronic
  level, term symbol or spin-orbit data, so none is sent or derived (an O or Cl atom will draw TCKDB's
  `missing_atomic_electronic_levels`). TS `statmech` on the reaction bundle (0.64): unwired. An IRC result whose
  direction ARC does not state (0.64 made `direction`, `has_forward` and `has_reverse` optional): the adapter still
  withholds the result (`irc_direction_not_stated`) because the contract does not say how TCKDB treats unlabelled
  non-TS points.

## Output schema 1.3 at ebc88ec8 (adapter 0.9.0, batch J)

Consumed ARC head: PR #1059 `ebc88ec8` (schema still `1.3`; the adapter consumed the draft `bc731fb4` before). Read first:
`python -m tckdb_schemas.contract --print` (reaction `atom_map`: `ReactionAtomMapIn`, `ReactionAtomMapParticipantIn.atom_to_ts`,
`source`, `note`; the 0.64 standalone `atom_map`, with `geometry_key` and a `key` and `geometry` on every participant).
Every new key is optional for the adapter: a document without it (the earlier draft; the fixtures `arc_1_3_levels`,
`arc_1_3_reactions`, `arc_1_3_thermo`, `arc_1_3_samples`) takes the previous path unchanged. The golden corpus hashes are
unchanged. Fixtures: `arc_1_3_samples_ebc88ec8` (ARC's real writer at `ebc88ec8`, two of the ARC agent's three samples; the
third, `legacy_restart`, fails ARC's own `minItems: 1` on the species labels and is not copied) and `arc_1_3_ts_atom_map`
(derived by hand, `generate.py`, README there). `test_arc_1_3_ts_atom_map.py` validates all three against ARC's
`output_yml_schema.json` when it is available (`ARC_OUTPUT_YML_SCHEMA`, else the ARC worktree path).

| ARC key (`ebc88ec8`) | What the adapter does |
|---|---|
| reaction `reactant_species_labels`, `product_species_labels` | **B16 done.** THE order and repeats of `reactant_keys` / `product_keys` on both routes (`_with_stated_participants`). Absent (earlier draft): the previous recovery from `atom_map_*_labels`, then `irc_participant_mapping`, then the collapsed lists when they balance (`reaction_stoichiometry_not_stated` otherwise). Present but contradicted (other species than `reactant_labels`, differing from `atom_map_*_labels` in content or order, not a list of labels, one side only): the reaction is refused with `reaction_species_labels_contradicted`. No repeat is ever guessed from the label string. |
| reaction `ts_atom_map`, `ts_atom_map_unavailable_reason` | **B8 revised done.** TCKDB's `atom_map` on the bundle and on the standalone route: for every participant slot, `atom_to_ts` is `{participant geometry atom (1-based): TS atom (1-based)}` over that participant's block of `reactants` / `products` (blocks in `atom_map_*_labels` order, which after the species labels is the slot order); `source: inferred`, `note` quoting ARC's method and symmetry-equivalent-atom convention; no `equivalent_map_count` (unknown is not 1). Sent only when every check passes (`_tckdb_reaction_atom_map`): the map is well formed and for this TS; the uploaded slot labels are exactly `atom_map_*_labels`; each block length is the participant's uploaded geometry atom count and equals `atom_map`'s; TS indices are in range and each side covers every TS atom once; each participant atom has the element of its TS atom (uploaded geometries); `products[atom_map[i]] == reactants[i]`; `ts_atom_order_follows_reactants` agrees with the list; and, when the IRC evidence that is sent carries participant mappings, each participant's TS atoms equal the IRC mapping's (TCKDB blocks `atom_map_contradicts_irc_mapping` otherwise). A failure sends no map and reports `reaction_ts_atom_map_not_sent` with the reason. `ts_atom_map: null` omits it with the same code and `ts_atom_map_unavailable_reason` in the warning context; a document without the key keeps `reaction_atom_map_ts_order_not_stated`. Standalone route: each participant gets `key` (`reactant_1`, ...) and `geometry` (key `reactant_1_geom`), the request `geometry_key` `ts_geom`; only when a map is sent, so requests without one are byte-identical to before. A repeated reactant is two participants of one species block (bundle) or two keyed participants (standalone), each with its own `atom_to_ts`. The correspondence is constitutional (2D); that is why the note says symmetry-equivalent atoms (diastereotopic ones too) are assigned by convention. |
| TS `nmd_forced` | **Q2 done.** `true` (the check failed and `skip_nmd` forced the pass): `mode_displacement_agrees: false` is sent (the check ran and the mode disagreed; never `true`; TCKDB has no rule tying it to `passed`) and the rationale says so; an index stated beside it is a contradiction (`ts_nmd_forced_contradicts_reaction_coordinate_index`) and is not used as the NMD designation of the frequency result (TCKDB's tau rule designates, or the record is refused as before). `false` with an index: unchanged (`True`). `null`: unchanged (the adapter no longer relies only on ARC nulling the index). There was no kinetics comment or other validation note that mentions NMD to extend. |
| species `conformer_ess_software`, `conformer_ess_version` | **Q4 done.** Where both lists are present and index-aligned with `conformers`, each screened conformer whose `conformer_levels[i]` is a level and whose program entry is non-null is filed at that level with `software_release` = the observed program and its banner (split as for `ess_versions`); a null program is not filed (`conformer_program_not_stated`, now worded for the null entry) and is never replaced by the header `conformer_opt_level` program; a null banner files the program without a version. The conformer single point keeps its own program from `conformer_sp_level` (ARC states the list names the optimization log). Absent or misaligned lists: the header rule of 0.8.0. Only the computed-species route files screened conformers (the conformer route uploads the selected one). |
| `conformer_energies` null entries (Q3) | **Q3 done, no change.** A null entry means no energy; `conformer_energy_kind` / `_level` / `_force_field` describe only the non-null entries. The adapter already sent an `opt_result` only when the kind is `electronic_kj_mol`, the level matches the conformer's and that entry is a finite number; a null entry never becomes one (pinned in `test_arc_1_3_ts_atom_map.py`). |
| `standard_state_pressure_pa` (Q6) | **Q6 done, no rule change.** ARC states 101325 for every thermo an Arkane run it invoked produced (also when read from `output.py` because `thermo.yaml` was missing); `null` only for a thermo no ARC path produces and for documents before the key. A number within 0.5 to 2 bar still becomes `reference_pressure_bar`; a null is still omitted with `thermo_reference_pressure_not_stated`, never defaulted (repo CLAUDE.md). On 1.3 final it is always stated. |
| header `bac_type`, thermo `bond_corrections_applied` (Q5) | **Q5 done.** ARC's schema now requires `bac_type` in `{p, m}` when a thermo or statmech states bond corrections were applied. `bond_corrections_applied: true` with a null `bac_type` raises `bac_type_not_stated`; the BAC scheme is still built from the record's `energy_corrections` and `bac_type` is not inferred. |

Not consumed: `ts_atom_map.reactant_endpoint` beyond the note, and `ts_label` beyond checking it names this TS. TCKDB has no
`equivalent_map_count` source in ARC, and no `ts_atom_map` for a reaction without an `atom_map` or a passed IRC (ARC states
`no_atom_map` / `irc_not_passed` / `irc_fallback_path` ... and the adapter sends no map). Still open on the ARC side: Q7
(a record-level signal that an NEB job ran) and B17 (composite step route lines).

Upgrading changes the payload hash, and so the idempotency key, only of documents that carry the new keys.

## tckdb-schemas 0.65 to 0.73 (adapter 0.10.0, batch K)

Read first: `python -m tckdb_schemas.contract --since 0.64.0` (the whole changelog) and `--print` for
`EnergyCorrectionSchemeRef`, `LevelOfTheoryRef`, `CalculationUploadRef`, `ArtifactsUploadResult` and
`ComputedReactionUploadResult`. The pin moved with `tools/tckdb_drift.py --bump` (TCKDB `96b71b09`, schemas 0.73.0,
client 0.111.0). **No test broke on the bump**: every payload accepted before is accepted unchanged by 0.65-0.73 (they only
add), and the contract-pin test passes against the installed 0.73. Nothing the adapter sends changes except where an item
below says so. The golden corpus hashes are unchanged.

| TCKDB release / item | What the adapter does |
|---|---|
| 0.65 (#615, atoms with an `sp` primary on the network route) | Not applicable: the adapter never calls `/uploads/networks/pdep`. |
| **6.2** public refs (0.57 + #599): `submission_ref`, `calculation_ref`, `calculation_key_refs` | **Done.** `UploadOutcome.submission_ref` and `.calculation_key_refs` (computed-reaction) carry what the response states; every `*_ref` is also in the sidecar's `public_refs` (`calculation_key_refs` values land under `calculation_refs`). The sweep names an artifact target by `primary_calculation.calculation_ref`; the URL (`/calculations/{calc_ref}/artifacts`), the batch idempotency key and the sidecar file name are built from the ref. The integer `calculation_id` is a fallback only for a response that carries no ref: it is used then with a `calculation_ref_not_returned` warning (log and sidecar) and never preferred. The adapter makes no rights-attestation call (neither does tckdb-client 0.111), so there is nothing to feed a `sub_` ref to; the ref is on the outcome for a caller that attests. The live-gate read helper (`tckdb_core.testing.live.commit_probe`, re-exported by `tests/integration/_live.py`) still reads by integer id: `GET /calculations/{id}` takes an integer. |
| artifact idempotency key | `(calculation_ref, artifact_kind, artifact_sha256)`, shape `arc:<project>:<species>:artifact:<calc_ref>:<kind>:<sha16>`. A sidecar written before 0.10 holds an integer-id key, so a new key would not replay and TCKDB keeps one `calculation_artifact` row per upload even for identical bytes (`backend/app/db/models/calculation.py`): re-posting would **duplicate the row**. Before building a ref-keyed artifact upload the adapter reads the pre-0.10 sidecar of the same artifact (`<species>.calc<int>.<kind>.artifact.meta.json`; the response still carries the integer id) and, if its status is `uploaded`, its sha256 matches and it was posted to the same `base_url` (when both state one), skips the artifact as done (`_legacy_uploaded_artifact`). No sidecar, a pending or failed one, or different bytes: upload under the ref key. Replaying a pre-0.10 sidecar itself (the replay tool) still posts under its old key. See `tckdb_arc/README.md`. |
| **6.4** correction schemes (0.66): `scheme.frequency_level_of_theory` | **Done.** Sent only on a `bac_petersson` / `bac_melius` scheme whose record's `matched_arkane_key` (ARC 1.3 export) is a `CompositeLevelOfTheory(freq=..., energy=...)`, with the key's freq half (`_bac_key_frequency_level`). Contract 0.66: a scheme Arkane keys on one level, and every atom-energy scheme, send nothing new; in RMG-database `quantum_corrections/data.py` only 4 of 47 Petersson keys are composite-keyed, so most BAC schemes carry none. ARC's freq-job level (`levels.freq`, or the header `freq_level` for a record without `levels`, see `_bac_frequency_level`) is only a cross-check: if it disagrees with the key's freq half (method, basis, software when both state it, compared ignoring case and punctuation) the field is omitted with a `bac_frequency_level_conflict` warning; a composite key with no ARC freq level sends the key's half. A pre-1.3 record has no `matched_arkane_key` and so sends none. **Identity consequence:** the field joins scheme identity, so a composite-keyed BAC scheme deposited by an earlier adapter and the same table now sent with a frequency level are two schemes (one new `energy_correction_scheme` row per distinct frequency level). A frequency level equal to the energy level is stored as absent by TCKDB. |
| `composite_delta_prefer_scheme_terms` (0.66) | **Never triggered.** The adapter only ever sends `application_role` `aec_total` and `bac_total`; pinned by `tests/test_correction_scheme_levels.py` (behaviour over the three routes, and the adapter source does not name `composite_delta`). |
| **0.67** `//` in `level_of_theory.method` (`level_of_theory_method_is_compound`) | **Proved absent.** Every level the adapter sends comes through `_arc_level_to_tckdb_lot` (calculation levels, conformer/scan/IRC/TS-guess levels, scheme levels, the frequency-scale-factor level, the energy-level declaration). It now refuses a `//` method (returns no level and logs `level_method_is_compound`; the callers already treat "no level" as "not sent / not built"), since which half of an `energy//geometry` pair belongs to a job is not stated. ARC itself splits the shorthand on input (`arc/main.py`), so this guards a hand-edited or foreign document. `tests/test_method_guards.py` mutates every level slot of the 1.3 fixture to a compound method and builds all five routes: no payload carries a `//` method. `atom_corrections_level` and the header `arkane_level_of_theory` are only compared, never sent. |
| 0.67 correction-table names (`level_of_theory_method_names_correction_table`) | **Done.** Where ARC's Arkane level string is such a name (`cbs-qb3-paraskevas`, `cbsqb32023`, recognised by the shipped `tckdb_schemas.fragments.refs.correction_table_method_stem`), the scheme's `level_of_theory.method` is the stem and **the scheme's `name` is the table name** as ARC states it (the contract names no other field for it: `name` is the scheme's label and part of its identity, `note` is not). The adapter warns `correction_table_method_split` (action `correction_table_named_on_scheme`). Identity consequence: such a scheme is now `(kind, name=<table>, level=<stem>)`, instead of `(kind, name=<kind>, level=<table-as-method>)`; the stem level is the real CBS-QB3 level, not a separate level named for a table. **Calculation levels too:** ARC accepts `cbs-qb3-paraskevas` as a composite method (`arc/level.py`, `data/ess_methods.yml`) and runs it as CBS-QB3 (`arc/job/adapters/gaussian.py`), so `_arc_level_to_tckdb_lot` applies the same stem to every level it projects (calculations, thermo/statmech levels); a calculation at `cbs-qb3-paraskevas` is sent with method `cbs-qb3` and logs `correction_table_method_split` naming the original string (log only: the calculation builder has no sidecar-warnings channel, so unlike the scheme case there is no sidecar entry). Only the scheme level keeps the table string (`split_table=False`), because the scheme's `name` carries it. A level whose method contains `//` is withheld: the calculation is not built and the skip reason starts with `level_method_is_compound`. |
| 0.68 composite schemes bound to a named method | Reads only. No payload change. |
| 0.69-0.73 (`composite` calculation type, `sp_energy_components`, `core_treatment`, `composite_scheme`, `composite_result`) | **Not consumed: composite runs (6.3) are deferred** to a later batch, pending ARC export B18 (the composite 0 K energy as printed, separately from `e_elect` and the scaled ZPE; `ARC_TCKDB_EXPORT_BRIEF.md` A6). The adapter derives none of `e0_hartree` / `recipe_zpe_hartree` / `electronic_energy_hartree`. `core_treatment` is never sent (ARC states none). |
| **0.71** G4 / G4MP2 logs are not compared (Gaussian 16 Rev A.03 summary labels shifted) | **Stopgap guard (6.6).** ARC's `sp_energy_hartree` for such a run is the number after the `G4(0 K)` / `G4MP2(0 K)` label, the 298 K value on that revision (9-11 kJ/mol from E0, ARC brief Bug 8). For a record with `composite_log`, whose composite level (`levels.composite`, else the header `composite_method`) is `g4` or `g4mp2` (any spelling the identity key joins) and whose `ess_versions.composite` is Gaussian 16 Revision A.03, the adapter sends no sp energy, no statmech energy links (`sp` and the `composite` role) and, on the reaction routes, no kinetics fitted from it, and strips only the E0-derived thermo content (H298, NASA polynomials, point H and G; the `_strip_enthalpy_content` path the enthalpy refusals use). S298, Cp and point S come from statmech, not E0, and are kept, as is the thermo block's provenance. It warns `g4_energy_loader_shifted_label_gaussian16_a03` per record (the message names what is withheld and what is kept) and once for the kinetics. Missing banner or method: not that case, nothing inferred. **Coverage limit:** only output 1.3 records carry `ess_versions.composite`, so a pre-1.3 document has no banner to match and the guard cannot fire on it; a G4 run on that revision in an older document is sent unguarded. It stays until ARC exports a correct E0 (B18). The AEC/BAC totals (sums of table parameters, independent of the energy) are still sent. |
| 6.1 identity hash replica | **Rebuilt** as `tckdb_arc/level_rules.py` (below). |

### The level-of-theory identity replica (`tckdb_arc/level_rules.py`)

TCKDB hashes a level over normalised keys (`calculation_resolution._level_of_theory_hash`, with `level_identity_keys`),
so two spellings the adapter might send are one level on the server. The replica moved into the package (the adapter can
pre-check identity at build time; `tests/_backend_level_rules.py` re-exports it) and now follows TCKDB `96b71b09`:

- method: strip and lower-case; whole-name aliases `wb97x-d`, `m06-2x` and the composite spellings
  `cbsqb3`/`rocbsqb3`/`cbs4m`/`cbsapno` to their hyphenated forms and `g4(mp2)`/`g3(mp2)`/`g3(mp2)b3` to
  `g4mp2`/`g3mp2`/`g3mp2b3`; trailing `-d3(bj)` / `-gd3bj` to `-d3bj`. `w1`/`w1u`/`w1bd`/`w1ro`, `cbs-qb3` / `rocbs-qb3` and
  correction-table names stay apart.
- dispersion column: `gd3bj`, `d3(bj)` to `d3bj`; `gd3`, `d30` to `d3zero`; `gd2` to `d2`; each also as Gaussian's
  `EmpiricalDispersion=X`, `=(X)` and `(X)`; bare `d3` is its own key.
- a recognised dispersion folded into the method (`b3lyp-d3bj`) moves into the dispersion key, only off the listed stems
  and suffixes, and not when the column states a different dispersion (then nothing is split).
- `core_treatment` joins the payload **only when stated**, never as a null placeholder.

The pinned pre-`core_treatment` hashes of TCKDB's `test_level_of_theory_core_treatment_hash.py` are reproduced in
`tests/test_level_rules.py`, and `test_hash_matches_the_backend` plus a method-by-dispersion corpus compare the replica with
the backend's own pure `app.chemistry` modules (and the real `_level_of_theory_hash` where the backend's dependencies are
installed). CI clones `backend/app/chemistry` at the pinned sha and sets `TCKDB_BACKEND_PATH` and `TCKDB_REQUIRE_BACKEND=1`
(a missing backend then fails instead of skipping); locally, point `TCKDB_BACKEND_PATH` at a TCKDB checkout's `backend/`.

## Core extraction (adapter 0.11.0, batch L1)

No mapping changed. The producer-agnostic half of the adapter moved to `tckdb_core/` (`tckdb-adapters-core` 0.1.0):
the payload writer, idempotency-key composition (namespace passed in), constraints, the level-identity replica
(`tckdb_core/level_rules.py`; its pinned-hash and backend-corpus tests moved to `tckdb_core/tests/test_level_rules.py`),
the config fields and API-key resolution, and the upload / sidecar / readiness / artifact-batch pipeline
(`TCKDBUploaderBase`). `tckdb_arc` re-exports every moved name, so the paths named above still resolve. Every payload,
sidecar, warning, key and log line is byte-identical to 0.10.0: all three upload modes, over eight fixtures, with an
accepting, a refusing and an offline client, were built at the 0.10.0 commit and at 0.11.0 and the JSON compared (an
empty diff). Left for the next batch: the NASA and thermo-point builders, unit tables, result flattening, the warning
code table, and the contract test kit.

## Core extraction, second half (adapter 0.12.0, batch L2)

No mapping changed. The rest of what a second producer would reuse moved to `tckdb_core` 0.2.0: the NASA and thermo-point
builders, the RMG unit tables, the reaction-route result flattening and its completeness guard, the element, formula and xyz
helpers (with `E_h_kJmol`), the isotope rule and the route-line comparison, the ESS-banner split, and the TCKDB rule
pre-checks (`rules.energy_level_declaration`, the reaction-coordinate window and tau rules, the validation-evidence record
shapes and the offline replica of the server's evidence rules). Each ARC function that reads an ARC key (`ts_checks`,
`xyz_isotopes`, `thermo.*`) stayed and calls the core with its facts as arguments. The contract test kit
(`tckdb_core.testing`) and the warning-code registry (`tckdb_arc/warning_codes.py`, rendered to
[`WARNING_CODES.md`](WARNING_CODES.md)) are new homes, not new behaviour. Every payload, sidecar, warning, key and log line is
byte-identical to 0.11.0: four fixtures, all four upload modes, with an accepting, a refusing and an offline client, were
built at the 0.11.0 commit and at 0.12.0 and compared (an empty diff).
