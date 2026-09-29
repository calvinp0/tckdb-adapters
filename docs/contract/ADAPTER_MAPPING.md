# A4 — what the ARC→TCKDB adapter currently wires

Companion narrative to `ADAPTER_MAPPING.yml` (775 rows, all distinct paths).

This inventory is **purely descriptive**. Every row carries
`status_at_0_22_0: unknown`; the adapter was written against `tckdb-schemas`
**0.8.0** and TCKDB_v2's working tree is at **0.22.0**, so whether a target field
still exists, still has that name, or still has that type is A5's call and Phase
B's join. Where I could see that the adapter's target no longer matches the
0.22.0 module layout at all, I said so in this narrative rather than editing the
`path` — paths are spelled the way the adapter spells them, per the brief.

## 1. Path-root caveat — read this before joining

`FIELD_KEY.md` defines exactly two roots, `species_upload` and
`reaction_upload`. The adapter builds **four** distinct wire payloads:

| Adapter entry point | Endpoint | Root used in the YAML | Joins against A1/A5? |
|---|---|---|---|
| `submit_computed_species_from_output` (adapter.py:899) | `/uploads/computed-species` | `species_upload` | yes |
| `submit_computed_reaction_from_output` (adapter.py:1945) | `/uploads/computed-reaction` | `reaction_upload` | yes |
| `submit_computed_ts_from_output` (adapter.py:2782) | `/uploads/transition-states` | `transition_state_upload` | **no** |
| `submit_from_output` (adapter.py:742) | `/uploads/conformers` | `conformer_upload` | **no** |

The last two have no root in the grammar because they have no published model in
`tckdb_schemas` at 0.22.0 — there is no `workflows/transition_state_upload.py`
and no `workflows/conformer_upload.py` in the package (`tests/test_ts_upload.py`
reconstructs `TransitionStateUploadRequest` locally from fragment schemas and
says so explicitly at its module docstring). I invented the two extra roots
rather than dropping ~188 real mappings or misfiling them under
`species_upload`. **Phase B must treat `transition_state_upload.*` and
`conformer_upload.*` as non-joining rows**: they are adapter behaviour that A1's
demand inventory cannot possibly cover, and a naive join will report them as
orphans. That orphan-ness is itself the finding — the adapter POSTs to two
endpoints whose request contracts are not in the shared schema package.

## 2. Structural map of `adapter.py` (6,788 lines)

