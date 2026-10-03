"""Deprecation shims for names that moved to ``tckdb_core``.

``tckdb_arc.constraints``, ``tckdb_arc.level_rules`` and ``tckdb_arc.payload_writer``
began as ARC modules and moved to :mod:`tckdb_core` in batch L1. They stay importable:
the names this package itself and ARC's repository use are re-exported without a
warning, and the rest of each module's old public API is forwarded by a module
``__getattr__`` that emits a ``DeprecationWarning`` naming the new home. Private
(underscore) names are not forwarded: import them from ``tckdb_core`` (the tests do).
"""

from __future__ import annotations

import importlib
import warnings
from collections.abc import Iterable
from typing import Any, Callable


def forward_to_core(
    module_name: str, core_module_name: str, names: Iterable[str],
) -> Callable[[str], Any]:
    """A module ``__getattr__`` forwarding exactly ``names`` to ``core_module_name`` with a warning.

    ``names`` is the module's old public API, spelled out: a stdlib module the core module
    happens to import, or a private helper, is not part of it.
    """
    forwarded = frozenset(names)

    def __getattr__(name: str) -> Any:
        if name not in forwarded:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        core_module = importlib.import_module(core_module_name)
        value = getattr(core_module, name)
        warnings.warn(
            f"{module_name}.{name} moved to {core_module_name}.{name}; import it from "
            f"{core_module_name} (this alias will be removed in a later release)",
            DeprecationWarning,
            stacklevel=2,
        )
        return value

    return __getattr__
