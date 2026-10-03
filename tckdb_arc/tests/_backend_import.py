"""Import TCKDB's backend identity rules for the replica comparison tests.

Moved to :mod:`tckdb_core.testing.backend_import` (one copy for every adapter's tests);
re-exported here so ``from _backend_import import backend_modules`` keeps working.
"""

from tckdb_core.testing.backend_import import (  # noqa: F401
    _unavailable,
    backend_level_hash,
    backend_modules,
)
