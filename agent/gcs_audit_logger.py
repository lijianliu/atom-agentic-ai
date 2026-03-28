"""
gcs_audit_logger.py — Async-friendly GCS activity logger for Atom Agent sessions.
=============================================================================
Buffers events in-memory as JSONL records; each flush overwrites the blob
with the full accumulated content (simple + race-condition-free).

GCS blob path:  {prefix}/{YYYY-MM-DD}/{session_id}.jsonl

Usage:
    logger = GCSLogger.from_env()   # reads ATOM_AUDIT_LOG_GCS_PATH env var
    if logger:
        await logger.log("user_prompt", {"prompt": "..."})
        await logger.close()        # flush + write session_end event

All public methods are fire-and-forget: exceptions are caught and printed
as warnings — they NEVER propagate to the caller.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)

# Lazy imports so the agent still starts even if the SDK is not installed.
try:
    from google.cloud import storage as _gcs  # type: ignore[import]
    from google.oauth2.credentials import Credentials as _TokenCredentials  # type: ignore[import]
    _GCS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _GCS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTO_FLUSH_EVERY = 50        # flush after this many pending events (safety valve)
IDLE_FLUSH_AFTER = 60.0      # seconds of inactivity before background flush
IDLE_CHECK_INTERVAL = 10.0   # how often the background task polls (seconds)
TOKEN_TTL_SECONDS = 60       # re-fetch gcloud token after this many seconds
ENV_PATH = "ATOM_AUDIT_LOG_GCS_PATH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# GCSLogger
# ---------------------------------------------------------------------------

class GCSLogger:
    """Non-blocking, JSONL-based GCS activity logger.

    Parameters
    ----------
    bucket_name:
        GCS bucket name (without ``gs://`` prefix).
    session_id:
        Unique session identifier.  Auto-generated UUID4 if not provided.
    prefix:
        Path prefix inside the bucket (default: ``logs``).

    Background flush
    ----------------
    A background asyncio task is started on the first ``log()`` call.
    It polls every ``IDLE_CHECK_INTERVAL`` seconds and flushes to GCS
    whenever ``IDLE_FLUSH_AFTER`` seconds have passed since the last
    ``log()`` call and there are un-flushed events pending.
    """

    def __init__(
        self,
        bucket_name: str,
        session_id: str | None = None,
        prefix: str = "",
    ) -> None:
        self.bucket_name = bucket_name
        self.session_id: str = session_id or str(uuid.uuid4())
        self.prefix = prefix.rstrip("/")
        self._started_at: str = _utcnow()
        self._all_lines: list[str] = []          # entire accumulated content
        self._pending_count: int = 0              # lines not yet written to GCS
        self._last_updated_at: float | None = None  # monotonic time of last log()
        self._bg_task: asyncio.Task | None = None   # idle-flush background task
        self._closed: bool = False

        if not _GCS_AVAILABLE:
            logger.warning(
                "GCSLogger: 'google-cloud-storage' not installed — "
                "logging disabled. Fix with:  uv add google-cloud-storage"
            )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        session_id: str | None = None,
    ) -> "GCSLogger | None":
        """Create a logger from environment variables.

        Returns ``None`` (silently) when ``ATOM_AUDIT_LOG_GCS_PATH`` is not set,
        so callers can do:  ``if logger: await logger.log(...)``

        Environment variables
        ---------------------
        ATOM_AUDIT_LOG_GCS_PATH     Required. Full GCS path, e.g.
                                    ``gs://my-bucket/some/prefix``.
                                    The first segment after ``gs://`` is the
                                    bucket name; the rest becomes the blob
                                    prefix.
        """
        raw = os.environ.get(ENV_PATH, "").strip()
        if not raw:
            return None
        if not raw.startswith("gs://"):
            logger.warning(
                "%s must start with 'gs://' (got '%s') — logging disabled.",
                ENV_PATH, raw,
            )
            return None

        # Parse gs://bucket-name/optional/path/prefix
        path_part = raw.removeprefix("gs://").strip("/")
        segments = path_part.split("/", 1)
        bucket_name = segments[0]
        prefix = segments[1] if len(segments) > 1 else ""

        return GCSLogger(bucket_name=bucket_name, session_id=session_id, prefix=prefix)

    # ------------------------------------------------------------------
    # Public API  (all fire-and-forget — never raise)
    # ------------------------------------------------------------------

    async def log(self, event: str, data: dict[str, Any]) -> None:
        """Append a structured event to the in-memory buffer.

        Auto-flushes to GCS every ``AUTO_FLUSH_EVERY`` pending events.
        Starts the background idle-flush task on the first call.
        """
        if not _GCS_AVAILABLE or self._closed:
            return
        record: dict[str, Any] = {
            "ts": _utcnow(),
            "session_id": self.session_id,
            "event": event,
        }
        record.update(data)
        self._all_lines.append(json.dumps(record, ensure_ascii=False))
        self._pending_count += 1
        self._last_updated_at = time.monotonic()  # stamp activity time
        self._ensure_bg_task()                    # no-op after first call

        if self._pending_count >= AUTO_FLUSH_EVERY:
            await self._safe_flush()

    async def flush(self) -> None:
        """Manually flush the current buffer to GCS."""
        await self._safe_flush()

    async def close(self) -> None:
        """Log a ``session_end`` event, cancel the bg task, flush, and seal."""
        if self._closed:
            return
        self._stop_bg_task()
        await self.log("session_end", {"started_at": self._started_at})
        await self._safe_flush()
        self._closed = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def blob_path(self) -> str:
        """GCS object path (without bucket), e.g. ``logs/2026-03-27/<uuid>.jsonl``."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{self.prefix}/{date}/{self.session_id}.jsonl"

    @property
    def gcs_uri(self) -> str:
        """Full GCS URI, e.g. ``gs://my-bucket/logs/2026-03-27/<uuid>.jsonl``."""
        return f"gs://{self.bucket_name}/{self.blob_path}"


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_bg_task(self) -> None:
        """Spin up the idle-flush background task exactly once."""
        if self._bg_task is None or self._bg_task.done():
            self._bg_task = asyncio.create_task(
                self._bg_flush_loop(),
                name=f"gcs-idle-flush-{self.session_id[:8]}",
            )

    def _stop_bg_task(self) -> None:
        """Cancel the background task (best-effort, never raises)."""
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()

    async def _bg_flush_loop(self) -> None:
        """Background coroutine: flush when idle for ``IDLE_FLUSH_AFTER`` seconds.

        Polls every ``IDLE_CHECK_INTERVAL`` seconds.  Exits cleanly when
        cancelled (i.e. on ``close()``) or when the logger is sealed.
        """
        try:
            while not self._closed:
                await asyncio.sleep(IDLE_CHECK_INTERVAL)
                if self._closed or not self._pending_count:
                    continue
                if self._last_updated_at is None:
                    continue
                idle_secs = time.monotonic() - self._last_updated_at
                if idle_secs >= IDLE_FLUSH_AFTER:
                    await self._safe_flush()
        except asyncio.CancelledError:
            pass  # normal shutdown path

    async def _safe_flush(self) -> None:
        """Write all accumulated lines to GCS (overwrites), swallowing exceptions."""
        if not _GCS_AVAILABLE or not self._pending_count:
            return
        snapshot = self._all_lines[:]  # immutable snapshot for the thread
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, partial(self._write_blob, snapshot))
            self._pending_count = 0
        except Exception as exc:  # noqa: BLE001
            logger.error("GCSLogger: flush to %s failed", self.gcs_uri, exc_info=exc)
            # Don't clear _pending_count — we'll retry on the next flush.

    def _write_blob(self, lines: list[str]) -> None:
        """Sync GCS upload — runs inside a thread-pool executor.

        Each call overwrites the blob with the *complete* session content
        so far — no GCS-level append needed, no race conditions.
        """
        client = _gcs_client_factory.get_client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(self.blob_path)
        content = "\n".join(lines) + "\n"
        blob.upload_from_string(content, content_type="application/jsonl")


