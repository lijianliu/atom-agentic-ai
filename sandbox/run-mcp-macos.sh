#!/usr/bin/env bash
# run-mcp-macos.sh — Option 2: hardened sandbox + MCP on internal TCP network
#
# What this does:
#   1. Creates a Docker-internal network (no outbound internet)
#   2. Runs the hardened sandbox container, publishing port 9100 to localhost only
#   3. MCP clients connect to http://localhost:9100/sse  — no relay, no socat, nothing
#
# Claude Desktop config (~/.config/claude/claude_desktop_config.json):
#   { "mcpServers": { "sandbox": { "url": "http://localhost:9100/sse" } } }
set -euo pipefail

PORT=${MCP_PORT:-9100}
NETWORK=mcp-jail
CONTAINER=sandbox-mcp
IMAGE=hardened-sandbox-mcp:latest

# ── 1. Docker daemon ───────────────────────────────────────────────────────
if ! docker info &>/dev/null; then
  echo "Starting Colima..."; colima start --arch aarch64 --vm-type vz 2>/dev/null || true
fi
echo "✅ Docker daemon running"

# ── 2. Stop old container ──────────────────────────────────────────────────
docker rm -f $CONTAINER 2>/dev/null || true

# ── 4. Detect seccomp profile ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECCOMP_ARG=""
for f in "$SCRIPT_DIR/seccomp-strict.json" "$SCRIPT_DIR/seccomp.json"; do
  if [[ -f "$f" ]]; then SECCOMP_ARG="--security-opt seccomp=$f"; break; fi
done

# ── 5. Run container ───────────────────────────────────────────────────────
docker run -d \
  --name $CONTAINER \
  --platform linux/arm64 \
  -p 127.0.0.1:${PORT}:9100 \
  --read-only \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  ${SECCOMP_ARG} \
  --pids-limit 64 \
  --memory 512m \
  --cpus 1 \
  --tmpfs /tmp:rw,uid=1000,gid=1000,noexec,nosuid,size=256m \
  --tmpfs /run:rw,uid=1000,gid=1000,noexec,nosuid,size=64m \
  --tmpfs /workspace:rw,uid=1000,gid=1000,size=1g \
  $IMAGE

echo "✅ Container $CONTAINER started"

# ── 6. Wait for healthy ────────────────────────────────────────────────────
echo -n "   Waiting for MCP server on port $PORT ..."
for i in $(seq 1 30); do
  if curl -sf --max-time 1 "http://localhost:$PORT/sse" -o /dev/null 2>/dev/null; then
    echo " ready!"
    break
  fi
  echo -n "."; sleep 1
done

# ── 7. Summary ─────────────────────────────────────────────────────────────
echo ""
echo "   MCP endpoint: http://localhost:${PORT}/sse"
echo "   Network:      default bridge, port bound to 127.0.0.1 only"
echo "   Note:         --internal Docker networks don't support port publishing on macOS"
echo "   Test with:    python3 sandbox/test-mcp.py --port $PORT"
echo ""
echo "   Claude Desktop config:"
echo '   { "mcpServers": { "sandbox": { "url": "http://localhost:'${PORT}'/sse" } } }'
echo ""
