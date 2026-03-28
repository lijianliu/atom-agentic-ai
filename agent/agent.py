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
"""
from __future__ import annotations

import argparse
import asyncio
import readline  # noqa: F401 — enables arrow keys & history in input()
import signal

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    UserPromptPart,
)

from model import build_model, build_openai_model
from mcp_helpers import DEFAULT_MCP_URL, build_tcp_mcp_server, check_mcp_reachable
from gcs_audit_logger import GCSLogger
from logging_config import setup_logging, get_logger, LOG_FILE_PATH

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
# Main REPL loop
# ---------------------------------------------------------------------------

async def main(
    agent: Agent,  # type: ignore[type-arg]
    verbose: bool = False,
    mcp_url: str = DEFAULT_MCP_URL,
    use_openai: bool = False,
) -> None:
    if not await check_mcp_reachable(mcp_url):
        logger.error("Cannot reach MCP server at %s", mcp_url)
        print(f"❌ Cannot reach MCP server at {mcp_url}")
        print("   Start the sandbox first:  bash sandbox/run-mcp-macos.sh")
        return

    print("🤖 Atom Agent (MCP Sandbox)")
    print(f"   📋 Log: {LOG_FILE_PATH}")

    gcs_audit_logger = GCSLogger.from_env()
    if gcs_audit_logger:
        logger.info("GCS logging enabled → %s", gcs_audit_logger.gcs_uri)
        print(f"   📝 GCS: {gcs_audit_logger.gcs_uri}")
        await gcs_audit_logger.log("session_start", {
            "mcp_url": mcp_url,
            "model": "openai" if use_openai else "anthropic",
            "verbose": verbose,
        })
    else:
        logger.info("GCS logging disabled (ATOM_AUDIT_LOG_GCS_PATH not set)")
        print("   📝 GCS logging disabled (set ATOM_AUDIT_LOG_GCS_PATH to enable)")

    print("   Type 'exit' to quit.  Ctrl+C cancels a running turn.")

    async with agent:
        message_history: list = []
        while True:
            try:
                prompt = input("\n👤 You: ")
            except (KeyboardInterrupt, EOFError):
                print("  (interrupted)")
                continue

            if prompt.strip().lower() in ("exit", "quit"):
                break
            if not prompt.strip():
                continue

            if gcs_audit_logger:
                await gcs_audit_logger.log("user_prompt", {"prompt": prompt})

            print("⏳ Thinking... (Ctrl+C to cancel)")
            cancelled = False
            loop = asyncio.get_running_loop()

            async def _run() -> None:
                async with agent.iter(prompt, message_history=message_history) as run:
                    async for node in run:
                        if Agent.is_model_request_node(node):
                            # --- Stream the model's response token-by-token ---
                            tool_args_printed = 0       # chars of tool args emitted
                            TOOL_ARGS_CAP = 200         # max chars before "…"
                            async with node.stream(run.ctx) as stream:
                                async for event in stream:
                                    if isinstance(event, PartStartEvent):
                                        # A new response part is beginning;
                                        # the part may already carry initial content.
                                        if isinstance(event.part, ThinkingPart):
                                            print("\n💭 Thinking: ", end="", flush=True)
                                            if event.part.content:
                                                print(event.part.content, end="", flush=True)
                                        elif isinstance(event.part, TextPart):
                                            print("\n💬 ", end="", flush=True)
                                            if event.part.content:
                                                print(event.part.content, end="", flush=True)
                                        elif isinstance(event.part, ToolCallPart):
                                            tool_args_printed = 0  # reset for each new tool call
                                            if verbose:
                                                args_str = str(event.part.args) if event.part.args else ""
                                                print(f"\n🔧 Tool: {event.part.tool_name}({args_str}", end="", flush=True)
                                                tool_args_printed += len(args_str)
                                    elif isinstance(event, PartDeltaEvent):
                                        if isinstance(event.delta, TextPartDelta):
                                            print(event.delta.content_delta, end="", flush=True)
                                        elif isinstance(event.delta, ThinkingPartDelta):
                                            if verbose:
                                                print(event.delta.content_delta, end="", flush=True)
                                        elif isinstance(event.delta, ToolCallPartDelta):
                                            if verbose and tool_args_printed < TOOL_ARGS_CAP:
                                                chunk = event.delta.args_delta
                                                remaining = TOOL_ARGS_CAP - tool_args_printed
                                                if len(chunk) > remaining:
                                                    print(chunk[:remaining] + "…)", flush=True)
                                                else:
                                                    print(chunk, end="", flush=True)
                                                tool_args_printed += len(chunk)
                                print()  # newline after each streamed model turn
                                # Show per-turn token usage with cache & call info
                                u = stream.usage()
                                in_t = u.input_tokens or 0
                                out_t = u.output_tokens or 0
                                cache_write = getattr(u, 'cache_write_tokens', 0) or 0
                                cache_read = getattr(u, 'cache_read_tokens', 0) or 0
                                new_t = in_t - cache_write - cache_read
                                reqs = getattr(u, 'requests', 0) or 0
                                tools = getattr(u, 'tool_calls', 0) or 0
                                cache_hit_pct = (cache_read / in_t * 100) if in_t > 0 else 0
                                uncached = new_t + cache_write
                                line = (
                                    f"{in_t:,} in "
                                    f"({new_t:,} new · {cache_write:,} cache write · {cache_read:,} cache read)"
                                    f" [{cache_hit_pct:.0f}% hit · {uncached:,} uncached]"
                                    f" / {out_t:,} out"
                                    f" | {reqs} reqs / {tools} tools"
                                )
                                print(f"  📊 [{line}]")

                        elif Agent.is_call_tools_node(node):
                            # Tools have been called — print a clean summary.
                            for part in node.model_response.parts:
                                if isinstance(part, ToolCallPart):
                                    args_str = str(part.args)[:200] if part.args else ""
                                    if verbose:
                                        print(f"  ⚙️  [Executing] {part.tool_name}({args_str})")
                                    else:
                                        print(f"🔧 Tool: {part.tool_name}({args_str})")
                                    if gcs_audit_logger:
                                        await gcs_audit_logger.log("tool_call", {
                                            "tool": part.tool_name,
                                            "args_preview": args_str,
                                        })


                        elif Agent.is_end_node(node):
                            if verbose:
                                print(f"VERBOSE> ✅ [End] {str(node.data)[:200]}")

                    result = run.result
                    message_history.extend(result.new_messages())
                    print(f"\n🤖 Agent: {result.output}")

                    usage = result.usage()
                    in_t = usage.input_tokens or 0
                    out_t = usage.output_tokens or 0
                    cache_write = getattr(usage, 'cache_write_tokens', 0) or 0
                    cache_read = getattr(usage, 'cache_read_tokens', 0) or 0
                    new_t = in_t - cache_write - cache_read
                    reqs = getattr(usage, 'requests', 0) or 0
                    tools = getattr(usage, 'tool_calls', 0) or 0
                    cache_hit_pct = (cache_read / in_t * 100) if in_t > 0 else 0
                    uncached = new_t + cache_write
                    total_line = (
                        f"{in_t:,} in "
                        f"({new_t:,} new · {cache_write:,} cache write · {cache_read:,} cache read)"
                        f" [{cache_hit_pct:.0f}% hit · {uncached:,} uncached]"
                        f" / {out_t:,} out"
                        f" / {in_t + out_t:,} total"
                        f" | {reqs} reqs / {tools} tools"
                    )
                    print(f"\n📊 Total: {total_line}")

                    if gcs_audit_logger:
                        await gcs_audit_logger.log("agent_response", {
                            "response": result.output,
                        })
                        await gcs_audit_logger.log("token_usage", {
                            "input_tokens": in_t,
                            "output_tokens": out_t,
                            "cache_write_tokens": cache_write,
                            "cache_read_tokens": cache_read,
                            "new_tokens": new_t,
                            "requests": reqs,
                            "tool_calls": tools,
                            "cache_hit_pct": round(cache_hit_pct, 1),
                        })

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
                print("\n⚠️  Cancelled.")

    if gcs_audit_logger:
        print(f"\n📝 Flushing session log to {gcs_audit_logger.gcs_uri} ...")
        await gcs_audit_logger.close()
        logger.info("Session log flushed → %s", gcs_audit_logger.gcs_uri)
        print("   ✅ Done.")


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
        asyncio.run(main(
            agent,
            verbose=args.verbose,
            mcp_url=args.mcp_url,
            use_openai=args.openai,
        ))
    except KeyboardInterrupt:
        print("\n👋 Bye!")
