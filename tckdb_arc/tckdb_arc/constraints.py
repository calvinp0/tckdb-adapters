"""Compatibility module: constraints moved to :mod:`tckdb_core.constraints`.

``TCKDBCalculationConstraint`` and ``serialize_constraints`` (the module's whole public API)
keep resolving here without a warning; ``serialize_constraints`` logs to the ``tckdb_arc``
logger, as before the move. The private helpers (``_validate``, ``_coerce``, ...) are in
:mod:`tckdb_core.constraints`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tckdb_arc._logging import get_logger
from tckdb_core.constraints import TCKDBCalculationConstraint  # noqa: F401
from tckdb_core.constraints import serialize_constraints as _core_serialize_constraints


def serialize_constraints(
    constraints: Iterable[TCKDBCalculationConstraint | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Serialize constraints into TCKDB payload shape (see :func:`tckdb_core.constraints.serialize_constraints`).

    Dropped entries are logged to the ``tckdb_arc`` logger.
    """
    return _core_serialize_constraints(constraints, log=get_logger())
