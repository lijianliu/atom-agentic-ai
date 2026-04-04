"""
turn_logger.py — Turn-by-Turn REPL Conversation Logging
========================================================
Logs every LLM output (thinking, text, tool plan, tool exec) in 
human-readable MIME multipart format.

See docs/logging-v2.md for the design document.

File naming:
    {session_dir}/t{T}.{S}.{type}.{label}.txt

Where:
    T = turn number (model request #), 3 chars, padded with '_'
    S = sequence within turn, 3 chars, padded with '_'
    type = thinking | text | plan | exec
    label = 50-char description (alphanumeric only, others become '_')

Usage:
    logger = TurnLogger.create(session_id)
    logger.start_turn()
    logger.log_thinking(content)
    logger.log_text(content)
    logger.log_tool_plan(tool_name, args, call_id)
    logger.log_tool_exec(tool_name, args, call_id, result)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_config import LOG_DIR, get_logger

logger = get_logger(__name__)


def _utcnow() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _generate_boundary() -> str:
    """Generate unique MIME boundary using full UUID."""
    return f"----=_Part_{uuid.uuid4().hex}"


def _format_args(args: Any) -> str:
    """Format tool arguments for logging."""
    if args is None:
        return ""
    if isinstance(args, dict):
        lines = []
        for k, v in args.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)
    return str(args)


def _sanitize_label(text: str, max_len: int = 50) -> str:
    """Sanitize text for use in filename.
    
    - Only allows 0-9, a-z, A-Z
    - Replaces other characters with '_'
    - Collapses multiple underscores
    - Strips leading/trailing underscores
    - Truncates to max_len (default 50)
    """
    if not text:
        return ""
    result = []
    for c in text:
        if c.isalnum():
            result.append(c)
        else:
            result.append("_")
    sanitized = "".join(result)
    # Collapse multiple underscores
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    # Strip and truncate
    return sanitized.strip("_")[:max_len]


class TurnLogger:
    """Turn-by-Turn conversation logger with MIME multipart format.
    
    Creates human-readable log files for each piece of LLM output,
    organized by turn and sequence number.
    """
    
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self._turn: int = 0
        self._sequences: dict[int, int] = {}  # turn -> last sequence
        
    @classmethod
    def create(cls, session_id: str) -> "TurnLogger":
        """Create a TurnLogger for the given session ID.
        
        Creates the session directory under LOG_DIR.
        """
        session_dir = LOG_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        logger.info("TurnLogger created: %s", session_dir)
        return cls(session_dir)
    
    def start_turn(self) -> None:
        """Start a new turn. Increments turn counter."""
        self._turn += 1
        self._sequences[self._turn] = 0
        logger.debug("Turn %d started", self._turn)
    
    def _next_sequence(self, turn: int | None = None) -> int:
        """Get next sequence number for given turn (default: current)."""
        t = turn if turn is not None else self._turn
        self._sequences.setdefault(t, 0)
        self._sequences[t] += 1
        return self._sequences[t]
    
    def _write_file(
        self,
        log_type: str,
        headers: dict[str, str],
        parts: list[tuple[str, str]],  # [(content_type, body), ...]
        label: str = "",
        override_turn: int | None = None,
    ) -> Path:
        """Write a MIME multipart-style log file.
        
        Args:
            log_type: File type suffix (thinking, text, plan, exec)
            headers: Key-value pairs for the header block
            parts: List of (content_type, body) tuples
            label: 30-char description for filename (auto-sanitized)
            
        Returns:
            Path to the created file
        """
        turn = override_turn if override_turn is not None else self._turn
        seq = self._next_sequence(turn)
        turn_str = f"{turn:3d}".replace(" ", "_")
        seq_str = f"{seq:3d}".replace(" ", "_")
        
        # Build filename with label
        label_part = _sanitize_label(label)
        if label_part:
            filename = f"t{turn_str}.{seq_str}.{log_type}.{label_part}.txt"
        else:
            filename = f"t{turn_str}.{seq_str}.{log_type}.txt"
        filepath = self.session_dir / filename
        
        # Ensure directory exists (defensive — create() should have done this)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        
        boundary = _generate_boundary()
        
        lines = [f"Boundary: {boundary}"]
        lines.append(f"Timestamp: {_utcnow()}")
        lines.append(f"Turn: {turn}")
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
        lines.append("")  # Blank line after headers
        
        for content_type, body in parts:
            lines.append(boundary)
            lines.append(f"Content-Type: {content_type}")
            lines.append("")
            lines.append(body)
            lines.append("")
        
        lines.append(f"{boundary}--")
        
        content = "\n".join(lines)
        filepath.write_text(content, encoding="utf-8")
        logger.debug("Wrote %s", filepath)
        return filepath
    
    def log_thinking(self, content: str, label: str = "") -> Path:
        """Log extended thinking content."""
        if not label and content:
            label = content[:50]  # Auto-generate from content
        return self._write_file(
            log_type="thinking",
            headers={},
            parts=[("thinking", content)],
            label=label,
        )
    
    def log_text(self, content: str, label: str = "") -> Path:
        """Log LLM text response."""
        if not label and content:
            label = content[:50]  # Auto-generate from content
        return self._write_file(
            log_type="text",
            headers={},
            parts=[("text", content)],
            label=label,
        )
    
    def log_tool_plan(
        self,
        tool_name: str,
        args: Any,
        call_id: str | None = None,
        label: str = "",
    ) -> Path:
        """Log tool call plan (what LLM wants to call)."""
        headers = {"Tool": tool_name}
        if call_id:
            headers["Call-ID"] = call_id
        
        if not label:
            # Auto-generate: tool_name + args preview
            args_preview = _format_args(args).replace("\n", " ")[:100]
            label = f"{tool_name}.{args_preview}"
        
        return self._write_file(
            log_type="plan",
            headers=headers,
            parts=[("input", _format_args(args))],
            label=label,
        )
    
    def log_tool_exec(
        self,
        tool_name: str,
        args: Any,
        call_id: str | None,
        result: Any,
        error: str | None = None,
        label: str = "",
        override_turn: int | None = None,
    ) -> Path:
        """Log tool execution (input + output/error).
        
        Args:
            override_turn: If set, log to this turn instead of current.
                          Used to attribute tool results to the requesting turn.
        """
        headers = {"Tool": tool_name}
        if call_id:
            headers["Call-ID"] = call_id
        
        parts = [("input", _format_args(args))]
        
        if error:
            parts.append(("error", str(error)))
        else:
            # Format result nicely
            if isinstance(result, dict):
                result_str = _format_args(result)
            else:
                result_str = str(result) if result is not None else ""
            parts.append(("output", result_str))
        
        if not label:
            # Auto-generate: tool_name + args preview
            args_preview = _format_args(args).replace("\n", " ")[:100]
            label = f"{tool_name}.{args_preview}"
        
        return self._write_file(
            log_type="exec",
            headers=headers,
            parts=parts,
            label=label,
            override_turn=override_turn,
        )
    
    @property
    def previous_turn(self) -> int:
        """Previous turn number (for attributing tool results)."""
        return max(self._turn - 1, 1)
    
    @property
    def current_turn(self) -> int:
        """Current turn number."""
        return self._turn
    
    @property
    def current_sequence(self) -> int:
        """Current sequence number within turn."""
        return self._sequence
