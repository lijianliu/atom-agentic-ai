#!/usr/bin/env bash
# =============================================================================
# run-hardened-mcp-macos.sh — MCP server in a maximally hardened container
# =============================================================================
# IPC architecture (virtiofs/9p can't create sockets from container side):
#
#   HOST                              CONTAINER (--network=none)
#   mcp_host_relay.py                 mcp_server.py
#     /tmp/mcp-sandbox/mcp.sock  ⇔    uvicorn on /tmp/mcp.sock (tmpfs)
#     docker exec -i              ⇔    mcp_container_relay.py
#
# Usage:
#   ./run-hardened-mcp-macos.sh             # foreground
#   ./run-hardened-mcp-macos.sh --detach    # background daemon
#   ./run-hardened-mcp-macos.sh --stop      # stop container + relay
#   ./run-hardened-mcp-macos.sh --shell     # debug shell in MCP image
#   ./run-hardened-mcp-macos.sh --socket PATH
#   ./run-hardened-mcp-macos.sh --transport sse|streamable-http
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECCOMP_PROFILE="${SCRIPT_DIR}/seccomp-strict-macos.json"

BASE_IMAGE_NAME="hardened-sandbox:latest"
MCP_IMAGE_NAME="hardened-sandbox-mcp:latest"
CONTAINER_NAME="sandbox-mcp"

DETACH=false
SHELL_MODE=false
STOP_MODE=false
TRANSPORT="sse"
SOCKET_HOST_PATH="/tmp/mcp-sandbox/mcp.sock"
RELAY_LOG="/tmp/mcp-host-relay.log"

# -- Parse arguments ----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --detach|-d) DETACH=true; shift ;;
        --stop)      STOP_MODE=true; shift ;;
        --shell)     SHELL_MODE=true; shift ;;
        --transport) TRANSPORT="$2"; shift 2 ;;
        --socket)    SOCKET_HOST_PATH="$2"; shift 2 ;;
        *) echo "Unknown option: $1"
           echo "Usage: $0 [--detach] [--stop] [--shell] [--socket PATH] [--transport sse|streamable-http]"
           exit 1 ;;
    esac
done

# -- Stop mode ----------------------------------------------------------------
if [ "${STOP_MODE}" = true ]; then
    echo "🛑 Stopping ${CONTAINER_NAME}..."
    docker stop "${CONTAINER_NAME}" 2>/dev/null && echo "   ✅ Container stopped." || echo "   ⚠️  Not running."
    docker rm "${CONTAINER_NAME}" 2>/dev/null || true
    pkill -f "mcp_host_relay.py" 2>/dev/null && echo "   ✅ Relay stopped." || true
    rm -f "${SOCKET_HOST_PATH}"
    echo "   Socket removed: ${SOCKET_HOST_PATH}"
    exit 0
fi

# -- Pre-flight checks --------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "❌ Docker not found.  brew install docker colima"
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "⏳ Docker not reachable — trying Colima..."
    if command -v colima &>/dev/null; then
        if colima status &>/dev/null; then
            docker context use colima &>/dev/null
        else
            colima start
            docker context use colima &>/dev/null
        fi
    fi
    docker info &>/dev/null || { echo "❌ Docker daemon not running."; exit 1; }
fi
echo "✅ Docker daemon running ($(docker context show))"

# -- Architecture -------------------------------------------------------------
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    arm64|aarch64) DOCKER_PLATFORM="linux/arm64" ;;
    x86_64)        DOCKER_PLATFORM="linux/amd64" ;;
    *)             DOCKER_PLATFORM="linux/arm64" ;;
esac

# -- Build images -------------------------------------------------------------
echo "🐶 Building hardened sandbox base image (includes MCP deps)..."
docker build \
    --platform "${DOCKER_PLATFORM}" \
    -t "${BASE_IMAGE_NAME}" \
    -f "${SCRIPT_DIR}/Dockerfile.macos" \
    "${SCRIPT_DIR}"

echo "🐶 Building hardened sandbox MCP image..."
docker build \
    --platform "${DOCKER_PLATFORM}" \
    -t "${MCP_IMAGE_NAME}" \
    -f "${SCRIPT_DIR}/Dockerfile.mcp" \
    "${SCRIPT_DIR}"

# -- Prepare host socket dir --------------------------------------------------
mkdir -p "$(dirname "${SOCKET_HOST_PATH}")"
rm -f "${SOCKET_HOST_PATH}"

