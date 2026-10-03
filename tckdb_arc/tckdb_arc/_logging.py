"""Package logger for ``tckdb_arc``.

Replaces ``arc.common.get_logger`` for the standalone ``tckdb_arc`` package: ARC
returns a single module-level ``logging.getLogger('arc')`` from ``get_logger``;
here the logger is package-local, so ``tckdb_arc`` never has to import ARC just
to log.

The shared upload pipeline in ``tckdb_core`` logs through the instance
(``TCKDBAdapter._log``, bound to ``tckdb_arc.adapter.logger``) and through the
``log=`` argument of its free functions, so every record it emits for an ARC run
lands under the ``tckdb_arc`` logger with no process-global state.
"""

import logging

#: Name of the logger every ``tckdb_arc`` record goes to.
LOGGER_NAME = "tckdb_arc"

logger = logging.getLogger(LOGGER_NAME)


def get_logger() -> logging.Logger:
    """Return the package logger (avoids multiple logger entries)."""
    return logger
