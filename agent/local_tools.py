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

Host-side tool (available in both root mode and sandbox mode):
  - upload_output_file
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent

from logging_config import get_logger

if TYPE_CHECKING:
    from pydantic_ai import RunContext

logger = get_logger(__name__)

# DEBUG: log module identity at import time
logger.warning(
    "DEBUG_IMPORT local_tools loaded: __name__=%r, module_id=%d, file=%s",
    __name__, id(sys.modules.get(__name__)), __file__,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Default to CWD; can be overridden via env var
SAFE_ROOT = Path(os.environ.get("LOCAL_TOOLS_ROOT", os.getcwd())).resolve()
COMMAND_TIMEOUT = int(os.environ.get("LOCAL_CMD_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _safe_path(path: str, root: Path = SAFE_ROOT) -> Path:
    """Resolve *path* and raise ValueError if it escapes *root*."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and not str(resolved).startswith(str(root) + os.sep):
        raise ValueError(
            f"Access denied — path must be within {root}. Got: {resolved}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Tool registration helpers
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


def register_upload_tool(agent: Agent, workspace: Path | None = None) -> None:
    """Register the upload_output_file tool on the given agent.

    This tool runs on the HOST (not inside the sandbox) and uploads files
    to GCS using the active GCSLogger session.

    Parameters
    ----------
    agent:
        The pydantic-ai Agent to register the tool on.
    workspace:
        Root directory for resolving file paths.  Defaults to SAFE_ROOT
        (CWD in root mode, /workspace in sandbox mode).
    """
    upload_root = (workspace or SAFE_ROOT).resolve()

    @agent.tool_plain
    def upload_output_file(path: str, destination_filename: str | None = None) -> str:
        """Upload a file you created to cloud storage for delivery.

        WHEN TO USE: Call this tool after you have finished creating or
        generating an output file (e.g., a report, CSV, analysis, chart,
        processed data, or any other deliverable) and you want to make it
        available to the user via cloud storage.

        The file is uploaded to the current session folder in Google Cloud
        Storage (configured by ATOM_AUDIT_LOG_GCS_PATH).

        Args:
            path:                 Path to the local file to upload (relative
                                  to the working directory or absolute).
            destination_filename: Optional override for the filename in cloud
                                  storage.  Defaults to the original filename.

        Returns:
            The full GCS path (gs:// URI) and web URL of the uploaded file,
            or an error message.
        """
        try:
            resolved = _safe_path(path, root=upload_root)
        except ValueError as exc:
            logger.warning("upload_output_file: path rejected: %s", exc)
            return f"Error: {exc}"
        if not resolved.exists():
            logger.warning("upload_output_file: file not found: %s", resolved)
            return f"Error: file not found: {resolved}"
        if not resolved.is_file():
            logger.warning("upload_output_file: not a file: %s", resolved)
            return f"Error: not a file: {resolved}"

        size = resolved.stat().st_size
        logger.info(
            "upload_output_file: starting upload of %s (%d bytes, dest=%s)",
            resolved, size, destination_filename or resolved.name,
        )

        try:
            # DEBUG: log which module we are importing from and sys.path state
            import gcs_audit_logger as _gcs_mod_direct
            logger.warning(
                "DEBUG_UPLOAD import check: 'import gcs_audit_logger' resolved to "
                "module_id=%d, file=%s, __name__=%r, sys.path=%s",
                id(_gcs_mod_direct), getattr(_gcs_mod_direct, '__file__', '?'),
                _gcs_mod_direct.__name__,
                sys.path,
            )

            # Also check if agent.gcs_audit_logger exists as a separate module
            agent_gcs_mod = sys.modules.get("agent.gcs_audit_logger")
            bare_gcs_mod = sys.modules.get("gcs_audit_logger")
            logger.warning(
                "DEBUG_UPLOAD sys.modules check: "
                "agent.gcs_audit_logger=%s (id=%d), "
                "gcs_audit_logger=%s (id=%d), "
                "SAME=%s",
                "present" if agent_gcs_mod else "MISSING",
                id(agent_gcs_mod) if agent_gcs_mod else 0,
                "present" if bare_gcs_mod else "MISSING",
                id(bare_gcs_mod) if bare_gcs_mod else 0,
                agent_gcs_mod is bare_gcs_mod if (agent_gcs_mod and bare_gcs_mod) else "N/A",
            )

            from gcs_audit_logger import get_active_gcs_logger, gcs_uri_to_web_url

            gcs_logger = get_active_gcs_logger()
            if gcs_logger is None:
                logger.error("upload_output_file: no active GCS logger (ATOM_AUDIT_LOG_GCS_PATH not set?)")
                return (
                    "Error (configuration): GCS logging is not configured. "
                    "Set ATOM_AUDIT_LOG_GCS_PATH to enable output file uploads."
                )

            gcs_uri = gcs_logger.upload_output_file(
                local_path=resolved,
                destination_filename=destination_filename,
            )
            web_url = gcs_uri_to_web_url(gcs_uri)
            logger.info(
                "upload_output_file: success — %s (%d bytes) → %s (web: %s)",
                resolved.name, size, gcs_uri, web_url or "N/A",
            )
            result = gcs_uri
            if web_url:
                result += f"\n{web_url}"
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "upload_output_file: upload failed for %s: %s",
                resolved.name, exc, exc_info=True,
            )
            return f"Error uploading file: {exc}"
