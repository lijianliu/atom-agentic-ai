# MCP Server Design — Implementation Options

> **MCP** = Model Context Protocol. An open standard by Anthropic that lets AI
> agents talk to tools and services via a structured JSON-RPC protocol.
> This document captures every transport option considered during the design
> of the AtomAI hardened sandbox MCP server, with honest tradeoffs for each.

---

## Table of Contents

1. [What is MCP?](#1-what-is-mcp)
2. [What is SSE?](#2-what-is-sse)
3. [Option 1 — stdio transport](#3-option-1--stdio-transport)
4. [Option 2 — TCP on localhost (chosen)](#4-option-2--tcp-on-localhost-chosen)
5. [Option 3 — Unix socket + docker exec relay](#5-option-3--unix-socket--docker-exec-relay)
6. [Option 4 — Named pipes (FIFOs)](#6-option-4--named-pipes-fifos)
7. [Comparison table](#7-comparison-table)
8. [What we actually built](#8-what-we-actually-built)
9. [Testing with curl](#9-testing-with-curl)
10. [Lessons learned](#10-lessons-learned)

---

## 1. What is MCP?

MCP is a client-server protocol where an **AI agent** (the client) calls
**tools** exposed by an **MCP server**. Communication is JSON-RPC 2.0.

```
AI Agent (client)          MCP Server
─────────────────          ──────────────────────────────
initialize          ──▶    handshake, return capabilities
tools/list          ──▶    return list of available tools
tools/call          ──▶    execute tool, return result
```

MCP supports two transports:

| Transport | How it works |
|---|---|
| **stdio** | Agent spawns server as subprocess; JSON-RPC over stdin/stdout |
| **SSE** | Server is an HTTP server; agent connects via Server-Sent Events |

---

## 2. What is SSE?

**SSE = Server-Sent Events.** A browser/HTTP standard where the server pushes
data to the client over a single long-lived HTTP connection (one-way:
server → client only).

```
GET /sse HTTP/1.1
Accept: text/event-stream       ← magic header: "keep this open"

HTTP/1.1 200 OK
Content-Type: text/event-stream

event: endpoint                 ← server pushes immediately
data: /messages/?session_id=abc123

: ping - 2026-03-22 02:06:25   ← keepalive every 30s, connection stays open

event: message                  ← result pushed when ready
data: {"jsonrpc":"2.0","result": ...}
```

MCP's SSE transport uses **two channels** simultaneously:

```
Client                          MCP Server
  │                                │
  │──── GET /sse ─────────────────▶│  long-lived, never closes
  │◀─── stream of events ──────────│  server pushes results here
  │                                │
  │──── POST /messages/?session=X ▶│  short-lived per request
  │◀─── 202 Accepted ──────────────│  result arrives on SSE, not here
```

### SSE vs HTTP/2

A common misconception: SSE is **not** HTTP/2 Server Push.

| | SSE | HTTP/2 Server Push |
|---|---|---|
| Direction | Server → Client | Server → Client |
| Protocol | HTTP/1.1 ✅ | HTTP/2 only |
| Pushes | Arbitrary events/data | Pre-emptive HTTP responses |
| Status | Alive and well | **Killed by Chrome in 2022** 💀 |
| Use case | Real-time feeds, MCP | CSS before you asked for it |

HTTP/2's real superpower is **multiplexed streams**, not push. SSE predates
HTTP/2 and works perfectly over HTTP/1.1.

---

## 3. Option 1 — stdio transport

### How it works

The MCP client spawns the server as a subprocess and communicates via
stdin/stdout. This is the original, idiomatic MCP transport.

```bash
# Claude Desktop does exactly this:
docker exec -i sandbox-mcp python3 /opt/mcp/mcp_server.py
#           ↑ stdin/stdout become the MCP channel
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "sandbox": {
      "command": "docker",
      "args": ["exec", "-i", "sandbox-mcp", "python3", "/opt/mcp/mcp_server.py"]
    }
  }
}
```

Server implementation:

```python
# mcp_server.py — stdio mode (fastmcp handles the framing)
mcp = FastMCP("sandbox-tools")

@mcp.tool()
def execute_command(command: str) -> str:
    ...

if __name__ == "__main__":
    mcp.run()   # reads from stdin, writes to stdout
```

### Tradeoffs

| | |
|---|---|
| ✅ Zero infrastructure | No sockets, no ports, no relay |
| ✅ Works with `--network=none` | Container has zero network access |
| ✅ Idiomatic MCP | Protocol designed for this |
| ✅ Claude Desktop native | Just point at the command |
| ❌ One process per session | Each client spawns a fresh `docker exec` |
| ❌ No persistent state | Container restarts between sessions |
| ❌ No multi-client | Only one agent can talk at a time |

### Verdict

> **Best for:** single agent, Claude Desktop integration, maximum simplicity.
> The path of least resistance. Start here.

---

## 4. Option 2 — TCP on localhost (chosen)

### How it works

The MCP server binds uvicorn to a TCP port inside the container. Docker
publishes it to `127.0.0.1` only, so only processes on the host can reach it.
The `--internal` Docker network flag was intended to block outbound traffic,
but **does not work on macOS/Colima** (port publishing is blocked too).
So we use the default bridge network + `127.0.0.1` binding instead.

```bash
# Create container, publish port to localhost only
docker run -d \
  --name sandbox-mcp \
  -p 127.0.0.1:9100:9100 \
  --read-only --cap-drop=ALL \
  --tmpfs /workspace:rw,uid=1000,gid=1000,size=1g \
  hardened-sandbox-mcp:latest
```

Claude Desktop config:

```json
{
  "mcpServers": {
    "sandbox": {
      "url": "http://localhost:9100/sse"
    }
  }
}
```

Server implementation:

```python
async def _run_tcp(host: str = "0.0.0.0", port: int = 9100) -> None:
    app = mcp.http_app(transport="sse")
    config = uvicorn.Config(app, host=host, port=port, lifespan="on")
    await uvicorn.Server(config).serve()
```

Testing with curl (two terminals needed — SSE is two channels):

```bash
# Terminal 1 — keep open, results appear here
curl http://localhost:9100/sse
# → event: endpoint
# → data: /messages/?session_id=abc123

# Terminal 2 — send commands
SESSION=abc123

# Step 1: always initialize first
curl -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1.0"}}}'

# Step 2: call a tool
curl -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"list_dir","arguments":{"path":"."}}}''
# result arrives on Terminal 1, not here (202 Accepted here)
```

### macOS note: `--internal` network doesn't work

On Linux, the ideal setup is:

```bash
# Linux only — blocks all outbound, host can still reach in
docker network create --internal mcp-jail
docker run --network mcp-jail -p 127.0.0.1:9100:9100 ...
```

On macOS/Colima, `--internal` prevents port publishing entirely (the VM
networking layer doesn't support it). The workaround:

```bash
# macOS — default bridge + 127.0.0.1 binding
# Port is only reachable from localhost, not from outside the machine
docker run -p 127.0.0.1:9100:9100 ...
```

### Tradeoffs

| | |
|---|---|
| ✅ Persistent server | One process serves all clients |
| ✅ Multi-client | Many agents can connect simultaneously |
| ✅ Standard HTTP | Works with any HTTP client, curl, browser |
| ✅ Zero relay code | Just `docker run -p` |
| ✅ Fast | Test suite runs in ~0.1s |
| ⚠️ Container has internet | `--internal` doesn't work on macOS |
| ⚠️ Port exposed on host | Only localhost, but still a port |

### Verdict

> **Best for:** persistent server, multiple clients, easy testing with curl.
> What we chose. Simple and fast.

---

## 5. Option 3 — Unix socket + docker exec relay

### How it works

The MCP server binds to a Unix socket **inside** the container's tmpfs
(`/tmp/mcp.sock`). A host-side relay accepts connections on a host Unix socket
and bridges each one through `docker exec -i` to a container-side relay
that connects to the internal socket.

```
Host Unix socket (/tmp/mcp-sandbox/mcp.sock)
    └─ mcp_host_relay.py      accepts connections, spawns docker exec per connection
         └─ docker exec -i sandbox-mcp python3 mcp_container_relay.py
              └─ mcp_container_relay.py   bridges stdin/stdout ↔ /tmp/mcp.sock
                   └─ uvicorn on /tmp/mcp.sock   (MCP server)
```

Host relay (simplified):

```python
def bridge(client: socket.socket, container: str) -> None:
    proc = subprocess.Popen(
        ["docker", "exec", "-i", "-e", "PYTHONUNBUFFERED=1", container,
         "python3", "-u", "/opt/mcp/mcp_container_relay.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    # copy client → proc.stdin in a thread
    # copy proc.stdout → client in main thread
    ...
```

Container relay (simplified):

```python
def main() -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect("/tmp/mcp.sock")
    # thread: os.read(stdin_fd) → sock.sendall()
    # main:   sock.recv()       → os.write(stdout_fd)
```

### The bugs we hit building this

This approach sounds simple but has several nasty gotchas:

**Bug 1 — `BufferedReader.read(n)` blocks**
```python
# ❌ waits for 65536 bytes or EOF — HTTP requests are only ~80 bytes
data = sys.stdin.buffer.read(65536)

# ✅ returns immediately with whatever bytes are available
data = os.read(sys.stdin.fileno(), 65536)
```

**Bug 2 — `proc.stdout.raw` crashes with `bufsize=0`**
```python
# ❌ with bufsize=0, proc.stdout is FileIO — has no .raw attribute
data = proc.stdout.raw.read(BUF)

# ✅ always works regardless of buffering mode
data = os.read(proc.stdout.fileno(), BUF)
```

**Bug 3 — `--tmpfs /workspace` owned by root**
```bash
# ❌ tmpfs mount replaces the chown'd dir in the image — owned by root
--tmpfs /workspace:rw,size=1g

# ✅ set uid/gid so sandbox user (uid=1000) can write
--tmpfs /workspace:rw,uid=1000,gid=1000,size=1g
```

**Bug 4 — Python stdout buffering inside docker exec**
```bash
# ❌ Python buffers stdout in block mode when not connected to a TTY
docker exec -i container python3 relay.py

# ✅ force unbuffered mode
docker exec -i -e PYTHONUNBUFFERED=1 container python3 -u relay.py
```

### Tradeoffs

| | |
|---|---|
| ✅ Works with `--network=none` | Container truly isolated |
| ✅ No port exposed | Pure Unix socket, no TCP |
| ❌ ~300 lines of relay code | Complex, fragile |
| ❌ One `docker exec` per request | ~200ms overhead per call |
| ❌ Multiple buffering traps | `os.read` vs `BufferedReader.read` |
| ❌ Slow | Test suite took ~6.8s vs 0.1s for Option 2 |
| ❌ macOS virtiofs limitation | Can't bind AF_UNIX to bind-mounted dirs |

### Verdict

> **Avoid unless** you absolutely need `--network=none` AND can't use stdio.
> We built this, debugged it for hours, then replaced it with Option 2.
> The complexity is not worth it.

---

## 6. Option 4 — Named pipes (FIFOs)

### How it works

FIFOs (named pipes) work over virtiofs/bind mounts on macOS, unlike Unix
sockets. Two pipes provide a bidirectional channel:

```bash
mkfifo /tmp/mcp-bridge/req
mkfifo /tmp/mcp-bridge/resp
# Container reads from req, writes to resp
# Host does the inverse
```

### Why we didn't use it

FIFOs are single-reader/single-writer and half-duplex per pipe. To multiplex
multiple concurrent MCP sessions you'd need custom framing, session
multiplexing, and essentially reinvent TCP. Not worth it when stdio exists.

### Verdict

> **Don't bother.** A fun systems curiosity, but Option 1 (stdio) solves the
> same problem with zero code.

---

## 7. Comparison table

| | Option 1 stdio | **Option 2 TCP** ✅ | Option 3 Relay | Option 4 FIFOs |
|---|---|---|---|---|
| **Complexity** | Zero | Low | High | Very high |
| **Extra code** | 0 lines | ~20 lines | ~300 lines | ~200+ lines |
| **network=none** | ✅ | ❌ (macOS) | ✅ | ✅ |
| **Multi-client** | ❌ | ✅ | ✅ | ❌ |
| **Persistent server** | ❌ | ✅ | ✅ | ❌ |
| **Test with curl** | ❌ | ✅ | ❌ | ❌ |
| **Claude Desktop** | ✅ native | ✅ via URL | ✅ via socat | ❌ |
| **Test suite time** | ~0.3s | **~0.1s** | ~6.8s | N/A |
| **macOS virtiofs** | ✅ | ✅ | ⚠️ bugs | ✅ |
| **Bugs to debug** | None | One (--internal) | Four | Many |

---

## 8. What we actually built

We went through all the options the hard way:

```
Option 3 (relay)  ──▶  discovered ~300 lines of complexity + 4 bugs
        │
        ▼
Option 2 (TCP)    ──▶  deleted all relay code, 0.1s test time, done
```

### Final architecture

```
MCP Client (Claude Desktop / curl / test-mcp.py)
        │
        │  HTTP/SSE  http://localhost:9100/sse
        │
        ▼
┌─────────────────────────────────────────────┐
│  Docker container  (sandbox-mcp)            │
│                                             │
│  uvicorn + fastmcp   0.0.0.0:9100          │
│                                             │
│  Tools (jailed to /workspace):              │
│    execute_command  read_file  write_file   │
│    append_file      list_dir   delete_file  │
│                                             │
│  Security:                                  │
│    uid=1000(sandbox)  --cap-drop=ALL        │
│    --read-only rootfs  seccomp profile      │
│    --pids-limit 64  --memory 512m           │
│    --tmpfs /workspace (uid=1000, 1g)        │
└─────────────────────────────────────────────┘
```

### Key files

| File | Purpose |
|---|---|
| `sandbox/mcp_server.py` | MCP server — supports both TCP (`--port`) and UDS (`--uds`) |
| `sandbox/Dockerfile.mcp` | Container image (extends `hardened-sandbox`) |
| `sandbox/run-mcp-macos.sh` | Launch script — `docker run -p 127.0.0.1:9100:9100` |
| `sandbox/test-mcp.py` | Smoke test — stdlib only, ~0.1s, auto-detects TCP or UDS |

### Quick start

```bash
# build + start
bash sandbox/run-mcp-macos.sh

# verify everything works
python3 sandbox/test-mcp.py

# peek at the SSE stream
curl http://localhost:9100/sse
```

---

## 9. Testing with curl

MCP's SSE transport needs **two terminal windows open at the same time** —
one holds the SSE stream open, the other sends commands. This is the most
important thing to understand when testing manually.

```
Terminal 1 (SSE stream)          Terminal 2 (send commands)
────────────────────────         ──────────────────────────
curl http://localhost:9100/sse   curl -X POST /messages/...
← stays open forever             → fires request, gets 202
← results appear HERE            ← result does NOT appear here
```

### Step 1 — open the SSE stream (Terminal 1)

Run this and **leave it open**. Every result from every command will appear
here.

```bash
curl http://localhost:9100/sse
```

You'll see:

```
event: endpoint
data: /messages/?session_id=9505c8df659c4335b978a5ecb704040d

: ping - 2026-03-22 02:06:25.871864+00:00
: ping - 2026-03-22 02:06:55.871864+00:00
```

> **Why doesn't it exit?** Because SSE is a long-lived connection that stays
> open intentionally. The server sends a `ping` keepalive every 30 seconds so
> the line doesn't drop. `Ctrl+C` to close it.

### Step 2 — grab the session ID (Terminal 2)

Copy the `session_id` from Terminal 1 and export it:

```bash
export SESSION=9505c8df659c4335b978a5ecb704040d
```

Verify it's set:

```bash
echo $SESSION   # must not be blank!
```

### Step 3 — initialize (mandatory handshake)

You **must** call `initialize` before any tool calls. Skip it and you get
`-32602 Invalid request parameters`.

```bash
curl -s -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "curl-test", "version": "1.0"}
    }
  }'
```

This returns `Accepted` (202). The actual result appears in **Terminal 1**:

```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{...}}}
```

### Step 4 — call tools

Now you can call any tool. Results always appear in Terminal 1.

**List files in /workspace:**

```bash
curl -s -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_dir","arguments":{"path":"."}}}''
```

**Write a file:**

```bash
curl -s -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"write_file","arguments":{"path":"hello.txt","content":"hello from MCP!\n"}}}'
```

**Read a file:**

```bash
curl -s -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"hello.txt"}}}'
```

**Run a shell command:**

```bash
curl -s -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"execute_command","arguments":{"command":"echo hello && whoami && id"}}}'
```

**List all available tools:**

```bash
curl -s -X POST "http://localhost:9100/messages/?session_id=$SESSION" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/list","params":{}}'
```

### Common gotchas

**❌ Shell eating your JSON quotes**

This is the #1 mistake. The shell interprets `{` and `"` unless you wrap
the JSON in single quotes:

```bash
# ❌ broken — shell eats the quotes, server gets invalid JSON
-d {"jsonrpc":"2.0"...}

# ✅ correct — single quotes protect the JSON from the shell
-d '{"jsonrpc":"2.0"...}'
```

Also quote the URL and headers:

```bash
# ❌ broken — ? and & confuse the shell
-X POST http://localhost:9100/messages/?session_id=$SESSION
-H Content-Type: application/json

# ✅ correct
-X POST "http://localhost:9100/messages/?session_id=$SESSION"
-H "Content-Type: application/json"
```

**❌ Using a stale session ID**

Every time you run `curl http://localhost:9100/sse` you get a **new**
`session_id`. If you close Terminal 1 and reopen it, you must re-export
`SESSION` with the new ID.

**❌ Forgetting to initialize**

Every new SSE session must start with `initialize` (Step 3). Jump straight
to `tools/call` and you get:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid request parameters"}}
```

**❌ Looking for the result in the wrong place**

The POST returns `202 Accepted` — that's correct. The actual JSON result
always comes back on the **SSE stream in Terminal 1**, never in the curl
response of the POST.

### Automated test (no two terminals needed)

For a proper automated test that handles both channels correctly:

```bash
python3 sandbox/test-mcp.py --port 9100
```

Runs all 6 test stages in ~0.1s. No curl juggling required. 🐾

---

## 10. Lessons learned

**1. Start with the simplest option.** We built Option 3 first because it
seemed elegant. We should have started with Option 1 or 2.

**2. `os.read(fd, n)` vs `file.read(n)` are not the same.**
`BufferedReader.read(n)` waits for `n` bytes or EOF. `os.read(fd, n)` returns
immediately with whatever is available. When proxying HTTP over pipes, always
use `os.read`.

**3. `--internal` Docker networks don't support port publishing on macOS.**
This is a macOS VM networking limitation (Colima/Docker Desktop). On Linux it
works fine. Use `127.0.0.1` port binding as the macOS equivalent.

**4. `--tmpfs` resets directory ownership.** When you mount
`--tmpfs /workspace`, the tmpfs root is owned by `uid=0` regardless of what
the Dockerfile's `chown` did. Always add `uid=1000,gid=1000` to the mount
options if a non-root user needs to write there.

**5. MCP requires `initialize` before any tool calls.** Jump straight to
`tools/call` and you get `-32602 Invalid request parameters`. The handshake is
mandatory.

**6. SSE is not HTTP/2.** SSE is a long-lived HTTP/1.1 response that never
closes. HTTP/2 Server Push is a different (now-dead) feature. SSE is alive,
simple, and exactly what MCP needs.
