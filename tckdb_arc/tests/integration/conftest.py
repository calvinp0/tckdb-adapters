"""Opt-in live gate against an isolated, locally run TCKDB backend.

Skipped unless ``TCKDB_INTEGRATION_URL`` is set. A target that is not
loopback, uses port 8010, or whose ``/status`` names a non-integration
artifact bucket fails the run instead of skipping it: pointing the gate at a
shared deployment is a mistake to surface, not a configuration to tolerate.
See ``docs/contract/INTEGRATION_GATE.md`` for bringing the backend up.

Reads tolerate TCKDB committing after it has answered (T4 in that document):
id-addressed reads retry a 404 for a bounded time, and row counts are taken
only once two consecutive snapshots agree.
"""

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from tckdb_client import TCKDBClient
from tckdb_client.errors import TCKDBHTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _live import (  # noqa: E402
    API_KEY_ENV,
    commit_probe,
    verify_isolated_backend,
)

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


@pytest.fixture(scope="session")
def live_tckdb():
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
