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
# Copy files to the VM
scp -r hardened-container/ <YOUR-VM-IP>:~/hardened-container/

# SSH in
ssh <YOUR-VM-IP>
cd ~/hardened-container
chmod +x *.sh

# Run with NO network (maximum isolation)
./run-hardened.sh

# Run WITH network (for pip install, etc.)
./run-hardened-with-network.sh

# Run a specific command
./run-hardened.sh python3 -c "print('hello from jail')"
```

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

- **Need network?** Use `run-hardened-with-network.sh`
- **Need to mount files in?** Add `-v /host/path:/workspace/data:ro` (read-only!)
- **Need more memory?** Edit `--memory=2g` in the script
- **Need specific tools?** Edit the Dockerfile and rebuild
