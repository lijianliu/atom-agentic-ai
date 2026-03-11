#!/bin/bash
# gsutil wrapper — thin client that sends commands to the host proxy via Unix socket.
# This script lives INSIDE the container. The real gsutil runs OUTSIDE.
set -euo pipefail

SOCKET_PATH="/var/run/gsutil-proxy.sock"

if [ ! -S "${SOCKET_PATH}" ]; then
    echo "ERROR: gsutil proxy socket not found at ${SOCKET_PATH}" >&2
    echo "The gsutil proxy service may not be running on the host." >&2
    exit 1
fi

# Build JSON request and send over Unix socket, print response directly
exec python3 -c "
import json, socket, sys

args = sys.argv[1:]
request = json.dumps({'args': args})

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
    data = b''.join(chunks)
    resp = json.loads(data.decode('utf-8'))
    if resp.get('stdout'):
        print(resp['stdout'], end='', file=sys.stdout)
    if resp.get('stderr'):
        print(resp['stderr'], end='', file=sys.stderr)
    sys.exit(resp.get('exit_code', 1))
finally:
    sock.close()
" "$@"
