"""
gcs_audit_logger.py — Async-friendly GCS activity logger for Atom Agent sessions.
=============================================================================
Buffers events in-memory as JSONL records per turn.  Each turn is flushed
to its own GCS blob when ``flush_turn()`` is called.

GCS blob path:
    {prefix}/{date}/{username}/{time}/{username}_{session_ts}-{turn}-{prompt_slug}.jsonl

    Example:
    logs/2026-03-29/jdoe/19-14-11.422Z/jdoe-2026-03-29T19-14-11.422Z-001-what_is_the_meaning_of_life.jsonl

Session ID = ``{username}-{session_ts}`` (stable for the whole ``./run.sh``
lifetime — no UUID needed).

Usage:
    logger = GCSLogger.from_env()
    if logger:
        logger.start_turn("tell me about cats")      # begin turn 1
        await logger.log("user_prompt", {...})
        await logger.log("tool_call", {...})
        await logger.flush_turn()                    # write turn 1 blob
        await logger.close()                         # session_end + final flush

All public methods are fire-and-forget: exceptions are caught and printed
as warnings — they NEVER propagate to the caller.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import subprocess
import threading
import time
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

TOKEN_TTL_SECONDS = 25 * 60   # re-fetch gcloud token after 25 minutes
ENV_PATH = "ATOM_AUDIT_LOG_GCS_PATH"
PROMPT_SLUG_MAX = 50          # max chars in the prompt portion of the filename

FETCH_TOKEN_TIMEOUT = 60      # seconds to wait for gcloud auth command
FETCH_TOKEN_MAX_RETRIES = 3   # number of attempts before giving up
FETCH_TOKEN_RETRY_BACKOFF = 2 # base seconds for exponential backoff

# Content-type mapping for uploaded turn-log files
_CONTENT_TYPE_MAP = {
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
}
_DEFAULT_CONTENT_TYPE = "text/plain"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _session_timestamp() -> str:
    """UTC timestamp for session IDs / filenames.

    Format: ``2026-03-29T19-14-11.422Z``

    Colons are replaced with hyphens (filesystem-safe); everything
    else uses hyphens for consistency.
    """
    now = datetime.now(timezone.utc)
    base = now.strftime("%Y-%m-%dT%H-%M-%S")
    millis = f"{now.microsecond // 1000:03d}"
    return f"{base}.{millis}Z"


def _resolve_username() -> str:
    """Best-effort username from the running environment.

    Resolution order:
      1. ``USER`` env var  (Unix/macOS)
      2. ``LOGNAME`` env var  (Unix/macOS)
      3. ``USERNAME`` env var  (Windows)
      4. ``getpass.getuser()`` fallback
      5. ``"unknown"``
    """
    for var in ("USER", "LOGNAME", "USERNAME"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def _slugify_prompt(text: str, max_len: int = PROMPT_SLUG_MAX) -> str:
    """Turn a user prompt into a filename-safe slug.

    Rules:
      • Keep only ``[a-zA-Z0-9]``.
      • Every other character (space, punctuation, unicode) → ``_``.
      • Collapse consecutive ``_`` into one.
      • Strip leading/trailing ``_``.
      • Truncate to *max_len* characters.
      • If nothing survives, return ``"prompt"``.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "_", text)
    slug = re.sub(r"_+", "_", slug).strip("_")
    slug = slug[:max_len].rstrip("_")
    return slug or "prompt"


def _content_type_for_path(filepath: Any) -> str:
    """Return the Content-Type for a file based on its suffix."""
    try:
        suffix = filepath.suffix.lower()
    except AttributeError:
        suffix = ""
    return _CONTENT_TYPE_MAP.get(suffix, _DEFAULT_CONTENT_TYPE)


# ---------------------------------------------------------------------------
# Null logger (no-op stand-in when GCS is not configured)
# ---------------------------------------------------------------------------

