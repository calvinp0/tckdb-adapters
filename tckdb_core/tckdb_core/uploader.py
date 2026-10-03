"""The producer-agnostic upload pipeline: POST, sidecar bookkeeping, readiness, artifacts.

Everything here happens *after* a producer has built a payload and written it to
disk with :class:`~tckdb_core.payload_writer.PayloadWriter`: the readiness probe
with exponential backoff, the POST through ``tckdb-client``, the sidecar's
status, response summary, server warnings and request ids, strict-mode raising,
calculation-scoped artifact batches with their idempotency keys, and the outcome
objects. :class:`TCKDBUploaderBase` is the class a producer adapter inherits.

Nothing in this module reads a producer's output format.
"""

import base64
import hashlib
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tckdb_client import TCKDBClient
from tckdb_client.errors import TCKDBError

from tckdb_core._logging import CORE_LOGGER_NAME, get_logger, resolve_log
from tckdb_core.config import (
    IMPLEMENTED_ARTIFACT_KINDS,
    UPLOAD_MODE_COMPUTED_REACTION,
    UPLOAD_MODE_COMPUTED_SPECIES,
    TCKDBConfig,
)
from tckdb_core.constants import (
    ARTIFACTS_ENDPOINT_TEMPLATE,
    CONFORMER_UPLOAD_ENDPOINT,
    PREFLIGHT_BASE_DELAY_SECONDS,
    PREFLIGHT_MAX_ATTEMPTS,
    PREFLIGHT_MAX_DELAY_SECONDS,
    _OUTPUT_LOG_ALLOWED_EXTS,
)
from tckdb_core.idempotency import (
    ArtifactIdempotencyInputs,
    build_artifact_idempotency_key,
)
from tckdb_core.outcomes import (
    ArtifactUploadOutcome,
    TCKDBReadinessError,
    UploadOutcome,
    _ArtifactBatchResult,
    _PreparedArtifactUpload,
)
from tckdb_core.payload_writer import (
    PayloadWriter,
    WrittenArtifact,
    WrittenPayload,
    _utcnow_iso,
)
from tckdb_core.adapter_warnings import AdapterWarning
from tckdb_core.warning_codes import CoreWarning

# Upload modes whose bundle payload already carries input/output_log
# artifacts inline under each calculation. Standalone artifact sidecars
# in these modes would (a) duplicate uploads and (b) lock in
# server-assigned calculation IDs that go stale on DB resets — see
# the producer's ``submit_artifacts_for_calculation`` for the gating logic.
_BUNDLE_MODES_WITH_INLINE_ARTIFACTS = frozenset({
    UPLOAD_MODE_COMPUTED_SPECIES,
    UPLOAD_MODE_COMPUTED_REACTION,
})


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _coerce_artifact_filename(name: str, kind: str) -> str:
    """Adapt an artifact ``filename`` to satisfy the backend's per-kind
    extension allowlist. Currently only ``output_log`` is coerced —
    other kinds either match upstream (``input``, ``checkpoint``) or
    aren't emitted by ARC.

    For ``output_log`` files whose extension is already permitted
    (``.log`` / ``.out`` / ``.orca``), the name is returned verbatim.
    For non-conforming names (e.g. GSM ``stringfile.xyz0000``), a
    ``.log`` suffix is appended so the original basename is preserved
    as the prefix (``stringfile.xyz0000.log``). The coercion is a
    no-op for any other kind.
    """
    if kind != "output_log":
        return name
    ext = os.path.splitext(name)[1].lower()
    if ext in _OUTPUT_LOG_ALLOWED_EXTS:
        return name
    return f"{name}.log"


def _artifact_batch_digest(items: list[_PreparedArtifactUpload]) -> str:
    """Content digest for a calculation-scoped artifact batch idempotency key."""
    h = hashlib.sha256()
    for item in items:
        h.update(str(item.handle).encode("utf-8"))
        h.update(b"\0")
        h.update(item.kind.encode("utf-8"))
        h.update(b"\0")
        h.update(item.filename.encode("utf-8"))
        h.update(b"\0")
        h.update(item.sha256.encode("ascii"))
        h.update(b"\0")
        h.update(str(item.bytes).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _artifact_batch_idempotency_prefix(
    project_label: Any,
    species_label: Any,
    *,
    namespace: str,
) -> str:
    """Stable prefix consumed by ``client.upload_artifacts`` batch mode."""
    def clean(value: Any, *, limit: int) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "unknown"))
        cleaned = cleaned.strip(":-.") or "unknown"
        return cleaned[:limit]

    return f"{namespace}:{clean(project_label, limit=48)}:{clean(species_label, limit=64)}:artifact"


def _summarize_artifact_batch_results(batch_results: Any) -> Any:
    """Make tckdb-client ``ArtifactUploadBatchResult`` objects JSON-shaped."""
    if isinstance(batch_results, list):
        summarized = []
        for result in batch_results:
            response = getattr(result, "response", result)
            response_data = getattr(response, "data", response)
            handle = getattr(result, "handle", None)
            summarized.append({
                "calculation_id": handle if isinstance(handle, int) else None,
                "calculation_ref": handle if isinstance(handle, str) else None,
                "calculation_keys": list(getattr(result, "calculation_keys", ()) or ()),
                "artifact_count": getattr(result, "artifact_count", None),
                "response": response_data,
            })
        return summarized
    return batch_results


