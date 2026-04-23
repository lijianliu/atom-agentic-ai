"""Main Slack bot that polls channels and invokes the Atom agent."""

import asyncio
import logging
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from .agent_invoker import AgentInvoker
from .config import SlackBotConfig
from .slack_client import SlackClient

# GCS audit logger — imported lazily so bot still works without google-cloud-storage
try:
    from gcs_audit_logger import GCSLogger, set_active_gcs_logger
except ImportError:
    GCSLogger = None  # type: ignore[misc,assignment]
    set_active_gcs_logger = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# DEBUG: log which gcs_audit_logger module bot.py resolved
_gcs_mod_in_bot = sys.modules.get("gcs_audit_logger")
_agent_gcs_mod_in_bot = sys.modules.get("agent.gcs_audit_logger")
logger.warning(
    "DEBUG_IMPORT bot.py: gcs_audit_logger module_id=%d, "
    "agent.gcs_audit_logger module_id=%d, SAME=%s, "
    "GCSLogger=%s, set_active_gcs_logger=%s",
    id(_gcs_mod_in_bot) if _gcs_mod_in_bot else 0,
    id(_agent_gcs_mod_in_bot) if _agent_gcs_mod_in_bot else 0,
    _gcs_mod_in_bot is _agent_gcs_mod_in_bot if (_gcs_mod_in_bot and _agent_gcs_mod_in_bot) else "N/A",
    GCSLogger, set_active_gcs_logger,
)

# Resolve host info once
_HOSTNAME = socket.gethostname()


def _format_footer(
    elapsed_s: float,
    usage: dict[str, Any],
    status: str = "✅",
) -> str:
    """Build a compact metadata footer for Slack."""
    mins, secs = divmod(int(elapsed_s), 60)
    dur = f"{mins}m{secs}s" if mins else f"{secs}s"

    in_t = usage.get("input_tokens", 0)
    out_t = usage.get("output_tokens", 0)
    reqs = usage.get("requests", 0)
    cache_read = usage.get("cache_read_tokens", 0)
    cache_pct = f"{cache_read / in_t * 100:.0f}%" if in_t else "0%"

    return (
        f"{status} {dur} | "
        f"tokens: {in_t:,} in / {out_t:,} out | "
        f"{reqs} reqs | cache {cache_pct} | "
        f"{_HOSTNAME}"
    )


