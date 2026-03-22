#!/usr/bin/env bash
# =============================================================================
# sandbox.sh — Unified hardened sandbox launcher (Linux + macOS)
# =============================================================================
# Usage:
#   ./sandbox.sh                             # interactive shell, no network
#   ./sandbox.sh -- python3 app.py           # run command, no network
#   ./sandbox.sh --network                   # interactive shell, with network
#   ./sandbox.sh --network -- CMD            # run command, with network
#   ./sandbox.sh --mcp                       # MCP server on TCP port 9100
#   ./sandbox.sh --mcp --port 8811           # MCP server on custom port
#   ./sandbox.sh --mcp --transport streamable-http
#   ./sandbox.sh --mcp --detach              # background MCP container
#   ./sandbox.sh --mcp --stop                # stop background MCP container
#   ./sandbox.sh --mcp --shell               # debug shell in MCP image
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="hardened-sandbox:latest"
MCP_CONTAINER_NAME="sandbox-mcp"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
NETWORK=false
MCP_MODE=false
DETACH=false
SHELL_MODE=false
STOP_MODE=false
MCP_PORT=9100
TRANSPORT="sse"
USER_CMD=()

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --network|-n)  NETWORK=true; shift ;;
        --mcp)         MCP_MODE=true; shift ;;
        --detach|-d)   DETACH=true; shift ;;
        --stop)        STOP_MODE=true; shift ;;
        --shell)       SHELL_MODE=true; shift ;;
        --port)        MCP_PORT="$2"; shift 2 ;;
        --transport)   TRANSPORT="$2"; shift 2 ;;
        --)            shift; USER_CMD=("$@"); break ;;
        -*)
            echo "Unknown option: $1"
            echo ""
            echo "Usage: $0 [OPTIONS] [-- COMMAND]"
            echo ""
            echo "  --network, -n          Enable outbound network (non-MCP shells)"
            echo "  --mcp                  Launch MCP server on TCP (default port: 9100)"
            echo "  --port PORT            MCP TCP port (default: 9100)"
            echo "  --transport TRANSPORT  sse | streamable-http (default: sse)"
            echo "  --detach, -d           Run MCP container in background"
            echo "  --stop                 Stop background MCP container"
            echo "  --shell                Debug shell inside container"
            echo "  -- COMMAND             Run a specific command"
            exit 1 ;;
        *) USER_CMD=("$@"); break ;;
    esac
done

# MCP always needs the container port exposed
[ "${MCP_MODE}" = true ] && NETWORK=true

# Non-MCP containers get unique names; MCP reuses a fixed name for --stop
if [ "${MCP_MODE}" = false ]; then
    CONTAINER_NAME="sandbox-$(date +%s)"
else
    CONTAINER_NAME="${MCP_CONTAINER_NAME}"
fi

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"
HOST_ARCH="$(uname -m)"

case "${HOST_ARCH}" in
    arm64|aarch64) DOCKER_PLATFORM="linux/arm64" ;;
    x86_64)        DOCKER_PLATFORM="linux/amd64" ;;
    *)             DOCKER_PLATFORM="linux/arm64" ;;
esac

# macOS Docker mounts don't support noexec/nosuid on tmpfs
if [ "${OS}" = "Darwin" ]; then
    TMPFS_OPTS="rw"
    GSUTIL_SOCK="/tmp/gsutil-proxy.sock"
else
    TMPFS_OPTS="rw,noexec,nosuid"
    GSUTIL_SOCK="/var/run/gsutil-proxy.sock"
fi

SECCOMP_PROFILE="${SCRIPT_DIR}/seccomp-strict.json"

# ---------------------------------------------------------------------------
# Stop mode
# ---------------------------------------------------------------------------
if [ "${STOP_MODE}" = true ]; then
    echo "🛑 Stopping ${CONTAINER_NAME}..."
    docker stop "${CONTAINER_NAME}" 2>/dev/null \
        && echo "   ✅ Container stopped." \
        || echo "   ⚠️  Container not running."
    docker rm "${CONTAINER_NAME}" 2>/dev/null || true
    exit 0
fi

# ---------------------------------------------------------------------------
# Pre-flight: Docker daemon
# ---------------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "❌ Docker not found."
    [ "${OS}" = "Darwin" ] \
        && echo "   Install: brew install docker colima" \
        || echo "   Install: https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info &>/dev/null; then
    if [ "${OS}" = "Darwin" ] && command -v colima &>/dev/null; then
        echo "⏳ Docker not reachable — trying Colima..."
        if colima status &>/dev/null; then
            docker context use colima &>/dev/null
        else
            colima start
            docker context use colima &>/dev/null
        fi
    fi
    docker info &>/dev/null || { echo "❌ Docker daemon not running."; exit 1; }
