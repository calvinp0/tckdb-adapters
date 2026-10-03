"""Package logger shared by every producer adapter built on ``tckdb_core``.

Log records are emitted under a configurable logger name so a producer keeps its
own namespace (its operators filter on it, and its tests ``caplog`` on it). The
default is ``tckdb_core``; a producer package calls :func:`set_logger_name` once
at import (before any record is logged) to route the shared code's records to
its own logger. The module-level :data:`logger` is a thin proxy that resolves
the name at call time, so modules that did ``logger = get_logger()`` at import
follow a later :func:`set_logger_name`.
"""

import logging

_logger_name = "tckdb_core"


class _LoggerProxy:
    """Delegate every attribute to ``logging.getLogger(<current name>)``."""

    def __getattr__(self, attr: str):
        return getattr(logging.getLogger(_logger_name), attr)


logger = _LoggerProxy()


def set_logger_name(name: str) -> None:
    """Route this package's log records to the logger called ``name``."""
    global _logger_name
    _logger_name = name


def get_logger():
    """Return the shared package logger (a proxy; avoids multiple logger entries)."""
    return logger