def _artifact_batch_bodies(
    prepared: list[_PreparedArtifactUpload],
) -> list[tuple[int | str, list[_PreparedArtifactUpload], dict[str, Any]]]:
    """Group a plan by calculation and build each ``ArtifactsUploadRequest`` body.

    Groups keep first-seen order; each artifact carries its kind, filename,
    base64 content and the declared sha256 and byte count.
    """
    groups: dict[int | str, list[_PreparedArtifactUpload]] = {}
    for item in prepared:
        groups.setdefault(item.handle, []).append(item)
    return [
        (
            handle,
            group,
            {"artifacts": [
                {
                    "kind": item.kind,
                    "filename": item.filename,
                    "content_base64": base64.b64encode(item.path.read_bytes()).decode("ascii"),
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                }
                for item in group
            ]},
        )
        for handle, group in groups.items()
    ]


def _server_warnings(response_data: Any) -> list[dict[str, Any]]:
    """The structured ``warnings`` list of an upload response body."""
    warnings = response_data.get("warnings", []) if isinstance(response_data, dict) else []
    if not isinstance(warnings, list):
        return []
    return [dict(item) for item in warnings if isinstance(item, dict)]


def _headers_from(obj: Any) -> Mapping[str, str] | None:
    headers = getattr(obj, "headers", None)
    if headers is None:
        response = getattr(obj, "response", None)
        headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else None


def _request_id_from(obj: Any) -> str | None:
    headers = _headers_from(obj)
    if not headers:
        return None
    for name, value in headers.items():
        if str(name).lower() == "x-request-id" and value:
            return str(value)
    return None


def _append_request_id(sidecar: Any, operation: str, obj: Any) -> None:
    request_id = _request_id_from(obj)
    if not request_id:
        return
    entry = {
        "operation": operation,
        "request_id": request_id,
        "status_code": getattr(obj, "status_code", None),
    }
    if entry["status_code"] is None:
        response = getattr(obj, "response", None)
        entry["status_code"] = getattr(response, "status_code", None)
    sidecar.request_ids.append(entry)


def _attach_preflight(sidecar: Any, metadata: dict[str, Any] | None) -> None:
    if metadata is None:
        return
    sidecar.preflight = dict(metadata)
    request_id = metadata.get("request_id")
    if request_id:
        sidecar.request_ids.append({
            "operation": "readyz",
            "request_id": request_id,
            "status_code": metadata.get("status_code"),
        })


def _preflight_sleep(seconds: float) -> None:
    """Sleep between readiness-probe retries.

    Thin indirection over :func:`time.sleep` so tests can patch out the
    real backoff wait (a producer rebinds ``TCKDBUploaderBase._sleep_between_probes`` to its own module's name)
    and run instantly without changing the retry logic.
    """
    time.sleep(seconds)


def _readyz_body_is_ready(body: Any) -> bool:
    if not isinstance(body, Mapping):
        return False
    if body.get("ready") is True:
        return True
    status = body.get("status")
    return isinstance(status, str) and status.lower() in {"ready", "ok"}


def _build_readiness_error(exc: BaseException) -> TCKDBReadinessError:
    response_json = getattr(exc, "response_json", None)
    response_text = getattr(exc, "response_text", None)
    status_code = getattr(exc, "status_code", None)
    headers = getattr(exc, "headers", None)
    return TCKDBReadinessError(
        _format_readiness_message(
            status_code=status_code,
            body=response_json if response_json is not None else response_text,
            request_id=_request_id_from(exc),
        ),
        status_code=status_code,
        response_json=response_json,
        response_text=response_text,
        headers=headers,
    )


def _format_readiness_message(
    *,
    status_code: Any,
    body: Any,
    request_id: Any,
) -> str:
    status = None
    code = None
    if isinstance(body, Mapping):
        status = body.get("status")
        code = body.get("code")
    parts = ["TCKDB readiness check failed before upload:"]
    if status_code is not None:
        parts.append(f"status_code={status_code}")
    if status is not None:
        parts.append(f"status={status}")
    if code is not None:
        parts.append(f"code={code}")
    if request_id is not None:
        parts.append(f"request_id={request_id}")
    if len(parts) == 1 and body:
        parts.append(str(body))
    return " ".join(parts)


def _summarize_response_body(body: Any, *, max_chars: int = 2000) -> Any:
    """Truncate huge bodies so the sidecar stays small and grep-friendly."""
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    text = str(body)
    if len(text) > max_chars:
        return text[:max_chars] + "...<truncated>"
    return text


_PUBLIC_REF_ECHO_KEYS = frozenset({"request", "requests", "payload", "payloads"})


