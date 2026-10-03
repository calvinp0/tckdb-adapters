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

Validated against TCKDB_v2 `96b71b09` (the pinned sha), with tckdb-client 0.111.0,
tckdb-schemas 0.73.0 and tckdb-arc 0.10.0: 62 passed and no xfails, fresh (empty database)
and on replay (a second run against the same database). The 0.65-0.73 server changes are
additive for these corpora: none of the new warning codes (`named_composite_deposited_as_opt/sp`,
`level_of_theory_method_names_correction_table`, `composite_delta_prefer_scheme_terms`) and none
of the 0.65 `*_role_duplicate` refusals fired. The run covers the 0.64 `imaginary_mode` and
`energy_ordering` evidence (`golden_ts_evidence`: bundle with both kinds read back with
their stored values and cited calculations, standalone TS with `imaginary_mode`), which
exercises the server-only rules (stored-row ownership of each compared energy, the
stored-frequency cross-check, electronic energy from an sp, one level per energy kind), and
the ARC 1.3 reaction atom map built from `ts_atom_map` (`ts_atom_map_sample` and
`ts_atom_map_derived`, both routes), which pass the server's atom-map rules
(`atom_map_participant_not_declared`, `atom_map_indices_not_geometry_relative`,
`atom_map_element_not_conserved`, `atom_map_contradicts_irc_mapping`); no case sends a map they should refuse.
The corpus drifted once before this run: `arc_1_2_corrections` still stated CH4's bond
corrections as not applied, so the adapter (correctly, for a pre-1.3 document) deposited no
`bac_total`; the corpus now states them applied. The strict xfails
below match on server warning codes and read-back shapes, so a TCKDB change to
either can flip them without any adapter change.

## Server warning codes seen at `96b71b09`

Server warnings (`response_body.warnings`) by corpus; identical fresh and on replay. These
are the codes the gate has observed, not a contract. `ts_atom_map_sample`,
`current_arc_h2o` and `synthetic_species_as_shipped` drew none.

| Corpus | Server warning codes |
|---|---|
| `golden` (species, conformer, reaction, TS, artifacts) | `freq_list_incomplete_for_geometry`, `missing_software_release_provenance`, `multiplicity_mismatch` (sample log, artifact test), `reaction_atom_map_absent`, `transition_state_missing_irc_evidence` |
| `golden_kinetics`, `synthetic_reaction` | `freq_list_incomplete_for_geometry`, `reaction_atom_map_absent`, `transition_state_missing_irc_evidence` (`synthetic_reaction` also `missing_software_release_provenance`) |
| `golden_irc_failed` | `freq_list_incomplete_for_geometry`, `reaction_atom_map_absent`, `transition_state_missing_irc_evidence` |
| `golden_irc_passed`, `golden_ts_evidence` | `freq_list_incomplete_for_geometry`, `reaction_atom_map_absent` |
| `ts_atom_map_derived` | `freq_list_incomplete_for_geometry`, `missing_frequency_scale_factor_provenance` |
| `arc_1_2` | `converged_opt_no_usable_energy`, `missing_software_release_provenance` |
| `arc_1_2_corrections` | `missing_energy_correction_scheme_software`, `missing_frequency_scale_factor_provenance`, `missing_literature_provenance`, `missing_statmech_frequency_source` |

## Prerequisites

- `tckdb_env` (conda) has the TCKDB backend installed editable
  (`pip install -e "$TCKDB/backend"`), so Alembic, uvicorn and
  `scripts/bootstrap_admin.py` run the checkout's code.
- `arc_env` has `tckdb_arc` installed editable from this repository and
  `tckdb-client` 0.95.x. If `arc_env` still has an older client, run with the
  checkout's client source first on the path:
  `PYTHONPATH=$TCKDB/clients/python/src conda run -n arc_env python -m pytest ...`. Its `tckdb_schemas` is an editable install of
  `$TCKDB/schemas/python/tckdb-schemas`, so the adapter validates against
  whatever that working tree holds.
- If the checkout is newer than the editable install, shadow it instead of reinstalling:
  at `96b71b09` `tckdb_env` carried tckdb-schemas 0.51, which lacks
  `tckdb_schemas.composite_total` that the backend imports, so run alembic, uvicorn and
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
| `golden` + kinetics at T0 = 300 K and Arkane commit | reaction, TS | Arrhenius `a` = ARC's A as sent with `t0_k` = T0 (since 0.63 and adapter 0.8.0; not `A/T0**n`), `n`, `Ea`, kinetics source-calculation roles and owners, TS composition against both sides, IRC result, the standalone TS's calculations (no GSM path search since 0.6.4: ARC exports no GSM level) |
| `golden` + kinetics + `ts_checks` (IRC, freq, e_elect all true) | reaction, TS | bundle: `irc`, `imaginary_mode` (count, cm^-1, cited freq calculation) and `energy_ordering` (each compared energy, cited to its own participant's sp) read back with the stored values and no `transition_state_energy_ordering_mixed_levels`; standalone TS: `irc` and `imaginary_mode` only |
| `ts_atom_map_sample` (real ARC 1.3 writer output, `nC3H7 <=> iC3H7`) and `ts_atom_map_derived` (`OH + CH4 <=> H2O + CH3` with a TS order unlike the reactants'; `CH3 + CH3 <=> C2H6` with a repeated reactant) | reaction, TS | the one stored atom map (`include=atom_map`): `source` `inferred` with the adapter's note and no `equivalent_map_count`, on the uploaded TS entry; each participant's `atom_to_ts` equals the payload's and ARC's `ts_atom_map` counted from the species labels (elements agree with the TS atom, `ts_atom_order_follows_reactants` agrees with the stored indices); a repeated reactant is two participants of one species entry, each with its own disjoint block; mapped atom counts on both legs; none of `reaction_atom_map_absent`, `reaction_ts_atom_map_not_sent`, `reaction_species_labels_contradicted` (or the server's incompleteness and IRC-contradiction codes) from the adapter or the server. The replay snapshots also count stored map pairs, so a replay cannot add a second map |
| `arc_1_2` (output 1.2 atom-correction flags) | species, conformer | formation enthalpy kept for CH4, stripped to S and Cp (no H298, NASA, point H or G, or reference kind) for the other five, with the producer warning |
| `arc_1_2` CH4 + `energy_corrections` | species | applied AEC and BAC totals, units, components, source `sp` calculation |
| `current_arc` H2O (`parser_evidence.json`) | species | Hessian stored verbatim in its own frame |
| synthetic CHO + CH4 reaction, real compositions | reaction, TS | kinetics normalisation, source calculations and TS composition |
| synthetic ethanol as shipped (two-atom geometry) | species | 422 refusal recorded in the sidecar |

Every successful mode is also run as a whole sweep twice. The second pass must
replay each upload with the same key and body. The counts of calculations,
thermo, kinetics, conformer observations, transition states, energy
corrections, geometries, species entries, reaction entries, artifacts and
Hessians must stay unchanged. Artifact uploads are replayed as well: the
calculation keeps its two artifacts, whose SHA-256 and size match the local
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
