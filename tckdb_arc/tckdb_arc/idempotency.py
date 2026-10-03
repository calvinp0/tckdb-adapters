"""Idempotency-key generation for ARC -> TCKDB uploads.

Wraps :func:`tckdb_client.make_idempotency_key`. The composition rules
(*which* ARC-level facts go into the key) are intentionally kept here in
ARC, because they decide what "the same logical upload" means. Stable
across retries, distinct across logically-different inputs.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tckdb_client import make_idempotency_key


@dataclass(frozen=True)
class IdempotencyInputs:
    """Stable inputs that identify one logical conformer/calculation upload.

    Every field here MUST be deterministic for the same ARC output —
    no timestamps, no PIDs, no random suffixes. The combination is what
    the TCKDB server uses to deduplicate replays.
    """

    project_label: str | None
    species_label: str
    conformer_label: str
    payload_kind: str
    payload_hash: str

    @classmethod
    def from_payload(
        cls,
        *,
        project_label: str | None,
        species_label: str,
        conformer_label: str,
        payload_kind: str,
        payload: Any,
    ) -> "IdempotencyInputs":
        """Hash the canonical-JSON payload to produce a stable identity tail.

        Sorted-key JSON is used so dict ordering does not change the
        hash; this lets a re-built payload with the same content produce
        the same key on retry.
        """
        canonical = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()[:16]
        return cls(
            project_label=project_label,
            species_label=species_label,
            conformer_label=conformer_label,
            payload_kind=payload_kind,
            payload_hash=digest,
        )


def build_idempotency_key(inputs: IdempotencyInputs) -> str:
    """Compose a stable idempotency key from :class:`IdempotencyInputs`.

    Shape: ``arc:<project>:<species>:<conformer>:<kind>:<payload-hash>``.

    - ``"arc"`` namespaces the key against other producers (e.g. RMG).
    - ``project_label`` keeps two runs of the same species under
      different projects from replaying each other.
    - The trailing ``payload_hash`` makes content-changes produce a
      *different* key, so edited geometries do not silently replay the
      old upload.

    ``make_idempotency_key`` sanitizes illegal characters and validates
    the result against the server constraint
    ``^[A-Za-z0-9._:-]{16,200}$``; callers don't need to pre-clean parts.
    """
    parts: list[str] = ["arc"]
    if inputs.project_label:
        parts.append(inputs.project_label)
    parts.extend(
        [
            inputs.species_label,
            inputs.conformer_label,
            inputs.payload_kind,
            inputs.payload_hash,
        ]
    )
    return make_idempotency_key(*parts)


@dataclass(frozen=True)
class ArtifactIdempotencyInputs:
    """Stable inputs that identify one logical artifact upload event.

    Distinct from :class:`IdempotencyInputs`: the chemistry-upload key
    is scoped to a (species, conformer) — but artifact uploads target a
    concrete TCKDB calculation row, and the same calculation can carry
    multiple artifacts of different kinds. So the artifact key tail is
    ``(calculation_ref, artifact_kind, artifact_sha256)``: the calculation's
    ``calc_`` ref (TCKDB public refs, #599), with the integer id only as a
    fallback for a response that carried no ref. A key built from a pre-0.10
    sidecar's integer id differs and would not replay (the server keeps one
    artifact row per upload), so the adapter checks for a pre-0.10 sidecar of
    the same artifact first and skips an artifact it records as uploaded
    (``TCKDBAdapter._legacy_uploaded_artifact``).

    The artifact's bytes-hash is part of the key so that re-uploading
    different content for the same kind under the same calculation
    produces a different key (i.e. a new upload event), while a literal
    retry of the same bytes replays.
    """

    project_label: str | None
    species_label: str
    calculation_id: int | None
    artifact_kind: str
    artifact_sha256: str
    calculation_ref: str | None = None

    @property
    def calculation_handle(self) -> int | str:
        """The ``calc_`` ref when the response gave one, else the integer id (fallback)."""
        handle = self.calculation_ref or self.calculation_id
        if handle is None:
            raise ValueError("an artifact idempotency key needs a calculation_ref or calculation_id")
        return handle


def build_artifact_idempotency_key(inputs: ArtifactIdempotencyInputs) -> str:
    """Compose a stable per-artifact idempotency key.

    Shape:
        ``arc:<project>:<species>:artifact:<calc_ref>:<kind>:<sha-prefix>``

    The artifact sha256 is truncated to 16 hex chars to keep the key
    well under the 200-char server cap while preserving collision
    resistance for any plausible run.
    """
    parts: list[str] = ["arc"]
    if inputs.project_label:
        parts.append(inputs.project_label)
    parts.extend(
        [
            inputs.species_label,
            "artifact",
            str(inputs.calculation_handle),
            inputs.artifact_kind,
            inputs.artifact_sha256[:16],
        ]
    )
    return make_idempotency_key(*parts)


__all__ = [
    "ArtifactIdempotencyInputs",
    "IdempotencyInputs",
    "build_artifact_idempotency_key",
    "build_idempotency_key",
]
