"""Vendored logging shim.

Replaces ``arc.common.get_logger`` for the standalone ``tckdb_arc`` package.
ARC returns a single module-level ``logging.getLogger('arc')`` from
``get_logger`` (see ``arc/common.py``); here we return a package-local logger
so ``tckdb_arc`` never has to import ARC just to log.
"""

import logging

logger = logging.getLogger("tckdb_arc")


def get_logger() -> logging.Logger:
    """Return the package logger (avoids multiple logger entries)."""
    return logger