| Lines | Region | Contents |
|---|---|---|
| 1–72 | imports, module docstring | three stated guarantees (no-op when disabled; payload on disk before any network call; failure is non-fatal unless `strict`) |
| 74–253 | neutral-record translators | `_serialize_calc_constraints`, `_scan_entries_from_record`, `_neutral_scan_result_to_tckdb`, `_correction_records_from_record` — the ARC-output-1.1 "tool-neutral" → legacy-TCKDB-shape bridge |
| 256–632 | constants + small resolvers | endpoint/kind constants, calc-key namespace, artifact field maps, TS-guess method maps, kinetics unit maps, `_resolve_ts_guess_path_search*`, `_coerce_artifact_filename` |
| 634–701 | dataclasses | `UploadOutcome`, `ArtifactUploadOutcome`, `_PreparedArtifactUpload`, `TCKDBReadinessError` |
| 703–737 | `TCKDBAdapter.__init__` | config, payload writer, evidence store, one-shot warn flags |
| 742–893 | conformer + artifact entry points | `submit_from_output`, `submit_artifacts_for_calculation`, `submit_artifact_batch_for_calculation` |
| 899–1047 | **computed-species bundle** | `submit_computed_species_from_output`, `_build_computed_species_payload` |
| 1049–1345 | **conformer blocks** | `_build_conformer_block` (selected), `_build_alt_conformer_blocks` (screened) |
| 1347–1552 | **calc-in-bundle core** | `_build_calc_in_bundle`, `_output_geometries_for_calc`, `_input_geometries_for_calc` |
| 1554–1734 | coarse-opt + inline artifacts | `_build_opt_coarse_calc`, `_inline_artifacts_for_calc`, `_read_inline_artifact`, `_resolve_local_path` |
| 1736–1939 | **evidence-backed reparse seams** | `_build_freq_hessian_payload`, `_parse_irc_trajectories` |
| 1945–2203 | **computed-reaction bundle** | `submit_computed_reaction_from_output`, `_build_computed_reaction_payload` |
| 2205–2454 | reaction species blocks | `_build_reaction_species_block` |
| 2456–2776 | **TS block** | `_build_ts_block` (ts_guess/path_search, opt, freq, sp, irc, AEC) |
| 2782–3052 | **standalone TS request** | `submit_computed_ts_from_output`, `_compose_transition_state_request`, `_ts_calc_to_standalone`, `_build_ts_reaction_upload` |
| 3058–3293 | shared payload shapers | `_species_entry_payload`, `_build_payload`, `_build_calculations`, `_calculation_payload` |
| 3299–3785 | upload/sidecar/network machinery | `_finalize_skipped`, `_upload`, `_record_failure`, `_ensure_ready` (readyz retry), artifact batch upload |
| 3793–3977 | HTTP/sidecar helpers | batch digests, request-id extraction, readiness message formatting |
| 3980–4176 | provenance qualifiers + level resolution | `_sp_is_reused_from_opt`, `_reused_origin`, `_final_settings_for_calc`, `_merge_parameters_json`, `_screened_conformer_origin`, `_resolve_level` |
| 4179–4366 | **result payloads** | `_opt_result_payload`, `_coarse_opt_result_payload`, `_freq_result_payload`, `_sp_result_payload`, `_spin_diagnostic_payload` |
| 4369–4540 | **level-of-theory + energy corrections** | `_arc_args_to_keywords`, `_arc_level_to_tckdb_lot`, `_scheme_level_of_theory`, `_build_applied_energy_corrections` |
| 4543–4758 | **thermo** | `_build_thermo_block`, `_build_nasa_block`, `_build_thermo_points` |
| 4761–4872 | key minting + release refs | `_safe_key_part`, `_local_key_for_actor`, `_calc_prefix_for_actor`, `_index_species`, `_index_transition_states`, `_arc_workflow_tool_release`, `_arc_analysis_software_release` |
| 4875–5349 | **statmech** | `_build_freq_scale_factor_ref`, `_build_statmech_block_for_species`, `_classify_statmech_treatment`, `_build_slim_torsions`, `_coerce_torsion_coordinates`, `_build_statmech_source_calculations` |
| 5352–5477 | TS handle + reaction result flattening | `_ts_unmapped_smiles_handle`, `_flatten_result_fields`, `_flatten_all_reaction_calcs` |
| 5480–5783 | **kinetics** | `_build_kinetics_block`, `_build_kinetics_source_calculations` |
| 5786–6017 | **IRC result** | `_detect_irc_direction`, `_build_irc_result_payload` |
| 6020–6258 | xTB/GSM on-disk parsers | `_level_keys_match`, Turbomole `energy`/`gradient` parsers, `.xtbout` regex parser, `_read_gsm_node_outputs` |
| 6261–6588 | **path-search result** | `_build_path_search_result_payload`, `_resolve_irc_zero_energy_reference` |
| 6591–6788 | xyz normalization + response helpers | `_normalize_xyz_text`, `_require_xyz_text`, `_summarize_response_body`, `_extract_tckdb_public_refs`, `_extract_calc_refs`, `_skip`, `_close_quietly` |

## 3. The three ARC-optional reparse paths

`_arc_optional.py` is a lazy, guarded `import arc` boundary. Its wrappers raise
`OptionalArcUnavailable`, a subclass of `ImportError`, so every caller's
pre-existing `except Exception` / `except ImportError` degrades to "sub-payload
omitted" instead of crashing. All three paths are **evidence-first**: if ARC
emitted a valid `output/tckdb_evidence.json` sidecar (output schema 1.1), the
adapter never touches ARC's parsers at all.

