# A2 — ARC exported supply (refresh for tckdb-schemas 0.51.0)

**Repo inventoried:** `/home/calvin/code/ARC`, `main` @ `db0934d5` (output.yml schema 1.1), plus
the open PR branch `feature_export_atom_corrections_applied` @ `1977e53b` (ARC PR #1059, schema 1.2),
read with `git show`, nothing checked out.
**Companion:** `ARC_SUPPLY_EXPORTED.yml`, 535 rows. Canonical spellings come from
`scratchpad/shared/paths_0_51.tsv` (tckdb-schemas 0.51.0, TCKDB_v2 @ `ad3cd706`). Every
non-`arc_only` path resolves to a leaf or container in that file. The check is mechanical and 0 rows fail it.

## Scope

What leaves ARC at the end of a run, in `<project>/output/`:

| File | Writer | Content |
|---|---|---|
| `output.yml` | `arc/output.py:113` `write_output_yml` | Result contract, closed JSON schema `arc/schemas/output_yml_schema.json` |
| `parser_evidence.json` | `arc/parser_evidence.py:576` `build_parser_evidence` | Hessian, IRC trajectories and GSM path, versioned `arc-parser-evidence` 1.0 |

The sidecar was renamed from the old inventory's `tckdb_evidence.json`, and the output.yml
descriptor key changed from `tckdb_evidence` to `parser_evidence`. All old `arc_only.tckdb_evidence.*`
rows are now `arc_only.parser_evidence.*`.

## Keying

- Per-species values are keyed under both species-shaped roots, `species_upload.*` and
  `conformer_upload.*`. The `reaction_upload.species[].*` mirror is not duplicated; that is
  ROUTE_ADJUDICATION's job, as before.
- TS values are keyed under both `reaction_upload.transition_state.*` and `ts_upload.*`. At 0.51,
  `reaction_upload.transition_state` has no `conformers[]` any more: it has `geometry`, `calculation`
  (primary) and `calculations[]`, and its calculations use flattened result scalars
  (`opt_converged`, `freq_n_imag`, `freq_frequencies_cm1`, `sp_electronic_energy_hartree`, …)
  rather than `opt_result.*` sub-objects. The rows follow that shape.
- **No placeholder rows.** The old inventory had about 40 rows with `arc_key: None`, for TCKDB
  fields ARC does not export. `join_inventories.py` counts any A2 row as supply, so those rows
  turned ARC_ABSENT gaps into ADAPTER_GAP. They are removed. Absences are recorded in the
  known-gap section below.
- Duplicate paths are intentional where two distinct ARC sources feed one TCKDB leaf. Four paths
  have this: `additional_calculations[].type`, `.artifacts[].filename` (freq/sp logs and rotor
  scan logs), `reaction_upload.transition_state.calculations[].artifacts[].filename` (IRC logs and
  GSM/NEB logs), and `conformer_upload.statmech.torsions[].torsion_index` (successful and
  rejected rotors).

## Row counts

| Root | Rows |
|---|---:|
| `species_upload` | 186 |
| `conformer_upload` | 97 |
| `reaction_upload` | 93 |
| `ts_upload` | 70 |
| `transport_upload` | 0 (ARC exports no transport) |
| `arc_only` | 89 |
| **total** | **535** |

Availability is 35 always, 460 conditional and 40 rare. Fidelity is 296 exact, 160 derived and
79 lossy. Five rows exist only on the 1.2 branch.

## Top findings

1. **The golden fixture is thin.** `arc/testing/parser_evidence/golden/output.yml` is 18 lines. It
   holds the header, one species label and `freq_log`, one TS label, `chosen_ts_method`,
   `irc_logs` and `irc_log_directions`, and `gsm_log`. Only 111 of the 535 rows cite a golden line,
   nearly all of them header-provenance or evidence-sidecar rows. Everything else in output.yml rests on
   `output_schema_test.py`, which validates a document built by the real writer, or on unit
   asserts in `output_test.py`. The adapter's `tckdb_arc/tests/fixtures/current_arc/` is a
   byte-identical copy of that golden pair. The adapter's `arc_1_2/output.yml` is hand-written.
2. **No-fixture exports** (`fixture_evidence: null`, 64 rows, 41 distinct ARC keys). No committed
   ARC test asserts these values:
   - `kinetics.T0_k`: the rich fixture's kinetics omits T0 (`output_schema_test.py:221-226`).
   - `inchi` and `original_label`.
   - `coarse_opt_n_steps` and `coarse_opt_final_energy_hartree`.
   - freq and sp constraints, and rotor-scan `constraints[]`.
   - the TS `freq_hessian` evidence record: the golden TS record has none.
   - GSM `stringfile_relative_energy_kcal_mol`, which is omitted on real xtb_gsm runs anyway.
   - `unavailable` envelopes: `reason`, `source_paths`, `omitted_source_paths`.
   - every optional level-dict key: auxiliary_basis, cabs, dispersion, solvent,
     solvation_method, args, year, method_type.

   `e0_kj_mol` is set by the rich fixture (`output_schema_test.py:138`) but never asserted.
3. **Exported but lossy (79 rows).** The ones that matter most:
   - `h298_kj_mol` / NASA `a6` / `thermo_points.h,g` are a formation enthalpy or an absolute
     electronic energy under the same key. On main nothing distinguishes the two. PR #1059 adds
     `atom_corrections_applied`, `bond_corrections_applied` and `atom_corrections_level` (branch
     `arc/output.py:2084-2087`), and also drops `energy_corrections` records whose flag is false
     (branch `:223`, `:1420`). `enthalpy_reference_kind` stays *derived*: it needs the correction
     level to equal the species' energy level, and output.yml cannot show that under
     `adaptive_levels`.
   - `thermo.energy_level_of_theory` can only come from `atom_corrections_level` (1.2), which
     always repeats `arkane_level_of_theory`, or from run-level `sp_level`/`composite_method`.
     No per-species energy level exists.
   - `statmech.e0_kj_mol`, for species and TSs, carries no correction marker and has no TCKDB
     field. It stays `arc_only`.
   - `freq_result.modes[]` is rebuilt from two lists. For TSs, `harmonic_frequencies_cm1` drops
     every negative mode (`output.py:2107-2110`). No mode order, reduced mass or intensities.
   - `rigid_rotor_kind` only ever takes the values `linear` and `asymmetric_top`.
   - `ess_versions` is a full banner and has to be split into version and revision.
   - All `artifacts[].filename` rows are run-relative paths with no content or sha256.
   - IRC `reaction_coordinate`, `max_gradient`, `rms_gradient` and the GSM `path_coordinate`:
     ARC names the unit, but TCKDB has no unit column for any of them.
   - `validation_evidence[].rationale`: TCKDB requires `min_length=1`, while ARC's
     `ts_checks.warnings` is usually `""`.
   - `constraints[].target_value`: the unit lives in `target_value_units`, which has no TCKDB column.
   - `kinetics.dEa`: its unit is separate from `Ea_units`.
4. **Conditional-only routes.** These deserve attention:
   - `workflow_tool_release.name` exists only in the sidecar (`parser_evidence.py:624`), and the
     sidecar is dropped on any build failure (`output.py:265-267`).
   - The GSM path is parsed only when the chosen method resolves to gsm
     (`parser_evidence.py:593-596`). NEB paths are never parsed.
   - `rotor_scans` holds only 1D, successful, fully parseable scans.
   - The scan `requested_*` fields exist only for Gaussian scans.
   - `freq_hessian_method` is null for composite levels, unlisted methods and cam-b3lyp.
5. **T0 has no TCKDB home.** ARC exports `kinetics.T0_k` (`output.py:2824`), but
   `BundleKineticsIn` has no reference-temperature field. The (A, n) pair is interpretable only
   under an assumed T0 = 1 K. The row is `arc_only`, and the risk is silent.

## Known-gap list: confirmed or refuted against code

| Claimed gap | Verdict | Evidence |
|---|---|---|
| Scan level / software for rotor scans | **Confirmed.** | A `rotor_scans[]` entry is only `{key, source_log, result, constraints?}` (`arc/output.py:2379-2386`). `write_output_yml` takes no scan level (`:113-131`; call site `arc/main.py:673-690`). `rotors_dict['scan_software']` exists but is used only to dispatch the constraint parser (`output.py:2420`). ess_versions and ess_software cover only opt, freq, sp and neb. IRC level (`main.py:346`) is also unexported. |
| Per-species energy level under `adaptive_levels` | **Confirmed.** | Only run-level `opt_level`/`freq_level`/`sp_level` are written (`output.py:176-182`). Adaptive levels are applied per species in the scheduler (`scheduler.py:451`, `:5273`). The PR #1059 doc says so outright (branch `docs/output_yml_schema.md:721`). |
| Run-level correction flag for kinetics / TS | **Confirmed.** | The 1.2 flags live only in the thermo block. Thermo is always null for TSs (`output.py:1990-1993`), and `_rxn_to_dict` has no flag (`:2808-2854`). The 1.2 drop logic leaves TS records untouched because their thermo is None (branch `output.py:1429-1432`). |
| e0 marker | **Confirmed.** | `statmech.e0_kj_mol` = `spc.e0` (`output.py:2113`). spc.e0 is set from the Arkane output parse (`statmech/arkane.py:1298`, `:1325`), from restart data (`species/species.py:916`), or from an Arkane YAML (`:1122`). No field records its source or whether AEC/BAC were applied. |
| Execution environment / effective settings | **Confirmed.** | `opt_final_settings` holds only `{'optimization_stage': 'fine'}` (`output.py:1910`). `freq_final_settings` and `sp_final_settings` are always None (`:1915-1916`). No server, queue, module, container or conda field is written anywhere in `write_output_yml` (`:161-229`). |
| Transport not exported | **Confirmed.** | output.py has no transport code at all. Transport goes only to an RMG library via `processor.py:232-240`, and only for OneDMin jobs. |
| `ts_checks` verdicts | **Refuted as an export gap.** | Exported since 1.1 (`output.py:1986`, `:2779-2805`; schema `:1814`) and tested (`output_schema_test.py:691-698`). The real gap is on the TCKDB side: `validation_evidence.kind` is `Literal["irc"]` (`fragments/ts_validation_evidence.py:58`), so only `ts_checks.IRC` has a home. E0, e_elect, freq and NMD are `arc_only`. It is absent from the golden fixture. |
| `wavefunction_stability` | **Refuted as an export gap.** | Exported (`output.py:1841-1842`, parser `:586-633`; schema `:2084-2156`) and tested (`output_test.py:1965`). It maps onto `scf_stability.*` only as *derived* values: the verdict vocabulary differs from `SCFStabilityStatus`, and the analysis is its own job with no host calculation. `scf_reference.sp_reference`/`freq_reference` newly supply `level_of_theory.spin_treatment` for freq and sp jobs, but lossily: the values are two-valued, and ROHF is indistinguishable from restricted. |

## Path-spelling problems in `paths_0_51.tsv`

1. **`transport_upload` is the wrong model.** `walk_paths.py` picks every class in
   `transport_upload.py` whose name ends in `UploadRequest`. The only match is the imported
   `LiteratureUploadRequest`, so all 12 `transport_upload.*` rows are literature fields (`kind`,
   `title`, `doi`, …). The real model is `TransportUploadPayload`
   (`tckdb_schemas/workflows/transport_upload.py:24`); the standalone `TransportUploadRequest` is
   backend-only. Its fields (`sigma_angstrom`, `epsilon_over_k_k`, …) appear correctly only under
   `conformer_upload.transport.*`.
2. **Forward-referenced lists are recorded as leaves.** `scheme.atom_params`, `bond_params` and
   `component_params` are `list["SchemeAtomParamPayload"]` (`energy_correction.py:92-94`). The
   walker reads them as leaves typed `list[Scheme…Payload]`, 5 routes each. It never descends to
   `atom_params[].element/.value`. I used the tsv's leaf spelling verbatim. The old inventory's
   `atom_params[].element` rows are therefore gone.
3. **One model name with differing shapes.** `scf_stability` has `source_calculation_id` and
   `source_artifact_id` under `ts_upload` and `conformer_upload` but not under `species_upload`.
   Not a spelling bug, but a join trap for sub-model tail matching.
4. **The join's near-miss check flagged two A2 paths**
   (`species_upload.thermo.applied_energy_corrections[].scheme.software.{name,version}`). They are
   false positives: the worktree's `TCKDB_DEMAND.yml` is still the 0.22-era A1 (2085 rows), and
   the paths are valid at 0.51.

## Coverage self-assessment

- **Strong:** header provenance, species identity, rotor scans, energy corrections, thermo,
  statmech, kinetics and all evidence-sidecar content. Every emitting line was read in
  `output.py`/`parser_evidence.py`, and each cited `file:line` was machine-checked to hold the
  named key.
- **Weaker:**
  - `fixture_evidence` for many output.yml rows points at the schema-validated rich document or
    at unit tests, not at a golden file. Several cite the test's *setup* line, not an assertion.
  - Availability `rare` for optional level-dict keys is my judgement of typical ARC inputs, not a
    measured frequency.
  - The `SCFStabilityStatus` mapping (`followed_to_stable` → `stabilized`) is a proposal, not
    something ARC states.
- **Deliberately excluded:** the `reaction_upload.species[]` mirror; the execution-environment
  subtree, literature and rights, for which ARC exports nothing; and the internal-only state ARC
  holds but does not write, which is A3's.
- **Not verified:** whether TCKDB's kinetics implicitly fixes T0 = 1 K. The schema is silent on it.
