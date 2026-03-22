#!/usr/bin/env bash
# =============================================================================
# run-hardened-macos.sh — Launch a maximally hardened Docker container (macOS)
# =============================================================================
# macOS (Apple Silicon / Intel) compatible version of run-hardened.sh
# Supports both Docker Desktop and Colima as Docker runtimes.
#
# Usage:
#   ./run-hardened-macos.sh                    # Interactive bash shell
#   ./run-hardened-macos.sh python3 app.py     # Run a specific command
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="hardened-sandbox:latest"
CONTAINER_NAME="sandbox-$(date +%s)"
SECCOMP_PROFILE="${SCRIPT_DIR}/seccomp-strict-macos.json"

# macOS: socket in /tmp since /var/run requires root
SOCKET_PATH="/tmp/gsutil-proxy.sock"

# ── Pre-flight checks ───────────────────────────────────────────────────────

# 1. Check Docker is installed
if ! command -v docker &>/dev/null; then
    echo "❌ Docker not found. Install Docker Desktop or Colima:"
    echo "   brew install docker colima"
    exit 1
fi

# 2. Check Docker daemon is running (try Colima first, then Docker Desktop)
if ! docker info &>/dev/null; then
    echo "⏳ Docker daemon not reachable. Attempting to detect runtime..."

    # Try switching to colima context
    if command -v colima &>/dev/null; then
        if colima status &>/dev/null; then
            echo "   Found running Colima instance, switching context..."
            docker context use colima &>/dev/null
        else
            echo "   Starting Colima..."
            colima start 2>&1
            docker context use colima &>/dev/null
        fi
    fi

    # Final check
    if ! docker info &>/dev/null; then
        echo "❌ Docker daemon is not running."
        echo "   If using Colima:         colima start"
        echo "   If using Docker Desktop: open -a Docker"
        exit 1
    fi
fi

echo "✅ Docker daemon is running (context: $(docker context show))"

# 3. Detect host architecture for multi-arch build
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    arm64|aarch64)
        DOCKER_PLATFORM="linux/arm64"
        ;;
    x86_64)
        DOCKER_PLATFORM="linux/amd64"
        ;;
    *)
        echo "⚠️  Unknown architecture: ${HOST_ARCH}, defaulting to linux/arm64"
        DOCKER_PLATFORM="linux/arm64"
        ;;
esac

# ── Build image if it doesn't exist ─────────────────────────────────────────

if ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
    echo "🐶 Building hardened sandbox image (platform: ${DOCKER_PLATFORM})..."
    docker build --platform "${DOCKER_PLATFORM}" -t "${IMAGE_NAME}" -f "${SCRIPT_DIR}/Dockerfile.macos" "${SCRIPT_DIR}"
fi

# ── Determine the command to run ────────────────────────────────────────────

if [ $# -eq 0 ]; then
    CMD=("/bin/bash")
else
    CMD=("$@")
fi

# ── Check if gsutil proxy socket exists ─────────────────────────────────────

SOCKET_MOUNT=()
if [ -S "${SOCKET_PATH}" ]; then
    SOCKET_MOUNT=(-v "${SOCKET_PATH}:/tmp/gsutil-proxy.sock")
    echo "   ✅ gsutil proxy socket mounted (${SOCKET_PATH})"
else
    echo "   ⚠️  gsutil proxy not running (start with: python3 gsutil-proxy.py)"
    echo "      Note on macOS: socket is at ${SOCKET_PATH} (not /var/run)"
fi

# ── Launch ───────────────────────────────────────────────────────────────────

echo "🔒 Launching hardened container: ${CONTAINER_NAME}"
echo "   Platform: ${DOCKER_PLATFORM} (host: ${HOST_ARCH})"
echo "   Security layers active:"
echo "   ✅ Non-root user (UID 1000)"
echo "   ✅ All capabilities dropped"
echo "   ✅ no-new-privileges"
echo "   ✅ Read-only root filesystem"
echo "   ✅ Custom seccomp profile (whitelist-only)"
echo "   ✅ No network (--network=none)"
echo "   ✅ PID namespace isolation"
echo "   ✅ Memory limit: 2GB | CPU limit: 2 cores"
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
    --network=none \
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