| Path | Fallback call site | ARC symbols needed | What is silently lost when ARC is absent AND no sidecar exists |
|---|---|---|---|
| **freq Hessian** | `_build_freq_hessian_payload`, adapter.py:1789 | `arc.parser.parser.determine_ess`, `arc.parser.factory.ess_factory` | The entire `hessian` sub-payload on every freq calc — `geometry.xyz_text`, `lower_triangle_hartree_bohr2`, `source`, `parser_version`. Caught by the `except Exception` at adapter.py:1800, logged at **debug** level only. |
| **IRC trajectory** | `_parse_irc_trajectories`, adapter.py:1891 | `arc.parser.parser.parse_irc_path`, `parse_irc_traj` | The entire `irc_result` sub-payload (points, geometries, energies, gradients, reaction coordinates, TS marker, `zero_energy_reference_hartree`). The `type=irc` calc node itself **is still emitted** (adapter.py:2682) with its `depends_on(irc_start)` edge, so `kinetics.source_calculations(role=irc)` still resolves — the calc exists but carries no scientific content. Per-log failures logged at **debug**. |
| **GSM string-file** | `_build_path_search_result_payload`, adapter.py:6307 | `arc.parser.parser.parse_trajectory`, `parse_gsm_stringfile_energies`, `arc.species.converter.kabsch` | The multi-frame GSM path collapses to a **single-point fallback** built from `transition_states[].opt_input_xyz` (adapter.py:6437–6450), marked `is_ts_guess=True`. `n_points` becomes 1, per-node energies/gradients/`path_coordinate` vanish, and `is_climbing_image` is never set. The trajectory parse failure is logged at **warning**; the `kabsch` and `parse_gsm_stringfile_energies` failures at **debug**. NEB always takes this single-point fallback regardless of ARC — NEB image extraction is explicitly described as "a future parser lift" (adapter.py:6287). |

**This is a real data-loss surface.** In all three cases the payload still
validates and still uploads: a base `tckdb-arc` install processing a pre-1.1 ARC
run produces a *smaller but structurally valid* payload, with no error, no
warning at INFO or above (except the GSM trajectory-parse case), and no marker on
the wire saying the data was omitted. The GSM case is the worst of the three
because it does not omit — it **substitutes** a one-point path for an N-point one,
which reads downstream as "ARC only computed one image", not as "the parser was
unavailable".

`_vendor.py` deliberately does *not* vendor `kabsch` (it would drag in ARC's
atomic-mass subsystem and scipy), so the GSM `path_coordinate` derivation is
ARC-coupled even when everything else on that path is not.

## 4. Hardcoded, defaulted, or fabricated values — provenance risks

Everything below is asserted by the adapter rather than read from ARC. Ordered
roughly by how much scientific weight it carries.

### Fabricated scientific content

* **`path_search_result.converged = True`** (adapter.py:6531) — hardcoded on
  every emitted path-search calc. Justified in the comment by the emission gate
  (a log path only exists when the guess succeeded), but no ARC convergence flag
  is consulted.
* **`opt_result.converged = True` for `opt_coarse`** (adapter.py:4207) —
  hardcoded, on the convention that ARC only writes `coarse_opt_log` after a
  successful coarse run. ARC emits no `coarse_opt_converged` field, and the
  docstring concedes a real field "would be slightly more honest".
* **Synthesized imaginary mode** (adapter.py:4280) — ARC's
  `statmech.harmonic_frequencies_cm1` lists only real modes, so the adapter
  *inserts* a mode `{frequency_cm1: -abs(imag_freq_cm1), is_imaginary: True}` at
  position 0 to make `count(is_imaginary) == n_imag` hold.
* **Synthesized IRC TS-marker point** (adapter.py:5980–6002) — an extra point
  appended after all trajectory points with `is_ts=True`,
  `reaction_coordinate=0.0` (hardcoded), and the same energy used as the zero
  reference, so its `relative_energy_kj_mol` is 0.0 by construction. It is ARC's
  IRC *seed* geometry, not a parsed trajectory point.
* **`irc_result.direction` defaults to `"both"`** (adapter.py:5964–5970) when no
  direction could be established from evidence, scheduler list, or filename. The
  comment justifies this from an ARC invariant, but with a single unlabeled log
  the payload will claim a two-branch IRC.
* **`transition_state.unmapped_smiles`** (adapter.py:5405) — when the TS record
  has no SMILES, the adapter *builds* a reaction-SMILES handle
  `"<r1>.<r2>>><p1>.<p2>"` by concatenating participant SMILES. It is a
  deterministic traceability handle, not a structure, but it is producer-invented
  text sitting in an identity field.
* **`applied_energy_corrections[].value_unit` / `scheme.units`**
  (adapter.py:226) — when the neutral record omits the unit, the adapter defaults
  to `"hartree"` for atom-energy corrections and `"kcal_mol"` for bond-additivity.
  A silently wrong unit here is a direct scientific error.
