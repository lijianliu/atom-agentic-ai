"""
agent.py — Atom Agent backed by MCP tools in a hardened Docker sandbox
======================================================================
All tools (execute_command, read_file, write_file, append_file,
list_dir, delete_file) run INSIDE the sandbox container via MCP.
Nothing executes on the host.

Architecture:

  HOST                              CONTAINER (sandbox-mcp)
  ──────────────────            ────────────────────────────────
  agent.py                          mcp_server.py
    MCPServerSSE ─ HTTP/SSE ───▶ uvicorn 0.0.0.0:9100
    127.0.0.1:9100/sse              tools: execute_command, read_file ...

Usage:
  bash sandbox/run-mcp-macos.sh     # 1. start the sandbox
  python -m agent.agent             # 2. run the agent
  python -m agent.agent --openai
  python -m agent.agent --verbose
  python -m agent.agent --mcp-url http://127.0.0.1:9100/sse
  python -m agent.agent --session my-session.json   # save/resume
"""
from __future__ import annotations

import argparse
import asyncio
import readline  # noqa: F401 — enables arrow keys & history in input()
from pathlib import Path

from pydantic_ai import Agent

from model import build_model, build_openai_model
from mcp_helpers import DEFAULT_MCP_URL, build_tcp_mcp_server
from logging_config import setup_logging, get_logger
from repl import run_repl

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def get_system_prompt() -> str:
    return (
        "You are a helpful agent. Your tools (execute_command, read_file, "
        "write_file, append_file, delete_file, list_dir) run inside a hardened "
        "Docker sandbox with no network access. File paths are relative to "
        "/workspace inside the sandbox."
    )


def build_agent(
    use_openai: bool = False,
    mcp_url: str = DEFAULT_MCP_URL,
) -> Agent:  # type: ignore[type-arg]
    mcp_server = build_tcp_mcp_server(mcp_url)
    if use_openai:
        return Agent(
            model=build_openai_model(),
            model_settings={"max_tokens": 127_000},
            system_prompt=get_system_prompt(),
            mcp_servers=[mcp_server],
        )
    return Agent(
        model=build_model(),
        model_settings={
            "max_tokens": 127_000,
            "anthropic_cache_instructions": True,
            "anthropic_cache_tool_definitions": True,
            "anthropic_cache_messages": True,
        },
        system_prompt=get_system_prompt(),
        mcp_servers=[mcp_server],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Atom Agent — MCP tools in a hardened Docker sandbox")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--openai", action="store_true", help="Use OpenAI model")
    p.add_argument(
        "--session",
        type=Path,
        default=None,
        metavar="FILE",
        help="Resume a specific session file. "
             "If omitted, auto-creates a new timestamped session.",
    )
    p.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        metavar="URL",
        help=f"MCP server SSE URL (default: {DEFAULT_MCP_URL})",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_logging()
    logger.info("Atom Agent starting (openai=%s, mcp_url=%s)", args.openai, args.mcp_url)
    agent = build_agent(
        use_openai=args.openai,
        mcp_url=args.mcp_url,
    )
    try:
        asyncio.run(run_repl(
            agent,
            verbose=args.verbose,
            mcp_url=args.mcp_url,
            use_openai=args.openai,
            session_file=args.session,
        ))
    except KeyboardInterrupt:
        print("\n👋 Bye!")
