# 🔒 Hardened Sandbox Container

A maximally locked-down Docker container that prevents **any** code running inside
from escaping, damaging, or even seeing the host VM.

## Security Layers

| Layer | What it does |
|---|---|
| **Non-root user** | Runs as UID 1000, no root access |
| **`--cap-drop=ALL`** | Drops every Linux capability |
| **`--no-new-privileges`** | Blocks setuid/sudo escalation |
| **Read-only rootfs** | Container filesystem is immutable |
| **Custom seccomp** | Whitelist-only syscall filter |
| **`--network=none`** | Zero network access (default) |
| **PID isolation** | Max 256 processes |
| **Memory cap** | Hard 2GB limit, no swap abuse |
| **CPU cap** | Max 2 cores |
| **IPC isolation** | `--ipc=private` |
| **No host mounts** | Zero access to host filesystem |
| **tmpfs noexec** | /tmp and /run can't run binaries |
| **No SUID binaries** | All setuid bits stripped |
| **No pkg manager** | apt/dpkg removed from image |

## Quick Start

```bash
chmod +x sandbox.sh

# Interactive shell, NO network (maximum isolation)
./sandbox.sh

# Run a one-off command, no network
./sandbox.sh -- python3 -c "print('hello from jail')"

# Interactive shell WITH network
./sandbox.sh --network

# Run a command WITH network
./sandbox.sh --network -- pip install requests

# MCP server on TCP port 9100 (default, localhost-only)
./sandbox.sh --mcp

# MCP server on a custom port
./sandbox.sh --mcp --port 8811

# MCP server in the background
./sandbox.sh --mcp --detach

# Stop the background MCP container
./sandbox.sh --mcp --stop

# Drop into a debug shell
./sandbox.sh --mcp --shell

# MCP over streamable-HTTP instead of SSE
./sandbox.sh --mcp --transport streamable-http
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

## What can the container NOT do?

- ❌ Access host filesystem
- ❌ See host processes
- ❌ Access host network (default)
- ❌ Escalate to root
- ❌ Load kernel modules
- ❌ Mount filesystems
- ❌ Use dangerous syscalls (ptrace, mount, etc.)
- ❌ Fork-bomb (PID limit)
- ❌ OOM-kill the host (memory cap)
- ❌ Starve host CPU (CPU cap)
- ❌ Write to container OS files (read-only)
- ❌ Execute binaries from /tmp (noexec)

## Customization

- **Need network?** Use `./sandbox.sh --network`
- **Need to mount files in?** Add `-v /host/path:/workspace/data:ro` (read-only!) to `sandbox.sh`
- **Need more memory?** Edit `--memory=2g` in `sandbox.sh`
- **Need specific tools?** Edit `Dockerfile` and rebuild (image rebuilds automatically on next run)