* **`scheme.name = scheme.kind`** (adapter.py:228) — ARC supplies no scheme name.
* **`scan_result.coordinates[].resolution_degrees`** (adapter.py:169) — the
  producer's `requested_step_size` is written into a *degrees*-named field
  without consulting `coordinate.unit`.

### Hardcoded enum/literal assertions

* `species_entry.molecule_kind = "molecule"` (adapter.py:3080).
* `species_entry.electronic_state_kind = "ground"` (adapter.py:3085) — ARC has no
  excited-state workflow; asserted deliberately rather than left to a server
  default.
* `calculation.quality = "raw"` (adapter.py:3249) on every calc.
* `output_geometries[].role = "final"` (adapter.py:1498) on every output geometry.
* `kinetics.model_kind = "modified_arrhenius"` (adapter.py:5593).
* `kinetics.a_uncertainty_kind = "multiplicative"` (adapter.py:5696).
* `statmech.freq_scale_factor.scale_kind = "fundamental"` (adapter.py:4959) — ARC
  does not distinguish ZPE/enthalpy/entropy/Cp scale factors.
* `path_search_result.is_double_ended = True` / `source_endpoint_count = 2`
  (adapter.py:434, applied at 6540) — static per-method table.
* `workflow_tool_release.name = "ARC"`, `analysis_software_release.name =
  "Arkane"` (adapter.py:4850, 4872).
* `reaction_family_source_note = "ARC-reported family"` (adapter.py:2187, 3051) —
  stamped unconditionally whenever a family is emitted, because the producer has
  no copy of TCKDB's canonical-family list.
* `conformer_upload.scientific_origin = "computed"` (adapter.py:3118).
* `transition_state_upload.reaction.reversible` **defaults to `True`**
  (adapter.py:3034) because the standalone schema requires the field and ARC has
  no `reversible` attribute. On the computed-reaction path the same missing value
  is instead *omitted* (adapter.py:2165) — the two paths disagree.
* `parameters_json.tckdb_origin` markers (`_reused_origin`, adapter.py:4020;
  `_screened_conformer_origin`, adapter.py:4122) — `origin_kind`, human-readable
  `reason` strings, `independent_ess_job: False`, and `producer: "ARC"` are all
  adapter-authored.
* `_HESSIAN_PARSER_VERSION = "arc-hessian-1"` (adapter.py:320) stamped on
  reparsed Hessians.

### Defaults that mask absence

* `species_entry.charge` defaults to `0`, `multiplicity` to `1`
  (adapter.py:3082–3083) — an ARC record missing multiplicity uploads as a
  singlet.
* `transition_state.charge` defaults to `0` (adapter.py:2754).
* `_resolve_level` (adapter.py:4156) **falls back `freq_level`/`sp_level` →
  `opt_level`** whenever the job-specific level is null. Common ARC runs declare
  only `opt_level`, so freq and sp calcs are labelled with the opt level of theory.
* `software_release.version` falls back to `ess_versions['opt']` when the
  job-specific ESS version is absent (adapter.py:3243).
* `freq_scale_factor.software` falls back to `opt_level.software`
  (adapter.py:4969).
* `scan_result.coordinates` `index_base` **defaults to 1** (adapter.py:139) — see
  §5.
* `scan_result.dimension` defaults to `1`, `is_relaxed` to `True`
  (adapter.py:191–192); `value_unit` defaults to `"degree"` (adapter.py:155).
* TS multiplicity falls back to the reaction's multiplicity (adapter.py:2744).
* Conformer key defaults to the literal `"conf0"` (adapter.py:924); TS label
  falls back to `"unlabeled-ts"` (adapter.py:2899); species label falls back to
  `"unlabeled"` (adapter.py:923).

## 5. Unit conversions and index bases — the audit's high-value findings

### Unit conversions actually performed

| Target | Conversion | Constant | Site (working tree) |
|---|---|---|---|
| `irc_result.points[].relative_energy_kj_mol` | Hartree → kJ/mol | `_vendor.E_h_kJmol = E_h·N_A/1000 = 2625.4998583629967` | `_vendor.py:43`, applied `adapter.py:6729` & `:6785` |
| `path_search_result.points[].relative_energy_kj_mol` (absolute-energy branch) | Hartree → kJ/mol | `_vendor.E_h_kJmol` (same constant, same value) | `_vendor.py:43`, applied `adapter.py:7264` |
| `path_search_result.points[].relative_energy_kj_mol` (string-file branch) | kcal/mol → kJ/mol | `_KCAL_MOL_TO_KJ_MOL = 4.184` | `adapter.py:496`, applied `:7303` |

