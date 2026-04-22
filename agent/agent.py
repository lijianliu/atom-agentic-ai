"""
agent.py — Atom Agent backed by MCP tools in a hardened Docker sandbox
======================================================================
All tools (execute_command, read_file, write_file, append_file,
list_dir, delete_file) run INSIDE the sandbox container via MCP.
Nothing executes on the host.

Architecture (normal mode):

  HOST                              CONTAINER (sandbox-mcp)
  ──────────────────            ────────────────────────────────
  agent.py                          mcp_server.py
    MCPServerSSE ─ HTTP/SSE ───▶ uvicorn 0.0.0.0:9100
    127.0.0.1:9100/sse              tools: execute_command, read_file ...

  upload_output_file runs on the HOST (reads from shared /workspace mount,
  uploads to GCS via host credentials).

Root mode (--root):
  Tools run DIRECTLY on the host via local_tools.py — no sandboxing!
  Use with caution.

Usage:
  bash sandbox/run-mcp-macos.sh     # 1. start the sandbox
  python -m agent.agent             # 2. run the agent
  python -m agent.agent --openai
  python -m agent.agent --verbose
  python -m agent.agent --mcp-url http://127.0.0.1:9100/sse
  python -m agent.agent --session my-session.json   # save/resume
  python -m agent.agent --root      # root mode: local tools, no sandbox
"""
from __future__ import annotations

import argparse
import asyncio
import os
import readline  # noqa: F401 — enables arrow keys & history in input()
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModelSettings

from model import build_model, build_openai_model
from mcp_helpers import DEFAULT_MCP_URL, build_tcp_mcp_server
from local_tools import register_local_tools, register_upload_tool
from logging_config import setup_logging, get_logger
from repl import run_repl, strip_thinking_blocks

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_PATH = Path.home() / ".config" / "atom-agentic-ai" / "system_prompt.md"

_UPLOAD_INSTRUCTION = (
    "\n\nIMPORTANT: When you create or generate any output files (reports, CSVs, "
    "analyses, charts, processed data, or any other deliverables), call the "
    "upload_output_file tool to upload each file to cloud storage so the "
    "user can access it. Do this as a final step after the file is written."
)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful agent. Your tools (execute_command, read_file, "
    "write_file, append_file, delete_file, list_dir, upload_output_file) "
    "run inside a hardened Docker sandbox with no network access. File "
    "paths are relative to /workspace inside the sandbox. The "
    "upload_output_file tool runs on the host and uploads files from "
    "/workspace to cloud storage."
    + _UPLOAD_INSTRUCTION
)

_ROOT_MODE_SYSTEM_PROMPT = (
    "You are a helpful agent. Your tools (execute_command, read_file, "
    "write_file, append_file, delete_file, list_dir, upload_output_file) "
    "run DIRECTLY on the host machine with NO sandboxing. File paths are "
    "relative to the current working directory. Be careful with destructive "
    "operations!"
    + _UPLOAD_INSTRUCTION
)

# Shared workspace: same path on host and container (default /workspace).
_SHARED_WORKSPACE = Path(
    os.environ.get("ATOM_SHARED_WORKSPACE", "/workspace")
).resolve()


def get_system_prompt(root_mode: bool = False, prompt_file: Path | None = None) -> str:
    """Load system prompt from file, falling back to defaults.

    Priority:
      1. Explicit --system-prompt file (if provided)
      2. ~/.config/atom-agentic-ai/system_prompt.md (if exists)
      3. Built-in default (root mode or sandbox mode)
    """
    # 1. Explicit file from CLI
    if prompt_file and prompt_file.is_file():
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        if prompt:
            logger.info("Loaded system prompt from %s", prompt_file)
            return prompt
        logger.warning("System prompt file is empty: %s", prompt_file)

    # 2. Default config location
    if _SYSTEM_PROMPT_PATH.is_file():
        prompt = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        if prompt:
            logger.info("Loaded system prompt from %s", _SYSTEM_PROMPT_PATH)
            return prompt
        logger.warning("System prompt file is empty, using default: %s", _SYSTEM_PROMPT_PATH)

    # 3. Built-in default
    return _ROOT_MODE_SYSTEM_PROMPT if root_mode else _DEFAULT_SYSTEM_PROMPT


