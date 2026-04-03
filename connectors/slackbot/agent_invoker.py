"""Agent invoker — bridges Slack messages to the Atom pydantic-ai Agent.

Uses disk-based session persistence (via session_store) keyed by Slack
thread_ts so conversations survive bot restarts.  Streams responses via
agent.iter() and calls an optional progress callback during streaming.
"""

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from pydantic_ai import Agent
from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)

logger = logging.getLogger(__name__)

# Add project root (so `agent` resolves as a package) and then the agent/
# directory (so agent.py's bare imports like `from model import …` resolve).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_AGENT_DIR = str(_PROJECT_ROOT / "agent")
if _AGENT_DIR not in sys.path:
    sys.path.append(_AGENT_DIR)  # append, not insert — project root must win

# Session directory for Slack threads
_SLACK_SESSIONS_DIR = Path.home() / ".config" / "atom-agentic-ai" / "sessions" / "slack"

# Minimum interval between Slack progress updates (avoid rate-limiting)
_PROGRESS_UPDATE_INTERVAL_S = 2.0


ProgressCallback = Callable[[str], Awaitable[None]]
"""async callback(text) — called with the last 500 chars of streaming output."""


def _session_path_for_thread(thread_ts: str) -> Path:
    """Derive a session file path from a Slack thread timestamp."""
    _SLACK_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = thread_ts.replace(".", "_")
    return _SLACK_SESSIONS_DIR / f"{safe_name}.session.json"


