#!/usr/bin/env python3
"""
MCP Server for Hardened Docker Sandbox
======================================
Runs INSIDE the Docker container. Binds to a TCP port (default 9100)
published to localhost-only via sandbox.sh (-p 127.0.0.1:PORT:PORT).

Clients connect to: http://127.0.0.1:<PORT>/sse

Tools:
  execute_command  — run any shell command, return stdout/stderr/exit-code
  read_file        — read a file    (jailed to /workspace)
  write_file       — write a file   (jailed to /workspace)
  append_file      — append to file (jailed to /workspace)
  delete_file      — delete a file  (jailed to /workspace)
  list_dir         — list a dir     (jailed to /workspace)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import uvicorn
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SAFE_ROOT = Path(os.environ.get("MCP_SAFE_ROOT", "/workspace")).resolve()
COMMAND_TIMEOUT = int(os.environ.get("MCP_CMD_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="sandbox-tools",
    instructions=(
        "Hardened sandbox tools running inside a Docker container with no "
        f"network access. All file operations are confined to {SAFE_ROOT}. "
        "Commands execute inside the sandboxed container."
    ),
)


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
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def execute_command(command: str, working_dir: str = "/workspace") -> str:
    """Execute a shell command inside the sandbox and return the result.

    Args:
        command:     Shell command string to run.
        working_dir: Working directory (must be within /workspace).
    """
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


@mcp.tool()
def read_file(path: str) -> str:
    """Read the contents of a file within /workspace."""
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


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write (overwrite) a file within /workspace."""
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


@mcp.tool()
def append_file(path: str, content: str) -> str:
    """Append text to a file within /workspace (creates it if missing)."""
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


@mcp.tool()
def delete_file(path: str) -> str:
    """Delete a file within /workspace."""
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


@mcp.tool()
def list_dir(path: str = "/workspace") -> str:
    """List files and directories at a path within /workspace."""
    try:
        resolved = _safe_path(path)
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


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

async def _run_tcp(host: str = "0.0.0.0", port: int = 9100,
                   transport: str = "sse") -> None:
    """Run the MCP server on a TCP port, bound to localhost-only by the host."""
    app = mcp.http_app(transport=transport)
    config = uvicorn.Config(
        app, host=host, port=port, lifespan="on",
        log_level="info", timeout_graceful_shutdown=0,
    )
    server = uvicorn.Server(config)

    print(f"\U0001f512 MCP Sandbox Server — tcp://{host}:{port} [{transport}]")
    print(f"   Safe root:   {SAFE_ROOT}")
    print(f"   Cmd timeout: {COMMAND_TIMEOUT}s")
    print(f"   Tools: execute_command | read_file | write_file | append_file | delete_file | list_dir")
    await server.serve()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MCP Sandbox Server")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MCP_PORT", "9100")),
        help="TCP port to bind (default: 9100).  Env var: MCP_PORT",
    )
    parser.add_argument(
        "--host", default=os.environ.get("MCP_HOST", "0.0.0.0"),
        help="TCP bind host (default: 0.0.0.0).  Env var: MCP_HOST",
    )
    parser.add_argument(
        "--transport", choices=["sse", "streamable-http"], default="sse",
        help="HTTP transport flavour (default: sse)",
    )
    args = parser.parse_args()
    asyncio.run(_run_tcp(host=args.host, port=args.port, transport=args.transport))