class SlackBot:
    """Slack bot that monitors channels and responds using the Atom agent."""

    def __init__(
        self,
        config: SlackBotConfig,
        system_prompt_file: Path | None = None,
    ) -> None:
        self.config = config
        logger.info("Config: %s", config)
        self.client = SlackClient(config.bot_token)
        self.invoker = AgentInvoker(
            use_openai=config.use_openai,
            mcp_url=config.mcp_url,
            system_prompt_file=system_prompt_file,
        )
        self._running = False
        self._channel_ids: list[str] = []

        if system_prompt_file:
            logger.info("System prompt file: %s", system_prompt_file)

        # GCS audit logging (None if env var not set or SDK not installed)
        self._gcs_logger = None
        if GCSLogger is not None:
            self._gcs_logger = GCSLogger.from_env()
            if self._gcs_logger:
                # Register at module level so upload_output_file (in
                # local_tools.py) can find the active GCS logger instance.
                logger.warning(
                    "DEBUG_GCS bot.py: about to call set_active_gcs_logger, "
                    "gcs_logger=%s (id=%d), "
                    "set_active_gcs_logger func id=%d, "
                    "gcs_audit_logger module in sys.modules: %s",
                    type(self._gcs_logger).__name__, id(self._gcs_logger),
                    id(set_active_gcs_logger),
                    {k: id(v) for k, v in sys.modules.items() if "gcs_audit_logger" in k},
                )
                set_active_gcs_logger(self._gcs_logger)
                logger.info("GCS audit logging enabled → %s", self._gcs_logger.gcs_uri)
            else:
                logger.info("GCS audit logging disabled (ATOM_AUDIT_LOG_GCS_PATH not set)")

    # ── Channel setup ──────────────────────────────────────────────────

    def _setup_channels(self) -> bool:
        """Resolve channel names to IDs and join them."""
        for channel in self.config.channels:
            try:
                channel_id = self.client.get_channel_id(channel)
                if channel_id:
                    self._channel_ids.append(channel_id)
                    self.client.join_channel(channel_id)
                    logger.info("Monitoring channel: %s (%s)", channel, channel_id)
                else:
                    logger.error("Could not find channel: %s", channel)
            except RuntimeError as e:
                logger.error("Failed to setup channel %s: %s", channel, e)

        return len(self._channel_ids) > 0

    # ── Message handling ──────────────────────────────────────────────

    def _should_respond(self, message: dict[str, Any]) -> bool:
        """Check if bot should respond to this message."""
        text = message.get("text", "")

        if self.config.trigger_prefix:
            return text.startswith(self.config.trigger_prefix)

        # No trigger prefix — respond to all messages in monitored channels
        return True

    def _extract_prompt(self, message: dict[str, Any]) -> str:
        """Extract the user prompt from the message text."""
        text = message.get("text", "")

        # Remove bot mention
        bot_id = self.client.get_bot_user_id()
        text = text.replace(f"<@{bot_id}>", "").strip()

        # Remove trigger prefix
        if self.config.trigger_prefix and text.startswith(self.config.trigger_prefix):
            text = text[len(self.config.trigger_prefix) :].strip()

        return text

    async def _handle_message(self, channel_id: str, message: dict[str, Any]) -> None:
        """Handle a single incoming message."""
        if not self._should_respond(message):
            return

        prompt = self._extract_prompt(message)
        if not prompt:
            return

        msg_ts = message.get("ts", "")
        thread_ts = message.get("thread_ts") or msg_ts
        user_id = message.get("user", "")
        username = self.client.get_user_name(user_id) if user_id else "unknown"

        # Append Slack formatting instructions
        prompt += (
            "\n\nPlease format your response using Slack markdown "
            "(bold with *text*, italic with _text_, code with `text`, "
            "bullet points with \u2022). Please keep response extremely concise "
            "and short because nobody will like to read a large amount of verbose "
            "output generated by AI!!! Try to answer in one sentence only, do not repeat user's request in the answer."
            "If the response has hyperlink, use slick markdown to remove the web site host name and full path, "
            "only leave the leaf path as the display name. Stop using any emoji unless it is absolutely needed."
        )

        logger.info("Processing message from %s: %s", username, prompt[:80])

        # Audit log: user prompt
        if self._gcs_logger:
            await self._gcs_logger.log("slack_user_prompt", {
                "user": username,
                "user_id": user_id,
                "channel": channel_id,
                "thread_ts": thread_ts,
                "prompt": prompt,
            })

        # Post a "thinking" indicator — this message will show streaming
        # progress and finally be updated with usage info.
        progress_ts = self.client.send_message(
            channel_id,
            f"\u23f3 Processing request from {username}...",
            thread_ts=thread_ts,
        )

        # Build a progress callback that updates the progress message
        async def _on_progress(text: str) -> None:
            if progress_ts:
                self.client.update_message(channel_id, progress_ts, text)

        # Invoke the agent (with streaming + disk-persisted session)
        result = await self.invoker.invoke(
            prompt=prompt,
            thread_ts=thread_ts,
            on_progress=_on_progress,
        )

        response = result["response"]
        usage = result.get("usage", {})
        usage_line = result.get("usage_line", "")
        elapsed = result.get("elapsed_s", 0)
        error = result.get("error")

        # Build footer and update the progress message with usage info
        status = "\u274c" if error else "\u2705"
        footer = _format_footer(elapsed, usage, status=status)
        final_progress = f"{footer}\n{usage_line}" if usage_line else footer

        if progress_ts:
            self.client.update_message(channel_id, progress_ts, final_progress)

        # Send the actual response in thread
        self.client.send_message(channel_id, response, thread_ts=thread_ts)

        # Audit log: agent response
        if self._gcs_logger:
            await self._gcs_logger.log("slack_agent_response", {
                "user": username,
                "channel": channel_id,
                "thread_ts": thread_ts,
                "response_len": len(response),
                "elapsed_s": elapsed,
                "usage": usage,
                "error": error,
            })

    # ── Polling loop ───────────────────────────────────────────────────

    async def _poll_channels(self) -> None:
        """Poll all channels for new messages."""
        for channel_id in self._channel_ids:
            try:
                messages = self.client.get_new_messages(channel_id)
                if messages:
                    logger.debug(
                        "Got %d new messages from %s", len(messages), channel_id,
                    )
                for message in messages:
                    await self._handle_message(channel_id, message)
            except Exception as e:
                logger.error("Error polling channel %s: %s", channel_id, e)

    async def _run_socket_mode(self) -> None:
        """Run using Socket Mode for real-time events."""
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse

        socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=None,
        )

        async def handle_socket_event(
            client: SocketModeClient, req: SocketModeRequest,
        ) -> None:
            response = SocketModeResponse(envelope_id=req.envelope_id)
            await client.send_socket_mode_response(response)

            if req.type != "events_api":
                return

            event = req.payload.get("event", {})
            event_type = event.get("type", "")

            if event_type not in ("message", "app_mention"):
                return

            subtype = event.get("subtype")
            if subtype in ("bot_message", "message_changed", "message_deleted"):
                return
            if event.get("bot_id") or event.get("bot_profile"):
                return
            if event.get("user") == self.client.get_bot_user_id():
                return

            channel_id = event.get("channel", "")
            if channel_id not in self._channel_ids:
                return

            logger.info("⚡ Socket Mode: processing message in %s", channel_id)
            await self._handle_message(channel_id, event)

        socket_client.socket_mode_request_listeners.append(handle_socket_event)

        logger.info("⚡ Starting Socket Mode connection...")
        await socket_client.connect()
        logger.info("⚡ Socket Mode connected!")

        while self._running:
            await asyncio.sleep(1)

        await socket_client.close()

    # ── Main entry ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main run loop."""
        errors = self.config.validate()
        if errors:
            for error in errors:
                logger.error("Config error: %s", error)
            sys.exit(1)

        if not self._setup_channels():
            logger.error("No valid channels to monitor")
            sys.exit(1)

        self._running = True

        # GCS audit: warm token + log session start
        if self._gcs_logger:
            await self._gcs_logger.warm_token()
            await self._gcs_logger.log("slack_session_start", {
                "channels": self.config.channels,
                "channel_ids": self._channel_ids,
                "mode": "socket" if self.config.app_token else "polling",
                "poll_interval": self.config.poll_interval,
                "use_openai": self.config.use_openai,
                "mcp_url": self.config.mcp_url,
            })

        logger.info(
            "🤖 Slack bot started! Polling every %.1fs across %d channel(s)",
            self.config.poll_interval,
            len(self._channel_ids),
        )

        def signal_handler(sig, frame):
            logger.info("Shutdown signal received...")
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if self.config.app_token:
            logger.info("⚡ App token found — using Socket Mode")
            await self._run_socket_mode()
        else:
            logger.info("📡 Using polling mode (%.1fs interval)", self.config.poll_interval)
            while self._running:
                try:
                    await self._poll_channels()
                    await asyncio.sleep(self.config.poll_interval)
                except Exception as e:
                    logger.error("Poll loop error: %s", e)
                    await asyncio.sleep(self.config.poll_interval)

        # Clean up MCP connection + GCS audit
        await self.invoker.close()
        if self._gcs_logger:
            await self._gcs_logger.close()
        logger.info("🛑 Slack bot stopped")

    def stop(self) -> None:
        """Stop the bot gracefully."""
        self._running = False