**Status: unified (PHASE_C_PLAN.md C-3, fixed).** Both Hartree→kJ/mol call sites now
import and consume the single vendored `_vendor.E_h_kJmol` — the table above no
longer documents two constants because there is only one. Historically (through
`tckdb-adapters` `HEAD` commit `fedfe6b`) they did differ: the IRC path used a
literal `_HARTREE_TO_KJ_MOL = 2625.4996` defined inline "to keep this helper free of
further imports" (`HEAD` `adapter.py:5817–5820`, applied `:5943` & `:5999`), while
the GSM/path-search path already used the vendored constant (`HEAD` `adapter.py:6478`).
Two relative-energy fields with the same name and unit were therefore computed with
numerically different factors — relative discrepancy `|2625.4998583629967 −
2625.4996| / 2625.4996 ≈ 9.84e-8` (not the `~1.5e-8` this section previously stated —
that figure was itself in error), scientifically negligible but a genuine
consistency defect, now closed. Rebuilding the golden fixture through the pre-fix
and post-fix code confirms the fix touches exactly four `relative_energy_kj_mol`
leaves, all under `irc_result` (two in the reaction-route TS block, two in the
matching standalone TS payload); `path_search_result` does not move, because it was
never on the stale constant to begin with (`tests/test_golden_corpus.py`'s
`test_phase3_disk_corpus_builds_all_payloads_from_sidecar`).

Everything else is **unit-preserving passthrough**: Hartree energies
(`opt_final_energy_hartree`, `sp_result.electronic_energy_hartree`,
`zpe_hartree`, all `zero_energy_reference_hartree`), cm⁻¹ frequencies, K
temperatures, kJ/mol and J/(mol·K) thermo scalars, and the Hessian's native
Hartree/Bohr² lower triangle (explicitly *not* converted, adapter.py:1750–1754).
Kinetics `A` and `Ea` are never converted — the unit rides alongside as a mapped
enum, and an **unrecognized unit string silently drops the unit field while the
number still ships** (`arc_to_tckdb_a_units`, adapter.py:597; the miss is logged
at debug only). That is the single most dangerous unit path in the adapter.

Two field renames drop the unit from the name without converting:
`max_gradient_hartree_per_bohr` → `max_gradient` and
`rms_gradient_hartree_per_bohr` → `rms_gradient` (adapter.py:1873–1874,
6340–6341). Likewise `reaction_coordinate_sqrt_amu_bohr` →
`reaction_coordinate` (adapter.py:1872).

### Atom-index bases — three different policies in one adapter

1. **`constraints[].atomN_index` — rebased, conditionally.**
   `constraints.py:202` applies `atom - index_base + 1`, where `index_base` is
   read from the tool-neutral record and must be 0 or 1. **But the rebase is
   gated on `coordinate_type` being present** (`constraints.py:193`): a *legacy*
   parser dict using the `atoms` key is assumed already 1-based and is passed
   through unshifted. Two shapes, two policies, in one function.
2. **`scan_result.coordinates[].atomN_index` — rebased, with a dangerous
   default.** `adapter.py:140` does `int(atom) - index_base + 1` but reads
   `index_base` with `coordinate.get("index_base", 1)` — i.e. **a 0-based ARC
   coordinate that omits `index_base` is emitted unshifted**, producing a silent
   off-by-one in every scanned dihedral. Unlike `constraints.py`, there is no
   validity check on the resulting indices beyond a length ≤ 4 test
   (adapter.py:149).
3. **`statmech.torsions[].coordinates[].atomN_index` — never rebased.** ARC's
   statmech torsion `atom_indices` are documented as already 1-based
   (adapter.py:5182). `_coerce_torsion_coordinates` (adapter.py:5269) only
   *validates* (exactly 4 entries, all ≥ 1, all distinct) and drops the
   coordinate list on failure. There is no `index_base` field to consult here at
   all, so the 1-based-ness is an unverifiable producer-side assumption.

