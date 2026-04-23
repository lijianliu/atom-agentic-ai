#!/usr/bin/env bash
# =============================================================================
# run.sh — Run AtomAI agent with tools in hardened Docker sandbox
# =============================================================================
# This script:
#   1. Ensures the hardened Docker MCP sandbox is running
#   2. Starts atom-command-broker (host-side command execution engine)
#   3. Starts the agent that connects to the MCP server
#
# Usage:
#   ./run.sh                     # New auto-named session
#   ./run.sh --session s.json    # Resume a specific session
#   ./run.sh --openai            # Use OpenAI
#   ./run.sh --verbose           # Verbose mode
#   ./run.sh --skip-update       # Skip uv sync
#   ./run.sh --no-sandbox        # Skip sandbox auto-start
#   ./run.sh --root               # Root mode: local tools, no sandbox
#   ./run.sh --system-prompt p.md  # Custom system prompt file
#   ./run.sh --slackbot           # Run as Slack bot connector
#   ./run.sh --slackbot -v        # Slack bot with verbose logging
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_PORT="${MCP_PORT:-9100}"
MCP_URL="http://127.0.0.1:${MCP_PORT}/sse"

# ---- Parse flags ----
SKIP_UPDATE=false
VERBOSE=false
USE_OPENAI=false
NO_SANDBOX=false
SLACKBOT=false
ROOT_MODE=false
SESSION_FILE=""
SYSTEM_PROMPT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-update|-s) SKIP_UPDATE=true; shift ;;
    --verbose|-v)     VERBOSE=true; shift ;;
    --openai)         USE_OPENAI=true; shift ;;
    --no-sandbox)     NO_SANDBOX=true; shift ;;
    --root)           ROOT_MODE=true; shift ;;
    --slackbot)       SLACKBOT=true; shift ;;
    --session)        SESSION_FILE="${2:-}"; shift 2 ;;
    --system-prompt)  SYSTEM_PROMPT="${2:-}"; shift 2 ;;
    *)                shift ;;
  esac
done

# ---- Load local env if present ----
ENV_FILE="${HOME}/.config/atom-agentic-ai/env.sh"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
fi

# ---- Ensure uv exists ----
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed or not in PATH"
  exit 1
fi

# ---- Create venv if missing (in SCRIPT_DIR, not CWD) ----
if [ ! -d "${SCRIPT_DIR}/.venv" ]; then
  echo "Creating virtual environment (Python 3.13)..."
  (cd "${SCRIPT_DIR}" && uv venv --python 3.13)
  SKIP_UPDATE=false
fi

if [ "$SKIP_UPDATE" = false ]; then
  echo "Syncing dependencies..."
  (cd "${SCRIPT_DIR}" && uv sync --all-groups)
else
  echo "Skipping dependency update (--skip-update)"
fi

# ---- Root mode: skip all sandbox/proxy setup ----
if [ "$ROOT_MODE" = true ]; then
  echo "⚠️  ROOT MODE: Skipping sandbox & proxy, using local tools directly"
elif [ "$NO_SANDBOX" = false ]; then
  # ---- Restart atom-command-broker (replaces old gsutil-proxy) ----
  echo "🔄 Restarting atom-command-broker..."
  "${SCRIPT_DIR}/sandbox/atom-command-broker/broker-ctl.sh" stop 2>/dev/null || true
  "${SCRIPT_DIR}/sandbox/atom-command-broker/broker-ctl.sh" start

  # ---- Start hardened Docker sandbox if not running ----
  CONTAINER_NAME="sandbox-mcp"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
    echo "✅ Hardened sandbox already running (${CONTAINER_NAME})"
  else
    "${SCRIPT_DIR}/sandbox/sandbox.sh" start --port "${MCP_PORT}"
    echo ""
    # Wait for MCP server to be ready
    # NOTE: SSE endpoints keep connections open, so curl's exit code is always
    # non-zero (timeout/28) even when the server IS up. Check HTTP status instead.
    echo "⏳ Waiting for MCP server to be ready..."
    for i in $(seq 1 30); do
      http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:${MCP_PORT}/sse" 2>/dev/null || true)
      if [ "$http_code" = "200" ]; then
        echo "   ✅ MCP server is ready!"
        break
      fi
      if [ "$i" -eq 30 ]; then
        echo "   ⚠️  MCP server may not be ready yet. Trying anyway..."
      fi
      sleep 1
    done
  fi
else
  echo "⚠️  Sandbox auto-start disabled (--no-sandbox)"
fi

# ---- Build common flags ----
COMMON_FLAGS=""
[ "$VERBOSE" = true ]    && COMMON_FLAGS="$COMMON_FLAGS --verbose"
[ "$USE_OPENAI" = true ] && COMMON_FLAGS="$COMMON_FLAGS --openai"
[ "$ROOT_MODE" = true ]  && COMMON_FLAGS="$COMMON_FLAGS --root"
[ -n "$SYSTEM_PROMPT" ]  && COMMON_FLAGS="$COMMON_FLAGS --system-prompt $SYSTEM_PROMPT"

# Use the venv from SCRIPT_DIR, but run in user's CWD
PYTHON="${SCRIPT_DIR}/.venv/bin/python"

if [ "$SLACKBOT" = true ]; then
  # ---- Run Slack bot connector ----
  SLACKBOT_FLAGS="--mcp-url ${MCP_URL}${COMMON_FLAGS}"
  echo "🤖 Starting Slack Bot Connector..."
  echo "   MCP URL: ${MCP_URL}"
  [ -n "$SYSTEM_PROMPT" ] && echo "   📝 System prompt: ${SYSTEM_PROMPT}"
  (cd "${SCRIPT_DIR}" && $PYTHON -m connectors.slackbot $SLACKBOT_FLAGS)
else
  # ---- Run AtomAI interactive REPL ----
  AGENT_FLAGS="${COMMON_FLAGS}"
  [ "$ROOT_MODE" = false ] && AGENT_FLAGS="--mcp-url ${MCP_URL}${AGENT_FLAGS}"
  [ -n "$SESSION_FILE" ] && AGENT_FLAGS="$AGENT_FLAGS --session $SESSION_FILE"
  if [ "$ROOT_MODE" = true ]; then
    echo "🚀 Starting AtomAI (ROOT MODE — local tools)..."
  else
    echo "🚀 Starting AtomAI (MCP Sandbox Mode)..."
    echo "   MCP URL: ${MCP_URL}"
  fi
  [ -n "$SESSION_FILE" ] && echo "   💾 Session: ${SESSION_FILE}"
  PYTHONPATH="${SCRIPT_DIR}/agent" $PYTHON "${SCRIPT_DIR}/agent/agent.py" $AGENT_FLAGS
fi