# ---------------------------------------------------------------------------
# GCS client factory (lazy token refresh on access)
# ---------------------------------------------------------------------------

class GCSClientFactory:
    """Thread-safe GCS client factory backed by ``gcloud auth print-access-token``.

    Callers must **never cache** the returned client — always call
    ``get_client()`` to ensure a fresh token.  Internally the factory
    caches the client and only re-creates it when the token is older
    than ``TOKEN_TTL_SECONDS``.
    """

    def __init__(self, ttl_seconds: int = TOKEN_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._client: _gcs.Client | None = None
        self._fetched_at: float = 0.0  # monotonic timestamp of last fetch
        self._lock = threading.Lock()

    def get_client(self) -> _gcs.Client:
        """Return a GCS client with a valid access token.

        Thread-safe.  Re-fetches the token and builds a new client
        when the current one is older than ``ttl_seconds``.
        """
        with self._lock:
            now = time.monotonic()
            if self._client is None or (now - self._fetched_at) >= self._ttl:
                self._client = self._build_client()
                self._fetched_at = now
            return self._client

    @staticmethod
    def _fetch_token() -> str:
        """Shell out to ``gcloud auth print-access-token``."""
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"],
            encoding="utf-8",
            timeout=10,
        ).strip()
        if not tok:
            raise RuntimeError("gcloud auth print-access-token returned empty")
        logger.debug("Fetched fresh gcloud access token (%d chars)", len(tok))
        return tok

    @classmethod
    def _build_client(cls) -> _gcs.Client:
        """Create a new GCS client with a fresh access token."""
        credentials = _TokenCredentials(cls._fetch_token())
        return _gcs.Client(credentials=credentials)


# Module-level singleton — lazy, no work until first get_client() call.
_gcs_client_factory = GCSClientFactory()
