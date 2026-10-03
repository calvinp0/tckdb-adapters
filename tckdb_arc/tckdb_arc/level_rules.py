"""Compatibility module: level-of-theory identity moved to :mod:`tckdb_core.level_rules`.

The old public names are forwarded to :mod:`tckdb_core.level_rules` with a ``DeprecationWarning``;
import ``method_identity_key``, ``level_hash`` and the rest from there.
"""

from tckdb_arc._compat import forward_to_core

__getattr__ = forward_to_core(__name__, "tckdb_core.level_rules", (
    "DISPERSION_ALIASES",
    "FOLDED_STEMS",
    "FOLDED_SUFFIXES",
    "NAME_ALIASES",
    "SUFFIX_ALIASES",
    "basis_identity_key",
    "component_identity_key",
    "dispersion_identity_key",
    "level_hash",
    "level_identity_keys",
    "level_payload",
    "method_identity_key",
    "same_level",
))
