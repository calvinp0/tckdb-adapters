"""Scaffolding for an adapter's opt-in live gate against an isolated local TCKDB backend.

The gate is skipped unless ``TCKDB_INTEGRATION_URL`` is set. A target that is not
loopback, uses port 8010, or whose ``/status`` names a non-integration artifact bucket
fails the run instead of skipping it: pointing the gate at a shared deployment is a
mistake to surface, not a configuration to tolerate.

Reads tolerate TCKDB committing after it has answered (T4 in the integration-gate
document): id-addressed reads retry a 404 for a bounded time, and row counts are taken
only once two consecutive snapshots agree.

An adapter's ``tests/integration/conftest.py`` registers the session fixture::

    from tckdb_core.testing.live import live_tckdb_fixture
    live_tckdb = live_tckdb_fixture()

and its tests take ``live_tckdb`` (a :class:`LiveTCKDB`). Everything but the fixture's
network calls is offline (the guards, the commit probe), so the guards run in the default
suite too.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from tckdb_client import TCKDBClient
from tckdb_client.errors import TCKDBHTTPError

API_KEY_ENV = "TCKDB_INTEGRATION_API_KEY"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
REFUSED_PORTS = frozenset({8010})
INTEGRATION_BUCKET_PREFIX = "tckdb-integ"


def assert_loopback_url(url):
    """Refuse any TCKDB base URL that is not loopback, or that uses port 8010.

    Loopback alone does not mean isolated: TCKDB's API listens on
    127.0.0.1:8010 on the production host and on a development machine, and
    an ``ssh -L 8010:...`` tunnel makes a remote deployment loopback too. The
    port is refused outright; :func:`assert_integration_backend` then checks
    what actually answered.
    """
    parts = urlsplit(url or "")
    host = parts.hostname
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing TCKDB integration target {url!r}: host {host!r} is not "
            f"loopback ({', '.join(sorted(LOOPBACK_HOSTS))}). The gate only "
            "runs against an isolated local backend."
        )
    if parts.port in REFUSED_PORTS:
        raise ValueError(
            f"refusing TCKDB integration target {url!r}: port {parts.port} is "
            "where TCKDB's production and development APIs listen (directly or "
            "through an SSH tunnel). Run the isolated backend on another port."
        )
    return url


def assert_integration_backend(status):
    """Refuse a backend whose artifact bucket is not an integration sentinel.

    ``status`` is the body of ``GET /status``. The isolated stack in
    ``docs/contract/INTEGRATION_GATE.md`` uses bucket ``tckdb-integ-artifacts``;
    deployments use ``tckdb-artifacts``.
    """
    storage = ((status or {}).get("components") or {}).get("artifact_storage") or {}
    bucket = storage.get("bucket")
    if not (isinstance(bucket, str) and bucket.startswith(INTEGRATION_BUCKET_PREFIX)):
        raise ValueError(
            f"refusing TCKDB integration target: its artifact bucket is {bucket!r}, "
            f"not an isolated integration bucket ({INTEGRATION_BUCKET_PREFIX}*)."
        )
    return bucket


def verify_isolated_backend(url, timeout=30.0):
    """Check that ``url`` is an isolated integration backend, without credentials.

    The loopback/port check runs first, then ``/status`` (bucket sentinel) and
    ``/readyz`` are fetched by a client that holds no API key: tckdb-client
    attaches ``X-API-Key`` to every request whenever it has one, even
    unauthenticated ones, so a keyed probe would hand the key to whatever
    answered before it was verified.
    """
    from tckdb_client import TCKDBClient
    from tckdb_client.errors import TCKDBHTTPError

    assert_loopback_url(url)
    with TCKDBClient(url, api_key=None, timeout=timeout) as probe:
        try:
            status = probe.request_json("GET", "/status", authenticated=False).data
        except TCKDBHTTPError as exc:  # a degraded backend answers 503 with the same body
            status = exc.response_json
        assert_integration_backend(status)
        ready = probe.request_json("GET", "/readyz", authenticated=False).data
    if not (isinstance(ready, dict) and ready.get("status") == "ready"):
        raise RuntimeError(f"TCKDB at {url} is not ready: {ready!r}")
    return url


def commit_probe(response):
    """An id-addressed path that exists once ``response``'s upload has committed.

    TCKDB commits in the teardown of its write-session dependency, after the
    201 has been sent (see T4 in INTEGRATION_GATE.md), so a read issued the
    moment an upload returns can miss the rows. The transaction is atomic:
    once one of its rows is visible, all are.
    """
    if not isinstance(response, dict):
        return None
    if response.get("conformers"):
        return f"/calculations/{response['conformers'][0]['primary_calculation']['calculation_id']}"
    if response.get("primary_calculation"):
        return f"/calculations/{response['primary_calculation']['calculation_id']}"
    if response.get("calculation_keys"):
        return f"/calculations/{min(response['calculation_keys'].values())}"
    if response.get("type") == "transition_state_entry":
        return f"/transition-states/entries/{response['id']}"
    return None


URL_ENV = "TCKDB_INTEGRATION_URL"
READ_TIMEOUT_S = 10.0
LIST_LIMIT = 200  # the API caps list pages at 200

# Collection endpoints whose ``total`` must not move when an upload replays.
COUNTED = ("/calculations", "/thermo", "/kinetics", "/conformer-observations",
           "/transition-states", "/applied-energy-corrections", "/geometries")


@dataclass
class LiveTCKDB:
    url: str
    client: TCKDBClient

    def _get(self, path, params=None):
        return self.client.request_json("GET", path, params=params or None).data

    def get(self, path, **params):
        """GET, retrying a 404 with backoff for up to ``READ_TIMEOUT_S``."""
        deadline = time.monotonic() + READ_TIMEOUT_S
        delay = 0.05
        while True:
            try:
                return self._get(path, params)
            except TCKDBHTTPError as exc:
                if exc.status_code != 404 or time.monotonic() > deadline:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)

    def get_until(self, path, ready, **params):
        """Poll until ``ready(body)``; return the last body either way."""
        deadline = time.monotonic() + READ_TIMEOUT_S
        body = self.get(path, **params)
        while not ready(body) and time.monotonic() < deadline:
            time.sleep(0.2)
            body = self.get(path, **params)
        return body

    def await_commit(self, response):
        """Block until the rows behind an upload response are readable."""
        probe = commit_probe(response)
        if probe is None:
            raise RuntimeError(f"no commit probe for upload response {response!r}")
        self.get(probe)

    def total(self, path, **params):
        return self._get(path, {"limit": 1, **params})["total"]

    def _hessian_count(self):
        freq = self._get("/calculations", {"type": "freq", "limit": LIST_LIMIT})
        if freq["total"] > LIST_LIMIT:
            raise RuntimeError("too many freq calculations to count Hessians")
        count = 0
        for calc in freq["items"]:
            try:
                self._get(f"/calculations/{calc['id']}/hessian")
            except TCKDBHTTPError as exc:
                if exc.status_code != 404:
                    raise
            else:
                count += 1
        return count

    def counts(self):
        snapshot = {path: self.total(path) for path in COUNTED}
        species = self._get("/scientific/species/browse", {"limit": 200})
        reactions = self._get("/scientific/reactions/browse", {"limit": 200})
        if len(species["records"]) >= 200 or len(reactions["records"]) >= 200:
            raise RuntimeError("too many species or reactions to count on one page")
        snapshot["species_entries"] = sum(len(r["entries"]) for r in species["records"])
        entry_refs = sorted({r["reaction_entry_ref"] for r in reactions["records"]})
        snapshot["reaction_entries"] = len(entry_refs)
        # An atom map is written once per reaction entry's transition state: a replay must not add pairs.
        snapshot["atom_map_pairs"] = sum(
            len(atom_map["pairs"])
            for ref in entry_refs
            for atom_map in self._get(f"/scientific/reaction-entries/{ref}/full",
                                      {"include": "atom_map"})["atom_map"]
        )
        snapshot["artifacts"] = self._get(
            "/scientific/artifacts/search", {"has_sha256": "true", "limit": 1},
        )["pagination"]["total"]
        snapshot["hessians"] = self._hessian_count()
        return snapshot

    @staticmethod
    def _settle(snapshot, interval=0.3, timeout=15.0):
        """``snapshot()`` once two consecutive calls agree."""
        deadline = time.monotonic() + timeout
        previous = snapshot()
        while True:
            time.sleep(interval)
            current = snapshot()
            if current == previous:
                return current
            if time.monotonic() > deadline:
                raise RuntimeError(f"snapshot did not settle: {previous} -> {current}")
            previous = current

    def settled_counts(self):
        """Row counts once two consecutive snapshots agree."""
        return self._settle(self.counts)

    def settled(self, path, **params):
        """A GET body once two consecutive reads agree."""
        return self._settle(lambda: self.get(path, **params))


def _live_tckdb():
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(f"{URL_ENV} not set; the live TCKDB integration gate is opt-in")
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        pytest.fail(f"{URL_ENV} is set but {API_KEY_ENV} is not")
    try:
        verify_isolated_backend(url)
    except (ValueError, RuntimeError) as exc:
        pytest.fail(str(exc))
    # Only now does the key leave this process.
    print(f"\nTCKDB integration target: {url}")
    client = TCKDBClient(url, api_key=api_key, timeout=120)
    yield LiveTCKDB(url=url, client=client)
    client.close()


def live_tckdb_fixture():
    """The session-scoped ``live_tckdb`` fixture, for an adapter's integration conftest to register."""
    return pytest.fixture(scope="session", name="live_tckdb")(_live_tckdb)
