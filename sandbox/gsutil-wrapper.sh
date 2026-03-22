#!/bin/bash
# gsutil-wrapper.sh
# Thin client that forwards gsutil commands to the host proxy via Unix socket.
# Lives INSIDE the container; the real gsutil + credentials live OUTSIDE.
#
# Socket path: /tmp/gsutil-proxy/gsutil-proxy.sock (directory mounted in by sandbox.sh)
set -euo pipefail

SOCKET_PATH="/tmp/gsutil-proxy/gsutil-proxy.sock"

if [ ! -S "${SOCKET_PATH}" ]; then
    echo "ERROR: gsutil proxy socket not found at ${SOCKET_PATH}" >&2
    echo "The gsutil proxy service may not be running on the host." >&2
    echo "On macOS, start it with: sandbox/gsutil-proxy-ctl.sh start" >&2
    exit 1
fi

exec python3 -c "
import json, socket, sys

request = json.dumps({'args': sys.argv[1:]})
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.connect('${SOCKET_PATH}')
    sock.sendall(request.encode('utf-8'))
    sock.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    resp = json.loads(b''.join(chunks).decode('utf-8'))
    if resp.get('stdout'):
        print(resp['stdout'], end='', file=sys.stdout)
    if resp.get('stderr'):
        print(resp['stderr'], end='', file=sys.stderr)
    sys.exit(resp.get('exit_code', 1))
finally:
    sock.close()
" "$@"