def _extract_tckdb_public_refs(obj: Any) -> dict[str, list[str]]:
    """Collect returned TCKDB ``*_ref`` strings from a nested response body.

    The helper stores all returned refs it sees outside obvious echoed
    request/payload blocks. Values are grouped by pluralized key name and
    deduped in first-seen order, e.g. ``species_entry_ref`` becomes
    ``species_entry_refs``.
    """
    refs: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    def bucket_for(key: str) -> str:
        return key[:-4] + "_refs"

    def add(key: str, value: str) -> None:
        bucket = bucket_for(key)
        bucket_seen = seen.setdefault(bucket, set())
        if value in bucket_seen:
            return
        bucket_seen.add(value)
        refs.setdefault(bucket, []).append(value)

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_str = str(key)
                if key_str in _PUBLIC_REF_ECHO_KEYS:
                    continue
                if key_str.endswith("_ref") and isinstance(child, str):
                    add(key_str, child)
                    continue
                if key_str == "calculation_key_refs" and isinstance(child, Mapping):
                    # computed-reaction: bundle-local key -> ``calc_`` ref
                    for ref_value in child.values():
                        if isinstance(ref_value, str):
                            add("calculation_ref", ref_value)
                    continue
                if child is None:
                    continue
                walk(child)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return refs