fi
echo "✅ Docker daemon running ($(docker context show 2>/dev/null || echo 'default'))"

# ---------------------------------------------------------------------------
# Build image if missing
# ---------------------------------------------------------------------------
if ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
    echo "🐶 Building hardened sandbox image (platform: ${DOCKER_PLATFORM})..."
    docker build \
        --platform "${DOCKER_PLATFORM}" \
        -t "${IMAGE_NAME}" \
        "${SCRIPT_DIR}"
fi

# ---------------------------------------------------------------------------
# Optional: gsutil proxy socket
# ---------------------------------------------------------------------------
GSUTIL_MOUNT=()
if [ -S "${GSUTIL_SOCK}" ]; then
    GSUTIL_MOUNT=(-v "${GSUTIL_SOCK}:/tmp/gsutil-proxy.sock")
    echo "   ✅ gsutil proxy mounted (${GSUTIL_SOCK})"
else
    echo "   ⚠️  gsutil proxy not running (optional; start with: python3 gsutil-proxy.py)"
fi

# ---------------------------------------------------------------------------
# Evict stale MCP container before relaunching
# ---------------------------------------------------------------------------
if [ "${MCP_MODE}" = true ] \
    && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
    echo "⏳ Removing stale ${CONTAINER_NAME}..."
    docker rm -f "${CONTAINER_NAME}" &>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Build docker run flags
# ---------------------------------------------------------------------------
NETWORK_FLAGS=()
[ "${NETWORK}" = false ] && NETWORK_FLAGS=(--network=none)

PORT_FLAGS=()
[ "${MCP_MODE}" = true ] && PORT_FLAGS=(-p "127.0.0.1:${MCP_PORT}:${MCP_PORT}")

# Determine command + interactive flags
if [ "${SHELL_MODE}" = true ] || { [ "${MCP_MODE}" = false ] && [ ${#USER_CMD[@]} -eq 0 ]; }; then
    CMD=("/bin/bash")
    IFLAGS=("-it")
elif [ "${MCP_MODE}" = true ]; then
    CMD=("python3" "/opt/mcp/mcp_server.py"
         "--port" "${MCP_PORT}"
         "--transport" "${TRANSPORT}")
    IFLAGS=()
    [ "${DETACH}" = true ] && IFLAGS=("-d")
else
    CMD=("${USER_CMD[@]}")
    IFLAGS=("-it")
fi

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
echo ""
echo "🔒 Launching hardened sandbox: ${CONTAINER_NAME}"
echo "   Platform: ${DOCKER_PLATFORM} (${OS} / ${HOST_ARCH})"
echo "   Network:  $([ "${NETWORK}" = true ] && echo 'enabled' || echo 'none (maximum isolation)')"
[ "${MCP_MODE}" = true ] && echo "   MCP:      http://127.0.0.1:${MCP_PORT}/sse  [transport=${TRANSPORT}]"
echo "   Security: cap-drop=ALL | no-new-privileges | read-only rootfs | seccomp"
echo "   Limits:   memory=2g | cpus=2 | pids=256"
echo ""

docker run \
    --name "${CONTAINER_NAME}" \
    --rm \
    "${IFLAGS[@]+${IFLAGS[@]}}" \
    --platform "${DOCKER_PLATFORM}" \
    --log-driver=json-file \
    --user 1000:1000 \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --security-opt seccomp="${SECCOMP_PROFILE}" \
    --read-only \
    --tmpfs "/tmp:${TMPFS_OPTS},size=256m" \
    --tmpfs "/run:${TMPFS_OPTS},size=64m" \
    --tmpfs "/workspace:rw,uid=1000,gid=1000,size=1g" \
    "${NETWORK_FLAGS[@]+${NETWORK_FLAGS[@]}}" \
    "${PORT_FLAGS[@]+${PORT_FLAGS[@]}}" \
    --pids-limit=256 \
    --memory=2g \
    --memory-swap=2g \
    --cpus=2 \
    --ipc=private \
    --ulimit nproc=512:512 \
    --ulimit fsize=104857600:104857600 \
    --ulimit nofile=1024:2048 \
    "${GSUTIL_MOUNT[@]+${GSUTIL_MOUNT[@]}}" \
    "${IMAGE_NAME}" "${CMD[@]}"

echo "🐶 Container exited. Host is safe!"
