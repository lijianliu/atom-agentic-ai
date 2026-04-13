"""Session persistence — save / load message history + usage to disk.

Uses pydantic-ai's built-in ``ModelMessagesTypeAdapter`` for
type-safe (de)serialisation of the full conversation history.

File format (JSON):
    {
        "messages": [ ... pydantic-ai message objects ... ],
        "usage": { ... session usage accumulator ... }
    }

Usage:
    from session_store import save_session, load_session

    save_session(message_history, session_usage, Path("my-session.json"))
    message_history, session_usage = load_session(Path("my-session.json"))
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, ThinkingPart

from logging_config import LOG_DIR

logger = logging.getLogger(__name__)

_SESSIONS_DIR = LOG_DIR / "sessions"


def default_session_path(session_id: str | None = None) -> Path:
    """Generate a new session file path using the given *session_id*.

    If *session_id* is provided the file is named ``<session_id>.session.json``
    so that the session file matches the turn-log folder and GCS session id.

    Always creates a fresh session.  To resume a previous session,
    pass ``--session <file>`` explicitly on the command line.
    """
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if session_id is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        name = ts
    else:
        name = session_id
    return _SESSIONS_DIR / f"{name}.session.json"


def _strip_thinking_parts(history: list) -> int:
    """Remove ThinkingPart blocks from ModelResponse messages in history.

    Thinking blocks carry a ``signature`` that the Anthropic API validates
    on subsequent requests.  When a session is serialised to disk and
    resumed later, these signatures become stale/invalid, causing:

        Invalid `signature` in `thinking` block

    Thinking parts are only for transparency — they are NOT required for
    the model to continue the conversation, so stripping them is safe.

    Returns the number of thinking parts removed.
    """
    removed = 0
    for msg in history:
        if isinstance(msg, ModelResponse):
            original_len = len(msg.parts)
            msg.parts = [p for p in msg.parts if not isinstance(p, ThinkingPart)]
            removed += original_len - len(msg.parts)
    return removed


def save_session(
    message_history: list,
    session_usage: dict[str, Any],
    path: Path,
) -> None:
    """Persist message history + usage to a JSON file.

    Writes atomically via a temp file to prevent corruption
    if the process is killed mid-write.
    """
    messages_json = json.loads(
        ModelMessagesTypeAdapter.dump_json(message_history)
    )
    envelope = {
        "messages": messages_json,
        "usage": session_usage,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, indent=2, ensure_ascii=False))
    tmp.replace(path)  # atomic rename
    logger.info(
        "Session saved to %s (%d messages, %d queries)",
        path, len(message_history), session_usage.get("queries", 0),
    )


def load_session(
    path: Path,
) -> tuple[list, dict[str, Any]]:
    """Load message history + usage from a JSON file.

    Returns:
        (message_history, session_usage) — both empty/fresh if file
        doesn't exist or is corrupted.
    """
    from usage_helpers import new_session_usage

    if not path.exists():
        return [], new_session_usage()

    try:
        raw = path.read_text(encoding="utf-8")
        envelope = json.loads(raw)

        # Deserialise messages via pydantic-ai's type adapter
        messages_raw = json.dumps(envelope["messages"]).encode()
        history = list(ModelMessagesTypeAdapter.validate_json(messages_raw))

        # Strip thinking parts to avoid "Invalid signature in thinking
        # block" errors when the session is resumed.  Signatures are
        # only valid for the request that produced them.
        stripped = _strip_thinking_parts(history)
        if stripped:
            logger.info(
                "Stripped %d stale thinking parts from loaded session", stripped
            )

        # Restore usage (merge with defaults so new keys are covered)
        usage = new_session_usage()
        saved_usage = envelope.get("usage", {})
        usage.update(saved_usage)

        logger.info(
            "Session loaded from %s (%d messages, %d queries)",
            path, len(history), usage.get("queries", 0),
        )
        return history, usage

    except Exception as exc:
        logger.warning(
            "Could not load session from %s: %s — starting fresh", path, exc,
        )
        return [], new_session_usage()
