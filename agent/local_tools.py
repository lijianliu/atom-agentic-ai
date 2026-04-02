"""
local_tools.py — Local file/exec tools that run directly on the host.
======================================================================
Used when running in "root" mode (--root flag), bypassing the MCP sandbox.
WARNING: These tools execute directly on your machine with no sandboxing!

Tools provided (match MCP sandbox API):
  - execute_command
  - read_file
  - write_file
  - append_file
  - delete_file
  - list_dir
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent

if TYPE_CHECKING:
    from pydantic_ai import RunContext

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Default to CWD; can be overridden via env var
SAFE_ROOT = Path(os.environ.get("LOCAL_TOOLS_ROOT", os.getcwd())).resolve()
COMMAND_TIMEOUT = int(os.environ.get("LOCAL_CMD_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _safe_path(path: str) -> Path:
    """Resolve *path* and raise ValueError if it escapes SAFE_ROOT."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = SAFE_ROOT / candidate
    resolved = candidate.resolve()
    if resolved != SAFE_ROOT and not str(resolved).startswith(str(SAFE_ROOT) + os.sep):
        raise ValueError(
            f"Access denied — path must be within {SAFE_ROOT}. Got: {resolved}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Tool registration helper
# ---------------------------------------------------------------------------

def register_local_tools(agent: Agent) -> None:
    """Register all local file/exec tools on the given agent."""

    @agent.tool_plain
    def execute_command(command: str, working_dir: str | None = None) -> str:
        """Execute a shell command on the host and return the result.

        Args:
            command:     Shell command string to run.
            working_dir: Working directory (defaults to current directory).
        """
        cwd = str(SAFE_ROOT)
        if working_dir:
            try:
                cwd = str(_safe_path(working_dir))
            except ValueError as exc:
                return f"Error: {exc}"
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                cwd=cwd,
            )
            lines = [
                f"Exit Code: {result.returncode}",
                f"STDOUT:\n{result.stdout}" if result.stdout else "STDOUT: (empty)",
            ]
            if result.stderr:
                lines.append(f"STDERR:\n{result.stderr}")
            return "\n".join(lines)
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {COMMAND_TIMEOUT}s."
        except Exception as exc:  # noqa: BLE001
            return f"Error executing command: {exc}"

    @agent.tool_plain
    def read_file(path: str) -> str:
        """Read the contents of a file."""
        try:
            resolved = _safe_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not resolved.exists():
            return f"Error: file not found: {resolved}"
        if not resolved.is_file():
            return f"Error: not a file: {resolved}"
        try:
            return resolved.read_text(errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"Error reading file: {exc}"

    @agent.tool_plain
    def write_file(path: str, content: str) -> str:
        """Write (overwrite) a file."""
        try:
            resolved = _safe_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content)
            return f"OK: wrote {len(content)} chars to {resolved}"
        except Exception as exc:  # noqa: BLE001
            return f"Error writing file: {exc}"

    @agent.tool_plain
    def append_file(path: str, content: str) -> str:
        """Append text to a file (creates it if missing)."""
        try:
            resolved = _safe_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with resolved.open("a") as fh:
                fh.write(content)
            return f"OK: appended {len(content)} chars to {resolved}"
        except Exception as exc:  # noqa: BLE001
            return f"Error appending to file: {exc}"

    @agent.tool_plain
    def delete_file(path: str) -> str:
        """Delete a file."""
        try:
            resolved = _safe_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not resolved.exists():
            return f"Error: file not found: {resolved}"
        if not resolved.is_file():
            return f"Error: {resolved} is a directory — use execute_command('rm -rf ...')"
        try:
            resolved.unlink()
            return f"OK: deleted {resolved}"
        except Exception as exc:  # noqa: BLE001
            return f"Error deleting file: {exc}"

    @agent.tool_plain
    def list_dir(path: str | None = None) -> str:
        """List files and directories at a path."""
        target = path or str(SAFE_ROOT)
        try:
            resolved = _safe_path(target)
        except ValueError as exc:
            return f"Error: {exc}"
        if not resolved.exists():
            return f"Error: path not found: {resolved}"
        if not resolved.is_dir():
            return f"Error: not a directory: {resolved}"
        try:
            entries = []
            for entry in sorted(resolved.iterdir()):
                kind = "[DIR] " if entry.is_dir() else "[FILE]"
                size = f"{entry.stat().st_size:>10} B" if entry.is_file() else " " * 12
                entries.append(f"{kind} {size}  {entry.name}")
            if not entries:
                return f"{resolved} is empty."
            return f"Contents of {resolved} ({len(entries)} entries):\n" + "\n".join(entries)
        except Exception as exc:  # noqa: BLE001
            return f"Error listing directory: {exc}"