class NullGCSLogger:
    """No-op logger used when GCS is not configured."""

    def upload_turn_log(self, filepath, content):
        """No-op upload — always returns ``None``."""
        return None


# ---------------------------------------------------------------------------
# GCSLogger
# ---------------------------------------------------------------------------

class GCSLogger:
    """Non-blocking, per-turn JSONL GCS activity logger.

    Each user prompt → agent response cycle ("turn") accumulates events
    in an in-memory buffer.  When ``flush_turn()`` is called the buffer
    is written to a uniquely-named GCS blob and then cleared for the
    next turn.

    Parameters
    ----------
    bucket_name:
        GCS bucket name (without ``gs://`` prefix).
    prefix:
        Path prefix inside the bucket (default: ``""``).
    username:
        Username for the blob path.  Auto-resolved from env if not given.
    """

    def __init__(
        self,
        bucket_name: str,
        prefix: str = "",
        username: str | None = None,
    ) -> None:
        self.bucket_name = bucket_name
        self.prefix = prefix.rstrip("/")
        self.username: str = username or _resolve_username()

        # Session ID = "{username}-{ts}" — stable for the entire run.sh session
        self._session_ts: str = _session_timestamp()
        self.session_id: str = f"{self.username}-{self._session_ts}"

        self._started_at: str = _utcnow()
        self._closed: bool = False

        # Per-turn state
        self._turn: int = 0                      # current turn number (1-based after start_turn)
        self._turn_slug: str = ""                 # slugified prompt for the filename
        self._turn_lines: list[str] = []          # JSONL lines for the current turn

        if not _GCS_AVAILABLE:
            logger.warning(
                "GCSLogger: 'google-cloud-storage' not installed — "
                "logging disabled. Fix with:  uv add google-cloud-storage"
            )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "GCSLogger | None":
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

        return cls(bucket_name=bucket_name, prefix=prefix)

    # ------------------------------------------------------------------
    # Public API  (all fire-and-forget — never raise)
    # ------------------------------------------------------------------

    def start_turn(self, prompt: str = "") -> None:
        """Begin a new turn.  Clears the buffer and captures a prompt slug.

        Parameters
        ----------
        prompt:
            The raw user prompt.  Slugified to ``[a-zA-Z0-9_]`` (max 50
            chars) for use in the GCS blob filename.
        """
        self._turn += 1
        self._turn_slug = _slugify_prompt(prompt)
        self._turn_lines = []
        logger.debug(
            "GCS turn %d started (slug=%s)", self._turn, self._turn_slug,
        )

    async def log(self, event: str, data: dict[str, Any]) -> None:
        """Append a structured event to the current turn's buffer."""
        if not _GCS_AVAILABLE or self._closed:
            return
        record: dict[str, Any] = {
            "ts": _utcnow(),
            "session_id": self.session_id,
            "username": self.username,
            "turn": self._turn,
            "event": event,
        }
        record.update(data)
        self._turn_lines.append(json.dumps(record, ensure_ascii=False))

    async def flush_turn(self) -> str | None:
        """Write the current turn's buffered events to a GCS blob.

        The blob path includes username, session timestamp, turn number,
        and a prompt slug so every turn lands in its own human-readable
        file.  After a successful write the buffer is cleared.

        Returns the full ``gs://`` URI on success, or ``None`` on skip/failure.
        """
        if not _GCS_AVAILABLE or not self._turn_lines:
            return None
        blob_path = self._blob_path_for_turn(self._turn, self._turn_slug)
        gcs_uri = f"gs://{self.bucket_name}/{blob_path}"
        snapshot = self._turn_lines[:]
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, partial(self._write_blob, blob_path, snapshot),
            )
            logger.info(
                "GCS turn %d flushed → %s (%d events)",
                self._turn, gcs_uri, len(snapshot),
            )
            self._turn_lines = []
            return gcs_uri
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "GCSLogger: flush turn %d to %s failed",
                self._turn, gcs_uri, exc_info=exc,
            )
            return None

    async def warm_token(self) -> None:
        """Pre-fetch the gcloud access token in a background thread.

        Call this at session start so the exit flush doesn't pay the
        ~1-2 s subprocess cost of ``gcloud auth print-access-token``.
        """
        if not _GCS_AVAILABLE:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _gcs_client_factory.get_client)
            logger.debug("GCS token pre-warmed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("GCSLogger: token pre-warm failed: %s", exc)

    async def close(self, extra: dict[str, Any] | None = None) -> str | None:
        """Write a ``999-EXIT`` sentinel blob with session usage, then seal.

        Returns the ``gs://`` URI of the sentinel blob, or ``None``.
        """
        if self._closed:
            return None
        self._turn_lines = []  # fresh buffer for the sentinel
        data: dict[str, Any] = {"started_at": self._started_at}
        if extra:
            data.update(extra)
        await self.log("session_end", data)

        # Write as turn 999 with slug "EXIT"
        blob_path = self._blob_path_for_turn(999, "EXIT")
        snapshot = self._turn_lines[:]
        self._closed = True
        if not _GCS_AVAILABLE or not snapshot:
            return None
        gcs_uri = f"gs://{self.bucket_name}/{blob_path}"
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, partial(self._write_blob, blob_path, snapshot),
            )
            logger.info("GCS session-end flushed → %s", gcs_uri)
            self._turn_lines = []
            return gcs_uri
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "GCSLogger: session-end flush to %s failed",
                gcs_uri, exc_info=exc,
            )
            return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def gcs_uri(self) -> str:
        """Representative GCS URI (uses current turn info)."""
        slug = self._turn_slug or "prompt"
        blob = self._blob_path_for_turn(self._turn or 1, slug)
        return f"gs://{self.bucket_name}/{blob}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _blob_path_for_turn(self, turn: int, slug: str) -> str:
        """Build the blob path for a specific turn.

        Format:
            {prefix}/{date}/{username}/{time}/{username}_{session_ts}-{turn}-{slug}.jsonl

        Example:
            logs/2026-03-29/jdoe/19-14-11.422Z/jdoe-2026-03-29T19-14-11.422Z-001-what_is_the_meaning_of_life.jsonl
        """
        # session_ts = "2026-03-29T19-14-11.422Z"
        date_part, time_part = self._session_ts.split("T", 1)
        date_folder = date_part                             # 2026-03-29

        filename = (
            f"{self.username}-{self._session_ts}-{turn:03d}-{slug}.jsonl"
        )
        parts = [date_folder, self.username, time_part, filename]
        if self.prefix:
            parts.insert(0, self.prefix)
        return "/".join(parts)

    def upload_turn_log(self, filepath: "Path", content: str) -> str | None:
        """Upload a turn log file to GCS (fire-and-forget).

        Mirrors the local turn-log directory structure into the same
        GCS prefix used by this logger.  Content-Type is auto-detected
        from the file extension (``.html`` → ``text/html``, etc.).

        Returns the ``gs://`` URI on success, or ``None`` on skip/failure.
        """
        if not _GCS_AVAILABLE:
            return None
        try:
            from logging_config import LOG_DIR
            relative = filepath.relative_to(LOG_DIR)
            if self.prefix:
                blob_path = f"{self.prefix}/{relative}"
            else:
                blob_path = str(relative)
            content_type = _content_type_for_path(filepath)
            client = _gcs_client_factory.get_client()
            bucket = client.bucket(self.bucket_name)
            blob = bucket.blob(blob_path)
            blob.upload_from_string(content, content_type=content_type)
            gcs_uri = f"gs://{self.bucket_name}/{blob_path}"
            logger.debug("GCS turn-log uploaded %s (%s)", gcs_uri, content_type)
            return gcs_uri
        except Exception as exc:  # noqa: BLE001
            logger.warning("GCS turn-log upload failed for %s: %s", filepath.name, exc)
            return None

    def _write_blob(self, blob_path: str, lines: list[str]) -> None:
        """Sync GCS upload — runs inside a thread-pool executor."""
        client = _gcs_client_factory.get_client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(blob_path)
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
        self._refresh_count: int = 0   # total number of token refreshes

    def get_client(self) -> _gcs.Client:
        """Return a GCS client with a valid access token.

        Thread-safe.  Re-fetches the token and builds a new client
        when the current one is older than ``ttl_seconds``.
        """
        with self._lock:
            now = time.monotonic()
            age = now - self._fetched_at
            if self._client is None or age >= self._ttl:
                reason = "first call" if self._client is None else f"token expired (age={age:.1f}s, ttl={self._ttl}s)"
                logger.info(
                    "GCS_AUTH get_client → REFRESH needed (%s), "
                    "thread=%s, refresh_count=%d",
                    reason, threading.current_thread().name, self._refresh_count,
                )
                t0 = time.monotonic()
                self._client = self._build_client()
                elapsed = time.monotonic() - t0
                self._fetched_at = now          # original: TTL counts from before the build
                self._refresh_count += 1
                logger.info(
                    "GCS_AUTH get_client → REFRESH done in %.3fs, "
                    "thread=%s, total_refreshes=%d",
                    elapsed, threading.current_thread().name, self._refresh_count,
                )
            else:
                logger.debug(
                    "GCS_AUTH get_client → CACHE HIT (age=%.1fs / ttl=%ds), "
                    "thread=%s",
                    age, self._ttl, threading.current_thread().name,
                )
            return self._client

    @staticmethod
    def _fetch_token() -> str:
        """Shell out to ``gcloud auth print-access-token``.

        Retries up to ``FETCH_TOKEN_MAX_RETRIES`` times with exponential
        backoff on ``TimeoutExpired`` or ``CalledProcessError``.
        """
        last_exc: Exception | None = None
        for attempt in range(1, FETCH_TOKEN_MAX_RETRIES + 1):
            try:
                logger.info(
                    "GCS_AUTH _fetch_token → START (attempt %d/%d), thread=%s",
                    attempt, FETCH_TOKEN_MAX_RETRIES, threading.current_thread().name,
                )
                t0 = time.monotonic()
                tok = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"],
                    encoding="utf-8",
                    timeout=FETCH_TOKEN_TIMEOUT,
                ).strip()
                elapsed = time.monotonic() - t0
                if not tok:
                    raise RuntimeError("gcloud auth print-access-token returned empty")
                logger.info(
                    "GCS_AUTH _fetch_token → END OK in %.3fs (%d chars), thread=%s",
                    elapsed, len(tok), threading.current_thread().name,
                )
                return tok
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                elapsed = time.monotonic() - t0
                last_exc = exc
                logger.warning(
                    "GCS_AUTH _fetch_token → FAILED attempt %d/%d in %.3fs (%s), thread=%s",
                    attempt, FETCH_TOKEN_MAX_RETRIES, elapsed, exc,
                    threading.current_thread().name,
                )
                if attempt < FETCH_TOKEN_MAX_RETRIES:
                    backoff = FETCH_TOKEN_RETRY_BACKOFF ** attempt
                    logger.warning(
                        "GCSLogger: retrying in %ds ...", backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        "GCS_AUTH _fetch_token → GAVE UP after %d attempts",
                        FETCH_TOKEN_MAX_RETRIES,
                    )
        raise last_exc  # type: ignore[misc]

    @classmethod
    def _build_client(cls) -> _gcs.Client:
        """Create a new GCS client with a fresh access token."""
        credentials = _TokenCredentials(cls._fetch_token())
        return _gcs.Client(credentials=credentials)


# Module-level singleton — lazy, no work until first get_client() call.
_gcs_client_factory = GCSClientFactory()
