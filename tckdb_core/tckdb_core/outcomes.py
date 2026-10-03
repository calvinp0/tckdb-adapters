"""Result types returned by an upload attempt (mirroring what the sidecar records)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tckdb_core.payload_writer import WrittenArtifact


@dataclass(frozen=True)
class UploadOutcome:
    """Result of one adapter invocation; mirrors the sidecar status.

    ``primary_calculation`` and ``additional_calculations`` are populated
    on successful conformer uploads (status == ``uploaded``) and carry
    the server's :class:`CalculationUploadRef` shape — i.e. dicts with
    ``calculation_id``, ``type``, and (for additional calcs) ``request_index``.
    They are ``None`` / empty otherwise.
    """

    status: str  # pending | uploaded | failed | skipped
    # ``None`` for a species skipped before any payload was built
    # (``irc_endpoint_species_skipped``).
    payload_path: Path | None
    sidecar_path: Path | None
    idempotency_key: str
    error: str | None = None
    response: Any = None
    primary_calculation: dict[str, Any] | None = None
    additional_calculations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # TCKDB 0.57+/#599 public refs: the submission's ``sub_`` ref (what a later
    # request such as a rights attestation names) and, on computed-reaction, the
    # bundle-local calculation key -> ``calc_`` ref map.
    submission_ref: str | None = None
    calculation_key_refs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactUploadOutcome:
    """Result of one artifact upload attempt."""

    status: str  # uploaded | failed | skipped
    sidecar_path: Path | None
    idempotency_key: str | None
    # ``calculation_id`` is ``None`` when the target was named by its ``calc_`` ref only.
    calculation_id: int | None
    kind: str
    error: str | None = None
    response: Any = None
    skip_reason: str | None = None
    calculation_ref: str | None = None
    # The server's per-item findings from the artifact response body
    # (e.g. ``software_release_version_filled_from_artifact``,
    # ``multiplicity_mismatch``), the same list the sidecar records.
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _ArtifactBatchResult:
    """One server response to one calculation's artifact batch POST.

    ``response`` is the client's full ``TCKDBResponse`` (status, headers, body),
    which ``TCKDBClient.upload_artifacts`` discards in favour of the body.
    """

    handle: int | str
    calculation_keys: tuple[str, ...]
    artifact_count: int
    response: Any


@dataclass(frozen=True)
class _PreparedArtifactUpload:
    """Artifact upload plan item plus its sidecar handle."""

    written: WrittenArtifact
    calculation_key: str
    calculation_id: int | None
    path: Path
    kind: str
    label: str | None
    sha256: str
    bytes: int
    filename: str
    calculation_ref: str | None = None

    @property
    def handle(self) -> int | str:
        """What names the calculation in the URL: its ``calc_`` ref, else the integer id."""
        return self.calculation_ref if self.calculation_ref else self.calculation_id  # type: ignore[return-value]


class TCKDBReadinessError(RuntimeError):
    """Raised when the TCKDB readyz preflight fails before upload."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_json: Any = None,
        response_text: str | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_json = response_json
        self.response_text = response_text
        self.headers = dict(headers) if headers is not None else None