Two other index bases worth flagging for the join: `scan_result.points[].point_index`
is **1-based** (adapter.py:172) while `irc_result.points[].point_index` is
**0-based** (adapter.py:5920). `torsions[].torsion_index` counts only *emitted*
torsions (adapter.py:5232, 5265), so a rotor dropped for an unrecognized
treatment silently shifts every subsequent index away from ARC's rotor ordinals.

## 6. Structural rewrites worth flagging to Phase B

* **Reaction-bundle result flattening.** `_flatten_all_reaction_calcs`
  (adapter.py:5453) runs once at the end of `_build_computed_reaction_payload`
  and rewrites every calc in place: `opt_result` / `freq_result` / `sp_result`
  are unwrapped into flat `opt_converged`, `opt_n_steps`,
  `opt_final_energy_hartree`, `freq_n_imag`, `freq_imag_freq_cm1`,
  `freq_zpe_hartree`, `sp_electronic_energy_hartree`. Only those three are
  flattened — `scan_result`, `irc_result`, `path_search_result`, `hessian`, and
  `spin_diagnostic` stay nested. So the *same* datum has two path spellings
  depending on which root you are under; the YAML records both.
* **`freq_result.modes[]` collapses to `freq_frequencies_cm1`** on the reaction
  path (adapter.py:5450): the list of `{frequency_cm1, is_imaginary, mode_index}`
  objects becomes a bare list of floats. `is_imaginary` and `mode_index` are
  discarded; the sign of each float is the only surviving imaginary marker.
* **NASA coefficient explosion**: `nasa_low.coeffs[0..6]` → `a1..a7`,
  `nasa_high.coeffs[0..6]` → `b1..b7` (adapter.py:4689–4692).
* **AEC parameter-table explosion**: the `{element: value}` /
  `{bond_key: value}` mapping becomes a sorted list of two-key objects
  (adapter.py:237, 242).
* **`level_of_theory.keywords`**: ARC's nested `args` mapping is flattened into
  one deterministic string `"category:key=<json>; …"` with sorted categories and
  keys (adapter.py:4397). ARC's `method_type`, `year`, `solvation_scheme_level`,
  `compatible_ess`, `software`, `software_version` are dropped at that seam
  (adapter.py:4379–4385).
* **`_ts_calc_to_standalone`** (adapter.py:2967) strips `key`, `depends_on`,
  `geometry_key`, and `artifacts` from every calc on the standalone-TS path, and
  `_compose_transition_state_request` drops the TS's
  `applied_energy_corrections` entirely (adapter.py:2931, debug-logged). Inline
  artifacts are skipped up front via `include_artifacts=False` with a one-time
  WARNING (adapter.py:2902).
* **`xyz_text` normalization**: ARC's atom-only xyz block is turned into
  canonical XYZ by prepending an atom-count line plus a comment line carrying the
  label (`_normalize_xyz_text`, adapter.py:6591). Angstroms in, Angstroms out —
  no coordinate transformation.

## 7. Deliberate non-mappings (recorded here, not in the YAML)

These are ARC data the adapter sees and consciously refuses to forward. I kept
them out of `ADAPTER_MAPPING.yml` because they are A2/A3 territory
(`arc_only.*`), but they are adapter *decisions* and belong in the record:

* `species[].conformer_energies` — workflow-local relative E0 values whose
  reference is "lowest in this screening set". Explicitly dropped, with an
  instruction not to reintroduce them via `note` or `parameters_json`
  (adapter.py:1248–1259, 1338–1341).
* `statmech.torsions[].pivot_atoms` and `torsions[].barrier_kj_mol` — no bundle
  column (adapter.py:5217–5219).
* `transition_states[].chosen_ts_method` as provenance on `ts_opt` — deliberately
  *not* emitted as a `tckdb_origin` marker; treated as workflow narrative
  (adapter.py:2589–2600). It is used only to gate the `path_search` calc.
* `atom_map`, `ts_report`, `successful_methods`, `server`, `job_id`,
  `relative_e0_kj_mol` — asserted absent from every built payload by
  `test_golden_corpus.py`'s `FORBIDDEN_KEYS` check. Notably **there is no
  reaction atom-map mapping anywhere in the adapter**, and the golden corpus test
  actively enforces its absence.
* Level fields `method_type` / `year` / `solvation_scheme_level` /
  `compatible_ess` (adapter.py:4384).
* `applied_energy_corrections[].components[].parameter_unit` — stripped by the
  five-field allowlist (adapter.py:4369).