def build_agent(
    use_openai: bool = False,
    mcp_url: str = DEFAULT_MCP_URL,
    root_mode: bool = False,
    system_prompt_file: Path | None = None,
) -> Agent:  # type: ignore[type-arg]
    """Build an agent with either MCP tools (sandbox) or local tools (root mode)."""
    prompt = get_system_prompt(root_mode=root_mode, prompt_file=system_prompt_file)

    # --- Root mode: local tools, no MCP ---
    if root_mode:
        logger.info("Root mode enabled — using local tools (no MCP sandbox)")
        if use_openai:
            agent = Agent(
                model=build_openai_model(),
                model_settings={"max_tokens": 127_000},
                system_prompt=prompt,
            )
        else:
            agent = Agent(
                model=build_model(),
                model_settings=AnthropicModelSettings(
                    max_tokens=127_000,
                    anthropic_thinking={"type": "adaptive"},
                    anthropic_betas=["fine-grained-tool-streaming-2025-05-14"],
                    anthropic_cache_instructions=True,
                    anthropic_cache_tool_definitions=True,
                    anthropic_cache_messages=True,
                ),
                system_prompt=prompt,
                history_processors=[strip_thinking_blocks],
            )
        register_local_tools(agent)
        register_upload_tool(agent)
        return agent

    # --- Normal mode: MCP sandbox tools + host-side upload tool ---
    mcp_server = build_tcp_mcp_server(mcp_url)
    if use_openai:
        agent = Agent(
            model=build_openai_model(),
            model_settings={"max_tokens": 127_000},
            system_prompt=prompt,
            mcp_servers=[mcp_server],
        )
    else:
        agent = Agent(
            model=build_model(),
            model_settings=AnthropicModelSettings(
                max_tokens=127_000,
                anthropic_thinking={"type": "adaptive"},
                anthropic_betas=["fine-grained-tool-streaming-2025-05-14"],
                anthropic_cache_instructions=True,
                anthropic_cache_tool_definitions=True,
                anthropic_cache_messages=True,
            ),
            system_prompt=prompt,
            mcp_servers=[mcp_server],
            history_processors=[strip_thinking_blocks],
        )

    # upload_output_file runs on the HOST — reads from the shared workspace
    # mount and uploads to GCS using host credentials.
    register_upload_tool(agent, workspace=_SHARED_WORKSPACE)
    return agent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Atom Agent — MCP tools in a hardened Docker sandbox")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--openai", action="store_true", help="Use OpenAI model")
    p.add_argument(
        "--root",
        action="store_true",
        help="Run in root mode: bypass MCP sandbox, use local file/exec tools directly on host",
    )
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
    p.add_argument(
        "--system-prompt",
        type=Path,
        default=None,
        metavar="FILE",
        help="Path to custom system prompt file (overrides default)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_logging()
    logger.info(
        "Atom Agent starting (openai=%s, root=%s, mcp_url=%s)",
        args.openai, args.root, args.mcp_url,
    )

    # Get system prompt before building agent so we can log it
    system_prompt = get_system_prompt(root_mode=args.root, prompt_file=args.system_prompt)

    agent = build_agent(
        use_openai=args.openai,
        mcp_url=args.mcp_url,
        root_mode=args.root,
        system_prompt_file=args.system_prompt,
    )
    try:
        asyncio.run(run_repl(
            agent,
            verbose=args.verbose,
            mcp_url=args.mcp_url,
            use_openai=args.openai,
            session_file=args.session,
            root_mode=args.root,
            system_prompt=system_prompt,
        ))
    except KeyboardInterrupt:
        print("\n👋 Bye!")
