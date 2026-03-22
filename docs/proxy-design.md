# gsutil Proxy Design — Implementation Options

> The sandbox container runs as an unprivileged, network-isolated process.
> But the agent needs to read/write Google Cloud Storage buckets.
> This document captures every approach considered for bridging gsutil
> commands from inside the container to the real credentials on the host.

---

## Table of Contents

1. [The problem](#1-the-problem)
2. [Option 1 — Credentials inside the container](#2-option-1--credentials-inside-the-container)
3. [Option 2 — HTTP proxy on localhost](#3-option-2--http-proxy-on-localhost)
4. [Option 3 — Socket file mount (first attempt)](#4-option-3--socket-file-mount-first-attempt)
5. [Option 4 — Directory mount (chosen)](#5-option-4--directory-mount-chosen)
6. [Comparison table](#6-comparison-table)
7. [What we actually built](#7-what-we-actually-built)
8. [How to operate the proxy](#8-how-to-operate-the-proxy)
9. [Lessons learned](#9-lessons-learned)

---

## 1. The problem

The sandbox container is deliberately hostile to anything that could leak
data or credentials:

```
--cap-drop=ALL          no Linux capabilities
--read-only             rootfs is immutable
--network=none          (or minimal) no outbound internet
--user 1000:1000        non-root
--security-opt seccomp  strict syscall filter
```

But the agent needs gsutil. The real `gsutil` binary requires:

- Google Cloud credentials (`~/.config/gcloud/` or a service account key)
- Outbound HTTPS to `storage.googleapis.com`
- A writable home directory for its own cache

None of those are available inside the hardened container. So we need a
bridge: the container sends a command description to the host, the host runs
the real gsutil, and the result comes back.

```
┌────────────────────────────────┐          ┌──────────────────────────┐
│  Container (sandbox-mcp)       │          │  Host                    │
│                                │          │                          │
│  gsutil ls gs://my-bucket      │          │  real gsutil             │
│      │                         │          │  + gcloud credentials    │
│      ▼                         │          │  + outbound HTTPS        │
│  gsutil-wrapper.sh             │◀────────▶│                          │
│  (thin client)                 │  bridge  │  gsutil-proxy.py         │
│                                │          │  (policy enforcer)       │
└────────────────────────────────┘          └──────────────────────────┘
```

The proxy also enforces a **policy** (`gsutil-policy.json`) — only
allowed commands on allowed buckets go through. Everything else is rejected
before gsutil is ever invoked.

---

## 2. Option 1 — Credentials inside the container

### How it works

Mount gcloud credentials directly into the container and run the real gsutil
binary inside.

```bash
docker run \
  -v ~/.config/gcloud:/home/sandbox/.config/gcloud:ro \
  --network bridge \
  hardened-sandbox:latest
```

### Why we didn't use it

| | |
|---|---|
| ❌ Credentials in the sandbox | The whole point of the sandbox is to contain untrusted code. Putting credentials inside defeats that. |
| ❌ Network access required | gsutil needs outbound HTTPS. That conflicts with `--network=none`. |
| ❌ No policy enforcement | Any gsutil command the agent constructs runs as-is. |
| ❌ Writable home needed | gsutil writes cache files. Hard to allow without relaxing `--read-only`. |

### Verdict

> **Never do this.** Handing credentials to untrusted code is exactly what
> the sandbox exists to prevent.

---

## 3. Option 2 — HTTP proxy on localhost

### How it works

Run an HTTP server on the host. The container calls it via TCP (like the
MCP server, but in reverse — container is the client, host is the server).

```
Container
  gsutil ls gs://bucket
      │
      ▼
  curl http://host-gateway:9200/gsutil \
    -d '{"args": ["ls", "gs://bucket"]}'
      │
      ▼ (host)
  gsutil-http-proxy.py
  → runs real gsutil
  → returns {stdout, stderr, exit_code}
```

On macOS with Colima, the host is reachable from the container via
`host.docker.internal`.

### Tradeoffs

| | |
|---|---|
| ✅ No socket file juggling | Standard HTTP, easy to test with curl |
| ✅ Proxy can restart freely | TCP reconnects transparently |
| ❌ Container needs network | Contradicts `--network=none` isolation goal |
| ❌ Port exposed on container | Another attack surface |
| ❌ `host.docker.internal` is macOS-only | Breaks on Linux without extra config |
| ❌ More moving parts | HTTP server, content-type handling, etc. |

### Verdict

> **Skip it.** Unix sockets give the same result with zero network exposure
> and no platform-specific hostname hacks.

---

## 4. Option 3 — Socket file mount (first attempt)

### How it works

Run the proxy on the host, listening on a Unix socket file. Mount the
socket file directly into the container. The wrapper inside the container
connects to the socket, sends a JSON command, reads the JSON response.

```bash
# Host: start proxy
python3 gsutil-proxy.py  # listens on /tmp/gsutil-proxy.sock

# Container: mount the socket file
docker run \
  -v /tmp/gsutil-proxy.sock:/tmp/gsutil-proxy.sock \
  hardened-sandbox:latest
```

Protocol is minimal JSON over the socket:

```
Request:   {"args": ["ls", "gs://my-bucket"]}
Response:  {"stdout": "gs://my-bucket/file.txt\n", "stderr": "", "exit_code": 0}
```

### The fatal problem: ordering

Docker resolves volume mounts **at container start time**. If the socket
file doesn't exist yet, Docker errors out or mounts nothing:

```bash
# ❌ proxy not running yet → mount fails or container can't reach socket
docker run -v /tmp/gsutil-proxy.sock:/tmp/gsutil-proxy.sock ...

# ❌ proxy crashes and is restarted → new socket file, but container
#    mount still points at the old (now-deleted) inode
```

This creates a brittle startup ordering requirement: **proxy must be running
before the container starts**, and **any proxy restart requires a container
restart**.

### Tradeoffs

| | |
|---|---|
| ✅ No network exposure | Pure Unix socket |
| ✅ Works with `--network=none` | No TCP needed |
| ✅ Simple protocol | JSON over socket |
| ❌ Proxy must start before container | Tight ordering dependency |
| ❌ Proxy restart = container restart | Operational pain |
| ❌ Socket file can disappear | `docker run -v` binds the inode at start |

### Verdict

> **Works, but fragile.** The ordering dependency makes operations annoying
> and error-prone. This was our first implementation. We replaced it.

---

## 5. Option 4 — Directory mount (chosen)

### How it works

Instead of mounting the **socket file**, mount the **directory that contains
the socket**. The directory always exists on the host (created with
`mkdir -p`). The socket appears and disappears inside it as the proxy
starts and stops. The container sees the directory as a stable mount —
it doesn't care whether the socket exists at startup.

```bash
# Host: ensure directory exists (idempotent)
mkdir -p /tmp/gsutil-proxy

# Container: mount the DIRECTORY, not the socket file
docker run \
  -v /tmp/gsutil-proxy:/tmp/gsutil-proxy \
  hardened-sandbox:latest

# Proxy can now start at any time — container sees the socket immediately
python3 gsutil-proxy.py  # writes /tmp/gsutil-proxy/gsutil-proxy.sock
```

The wrapper inside the container looks for the socket at the well-known path:

```bash
SOCKET_PATH="/tmp/gsutil-proxy/gsutil-proxy.sock"
```

Because it's a directory mount, the kernel inode tracking works correctly:
the container's view of `/tmp/gsutil-proxy/` is a live window into the
host directory. A new socket file created after container start is visible
immediately. A crashed-and-restarted proxy just recreates the socket in
place — no container restart needed.

### Lifecycle

```
run.sh
  │
  ├─ mkdir -p /tmp/gsutil-proxy          (sandbox.sh, always)
  ├─ docker run -v /tmp/gsutil-proxy:…   (sandbox.sh)
  │     container is up, no socket yet — gsutil calls will fail with
  │     a clear error message, not a container crash
  │
  └─ gsutil-proxy-ctl.sh start           (run.sh, after docker)
        writes /tmp/gsutil-proxy/gsutil-proxy.sock
        container sees it instantly ✅

Later, if proxy crashes:
  gsutil-proxy-ctl.sh start             (any time, no docker restart)
```

### Tradeoffs

| | |
|---|---|
| ✅ No ordering constraint | Container and proxy start independently |
| ✅ Proxy restart is free | `gsutil-proxy-ctl.sh stop && start` — done |
| ✅ No network exposure | Pure Unix socket, `--network=none` compatible |
| ✅ Graceful degradation | gsutil fails with a clear message, not a crash |
| ✅ Managed lifecycle | `gsutil-proxy-ctl.sh start/stop/status` |
| ⚠️ Directory always mounted | Even if proxy is never started |

### Verdict

> **This is what we use.** The directory mount sidesteps the entire ordering
> problem with zero extra code. Obvious in hindsight.

---

## 6. Comparison table

| | Option 1 Creds in container | Option 2 HTTP proxy | Option 3 Socket file ✗ | **Option 4 Dir mount ✅** |
|---|---|---|---|---|
| **Security** | ❌ creds exposed | ⚠️ needs network | ✅ | ✅ |
| **network=none** | ❌ | ❌ | ✅ | ✅ |
| **Policy enforcement** | ❌ | ✅ | ✅ | ✅ |
| **Proxy before container?** | N/A | No | **Yes ❌** | No ✅ |
| **Proxy restart = container restart?** | N/A | No | **Yes ❌** | No ✅ |
| **Cross-platform** | ✅ | ⚠️ macOS only | ✅ | ✅ |
| **Extra code** | 0 | ~100 lines | ~200 lines | ~200 lines |
| **Complexity** | Zero | Medium | Medium | Medium |

---

## 7. What we actually built

```
Option 3 (socket file)  ──▶  brittle ordering, proxy restart pain
        │
        ▼
Option 4 (dir mount)    ──▶  ordering gone, restart free, done
```

### Architecture

```
┌──────────────────────────────────────────────┐
│  Container (sandbox-mcp)                     │
│                                              │
│  /usr/local/bin/gsutil  ← gsutil-wrapper.sh  │
│      │                                       │
│      │  JSON over Unix socket                │
│      ▼                                       │
│  /tmp/gsutil-proxy/gsutil-proxy.sock  ←──────┼──┐
└──────────────────────────────────────────────┘  │ dir mount
                                                  │ -v /tmp/gsutil-proxy
┌──────────────────────────────────────────────┐  │    :/tmp/gsutil-proxy
│  Host                                        │  │
│                                              │  │
│  /tmp/gsutil-proxy/         ←────────────────┼──┘
│      gsutil-proxy.sock  (created by proxy)   │
│                                              │
│  gsutil-proxy.py                             │
│      validates command against policy        │
│      runs real gsutil                        │
│      returns {stdout, stderr, exit_code}     │
│                                              │
│  gsutil-proxy-ctl.sh  start | stop | status  │
└──────────────────────────────────────────────┘
```

### Key files

| File | Purpose |
|---|---|
| `sandbox/gsutil-proxy.py` | Host-side daemon — policy enforcer + real gsutil runner |
| `sandbox/gsutil-proxy-ctl.sh` | start / stop / status lifecycle management |
| `sandbox/gsutil-wrapper.sh` | In-container thin client — sends JSON, prints result |
| `sandbox/gsutil-policy.json` | Allowlist of commands and buckets |
| `sandbox/sandbox.sh` | Mounts `/tmp/gsutil-proxy` dir into container |

---

## 8. How to operate the proxy

```bash
# Start (idempotent — safe to run if already running)
./sandbox/gsutil-proxy-ctl.sh start

# Stop
./sandbox/gsutil-proxy-ctl.sh stop

# Check status
./sandbox/gsutil-proxy-ctl.sh status

# Restart (no container restart needed)
./sandbox/gsutil-proxy-ctl.sh stop
./sandbox/gsutil-proxy-ctl.sh start

# Logs
tail -f /tmp/gsutil-proxy.log
```

`run.sh` starts the proxy automatically on launch. If it crashes later,
restart it independently — the sandbox container keeps running.

### Testing from inside the container

```bash
# Exec into the running MCP container
./sandbox/sandbox.sh --shell

# Basic list
gsutil ls gs://your-bucket

# Copy a file out
gsutil cp gs://your-bucket/file.txt /workspace/file.txt
```

### Testing the socket directly from the host

```python
import json, socket

req = json.dumps({"args": ["ls", "gs://your-bucket"]})
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/tmp/gsutil-proxy/gsutil-proxy.sock")
sock.sendall(req.encode())
sock.shutdown(socket.SHUT_WR)
print(json.loads(b"".join(iter(lambda: sock.recv(65536), b"")).decode()))
```

---

## 9. Lessons learned

**1. Mount directories, not files.** Docker resolves file mounts to inodes
at container start. If the file is recreated (proxy restart), the container
still holds the old inode. Mounting the parent directory gives the container
a live view — new files appear instantly.

**2. Ordering dependencies are a smell.** If component A must start before
component B, that's a design coupling waiting to become an ops incident.
The directory mount eliminates the coupling entirely.

**3. Graceful degradation beats hard failures.** With the directory always
mounted, a missing socket produces a clean error message from the wrapper:
`ERROR: gsutil proxy socket not found`. The container doesn't crash, the MCP
server keeps running, and the user knows exactly what to do.

**4. Policy enforcement belongs on the host.** The proxy validates every
command against `gsutil-policy.json` before touching real gsutil. Even if
an agent somehow constructs a malicious command inside the sandbox, the proxy
is the last line of defense — and it runs outside the sandbox.
