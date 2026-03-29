"""Entry point for running the Slack bot as a module.

Usage:
    python -m connectors.slackbot -v
    python -m connectors.slackbot --channels C0AG0DYHLA1 -v
    python -m connectors.slackbot --openai -v
"""

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from .bot import SlackBot
from .config import SlackBotConfig

_LOG_DIR = Path.home() / ".config" / "atom-agentic-ai" / "logs"
_LOG_FILE = _LOG_DIR / "slackbot.log"
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB per file
_BACKUP_COUNT = 4              # keep slackbot.log.1 … .4

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(verbose: bool = False) -> None:
    """Configure console + rotating file logging."""
    console_level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let handlers filter

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE_FMT))
    root.addHandler(ch)

    # Rotating file handler (DEBUG+ — captures everything)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE_FMT))
    root.addHandler(fh)

    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.INFO)

    logging.getLogger(__name__).info("Logging to file: %s", _LOG_FILE)


def main() -> None:
    """Main entry point."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Slack bot connector for Atom agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  SLACK_BOT_TOKEN    Bot User OAuth Token (xoxb-...)
  SLACK_CHANNELS     Comma-separated channel IDs
  SLACK_APP_TOKEN    App-level token for Socket Mode (xapp-...)
  MCP_URL            MCP server URL (default: http://127.0.0.1:9100/sse)

Example:
  export SLACK_BOT_TOKEN="xoxb-your-token"
  export SLACK_CHANNELS="C0AG0DYHLA1"
  python -m connectors.slackbot -v
""",
    )
    parser.add_argument(
        "--channels", "-c", type=str,
        help="Comma-separated channels to monitor (overrides env)",
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=3.0,
        help="Polling interval in seconds (default: 3)",
    )
    parser.add_argument(
        "--trigger", "-t", type=str, default="",
        help="Trigger prefix (e.g., '!atom'). If not set, responds to mentions.",
    )
    parser.add_argument(
        "--openai", action="store_true",
        help="Use OpenAI model instead of Anthropic",
    )
    parser.add_argument(
        "--mcp-url", type=str, default=None,
        help="MCP server SSE URL (default: from MCP_URL env or http://127.0.0.1:9100/sse)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--check-scopes", action="store_true",
        help="Check available OAuth scopes and exit",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Check scopes mode
    if args.check_scopes:
        _check_scopes()
        return

    # Build config
    config = SlackBotConfig(
        poll_interval=args.interval,
        trigger_prefix=args.trigger,
    )

    if args.channels:
        config.channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    if args.openai:
        config.use_openai = True
    if args.mcp_url:
        config.mcp_url = args.mcp_url

    # Create and run bot
    bot = SlackBot(config)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n\ud83d\udc4b Bye!")
        sys.exit(0)


def _check_scopes() -> None:
    """Check Slack OAuth scopes and exit."""
    from .slack_client import SlackClient

    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        print("\u274c No SLACK_BOT_TOKEN found in environment")
        sys.exit(1)

    print("\ud83d\udd0d Checking Slack OAuth scopes...\n")
    client = SlackClient(token)
    results = client.check_scopes()

    if "_bot_name" in results:
        print(f"\ud83e\udd16 Bot: {results.pop('_bot_name')}")
    if "_team" in results:
        print(f"\ud83c\udfe2 Team: {results.pop('_team')}")
    print()

    required_scopes = [
        "channels:read", "channels:history", "channels:join",
        "chat:write", "reactions:write",
    ]

    all_good = True
    for scope in required_scopes:
        status = results.get(scope, "unknown")
        if status is True:
            print(f"  \u2705 {scope}")
        elif "needs channel" in str(status):
            print(f"  \u26a0\ufe0f  {scope} {status}")
        else:
            print(f"  \u274c {scope}: {status}")
            all_good = False

    print()
    if all_good:
        print("\u2705 All testable scopes look good!")
    else:
        print("\ud83d\udc46 Add missing scopes in Slack App settings, then reinstall the app!")

    sys.exit(0 if all_good else 1)


if __name__ == "__main__":
    main()
