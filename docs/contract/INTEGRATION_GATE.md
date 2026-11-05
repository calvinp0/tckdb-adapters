# Live TCKDB integration gate

The unit suite validates payloads against the shared schemas but never shows
what TCKDB stores. `tckdb_arc/tests/integration/` uploads the offline corpora
through the adapter's real transport (`tckdb-client`) to a TCKDB backend you
run locally, then reads the rows back through the REST API.

The gate is opt-in. Without `TCKDB_INTEGRATION_URL` its live tests skip, and
only the offline guard tests run. Loopback is not enough to prove isolation:
TCKDB's API listens on 127.0.0.1:8010 on the production host and on a
development machine, and an `ssh -L 8010:...` tunnel makes a remote deployment
loopback too. So the run fails unless all three hold:

- the URL's host is `127.0.0.1`, `localhost` or `::1`;
- its port is not 8010;
- `GET /status` reports an artifact bucket starting with `tckdb-integ`
  (deployments use `tckdb-artifacts`).

The adapter reads its key from `TCKDB_INTEGRATION_API_KEY`, so an ambient
`TCKDB_API_KEY` is never used.

Validated against TCKDB_v2 `c7c8df8b`, with tckdb-client 0.184.0, tckdb-schemas 0.154.0, tckdb-core 0.4.1 and tckdb-arc 0.14.1:
69 passed and 1 strict xfail, fresh (empty database) and on replay (a second run against the same database), 70 tests. The xfail is the scan-points read-back, **still failing at `c7c8df8b`** (`GET /calculations/{id}/scan-points` answers 422, its response model rejecting the ORM `Geometry` once a point has a geometry; the strict xfail did not flip). Moving the pin from `f6cfecbf` through schemas 0.145-0.154 changed no count: no adapter change was needed, and the only 422s in the run are the two deliberate refusals (`species_stereo_contradiction`, and `species_geometry_composition_mismatch` for the two-atom ethanol corpus), plus the scan-points read. Of the new server warnings, two fire (`scan_point_coordinate_disagrees_with_geometry`, 0.146; `level_of_theory_disagrees_with_artifact`, 0.150; table below); none of the 0.151 (`t1_diagnostic_above_closed_shell_threshold`, `spin_contamination_above_conventional_threshold`, `spin_expected_contradicts_multiplicity`), 0.152 (`reported_setting_differs_from_requested`) or 0.154 (`output_log_job_not_bound`) warnings fires, nor `parameter_disagrees_with_artifact`, `input_deck_disagrees_with_output_log` or `output_log_disagrees_with_earlier_output_log` (0.150). The 0.151 pair would have fired on `arc_1_3_ts_atom_map` while its closed-shell fixtures carried a radical's spin block (`TS1`, multiplicity 1, `s_squared` 0.7646, `s_squared_expected` 0.75); the fixtures no longer state a spin block for a closed-shell record, as a restricted job prints no `<S**2>`, so the gate now exercises none of them (a T1 or spin value on a genuine radical or open-shell record would).

Earlier validated against TCKDB_v2 `f6cfecbf`, with tckdb-client 0.181.0, tckdb-schemas 0.144.0, tckdb-core 0.3.1 and tckdb-arc 0.13.1:
69 passed and 1 strict xfail, fresh (empty database) and on replay (a second run against the same database); 70 tests, of which 5 are new (three tests and two replay cases)
(below). The xfail is the scan-points read-back, **still failing at `f6cfecbf`** (`GET /calculations/{id}/scan-points` still fails its own response model once a
point has a geometry; the strict xfail did not flip). What 0.135-0.144 changed for these corpora, learned by running the unchanged gate first (with only the
pin moved): **no refusal fired and no adapter payload changed**; one read-back shape moved. The species read model of `/scientific/reaction-entries/{id}/full`
(`include=species`) now lists one member per species entry with a `stoichiometry`, so the repeated reactant of `CH3 + CH3 <=> C2H6` reads back as one member of
stoichiometry 2 (it was two members); the atom map still has one participant per slot. The two atom-map tests for that corpus (bundle and standalone) now assert the
new shape. New server codes seen: none beyond the deliberate 422 below. The two TCKDB rules this release brought that bear on what the adapter sends are covered live:

