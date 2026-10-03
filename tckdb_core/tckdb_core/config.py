"""Configuration shared by every producer adapter: fields, defaults, API-key resolution.

The adapter is opt-in. A producer parses its own input into a
:class:`TCKDBConfig` (the ARC adapter reads the ``tckdb`` block of its
input file); this module holds only what is the same for every producer, the
fields and defaults and the API-key resolution, and no parser.

API keys themselves are never stored in YAML. The config carries only:

- ``api_key_env`` — the *name* of an env var holding the key
  (default: ``TCKDB_API_KEY``).
- ``api_key_file`` — optional path to a plain text file containing only
  the raw key.
- ``api_key_env_file`` — optional path to a shell/dotenv-style file
  containing a ``TCKDB_API_KEY`` assignment (parsed, never executed).

At upload time, :func:`resolve_tckdb_api_key` resolves the key in that
order; an env var always wins so a temporary override on the command
line still works regardless of the producer's input file.
"""

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from tckdb_core._logging import get_logger


class InputError(ValueError):
    """Raised when parsing adapter input/config fails.

    Used for api-key file / config errors; a standalone class so the package
    carries no producer dependency.
    """


logger = get_logger()


DEFAULT_PAYLOAD_DIR = "tckdb_payloads"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_API_KEY_ENV = "TCKDB_API_KEY"

# Upload-mode switch. ``conformer`` (default) keeps the existing
# /uploads/conformers + per-artifact path. ``computed_species`` builds
# one self-contained bundle and posts it to /uploads/computed-species.
# ``computed_reaction`` builds a reaction bundle (reactants, products,
# inline TS, kinetics) and posts it to /uploads/computed-reaction.
# A run picks one mode; mixing per-species is intentionally not
# supported — combining species + reaction uploads in one run is a
# follow-up that needs a wider config change.
UPLOAD_MODE_CONFORMER = "conformer"
UPLOAD_MODE_COMPUTED_SPECIES = "computed_species"
UPLOAD_MODE_COMPUTED_REACTION = "computed_reaction"
# ``computed_ts`` uploads each converged transition state on its own to
# ``/uploads/transition-states`` (the standalone TS endpoint), with the
# reaction (reactants/products, family, reversibility) embedded from the
# TS's reaction record. Distinct from ``computed_reaction``, which carries
# the TS inline in a full reaction bundle alongside species + kinetics.
UPLOAD_MODE_COMPUTED_TS = "computed_ts"
VALID_UPLOAD_MODES = frozenset({
    UPLOAD_MODE_CONFORMER,
    UPLOAD_MODE_COMPUTED_SPECIES,
    UPLOAD_MODE_COMPUTED_REACTION,
    UPLOAD_MODE_COMPUTED_TS,
})

# Mirrors the server-side ArtifactKind enum
# (backend/app/db/models/common.py:147 in TCKDB_v2). Keeping the source
# of truth here keeps the adapter loud-failing on unknown kinds at
# config parse time rather than at HTTP 422 time.
VALID_ARTIFACT_KINDS = frozenset({
    "input",
    "output_log",
    "checkpoint",
    "formatted_checkpoint",
    "ancillary",
})

# Subset of VALID_ARTIFACT_KINDS the shared upload path can produce.
# Listing a valid-but-not-implemented kind in a producer's config is
# permitted (so users can opt into future kinds early) but warned at
# parse time so the silent-zero-uploads outcome is visible.
#
# As new upload paths land, add their kinds here. Today:
#   - output_log: the calculation's log file
#   - input:      the calculation's input deck
IMPLEMENTED_ARTIFACT_KINDS = frozenset({"output_log", "input"})

DEFAULT_ARTIFACT_KINDS: tuple[str, ...] = ("output_log",)
DEFAULT_ARTIFACT_MAX_SIZE_MB = 50


@dataclass(frozen=True)
class TCKDBArtifactConfig:
    """Per-artifact upload knobs.

    Defaults are conservative: artifact upload is opt-in (``upload=False``)
    and only ``output_log`` is in scope. Users opt in by adding an
    ``artifacts:`` sub-block to the ``tckdb`` config.
    """

    upload: bool = False
    kinds: tuple[str, ...] = DEFAULT_ARTIFACT_KINDS
    max_size_mb: int = DEFAULT_ARTIFACT_MAX_SIZE_MB


