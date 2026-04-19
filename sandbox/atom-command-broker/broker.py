#!/usr/bin/env python3
"""atom-command-broker: Host-side command execution broker.

Runs OUTSIDE the container on the host VM. Listens on a Unix domain socket
mounted into the container. Receives structured requests from atom-command-proxy,
enforces policy, routes approved requests to the correct host-side executable,
executes them, and returns results.

Protocol: Versioned JSON over Unix socket, newline-delimited.
Supports: execute, discover, health operations.
Modes: buffered and streaming execution.
"""
import argparse
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Add broker package to path
# ---------------------------------------------------------------------------
BROKER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BROKER_DIR))

from protocol import (
    PROTO_VERSION,
    FrameType,
    ErrorCategory,
    encode_frame,
    decode_request,
    encode_response,
    make_error_response,
    make_health_response,
)
from policy import PolicyEngine
from registry import ExecutableRegistry
from adapters import get_adapter

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SOCKET_DIR = "/tmp/atom-command-proxy"
DEFAULT_SOCKET_NAME = "command-broker.sock"
DEFAULT_POLICY_PATH = None  # Will search standard locations
MAX_MSG_SIZE = 1024 * 256  # 256KB max inbound message
DEFAULT_TIMEOUT = 120
MAX_OUTPUT_SIZE = 10 * 1024 * 1024  # 10MB max output

