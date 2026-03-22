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
  python -m agent.agent --socket /tmp/mcp-sandbox/mcp.sock  # legacy UDS
"""
from __future__ import annotations

import argparse
import asyncio
import signal

import httpx
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerSSE
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)

from agent.model import build_model, build_openai_model

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MCP_URL = "http://127.0.0.1:9100/sse"
DEFAULT_SOCKET = "/tmp/mcp-sandbox/mcp.sock"  # legacy UDS fallback


# ---------------------------------------------------------------------------
# MCP server constructors
# ---------------------------------------------------------------------------

def _build_tcp_mcp_server(url: str) -> MCPServerSSE:
    """MCP over plain TCP — the normal case."""
    return MCPServerSSE(
        url=url,
        http_client=httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=300.0),
        ),
    )


def _build_uds_mcp_server(socket_path: str) -> MCPServerSSE:
    """MCP over Unix domain socket — legacy Option 3."""
    transport = httpx.AsyncHTTPTransport(uds=socket_path)
    return MCPServerSSE(
        url="http://sandbox/sse",
        http_client=httpx.AsyncClient(
            transport=transport,
            base_url="http://sandbox",
            timeout=httpx.Timeout(30.0, read=300.0),
        ),
    )


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
    socket_path: str | None = None,
) -> Agent:  # type: ignore[type-arg]
    mcp_server = (
        _build_uds_mcp_server(socket_path)
        if socket_path
        else _build_tcp_mcp_server(mcp_url)
    )
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
# Pretty-printing helpers
# ---------------------------------------------------------------------------

from pydantic_ai._agent_graph import CallToolsNode, End, ModelRequestNode, UserPromptNode  # noqa: E402


def _print_request(request: ModelRequest, verbose: bool) -> None:
    for part in request.parts:
        if isinstance(part, UserPromptPart):
            print(
                f"  \U0001f4e4 [UserPrompt] {part.content}"
                if verbose
                else f"\n\U0001f4ac Request: {part.content}"
            )


def _print_response(response: ModelResponse, verbose: bool) -> None:
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            print(
                f"  \U0001f9e0 [Thinking] {part.content}"
                if verbose
                else f"\n\U0001f9e0 Thinking:\n{part.content}"
            )
        elif isinstance(part, TextPart):
            print(
                f"  \U0001f4ac [Text] {part.content}"
                if verbose
                else f"\n\U0001f4ac Response:\n{part.content}"
            )
        elif isinstance(part, ToolCallPart):
            snippet = str(part.args)[:120]
            print(
                f"  \U0001f527 [ToolCall] {part.tool_name}({snippet})"
                if verbose
                else f"\n\U0001f527 Tool: {part.tool_name}({snippet})"
            )


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------

async def _check_reachable(mcp_url: str, socket_path: str | None) -> bool:
    """Return True if the MCP server is reachable."""
    if socket_path:
        from pathlib import Path
        return Path(socket_path).exists()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=2.0)) as c:
            await c.get(mcp_url)
    except httpx.ReadTimeout:
        return True  # SSE streams forever — timeout == connected
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Main REPL loop
# ---------------------------------------------------------------------------

async def main(
    agent: Agent,  # type: ignore[type-arg]
    verbose: bool = False,
    mcp_url: str = DEFAULT_MCP_URL,
    socket_path: str | None = None,
) -> None:
    label = f"socket://{socket_path}" if socket_path else mcp_url

    if not await _check_reachable(mcp_url, socket_path):
        print(f"\u274c Cannot reach MCP server at {label}")
        print("   Start the sandbox first:  bash sandbox/run-mcp-macos.sh")
        return

    print("\U0001f916 Atom Agent (MCP Sandbox)")
    print(f"   MCP: {label}")
    print("   Type 'exit' to quit.  Ctrl+C cancels a running turn.")

    async with agent:
        message_history: list = []
        while True:
            try:
                prompt = input("\n\U0001f464 You: ")
            except (KeyboardInterrupt, EOFError):
                print("  (interrupted)")
                continue

            if prompt.strip().lower() in ("exit", "quit"):
                break
            if not prompt.strip():
                continue

            print("\u23f3 Thinking... (Ctrl+C to cancel)")
            cancelled = False
            loop = asyncio.get_running_loop()

            async def _run() -> None:
                async with agent.iter(prompt, message_history=message_history) as run:
                    async for node in run:
                        if verbose:
                            match node:
                                case ModelRequestNode(request=req):
                                    _print_request(req, verbose=True)
                                case CallToolsNode(model_response=resp):
                                    _print_response(resp, verbose=True)
                                case End(data=data):
                                    print(f"VERBOSE> \u2705 [End] {str(data)[:200]}")
                                case _:
                                    print(f"VERBOSE> [{type(node).__name__}]")
                        else:
                            match node:
                                case CallToolsNode(model_response=resp):
                                    _print_response(resp, verbose=False)
                                case _:
                                    pass
                result = run.result
                message_history.extend(result.new_messages())
                print(f"\n\U0001f916 Agent: {result.output}")

            task = asyncio.ensure_future(_run())

            def _cancel(_):
                nonlocal cancelled
                cancelled = True
                task.cancel()

            loop.add_signal_handler(signal.SIGINT, _cancel, None)
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                loop.remove_signal_handler(signal.SIGINT)

            if cancelled:
                print("\n\u26a0\ufe0f  Cancelled.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Atom Agent — MCP tools in a hardened Docker sandbox")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--openai", action="store_true", help="Use OpenAI model")
    p.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        metavar="URL",
        help=f"MCP server SSE URL (default: {DEFAULT_MCP_URL})",
    )
    p.add_argument(
        "--socket",
        default=None,
        metavar="PATH",
        help="Legacy: Unix socket path (overrides --mcp-url when set)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent = build_agent(
        use_openai=args.openai,
        mcp_url=args.mcp_url,
        socket_path=args.socket,
    )
    try:
        asyncio.run(main(
            agent,
            verbose=args.verbose,
            mcp_url=args.mcp_url,
            socket_path=args.socket,
        ))
    except KeyboardInterrupt:
        print("\n\U0001f44b Bye!")