@dataclass(frozen=True)
class TCKDBConfig:
    """TCKDB adapter configuration (producer-agnostic fields).

    ``enabled`` is the master switch. ``upload`` controls whether the
    adapter contacts the network at all — when false, payloads are
    written to disk and the sidecar is marked ``skipped``.

    Strict mode raises on upload failure; the default is to log a
    warning, record the error in the sidecar, and let the producer's run continue.
    """

    enabled: bool = False
    base_url: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    api_key_file: str | None = None
    api_key_env_file: str | None = None
    payload_dir: str = DEFAULT_PAYLOAD_DIR
    upload: bool = True
    preflight: bool = True
    strict: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    project_label: str | None = field(
        default=None,
        metadata={"help": "Optional project/run label baked into idempotency keys."},
    )
    upload_mode: str = UPLOAD_MODE_CONFORMER
    artifacts: TCKDBArtifactConfig = field(default_factory=TCKDBArtifactConfig)
    # Phase-1 partial-sidecar support. When true (default), the sweep
    # writes a partial computed-reaction sidecar for any reaction whose
    # TS is missing or non-converged: ``ts_label`` and ``kinetics`` are
    # stripped, the bundle is marked ``is_partial=true``, and the file
    # name gains a ``.partial`` infix. Partial sidecars are deliberately
    # *not* live-POSTed in phase-1 regardless of ``upload`` — they only
    # land on disk. Set to false to suppress partial sidecars entirely
    # (the sweep will then skip reactions with a non-converged TS and
    # log the reason). There is no separate ``live_upload_partial``
    # knob yet; add one only when the TCKDB server has confirmed it
    # accepts ``transition_state=null`` payloads.
    allow_partial_uploads: bool = True

    def resolve_api_key(self) -> str | None:
        """Resolve the API key from env or configured local files.

        Delegates to :func:`resolve_tckdb_api_key`. The value is never
        logged. Raises :class:`InputError` if a configured file is
        missing, empty, or doesn't define the key — misconfigured
        producer input should fail loud instead of silently producing
        zero uploads.
        """
        return resolve_tckdb_api_key(
            api_key_env=self.api_key_env,
            api_key_file=self.api_key_file,
            api_key_env_file=self.api_key_env_file,
        )

    def describe_api_key_sources(self) -> str:
        """Human-readable list of resolution sources, for log messages.

        Includes file paths only when configured, so the dominant
        "env-var-only" case stays terse.
        """
        sources = [f"env var '{self.api_key_env}'"]
        if self.api_key_file:
            sources.append(f"api_key_file={self.api_key_file}")
        if self.api_key_env_file:
            sources.append(f"api_key_env_file={self.api_key_env_file}")
        return ", ".join(sources)


def resolve_tckdb_api_key(
    *,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    api_key_file: str | None = None,
    api_key_env_file: str | None = None,
) -> str | None:
    """Resolve the TCKDB API key from env or configured local files.

    Resolution order:

    1. ``api_key_env`` from the current process environment.
    2. ``api_key_file`` — plain text file containing only the raw key.
    3. ``api_key_env_file`` — shell/dotenv-style file containing a
       ``<api_key_env>=...`` assignment. Parsed, never executed.

    Args:
        api_key_env: Name of the env var to consult (default
            ``TCKDB_API_KEY``). Also used as the variable name to look
            for inside ``api_key_env_file``.
        api_key_file: Optional path to a plain text file whose entire
            (whitespace-stripped) contents are the API key.
        api_key_env_file: Optional path to a small dotenv-style file.

    Returns:
        The resolved API key (whitespace-stripped), or ``None`` when no
        source is configured / set.

    Raises:
        InputError: A file path is configured but missing, unreadable,
            empty, or doesn't define the variable.
    """
    env_key = os.environ.get(api_key_env)
    if env_key:
        return env_key.strip()

    if api_key_file:
        path = Path(api_key_file).expanduser()
        if not path.is_file():
            raise InputError(f"tckdb.api_key_file does not exist: {path}")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(
                f"tckdb.api_key_file is not readable ({path}): {exc}"
            ) from exc
        key = content.strip()
        if not key:
            raise InputError(f"tckdb.api_key_file is empty: {path}")
        return key

    if api_key_env_file:
        path = Path(api_key_env_file).expanduser()
        if not path.is_file():
            raise InputError(f"tckdb.api_key_env_file does not exist: {path}")
        try:
            key = _read_tckdb_api_key_from_env_file(path, api_key_env)
        except OSError as exc:
            raise InputError(
                f"tckdb.api_key_env_file is not readable ({path}): {exc}"
            ) from exc
        if not key:
            raise InputError(
                f"tckdb.api_key_env_file does not define {api_key_env}: {path}"
            )
        return key

    return None


def _read_tckdb_api_key_from_env_file(path: Path, var_name: str) -> str | None:
    """Read ``var_name`` from a small shell/dotenv-style env file.

    Recognized assignment shapes (per :func:`shlex.split` in POSIX mode):

    - ``KEY=abc``
    - ``KEY='abc'``
    - ``KEY="abc"``
    - ``export KEY=...``

    Blank lines and ``#``-prefixed comments are ignored. Other variables
    are skipped. The file is parsed as text — never executed — and POSIX
    ``shlex`` mode does not perform ``$VAR`` interpolation.

    Returns the first matching value found, or ``None`` if the variable
    is not assigned (or its value is empty / unparseable).
    """
    prefix = f"{var_name}="
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if not value:
            return None
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError:
            return None
        if tokens:
            return tokens[0].strip()
    return None
