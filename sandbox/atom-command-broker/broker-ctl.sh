#!/usr/bin/env bash
# =============================================================================
# broker-ctl.sh — Start/stop/status for atom-command-broker on the host VM
# =============================================================================
# Usage:
#   ./broker-ctl.sh start [--policy /path/to/policy.json] [--verbose]
#   ./broker-ctl.sh stop
#   ./broker-ctl.sh status
#   ./broker-ctl.sh restart [--policy /path/to/policy.json]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BROKER_SCRIPT="${SCRIPT_DIR}/broker.py"
SOCKET_DIR="${ATOM_BROKER_SOCKET_DIR:-/tmp/atom-command-proxy}"
SOCKET_NAME="${ATOM_BROKER_SOCKET_NAME:-command-broker.sock}"
SOCKET_PATH="${SOCKET_DIR}/${SOCKET_NAME}"
PID_FILE="/tmp/atom-command-broker.pid"
LOG_FILE="${ATOM_BROKER_LOG:-/tmp/atom-command-broker.log}"
AUDIT_LOG="${ATOM_BROKER_AUDIT_LOG:-/tmp/atom-command-broker-audit.log}"

# Parse extra flags for start
EXTRA_FLAGS=""

case "${1:-}" in
    start)
        shift
        # Collect remaining flags
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --policy)   EXTRA_FLAGS="${EXTRA_FLAGS} --policy $2"; shift 2 ;;
                --verbose)  EXTRA_FLAGS="${EXTRA_FLAGS} --verbose"; shift ;;
                *)          echo "Unknown flag: $1"; exit 1 ;;
            esac
        done

        if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
            echo "⚠️  Broker already running (PID $(cat "${PID_FILE}"))"
            exit 0
        fi

        echo "🚀 Starting atom-command-broker..."
        mkdir -p "${SOCKET_DIR}"
        chmod 755 "${SOCKET_DIR}"
        touch "${LOG_FILE}" "${AUDIT_LOG}"

        # Use setsid to put the broker in its own session/process-group so
        # Ctrl-C / SIGINT sent to the parent shell (run.sh / agent) does NOT
        # propagate to the broker.  Falls back to nohup on platforms without
        # setsid (e.g. macOS without coreutils).
        if command -v setsid >/dev/null 2>&1; then
            setsid python3 "${BROKER_SCRIPT}" \
                --socket-dir "${SOCKET_DIR}" \
                --socket-name "${SOCKET_NAME}" \
                --log-file "${LOG_FILE}" \
                --audit-log "${AUDIT_LOG}" \
                ${EXTRA_FLAGS} \
                >> "${LOG_FILE}" 2>&1 &
        else
            nohup python3 "${BROKER_SCRIPT}" \
                --socket-dir "${SOCKET_DIR}" \
                --socket-name "${SOCKET_NAME}" \
                --log-file "${LOG_FILE}" \
                --audit-log "${AUDIT_LOG}" \
                ${EXTRA_FLAGS} \
                >> "${LOG_FILE}" 2>&1 &
        fi
        echo $! > "${PID_FILE}"

        # Wait up to 10s for the socket to appear
        for i in $(seq 1 20); do
            if [ -S "${SOCKET_PATH}" ]; then
                echo "✅ Broker started (PID $(cat "${PID_FILE}"))"
                echo "   Socket:    ${SOCKET_PATH}"
                echo "   Log:       ${LOG_FILE}"
                echo "   Audit log: ${AUDIT_LOG}"
                break
            fi
            if [ "$i" -eq 20 ]; then
                echo "❌ Failed to start after 10s. Check ${LOG_FILE}"
                cat "${LOG_FILE}" | tail -20
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
                echo "✅ Broker stopped (PID ${PID})"
            else
                echo "⚠️  PID ${PID} not running"
            fi
            rm -f "${PID_FILE}"
        else
            echo "⚠️  No PID file found"
        fi
        rm -f "${SOCKET_PATH}"
        ;;

    restart)
        shift
        "$0" stop
        "$0" start "$@"
        ;;

    status)
        if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
            echo "✅ Broker running (PID $(cat "${PID_FILE}"))"
            echo "   Socket: $([ -S "${SOCKET_PATH}" ] && echo 'exists ✅' || echo 'MISSING ❌')"
            echo "   Log:    ${LOG_FILE}"
            echo "   Audit:  ${AUDIT_LOG}"
        else
            echo "❌ Broker not running"
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status} [--policy PATH] [--verbose]"
        exit 1
        ;;
esac
