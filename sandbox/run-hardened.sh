#!/usr/bin/env bash
# =============================================================================
# run-hardened.sh — Launch a maximally hardened Docker container
# =============================================================================
# Usage:
#   ./run-hardened.sh                    # Interactive bash shell
#   ./run-hardened.sh python3 app.py     # Run a specific command
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="hardened-sandbox:latest"
CONTAINER_NAME="sandbox-$(date +%s)"
SECCOMP_PROFILE="${SCRIPT_DIR}/seccomp-strict.json"
SOCKET_PATH="/var/run/gsutil-proxy.sock"

# Build image if it doesn't exist
if ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
    echo "🐶 Building hardened sandbox image..."
    docker build -t "${IMAGE_NAME}" "${SCRIPT_DIR}"
fi

# Determine the command to run
if [ $# -eq 0 ]; then
    CMD=("/bin/bash")
else
    CMD=("$@")
fi

# Check if gsutil proxy socket exists
SOCKET_MOUNT=()
if [ -S "${SOCKET_PATH}" ]; then
    SOCKET_MOUNT=(-v "${SOCKET_PATH}:${SOCKET_PATH}")
    echo "   ✅ gsutil proxy socket mounted"
else
    echo "   ⚠️  gsutil proxy not running (start with: sudo python3 gsutil-proxy.py)"
fi

echo "🔒 Launching hardened container: ${CONTAINER_NAME}"
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
    --log-driver=json-file \
    --user 1000:1000 \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --security-opt seccomp="${SECCOMP_PROFILE}" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=256m \
    --tmpfs /run:rw,noexec,nosuid,size=64m \
    --tmpfs /workspace:rw,noexec,nosuid,size=1g \
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
