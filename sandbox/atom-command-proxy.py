#!/usr/bin/env python3
"""atom-command-proxy: Container-side thin command relay.

Runs INSIDE the container. Forwards structured requests to atom-command-broker
on the host VM over a Unix domain socket. Does NOT execute real host tools,
hold credentials, or enforce policy.

Usage:
    atom-command-proxy <tool> [args...]
    atom-command-proxy discover
    atom-command-proxy health

Examples:
    atom-command-proxy gsutil ls gs://my-bucket
    atom-command-proxy gcloud storage buckets list
    atom-command-proxy kafka-console-consumer --bootstrap-server host:9092 --topic t --max-messages 10
    atom-command-proxy discover
    atom-command-proxy health
"""
import json
import os
import socket
import struct
import sys
import uuid

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SOCKET_DIR = os.environ.get("ATOM_PROXY_SOCKET_DIR", "/tmp/atom-command-proxy")
SOCKET_NAME = os.environ.get("ATOM_PROXY_SOCKET_NAME", "command-broker.sock")
SOCKET_PATH = os.path.join(SOCKET_DIR, SOCKET_NAME)

PROTO_VERSION = 1
MAX_RESPONSE_SIZE = 16 * 1024 * 1024  # 16 MB max buffered response
CONNECT_TIMEOUT = 10   # seconds
RECV_TIMEOUT = 300     # seconds (5 min for long commands)


# ---------------------------------------------------------------------------
# Frame decoding for streaming mode
# ---------------------------------------------------------------------------
def decode_frame(buf: bytes):
    """Decode one length-prefixed frame from buffer.

    Frame format: [4 bytes uint32 big-endian length][JSON payload]
    Returns (frame_dict, remaining_buffer) or (None, buffer) if incomplete.
    """
    if len(buf) < 4:
        return None, buf
    length = struct.unpack("!I", buf[:4])[0]
    if len(buf) < 4 + length:
        return None, buf
    frame_data = buf[4:4 + length]
    remaining = buf[4 + length:]
    frame = json.loads(frame_data.decode("utf-8"))
    return frame, remaining


def is_streaming_response(data: bytes) -> bool:
    """Detect whether the response is streaming (length-prefixed frames)
    or buffered (plain JSON).

    Streaming frames start with a 4-byte length prefix. Plain JSON starts
    with '{'. We check by trying to parse as JSON first.
    """
    if not data:
        return False
    # If it starts with '{', try JSON
    if data[0:1] == b"{":
        try:
            json.loads(data)
            return False  # valid JSON → buffered
        except json.JSONDecodeError:
            pass
    # If starts with 4-byte length prefix, likely streaming
    if len(data) >= 4:
        length = struct.unpack("!I", data[:4])[0]
        # Sanity check: length should be reasonable
        if 0 < length < MAX_RESPONSE_SIZE:
            return True
    return False


# ---------------------------------------------------------------------------
# Socket communication
# ---------------------------------------------------------------------------
def send_request(request: dict) -> int:
    """Send a request to the broker and handle the response.

    Returns the exit code to use.
    """
    if not os.path.exists(SOCKET_PATH):
        print(
            f"ERROR: atom-command-broker socket not found at {SOCKET_PATH}\n"
            f"The broker may not be running on the host.\n"
            f"Start it with: sandbox/atom-command-broker/broker-ctl.sh start",
            file=sys.stderr,
        )
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)

    try:
        sock.connect(SOCKET_PATH)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
        print(
            f"ERROR: Cannot connect to atom-command-broker at {SOCKET_PATH}: {e}",
            file=sys.stderr,
        )
        return 1

    # Send request
    data = json.dumps(request).encode("utf-8")
    try:
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
    except (BrokenPipeError, OSError) as e:
        print(f"ERROR: Failed to send request to broker: {e}", file=sys.stderr)
        return 1

    # Receive response — may be buffered JSON or streaming frames
    sock.settimeout(RECV_TIMEOUT)
    buf = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk

            # Check if we're in streaming mode (after first data arrives)
            if is_streaming_response(buf):
                return _handle_streaming(sock, buf)

            # Safety: don't accumulate forever for buffered mode
            if len(buf) > MAX_RESPONSE_SIZE:
                print(
                    "ERROR: Response too large from broker",
                    file=sys.stderr,
                )
                return 1
    except socket.timeout:
        print("ERROR: Timeout waiting for broker response", file=sys.stderr)
        return 124
    finally:
        sock.close()

    if not buf:
        print("ERROR: Empty response from broker", file=sys.stderr)
        return 1

    # Buffered response
    return _handle_buffered(buf)


def _handle_buffered(data: bytes) -> int:
    """Handle a buffered JSON response."""
    try:
        resp = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        print("ERROR: Invalid JSON response from broker", file=sys.stderr)
        return 1

    operation = resp.get("operation", "")

    if operation == "health":
        return _display_health(resp)
    elif operation == "discover":
        return _display_discover(resp)
    else:
        return _display_execute(resp)


def _handle_streaming(sock: socket.socket, initial_buf: bytes) -> int:
    """Handle streaming frame responses."""
    buf = initial_buf
    exit_code = 1

    try:
        while True:
            # Try to decode frames from buffer
            while True:
                frame, buf = decode_frame(buf)
                if frame is None:
                    break  # need more data

                frame_type = frame.get("frame_type", "")

                if frame_type == "start":
                    pass  # could print debug info
                elif frame_type == "stdout":
                    text = frame.get("data", "")
                    if text:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                elif frame_type == "stderr":
                    text = frame.get("data", "")
                    if text:
                        sys.stderr.write(text)
                        sys.stderr.flush()
                elif frame_type == "exit":
                    exit_code = frame.get("exit_code", 1)
                    return exit_code
                elif frame_type == "error":
                    msg = frame.get("message", "Unknown error")
                    print(f"BROKER ERROR: {msg}", file=sys.stderr)
                    exit_code = frame.get("exit_code", 1)
                    return exit_code

            # Read more data
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                print("ERROR: Timeout during streaming", file=sys.stderr)
                return 124
    finally:
        sock.close()

    return exit_code


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def _display_execute(resp: dict) -> int:
    """Display an execute response."""
    stdout = resp.get("stdout", "")
    stderr = resp.get("stderr", "")
    exit_code = resp.get("exit_code", 1)

    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")

    return exit_code


