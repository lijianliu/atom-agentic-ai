"""
turn_logger.py — Turn-by-Turn REPL Conversation Logging
========================================================
Logs every LLM output (thinking, text, tool plan, tool exec) in 
human-readable MIME multipart format.

See docs/logging-v2.md for the design document.

File naming:
    {session_dir}/turn{T}.seq{S}.{type}.txt

Where:
    T = model request number (matches Usage #N, 3 chars, padded with '_')
    S = sequence within request (1-based, 3 chars, padded with '_')
    type = thinking | text | tool-plan | tool-exec

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


class TurnLogger:
    """Turn-by-Turn conversation logger with MIME multipart format.
    
    Creates human-readable log files for each piece of LLM output,
    organized by turn and sequence number.
    """
    
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self._turn: int = 0
        self._sequence: int = 0
        
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
        """Start a new turn. Increments turn counter, resets sequence."""
        self._turn += 1
        self._sequence = 0
        logger.debug("Turn %d started", self._turn)
    
    def _next_sequence(self) -> int:
        """Get next sequence number for current turn."""
        self._sequence += 1
        return self._sequence
    
    def _write_file(
        self,
        log_type: str,
        headers: dict[str, str],
        parts: list[tuple[str, str]],  # [(content_type, body), ...]
    ) -> Path:
        """Write a MIME multipart-style log file.
        
        Args:
            log_type: File type suffix (thinking, text, tool-plan, tool-exec)
            headers: Key-value pairs for the header block
            parts: List of (content_type, body) tuples
            
        Returns:
            Path to the created file
        """
        seq = self._next_sequence()
        turn_str = f"{self._turn:3d}".replace(" ", "_")
        seq_str = f"{seq:3d}".replace(" ", "_")
        filename = f"turn{turn_str}.seq{seq_str}.{log_type}.txt"
        filepath = self.session_dir / filename
        
        boundary = _generate_boundary()
        
        lines = [f"Boundary: {boundary}"]
        lines.append(f"Timestamp: {_utcnow()}")
        lines.append(f"Turn: {self._turn}")
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
    
    def log_thinking(self, content: str) -> Path:
        """Log extended thinking content."""
        return self._write_file(
            log_type="thinking",
            headers={},
            parts=[("thinking", content)],
        )
    
    def log_text(self, content: str) -> Path:
        """Log LLM text response."""
        return self._write_file(
            log_type="text",
            headers={},
            parts=[("text", content)],
        )
    
    def log_tool_plan(
        self,
        tool_name: str,
        args: Any,
        call_id: str | None = None,
    ) -> Path:
        """Log tool call plan (what LLM wants to call)."""
        headers = {"Tool": tool_name}
        if call_id:
            headers["Call-ID"] = call_id
        
        return self._write_file(
            log_type="tool-plan",
            headers=headers,
            parts=[("input", _format_args(args))],
        )
    
    def log_tool_exec(
        self,
        tool_name: str,
        args: Any,
        call_id: str | None,
        result: Any,
        error: str | None = None,
    ) -> Path:
        """Log tool execution (input + output/error)."""
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
        
        return self._write_file(
            log_type="tool-exec",
            headers=headers,
            parts=parts,
        )
    
    @property
    def current_turn(self) -> int:
        """Current turn number."""
        return self._turn
    
    @property
    def current_sequence(self) -> int:
        """Current sequence number within turn."""
        return self._sequence
