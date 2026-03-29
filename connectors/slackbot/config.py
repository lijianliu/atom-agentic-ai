"""Configuration for Slack bot connector."""

import os
from dataclasses import dataclass, field


@dataclass
class SlackBotConfig:
    """Configuration for the Slack bot."""

    # Slack credentials
    bot_token: str = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", ""))
    app_token: str = field(default_factory=lambda: os.getenv("SLACK_APP_TOKEN", ""))

    # Channels to monitor (comma-separated in env, or pass as list)
    channels: list[str] = field(default_factory=list)

    # Polling interval in seconds
    poll_interval: float = 3.0

    # Bot mention trigger (if empty, responds to all messages)
    trigger_prefix: str = ""

    # Whether to use OpenAI model instead of Anthropic
    use_openai: bool = field(
        default_factory=lambda: os.getenv("ATOM_USE_OPENAI", "").lower() in ("1", "true", "yes"),
    )

    # MCP server URL
    mcp_url: str = field(
        default_factory=lambda: os.getenv("MCP_URL", "http://127.0.0.1:9100/sse"),
    )

    def __post_init__(self) -> None:
        """Load channels from env if not provided."""
        if not self.channels:
            env_channels = os.getenv("SLACK_CHANNELS", "")
            if env_channels:
                self.channels = [c.strip() for c in env_channels.split(",") if c.strip()]

    def validate(self) -> list[str]:
        """Validate config and return list of errors."""
        errors = []
        if not self.bot_token:
            errors.append("SLACK_BOT_TOKEN is required")
        if not self.channels:
            errors.append("At least one channel is required (set SLACK_CHANNELS)")
        return errors

    @staticmethod
    def _obfuscate(token: str) -> str:
        """Obfuscate a token, showing only first 4 and last 4 chars."""
        if not token:
            return "<not set>"
        if len(token) <= 12:
            return "****"
        return f"{token[:4]}...{token[-4:]}"

    def __str__(self) -> str:
        return (
            f"SlackBotConfig("
            f"bot_token={self._obfuscate(self.bot_token)!r}, "
            f"app_token={self._obfuscate(self.app_token)!r}, "
            f"channels={self.channels!r}, "
            f"poll_interval={self.poll_interval}, "
            f"trigger_prefix={self.trigger_prefix!r}, "
            f"use_openai={self.use_openai}, "
            f"mcp_url={self.mcp_url!r})"
        )
