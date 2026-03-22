# 🔒 Hardened Sandbox Container

A maximally locked-down Docker container that prevents **any** code running inside
from escaping, damaging, or even seeing the host.

## Security Layers

| Layer | What it does |
|---|---|
| **Non-root user** | Runs as UID 1000, no root access |
| **`--cap-drop=ALL`** | Drops every Linux capability |
| **`--no-new-privileges`** | Blocks setuid/sudo escalation |
| **Read-only rootfs** | Container filesystem is immutable |
| **Custom seccomp** | Whitelist-only syscall filter |
| **Network isolated** | No outbound internet (port 9100 localhost-only) |
| **PID isolation** | Max 256 processes |
| **Memory cap** | Hard 2GB limit |
| **CPU cap** | Max 2 cores |
| **IPC isolation** | `--ipc=private` |
| **tmpfs noexec** | `/tmp` and `/run` can't run binaries |
| **No SUID binaries** | All setuid bits stripped |
| **No pkg manager** | apt/dpkg removed from image |

## Quick Start

```bash
# (re)build the image
./sandbox.sh build

# start MCP server (detached, port 9100)
./sandbox.sh start

# custom port
./sandbox.sh start --port 8811

# stop MCP server
./sandbox.sh stop

# is it running?
./sandbox.sh status

# exec into the running container
./sandbox.sh shell

# run a one-off command in a fresh container
./sandbox.sh run -- python3 -c "print('hello from jail')"

# stop container + remove image
./sandbox.sh clean
```

Works on **Linux and macOS** (Apple Silicon + Intel). Automatically detects
the platform, picks the right Docker platform, and handles Colima on macOS.

## MCP Server Tools

| Tool | Description |
|---|---|
| `execute_command` | Run any shell command; returns exit code + stdout/stderr |
| `read_file` | Read file contents (jailed to `/workspace`) |
| `write_file` | Write/overwrite a file (jailed to `/workspace`) |
| `append_file` | Append text to a file (jailed to `/workspace`) |
| `delete_file` | Delete a file (jailed to `/workspace`) |
| `list_dir` | List directory contents (jailed to `/workspace`) |

All file tools accept absolute paths **or** paths relative to `/workspace`.
Attempts to escape `/workspace` are rejected with an error.

Connect your MCP client to: `http://127.0.0.1:9100/sse` (default port)

Test it with: `python3 sandbox/test-mcp.py` (or `--port PORT` for a custom port)

## gsutil Proxy

The sandbox has no credentials or network access. gsutil commands are forwarded
via Unix socket to a proxy running on the host.

```bash
./gsutil-proxy-ctl.sh start    # start proxy
./gsutil-proxy-ctl.sh stop     # stop proxy
./gsutil-proxy-ctl.sh status   # check status
```

The proxy can be started or restarted at any time without restarting the
container. See [`docs/proxy-design.md`](../docs/proxy-design.md) for design details.

## What can the container NOT do?

- ❌ Access host filesystem
- ❌ See host processes
- ❌ Access the internet
- ❌ Escalate to root
- ❌ Load kernel modules
- ❌ Mount filesystems
- ❌ Use dangerous syscalls (ptrace, mount, etc.)
- ❌ Fork-bomb (PID limit)
- ❌ OOM-kill the host (memory cap)
- ❌ Starve host CPU (CPU cap)
- ❌ Write to container OS files (read-only rootfs)
- ❌ Execute binaries from /tmp (noexec)

## Customization

- **Need more memory?** Edit `--memory=2g` in `sandbox.sh`
- **Need specific tools?** Edit `Dockerfile` and run `./sandbox.sh build`
- **Need a different port?** `./sandbox.sh start --port 8811`
