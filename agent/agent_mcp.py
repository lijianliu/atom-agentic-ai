"""
agent_mcp.py — Atom Agent backed by MCP tools in a hardened Docker sandbox
==========================================================================
The MCP server runs INSIDE a hardened Docker container (--network=none).
Communication uses a Unix domain socket bind-mounted from the host —
the same pattern as gsutil-proxy, just with the roles flipped.

Architecture:

  HOST                                CONTAINER (--network=none)
  ─────────────────────────           ──────────────────────────────────────
  agent_mcp.py                        mcp_server.py
    httpx + UDS transport   ←──────── uvicorn bound to /mcp-socket/mcp.sock
    /tmp/mcp-sandbox/mcp.sock         (bind-mounted from host)
    MCPServerSSE                       tools: execute_command, read_file ...

  LLM API calls go out normally via host network — the container itself
  has zero network access.

Usage:
  # 1. Start the sandbox MCP server:
  sandbox/run-hardened-mcp-macos.sh --detach

  # 2. Run this agent:
  python -m agent.agent_mcp
  python -m agent.agent_mcp --openai
  python -m agent.agent_mcp --verbose
  python -m agent.agent_mcp --socket /tmp/mcp-sandbox/mcp.sock
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

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


# Default socket path — must match SOCKET_HOST_PATH in run-hardened-mcp-macos.sh
DEFAULT_SOCKET = "/tmp/mcp-sandbox/mcp.sock"
# SSE endpoint path as configured by FastMCP defaults
SSE_PATH = "/sse"


def _build_uds_mcp_server(socket_path: str) -> MCPServerSSE:
    """Build an MCPServerSSE that connects via Unix domain socket.

    httpx supports UDS transport natively.  We pass a pre-configured
    AsyncClient to MCPServerSSE so it routes all HTTP over the socket
    instead of TCP — no port, no network needed.

    The URL hostname is arbitrary when using UDS (httpx ignores it),
    but it must be set to something so the SSE client constructs paths
    correctly.
    """
    transport = httpx.AsyncHTTPTransport(uds=socket_path)
    http_client = httpx.AsyncClient(
        transport=transport,
        base_url="http://sandbox",  # hostname is ignored by UDS transport
        timeout=httpx.Timeout(30.0, read=300.0),  # long read for SSE stream
    )
    return MCPServerSSE(
        url=f"http://sandbox{SSE_PATH}",
        http_client=http_client,
    )


def get_system_prompt() -> str:
    return (
        "You are a helpful agent.  Your tools (execute_command, read_file, "
        "write_file, append_file, delete_file, list_dir) run inside a hardened "
        "Docker container with no network access.  File paths are relative to "
        "/workspace inside the container."
    )


def build_agent(
    use_openai: bool = False,
    socket_path: str = DEFAULT_SOCKET,
) -> Agent:  # type: ignore[type-arg]
    """Build the agent, wiring its MCP tools through the UDS sandbox."""
    mcp_server = _build_uds_mcp_server(socket_path)

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
            print(f"  \U0001f4e4 [UserPrompt] {part.content}" if verbose else f"\n\U0001f4ac Request: {part.content}")


def _print_response(response: ModelResponse, verbose: bool) -> None:
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            print(f"  \U0001f9e0 [Thinking] {part.content}" if verbose else f"\n\U0001f9e0 Thinking:\n{part.content}")
        elif isinstance(part, TextPart):
            print(f"  \U0001f4ac [Text] {part.content}" if verbose else f"\n\U0001f4ac Response:\n{part.content}")
        elif isinstance(part, ToolCallPart):
            snippet = str(part.args)[:120]
            print(f"  \U0001f527 [ToolCall] {part.tool_name}({snippet})" if verbose else f"\n\U0001f527 Tool: {part.tool_name}({snippet})")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main(agent: Agent, verbose: bool = False, socket_path: str = DEFAULT_SOCKET) -> None:  # type: ignore[type-arg]
    sock = Path(socket_path)
    if not sock.exists():
        print(f"\u274c Socket not found: {socket_path}")
        print("   Start the sandbox first:")
        print("     sandbox/run-hardened-mcp-macos.sh --detach")
        return

    print("\U0001f916 Atom Agent (MCP Sandbox — UDS mode)")
    print(f"   Socket: {socket_path}")
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
    p = argparse.ArgumentParser(
        description="Atom Agent — MCP tools over Unix domain socket sandbox"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--openai", action="store_true", help="Use OpenAI model")
    p.add_argument(
        "--socket",
        default=DEFAULT_SOCKET,
        metavar="PATH",
        help=f"Unix socket path of the sandbox MCP server (default: {DEFAULT_SOCKET})",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent = build_agent(use_openai=args.openai, socket_path=args.socket)
    try:
        asyncio.run(main(agent, verbose=args.verbose, socket_path=args.socket))
    except KeyboardInterrupt:
        print("\n\U0001f44b Bye!")