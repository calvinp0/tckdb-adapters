"""Vendored logging shim.

Replaces ``arc.common.get_logger`` for the standalone ``tckdb_arc`` package.
ARC returns a single module-level ``logging.getLogger('arc')`` from
``get_logger`` (see ``arc/common.py``); here we return a package-local logger
so ``tckdb_arc`` never has to import ARC just to log.

The shared upload code in ``tckdb_core`` logs through a name-configurable logger;
pointing it at ``tckdb_arc`` here keeps every record it emits for an ARC run
under the ``tckdb_arc`` logger, as it was before the code moved.
"""

import logging

from tckdb_core._logging import set_logger_name

logger = logging.getLogger("tckdb_arc")
set_logger_name("tckdb_arc")


def get_logger() -> logging.Logger:
    """Return the package logger (avoids multiple logger entries)."""
    return logger
