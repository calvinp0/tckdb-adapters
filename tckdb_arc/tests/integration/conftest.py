"""Opt-in live gate against an isolated, locally run TCKDB backend.

The scaffolding (loopback guard, isolated-backend check, retrying reads, commit probe,
settled row counts) lives in :mod:`tckdb_core.testing.live`; this conftest registers its
session fixture for the ARC adapter's tests. Skipped unless ``TCKDB_INTEGRATION_URL`` is
set. See ``docs/contract/INTEGRATION_GATE.md`` for bringing the backend up.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tckdb_core.testing.live import (  # noqa: F401  (re-exported for the tests' ``from conftest import``)
    API_KEY_ENV,
    COUNTED,
    LIST_LIMIT,
    READ_TIMEOUT_S,
    URL_ENV,
    LiveTCKDB,
    live_tckdb_fixture,
)

live_tckdb = live_tckdb_fixture()
