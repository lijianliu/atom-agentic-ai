#!/usr/bin/env bash
# =============================================================================
# run-hardened-with-network-macos.sh — Hardened sandbox WITH outbound network
# =============================================================================
# macOS (Apple Silicon / Intel) compatible version.
# Use this when the workload inside the container needs outbound internet
# (e.g. pip install, curl).  For the MCP server use run-hardened-mcp-macos.sh
# instead — it runs with --network=none and communicates via Unix socket.
#
# Usage:
#   ./run-hardened-with-network-macos.sh            # interactive bash
#   ./run-hardened-with-network-macos.sh -- CMD     # run a command
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="hardened-sandbox:latest"
CONTAINER_NAME="sandbox-net-$(date +%s)"
SECCOMP_PROFILE="${SCRIPT_DIR}/seccomp-strict-macos.json"
SOCKET_PATH="/tmp/gsutil-proxy.sock"

# -- Parse arguments ----------------------------------------------------------
USER_CMD=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --) shift; USER_CMD=("$@"); break ;;
        *) echo "Unknown option: $1"; echo "Usage: $0 [-- COMMAND]"; exit 1 ;;
    esac
done

# -- Pre-flight checks --------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "❌ Docker not found. Install Docker Desktop for Mac."
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

# -- Detect architecture ------------------------------------------------------
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    arm64|aarch64) DOCKER_PLATFORM="linux/arm64" ;;
    x86_64)        DOCKER_PLATFORM="linux/amd64" ;;
    *)             DOCKER_PLATFORM="linux/arm64" ;;
esac

# -- Build base image if missing ----------------------------------------------
if ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
    echo "🐶 Building hardened sandbox image (platform: ${DOCKER_PLATFORM})..."
    docker build \
           --platform "${DOCKER_PLATFORM}" \
           -t "${IMAGE_NAME}" \
           -f "${SCRIPT_DIR}/Dockerfile.macos" \
           "${SCRIPT_DIR}"
fi

# -- gsutil proxy socket (optional) ------------------------------------------
SOCKET_MOUNT=()
if [ -S "${SOCKET_PATH}" ]; then
    SOCKET_MOUNT=(-v "${SOCKET_PATH}:/tmp/gsutil-proxy.sock")
    echo "   ✅ gsutil proxy socket mounted"
else
    echo "   ⚠️  gsutil proxy not running (optional; start with: python3 gsutil-proxy.py)"
fi

# -- Choose command -----------------------------------------------------------
if [ ${#USER_CMD[@]} -gt 0 ]; then
    CMD=("${USER_CMD[@]}")
else
    CMD=("/bin/bash")
fi

# -- Launch -------------------------------------------------------------------
echo ""
echo "🔒 Launching hardened container (with network): ${CONTAINER_NAME}"
echo "   Platform: ${DOCKER_PLATFORM} (host: ${HOST_ARCH})"
echo "   Security: cap-drop=ALL | no-new-privileges | read-only rootfs | seccomp"
echo ""

docker run \
       --name "${CONTAINER_NAME}" \
       --rm \
       -it \
       --platform "${DOCKER_PLATFORM}" \
       --log-driver=json-file \
       --user 1000:1000 \
       --cap-drop=ALL \
       --security-opt=no-new-privileges:true \
       --security-opt seccomp="${SECCOMP_PROFILE}" \
       --read-only \
       --tmpfs /tmp:rw,size=256m \
       --tmpfs /run:rw,size=64m \
       --tmpfs /workspace:rw,size=1g \
       --pids-limit=256 \
       --memory=2g \
       --memory-swap=2g \
       --cpus=2 \
       --ipc=private \
       --ulimit nproc=512:512 \
       --ulimit fsize=104857600:104857600 \
       --ulimit nofile=1024:2048 \
       "${SOCKET_MOUNT[@]+${SOCKET_MOUNT[@]}}" \
       "${IMAGE_NAME}" "${CMD[@]}"

echo "🐶 Container exited. Host is safe!"