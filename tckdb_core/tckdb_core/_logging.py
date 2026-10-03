"""Loggers for the shared upload code.

A producer owns its logger name (its operators filter on it, its tests ``caplog``
on it), and two producers can live in one process, so the name is **not** process
state. The pipeline logs through the instance: :class:`~tckdb_core.uploader.TCKDBUploaderBase`
writes to ``self._log``, which is ``logging.getLogger(self.LOGGER_NAME)`` unless a
producer overrides the property (the ARC adapter returns its module's ``logger`` so
a test that patches the producer module's ``logger`` still intercepts every record).

Free functions in this package that log take a keyword ``log=``; when it is
omitted they use the package's own logger, ``tckdb_core``. A producer's wrapper
passes its logger so the records land where its operators expect them.
"""

import logging

#: Name of the logger the shared code uses when no producer passes one.
CORE_LOGGER_NAME = "tckdb_core"


def get_logger(name: str | None = None) -> logging.Logger:
    """The logger called ``name``, or the package's own (``tckdb_core``)."""
    return logging.getLogger(name or CORE_LOGGER_NAME)


def resolve_log(log: "logging.Logger | None") -> "logging.Logger":
    """``log`` when a producer passed one, else the package logger."""
    return log if log is not None else get_logger()