## 8. TODO / FIXME / XXX

**None.** A grep for `TODO|FIXME|XXX|HACK` across `tckdb_arc/tckdb_arc/*.py`
returns no hits. Known-incomplete work is instead expressed as prose in
docstrings; the two that matter are:

* adapter.py:6287 — "NEB log image extraction is a future parser lift"; NEB
  path-search results are always single-point.
* adapter.py:5197 — `source_scan_calculation_key` support described as "deferred
  until ARC emits scan calcs" (the surrounding code now does handle it, so this
  comment is stale rather than an open gap).

A third, adapter.py:1579–1586, documents a *known-wrong* server-side outcome: the
bundle workflow auto-anchors every calc's output geometry to the fine opt's
geometry, so `opt_coarse`'s output-geometry row "will incorrectly point at the
fine geometry server-side". The producer emits explicit `output_geometries` to
mitigate this; whether 0.22.0 still behaves that way is A5/Phase B's call.

## 9. Golden fixtures vs. what `adapter.py` can emit

`tckdb_arc/tests/fixtures/golden/` is **two files**: `phase3_output.yml` (85
lines) and `tckdb_evidence.json` (73 lines). Its own README describes it as "a
small H/H₂ exchange example". It is a *Phase-3 evidence-sidecar* fixture, not a
mapping-coverage corpus, and `test_golden_corpus.py::test_phase3_disk_corpus_builds_all_payloads_from_sidecar`
uses it precisely to prove the sidecar is sufficient without ARC (it patches
`require_arc_parser` to raise).

**Covered by the golden fixture:** species_entry identity; conformer geometry;
opt/freq/sp calcs and their results; `ess_versions` version fallback; opt/freq/sp
level resolution (all three levels declared, `sp_level` differing so the
reused-from-opt marker is *not* exercised); `opt_input_xyz` input geometries;
freq Hessian **from the sidecar**, for both a species and the TS; IRC forward +
reverse **from the sidecar**; a three-frame GSM path **from the sidecar** whose
endpoint lacks an absolute energy; `chosen_ts_method: xtb_gsm` → `method: "gsm"`;
TS multiplicity fallback; the derived TS reaction-SMILES handle (TS `smiles` is
null); `reaction_family`; ARC workflow-tool release. All three payload builders
are exercised and byte-pinned by canonical SHA-256 snapshots.

**Not covered by the golden fixture at all** — every one of these is adapter code
that no golden payload exercises:

| Region | Why uncovered |
|---|---|
| thermo (h298/s298/NASA/points/source_calculations) | no `thermo` key on any record |
| statmech (all of it) | no `statmech` key; no `freq_scale_factor`, no `freq_scale_factor_source` |
| torsions / `statmech_treatment` / torsion coordinates | ditto |
| rotor scans (`scan_result`, its coordinates and the index rebase) | no `rotor_scans` / `additional_calculations` |
| constraints (and their index rebase) | no `*_constraints` fields |
| applied energy corrections (AEC/BAC) | no `energy_corrections` / `applied_energy_corrections` |
| kinetics (the entire `_build_kinetics_block`) | `kinetics: null` in the fixture |
| coarse opt (`opt_coarse` calc, its chain edge, its output geometry) | no `coarse_opt_log` |
| screened alt conformers + `_screened_conformer_origin` | no `conformers` list |
| `_reused_origin` (sp reused from opt) | `sp_level` differs from `opt_level` |
| spin diagnostic | no `sp_spin_diagnostic` |
| `final_settings` → `parameters_json` | no `*_final_settings` fields |
| inline artifacts (base64 logs/input decks) | README says "deliberately contains no ARC calculation files" |
| `analysis_software_release` | no `arkane_git_commit` |
| `unmapped_smiles` passthrough on species_entry | no `unmapped_smiles` on any record |
| **all three ARC-reparse fallbacks** | the test poisons `require_arc_parser`; only the evidence path runs |
| NEB path search | fixture uses GSM only |
| `gsm_node_outputs/` on-disk energy/gradient parsing | evidence path used instead |

