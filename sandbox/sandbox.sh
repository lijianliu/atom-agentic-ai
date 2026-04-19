#!/usr/bin/env bash
# =============================================================================
# sandbox.sh — Hardened Docker sandbox manager
# =============================================================================
# Usage:
#   ./sandbox.sh build                    # (re)build the sandbox image
#   ./sandbox.sh clean                    # stop container + remove image
#   ./sandbox.sh start [--port PORT]      # start MCP server (detached)
#   ./sandbox.sh stop                     # stop MCP server
#   ./sandbox.sh status                   # show running state + port + uptime
#   ./sandbox.sh shell                    # exec into running container
#   ./sandbox.sh run -- CMD               # run one-off command in fresh container
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="hardened-sandbox:latest"
CONTAINER_NAME="sandbox-mcp"
DEFAULT_PORT=9100

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $(basename "$0") <command> [options]"
    echo ""
    echo "Commands:"
    echo "  build                 (Re)build the sandbox image"
    echo "  clean                 Stop container + remove image"
    echo "  start [--port PORT]   Start MCP server detached (default port: ${DEFAULT_PORT})"
    echo "  stop                  Stop MCP server"
    echo "  status                Show running state, port, uptime"
    echo "  shell                 Exec into running container"
    echo "  run -- CMD            Run one-off command in a fresh container"
    echo ""
}

if [ $# -eq 0 ]; then
    usage
    exit 0
fi

COMMAND="$1"
shift

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
else
    TMPFS_OPTS="rw,noexec,nosuid"
fi

# atom-command-broker socket directory
BROKER_SOCK_DIR="${ATOM_BROKER_SOCKET_DIR:-/tmp/atom-command-proxy}"

# Shared workspace: same path (/workspace) on both host and container.
# The broker executes commands on the host using the same /workspace path
# the container sees, so no path rewriting is needed.
SHARED_WORKSPACE="${ATOM_SHARED_WORKSPACE:-/workspace}"

SECCOMP_PROFILE="${SCRIPT_DIR}/seccomp-strict.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ensure_docker() {
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
}

is_running() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"
}

ensure_image() {
    if ! docker image inspect "${IMAGE_NAME}" &>/dev/null; then
        echo "🐶 Image not found — building first..."
        do_build
    fi
}

do_build() {
    echo "🐶 Building sandbox image (platform: ${DOCKER_PLATFORM})..."
    docker build \
        --platform "${DOCKER_PLATFORM}" \
        -t "${IMAGE_NAME}" \
        "${SCRIPT_DIR}"
    echo "✅ Image built: ${IMAGE_NAME}"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
cmd_build() {
    ensure_docker
    do_build
}

cmd_clean() {
    ensure_docker
    # Remove all containers (running or stopped) using this image
    local containers
    containers=$(docker ps -a --filter "ancestor=${IMAGE_NAME}" --format '{{.ID}}' 2>/dev/null || true)
    if [ -n "${containers}" ]; then
        echo "🛑 Removing containers using ${IMAGE_NAME}..."
        echo "${containers}" | xargs docker rm -f
        echo "   ✅ Containers removed."
    else
        echo "   ℹ️  No containers to remove."
    fi
    if docker image inspect "${IMAGE_NAME}" &>/dev/null; then
        echo "🗑️  Removing image ${IMAGE_NAME}..."
        docker rmi -f "${IMAGE_NAME}"
        echo "   ✅ Image removed."
    else
        echo "   ℹ️  Image not found."
    fi
}

cmd_start() {
    local port="${DEFAULT_PORT}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port) port="$2"; shift 2 ;;
            *) echo "Unknown option: $1"; usage; exit 1 ;;
        esac
    done

    ensure_docker
    ensure_image

    # Evict stale container
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        echo "⏳ Removing stale ${CONTAINER_NAME}..."
        docker rm -f "${CONTAINER_NAME}" &>/dev/null
    fi

    # Create socket directory for atom-command-broker
    mkdir -p "${BROKER_SOCK_DIR}"
    chmod 755 "${BROKER_SOCK_DIR}"

    # Create shared workspace directory (same path on host and container)
    sudo mkdir -p "${SHARED_WORKSPACE}"
    sudo chmod 777 "${SHARED_WORKSPACE}"

    echo ""
    echo "🔒 Starting sandbox: ${CONTAINER_NAME}"
    echo "   Platform:   ${DOCKER_PLATFORM} (${OS} / ${HOST_ARCH})"
    echo "   MCP:        http://127.0.0.1:${port}/sse"
    echo "   Broker:     ${BROKER_SOCK_DIR} (mounted → /tmp/atom-command-proxy)"
    echo "   Workspace:  ${SHARED_WORKSPACE} (same path on host and container)"
    echo "   Security:   cap-drop=ALL | no-new-privileges | read-only rootfs | seccomp"
    echo "   Limits:     memory=2g | cpus=2 | pids=256"
    echo ""

    docker run \
        --name "${CONTAINER_NAME}" \
        --rm \
        -d \
        --platform "${DOCKER_PLATFORM}" \
        --log-driver=json-file \
        --user 1000:1000 \
        --cap-drop=ALL \
        --security-opt=no-new-privileges:true \
        --security-opt seccomp="${SECCOMP_PROFILE}" \
        --read-only \
        --tmpfs "/tmp:${TMPFS_OPTS},size=256m" \
        --tmpfs "/run:${TMPFS_OPTS},size=64m" \
        -v "${SHARED_WORKSPACE}:/workspace" \
        -p "127.0.0.1:${port}:${port}" \
        --pids-limit=256 \
        --memory=2g \
        --memory-swap=2g \
        --cpus=2 \
        --ipc=private \
        --ulimit nproc=512:512 \
        --ulimit fsize=104857600:104857600 \
        --ulimit nofile=1024:2048 \
        -v "${BROKER_SOCK_DIR}:/tmp/atom-command-proxy" \
        "${IMAGE_NAME}" \
        python3 /opt/mcp/mcp_server.py --port "${port}" --transport sse

    echo "✅ Sandbox started — MCP at http://127.0.0.1:${port}/sse"
}