def _extract_calc_refs(
    response_data: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pull primary/additional CalculationUploadRef dicts from a server response.

    Two response shapes are recognized:

    1. **Conformer upload** (``/uploads/conformers``): ``primary_calculation``
       and ``additional_calculations`` sit at the top level::

           {"primary_calculation": {"calculation_id": ..., "type": ...},
            "additional_calculations": [...]}

    2. **Computed-species bundle** (``/uploads/computed-species``): the
       same fields are nested under ``conformers[0]``::

           {"species_entry_id": ..., "conformers": [
              {"key": "...", "primary_calculation": {...},
               "additional_calculations": [...]}, ...]}

       Only the first conformer's refs are surfaced — today's ARC bundles
       carry exactly one conformer per species, so multi-conformer
       responses would need a richer outcome shape, which we'd add when
       the producer side starts emitting them.

    Older server builds omit these fields entirely; the caller treats
    their absence as "no artifact targets known".
    """
    if not isinstance(response_data, Mapping):
        return None, []
    if "conformers" in response_data and isinstance(response_data["conformers"], list):
        confs = response_data["conformers"]
        if confs and isinstance(confs[0], Mapping):
            return _extract_calc_refs(confs[0])
        return None, []
    primary = response_data.get("primary_calculation")
    if not isinstance(primary, Mapping):
        primary = None
    else:
        primary = dict(primary)
    additional_raw = response_data.get("additional_calculations") or []
    additional = [dict(ref) for ref in additional_raw if isinstance(ref, Mapping)]
    return primary, additional


_W_CALCULATION_REF_NOT_RETURNED = CoreWarning.CALCULATION_REF_NOT_RETURNED.value


def _calculation_ref_not_returned_warning(
    calculation_id: int | None, *, producer: str
) -> dict[str, Any]:
    """Self-check finding: an artifact target was named by integer id, not ``calc_`` ref."""
    return AdapterWarning(
        code=_W_CALCULATION_REF_NOT_RETURNED,
        message=(
            f"The upload response carried no calculation_ref for calculation {calculation_id}, "
            "so its artifacts were posted to the deprecated integer-id path and the "
            "idempotency key was built from that id."
        ),
        field="calculation_ref",
        context={"calculation_id": calculation_id},
    ).to_dict(producer)


def _extract_submission_refs(response_data: Any) -> tuple[str | None, dict[str, str]]:
    """The response's ``submission_ref`` (``sub_...``) and ``calculation_key_refs`` map.

    Both are optional (an older server omits them); a value that is not a string is ignored.
    """
    if not isinstance(response_data, Mapping):
        return None, {}
    submission_ref = response_data.get("submission_ref")
    raw = response_data.get("calculation_key_refs")
    key_refs = (
        {str(k): v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, Mapping) else {}
    )
    return (submission_ref if isinstance(submission_ref, str) else None), key_refs


def _skip(
    calculation_id: int | None, kind: str, reason: str, calculation_ref: str | None = None,
    log: Any = None,
) -> "ArtifactUploadOutcome":
    """Build a skipped outcome and log the reason once (to ``log``, else the package logger)."""
    resolve_log(log).info(
        "TCKDB artifact upload skipped: calc=%s kind=%s reason=%s",
        calculation_ref or calculation_id, kind, reason,
    )
    return ArtifactUploadOutcome(
        status="skipped",
        sidecar_path=None,
        idempotency_key=None,
        calculation_id=calculation_id,
        calculation_ref=calculation_ref,
        kind=kind,
        skip_reason=reason,
    )


def _close_quietly(client: Any, context: str, log: Any = None) -> None:
    """Close a TCKDB client and swallow close errors with a debug log."""
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # pragma: no cover - close errors swallowed
        resolve_log(log).debug("TCKDB client close errored %s", context, exc_info=True)


class TCKDBUploaderBase:
    """The producer-agnostic upload, sidecar and readiness pipeline.

    A producer adapter subclasses this, builds its payload, writes it with
    ``self._writer`` and hands it to :meth:`_upload` (or the artifact methods).
    The base owns what happens after a payload is on disk: the readiness probe
    with backoff, the POST, the sidecar status/response/warning/request-id
    bookkeeping, strict-mode raising, artifact batching and idempotency, and the
    outcome objects. It reads nothing from the producer's output format.

    A subclass sets the three class attributes below and may override the hooks.
    """

    #: Machine tag for the producer; sidecar self-check warnings carry
    #: ``context.source == f"{PRODUCER_TAG}_self_check"``.
    PRODUCER_TAG: str = "producer"
    #: Display name used in skip reasons and messages.
    PRODUCER_NAME: str = "the producer"
    #: Namespace of the producer's idempotency keys (``<namespace>:<project>:...``).
    IDEMPOTENCY_NAMESPACE: str = "producer"
    #: Name of the logger the pipeline writes to (``logging.getLogger(LOGGER_NAME)``).
    #: Per producer, not process-global: two producers in one process log under
    #: their own names. A producer may instead override :attr:`_log` to return a
    #: logger object of its own (e.g. a module-level one tests patch).
    LOGGER_NAME: str = CORE_LOGGER_NAME

    def __init__(
        self,
        config: TCKDBConfig,
        *,
        project_directory: str | Path | None = None,
        client_factory=None,
    ):
        self._config = config
        self._project_directory = (
            Path(project_directory) if project_directory is not None else None
        )
        # Resolve payload_dir against the project directory if it's relative,
        # so payloads land under the active project rather than CWD.
        payload_root = Path(config.payload_dir)
        if not payload_root.is_absolute() and project_directory is not None:
            payload_root = Path(project_directory) / payload_root
        self._writer = PayloadWriter(payload_root)
        self._client_factory = client_factory
        self._preflight_checked = False
        self._preflight_metadata: dict[str, Any] | None = None
        self._preflight_error: TCKDBReadinessError | None = None

    # ------------------------------------------------------------------
    # Hooks a producer may override
    # ------------------------------------------------------------------

    @property
    def _log(self):
        """The logger the pipeline writes to (a producer may return its own module's)."""
        return get_logger(self.LOGGER_NAME)

    def _sleep_between_probes(self, seconds: float) -> None:
        """Wait between readiness-probe retries (a producer may rebind this for tests)."""
        _preflight_sleep(seconds)

    def _project_label_for(self, output_doc: Mapping[str, Any]) -> Any:
        """The project label to key artifact uploads on when the config names none."""
        return None

    def _log_readiness_recovery(self) -> None:
        """Tell the user the payloads are on disk once the server was never ready."""
        self._log.warning(
            "TCKDB server was not ready after %d attempts; payloads were "
            "written to %s. Re-run the upload once it is up.",
            PREFLIGHT_MAX_ATTEMPTS, self._writer.root,
        )

    # ------------------------------------------------------------------
    # Local files
    # ------------------------------------------------------------------

    def _resolve_local_path(self, file_path: str | Path) -> Path | None:
        """Resolve a local file path against project_directory if it's relative."""
        if file_path is None:
            return None
        path = Path(file_path)
        if path.is_absolute():
            return path
        if self._project_directory is not None:
            return Path(self._project_directory) / path
        return path.resolve()

    def _read_artifact_file(
        self,
        path_value: str | Path,
        *,
        calc_role: str,
        kind: str,
        label: Any,
    ) -> dict[str, Any] | None:
        """Resolve, read, hash, and base64-encode one artifact for a bundle.

        Returns ``None`` (with a debug or warning log) when the file is not on
        disk or exceeds ``max_size_mb``. Otherwise returns the ``ArtifactIn``-shaped
        dict ready to drop into ``calc.artifacts``.
        """
        resolved = self._resolve_local_path(path_value)
        if resolved is None or not resolved.is_file():
            self._log.debug(
                "TCKDB bundle: %s %s artifact missing on disk for %s (path=%s)",
                calc_role, kind, label, path_value,
            )
            return None
        size_bytes = resolved.stat().st_size
        max_bytes = self._config.artifacts.max_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            self._log.warning(
                "TCKDB bundle: %s %s %s skipped (%s bytes > %s MB cap)",
                calc_role, kind, resolved.name, size_bytes,
                self._config.artifacts.max_size_mb,
            )
            return None
        with resolved.open("rb") as fh:
            content = fh.read()
        return {
            "kind": kind,
            "filename": _coerce_artifact_filename(resolved.name, kind),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": size_bytes,
        }

    # ------------------------------------------------------------------
    # Upload + sidecar finalization
    # ------------------------------------------------------------------

    def _finalize_skipped(self, written: WrittenPayload) -> UploadOutcome:
        sc = written.sidecar
        sc.status = "skipped"
        self._writer.update_sidecar(written.sidecar_path, sc)
        # Two distinct reasons land here: (a) ``upload=false`` for a
        # complete bundle, and (b) phase-1 partial sidecars that are
        # intentionally never live-POSTed. Pick the right log line so
        # operators reading logs don't have to grep ``is_partial`` to
        # reverse-engineer the cause.
        if sc.is_partial:
            self._log.info(
                "TCKDB partial sidecar finalized (status=skipped, is_partial=true; "
                "phase-1 policy: partial bundles are sidecar-only, never POSTed): %s",
                written.payload_path,
            )
        else:
            self._log.info(
                "TCKDB upload skipped (upload=false): %s", written.payload_path,
            )
        return UploadOutcome(
            status="skipped",
            payload_path=written.payload_path,
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            warnings=list(sc.warnings),
        )

    def _upload(
        self,
        written: WrittenPayload,
        payload: dict[str, Any],
        *,
        endpoint: str = CONFORMER_UPLOAD_ENDPOINT,
    ) -> UploadOutcome:
        sc = written.sidecar
        api_key = self._config.resolve_api_key()
        if not api_key:
            msg = (
                f"TCKDB API key not configured (tried: "
                f"{self._config.describe_api_key_sources()}); "
                "skipping network call and recording sidecar as failed."
            )
            return self._record_failure(written, msg, raised=ValueError(msg))

        try:
            client = self._make_client(api_key)
        except Exception as exc:  # pragma: no cover - defensive
            return self._record_failure(written, f"client init failed: {exc}", exc)

        try:
            self._ensure_ready(client)
            _attach_preflight(sc, self._preflight_metadata)
            response = client.request_json(
                "POST",
                endpoint,
                json=payload,
                idempotency_key=sc.idempotency_key,
            )
        except Exception as exc:
            _close_quietly(client, "after upload failure", self._log)
            return self._record_failure(written, str(exc), exc)
        else:
            _close_quietly(client, "after upload success", self._log)

        sc.status = "uploaded"
        sc.uploaded_at = _utcnow_iso()
        sc.response_status_code = getattr(response, "status_code", None)
        response_data = getattr(response, "data", None)
        sc.response_body = _summarize_response_body(response_data)
        # Keep the server's structured scientific findings independently of
        # the response summary so callers need not inspect an HTTP envelope.
        # They follow any producer-side warnings recorded at write time.
        server_warnings = _server_warnings(response_data)
        for warning in server_warnings:
            self._log.warning("TCKDB upload warning: %s", warning)
        sc.warnings = [*sc.warnings, *server_warnings]
        sc.public_refs = _extract_tckdb_public_refs(response_data)
        _append_request_id(sc, "upload", response)
        sc.idempotency_replayed = bool(getattr(response, "idempotency_replayed", False))
        sc.last_error = None
        self._writer.update_sidecar(written.sidecar_path, sc)
        if sc.idempotency_replayed:
            self._log.info(
                "TCKDB upload replayed (idempotent): %s key=%s",
                written.payload_path,
                sc.idempotency_key,
            )
        else:
            self._log.info(
                "TCKDB upload succeeded: %s key=%s",
                written.payload_path,
                sc.idempotency_key,
            )
        primary, additional = _extract_calc_refs(response_data)
        submission_ref, key_refs = _extract_submission_refs(response_data)
        return UploadOutcome(
            status="uploaded",
            payload_path=written.payload_path,
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            response=sc.response_body,
            primary_calculation=primary,
            additional_calculations=additional,
            warnings=sc.warnings,
            submission_ref=submission_ref,
            calculation_key_refs=key_refs,
        )

    def _record_failure(
        self, written: WrittenPayload, message: str, raised: BaseException
    ) -> UploadOutcome:
        sc = written.sidecar
        sc.status = "failed"
        # Pull structured HTTP details off the exception when present
        # (TCKDBClient attaches ``status_code``, ``response_json``, and
        # ``response_text`` on its HTTP-error subclasses). FastAPI 422
        # bodies otherwise collapse to "HTTP 422" via the exception's
        # default message, hiding the field-level rejection reason.
        # Non-HTTP failures (timeouts, connection errors, etc.) lack
        # these attrs and fall back to the original ``message`` —
        # preserving the prior log shape for those paths.
        status_code = getattr(raised, "status_code", None)
        response_json = getattr(raised, "response_json", None)
        response_text = getattr(raised, "response_text", None)
        if response_json is not None:
            sc.response_body = _summarize_response_body(response_json)
        elif response_text:
            sc.response_body = _summarize_response_body(response_text)
        if status_code is not None:
            sc.response_status_code = status_code
        if isinstance(raised, TCKDBReadinessError):
            sc.preflight = {
                "ready": False,
                "status_code": status_code,
                "request_id": _request_id_from(raised),
            }
            _append_request_id(sc, "readyz", raised)
        else:
            _append_request_id(sc, "upload", raised)
        detail_for_message: Any = (
            response_json
            if response_json is not None
            else (response_text or message)
        )
        sc.last_error = (
            f"HTTP {status_code}: {detail_for_message}"
            if status_code is not None
            else message
        )
        self._writer.update_sidecar(written.sidecar_path, sc)
        self._log.warning(
            "TCKDB upload failed (strict=%s): %s key=%s err=%s",
            self._config.strict,
            written.payload_path,
            sc.idempotency_key,
            sc.last_error,
        )
        if isinstance(raised, TCKDBReadinessError):
            self._log_readiness_recovery()
        if self._config.strict:
            raise raised
        return UploadOutcome(
            status="failed",
            payload_path=written.payload_path,
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            error=sc.last_error,
            warnings=list(sc.warnings),
        )

    def _make_client(self, api_key: str):
        if self._client_factory is not None:
            return self._client_factory(self._config, api_key)
        return TCKDBClient(
            self._config.base_url,
            api_key=api_key,
            timeout=self._config.timeout_seconds,
        )

    def _ensure_ready(self, client: Any) -> None:
        """Run the optional TCKDB readyz preflight once per adapter instance.

        The probe is retried up to :data:`PREFLIGHT_MAX_ATTEMPTS` times
        with exponential backoff (see the module constants) so a transient
        not-ready blip mid-run doesn't drop an upload. Both failure modes
        are retried: a request exception (server unreachable / 5xx) and a
        200 body that reports not-ready. The first attempt that reports
        ready wins; only after all attempts are exhausted is
        ``self._preflight_error`` set and raised. The single-check-per-
        instance semantics are preserved — the whole retry loop runs once,
        and the final error is cached so later calls re-raise it cheaply.
        """
        if not self._config.preflight:
            return
        if self._preflight_error is not None:
            raise self._preflight_error
        if self._preflight_checked:
            return
        self._preflight_checked = True

        last_error: TCKDBReadinessError | None = None
        for attempt in range(PREFLIGHT_MAX_ATTEMPTS):
            try:
                response = client.request_json("GET", "/readyz", authenticated=False)
            except TCKDBError as exc:
                # Server unreachable / timeout / 5xx during a blip. Build
                # the readiness error now so, if this is the last attempt,
                # we raise a message consistent with the not-ready path.
                last_error = _build_readiness_error(exc)
            else:
                data = getattr(response, "data", None)
                ready = _readyz_body_is_ready(data)
                metadata = {
                    "ready": ready,
                    "status_code": getattr(response, "status_code", None),
                    "request_id": _request_id_from(response),
                }
                if isinstance(data, Mapping):
                    for key in ("status", "code", "alembic_revision"):
                        if data.get(key) is not None:
                            metadata[key] = data.get(key)
                self._preflight_metadata = metadata
                if ready:
                    return
                last_error = TCKDBReadinessError(
                    _format_readiness_message(
                        status_code=metadata.get("status_code"),
                        body=data,
                        request_id=metadata.get("request_id"),
                    ),
                    status_code=metadata.get("status_code"),
                    response_json=data if isinstance(data, Mapping) else None,
                    response_text=None if isinstance(data, Mapping) else str(data),
                    headers=getattr(response, "headers", None),
                )

            # Not ready (error or not-ready body). Back off before the
            # next attempt unless this was the final one.
            if attempt < PREFLIGHT_MAX_ATTEMPTS - 1:
                delay = min(
                    PREFLIGHT_BASE_DELAY_SECONDS * (2 ** attempt),
                    PREFLIGHT_MAX_DELAY_SECONDS,
                )
                self._log.info(
                    "TCKDB /readyz not ready (attempt %d/%d); retrying in %.1fs.",
                    attempt + 1, PREFLIGHT_MAX_ATTEMPTS, delay,
                )
                self._sleep_between_probes(delay)

        # All attempts exhausted. ``last_error`` is always set here because
        # every loop iteration that reaches this point set it.
        self._preflight_error = last_error
        raise self._preflight_error

    def _legacy_uploaded_artifact(
        self, species_label: str, calculation_id: int | None, kind: str, sha256: str,
    ) -> Path | None:
        """Path of the pre-0.10 (``calc{int}``) sidecar when it records this artifact as uploaded.

        It counts only when its status is ``uploaded``, its sha256 is this artifact's, and, when
        both state one, it was posted to the same server (an integer id means nothing on another).
        """
        found = self._writer.read_legacy_artifact_sidecar(
            species_label=species_label, calculation_id=calculation_id, kind=kind)
        if found is None:
            return None
        path, data = found
        if data.get("status") != "uploaded" or data.get("sha256") != sha256:
            return None
        old_url, new_url = data.get("base_url"), self._config.base_url
        if old_url and new_url and str(old_url).rstrip("/") != str(new_url).rstrip("/"):
            return None
        return path

    def _prepare_artifact_upload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_label: str,
        calculation_id: int | None,
        kind: str,
        file_path: str | Path,
        artifact_cfg: Any,
        calculation_ref: str | None = None,
    ) -> _PreparedArtifactUpload | ArtifactUploadOutcome:
        handle = calculation_ref if calculation_ref else calculation_id
        # Defense-in-depth: in bundle modes the bundle payload already
        # carries input/output_log artifacts inline under each calc.
        if (
            self._config.upload_mode in _BUNDLE_MODES_WITH_INLINE_ARTIFACTS
            and kind in IMPLEMENTED_ARTIFACT_KINDS
        ):
            return _skip(
                calculation_id, kind,
                f"upload_mode={self._config.upload_mode!r} carries kind={kind!r} "
                "inline in the bundle; standalone artifact upload suppressed",
                calculation_ref, log=self._log)

        if not artifact_cfg.upload:
            return _skip(calculation_id, kind, "artifacts.upload is False", calculation_ref, log=self._log)
        if kind not in artifact_cfg.kinds:
            return _skip(calculation_id, kind, f"kind {kind!r} not in config.kinds", calculation_ref, log=self._log)
        if kind not in IMPLEMENTED_ARTIFACT_KINDS:
            return _skip(
                calculation_id, kind,
                f"kind {kind!r} is server-accepted but {self.PRODUCER_NAME} has no upload path yet", log=self._log)

        resolved = self._resolve_local_path(file_path)
        if resolved is None or not resolved.is_file():
            return _skip(calculation_id, kind, f"file missing: {file_path!r}", calculation_ref, log=self._log)

        size_bytes = resolved.stat().st_size
        max_bytes = artifact_cfg.max_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            return _skip(
                calculation_id,
                kind,
                f"file {resolved.name} is {size_bytes} bytes "
                f"(>{artifact_cfg.max_size_mb} MB cap)",
                calculation_ref, log=self._log)

        with resolved.open("rb") as fh:
            content = fh.read()
        sha256 = hashlib.sha256(content).hexdigest()

        if calculation_ref:
            # A project uploaded before 0.10 keyed (and named the sidecar of) each artifact by the
            # calculation's integer id. TCKDB keeps one calculation_artifact row per upload, even for
            # identical bytes, so re-posting under the ref-keyed key would duplicate the row: an
            # artifact the old sidecar records as uploaded, with the same bytes, is done.
            done = self._legacy_uploaded_artifact(species_label, calculation_id, kind, sha256)
            if done is not None:
                return _skip(
                    calculation_id, kind,
                    f"already uploaded by a pre-0.10 run (sidecar {done.name}, same sha256); "
                    "re-posting would add a second calculation_artifact row",
                    calculation_ref, log=self._log)

        project_label = self._config.project_label or self._project_label_for(output_doc)
        idempotency_key = build_artifact_idempotency_key(ArtifactIdempotencyInputs(
            project_label=project_label,
            species_label=species_label,
            calculation_id=calculation_id,
            calculation_ref=calculation_ref,
            artifact_kind=kind,
            artifact_sha256=sha256,
        ), namespace=self.IDEMPOTENCY_NAMESPACE)
        endpoint = ARTIFACTS_ENDPOINT_TEMPLATE.format(calculation_id=handle)
        written_artifact = self._writer.write_artifact_sidecar(
            species_label=species_label,
            calculation_id=calculation_id,
            calculation_ref=calculation_ref,
            kind=kind,
            filename=resolved.name,
            sha256=sha256,
            bytes_=size_bytes,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            source_path=str(resolved),
            base_url=self._config.base_url,
        )
        return _PreparedArtifactUpload(
            written=written_artifact,
            calculation_key=f"{kind}-{sha256[:16]}",
            calculation_id=calculation_id,
            path=resolved,
            kind=kind,
            label=species_label,
            sha256=sha256,
            bytes=size_bytes,
            filename=resolved.name,
            calculation_ref=calculation_ref,
        )

    def _upload_artifact_batch(
        self,
        *,
        prepared: list[_PreparedArtifactUpload],
        idempotency_key_prefix: str,
    ) -> list[ArtifactUploadOutcome]:
        api_key = self._config.resolve_api_key()
        if not api_key:
            msg = (
                f"TCKDB API key not configured (tried: "
                f"{self._config.describe_api_key_sources()}); "
                "skipping artifact network call."
            )
            return self._record_artifact_batch_failure(prepared, msg, ValueError(msg))

        try:
            client = self._make_client(api_key)
        except Exception as exc:  # pragma: no cover - defensive
            return self._record_artifact_batch_failure(
                prepared, f"client init failed: {exc}", exc
            )

        try:
            self._ensure_ready(client)
            for item in prepared:
                _attach_preflight(item.written.sidecar, self._preflight_metadata)
            batch_results = self._post_artifact_batches(
                client, prepared, idempotency_key_prefix=idempotency_key_prefix,
            )
        except Exception as exc:
            _close_quietly(client, "after artifact upload failure", self._log)
            return self._record_artifact_batch_failure(prepared, str(exc), exc)
        else:
            _close_quietly(client, "after artifact upload success", self._log)

        batch_summary = _summarize_artifact_batch_results(batch_results)
        result_by_calculation = {result.handle: result for result in batch_results}
        outcomes: list[ArtifactUploadOutcome] = []
        for item in prepared:
            sc = item.written.sidecar
            # Each item takes the transport metadata and findings of the
            # response to *its own* calculation's batch.
            result = result_by_calculation[item.handle]
            response = result.response
            response_data = getattr(response, "data", None)
            sc.status = "uploaded"
            sc.uploaded_at = _utcnow_iso()
            sc.response_status_code = getattr(response, "status_code", None)
            sc.response_body = _summarize_response_body(batch_summary)
            sc.public_refs = _extract_tckdb_public_refs(batch_summary)
            server_warnings = _server_warnings(response_data)
            for warning in server_warnings:
                self._log.warning("TCKDB artifact upload warning: %s", warning)
            sc.warnings = [*sc.warnings, *server_warnings]
            _append_request_id(sc, "artifact_upload", result)
            sc.idempotency_replayed = bool(getattr(response, "idempotency_replayed", False))
            sc.last_error = None
            if not item.calculation_ref:
                sc.warnings = [*sc.warnings, _calculation_ref_not_returned_warning(item.calculation_id, producer=self.PRODUCER_TAG)]
            self._writer.update_artifact_sidecar(item.written.sidecar_path, sc)
            self._log.info(
                "TCKDB artifact batch upload succeeded: calc=%s kind=%s key=%s",
                sc.calculation_ref or sc.calculation_id, sc.kind, sc.idempotency_key,
            )
            outcomes.append(ArtifactUploadOutcome(
                status="uploaded",
                sidecar_path=item.written.sidecar_path,
                idempotency_key=sc.idempotency_key,
                calculation_id=sc.calculation_id,
                calculation_ref=sc.calculation_ref,
                kind=sc.kind,
                response=sc.response_body,
                warnings=list(sc.warnings),
            ))
        return outcomes

    @staticmethod
    def _post_artifact_batches(
        client: Any,
        prepared: list[_PreparedArtifactUpload],
        *,
        idempotency_key_prefix: str,
    ) -> list[_ArtifactBatchResult]:
        """POST one artifact batch per calculation through ``client.request_json``.

        ``TCKDBClient.upload_artifacts`` (tckdb-client 0.93 to 0.95) does the
        same grouping and POSTs, but returns only each response's parsed body,
        dropping the status code, the request-id and replay headers. The public
        ``request_json`` returns the full ``TCKDBResponse``, so the batches are
        composed here and sent through it, the way every bundle upload already
        is. The bodies and idempotency keys follow ``upload_artifacts`` exactly
        (one POST per ``calculation_id`` in first-seen order, key
        ``<prefix>:<first calculation key>:artifact-batch``), so a replay of a
        batch stored by an earlier adapter still matches.
        """
        from tckdb_client.idempotency import validate_idempotency_key

        results: list[_ArtifactBatchResult] = []
        for handle, group, body in _artifact_batch_bodies(prepared):
            first_key = group[0].calculation_key if group else str(handle)
            response = client.request_json(
                "POST",
                ARTIFACTS_ENDPOINT_TEMPLATE.format(calculation_id=handle),
                json=body,
                idempotency_key=validate_idempotency_key(
                    f"{idempotency_key_prefix}:{first_key}:artifact-batch"
                ),
            )
            results.append(_ArtifactBatchResult(
                handle=handle,
                calculation_keys=tuple(item.calculation_key for item in group),
                artifact_count=len(group),
                response=response,
            ))
        return results

    def _record_artifact_batch_failure(
        self,
        prepared: list[_PreparedArtifactUpload],
        message: str,
        raised: BaseException,
    ) -> list[ArtifactUploadOutcome]:
        outcomes = [
            self._record_artifact_failure(item.written, message, raised, raise_on_strict=False)
            for item in prepared
        ]
        if self._config.strict:
            raise raised
        return outcomes

    def _record_artifact_failure(
        self,
        written: WrittenArtifact,
        message: str,
        raised: BaseException,
        *,
        raise_on_strict: bool = True,
    ) -> ArtifactUploadOutcome:
        sc = written.sidecar
        sc.status = "failed"
        sc.last_error = message
        status_code = getattr(raised, "status_code", None)
        response_json = getattr(raised, "response_json", None)
        response_text = getattr(raised, "response_text", None)
        if status_code is not None:
            sc.response_status_code = status_code
        if response_json is not None:
            sc.response_body = _summarize_response_body(response_json)
        elif response_text:
            sc.response_body = _summarize_response_body(response_text)
        if isinstance(raised, TCKDBReadinessError):
            sc.preflight = {
                "ready": False,
                "status_code": getattr(raised, "status_code", None),
                "request_id": _request_id_from(raised),
            }
            _append_request_id(sc, "readyz", raised)
        else:
            _append_request_id(sc, "artifact_upload", raised)
        self._writer.update_artifact_sidecar(written.sidecar_path, sc)
        self._log.warning(
            "TCKDB artifact upload failed (strict=%s): calc=%s kind=%s err=%s",
            self._config.strict, sc.calculation_id, sc.kind, message,
        )
        if self._config.strict and raise_on_strict:
            raise raised
        return ArtifactUploadOutcome(
            status="failed",
            sidecar_path=written.sidecar_path,
            idempotency_key=sc.idempotency_key,
            calculation_id=sc.calculation_id,
            calculation_ref=sc.calculation_ref,
            kind=sc.kind,
            error=message,
        )
