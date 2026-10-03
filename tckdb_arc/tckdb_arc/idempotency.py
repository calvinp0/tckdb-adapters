"""Idempotency-key generation for ARC -> TCKDB uploads.

The composition lives in :mod:`tckdb_core.idempotency`, which takes the key's
producer namespace as a required argument. This module binds ARC's namespace
(``"arc"``) so every key ARC has ever produced stays byte-identical, and
re-exports the input dataclasses.
"""

from tckdb_core.idempotency import (
    ArtifactIdempotencyInputs,
    IdempotencyInputs,
)
from tckdb_core.idempotency import build_artifact_idempotency_key as _core_build_artifact_key
from tckdb_core.idempotency import build_idempotency_key as _core_build_key

#: The key namespace that separates ARC's keys from other producers'.
IDEMPOTENCY_NAMESPACE = "arc"


def build_idempotency_key(inputs: IdempotencyInputs) -> str:
    """ARC's conformer/calculation key: ``arc:<project>:<species>:<conformer>:<kind>:<payload-hash>``."""
    return _core_build_key(inputs, namespace=IDEMPOTENCY_NAMESPACE)


def build_artifact_idempotency_key(inputs: ArtifactIdempotencyInputs) -> str:
    """ARC's artifact key: ``arc:<project>:<species>:artifact:<calc_ref>:<kind>:<sha-prefix>``."""
    return _core_build_artifact_key(inputs, namespace=IDEMPOTENCY_NAMESPACE)


__all__ = [
    "ArtifactIdempotencyInputs",
    "IDEMPOTENCY_NAMESPACE",
    "IdempotencyInputs",
    "build_artifact_idempotency_key",
    "build_idempotency_key",
]
