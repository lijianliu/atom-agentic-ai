# gsutil Proxy Design — Implementation Options (Historical)

> **⚠️ SUPERSEDED**: This document describes the original gsutil-only proxy.
> It has been replaced by **atom-command-proxy / atom-command-broker**, a
> general-purpose, multi-tool command mediation architecture.
>
> See [`docs/command-proxy-broker-design.md`](command-proxy-broker-design.md)
> for the current design.
>
> This document is preserved for historical context — the design tradeoffs
> and lessons learned (especially §5 directory mounts and §9 lessons) still
> apply to the new architecture.

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

The proxy also enforces a **policy** — only allowed commands on allowed
buckets go through. Everything else is rejected before gsutil is ever invoked.

---

## 2. Option 1 — Credentials inside the container

### Verdict

> **Never do this.** Handing credentials to untrusted code is exactly what
> the sandbox exists to prevent.

---

## 3. Option 2 — HTTP proxy on localhost

### Verdict

> **Skip it.** Unix sockets give the same result with zero network exposure
> and no platform-specific hostname hacks.

---

## 4. Option 3 — Socket file mount (first attempt)

### Verdict

> **Works, but fragile.** The ordering dependency makes operations annoying
> and error-prone. This was our first implementation. We replaced it.

---

## 5. Option 4 — Directory mount (chosen)

Instead of mounting the **socket file**, mount the **directory that contains
the socket**. The directory always exists on the host (created with
`mkdir -p`). The socket appears and disappears inside it as the broker
starts and stops. The container sees the directory as a stable mount —
it doesn't care whether the socket exists at startup.

Because it's a directory mount, the kernel inode tracking works correctly:
the container's view of the directory is a live window into the host
directory. A new socket file created after container start is visible
immediately. A crashed-and-restarted broker just recreates the socket in
place — no container restart needed.

> **This approach carries forward into atom-command-broker.** The new
> broker uses the same directory-mount pattern at
> `/tmp/atom-command-proxy/command-broker.sock`.

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

---

## 7. What we actually built

The original gsutil-only proxy has been **replaced** by:

- **atom-command-proxy** (container-side) — generic thin command relay
- **atom-command-broker** (host-side) — multi-tool policy enforcement + execution

See [`docs/command-proxy-broker-design.md`](command-proxy-broker-design.md).

---

## 8. How to operate (current)

```bash
# Start the broker
./sandbox/atom-command-broker/broker-ctl.sh start

# Stop
./sandbox/atom-command-broker/broker-ctl.sh stop

# Status
./sandbox/atom-command-broker/broker-ctl.sh status

# Restart (no container restart needed)
./sandbox/atom-command-broker/broker-ctl.sh restart
```

---

## 9. Lessons learned

These lessons from the original proxy carry forward:

**1. Mount directories, not files.** Docker resolves file mounts to inodes
at container start. If the file is recreated (broker restart), the container
still holds the old inode. Mounting the parent directory gives the container
a live view — new files appear instantly.

**2. Ordering dependencies are a smell.** If component A must start before
component B, that's a design coupling waiting to become an ops incident.
The directory mount eliminates the coupling entirely.

**3. Graceful degradation beats hard failures.** With the directory always
mounted, a missing socket produces a clean error message from the proxy:
`ERROR: atom-command-broker socket not found`. The container doesn't crash,
the MCP server keeps running, and the user knows exactly what to do.

**4. Policy enforcement belongs on the host.** The broker validates every
command against policy before executing. Even if an agent constructs a
malicious command inside the sandbox, the broker is the last line of
defense — and it runs outside the sandbox.