cmd_stop() {
    ensure_docker
    if is_running; then
        echo "🛑 Stopping ${CONTAINER_NAME}..."
        docker rm -f "${CONTAINER_NAME}" &>/dev/null
        echo "   ✅ Stopped."
    else
        echo "   ⚠️  Container not running."
    fi
}

cmd_status() {
    ensure_docker
    if is_running; then
        local uptime
        uptime=$(docker inspect --format '{{.State.StartedAt}}' "${CONTAINER_NAME}" 2>/dev/null || echo 'unknown')
        local port
        port=$(docker inspect --format '{{range $p, $conf := .NetworkSettings.Ports}}{{$p}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo 'unknown')
        echo "✅ ${CONTAINER_NAME} is running"
        echo "   Started:   ${uptime}"
        echo "   Port:      ${port}"
        echo "   MCP:       http://127.0.0.1:${DEFAULT_PORT}/sse"
        echo "   Broker:    ${BROKER_SOCK_DIR} → /tmp/atom-command-proxy"
        echo "   Workspace: ${SHARED_WORKSPACE}"
    else
        echo "❌ ${CONTAINER_NAME} is not running"
    fi
}

cmd_shell() {
    ensure_docker
    if ! is_running; then
        echo "❌ ${CONTAINER_NAME} is not running. Start it first: $(basename "$0") start"
        exit 1
    fi
    echo "🐚 Exec-ing into ${CONTAINER_NAME}..."
    exec docker exec -it "${CONTAINER_NAME}" /bin/bash
}

cmd_run() {
    if [ $# -eq 0 ] || [ "$1" != "--" ]; then
        echo "Usage: $(basename "$0") run -- CMD"
        exit 1
    fi
    shift  # drop the --

    ensure_docker
    ensure_image
    mkdir -p "${BROKER_SOCK_DIR}"
    sudo mkdir -p "${SHARED_WORKSPACE}"
    sudo chmod 777 "${SHARED_WORKSPACE}"

    echo "🔒 Running in fresh sandbox: $*"
    docker run \
        --name "sandbox-run-$(date +%s)" \
        --rm \
        -it \
        --platform "${DOCKER_PLATFORM}" \
        --log-driver=json-file \
        --user 1000:1000 \
        --cap-drop=ALL \
        --security-opt=no-new-privileges:true \
        --security-opt seccomp="${SECCOMP_PROFILE}" \
        --read-only \
        --tmpfs "/tmp:${TMPFS_OPTS},size=256m" \
        --tmpfs "/run:${TMPFS_OPTS},size=64m" \
        -v "${SHARED_WORKSPACE}:/workspace" \
        --network=none \
        --pids-limit=256 \
        --memory=2g \
        --memory-swap=2g \
        --cpus=2 \
        --ipc=private \
        --ulimit nproc=512:512 \
        --ulimit fsize=104857600:104857600 \
        --ulimit nofile=1024:2048 \
        -v "${BROKER_SOCK_DIR}:/tmp/atom-command-proxy" \
        "${IMAGE_NAME}" "$@"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${COMMAND}" in
    build)  cmd_build "$@" ;;
    clean)  cmd_clean "$@" ;;
    start)  cmd_start "$@" ;;
    stop)   cmd_stop "$@" ;;
    status) cmd_status "$@" ;;
    shell)  cmd_shell "$@" ;;
    run)    cmd_run "$@" ;;
    *)
        echo "❌ Unknown command: ${COMMAND}"
        echo ""
        usage
        exit 1
        ;;
esac
