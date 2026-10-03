"""Compatibility module: the payload writer moved to :mod:`tckdb_core.payload_writer`.

The old public names are forwarded to :mod:`tckdb_core.payload_writer` with a ``DeprecationWarning``.
"""

from tckdb_arc._compat import forward_to_core

__getattr__ = forward_to_core(__name__, "tckdb_core.payload_writer", (
    "ArtifactSidecarMetadata",
    "BUNDLE_FORMAT_VERSION",
    "PayloadWriter",
    "SidecarMetadata",
    "WrittenArtifact",
    "WrittenPayload",
    "should_replay_sidecar",
))
