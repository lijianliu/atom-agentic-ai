#!/usr/bin/env python3
"""gsutil-proxy: A Unix-socket proxy that enforces policy on gsutil commands.

Runs on the HOST, listens on a Unix socket that gets mounted into the container.
The container has a thin wrapper that sends commands here. This daemon:
  1. Validates the command against a policy file
  2. Runs the real gsutil if allowed
  3. Returns stdout/stderr/exit_code back over the socket

Protocol (JSON over Unix socket, newline-delimited):
  Request:  {"args": ["ls", "gs://bucket/path"]}
  Response: {"stdout": "...", "stderr": "...", "exit_code": 0}
"""
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SOCKET_PATH = "/var/run/gsutil-proxy.sock"
POLICY_PATH = os.environ.get(
    "GSUTIL_POLICY", "/home/l0l0cnm/hardened-container/gsutil-policy.json"
)
REAL_GSUTIL = "/usr/bin/gsutil"
MAX_MSG_SIZE = 1024 * 64  # 64KB max message
COMMAND_TIMEOUT = 120  # seconds

logger = logging.getLogger("gsutil-proxy")


def load_policy(path: str) -> dict:
    """Load and validate the policy configuration."""
    with open(path) as f:
        policy = json.load(f)
    required = ["allowed_commands", "allowed_buckets"]
    for key in required:
        if key not in policy:
            raise ValueError(f"Policy file missing required key: {key}")
    return policy


class RateLimiter:
    """Simple per-minute sliding window rate limiter."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self.timestamps: list[float] = []
        self.lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self.lock:
            self.timestamps = [t for t in self.timestamps if now - t < 60]
            if len(self.timestamps) >= self.max_per_minute:
                return False
            self.timestamps.append(now)
            return True


def extract_gs_paths(args: list[str]) -> list[str]:
    """Pull all gs:// paths from the argument list."""
    return [a for a in args if a.startswith("gs://")]


def get_bucket_from_path(gs_path: str) -> str:
    """Extract 'gs://bucket-name' from 'gs://bucket-name/some/path'."""
    match = re.match(r"(gs://[^/]+)", gs_path)
    return match.group(1) if match else gs_path


def validate_command(args: list[str], policy: dict) -> str | None:
    """Validate a gsutil command against policy. Returns error string or None."""
    if not args:
        return "Empty command"

    # Extract the gsutil sub-command (skip flags before it)
    command = None
    for arg in args:
        if not arg.startswith("-"):
            command = arg
            break

    if command is None:
        return "No command found in arguments"

    # 1. Check command is allowed
    allowed_cmds = policy["allowed_commands"]
    if command not in allowed_cmds:
        return f"Command '{command}' is not allowed. Permitted: {allowed_cmds}"

    # 2. For 'cp' — block uploads (local → gs://)
    if command == "cp":
        if not _is_download_only(args):
            return "Only downloads (gs:// → local) are allowed, not uploads"

    # 3. Check all gs:// paths are in allowed buckets
    gs_paths = extract_gs_paths(args)
    allowed_buckets = policy["allowed_buckets"]
    for gs_path in gs_paths:
        bucket = get_bucket_from_path(gs_path)
        if bucket not in allowed_buckets:
            return f"Bucket '{bucket}' is not in the allowed list: {allowed_buckets}"

    # 4. Check for blocked flags
    blocked_flags = policy.get("blocked_flags", [])
    for arg in args:
        if arg in blocked_flags:
            return f"Flag '{arg}' is blocked by policy"

    return None


def _is_download_only(args: list[str]) -> bool:
    """For 'cp' commands, ensure it's gs:// → local (download), not upload.

    gsutil cp [flags] src dst
    - Download: src=gs://..., dst=local
    - Upload:   src=local,   dst=gs://...
    """
    non_flag_args = [a for a in args if not a.startswith("-") and a != "cp"]
    if len(non_flag_args) < 2:
        return False
    destination = non_flag_args[-1]
    # If destination is a gs:// path, it's an upload → block it
    return not destination.startswith("gs://")


def execute_gsutil(args: list[str]) -> dict:
    """Run the real gsutil and return results."""
    cmd = [REAL_GSUTIL] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {COMMAND_TIMEOUT}s",
            "exit_code": 124,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Execution error: {e}",
            "exit_code": 1,
        }


def handle_client(conn: socket.socket, policy: dict, limiter: RateLimiter):
    """Handle a single client connection."""
    try:
        data = conn.recv(MAX_MSG_SIZE)
        if not data:
            return

        request = json.loads(data.decode("utf-8"))
        args = request.get("args", [])

        logger.info("Request: gsutil %s", " ".join(args))

        # Rate limit check
        if not limiter.allow():
            response = {
                "stdout": "",
                "stderr": "Rate limit exceeded. Try again shortly.",
                "exit_code": 429,
            }
            logger.warning("Rate limited: gsutil %s", " ".join(args))
        else:
            # Policy check
            error = validate_command(args, policy)
            if error:
                response = {
                    "stdout": "",
                    "stderr": f"POLICY DENIED: {error}",
                    "exit_code": 403,
                }
                logger.warning("Denied: %s | gsutil %s", error, " ".join(args))
            else:
                # Execute
                logger.info("Allowed: gsutil %s", " ".join(args))
                response = execute_gsutil(args)

        conn.sendall(json.dumps(response).encode("utf-8"))
    except json.JSONDecodeError:
        error_resp = {
            "stdout": "",
            "stderr": "Invalid request format",
            "exit_code": 400,
        }
        conn.sendall(json.dumps(error_resp).encode("utf-8"))
    except Exception as e:
        logger.exception("Error handling client")
        error_resp = {
            "stdout": "",
            "stderr": f"Proxy error: {e}",
            "exit_code": 500,
        }
        try:
            conn.sendall(json.dumps(error_resp).encode("utf-8"))
        except Exception:
            pass
    finally:
        conn.close()


def cleanup_socket():
    """Remove stale socket file."""
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)


def main():
    # Set up logging
    policy = load_policy(POLICY_PATH)
    log_file = policy.get("log_file", "/var/log/gsutil-proxy.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    rate_limit = policy.get("rate_limit_per_minute", 60)
    limiter = RateLimiter(rate_limit)

    cleanup_socket()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    # Make socket accessible to container user (UID 1000)
    os.chmod(SOCKET_PATH, 0o777)
    server.listen(5)

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down gsutil-proxy...")
        server.close()
        cleanup_socket()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("gsutil-proxy started on %s", SOCKET_PATH)
    logger.info("Policy: %s", json.dumps(policy, indent=2))
    logger.info("Allowed commands: %s", policy["allowed_commands"])
    logger.info("Allowed buckets: %s", policy["allowed_buckets"])
    logger.info("Rate limit: %d/min", rate_limit)

    while True:
        try:
            conn, _ = server.accept()
            thread = threading.Thread(
                target=handle_client,
                args=(conn, policy, limiter),
                daemon=True,
            )
            thread.start()
        except OSError:
            break


if __name__ == "__main__":
    main()
