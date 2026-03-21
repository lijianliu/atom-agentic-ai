#!/usr/bin/env bash
set -euo pipefail

# ---- Parse flags ----
SKIP_UPDATE=false
VERBOSE=false
USE_OPENAI=false
for arg in "$@"; do
  case "$arg" in
    --skip-update|-s) SKIP_UPDATE=true ;;
    --verbose|-v)     VERBOSE=true ;;
    --openai)         USE_OPENAI=true ;;
  esac
done

# ---- Load local env if present ----
ENV_FILE="${HOME}/.config/atom-agentic-ai/env.sh"
if [ -f "$ENV_FILE" ]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi

# ---- Ensure uv exists ----
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed or not in PATH"
  exit 1
fi

# ---- Create venv if missing ----
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (Python 3.13)..."
  uv venv --python 3.13
  SKIP_UPDATE=false  # force sync on fresh venv
fi

if [ "$SKIP_UPDATE" = false ]; then
  # ---- Sync dependencies from lockfile ----
  echo "Syncing dependencies..."
  uv sync --all-groups
else
  echo "Skipping dependency update (--skip-update)"
fi

# ---- Build flags for agent ----
AGENT_FLAGS=""
[ "$VERBOSE" = true ]    && AGENT_FLAGS="$AGENT_FLAGS --verbose"
[ "$USE_OPENAI" = true ] && AGENT_FLAGS="$AGENT_FLAGS --openai"

# ---- Run AtomAI ----
echo "Starting AtomAI..."
.venv/bin/python -m agent.agent $AGENT_FLAGS