def _display_health(resp: dict) -> int:
    """Display a health check response."""
    if resp.get("ok"):
        print(f"Broker status: {resp.get('status', 'unknown')}")
        print(f"Protocol version: {resp.get('broker_version', '?')}")
        print(f"Timestamp: {resp.get('timestamp', '?')}")
        return 0
    else:
        print(f"Broker unhealthy: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1


def _display_discover(resp: dict) -> int:
    """Display a discover response."""
    if not resp.get("ok"):
        print(f"Discovery failed: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1

    tools = resp.get("tools", [])
    if not tools:
        print("No tools available via broker.")
        return 0

    # Machine-readable if stdout is a pipe; human-readable if terminal
    if sys.stdout.isatty():
        print(f"Available tools ({len(tools)}):\n")
        for t in tools:
            name = t.get("name", "?")
            desc = t.get("description", "")
            modes = ", ".join(t.get("supported_modes", []))
            default = t.get("default_mode", "?")
            print(f"  {name}")
            if desc:
                print(f"    {desc}")
            print(f"    modes: {modes} (default: {default})")
            examples = t.get("examples", [])
            if examples:
                print(f"    examples:")
                for ex in examples[:3]:
                    print(f"      atom-command-proxy {ex}")
            print()
    else:
        # JSON output for piping
        print(json.dumps(resp, indent=2))

    return 0


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------
def build_execute_request(
    tool: str,
    argv: list[str],
    mode: str = "auto",
    timeout: int | None = None,
    stream: bool = False,
    cwd: str | None = None,
) -> dict:
    """Build an execute request."""
    req = {
        "version": PROTO_VERSION,
        "request_id": str(uuid.uuid4()),
        "operation": "execute",
        "tool": tool,
        "argv": argv,
        "requested_mode": mode,
    }
    if timeout is not None:
        req["timeout_sec"] = timeout
    if stream:
        req["stream"] = True
    if cwd:
        req["cwd"] = cwd
    return req


def build_discover_request() -> dict:
    return {
        "version": PROTO_VERSION,
        "request_id": str(uuid.uuid4()),
        "operation": "discover",
    }


def build_health_request() -> dict:
    return {
        "version": PROTO_VERSION,
        "request_id": str(uuid.uuid4()),
        "operation": "health",
    }


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------
def parse_proxy_args(args: list[str]):
    """Parse atom-command-proxy CLI arguments.

    Returns (operation, tool, argv, options).
    """
    if not args:
        return "help", None, [], {}

    # Extract proxy-level flags before the tool name
    mode = "auto"
    timeout = None
    stream = False
    remaining = list(args)

    while remaining and remaining[0].startswith("--"):
        flag = remaining.pop(0)
        if flag == "--mode" and remaining:
            mode = remaining.pop(0)
        elif flag == "--timeout" and remaining:
            timeout = int(remaining.pop(0))
        elif flag == "--stream":
            stream = True
        elif flag == "--help":
            return "help", None, [], {}
        else:
            # Unknown flag — put it back, it might be for the tool
            remaining.insert(0, flag)
            break

    if not remaining:
        return "help", None, [], {}

    first = remaining[0]

    if first == "discover":
        return "discover", None, [], {}
    elif first == "health":
        return "health", None, [], {}
    elif first == "help" or first == "--help":
        return "help", None, [], {}
    else:
        tool = remaining[0]
        argv = remaining[1:]
        return "execute", tool, argv, {
            "mode": mode,
            "timeout": timeout,
            "stream": stream,
        }


def print_usage():
    """Print usage information."""
    print("""atom-command-proxy — Thin command relay for atom-command-broker

Usage:
    atom-command-proxy <tool> [args...]         Execute a command via broker
    atom-command-proxy discover                 List available tools
    atom-command-proxy health                   Check broker health

Options (before tool name):
    --mode <buffered|streaming|auto>    Request execution mode (default: auto)
    --timeout <seconds>                 Request timeout
    --stream                            Request streaming mode

Examples:
    atom-command-proxy gsutil ls gs://my-bucket
    atom-command-proxy gcloud storage buckets list
    atom-command-proxy kafka-console-consumer --bootstrap-server host:9092 --topic t --max-messages 10
    atom-command-proxy --stream kafka-console-consumer --bootstrap-server host:9092 --topic t
    atom-command-proxy discover
    atom-command-proxy health

Environment:
    ATOM_PROXY_SOCKET_DIR    Socket directory (default: /tmp/atom-command-proxy)
    ATOM_PROXY_SOCKET_NAME   Socket file name (default: command-broker.sock)
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    operation, tool, argv, options = parse_proxy_args(sys.argv[1:])

    if operation == "help":
        print_usage()
        sys.exit(0)
    elif operation == "health":
        req = build_health_request()
    elif operation == "discover":
        req = build_discover_request()
    elif operation == "execute":
        req = build_execute_request(
            tool=tool,
            argv=argv,
            mode=options.get("mode", "auto"),
            timeout=options.get("timeout"),
            stream=options.get("stream", False),
        )
    else:
        print_usage()
        sys.exit(1)

    exit_code = send_request(req)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
