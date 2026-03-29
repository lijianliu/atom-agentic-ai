# Slack Bot Connector for Atom Agent

A Slack bot that monitors channels and responds using the Atom pydantic-ai agent
with MCP sandbox tools.

## Features

- ⚡ Socket Mode support (real-time via WebSocket when `SLACK_APP_TOKEN` is set)
- 🔄 Polling fallback every 3 seconds (configurable)
- 📢 Supports multiple channels
- 🧵 Thread-aware responses with multi-turn memory
- 🎯 Responds to mentions or custom trigger prefix
- 🔧 All tools run inside the hardened Docker sandbox via MCP

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌───────────────┐
│   Slack     │───▶│  SlackBot   │───▶│ pydantic-ai │───▶│  MCP Sandbox  │
│  Channel    │◀───│  (socket)   │◀───│   Agent     │◀───│  (Docker)     │
└─────────────┘     └─────────────┘     └─────────────┘     └───────────────┘
```

## Setup

### 1. Create a Slack App

1. Go to [Slack API](https://api.slack.com/apps) and create a new app
2. Under **OAuth & Permissions**, add these Bot Token scopes:
   - `channels:read` — List channels
   - `channels:history` — Read messages
   - `channels:join` — Join channels
   - `chat:write` — Send messages
   - `reactions:write` — Add reactions
3. *(Optional)* For Socket Mode: enable it under **Socket Mode** and generate
   an App-Level Token with `connections:write` scope (`xapp-...`)
4. Install the app to your workspace
5. Copy the **Bot User OAuth Token** (`xoxb-...`)

### 2. Configure Environment

Add the following to `~/.config/atom-agentic-ai/env.sh` (this file is
automatically sourced by `run.sh` before the bot starts):

```bash
# ── Slack Bot Config ──────────────────────────────────────────
# Required: Bot User OAuth Token (starts with xoxb-)
export SLACK_BOT_TOKEN="xoxb-your-bot-token-here"

# Required: Comma-separated channel IDs to monitor
# Tip: use channel IDs (not names) to avoid extra API calls
export SLACK_CHANNELS="C0AG0DYHLA1"

# Optional: App-level token for Socket Mode (starts with xapp-)
# If set, bot uses real-time WebSocket instead of polling
export SLACK_APP_TOKEN="xapp-your-app-token-here"
```

> **Important:** Every variable must use `export` so it's visible to the
> Python child process. Without `export`, the bot will see `<not set>`.

### 3. Run the Bot

`run.sh` handles everything — sandbox startup, dependency sync, env loading:

```bash
./run.sh --slackbot        # Normal mode
./run.sh --slackbot -v     # Verbose logging
./run.sh --slackbot --openai  # Use OpenAI model
```

Alternatively, run the module directly (requires sandbox + env to be set up
manually):

```bash
.venv/bin/python -m connectors.slackbot -v
```

## CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `-c, --channels` | Comma-separated channels | `SLACK_CHANNELS` env |
| `-i, --interval` | Poll interval (seconds) | 3.0 |
| `-t, --trigger` | Trigger prefix (e.g. `!atom`) | None (responds to all) |
| `--openai` | Use OpenAI model | False |
| `--mcp-url` | MCP server URL | `MCP_URL` env |
| `-v, --verbose` | Debug logging | False |
| `--check-scopes` | Check OAuth scopes and exit | — |

## Environment Variables

| Variable | Required | Format | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | `xoxb-...` | Bot User OAuth Token |
| `SLACK_CHANNELS` | ✅ | `C0ABC,C0DEF` | Comma-separated channel IDs |
| `SLACK_APP_TOKEN` | ❌ | `xapp-...` | Enables Socket Mode |
| `MCP_URL` | ❌ | URL | MCP server (default `http://127.0.0.1:9100/sse`) |
| `ATOM_USE_OPENAI` | ❌ `/`false` | Use OpenAI instead of Anthropic |
| `LLM_API_KEY` | ✅ | JWT | Element LLM Gateway key |
| `MODEL_NAME` | ✅ | string | Model name for the gateway |
| `LLM_GATEWAY_URL` | ✅ | URL | LLM Gateway endpoint |

## Files

```
connectors/slackbot/
├── __init__.py        # Package init
├── __main__.py        # CLI entry point
├── agent_invoker.py   # Bridges Slack → pydantic-ai Agent
├── bot.py             # Main bot logic (polling + socket mode)
├── config.py          # Configuration dataclass
├── slack_client.py    # Slack API client
└── README.md          # This file
```

## Troubleshooting

### Bot token shows `<not set>`

Make sure you used `export` in `env.sh`:
```bash
# ✅ Correct — visible to child processes
export SLACK_BOT_TOKEN="xoxb-..."

# ❌ Wrong — shell-local only, Python won't see it
SLACK_BOT_TOKEN="xoxb-..."
```

### `method_not_supported_for_channel_type`

This is harmless. It means the channel is a type (DM, private) that doesn't
support `conversations.join`. The bot will still monitor it if already a member.

### `Cannot reopen a client instance`

The MCP connection was closed unexpectedly. Restart the bot — the agent context
is now kept alive for the bot's entire lifetime.

### Bot Not Responding

1. Check MCP sandbox is running: `curl http://127.0.0.1:9100/sse`
2. Check bot is running: `ps aux | grep connectors.slackbot`
3. Run with `-v` for debug logs
4. Ensure bot is invited to the channel
5. Messages sent BEFORE bot starts are skipped

### Rate Limiting

Use channel IDs instead of names in `SLACK_CHANNELS`:
```bash
# ❌ Causes extra API calls to resolve names
SLACK_CHANNELS=#general,#dev-help

# ✅ No resolution needed
SLACK_CHANNELS=C0AG0DYHLA1,C0AG0DYHLA2
```
