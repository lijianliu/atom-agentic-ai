"""
repl.py — Interactive Read-Eval-Print Loop for the Atom Agent
=============================================================
Handles the interactive session: prompt input, streaming model responses,
tool execution display, cancellation recovery, session persistence,
and GCS audit logging.

Split from agent.py to keep the agent factory / CLI separate from
the REPL orchestration.
"""
from __future__ import annotations

import asyncio
import copy
import os
import signal
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
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
    ToolReturnPart,
)

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI

from gcs_audit_logger import GCSLogger
from logging_config import get_logger, LOG_FILE_PATH
from session_store import save_session, load_session, default_session_path
from turn_logger import TurnLogger
from usage_helpers import (
    format_usage_line,
    build_usage_dict,
    accumulate_session_usage,
    format_session_usage,
)
from mcp_helpers import check_mcp_reachable

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# History processor: ThinkingPart stripping before every API call
# ---------------------------------------------------------------------------

_STRIP_ALL_THINKING = os.environ.get("STRIP_ALL_THINKING", "1") != "0"


def strip_thinking_blocks(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Remove ThinkingPart entries from history before each request.

    Default behavior:
        Strip ThinkingPart from every ModelResponse EXCEPT the most recent one.

    Fallback:
        If STRIP_ALL_THINKING=1, strip ThinkingPart from every ModelResponse,
        including the most recent one.
    """
    last_response_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, ModelResponse):
            last_response_idx = i

    if last_response_idx < 0:
        return messages

    stripped_count = 0
    cleaned: list[ModelMessage] = []

    for i, msg in enumerate(messages):
        if isinstance(msg, ModelResponse):
            preserve = (i == last_response_idx) and not _STRIP_ALL_THINKING
            if not preserve:
                new_parts = [p for p in msg.parts if not isinstance(p, ThinkingPart)]
                removed = len(msg.parts) - len(new_parts)
                if removed:
                    stripped_count += removed
                    msg = replace(msg, parts=new_parts)
        cleaned.append(msg)

    if stripped_count:
        logger.debug(
            "history_processor: stripped %d thinking block(s) "
            "(strip_all=%s, last_response_preserved=%s)",
            stripped_count,
            _STRIP_ALL_THINKING,
            (not _STRIP_ALL_THINKING) and last_response_idx >= 0,
        )
    return cleaned


# ---------------------------------------------------------------------------
# Multi-line paste-aware input  (prompt_toolkit + bracketed paste)
# ---------------------------------------------------------------------------

_bindings = KeyBindings()


@_bindings.add("enter")
def _submit(event):
    # A *typed* Enter submits the buffer.
    # Pasted newlines don't go through this binding (bracketed paste
    # inserts them directly), so multi-line pastes are preserved.
    event.current_buffer.validate_and_handle()


_session = PromptSession(
    history=InMemoryHistory(),
    multiline=True,
    key_bindings=_bindings,
    enable_history_search=True,
)


async def _read_multiline_input(prompt: str = "") -> str:
    """Read user input with paste-friendly multiline support (async)."""
    return await _session.prompt_async(
        ANSI(prompt),
        prompt_continuation=lambda width, line_number, is_soft_wrap: "",
    )


# ---------------------------------------------------------------------------
# History sanitisation (cancel-safety)
# ---------------------------------------------------------------------------

def _strip_thinking_parts_for_persistence(history: list) -> list:
    """Return a deep-copied history with ThinkingPart removed for saving."""
    history_copy = copy.deepcopy(history)
    removed = 0

    for msg in history_copy:
        if isinstance(msg, ModelResponse):
            original_len = len(msg.parts)
            msg.parts = [p for p in msg.parts if not isinstance(p, ThinkingPart)]
            removed += original_len - len(msg.parts)

    if removed:
        logger.debug(
            "Prepared persistence copy: stripped %d ThinkingPart blocks",
            removed,
        )

    return history_copy


def _sanitize_history(history: list) -> None:
    """Validate the full message history and strip any broken pairs.

    The Anthropic API requires every tool_result to reference a tool_use
    in the *immediately preceding* assistant message. After a Ctrl+C
    cancellation, partial tool exchanges can end up anywhere in the
    history — not just at the tail.
    """
    # Strip stale ThinkingPart blocks at the REPL boundary.
    for msg in history:
        if isinstance(msg, ModelResponse):
            original_len = len(msg.parts)
            msg.parts = [p for p in msg.parts if not isinstance(p, ThinkingPart)]
            stripped = original_len - len(msg.parts)
            if stripped:
                logger.debug(
                    "Sanitized history: stripped %d thinking parts from ModelResponse",
                    stripped,
                )

    truncate_at: int | None = None

    for i, msg in enumerate(history):
        if not isinstance(msg, ModelRequest):
            continue

        tool_return_ids = {
            p.tool_call_id
            for p in msg.parts
            if isinstance(p, ToolReturnPart)
        }
        if not tool_return_ids:
            continue  # plain user prompt — safe

        # The preceding message must be a ModelResponse with matching IDs
        if i == 0 or not isinstance(history[i - 1], ModelResponse):
            truncate_at = i
            break

        tool_call_ids = {
            p.tool_call_id
            for p in history[i - 1].parts
            if isinstance(p, ToolCallPart)
        }
        if not tool_return_ids <= tool_call_ids:
            # Mismatch — truncate from the bad ModelResponse onward
            truncate_at = i - 1
            break

    if truncate_at is not None:
        removed = len(history) - truncate_at
        del history[truncate_at:]
        logger.warning(
            "Sanitized history: truncated %d messages from index %d "
            "due to orphaned tool_result blocks",
            removed,
            truncate_at,
        )

    # Finally, strip any trailing ModelResponse with unanswered tool
    # calls (the model will just re-generate on the next turn).
    while history and isinstance(history[-1], ModelResponse):
        has_tool_calls = any(
            isinstance(p, ToolCallPart) for p in history[-1].parts
        )
        if not has_tool_calls:
            break
        history.pop()
        logger.warning(
            "Sanitized history: removed trailing ModelResponse "
            "with unanswered tool calls"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gcs_uri_to_web_url(gcs_uri: str) -> str | None:
    """Convert a ``gs://`` URI to a web URL using environment variables.

    Uses two environment variables:
        ATOM_LOG_URL_PREFIX     — The web URL prefix, e.g.
                                  ``atom.company.com/ext/atom-agentic-ui``
        ATOM_LOG_URL_GCS_PREFIX — The GCS path prefix to strip, e.g.
                                  ``gs://my-bucket/my-folder``

    Example:
        gcs_uri  = "gs://my-bucket/my-folder/2026-04-18/jdoe/17-32-28.060Z/session.html"
        gcs_pfx  = "gs://my-bucket/my-folder"
        web_pfx  = "atom.company.com/ext/atom-agentic-ui"
        result   = "atom.company.com/ext/atom-agentic-ui/2026-04-18/jdoe/17-32-28.060Z/session.html"

    Returns ``None`` if either env var is unset or the GCS URI doesn't
    start with the expected GCS prefix.
    """
    web_prefix = os.environ.get("ATOM_LOG_URL_PREFIX", "").strip().rstrip("/")
    gcs_prefix = os.environ.get("ATOM_LOG_URL_GCS_PREFIX", "").strip().rstrip("/")
    if not web_prefix or not gcs_prefix:
        return None
    if not gcs_uri.startswith(gcs_prefix):
        logger.debug(
            "GCS URI %r does not start with ATOM_LOG_URL_GCS_PREFIX %r",
            gcs_uri, gcs_prefix,
        )
        return None
    relative = gcs_uri[len(gcs_prefix):].lstrip("/")
    return f"{web_prefix}/{relative}"


# ---------------------------------------------------------------------------
# Main REPL loop
# ---------------------------------------------------------------------------

async def run_repl(
    agent: Agent,  # type: ignore[type-arg]
    verbose: bool = False,
    mcp_url: str = "http://127.0.0.1:9100/sse",
    use_openai: bool = False,
    session_file: Path | None = None,
    root_mode: bool = False,
    system_prompt: str = "",
) -> None:
    """Interactive REPL: read user prompts, stream agent responses, persist sessions."""
    # Skip MCP check in root mode (we're using local tools)
    if not root_mode and not await check_mcp_reachable(mcp_url):
        logger.error("Cannot reach MCP server at %s", mcp_url)
        print(f"\u274c Cannot reach MCP server at {mcp_url}")
        print("   Start the sandbox first:  bash sandbox/run-mcp-macos.sh")
        return

    if root_mode:
        print("\U0001f916 Atom Agent (\033[1;33mROOT MODE\033[0m — local tools, no sandbox)")
        print(f"   \u26a0\ufe0f  Working directory: {Path.cwd()}")
    else:
        print("\U0001f916 Atom Agent (MCP Sandbox)")
    print(f"   \U0001f4cb Log: {LOG_FILE_PATH}")

    gcs_audit_logger = GCSLogger.from_env()
    if gcs_audit_logger:
        logger.info("GCS logging enabled → %s", gcs_audit_logger.gcs_uri)
        print(f"   \U0001f194 Session ID: {gcs_audit_logger.session_id}")
        print(f"   \U0001f4dd GCS: {gcs_audit_logger.gcs_uri}")
        print(f"   \U0001f464 User: {gcs_audit_logger.username}")
        await gcs_audit_logger.warm_token()
    else:
        logger.info("GCS logging disabled (ATOM_AUDIT_LOG_GCS_PATH not set)")
        print("   \U0001f4dd GCS logging disabled (set ATOM_AUDIT_LOG_GCS_PATH to enable)")

    # ── Turn-by-Turn logging (uses same session_id as GCS if available) ──
    if gcs_audit_logger:
        session_id = gcs_audit_logger.session_id
    else:
        import getpass
        from datetime import datetime, timezone

        username = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser() or "unknown"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%f")[:-3] + "Z"
        session_id = f"{username}-{ts}"
        print(f"   \U0001f194 Session ID: {session_id}")

    turn_logger = TurnLogger.create(session_id, gcs_logger=gcs_audit_logger)
    print(f"   \U0001f4c1 Turn logs: {turn_logger.session_dir}")

    print("   Type 'exit' to quit.  Ctrl+C cancels a running turn.")

    async with agent:
        # Log session metadata after agent is fully initialized
        try:
            tool_names = []

            if hasattr(agent, "_function_toolset") and agent._function_toolset:
                if hasattr(agent._function_toolset, "tools"):
                    tool_names = sorted([name for name in agent._function_toolset.tools.keys()])
                    logger.debug("Extracted %d tools from _function_toolset", len(tool_names))
            elif hasattr(agent, "_user_toolsets") and agent._user_toolsets:
                for toolset in agent._user_toolsets:
                    if hasattr(toolset, "tools"):
                        tool_names.extend(toolset.tools.keys())
                tool_names = sorted(set(tool_names))
                logger.debug("Extracted %d tools from _user_toolsets", len(tool_names))

            model_name = ""
            if hasattr(agent, "model"):
                model_name = str(agent.model) if hasattr(agent.model, "__str__") else type(agent.model).__name__

            turn_logger.log_session_metadata(
                model_name=model_name,
                mcp_url=mcp_url if not root_mode else "N/A",
                root_mode=root_mode,
                tools=tool_names if tool_names else None,
            )

            if not tool_names:
                logger.warning(
                    "No tools extracted from agent (_function_toolset: %s, _user_toolsets: %s)",
                    hasattr(agent, "_function_toolset"),
                    hasattr(agent, "_user_toolsets"),
                )
        except Exception as e:
            logger.warning("Failed to log session metadata: %s", e, exc_info=True)

        # ── Resolve session file (auto-generate if not specified) ──
        if session_file is None:
            session_file = default_session_path(session_id)

        message_history, session_usage = load_session(session_file)
        if message_history:
            print(
                f"   ♻️  Resumed session from {session_file}"
                f" ({len(message_history)} messages, {session_usage.get('queries', 0)} queries)"
            )
            print(f"   📊 {format_session_usage(session_usage)}")
        else:
            print(f"   💾 Session: {session_file} (new)")

        while True:
            try:
                prompt = await _read_multiline_input("\033[30;48;5;226m\n👤 You: \033[0m ")
            except (KeyboardInterrupt, EOFError):
                print("  (interrupted)")
                continue

            if prompt.strip().lower() in ("exit", "quit"):
                break
            if not prompt.strip():
                continue

            if gcs_audit_logger:
                gcs_audit_logger.start_turn(prompt)
                await gcs_audit_logger.log("user_prompt", {"prompt": prompt})

            # Start a new query
            turn_logger.start_query()

            # Log system prompt and user prompt at turn 00
            if system_prompt:
                turn_logger.log_system_prompt(system_prompt)
            turn_logger.log_user_prompt(prompt)

            print("⏳ Thinking... (Ctrl+C to cancel)")
            cancelled = False
            loop = asyncio.get_running_loop()

            async def _run() -> None:
                nonlocal cancelled

                # REPL-boundary sanitization only
                _sanitize_history(message_history)

                async with agent.iter(
                    prompt,
                    message_history=message_history,
                    usage_limits=UsageLimits(request_limit=500),
                ) as run:
                    try:
                        pending_tool_calls: list[tuple[str, Any, str]] = []

                        async for node in run:
                            if Agent.is_model_request_node(node):
                                turn_logger.start_turn()

                                if pending_tool_calls:
                                    for part in node.request.parts:
                                        if isinstance(part, ToolReturnPart):
                                            for tool_name, args, call_id in list(pending_tool_calls):
                                                if call_id == part.tool_call_id:
                                                    turn_logger.log_tool_exec(
                                                        tool_name,
                                                        args,
                                                        call_id,
                                                        result=part.content,
                                                        override_query=turn_logger.current_query,
                                                        override_turn=turn_logger.previous_turn,
                                                    )
                                                    args_str = str(args)[:1000] if args else ""
                                                    print(f"\033[97;48;5;166m⚙️ [Tool Exec - End]\033[0m {tool_name}({args_str})")
                                                    if gcs_audit_logger:
                                                        await gcs_audit_logger.log("tool_call", {
                                                            "tool": tool_name,
                                                            "args_preview": args_str,
                                                        })
                                                    pending_tool_calls.remove((tool_name, args, call_id))
                                                    break

                                tool_args_printed = 0
                                TOOL_ARGS_CAP = 200
                                stream_thinking: list[str] = []
                                stream_text: list[str] = []
                                stream_tool_calls: dict[int, dict] = {}
                                tool_call_open = False

                                async with node.stream(run.ctx) as stream:
                                    async for event in stream:
                                        if isinstance(event, PartStartEvent):
                                            if tool_call_open:
                                                print(")", flush=True)
                                                tool_call_open = False
                                            if tool_args_printed >= TOOL_ARGS_CAP:
                                                kb = tool_args_printed / 1024
                                                print(f"\r\033[K  \u2705 streamed {kb:.1f}KB", flush=True)

                                            current_part_index = event.index
                                            tool_args_printed = 0

                                            if isinstance(event.part, ThinkingPart):
                                                print("\n\033[48;5;17m💭 [Thinking]\033[0m ", end="", flush=True)
                                                content = event.part.content
                                                if isinstance(content, str) and content:
                                                    print(content, end="", flush=True)
                                                    stream_thinking.append(content)
                                            elif isinstance(event.part, TextPart):
                                                print("\n\033[48;5;22m💬 [Text]\033[0m ", end="", flush=True)
                                                content = event.part.content
                                                if isinstance(content, str) and content:
                                                    print(content, end="", flush=True)
                                                    stream_text.append(content)
                                            elif isinstance(event.part, ToolCallPart):
                                                args_str = str(event.part.args) if event.part.args else ""
                                                print(f"\n\033[97;48;5;166m🔧 [Tool Plan]\033[0m {event.part.tool_name}(", end="", flush=True)
                                                if args_str:
                                                    if len(args_str) > TOOL_ARGS_CAP:
                                                        print(f"{args_str[:TOOL_ARGS_CAP]}…)", flush=True)
                                                        tool_args_printed = TOOL_ARGS_CAP
                                                        tool_call_open = False
                                                    else:
                                                        print(args_str, end="", flush=True)
                                                        tool_args_printed = len(args_str)
                                                        tool_call_open = True
                                                else:
                                                    tool_call_open = True
                                                stream_tool_calls[current_part_index] = {
                                                    "tool": event.part.tool_name,
                                                    "args": args_str,
                                                    "call_id": getattr(event.part, "tool_call_id", None),
                                                }

                                        elif isinstance(event, PartDeltaEvent):
                                            if isinstance(event.delta, TextPartDelta):
                                                delta_text = event.delta.content_delta
                                                if isinstance(delta_text, str) and delta_text:
                                                    print(delta_text, end="", flush=True)
                                                    stream_text.append(delta_text)
                                            elif isinstance(event.delta, ThinkingPartDelta):
                                                delta_text = event.delta.content_delta
                                                if isinstance(delta_text, str) and delta_text:
                                                    print(delta_text, end="", flush=True)
                                                    stream_thinking.append(delta_text)
                                            elif isinstance(event.delta, ToolCallPartDelta):
                                                chunk = event.delta.args_delta
                                                if not isinstance(chunk, str):
                                                    chunk = ""
                                                if tool_args_printed < TOOL_ARGS_CAP and chunk:
                                                    remaining = TOOL_ARGS_CAP - tool_args_printed
                                                    if len(chunk) > remaining:
                                                        print(chunk[:remaining] + "…)", flush=True)
                                                        tool_call_open = False
                                                    else:
                                                        print(chunk, end="", flush=True)
                                                tool_args_printed += len(chunk) if chunk else 0
                                                if tool_args_printed >= TOOL_ARGS_CAP and chunk:
                                                    kb = tool_args_printed / 1024
                                                    print(f"\r\033[K  \u23f3 streaming\u2026 {kb:.1f}KB", end="", flush=True)
                                                if chunk and event.index in stream_tool_calls:
                                                    stream_tool_calls[event.index]["args"] += chunk

                                    if tool_call_open:
                                        print(")", flush=True)
                                        tool_call_open = False
                                    if tool_args_printed >= TOOL_ARGS_CAP:
                                        kb = tool_args_printed / 1024
                                        print(f"\r\033[K  \u2705 streamed {kb:.1f}KB", flush=True)
                                        tool_args_printed = 0

                                    if stream_thinking:
                                        turn_logger.log_thinking("".join(s for s in stream_thinking if isinstance(s, str)))
                                    if stream_text:
                                        turn_logger.log_text("".join(s for s in stream_text if isinstance(s, str)))
                                    for tc in stream_tool_calls.values():
                                        turn_logger.log_tool_plan(tc["tool"], tc["args"], tc.get("call_id"))

                                    turn_usage = stream.usage()
                                    print(
                                        f"\n\033[48;5;240m📊 [Usage \033[0m"
                                        f"{format_usage_line(turn_usage, query=turn_logger.current_query, turn=turn_logger.current_turn)}"
                                    )
                                    turn_logger.log_usage(build_usage_dict(turn_usage))

                            elif Agent.is_call_tools_node(node):
                                for part in node.model_response.parts:
                                    if isinstance(part, ToolCallPart):
                                        pending_tool_calls.append((
                                            part.tool_name,
                                            part.args,
                                            part.tool_call_id,
                                        ))
                                        args_str = str(part.args)[:1000] if part.args else ""
                                        print(
                                            f"\033[97;48;5;172m⚙️ [Tool Exec - Start]\033[0m {part.tool_name}({args_str})",
                                            flush=True,
                                        )

                            elif Agent.is_end_node(node):
                                if pending_tool_calls:
                                    try:
                                        all_msgs = run.all_messages()
                                        for msg in all_msgs:
                                            if isinstance(msg, ModelRequest):
                                                for part in msg.parts:
                                                    if isinstance(part, ToolReturnPart):
                                                        for tool_name, args, call_id in list(pending_tool_calls):
                                                            if call_id == part.tool_call_id:
                                                                turn_logger.log_tool_exec(
                                                                    tool_name,
                                                                    args,
                                                                    call_id,
                                                                    result=part.content,
                                                                )
                                                                args_str = str(args)[:1000] if args else ""
                                                                print(f"\033[97;48;5;166m⚙️ [Tool Exec] {tool_name}({args_str})\033[0m")
                                                                if gcs_audit_logger:
                                                                    await gcs_audit_logger.log("tool_call", {
                                                                        "tool": tool_name,
                                                                        "args_preview": args_str,
                                                                    })
                                                                pending_tool_calls.remove((tool_name, args, call_id))
                                                                break
                                    except Exception:
                                        pass
                                if verbose:
                                    print(f"\n\033[48;5;125mVERBOSE> ✅ [is_end_node]\033[0m {str(node.data)[:1000]}")

                    except (asyncio.CancelledError, anyio.ClosedResourceError):
                        cancelled = True
                        raise
                    finally:
                        await _finalize_turn(
                            run,
                            message_history,
                            session_usage,
                            cancelled,
                            gcs_audit_logger,
                            verbose,
                            turn_logger,
                        )

            task = asyncio.ensure_future(_run())

            def _cancel(_):
                nonlocal cancelled
                cancelled = True
                task.cancel()

            loop.add_signal_handler(signal.SIGINT, _cancel, None)
            try:
                await task
            except (asyncio.CancelledError, anyio.ClosedResourceError):
                cancelled = True
                logger.info("Turn cancelled by user (Ctrl+C)")
            except Exception as e:
                logger.warning("Turn failed; returning to REPL: %s", e, exc_info=True)
                print(
                    f"\n\033[43;30m⚠️  Turn failed: {type(e).__name__}: {e}\033[0m"
                )
            finally:
                loop.remove_signal_handler(signal.SIGINT)

            if cancelled:
                print("\n\033[41m⚠️  Cancelled.\033[0m")

            if gcs_audit_logger:
                gcs_uri = await gcs_audit_logger.flush_turn()
                if gcs_uri:
                    print(f"\033[48;5;240m📝 [Logged]\033[0m {gcs_uri}")

            history_to_save = _strip_thinking_parts_for_persistence(message_history)
            save_session(history_to_save, session_usage, session_file)
            print(f"\033[48;5;240m💾 [Saved]\033[0m {session_file}")

    print(f"\n\033[48;5;24m📊 [Session Total]\033[0m {format_session_usage(session_usage)}")

    # ── Generate session HTML report ──
    print("\n📄 Generating session HTML report ...")
    html_path, html_gcs_uri = turn_logger.generate_session_html()
    if html_path:
        print(f"   📄 Local: {html_path}")
    if html_gcs_uri:
        print(f"   ☁️  GCS:   {html_gcs_uri}")
        web_url = _gcs_uri_to_web_url(html_gcs_uri)
        if web_url:
            print(f"   🌐 Web:   {web_url}")
    elif html_path:
        print("   ☁️  GCS:   (skipped — GCS not configured)")

    if gcs_audit_logger:
        print("\n📝 Flushing session-end log to GCS ...")
        exit_uri = await gcs_audit_logger.close(extra=session_usage)
        if exit_uri:
            print(f"   ✅ {exit_uri}")
        logger.info("Session log flushed")


# ---------------------------------------------------------------------------
# Turn finalisation (shared by normal completion & cancellation paths)
# ---------------------------------------------------------------------------

async def _finalize_turn(
    run,
    message_history: list,
    session_usage: dict,
    cancelled: bool,
    gcs_audit_logger: GCSLogger | None,
    verbose: bool,
    turn_logger: TurnLogger | None = None,
) -> None:
    """Save history & print usage after every turn (normal or cancelled)."""
    try:
        result = run.result
        message_history.extend(result.new_messages())

        if not cancelled:
            print(f"\n\033[97;48;5;18m⚛️ [Agent]\033[0m {result.output}")

        usage = result.usage()
        accumulate_session_usage(session_usage, usage)
        label = "(cancelled) " if cancelled else ""
        query = turn_logger.current_query if turn_logger else 0
        print(f"\n\033[48;5;240m📊 [Usage {label}\033[0m{format_usage_line(usage, query=query)}")
        print(f"\033[48;5;240m📊 [Session \033[0m{format_session_usage(session_usage)}")

        # Log query-level usage to turn log
        if turn_logger:
            turn_logger.log_usage(build_usage_dict(usage), is_query_total=True)

        if gcs_audit_logger:
            if cancelled:
                await gcs_audit_logger.log("turn_cancelled", build_usage_dict(usage))
            else:
                await gcs_audit_logger.log("agent_response", {"response": result.output})
                await gcs_audit_logger.log("token_usage", build_usage_dict(usage))

    except Exception:
        _save_partial_history(run, message_history)
        await _log_partial_usage(run, message_history, session_usage, gcs_audit_logger, turn_logger)


def _save_partial_history(run, message_history: list) -> None:
    """Best-effort partial history preservation after cancellation."""
    try:
        partial = run.all_messages()
        existing_count = len(message_history)
        new_msgs = partial[existing_count:]
        if new_msgs:
            message_history.extend(new_msgs)
            logger.info("Saved %d partial messages from cancelled turn", len(new_msgs))

        _sanitize_history(message_history)

        if new_msgs:
            print(
                f"\n\033[48;5;240m📊 [Partial]\033[0m "
                f"Saved {len(new_msgs)} messages from cancelled turn"
            )
    except Exception as inner_err:
        logger.warning("Could not save partial history: %s", inner_err)


async def _log_partial_usage(
    run,
    message_history: list,
    session_usage: dict,
    gcs_audit_logger: GCSLogger | None,
    turn_logger: TurnLogger | None = None,
) -> None:
    """Best-effort usage logging after cancellation."""
    try:
        usage = run.usage()
        accumulate_session_usage(session_usage, usage)
        query = turn_logger.current_query if turn_logger else 0
        print(
            f"\n\033[48;5;240m📊 [Usage (cancelled) \033[0m"
            f"{format_usage_line(usage, query=query)}"
        )
        print(f"\033[48;5;240m📊 [Session \033[0m{format_session_usage(session_usage)}")
        if gcs_audit_logger:
            await gcs_audit_logger.log("turn_cancelled", build_usage_dict(usage))
    except Exception as usage_err:
        logger.warning("Could not retrieve usage for cancelled turn: %s", usage_err)