class AgentInvoker:
    """Invokes the pydantic-ai Atom Agent with disk-persisted sessions."""

    def __init__(
        self,
        use_openai: bool = False,
        mcp_url: str = "http://127.0.0.1:9100/sse",
    ) -> None:
        self.use_openai = use_openai
        self.mcp_url = mcp_url
        self._agent: Agent | None = None
        self._agent_entered = False
        # Per-thread locks to serialise concurrent requests on the same
        # Slack thread and prevent session file race conditions.
        self._thread_locks: dict[str, asyncio.Lock] = {}

    async def _ensure_agent(self) -> None:
        """Lazily build the agent and enter its async context once."""
        if self._agent_entered:
            return

        from agent.agent import build_agent

        self._agent = build_agent(
            use_openai=self.use_openai,
            mcp_url=self.mcp_url,
        )
        await self._agent.__aenter__()
        self._agent_entered = True
        logger.info(
            "Agent built & context opened (openai=%s, mcp=%s)",
            self.use_openai, self.mcp_url,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def invoke(
        self,
        prompt: str,
        thread_ts: str = "",
        timeout: float = 300.0,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Invoke the agent with streaming + disk-persisted session.

        Args:
            prompt:      User message.
            thread_ts:   Slack thread timestamp (used as session key).
            timeout:     Max wall-clock seconds.
            on_progress: Async callback receiving the last 500 chars of
                         accumulated streaming text, throttled to avoid
                         Slack rate limits.

        Returns:
            dict with keys: response, usage, usage_line, elapsed_s, error
        """
        await self._ensure_agent()
        assert self._agent is not None

        # Serialise requests per thread to prevent session file races
        # when multiple messages arrive before the first one finishes.
        lock_key = thread_ts or "_default"
        if lock_key not in self._thread_locks:
            self._thread_locks[lock_key] = asyncio.Lock()

        async with self._thread_locks[lock_key]:
            return await self._invoke_locked(
                prompt=prompt,
                thread_ts=thread_ts,
                timeout=timeout,
                on_progress=on_progress,
            )

    async def _invoke_locked(
        self,
        prompt: str,
        thread_ts: str,
        timeout: float,
        on_progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        """Inner invoke — called while holding the per-thread lock."""
        assert self._agent is not None

        # -- Load or create session --
        from session_store import load_session, save_session
        from usage_helpers import format_usage_line, new_session_usage

        session_file = _session_path_for_thread(thread_ts or "_default")
        message_history, session_usage = load_session(session_file)

        if message_history:
            logger.info(
                "Resumed session %s (%d messages, %d turns)",
                session_file.name, len(message_history), session_usage["turns"],
            )
        else:
            logger.info("New session %s", session_file.name)

        start = time.time()

        try:
            result_output, usage_info, usage_line = await asyncio.wait_for(
                self._stream_run(
                    prompt, message_history, on_progress,
                ),
                timeout=timeout,
            )

            # -- Persist session to disk --
            save_session(message_history, session_usage, session_file)
            logger.info("Session saved → %s", session_file)

            elapsed = time.time() - start
            return {
                "response": result_output,
                "usage": usage_info,
                "usage_line": usage_line,
                "elapsed_s": elapsed,
                "error": None,
            }

        except asyncio.TimeoutError:
            elapsed = time.time() - start
            return {
                "response": f"❌ Request timed out after {timeout:.0f}s. Try a simpler question.",
                "usage": {},
                "usage_line": "",
                "elapsed_s": elapsed,
                "error": "timeout",
            }
        except Exception as e:
            logger.exception("Agent invocation failed")
            elapsed = time.time() - start
            return {
                "response": f"❌ Error: {e}",
                "usage": {},
                "usage_line": "",
                "elapsed_s": elapsed,
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Streaming iteration
    # ------------------------------------------------------------------

    async def _stream_run(
        self,
        prompt: str,
        message_history: list,
        on_progress: ProgressCallback | None,
    ) -> tuple[str, dict[str, Any], str]:
        """Run agent.iter() with streaming, call on_progress with last 500 chars.

        Returns:
            (response_text, usage_dict, usage_line)
        """
        from usage_helpers import format_usage_line, accumulate_session_usage

        assert self._agent is not None
        accumulated_text = ""
        last_progress_time = 0.0
        usage_line = ""

        async with self._agent.iter(prompt, message_history=message_history) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as stream:
                        async for event in stream:
                            # Collect streamed text
                            if isinstance(event, PartStartEvent):
                                if isinstance(event.part, TextPart) and event.part.content:
                                    accumulated_text += event.part.content
                            elif isinstance(event, PartDeltaEvent):
                                if isinstance(event.delta, TextPartDelta):
                                    accumulated_text += event.delta.content_delta

                            # Throttled progress callback
                            if on_progress and accumulated_text:
                                now = time.time()
                                if now - last_progress_time >= _PROGRESS_UPDATE_INTERVAL_S:
                                    last_progress_time = now
                                    snippet = accumulated_text[-500:]
                                    await on_progress(f"⏳ Streaming…\n```\n{snippet}\n```")

                        # Capture per-stream usage
                        try:
                            usage_line = format_usage_line(stream.usage())
                        except Exception:
                            pass

                elif Agent.is_call_tools_node(node):
                    # Report tool calls in progress
                    if on_progress:
                        tool_names = [
                            p.tool_name
                            for p in node.model_response.parts
                            if isinstance(p, ToolCallPart)
                        ]
                        if tool_names:
                            snippet = accumulated_text[-400:] if accumulated_text else ""
                            tools_str = ", ".join(tool_names)
                            msg = f"⚙️ Running tools: {tools_str}"
                            if snippet:
                                msg += f"\n```\n{snippet}\n```"
                            await on_progress(msg)

            # -- Finalise --
            result = run.result
            message_history.extend(result.new_messages())

            # Cap history to prevent unbounded growth
            if len(message_history) > 100:
                message_history[:] = message_history[-100:]

            output = str(result.output) if result.output else "(No response)"
            output = self._clean_output(output)

            usage = result.usage()
            usage_info = self._extract_usage(usage)

            # Build final usage line (2-line format)
            usage_line = f"📊 [Usage {format_usage_line(usage)}"

            return output, usage_info, usage_line

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(usage) -> dict[str, Any]:
        """Pull token usage from a pydantic-ai Usage object."""
        try:
            return {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "requests": getattr(usage, "requests", 0) or 0,
                "cache_read_tokens": getattr(usage, "cache_read_tokens", 0) or 0,
                "cache_write_tokens": getattr(usage, "cache_write_tokens", 0) or 0,
            }
        except Exception:
            return {}

    @staticmethod
    def _clean_output(output: str) -> str:
        """Strip ANSI codes, thinking blocks, and excessive whitespace."""
        output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)
        output = re.sub(r"\x1b\]52;c;[^\x07]*(?:\x07|$)", "", output)
        output = re.sub(r"\x1b\]52;[^\x1b]*", "", output)
        output = re.sub(
            r"<(?:think|thinking)>.*?</(?:think|thinking)>",
            "", output, flags=re.DOTALL | re.IGNORECASE,
        )
        output = re.sub(r"\n{3,}", "\n\n", output)
        return output.strip() or "(No response)"

    async def close(self) -> None:
        """Close the agent context (MCP connection)."""
        if self._agent is not None and self._agent_entered:
            try:
                await self._agent.__aexit__(None, None, None)
            except Exception:
                logger.debug("Agent context close error (benign)", exc_info=True)
            self._agent_entered = False
