"""Backward-compatible re-export: this module moved to :mod:`tckdb_core.constraints`."""

from tckdb_core.constraints import (  # noqa: F401
    _VALID_KINDS,
    _ATOMS_PER_KIND,
    TCKDBCalculationConstraint,
    _validate,
    serialize_constraints,
    _coerce,
    logger,
)