The synthetic corpus in `tests/test_adapter.py` (9,113 lines, via `_fake_output_doc`
/ `_full_record` / `_reaction_output_doc`) covers considerably more of the above,
and `test_golden_corpus.py` validates two of those synthetic payloads against the
real published `tckdb_schemas` models. But the **frozen, checked-in golden
corpus** — the thing a reviewer would point at as "this is what the wire looks
like" — covers roughly the opt/freq/sp/Hessian/IRC/GSM spine and nothing else.
Thermo, statmech, kinetics, scans, constraints and energy corrections have no
golden payload.

## 10. Coverage self-assessment

**Read closely, line by line** (I am confident the YAML is complete for these):

* adapter.py 1–260 (neutral translators + constants), 634–737, 742–1060,
  1049–1560 (conformer/alt-conformer/calc-in-bundle/geometry policy),
  1554–1940 (coarse opt, artifacts, Hessian, IRC parse), 1945–2460
  (computed-reaction + reaction species block), 2456–2790 (TS block),
  2782–3130 (standalone TS + species_entry + `_build_payload`), 3196–3300
  (`_calculation_payload`), 3980–4550 (origins, level resolution, all result
  payloads, LoT projection, AEC), 4543–4800 (thermo + key minting),
  4805–5350 (release refs + statmech), 5352–5790 (TS handle, flattening,
  kinetics), 5786–6020 (IRC result), 6045–6260 (xTB/GSM file parsers),
  6261–6640 (path search, IRC zero ref, xyz normalization).
* `constraints.py`, `_vendor.py`, `_arc_optional.py`, `evidence.py` — all read in
  full.

**Skimmed** (read, understood, judged to contain no payload-field mappings):

* adapter.py 3299–3790 — upload, sidecar finalization, `_record_failure`,
  `_ensure_ready` readyz retry loop, artifact batch upload. I did read
  `_prepare_artifact_upload` (3572) closely because it shapes the *standalone*
  artifact sidecar, but that path writes sidecar metadata rather than TCKDB
  payload fields, and in both bundle modes it self-suppresses (adapter.py:3584).
* adapter.py 3793–3977 — batch digests, request-id/header extraction, readiness
  message formatting. No mappings.
* adapter.py 6641–6788 — response-parsing helpers (`_extract_tckdb_public_refs`,
  `_extract_calc_refs`). These read *server responses*, not ARC output, so they
  are outside A4's remit.
* `payload_writer.py`, `sweep.py`, `config.py`, `idempotency.py`, `cli.py` — I
  inspected their public surfaces (`IMPLEMENTED_ARTIFACT_KINDS`, upload-mode
  constants, `IdempotencyInputs`) but did not walk them line by line. `sweep.py`
  and `cli.py` are dispatchers over the `submit_*` entry points; `payload_writer.py`
  and `idempotency.py` write local sidecars and idempotency keys, neither of which
  crosses into a TCKDB payload field. If any of those files contains a payload
  mutation I would have missed it. I assess that risk as **low but non-zero**.

**Known uncertainties, stated plainly:**

* `UNVERIFIED: whether ARC's tool-neutral scan records actually carry
  `index_base`.` The adapter defaults it to 1 (adapter.py:139). If ARC's producer
  omits it on 0-based data, every scanned-coordinate atom index is off by one and
  nothing in the adapter would notice. A2 can settle this from the ARC side.
* `UNVERIFIED: whether ARC's statmech torsion atom_indices are genuinely
  1-based.` The adapter asserts it in a docstring (adapter.py:5182) and never
  rebases. Only ARC-side evidence can confirm.
* `UNVERIFIED: the GSM node-label → stringfile-frame-index identity.` The adapter
  documents it as "confirmed on real reaction_06 data" (adapter.py:6413–6421)
  from a single observation; mismatched indices are silently skipped, so a
  producer-side change would quietly strip per-node energies.
* `UNVERIFIED: the request contracts for /uploads/transition-states and
  /uploads/conformers.` Neither has a published model in `tckdb_schemas` 0.22.0;
  `test_ts_upload.py` reconstructs the TS one locally and says it was "verified
  against the TCKDB Pi" at some past point. I recorded those 188 rows from the
  adapter's own emission code, not from a schema.
* Line numbers in `source` point at the **emission site** (where the key lands in
  the dict). Where the *value* is computed elsewhere I named the helper and its
  line in `transform`. A few rows share a single emission line because the
  adapter writes several keys in one loop (e.g. `nasa.a1..a7` at adapter.py:4690,
  LoT fields at adapter.py:4453); that is accurate, not sloppy.
