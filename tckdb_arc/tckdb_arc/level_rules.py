"""Backward-compatible re-export: this module moved to :mod:`tckdb_core.level_rules`."""

from tckdb_core.level_rules import (  # noqa: F401
    _HYPHEN_RULES,
    basis_identity_key,
    component_identity_key,
    NAME_ALIASES,
    SUFFIX_ALIASES,
    _SUFFIX_COMPILED,
    method_identity_key,
    DISPERSION_ALIASES,
    _DISPERSION_COMPILED,
    FOLDED_SUFFIXES,
    FOLDED_STEMS,
    _FOLDED_COMPILED,
    dispersion_identity_key,
    level_identity_keys,
    _enum_value,
    level_payload,
    level_hash,
    same_level,
)
