#!/usr/bin/env python3
"""
test-mcp.py — MCP sandbox smoke-test (zero external deps, stdlib only)

Usage:
    python3 sandbox/test-mcp.py
    python3 sandbox/test-mcp.py --port 9100
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time

# ─ ANSI colours ────────────────────────────────────────────────────────────
GREEN = "\033[92m"; RED = "\033[91m"; CYAN = "\033[96m"
YELLOW = "\033[93m"; DIM = "\033[2m"; BOLD = "\033[1m"; RST = "\033[0m"


def _ok(msg: str):   print(f"  {GREEN}✅{RST} {msg}")
def _fail(msg: str): print(f"  {RED}❌ {msg}{RST}"); sys.exit(1)
def _info(msg: str): print(f"     {DIM}{msg}{RST}")
def _hdr(n: int, total: int, title: str):
    print(f"\n{BOLD}{CYAN}── {n}/{total}  {title}{RST}")


# ─ MCP client (SSE transport, Unix socket) ─────────────────────────────

def _make_socket(tcp_addr: tuple) -> socket.socket:
    """Create and connect a raw TCP socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(tcp_addr)
    return s


class MCPClient:
    """
    Thin MCP-over-SSE client (TCP only).

    One persistent SSE connection receives all push events.
    Each call() sends the POST through a fresh short-lived connection.
    """

    def __init__(self, tcp_addr: tuple,
                 sse_timeout: int = 8) -> None:
        self._tcp  = tcp_addr
        self._msg_path: str = ""
        self._sse: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self._sse_timeout = sse_timeout
        self._id = 0

    # ─ connect ────────────────────────────────────────────────────────────
    def connect(self) -> str:
        """Open SSE stream, return messages endpoint path."""
        s = _make_socket(self._tcp)
        s.settimeout(self._sse_timeout)
        s.sendall(
            b"GET /sse HTTP/1.1\r\nHost: sandbox\r\n"
            b"Accept: text/event-stream\r\nConnection: keep-alive\r\n\r\n"
        )
        buf = b""
        deadline = time.monotonic() + self._sse_timeout
        while time.monotonic() < deadline:
            try:
                buf += s.recv(4096)
            except socket.timeout:
                break
            if b"event: endpoint" in buf:
                break
        for line in buf.split(b"\n"):
            if line.startswith(b"data:"):
                self._msg_path = line[5:].strip().decode()
                break
        if not self._msg_path:
            raise RuntimeError(f"No SSE endpoint.  raw={buf[:300]!r}")
        self._sse = s
        self._buf = buf  # keep whatever arrived alongside the handshake
        return self._msg_path

    def close(self) -> None:
        if self._sse:
            try: self._sse.close()
            except OSError: pass

    # ─ send a single JSON-RPC call ───────────────────────────────────────
    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        req = (
            f"POST {self._msg_path} HTTP/1.1\r\nHost: sandbox\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + body
        s = _make_socket(self._tcp)
        s.sendall(req)
        # The server returns 202 Accepted almost immediately.
        # Use a short timeout so we don't block and miss the SSE event.
        s.settimeout(0.5)
        try:
            while s.recv(4096): pass
        except (socket.timeout, OSError):
            pass
        s.close()

    # ─ wait for one SSE message event ──────────────────────────────────
    def _recv(self, timeout: int = 10) -> dict:
        """Block until a JSON-RPC response arrives on the SSE stream."""
        assert self._sse
        self._sse.settimeout(1)            # short recv slices so we can loop
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._sse.recv(8192)
                if not chunk:
                    break
                self._buf += chunk
            except socket.timeout:
                pass
            # Scan for any 'data:' line containing a JSON-RPC response.
            # We search raw bytes so chunked-encoding size headers don't matter.
            for line in self._buf.split(b"\n"):
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload.startswith(b"{"):
                    continue
                try:
                    obj = json.loads(payload)
                    if "jsonrpc" in obj:
                        # consume everything up to and including this line
                        idx = self._buf.find(payload)
                        if idx != -1:
                            self._buf = self._buf[idx + len(payload):]
                        return obj
                except json.JSONDecodeError:
                    pass
        return {}  # timed out

    # ─ high-level helpers ───────────────────────────────────────────────────
    def request(self, method: str, params: dict = {}, timeout: int = 8) -> dict:
        self._id += 1
        self._post({"jsonrpc": "2.0", "id": self._id,
                    "method": method, "params": params})
        return self._recv(timeout)

    def call_tool(self, name: str, args: dict, timeout: int = 12) -> str:
        resp = self.request("tools/call",
                            {"name": name, "arguments": args}, timeout=timeout)
        content = resp.get("result", {}).get("content", [])
        return content[0].get("text", "").strip() if content else "(empty)"


# ─ Tests ───────────────────────────────────────────────────────────────────

TOTAL = 6


def run(tcp_addr: tuple) -> None:
    t0 = time.monotonic()
    addr_str = f"{tcp_addr[0]}:{tcp_addr[1]}"
    print(f"{BOLD}\n🐶  MCP Sandbox Test Suite{RST}  {DIM}{addr_str}{RST}\n")

    # 1 ─ SSE handshake
    _hdr(1, TOTAL, "SSE handshake")
    client = MCPClient(tcp_addr=tcp_addr)
    try:
        ep = client.connect()
    except Exception as exc:
        _fail(f"connect: {exc}")
    _ok(f"stream open  →  {ep}")

    # 2 ─ initialize
    _hdr(2, TOTAL, "initialize")
    resp = client.request("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "mcp-smoke-test", "version": "1.0"},
    })
    r = resp.get("result", {})
    if not r:
        _fail(f"bad initialize response: {resp}")
    proto = r.get("protocolVersion", "?")
    caps  = list(r.get("capabilities", {}).keys())
    _ok(f"protocol={proto}  caps={caps}")

    # 3 ─ tools/list
    _hdr(3, TOTAL, "tools/list")
    resp  = client.request("tools/list", {})
    tools = {t["name"] for t in resp.get("result", {}).get("tools", [])}
    expected = {"execute_command", "read_file", "write_file",
                "list_dir", "delete_file", "append_file"}
    for name in sorted(expected):
        if name in tools: _ok(name)
        else: _fail(f"{name} missing from tools/list")

    # 4 ─ execute_command
    _hdr(4, TOTAL, "execute_command")
    out = client.call_tool("execute_command",
                           {"command": "echo HELLO_MCP && whoami && id && pwd"})
    if "HELLO_MCP" not in out:
        _fail(f"unexpected output:\n{out}")
    _ok("command ran inside sandbox")
    for line in out.splitlines(): _info(line)

    # 5 ─ file operations
    _hdr(5, TOTAL, "file operations  (write → append → read → list → delete)")
    FILE = "__mcp_test__.txt"

    out = client.call_tool("write_file",  {"path": FILE, "content": "line1\n"})
    if "Error" in out: _fail(f"write_file: {out}")
    _ok(f"write   {out}")

    out = client.call_tool("append_file", {"path": FILE, "content": "line2\n"})
    if "Error" in out: _fail(f"append_file: {out}")
    _ok(f"append  {out}")

    out = client.call_tool("read_file",   {"path": FILE})
    if "line1" not in out or "line2" not in out:
        _fail(f"read_file content wrong: {out}")
    _ok(f"read    content verified ✓")

    out = client.call_tool("list_dir",    {"path": "."})
    if FILE not in out: _fail(f"list_dir missing file: {out}")
    _ok(f"list    {FILE} visible ✓")

    out = client.call_tool("delete_file", {"path": FILE})
    if "Error" in out: _fail(f"delete_file: {out}")
    _ok(f"delete  {out}")

    # 6 ─ security spot-check
    _hdr(6, TOTAL, "security spot-check")

    # not root
    out = client.call_tool("execute_command", {"command": "whoami"})
    user = out.strip().splitlines()[-1] if out else "?"
    if user.strip() == "root":
        _fail("running as root — very bad!")
    _ok(f"non-root user ({user.strip()})")

    # path traversal blocked
    out = client.call_tool("read_file", {"path": "../../etc/passwd"})
    if "root:x" in out:
        _fail(f"path traversal succeeded! got: {out[:80]}")
    _ok(f"path traversal blocked  (→ {out[:60]!r})")

    # network check (best-effort; no fail if container has network)
    out = client.call_tool("execute_command",
                           {"command": "curl -s --max-time 2 http://1.1.1.1 2>&1 || true"},
                           timeout=8)
    net_blocked = any(k in out.lower() for k in
        ["network unreachable", "could not resolve",
         "connection refused", "operation not permitted",
         "name or service not known", "timed out", "curl: ("])
    if net_blocked:
        _ok("outbound network blocked ✓")
    else:
        print(f"  {YELLOW}⚠️  network NOT blocked (container has outbound access){RST}")

    client.close()

    elapsed = time.monotonic() - t0
    print(f"\n{BOLD}{GREEN}🎉  All tests passed! ({elapsed:.1f}s){RST}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100,
                    help="TCP port (default: 9100)")
    args = ap.parse_args()
    run(tcp_addr=("127.0.0.1", args.port))
