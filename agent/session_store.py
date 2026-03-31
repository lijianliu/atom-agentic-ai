"""Session persistence — save / load message history + usage to disk.

Uses pydantic-ai’s built-in ``ModelMessagesTypeAdapter`` for
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
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

logger = logging.getLogger(__name__)

_SESSIONS_DIR = Path.home() / ".config" / "atom-agentic-ai" / "sessions"


def default_session_path() -> Path:
    """Generate a timestamped session file path.

    e.g. ~/.config/atom-agentic-ai/sessions/2026-03-29_11-08-00.session.json
    """
    from datetime import datetime, timezone

    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    return _SESSIONS_DIR / f"{ts}.session.json"


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
        "Session saved to %s (%d messages, %d turns)",
        path, len(message_history), session_usage.get("turns", 0),
    )


def load_session(
    path: Path,
) -> tuple[list, dict[str, Any]]:
    """Load message history + usage from a JSON file.

    Returns:
        (message_history, session_usage) — both empty/fresh if file
        doesn’t exist or is corrupted.
    """
    from usage_helpers import new_session_usage

    if not path.exists():
        return [], new_session_usage()

    try:
        raw = path.read_text(encoding="utf-8")
        envelope = json.loads(raw)

        # Deserialise messages via pydantic-ai’s type adapter
        messages_raw = json.dumps(envelope["messages"]).encode()
        history = list(ModelMessagesTypeAdapter.validate_json(messages_raw))

        # Restore usage (merge with defaults so new keys are covered)
        usage = new_session_usage()
        usage.update(envelope.get("usage", {}))

        logger.info(
            "Session loaded from %s (%d messages, %d turns)",
            path, len(history), usage.get("turns", 0),
        )
        return history, usage

    except Exception as exc:
        logger.warning(
            "Could not load session from %s: %s — starting fresh", path, exc,
        )
        return [], new_session_usage()
