"""Agent invoker — bridges Slack messages to the Atom pydantic-ai Agent.

Unlike the original atom invoker which depends on atom's message bus,
agent manager, and callback system, this version directly uses pydantic-ai's
Agent.run() / Agent.iter() API against the local agent.
"""

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Add project root (so `agent` resolves as a package) and then the agent/
# directory (so agent.py's bare imports like `from model import …` resolve).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_AGENT_DIR = str(_PROJECT_ROOT / "agent")
if _AGENT_DIR not in sys.path:
    sys.path.append(_AGENT_DIR)  # append, not insert — project root must win


class AgentInvoker:
    """Invokes the pydantic-ai Atom Agent and returns text output."""

    def __init__(
        self,
        use_openai: bool = False,
        mcp_url: str = "http://127.0.0.1:9100/sse",
    ) -> None:
        self.use_openai = use_openai
        self.mcp_url = mcp_url
        self._agent = None
        self._agent_ctx = None  # holds the async context manager
        self._message_history: dict[str, list] = {}  # thread_ts → history

    async def _ensure_agent(self):
        """Lazily build the agent and enter its async context once."""
        if self._agent_ctx is not None:
            return

        from agent.agent import build_agent

        self._agent = build_agent(
            use_openai=self.use_openai,
            mcp_url=self.mcp_url,
        )
        # Enter the agent context once — keeps MCP connection alive
        self._agent_ctx = self._agent.__aenter__
        await self._agent.__aenter__()
        logger.info("Agent built & context opened (openai=%s, mcp=%s)", self.use_openai, self.mcp_url)

    async def invoke(
        self,
        prompt: str,
        thread_ts: str = "",
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Invoke the agent with a prompt.

        Uses per-thread message history so Slack threads get multi-turn
        conversations.

        Returns:
            dict with keys: response, usage, elapsed_s, error
        """
        await self._ensure_agent()
        assert self._agent is not None

        # Per-thread conversation history
        history_key = thread_ts or "_default"
        message_history = self._message_history.setdefault(history_key, [])

        start = time.time()

        try:
            result = await asyncio.wait_for(
                self._agent.run(
                    prompt,
                    message_history=message_history,
                ),
                timeout=timeout,
            )

            # Persist new messages into thread history
            message_history.extend(result.new_messages())

            # Cap history to avoid unbounded growth (~50 turns max)
            if len(message_history) > 100:
                message_history[:] = message_history[-100:]

            # Extract response
            output = str(result.output) if result.output else "(No response)"
            output = self._clean_output(output)

            # Extract usage
            usage_info = self._extract_usage(result)

            elapsed = time.time() - start
            return {
                "response": output,
                "usage": usage_info,
                "elapsed_s": elapsed,
                "error": None,
            }

        except asyncio.TimeoutError:
            elapsed = time.time() - start
            return {
                "response": "❌ Request timed out after {:.0f}s. Try a simpler question.".format(timeout),
                "usage": {},
                "elapsed_s": elapsed,
                "error": "timeout",
            }
        except Exception as e:
            logger.exception("Agent invocation failed")
            elapsed = time.time() - start
            return {
                "response": f"❌ Error: {e}",
                "usage": {},
                "elapsed_s": elapsed,
                "error": str(e),
            }

    @staticmethod
    def _extract_usage(result) -> dict[str, Any]:
        """Pull token usage from a pydantic-ai RunResult."""
        try:
            usage = result.usage()
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
        # ANSI escape codes
        output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)
        # Terminal copy-paste artifacts
        output = re.sub(r"\x1b\]52;c;[^\x07]*(?:\x07|$)", "", output)
        output = re.sub(r"\x1b\]52;[^\x1b]*", "", output)
        # <think>/<thinking> blocks
        output = re.sub(
            r"<(?:think|thinking)>.*?</(?:think|thinking)>",
            "", output, flags=re.DOTALL | re.IGNORECASE,
        )
        # Excessive newlines
        output = re.sub(r"\n{3,}", "\n\n", output)
        return output.strip() or "(No response)"

    async def close(self) -> None:
        """Close the agent context (MCP connection)."""
        if self._agent is not None and self._agent_ready:
            try:
                await self._agent.__aexit__(None, None, None)
            except Exception:
                logger.debug("Agent context close error (benign)", exc_info=True)
            self._agent_ready = False

    def clear_history(self, thread_ts: str = "") -> None:
        """Clear conversation history for a thread."""
        key = thread_ts or "_default"
        self._message_history.pop(key, None)