# -- Evict stale container ----------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "⏳ Removing existing ${CONTAINER_NAME}..."
    docker rm -f "${CONTAINER_NAME}" &>/dev/null || true
fi

# -- gsutil proxy socket (optional) ------------------------------------------
GSUTIL_SOCKET_PATH="/tmp/gsutil-proxy.sock"
GSUTIL_MOUNT=()
if [ -S "${GSUTIL_SOCKET_PATH}" ]; then
    GSUTIL_MOUNT=(-v "${GSUTIL_SOCKET_PATH}:/tmp/gsutil-proxy.sock")
    echo "   ✅ gsutil proxy socket mounted"
else
    echo "   ⚠️  gsutil proxy not running (optional)"
fi

# -- Choose command and flags -------------------------------------------------
if [ "${SHELL_MODE}" = true ]; then
    CMD=("/bin/bash")
    IFLAGS="-it"
elif [ "${DETACH}" = true ]; then
    CMD=("python3" "/opt/mcp/mcp_server.py" "--uds" "/tmp/mcp.sock" "--transport" "${TRANSPORT}")
    IFLAGS="-d"
else
    CMD=("python3" "/opt/mcp/mcp_server.py" "--uds" "/tmp/mcp.sock" "--transport" "${TRANSPORT}")
    IFLAGS=""
fi

# -- Launch container ---------------------------------------------------------
echo ""
echo "🔒 Launching hardened MCP sandbox: ${CONTAINER_NAME}"
echo "   Platform:  ${DOCKER_PLATFORM} (host: ${HOST_ARCH})"
echo "   Network:   none (zero egress)"
echo "   IPC:       docker exec relay → /tmp/mcp.sock (container tmpfs)"
echo "   Host sock: ${SOCKET_HOST_PATH} (created by relay after startup)"
echo "   Transport: ${TRANSPORT}"
echo "   Security:  cap-drop=ALL | no-new-privs | read-only rootfs | seccomp"
echo ""

# shellcheck disable=SC2086
docker run \
    --name "${CONTAINER_NAME}" \
    --rm \
    ${IFLAGS} \
    --platform "${DOCKER_PLATFORM}" \
    --log-driver=json-file \
    --user 1000:1000 \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --security-opt seccomp="${SECCOMP_PROFILE}" \
    --read-only \
    --tmpfs /tmp:rw,size=256m \
    --tmpfs /run:rw,size=64m \
    --tmpfs /workspace:rw,uid=1000,gid=1000,size=1g \
    --network=none \
    --pids-limit=256 \
    --memory=2g \
    --memory-swap=2g \
    --cpus=2 \
    --ipc=private \
    --ulimit nproc=512:512 \
    --ulimit fsize=104857600:104857600 \
    --ulimit nofile=1024:2048 \
    "${GSUTIL_MOUNT[@]+${GSUTIL_MOUNT[@]}}" \
    "${MCP_IMAGE_NAME}" "${CMD[@]}"

# -- Post-launch (detach mode) ------------------------------------------------
if [ "${DETACH}" = true ] && [ "${SHELL_MODE}" != true ]; then
    echo ""
    echo "✅ Container is up."

    # Start the host relay: waits for the container socket then exposes host socket.
    nohup python3 "${SCRIPT_DIR}/mcp_host_relay.py" \
        --socket "${SOCKET_HOST_PATH}" \
        --container "${CONTAINER_NAME}" \
        > "${RELAY_LOG}" 2>&1 &
    RELAY_PID=$!
    echo "   Relay PID: ${RELAY_PID}  (log: ${RELAY_LOG})"

    # Wait for relay to create the host socket (up to 35s).
    for i in $(seq 1 70); do
        [ -S "${SOCKET_HOST_PATH}" ] && break
        sleep 0.5
    done

    if [ -S "${SOCKET_HOST_PATH}" ]; then
        echo "   Socket:    ${SOCKET_HOST_PATH} ✅"
    else
        echo "   ⚠️  Socket not ready — check relay log: ${RELAY_LOG}"
        cat "${RELAY_LOG}" 2>/dev/null || true
    fi

    echo ""
    echo "   Container logs: docker logs -f ${CONTAINER_NAME}"
    echo "   Stop:           $0 --stop"
else
    echo "🐶 Container exited. Host is safe!"
fi
