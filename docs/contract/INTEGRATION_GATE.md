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

Validated against TCKDB_v2 `12c8cc63` (`backend/app` and `backend/alembic`
identical to the pinned `4adf7ff4`), with tckdb-client 0.95.1, tckdb-schemas
0.53.0 and tckdb-arc 0.6.2: 40 passed and 6 xfailed, fresh and on replay. (Adapter
0.6.4 fixes two of those xfails, the IRC evidence pair, and changes the golden TS0
assertions for the omitted GSM path search; that run has not been repeated against a
live backend.) The
benzene computed-species upload drew only `missing_literature_provenance` (×2).
The strict xfails
below match on server warning codes and read-back shapes, so a TCKDB change to
either can flip them without any adapter change.

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
| `golden` + kinetics at T0 = 300 K and Arkane commit | reaction, TS | Arrhenius `a = A/T0**n`, `n`, `Ea`, kinetics source-calculation roles and owners, TS composition against both sides, IRC result, the standalone TS's calculations (no GSM path search since 0.6.4: ARC exports no GSM level) |
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
removed with the fix, not yet run against a live backend).

Adapter gaps are pinned as strict xfails with `raises=AssertionError`, so
fixing one makes the gate fail until the marker is removed, and a setup
failure (which raises `RuntimeError`) fails the test instead of satisfying it:

- artifact-batch sidecars drop the server's warnings;
- artifact-batch sidecars lose the status code, the upload request ID and the
  replay flag, because tckdb-client (0.93–0.95) keeps only the response body (one
  xfail per field);


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
