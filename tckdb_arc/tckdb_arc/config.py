"""Configuration for the ARC TCKDB adapter.

The adapter is opt-in. If no ``tckdb`` block is present in the ARC input
(or ``enabled`` is false), :func:`TCKDBConfig.from_dict` returns ``None``
and the adapter is a no-op.

The fields, defaults and API-key resolution live in
:mod:`tckdb_core.config` (shared by every producer adapter); this module is
the ARC-specific part, the parser of the ``tckdb`` block of ARC's input.yml,
plus re-exports so ``tckdb_arc.config`` keeps resolving every name it did.

API keys themselves are never stored in YAML; see
:func:`tckdb_core.config.resolve_tckdb_api_key` for the resolution order.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tckdb_arc._logging import get_logger
from tckdb_core.config import (  # noqa: F401  (re-exported for backward compatibility)
    DEFAULT_API_KEY_ENV,
    DEFAULT_ARTIFACT_KINDS,
    DEFAULT_ARTIFACT_MAX_SIZE_MB,
    DEFAULT_PAYLOAD_DIR,
    DEFAULT_TIMEOUT_SECONDS,
    IMPLEMENTED_ARTIFACT_KINDS,
    UPLOAD_MODE_COMPUTED_REACTION,
    UPLOAD_MODE_COMPUTED_SPECIES,
    UPLOAD_MODE_COMPUTED_TS,
    UPLOAD_MODE_CONFORMER,
    VALID_ARTIFACT_KINDS,
    VALID_UPLOAD_MODES,
    InputError,
    TCKDBArtifactConfig as _CoreArtifactConfig,
    TCKDBConfig as _CoreConfig,
    resolve_tckdb_api_key,
)

logger = get_logger()


@dataclass(frozen=True)
class TCKDBArtifactConfig(_CoreArtifactConfig):
    """Per-artifact upload knobs, parsed from the ``artifacts:`` sub-block of ``tckdb``."""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "TCKDBArtifactConfig":
        if not raw:
            return cls()
        kinds_raw = raw.get("kinds", DEFAULT_ARTIFACT_KINDS)
        if isinstance(kinds_raw, str):
            kinds_raw = (kinds_raw,)
        kinds = tuple(str(k) for k in kinds_raw)
        unknown = [k for k in kinds if k not in VALID_ARTIFACT_KINDS]
        if unknown:
            raise ValueError(
                f"tckdb.artifacts.kinds contains unknown kind(s): {unknown}. "
                f"Valid kinds: {sorted(VALID_ARTIFACT_KINDS)}."
            )
        not_implemented = [k for k in kinds if k not in IMPLEMENTED_ARTIFACT_KINDS]
        if not_implemented:
            logger.warning(
                "tckdb.artifacts.kinds includes kind(s) the TCKDB server accepts "
                "but ARC doesn't yet produce uploads for: %s. Currently implemented: %s. "
                "These will be silently skipped at upload time.",
                not_implemented, sorted(IMPLEMENTED_ARTIFACT_KINDS),
            )
        max_size_mb = int(raw.get("max_size_mb", DEFAULT_ARTIFACT_MAX_SIZE_MB))
        if max_size_mb <= 0:
            raise ValueError(
                f"tckdb.artifacts.max_size_mb must be > 0; got {max_size_mb}."
            )
        return cls(
            upload=bool(raw.get("upload", False)),
            kinds=kinds,
            max_size_mb=max_size_mb,
        )


@dataclass(frozen=True)
class TCKDBConfig(_CoreConfig):
    """ARC-side TCKDB adapter configuration, parsed from input.yml's ``tckdb`` block."""

    artifacts: TCKDBArtifactConfig = field(default_factory=TCKDBArtifactConfig)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "TCKDBConfig | None":
        """Build a config from a raw mapping, or return ``None`` when disabled.

        Returning ``None`` for the disabled case lets callers write
        ``if cfg is None: return`` rather than checking a flag.
        """
        if not raw:
            return None
        if not raw.get("enabled", False):
            return None
        base_url = raw.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("tckdb.base_url is required when tckdb.enabled is true.")
        upload_mode = str(raw.get("upload_mode", UPLOAD_MODE_CONFORMER))
        if upload_mode not in VALID_UPLOAD_MODES:
            raise ValueError(
                f"tckdb.upload_mode must be one of {sorted(VALID_UPLOAD_MODES)}; "
                f"got {upload_mode!r}."
            )
        api_key_file = raw.get("api_key_file")
        if api_key_file is not None and not isinstance(api_key_file, str):
            raise ValueError("tckdb.api_key_file must be a string path or unset.")
        api_key_env_file = raw.get("api_key_env_file")
        if api_key_env_file is not None and not isinstance(api_key_env_file, str):
            raise ValueError("tckdb.api_key_env_file must be a string path or unset.")
        return cls(
            enabled=True,
            base_url=base_url,
            api_key_env=str(raw.get("api_key_env", DEFAULT_API_KEY_ENV)),
            api_key_file=api_key_file or None,
            api_key_env_file=api_key_env_file or None,
            payload_dir=str(raw.get("payload_dir", DEFAULT_PAYLOAD_DIR)),
            upload=bool(raw.get("upload", True)),
            preflight=bool(raw.get("preflight", True)),
            strict=bool(raw.get("strict", False)),
            timeout_seconds=float(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            project_label=raw.get("project_label"),
            upload_mode=upload_mode,
            artifacts=TCKDBArtifactConfig.from_dict(raw.get("artifacts")),
            allow_partial_uploads=bool(raw.get("allow_partial_uploads", True)),
        )
