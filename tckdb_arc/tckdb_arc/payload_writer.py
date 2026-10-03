"""Backward-compatible re-export: this module moved to :mod:`tckdb_core.payload_writer`."""

from tckdb_core.payload_writer import (  # noqa: F401
    _SAFE_LABEL,
    _utcnow_iso,
    _safe_label,
    _fs_safe_key,
    BUNDLE_FORMAT_VERSION,
    SidecarMetadata,
    WrittenPayload,
    ArtifactSidecarMetadata,
    WrittenArtifact,
    PayloadWriter,
    should_replay_sidecar,
)
