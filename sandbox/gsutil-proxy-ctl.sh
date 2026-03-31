#!/usr/bin/env bash
# =============================================================================
# gsutil-proxy-ctl.sh — Start/stop the gsutil proxy daemon on the host
# =============================================================================
# Usage:
#   sudo ./gsutil-proxy-ctl.sh start
#   sudo ./gsutil-proxy-ctl.sh stop
#   sudo ./gsutil-proxy-ctl.sh status
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SCRIPT="${SCRIPT_DIR}/gsutil-proxy.py"
SOCKET_DIR="/tmp/gsutil-proxy"
SOCKET_PATH="${SOCKET_DIR}/gsutil-proxy.sock"
PID_FILE="/tmp/gsutil-proxy.pid"
LOG_FILE="/tmp/gsutil-proxy.log"

case "${1:-}" in
    start)
        if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
            echo "⚠️  Proxy already running (PID $(cat "${PID_FILE}"))"
            exit 0
        fi
        echo "🚀 Starting gsutil proxy daemon..."
        mkdir -p "${SOCKET_DIR}"
        chmod 755 "${SOCKET_DIR}"
        touch "${LOG_FILE}"
        nohup python3 "${PROXY_SCRIPT}" >> "${LOG_FILE}" 2>&1 &
        echo $! > "${PID_FILE}"
        # Wait up to 10s for the socket to appear (race condition fix)
        for i in $(seq 1 20); do
            if [ -S "${SOCKET_PATH}" ]; then
                echo "✅ Proxy started (PID $(cat "${PID_FILE}"))"
                echo "   Socket: ${SOCKET_PATH}"
                echo "   Log:    ${LOG_FILE}"
                break
            fi
            if [ "$i" -eq 20 ]; then
                echo "❌ Failed to start after 5s. Check ${LOG_FILE}"
                exit 1
            fi
            sleep 0.5
        done
        ;;
    stop)
        if [ -f "${PID_FILE}" ]; then
            PID=$(cat "${PID_FILE}")
            if kill -0 "${PID}" 2>/dev/null; then
                kill "${PID}"
                echo "✅ Proxy stopped (PID ${PID})"
            else
                echo "⚠️  PID ${PID} not running"
            fi
            rm -f "${PID_FILE}"
        else
            echo "⚠️  No PID file found"
        fi
        rm -f "${SOCKET_PATH}"
        ;;
    status)
        if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
            echo "✅ Running (PID $(cat "${PID_FILE}"))"
            echo "   Socket: $([ -S "${SOCKET_PATH}" ] && echo 'exists' || echo 'MISSING')"
        else
            echo "❌ Not running"
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