logger = logging.getLogger("atom-command-broker")


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------
class AuditLogger:
    """Structured audit logging for all broker operations."""

    def __init__(self, log_file: str | None = None):
        self.logger = logging.getLogger("atom-command-broker.audit")
        if log_file:
            handler = logging.FileHandler(log_file)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def log(self, entry: dict):
        entry["audit_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.logger.info(json.dumps(entry, default=str))


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Command Broker
# ---------------------------------------------------------------------------
class CommandBroker:
    """Main broker that handles requests from atom-command-proxy."""

    def __init__(
        self,
        policy_engine: PolicyEngine,
        registry: ExecutableRegistry,
        audit: AuditLogger,
        rate_limiter: RateLimiter,
    ):
        self.policy = policy_engine
        self.registry = registry
        self.audit = audit
        self.rate_limiter = rate_limiter

    # ---- Handle a single client connection ----
    def handle_client(self, conn: socket.socket):
        """Handle a single client connection."""
        request_id = "unknown"
        tool = "unknown"
        start_time = time.time()
        try:
            data = self._recv_all(conn)
            if not data:
                return

            request = decode_request(data)
            request_id = request.get("request_id", str(uuid.uuid4()))
            version = request.get("version", 0)
            operation = request.get("operation", "")
            tool = request.get("tool", "")

            # Version check
            if version != PROTO_VERSION:
                resp = make_error_response(
                    request_id,
                    ErrorCategory.VALIDATION_ERROR,
                    f"Unsupported protocol version: {version} (expected {PROTO_VERSION})",
                )
                self._send_response(conn, resp)
                return

            # Rate limit
            if not self.rate_limiter.allow():
                resp = make_error_response(
                    request_id,
                    ErrorCategory.RATE_LIMITED,
                    "Rate limit exceeded. Try again shortly.",
                )
                self._send_response(conn, resp)
                return

            # Dispatch by operation
            if operation == "health":
                self._handle_health(conn, request_id)
            elif operation == "discover":
                self._handle_discover(conn, request_id)
            elif operation == "execute":
                self._handle_execute(conn, request, request_id)
            else:
                resp = make_error_response(
                    request_id,
                    ErrorCategory.VALIDATION_ERROR,
                    f"Unknown operation: {operation}",
                )
                self._send_response(conn, resp)

        except json.JSONDecodeError:
            resp = make_error_response(
                request_id,
                ErrorCategory.VALIDATION_ERROR,
                "Invalid JSON in request",
            )
            try:
                self._send_response(conn, resp)
            except Exception:
                pass
        except Exception as e:
            logger.exception("Error handling client request %s", request_id)
            resp = make_error_response(
                request_id,
                ErrorCategory.INTERNAL_ERROR,
                f"Internal broker error: {type(e).__name__}",
            )
            try:
                self._send_response(conn, resp)
            except Exception:
                pass
        finally:
            duration = time.time() - start_time
            self.audit.log({
                "request_id": request_id,
                "tool": tool,
                "duration_sec": round(duration, 3),
            })
            try:
                conn.close()
            except Exception:
                pass

    # ---- Health ----
    def _handle_health(self, conn: socket.socket, request_id: str):
        resp = make_health_response(request_id)
        self._send_response(conn, resp)

    # ---- Discover ----
    def _handle_discover(self, conn: socket.socket, request_id: str):
        tools = []
        for tool_name in self.registry.list_tools():
            adapter = get_adapter(tool_name)
            tool_policy = self.policy.get_tool_policy(tool_name)
            if tool_policy and not tool_policy.get("enabled", True):
                continue  # skip disabled tools
            info = {
                "name": tool_name,
                "description": adapter.description() if adapter else f"{tool_name} via broker",
                "supported_modes": adapter.supported_modes() if adapter else ["buffered"],
                "default_mode": adapter.default_mode() if adapter else "buffered",
            }
            if adapter:
                extra = adapter.discovery_metadata()
                if extra:
                    info.update(extra)
            tools.append(info)

        resp = {
            "version": PROTO_VERSION,
            "request_id": request_id,
            "ok": True,
            "operation": "discover",
            "tools": tools,
        }
        self._send_response(conn, resp)

    # ---- Execute ----
    def _handle_execute(self, conn: socket.socket, request: dict, request_id: str):
        tool = request.get("tool", "")
        argv = request.get("argv", [])
        requested_mode = request.get("requested_mode", "auto")
        timeout_sec = request.get("timeout_sec", DEFAULT_TIMEOUT)
        stream = request.get("stream", False)
        cwd = request.get("cwd", None)

        # Validate tool exists in registry
        executable = self.registry.get_executable(tool)
        if executable is None:
            resp = make_error_response(
                request_id,
                ErrorCategory.VALIDATION_ERROR,
                f"Unknown tool: {tool}. Use 'discover' to list available tools.",
            )
            self._send_response(conn, resp)
            self.audit.log({
                "request_id": request_id,
                "tool": tool,
                "argv": argv,
                "policy_decision": "unknown_tool",
            })
            return

        # Get adapter
        adapter = get_adapter(tool)
        if adapter is None:
            resp = make_error_response(
                request_id,
                ErrorCategory.INTERNAL_ERROR,
                f"No adapter found for tool: {tool}",
            )
            self._send_response(conn, resp)
            return

        # Adapter-level validation
        validation_error = adapter.validate(argv)
        if validation_error:
            resp = make_error_response(
                request_id,
                ErrorCategory.VALIDATION_ERROR,
                validation_error,
            )
            self._send_response(conn, resp)
            self.audit.log({
                "request_id": request_id,
                "tool": tool,
                "argv": argv,
                "policy_decision": "validation_error",
                "detail": validation_error,
            })
            return

        # Normalize arguments through adapter
        normalized_argv = adapter.normalize_args(argv)

        # Policy enforcement
        tool_policy = self.policy.get_tool_policy(tool)
        policy_result = self.policy.evaluate(tool, normalized_argv, tool_policy)
        if not policy_result["allowed"]:
            resp = make_error_response(
                request_id,
                ErrorCategory.POLICY_DENIED,
                f"POLICY DENIED: {policy_result['reason']}",
            )
            self._send_response(conn, resp)
            self.audit.log({
                "request_id": request_id,
                "tool": tool,
                "argv": normalized_argv,
                "policy_decision": "denied",
                "reason": policy_result["reason"],
            })
            return

        # Determine effective mode
        effective_mode, mode_reason = adapter.effective_mode(
            normalized_argv, requested_mode
        )
        if stream and effective_mode != "streaming":
            # Caller explicitly asked for streaming via flag
            effective_mode = "streaming"
            mode_reason = "caller_explicit_stream_flag"

        # Apply timeout limits from policy
        max_timeout = DEFAULT_TIMEOUT
        if tool_policy:
            max_timeout = tool_policy.get("max_timeout_sec", DEFAULT_TIMEOUT)
        effective_timeout = min(timeout_sec, max_timeout)

        # Apply output size limit from policy
        max_output = MAX_OUTPUT_SIZE
        if tool_policy:
            max_output = tool_policy.get("max_output_bytes", MAX_OUTPUT_SIZE)

        # Build full command
        cmd = adapter.build_command(executable, normalized_argv)

        # Build environment
        env = adapter.build_env(tool_policy)

        self.audit.log({
            "request_id": request_id,
            "tool": tool,
            "argv": normalized_argv,
            "policy_decision": "allowed",
            "effective_mode": effective_mode,
            "mode_reason": mode_reason,
            "timeout_sec": effective_timeout,
        })

        logger.info(
            "EXECUTE [%s] tool=%s mode=%s timeout=%ds cmd=%s",
            request_id[:8], tool, effective_mode, effective_timeout,
            " ".join(cmd),
        )

        # Execute
        if effective_mode == "streaming":
            self._execute_streaming(
                conn, request_id, cmd, env, effective_timeout,
                max_output, effective_mode, mode_reason, cwd,
            )
        else:
            self._execute_buffered(
                conn, request_id, cmd, env, effective_timeout,
                max_output, effective_mode, mode_reason, cwd,
            )

    # ---- Buffered execution ----
    def _execute_buffered(
        self, conn, request_id, cmd, env, timeout, max_output,
        effective_mode, mode_reason, cwd,
    ):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                env=env,
                cwd=cwd,
            )
            stdout = result.stdout
            stderr = result.stderr
            # Truncate if needed
            if len(stdout) > max_output:
                stdout = stdout[:max_output]
                stderr += (
                    f"\n[atom-command-broker] Output truncated to "
                    f"{max_output} bytes".encode()
                )

            resp = {
                "version": PROTO_VERSION,
                "request_id": request_id,
                "ok": result.returncode == 0,
                "operation": "execute",
                "effective_mode": effective_mode,
                "mode_reason": mode_reason,
                "exit_code": result.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
            self._send_response(conn, resp)
            self.audit.log({
                "request_id": request_id,
                "exit_code": result.returncode,
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            })
        except subprocess.TimeoutExpired:
            resp = make_error_response(
                request_id,
                ErrorCategory.TIMEOUT,
                f"Command timed out after {timeout}s",
            )
            resp["exit_code"] = 124
            self._send_response(conn, resp)
            self.audit.log({
                "request_id": request_id,
                "exit_code": 124,
                "error": "timeout",
            })
        except FileNotFoundError as e:
            resp = make_error_response(
                request_id,
                ErrorCategory.EXECUTION_FAILURE,
                f"Executable not found: {e}",
            )
            resp["exit_code"] = 127
            self._send_response(conn, resp)
        except Exception as e:
            resp = make_error_response(
                request_id,
                ErrorCategory.EXECUTION_FAILURE,
                f"Execution error: {type(e).__name__}: {e}",
            )
            resp["exit_code"] = 1
            self._send_response(conn, resp)

    # ---- Streaming execution ----
    def _execute_streaming(
        self, conn, request_id, cmd, env, timeout, max_output,
        effective_mode, mode_reason, cwd,
    ):
        total_output = 0
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
            )

            # Send start frame
            start_frame = encode_frame(FrameType.START, {
                "request_id": request_id,
                "effective_mode": effective_mode,
                "mode_reason": mode_reason,
            })
            conn.sendall(start_frame)

            # We'll read stdout and stderr in threads
            output_lock = threading.Lock()
            truncated = False

            def stream_pipe(pipe, frame_type: FrameType):
                nonlocal total_output, truncated
                try:
                    while True:
                        chunk = pipe.read(4096)
                        if not chunk:
                            break
                        with output_lock:
                            if truncated:
                                continue
                            total_output += len(chunk)
                            if total_output > max_output:
                                truncated = True
                                frame = encode_frame(FrameType.STDERR, {
                                    "data": "[atom-command-broker] Output truncated\n",
                                })
                                try:
                                    conn.sendall(frame)
                                except Exception:
                                    pass
                                continue
                        text = chunk.decode("utf-8", errors="replace")
                        frame = encode_frame(frame_type, {"data": text})
                        try:
                            conn.sendall(frame)
                        except (BrokenPipeError, ConnectionError):
                            break
                except Exception:
                    pass

            stdout_thread = threading.Thread(
                target=stream_pipe,
                args=(proc.stdout, FrameType.STDOUT),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=stream_pipe,
                args=(proc.stderr, FrameType.STDERR),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            # Wait for process with timeout
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                exit_code = 124

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            # Send exit frame
            exit_frame = encode_frame(FrameType.EXIT, {
                "request_id": request_id,
                "exit_code": exit_code,
            })
            conn.sendall(exit_frame)

            self.audit.log({
                "request_id": request_id,
                "exit_code": exit_code,
                "total_output_bytes": total_output,
                "streaming": True,
            })

        except FileNotFoundError as e:
            error_frame = encode_frame(FrameType.ERROR, {
                "request_id": request_id,
                "category": ErrorCategory.EXECUTION_FAILURE.value,
                "message": f"Executable not found: {e}",
                "exit_code": 127,
            })
            conn.sendall(error_frame)
        except Exception as e:
            logger.exception("Streaming execution error for %s", request_id)
            error_frame = encode_frame(FrameType.ERROR, {
                "request_id": request_id,
                "category": ErrorCategory.EXECUTION_FAILURE.value,
                "message": f"Execution error: {type(e).__name__}: {e}",
                "exit_code": 1,
            })
            try:
                conn.sendall(error_frame)
            except Exception:
                pass

    # ---- I/O helpers ----
    def _recv_all(self, conn: socket.socket) -> bytes:
        """Receive complete message from client."""
        chunks = []
        total = 0
        conn.settimeout(30)  # 30s to receive full request
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MSG_SIZE:
                raise ValueError("Request too large")
            # Check if we have a complete JSON message
            data = b"".join(chunks)
            try:
                json.loads(data)
                return data  # valid JSON, we're done
            except json.JSONDecodeError:
                continue  # keep reading
        return b"".join(chunks)

    def _send_response(self, conn: socket.socket, resp: dict):
        """Send a complete JSON response."""
        data = json.dumps(resp).encode("utf-8")
        conn.sendall(data)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class BrokerServer:
    """Unix domain socket server for atom-command-broker."""

    def __init__(self, socket_path: str, broker: CommandBroker):
        self.socket_path = socket_path
        self.broker = broker
        self.server = None
        self.running = False

    def start(self):
        """Start the broker server."""
        self._cleanup_socket()
        socket_dir = os.path.dirname(self.socket_path)
        os.makedirs(socket_dir, exist_ok=True)
        os.chmod(socket_dir, 0o755)

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)  # world-accessible: container UID differs from host UID
        self.server.listen(10)
        self.running = True

        logger.info("atom-command-broker started on %s", self.socket_path)

        while self.running:
            try:
                conn, _ = self.server.accept()
                thread = threading.Thread(
                    target=self.broker.handle_client,
                    args=(conn,),
                    daemon=True,
                )
                thread.start()
            except OSError:
                if self.running:
                    logger.exception("Server accept error")
                break

    def stop(self):
        """Stop the broker server."""
        self.running = False
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        self._cleanup_socket()
        logger.info("atom-command-broker stopped")

    def _cleanup_socket(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


# ---------------------------------------------------------------------------
# Policy path resolution
# ---------------------------------------------------------------------------
def resolve_policy_path(cli_path: str | None) -> str | None:
    """Resolve policy file path with standard search order."""
    if cli_path:
        return cli_path
    env_override = os.environ.get("ATOM_BROKER_POLICY")
    if env_override and os.path.isfile(env_override):
        return env_override
    user_policy = Path.home() / ".config" / "atom-agentic-ai" / "broker-policy.yaml"
    if user_policy.is_file():
        return str(user_policy)
    user_policy_json = Path.home() / ".config" / "atom-agentic-ai" / "broker-policy.json"
    if user_policy_json.is_file():
        return str(user_policy_json)
    default_policy = BROKER_DIR / "default-policy.json"
    if default_policy.is_file():
        return str(default_policy)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="atom-command-broker: Host-side command execution broker",
    )
    parser.add_argument(
        "--socket-dir",
        default=os.environ.get("ATOM_BROKER_SOCKET_DIR", DEFAULT_SOCKET_DIR),
        help=f"Directory for Unix domain socket (default: {DEFAULT_SOCKET_DIR})",
    )
    parser.add_argument(
        "--socket-name",
        default=os.environ.get("ATOM_BROKER_SOCKET_NAME", DEFAULT_SOCKET_NAME),
        help=f"Socket file name (default: {DEFAULT_SOCKET_NAME})",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="Path to policy configuration file",
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("ATOM_BROKER_LOG", "/tmp/atom-command-broker.log"),
        help="Log file path",
    )
    parser.add_argument(
        "--audit-log",
        default=os.environ.get("ATOM_BROKER_AUDIT_LOG", "/tmp/atom-command-broker-audit.log"),
        help="Audit log file path",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=int(os.environ.get("ATOM_BROKER_RATE_LIMIT", "120")),
        help="Max requests per minute (default: 120)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(args.log_file),
            logging.StreamHandler(sys.stderr),
        ],
    )

    # Policy
    policy_path = resolve_policy_path(args.policy)
    policy_engine = PolicyEngine(policy_path)
    logger.info("Policy loaded from: %s", policy_path or "(built-in defaults)")

    # Registry
    registry = ExecutableRegistry()
    logger.info("Registry: %d tools registered", len(registry.list_tools()))
    for t in registry.list_tools():
        logger.info("  %s -> %s", t, registry.get_executable(t))

    # Audit
    audit = AuditLogger(args.audit_log)

    # Rate limiter
    rate_limiter = RateLimiter(args.rate_limit)

    # Broker
    broker = CommandBroker(policy_engine, registry, audit, rate_limiter)

    # Server
    socket_path = os.path.join(args.socket_dir, args.socket_name)
    server = BrokerServer(socket_path, broker)

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.start()


if __name__ == "__main__":
    main()