- **0.138 `species_stereo_contradiction`.** `stereo_consistent` (SMILES `C/C=C/C`, an E alkene, with a trans geometry, C-C=C-C dihedral 180.0) is accepted with no
  `species_stereo_contradiction` (the only server warning is `converged_opt_no_usable_energy`, the corpus having no energy); `stereo_contradicting` (the same SMILES
  with a cis geometry, dihedral 0.0) is refused 422 `species_stereo_contradiction`, recorded in the sidecar. The adapter sends ARC's SMILES and geometry and no
  `stereo_label`, and does not pre-check the rule. Both geometries are RDKit ETKDG + MMFF embeddings.
- **0.144 `hessian_method_contradicts_input_deck`.** `hessian_deck` (arc_1_3_thermo's two butanols) ships a real Gaussian freq input in the bundle as an `input` artifact:
  `sBuOH` a bare `freq` paired with ARC's `analytic`, `nBuOH` `freq=numer` paired with `finite_difference_gradient`. Both are accepted, the stored
  `freq.hessian_method` is read back from `GET /calculations/{id}/parameters` (the adapter's row `raw_key` `freq_hessian_method`, once, as sent), and the stored `input`
  artifact's SHA-256 equals the deck's. For the `numer` deck TCKDB's own parameter extraction adds a second `freq.hessian_method` row (`raw_key` `numer`, bool, same
  canonical value), so that calculation has two rows that agree; the bare deck has one. Replay of both is idempotent.

Earlier validated against TCKDB_v2 `511253ad`, with tckdb-client 0.167.0, tckdb-schemas 0.134.0 and tckdb-arc 0.13.0:
64 passed and 1 strict xfail (a TCKDB read-back bug, below), fresh (empty database) and on replay (a second run against the same database). What 0.74-0.134
changed for these corpora, learned by running the unchanged gate first: with only the pin moved, 18 failed and 44 passed. Every
Hessian-bearing corpus was stopped client-side by `hessian.parser_version` (0.130 made it `parser: {name, version}`), and the
artifact tests drew the server's `422 invalid_handle` (0.116: a calculation is addressed by its `calc_` ref, and the gate passed the
integer id). With the Hessian stamp fixed, 6 failed and 56 passed (the artifact refusal, and the Hessian read-back, which is now
`parser_name` / `parser_version`). **No other refusal fired**: none of the 0.121 geometry/conformer codes
(`calculation_geometry_conformer_mismatch`, `transition_state_calculation_conformer_key`,
`transition_state_calculation_species_geometry`, `calculation_input_geometry_mismatch`), no isotope code, none of the 0.65 `*_role_duplicate`
refusals, and none of the atom-map, TS-evidence or energy-ordering rules the earlier runs covered. The only 422 in the run is the
deliberate one (`species_geometry_composition_mismatch` for the two-atom ethanol corpus), fresh and on replay. The one new server
warning was `missing_level_of_theory_provenance` (0.99), on every reaction bundle with kinetics; the adapter now declares the
kinetics `energy_level_of_theory` and the test asserts the stored level is the level of each linked energy. The run also covers
the ARC 1.3 deuterated `iC3H7_d` (`arc_1_3_isotopes`): its Hessian and its rotor-scan points are stored with the isotope on atom 4 (the scan's third frame, in another element order, is stored as a point without geometry). With that label taken off,
the server refuses it as `calculation_geometry_isotope_mismatch` (0.94, `hessian.geometry`), which is how the item was verified; the same refusal (`scan_result.points[1].geometry`) answers the scan with its point labels removed, posted directly to the throwaway server. A
sp/freq `input_geometries` drifted by 2e-3 Angstrom is refused as `calculation_input_geometry_mismatch` (0.121) and is dropped by
the adapter's pre-check, after which it uploads.
TCKDB `511253ad` (still so at `f6cfecbf`): `GET /calculations/{id}/scan-points` (and `scan-result`) fails its own response model, `CalculationScanPointRead.geometry` being handed the ORM `Geometry`, as soon as a point has a geometry (an unfiltered 500-class validation error; the points are stored correctly). `test_substituted_species_scan_points_are_read_back_with_their_isotopes` is a strict xfail on it and will flip when the route is fixed.
The strict xfails below match on server warning codes and read-back shapes, so a TCKDB change to either can flip them without any
adapter change.

Earlier validated at `96b71b09` (tckdb-schemas 0.73, adapter 0.10.0): 62 passed, no xfails. The 0.64 `imaginary_mode` and
`energy_ordering` evidence (`golden_ts_evidence`) exercises the server-only rules (stored-row ownership of each compared energy,
the stored-frequency cross-check, electronic energy from an sp, one level per energy kind), and the ARC 1.3 reaction atom map
(`ts_atom_map_sample` and `ts_atom_map_derived`, both routes) passes the atom-map rules (`atom_map_participant_not_declared`,
`atom_map_indices_not_geometry_relative`, `atom_map_element_not_conserved`, `atom_map_contradicts_irc_mapping`); no case sends a
map they should refuse. The corpus drifted once: `arc_1_2_corrections` still stated CH4's bond corrections as not applied, so the
adapter (correctly, for a pre-1.3 document) deposited no `bac_total`; the corpus now states them applied.

## Server warning codes seen at `c7c8df8b`

Server warnings (`response_body.warnings`) by corpus; identical fresh and on replay (read from the sidecars of both runs). Against the
`f6cfecbf` table, two codes are new: `level_of_theory_disagrees_with_artifact` (0.150) on the artifact tests, whose staged sample logs state another level than the
corpus declares, and `scan_point_coordinate_disagrees_with_geometry` (0.146) on `arc_1_3_isotopes`, whose scan samples are another geometry's frames; every other row is unchanged. These are the codes the gate has observed, not a contract. `ts_atom_map_sample`, `current_arc_h2o`,
`arc_1_3_isotopes` and `synthetic_species_as_shipped` drew none (the latter is the deliberate 422).

| Corpus | Server warning codes |
|---|---|
| `golden` (species, conformer, reaction, TS, artifacts) | `freq_list_incomplete_for_geometry`, `level_of_theory_disagrees_with_artifact` and `multiplicity_mismatch` (sample logs, artifact tests), `missing_software_release_provenance`, `reaction_atom_map_absent`, `transition_state_missing_irc_evidence` |
| `golden_kinetics`, `synthetic_reaction` | `freq_list_incomplete_for_geometry`, `reaction_atom_map_absent`, `transition_state_missing_irc_evidence` (`synthetic_reaction` also `missing_software_release_provenance`) |
| `golden_irc_failed` | `freq_list_incomplete_for_geometry`, `reaction_atom_map_absent`, `transition_state_missing_irc_evidence` |
| `golden_irc_passed`, `golden_ts_evidence` | `freq_list_incomplete_for_geometry`, `reaction_atom_map_absent` |
| `ts_atom_map_derived` | `freq_list_incomplete_for_geometry`, `missing_frequency_scale_factor_provenance` |
| `arc_1_2` | `converged_opt_no_usable_energy`, `missing_software_release_provenance` |
| `arc_1_2_corrections` | `missing_energy_correction_scheme_software`, `missing_frequency_scale_factor_provenance`, `missing_literature_provenance`, `missing_statmech_frequency_source` |
| `arc_1_3_isotopes` | `scan_point_coordinate_disagrees_with_geometry` |
| `stereo_consistent` | `converged_opt_no_usable_energy` (the corpus has no energy); never `species_stereo_contradiction` or `species_entry_stereo_*` |
| `stereo_contradicting` | none: the upload is refused, 422 `species_stereo_contradiction` |
| `hessian_deck` | `freq_list_incomplete_for_geometry`, `missing_energy_correction_scheme_software`, `missing_literature_provenance` |

## Prerequisites

- `tckdb_env` (conda) has the TCKDB backend installed editable
  (`pip install -e "$TCKDB/backend"`), so Alembic, uvicorn and
  `scripts/bootstrap_admin.py` run the checkout's code.
- `arc_env` has `tckdb_arc` installed editable from this repository and
  `tckdb-client` 0.184.x. If `arc_env` has another client, run with the
  checkout's client source first on the path:
  `PYTHONPATH=$TCKDB/clients/python/src conda run -n arc_env python -m pytest ...`. Its `tckdb_schemas` is an editable install of
  `$TCKDB/schemas/python/tckdb-schemas`, so the adapter validates against
  whatever that working tree holds.
- If the checkout is newer than the editable install, shadow it instead of reinstalling:
  at `96b71b09` `tckdb_env` carried tckdb-schemas 0.51, which lacks
  `tckdb_schemas.composite_total` that the backend imports (at `511253ad` the same shadowing is all it needs; no extra
  dependency was missing), so run alembic, uvicorn and
  `bootstrap_admin.py` with
  `PYTHONPATH=$TCKDB/backend:$TCKDB/schemas/python/tckdb-schemas`, and the adapter with
  `PYTHONPATH=$PWD/tckdb_arc` (the editable `tckdb_arc` in `arc_env` points at another
  checkout). Check `app.__file__` and `tckdb_arc.adapter.__file__` before running.
- Docker, with the two images below available locally or pullable.

## Bring up an isolated backend

Use a throwaway database and object store on ports that do not collide with a
development stack (which uses 5432, 9000 and 8010). Keep generated files out
of the TCKDB checkout.

```bash
export TCKDB=/path/to/TCKDB_v2 WORK=$(mktemp -d)

docker run -d --name tckdb-integ-db -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=tckdb -e POSTGRES_PASSWORD=integpw -e POSTGRES_DB=tckdb_integ \
  -e LANG=C.UTF-8 -e POSTGRES_INITDB_ARGS=--encoding=UTF8 \
  informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3
docker run -d --name tckdb-integ-minio -p 127.0.0.1:59000:9000 \
  -e MINIO_ROOT_USER=integ -e MINIO_ROOT_PASSWORD=integsecret123 \
  minio/minio server /data
until docker exec tckdb-integ-db pg_isready -q -U tckdb -d tckdb_integ; do sleep 1; done
until docker exec tckdb-integ-minio mc ready local >/dev/null 2>&1; do sleep 1; done
docker exec tckdb-integ-minio sh -c \
  'mc alias set l http://127.0.0.1:9000 integ integsecret123 && mc mb l/tckdb-integ-artifacts'

cat > "$WORK/integ.env" <<'EOF'
export DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=tckdb_integ DB_USER=tckdb DB_PASSWORD=integpw
export S3_ENDPOINT_URL=http://127.0.0.1:59000 S3_ACCESS_KEY=integ S3_SECRET_KEY=integsecret123
export S3_BUCKET=tckdb-integ-artifacts S3_REGION=us-east-1
export DEPLOYMENT_MODE=local TCKDB_INLINE_WORKER=true
export RATE_LIMIT_ENABLED=false SESSION_COOKIE_SECURE=false
EOF
source "$WORK/integ.env"

cd "$TCKDB/backend"
conda run -n tckdb_env alembic upgrade head
conda run -n tckdb_env --no-capture-output \
  uvicorn main:app --host 127.0.0.1 --port 58010 > "$WORK/api.log" 2>&1 &
until curl -sf http://127.0.0.1:58010/api/v1/readyz >/dev/null; do sleep 1; done
PYTHONPATH=. TCKDB_BOOTSTRAP_PASSWORD=integ-pass \
  conda run -n tckdb_env python scripts/bootstrap_admin.py --username integ --email integ@local

B=http://127.0.0.1:58010/api/v1
curl -s -c "$WORK/cookies" -H 'Content-Type: application/json' \
  -d '{"username":"integ","password":"integ-pass"}' "$B/auth/login"
# Retried: the new session may not be committed yet when login returns (T4).
until curl -sf -b "$WORK/cookies" -H 'Content-Type: application/json' \
  -d '{"label":"integ"}' "$B/auth/api-keys" > "$WORK/key.json"; do sleep 1; done
```

Set every `DB_*` and `S3_*` variable explicitly: Alembic loads
`backend/.env` for any variable left unset, which points at the development
database. The API must see `SESSION_COOKIE_SECURE=false` for the plain-HTTP
login above, and `RATE_LIMIT_ENABLED=false` because the gate makes more than
30 authenticated writes a minute.

## Run the gate

```bash
cd /path/to/tckdb-adapters
export TCKDB_INTEGRATION_URL=http://127.0.0.1:58010/api/v1
export TCKDB_INTEGRATION_API_KEY=$(python -c "import json; print(json.load(open('$WORK/key.json'))['key'])")
conda run -n arc_env python -m pytest tckdb_arc/tests/integration -q -rx
```

Project labels are fixed, so a second run against the same database replays
every upload instead of duplicating it; the checks allow for that.

Every read-back first waits for the upload's rows to become visible, and row
counts are taken only once two consecutive snapshots agree (see T4).

## What it checks

| Corpus | Modes | Read back |
|---|---|---|
| `golden` (Phase 3, `tckdb_evidence.json`) | species, conformer, reaction, TS | calculation owners and types; H2 thermo stored as S and Cp only (no H298, NASA, point H or G, or reference kind), because pre-1.2 output cannot show the enthalpy is a formation enthalpy and H2 is too light for the magnitude guard, with the `enthalpy_formation_unverifiable_light_species` warning; reference pressure not stated (read back as null; the golden output records none, so the adapter omits it with the `thermo_reference_pressure_not_stated` warning) and source calculations; Hessian values; conformer-mode log and input artifacts |
| `golden` + kinetics at T0 = 300 K and Arkane commit | reaction, TS | Arrhenius `a` = ARC's A as sent with `t0_k` = T0 (since 0.63 and adapter 0.8.0; not `A/T0**n`), `n`, `Ea`, kinetics source-calculation roles and owners, the declared `energy_level_of_theory` (0.99: the level of every linked energy calculation, and no `missing_level_of_theory_provenance`), TS composition against both sides, IRC result, the standalone TS's calculations (no GSM path search since 0.6.4: ARC exports no GSM level) |
| `golden` + kinetics + `ts_checks` (IRC, freq, e_elect all true) | reaction, TS | bundle: `irc`, `imaginary_mode` (count, cm^-1, cited freq calculation) and `energy_ordering` (each compared energy, cited to its own participant's sp) read back with the stored values and no `transition_state_energy_ordering_mixed_levels`; standalone TS: `irc` and `imaginary_mode` only |
| `ts_atom_map_sample` (real ARC 1.3 writer output, `nC3H7 <=> iC3H7`) and `ts_atom_map_derived` (`OH + CH4 <=> H2O + CH3` with a TS order unlike the reactants'; `CH3 + CH3 <=> C2H6` with a repeated reactant) | reaction, TS | the one stored atom map (`include=atom_map`): `source` `inferred` with the adapter's note and no `equivalent_map_count`, on the uploaded TS entry; each participant's `atom_to_ts` equals the payload's and ARC's `ts_atom_map` counted from the species labels (elements agree with the TS atom, `ts_atom_order_follows_reactants` agrees with the stored indices); a repeated reactant is two participants of one species entry, each with its own disjoint block; mapped atom counts on both legs; none of `reaction_atom_map_absent`, `reaction_ts_atom_map_not_sent`, `reaction_species_labels_contradicted` (or the server's incompleteness and IRC-contradiction codes) from the adapter or the server. The replay snapshots also count stored map pairs, so a replay cannot add a second map |
| `arc_1_2` (output 1.2 atom-correction flags) | species, conformer | formation enthalpy kept for CH4, stripped to S and Cp (no H298, NASA, point H or G, or reference kind) for the other five, with the producer warning |
| `arc_1_2` CH4 + `energy_corrections` | species | applied AEC and BAC totals, units, components, source `sp` calculation |
| `current_arc` H2O (`parser_evidence.json`) | species | Hessian stored verbatim in its own frame, with its parser stamp as `parser_name` `arc-hessian` and `parser_version` `1` (0.130) |
| `arc_1_3_isotopes` (ARC 1.3 `iC3H7_d` and `iC3H7`, parsed Hessians, a Gaussian rotor scan each whose samples state no isotopes) | species | the Hessian geometry of the deuterated species carries the isotope on atom 4 (0.94) and the ordinary one none; the server refuses the unlabelled one. The deuterated species' scan is accepted with its two frames in the record's element order labelled from the record's `xyz_isotopes` and the third frame's geometry dropped (`geometry_isotopes_not_stated`); the stored point geometries were read from the database (atom 4 mass 2 on the labelled frames only) because `GET /calculations/{id}/scan-points` is broken, see the strict xfail. IRC points of a substituted TS are covered offline only (the corpus has no TS) |
| synthetic CHO + CH4 reaction, real compositions | reaction, TS | kinetics normalisation, source calculations and TS composition |
| `stereo_consistent` (`C/C=C/C`, trans geometry) | species | accepted, no `species_stereo_contradiction` (0.138) |
| `stereo_contradicting` (`C/C=C/C`, cis geometry) | species | 422 `species_stereo_contradiction` recorded in the sidecar |
| `hessian_deck` (arc_1_3_thermo `sBuOH` bare `freq` + `analytic`, `nBuOH` `freq=numer` + `finite_difference_gradient`; real Gaussian decks) | computed_species, with artifacts | accepted (no `hessian_method_contradicts_input_deck`); stored `freq.hessian_method` row with the stated value (a second, deck-extracted row for `numer`); the stored `input` artifact equals the deck by SHA-256 (0.144) |
| synthetic ethanol as shipped (two-atom geometry) | species | 422 refusal recorded in the sidecar |

Every successful mode is also run as a whole sweep twice. The second pass must
replay each upload with the same key and body. The counts of calculations,
thermo, kinetics, conformer observations, transition states, energy
corrections, geometries, species entries, reaction entries, artifacts and
Hessians must stay unchanged. Artifact uploads are replayed as well: the
calculation (named by its `calc_` ref, 0.116) keeps its two artifacts, whose SHA-256 and size match the local
files.

`irc_converged` means only that ARC's IRC jobs finished; ARC's verdict on
whether the IRC connects the declared reactants and products is
`ts_checks.IRC` (ARC `arc/output.py`, `_ts_checks_to_dict`). The golden TS has
`irc_converged: true` and no `ts_checks`, and the gate checks that it is
deposited without validation evidence (read back as `irc: absent`). Since 0.6.4 a
`ts_checks.IRC` verdict of true or false is deposited as passed or failed
evidence (the `golden_irc_passed` and `golden_irc_failed` cases; their xfails were
removed with the fix and both pass against the live backend).

Adapter gaps are pinned as strict xfails with `raises=AssertionError`, so
fixing one makes the gate fail until the marker is removed, and a setup
failure (which raises `RuntimeError`) fails the test instead of satisfying it:

- (none of the artifact-batch gaps remain: adapter 0.6.7 sends each batch through
  `request_json` and records the server's warnings, status code, upload request ID and
  replay flag, so those four xfails were removed; the artifact tests pass live.)


`test_computed_species_thermo_names_arkane` was such an xfail until adapter
0.6.1, which names Arkane on computed-species thermo and statmech; it is now
an ordinary assertion.

## TCKDB findings

**T4 (high): TCKDB answers an upload before committing it.** `get_write_db`
(`backend/app/api/deps.py`, lines 123-150 at `ad3cd706`) commits in the
teardown of its yield dependency. Under FastAPI 0.135 that teardown runs after
the 201 has been sent, so:

- a client that reads immediately after an upload can get 404 for rows the
  response just named (read-your-writes is violated); without the gate's
  waits, fresh-database runs failed with "Calculation not found",
  "TransitionStateEntry not found" and counts that moved between snapshots;
- the same holds outside uploads: minting an API key right after logging in
  intermittently returns 401 "Invalid or expired session", because the
  session row is not yet committed;
- a commit that fails, for example on a deferred constraint, does so after
  the client has received a 201, so the producer records a deposit that does
  not exist.

## Tear down

```bash
fuser -k 58010/tcp        # uvicorn itself; killing the conda-run wrapper orphans it
docker rm -f -v tckdb-integ-db tckdb-integ-minio
rm -rf "$WORK"
```
