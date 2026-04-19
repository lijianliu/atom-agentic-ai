# atom-command-proxy & atom-command-broker — Architecture & Design

> **Two-component architecture for policy-driven command mediation between
> a hardened container and host-side executables.**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Component Responsibilities](#3-component-responsibilities)
4. [Protocol Specification](#4-protocol-specification)
5. [Execution Modes](#5-execution-modes)
6. [Tool Discovery](#6-tool-discovery)
7. [Policy Configuration](#7-policy-configuration)
8. [Tool Adapters](#8-tool-adapters)
9. [Executable Registry](#9-executable-registry)
10. [Security Model](#10-security-model)
11. [Socket & Deployment](#11-socket--deployment)
12. [Adding a New Tool Family](#12-adding-a-new-tool-family)
13. [Operations Guide](#14-operations-guide)
14. [Examples](#15-examples)
15. [File Reference](#16-file-reference)
16. [Migration from gsutil-proxy](#17-migration-from-gsutil-proxy)

---

## 1. Overview

The system provides secure, policy-controlled command execution from inside
a hardened Docker container to host-side tools (gsutil, gcloud, Kafka CLI, etc.).

**Key principles:**

- The container never directly executes privileged or credentials-bearing host tools
- Policy enforcement is centralized on the host side
- The container-side component is a thin, generic relay
- New tools can be added without modifying the container image
- Both short-lived (buffered) and long-running (streaming) commands are supported

### Two Components

| Component | Location | Role |
|---|---|---|
| **atom-command-proxy** | Inside the container | Thin command relay — forwards structured requests over Unix socket |
| **atom-command-broker** | Host VM (outside container) | Central policy enforcement and execution engine |

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────┐
│  Container (sandbox-mcp)                             │
│                                                      │
│  atom-command-proxy gsutil ls gs://my-bucket         │
│      │                                               │
│      │  Structured JSON request                      │
│      │  over Unix domain socket                      │
│      ▼                                               │
│  /tmp/atom-command-proxy/command-broker.sock  ◀──────┼──┐
│                                                      │  │
└──────────────────────────────────────────────────────┘  │ dir mount
                                                          │
┌──────────────────────────────────────────────────────┐  │
│  Host VM                                             │  │
│                                                      │  │
│  /tmp/atom-command-proxy/  ◀─────────────────────────┼──┘
│      command-broker.sock  (created by broker)        │
│                                                      │
│  atom-command-broker                                 │
│      ┌─────────────────────────────────────────┐     │
│      │  Protocol handler                       │     │
│      │  Policy engine     ← broker-policy.json │     │
│      │  Executable registry (auto-discovered)  │     │
│      │  Tool adapters:                         │     │
│      │    ├─ gsutil_adapter                    │     │
│      │    ├─ gcloud_adapter                    │     │
│      │    └─ kafka_adapter                     │     │
│      │  Audit logger                           │     │
│      └─────────────────────────────────────────┘     │
│                                                      │
│  Real executables:                                   │
│    /usr/bin/gsutil     + gcloud credentials           │
│    /usr/bin/gcloud     + outbound HTTPS               │
│    /opt/kafka/bin/*    + Kafka cluster access          │
└──────────────────────────────────────────────────────┘
```

The socket **directory** is mounted (not the socket file), so broker restarts
do not require container restarts. See [proxy-design.md](proxy-design.md) §5
for the rationale behind directory mounting.

---

## 3. Component Responsibilities

### atom-command-proxy (container-side)

| Does | Does NOT |
|---|---|
| Accept CLI invocations inside the container | Execute real gsutil/gcloud/kafka binaries |
| Convert invocations to structured JSON requests | Enforce meaningful policy |
| Send requests over Unix domain socket | Hold or use credentials |
| Receive buffered or streamed responses | Access external networks |
| Present stdout/stderr/exit_code to the caller | Contain tool-specific business logic |
| Support `discover` and `health` operations | Decide whether a command is allowed |

### atom-command-broker (host-side)

| Does | Does NOT |
|---|---|
| Listen on Unix domain socket | Run inside the container |
| Validate protocol version and request structure | Execute arbitrary caller-provided paths |
| Enforce centralized policy | Trust the container-side component |
| Route requests to correct host-side executables | Expose credentials to the container |
| Capture stdout/stderr/exit_code | Allow unrestricted shell execution |
| Support buffered and streaming execution | |
| Support tool discovery | |
| Maintain structured audit logs | |
| Distinguish error categories clearly | |

---

## 4. Protocol Specification

### Protocol Version

Current version: **1**

The version field is required in every request. The broker rejects requests
with unsupported versions.

### Request Format

```json
{
  "version": 1,
  "request_id": "unique-uuid",
  "operation": "execute | discover | health",
  "tool": "gsutil",
  "argv": ["ls", "gs://my-bucket"],
  "requested_mode": "auto | buffered | streaming",
  "timeout_sec": 30,
  "stream": false,
  "cwd": null
}
```

| Field | Required | Type | Description |
|---|---|---|---|
| `version` | ✅ | int | Protocol version (must be 1) |
| `request_id` | ✅ | string | Unique request identifier |
| `operation` | ✅ | string | `execute`, `discover`, or `health` |
| `tool` | execute only | string | Logical tool name |
| `argv` | execute only | string[] | Command arguments (after tool name) |
| `requested_mode` | ❌ | string | `auto`, `buffered`, or `streaming` |
| `timeout_sec` | ❌ | int | Requested timeout in seconds |
| `stream` | ❌ | bool | Explicit streaming request |
| `cwd` | ❌ | string | Working directory |

### Buffered Response Format

```json
{
  "version": 1,
  "request_id": "unique-uuid",
  "ok": true,
  "operation": "execute",
  "effective_mode": "buffered",
  "mode_reason": "gsutil_always_buffered",
  "exit_code": 0,
  "stdout": "gs://my-bucket/file.txt\n",
  "stderr": ""
}
```

### Error Response Format

```json
{
  "version": 1,
  "request_id": "unique-uuid",
  "ok": false,
  "operation": "execute",
  "error_category": "policy_denied",
  "error": "POLICY DENIED: Bucket 'gs://secret' not in allowed list",
  "stdout": "",
  "stderr": "POLICY DENIED: ...",
  "exit_code": 403
}
```

### Error Categories

| Category | Exit Code | Meaning |
|---|---|---|
| `policy_denied` | 403 | Command blocked by policy |
| `validation_error` | 400 | Malformed request or invalid arguments |
| `timeout` | 124 | Command exceeded timeout |
| `execution_failure` | 1 | Command failed during execution |
| `internal_error` | 500 | Broker internal error |
| `rate_limited` | 429 | Too many requests |

### Streaming Frame Format

For streaming mode, the broker sends length-prefixed frames:

```
[4 bytes: uint32 big-endian length][JSON payload]
```

Frame types:

| Frame Type | Fields | Description |
|---|---|---|
| `start` | `request_id`, `effective_mode`, `mode_reason` | Execution started |
| `stdout` | `data` | Incremental stdout data |
| `stderr` | `data` | Incremental stderr data |
| `exit` | `request_id`, `exit_code` | Process completed |
| `error` | `request_id`, `category`, `message`, `exit_code` | Error occurred |

---

## 5. Execution Modes

### Buffered Mode

The broker executes the command, waits for completion, and returns a single
JSON response containing stdout, stderr, and exit code.

**Appropriate for:**
- `gsutil ls`, `gsutil stat`, `gsutil cat`
- `gcloud ... list`, `gcloud ... describe`
- `kafka-broker-api-versions`, `kafka-get-offsets`
- Any short-lived command

### Streaming Mode

The broker starts the command and streams stdout/stderr back incrementally
as length-prefixed frames. A final `exit` frame carries the exit code.

**Appropriate for:**
- `kafka-console-consumer` (without `--max-messages`)
- Long-running Kafka commands
- Any command that produces output over time

### Mode Negotiation

The caller requests a mode (`auto`, `buffered`, `streaming`), but the
**broker is the final authority** on the effective mode.

The broker decides based on:
1. Policy configuration
2. Tool adapter logic
3. Command arguments (e.g., `--max-messages` makes a consumer bounded)
4. Caller-requested mode

The effective mode and reason are returned in the response:

```json
{
  "effective_mode": "streaming",
  "mode_reason": "consumer_command_without_bounded_message_limit"
}
```

---

## 6. Tool Discovery

The `discover` operation lets the caller (or AI) learn what proxied tools
are available without hardcoding.

### Request

```json
{
  "version": 1,
  "request_id": "unique-uuid",
  "operation": "discover"
}
```

### Response

```json
{
  "version": 1,
  "request_id": "unique-uuid",
  "ok": true,
  "operation": "discover",
  "tools": [
    {
      "name": "gsutil",
      "description": "Google Cloud Storage CLI (gsutil) via broker",
      "supported_modes": ["buffered"],
      "default_mode": "buffered",
      "examples": ["gsutil ls gs://bucket-name", "..."]
    },
    {
      "name": "gcloud",
      "description": "Google Cloud CLI (gcloud) via broker",
      "supported_modes": ["buffered"],
      "default_mode": "buffered"
    },
    {
      "name": "kafka-console-consumer",
      "description": "Consume messages from a Kafka topic",
      "supported_modes": ["buffered", "streaming"],
      "default_mode": "streaming"
    }
  ]
}
```

Only tools that are:
1. Registered in the executable registry (found on the host)
2. Enabled in policy

appear in the discovery response.

### CLI Usage

```bash
# Human-readable (when stdout is a terminal)
atom-command-proxy discover

# Machine-readable JSON (when piped)
atom-command-proxy discover | jq '.tools[].name'
```

---

## 7. Policy Configuration

### Policy File Location

The broker searches for policy in this order:

1. `--policy` CLI flag
2. `ATOM_BROKER_POLICY` environment variable
3. `~/.config/atom-agentic-ai/broker-policy.json`
4. `sandbox/atom-command-broker/default-policy.json`
5. Built-in defaults (in code)

### Policy Structure

```json
{
  "global": {
    "rate_limit_per_minute": 120,
    "max_timeout_sec": 300,
    "max_output_bytes": 10485760
  },
  "tools": {
    "gsutil": {
      "enabled": true,
      "allowed_subcommands": ["ls", "cat", "cp", "stat", "du", "hash", "version"],
      "allowed_buckets": ["gs://my-bucket", "gs://another-bucket"],
      "blocked_flags": ["-d", "--delete", "-D"],
      "cp_download_only": true,
      "max_timeout_sec": 120,
      "max_output_bytes": 10485760
    },
    "gcloud": {
      "enabled": true,
      "allowed_command_prefixes": [
        "storage buckets list",
        "config list",
        "info",
        "version"
      ],
      "blocked_flags": ["--impersonate-service-account"],
      "force_flags": ["--quiet"],
      "max_timeout_sec": 120
    },
    "kafka-console-consumer": {
      "enabled": true,
      "allowed_bootstrap_servers": ["kafka-1:9092", "kafka-2:9092"],
      "allowed_topics": ["my-topic"],
      "allowed_groups": ["my-group"],
      "require_bounded": false,
      "max_messages_limit": 1000,
      "max_timeout_sec": 120,
      "max_output_bytes": 10485760
    }
  }
}
```

### Policy Fields by Tool

#### gsutil

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | Whether gsutil is available |
| `allowed_subcommands` | string[] | Permitted subcommands (ls, cat, cp, etc.) |
| `allowed_buckets` | string[] | Permitted bucket prefixes (`gs://name`). Empty = allow all. |
| `blocked_flags` | string[] | Forbidden flags |
| `cp_download_only` | bool | If true, only gs://→local copies allowed |
| `max_timeout_sec` | int | Maximum execution time |
| `max_output_bytes` | int | Maximum output size |

#### gcloud

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | Whether gcloud is available |
| `allowed_command_prefixes` | string[] | Permitted command prefixes. Empty = allow all. |
| `blocked_flags` | string[] | Forbidden flags |
| `force_flags` | string[] | Flags injected automatically |
| `max_timeout_sec` | int | Maximum execution time |

#### Kafka tools

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | Whether this Kafka tool is available |
| `allowed_bootstrap_servers` | string[] | Permitted servers. Empty = allow all. |
| `allowed_topics` | string[] | Permitted topics. Empty = allow all. |
| `allowed_groups` | string[] | Permitted consumer groups. Empty = allow all. |
| `require_bounded` | bool | Require `--max-messages` for consumers |
| `max_messages_limit` | int | Maximum `--max-messages` value |
| `max_timeout_sec` | int | Maximum execution time |
| `max_output_bytes` | int | Maximum output size |

### Empty Restriction Lists

When a restriction list (e.g., `allowed_buckets`, `allowed_topics`) is
**empty** `[]`, it means **no restriction** (all values allowed). This is
the default for local development. Production deployments should populate
these lists.

---

## 8. Tool Adapters

Each tool family has an adapter responsible for:

| Responsibility | Description |
|---|---|
| Validation | Structural validation of arguments |
| Normalization | Strip redundant prefixes, clean up args |
| Mode selection | Determine effective execution mode |
| Command building | Map logical tool + args to real command |
| Environment | Build safe environment for execution |
| Discovery metadata | Provide tool-specific discovery info |

### Implemented Adapters

| Adapter | Tools | Module |
|---|---|---|
| `GsutilAdapter` | gsutil | `adapters/gsutil_adapter.py` |
| `GcloudAdapter` | gcloud | `adapters/gcloud_adapter.py` |
| `KafkaAdapter` | All kafka-* tools | `adapters/kafka_adapter.py` |

### Adapter Base Class

All adapters inherit from `BaseAdapter` (`adapters/base.py`):

```python
class BaseAdapter(ABC):
    def description(self) -> str: ...
    def supported_modes(self) -> list[str]: ...
    def default_mode(self) -> str: ...
    def discovery_metadata(self) -> dict | None: ...
    def validate(self, argv: list[str]) -> str | None: ...
    def normalize_args(self, argv: list[str]) -> list[str]: ...
    def effective_mode(self, argv, requested_mode) -> tuple[str, str]: ...
    def build_command(self, executable, argv) -> list[str]: ...
    def build_env(self, tool_policy) -> dict: ...
```

---

## 9. Executable Registry

The broker maintains an allowlisted registry mapping logical tool names
to approved host-side executable paths.

**The request carries only logical tool names** (e.g., `gsutil`), never
arbitrary executable paths. The registry resolves them.

### Auto-Discovery

On startup, the registry searches well-known paths and `$PATH` for each
supported tool:

```
gsutil  → /usr/bin/gsutil, /usr/local/bin/gsutil, ...
gcloud  → /usr/bin/gcloud, /usr/local/bin/gcloud, ...
kafka-* → /usr/bin/kafka-*, /opt/kafka/bin/kafka-*.sh, ...
```

Only tools found on the host appear in the registry. Missing tools are
logged and excluded from discovery.

### Manual Override

Additional executables can be registered programmatically or via config
overrides passed to the registry constructor.

---

## 10. Security Model

### Threat Model

The container runs untrusted code (AI-generated). The security boundary
is between the container and the host. The broker is the last line of defense.

### Security Layers

| Layer | Implementation |
|---|---|
| **Socket isolation** | Unix domain socket — no network exposure |
| **Socket permissions** | 0660 on socket file, 0755 on directory |
| **No credential leaking** | Broker builds controlled env; no arbitrary host env |
| **Request validation** | Protocol version, structure, tool name validated |
| **Policy enforcement** | Centralized allowlists for commands, buckets, topics |
| **Executable allowlist** | Only registered executables can be invoked |
| **Timeout limits** | Per-request and per-policy timeout enforcement |
| **Output size limits** | Truncation at configurable max output |
| **Rate limiting** | Sliding window per-minute rate limit |
| **Audit logging** | Every request logged with tool, args, decision, duration |
| **Error separation** | Distinct categories: policy_denied vs validation vs execution |
| **No shell execution** | Commands run as explicit argv arrays, not shell strings |

### What the Container Cannot Do

- ❌ Execute real gsutil/gcloud/kafka binaries
- ❌ Access host credentials
- ❌ Send requests to unapproved tools
- ❌ Use unapproved subcommands/buckets/topics
- ❌ Upload to GCS (if cp_download_only is set)
- ❌ Run arbitrary host commands
- ❌ Exceed timeout or output limits
- ❌ Flood the broker (rate limiting)

---

## 11. Socket & Deployment

### Socket Setup

The broker listens on a Unix domain socket inside a directory that is
mounted into the container:

```
Host:      /tmp/atom-command-proxy/command-broker.sock
Container: /tmp/atom-command-proxy/command-broker.sock  (same path via -v mount)
```

**Directory mount** (not file mount) — the broker can restart and recreate
the socket without requiring a container restart.

### Lifecycle

```
run.sh
  ├─ broker-ctl.sh start          # starts atom-command-broker on host
  │     creates /tmp/atom-command-proxy/command-broker.sock
  │
  └─ sandbox.sh start             # starts container
        mounts -v /tmp/atom-command-proxy:/tmp/atom-command-proxy
        container sees socket immediately ✅

Later, if broker restarts:
  broker-ctl.sh restart            # no container restart needed ✅
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ATOM_BROKER_SOCKET_DIR` | `/tmp/atom-command-proxy` | Socket directory |
| `ATOM_BROKER_SOCKET_NAME` | `command-broker.sock` | Socket file name |
| `ATOM_BROKER_POLICY` | (search order) | Policy file path |
| `ATOM_BROKER_LOG` | `/tmp/atom-command-broker.log` | Log file |
| `ATOM_BROKER_AUDIT_LOG` | `/tmp/atom-command-broker-audit.log` | Audit log |
| `ATOM_BROKER_RATE_LIMIT` | `120` | Max requests/minute |
| `ATOM_PROXY_SOCKET_DIR` | `/tmp/atom-command-proxy` | Proxy socket dir |
| `ATOM_PROXY_SOCKET_NAME` | `command-broker.sock` | Proxy socket name |

---

## 12. Adding a New Tool Family

To add support for a new tool (e.g., `aws`, `kubectl`):

### Step 1: Create an adapter

Create `sandbox/atom-command-broker/adapters/aws_adapter.py`:

```python
from .base import BaseAdapter

class AwsAdapter(BaseAdapter):
    def description(self) -> str:
        return "AWS CLI via broker"

    def supported_modes(self) -> list[str]:
        return ["buffered"]

    def default_mode(self) -> str:
        return "buffered"

    def validate(self, argv: list[str]) -> str | None:
        if not argv:
            return "aws requires at least a subcommand"
        return None

    def effective_mode(self, argv, requested_mode):
        return "buffered", "aws_default_buffered"
```

### Step 2: Register the adapter

In `adapters/__init__.py`, add:

```python
from .aws_adapter import AwsAdapter

def _register_defaults():
    ...
    aws = AwsAdapter()
    _ADAPTER_REGISTRY["aws"] = aws
```

### Step 3: Add to executable registry

In `registry.py`, add search paths:

```python
_SEARCH_PATHS = {
    ...
    "aws": ["/usr/local/bin/aws", "/usr/bin/aws"],
}
```

### Step 4: Add policy

In `default-policy.json` (or your custom policy file):

```json
{
  "tools": {
    "aws": {
      "enabled": true,
      "allowed_command_prefixes": ["s3 ls", "s3 cp", "sts get-caller-identity"],
      "blocked_flags": ["--profile"],
      "max_timeout_sec": 120
    }
  }
}
```

### Step 5: No container changes needed!

The container already has `atom-command-proxy`, which works generically:

```bash
atom-command-proxy aws s3 ls
atom-command-proxy discover   # new tool appears automatically
```

```bash
---

`run.sh` now starts `broker-ctl.sh`.

---

## 13. Operations Guide

### Starting the Broker

```bash
# Automatic (via run.sh)
./run.sh

# Manual
./sandbox/atom-command-broker/broker-ctl.sh start

# With custom policy
./sandbox/atom-command-broker/broker-ctl.sh start --policy /path/to/policy.json

# With verbose logging
./sandbox/atom-command-broker/broker-ctl.sh start --verbose
```

### Stopping the Broker

```bash
./sandbox/atom-command-broker/broker-ctl.sh stop
```

### Checking Status

```bash
./sandbox/atom-command-broker/broker-ctl.sh status
```

### Restarting (no container restart needed)

```bash
./sandbox/atom-command-broker/broker-ctl.sh restart
```

### Viewing Logs

```bash
# Application log
tail -f /tmp/atom-command-broker.log

# Audit log (structured JSON, one entry per line)
tail -f /tmp/atom-command-broker-audit.log

# Pretty-print audit entries
tail -f /tmp/atom-command-broker-audit.log | jq .
```

### Testing from Inside the Container

```bash
# Exec into container
./sandbox/sandbox.sh shell

# Test execute
atom-command-proxy gsutil ls gs://my-bucket
atom-command-proxy gcloud info

# Test discovery
atom-command-proxy discover

# Test health
atom-command-proxy health

```

### Testing the Socket Directly from the Host

```python
import json, socket

req = json.dumps({
    "version": 1,
    "request_id": "test-1",
    "operation": "health"
})
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/tmp/atom-command-proxy/command-broker.sock")
sock.sendall(req.encode())
sock.shutdown(socket.SHUT_WR)
print(json.loads(b"".join(iter(lambda: sock.recv(65536), b"")).decode()))
```

---

## 14. Examples

### gsutil

```bash
# List bucket contents
atom-command-proxy gsutil ls gs://my-bucket

# Read file contents
atom-command-proxy gsutil cat gs://my-bucket/data.csv

# Download a file
atom-command-proxy gsutil cp gs://my-bucket/data.csv /workspace/data.csv

# File info
atom-command-proxy gsutil stat gs://my-bucket/data.csv
```

### gcloud

```bash
# List storage buckets
atom-command-proxy gcloud storage buckets list

# Project info
atom-command-proxy gcloud config list

# Version
atom-command-proxy gcloud version
```

### Kafka

```bash
# Check broker API versions
atom-command-proxy kafka-broker-api-versions --bootstrap-server kafka:9092

# Get topic offsets
atom-command-proxy kafka-get-offsets --bootstrap-server kafka:9092 --topic my-topic

# Consume messages (bounded — will use buffered mode)
atom-command-proxy kafka-console-consumer \
  --bootstrap-server kafka:9092 \
  --topic my-topic \
  --max-messages 10

# Consume messages (unbounded — will use streaming mode)
atom-command-proxy --stream kafka-console-consumer \
  --bootstrap-server kafka:9092 \
  --topic my-topic

# Log directories
atom-command-proxy kafka-log-dirs --bootstrap-server kafka:9092

# Metadata quorum
atom-command-proxy kafka-metadata-quorum --bootstrap-server kafka:9092 describe
```

### Discovery

```bash
# Human-readable
atom-command-proxy discover

# Machine-readable
atom-command-proxy discover | jq '.tools[].name'
```

### Policy Denial

```bash
# This will be denied if gs://secret-bucket is not in allowed_buckets
$ atom-command-proxy gsutil ls gs://secret-bucket
POLICY DENIED: Bucket 'gs://secret-bucket' not in allowed list: ['gs://my-bucket']
$ echo $?
403
```

---

## 15. File Reference

### Container-Side

| File | Purpose |
|---|---|
| `sandbox/atom-command-proxy.py` | Generic thin command relay (installed as `/usr/local/bin/atom-command-proxy`) |

### Host-Side (Broker)

| File | Purpose |
|---|---|
| `sandbox/atom-command-broker/broker.py` | Main broker daemon |
| `sandbox/atom-command-broker/broker-ctl.sh` | start/stop/restart/status lifecycle management |
| `sandbox/atom-command-broker/protocol.py` | Protocol definitions (frames, errors, version) |
| `sandbox/atom-command-broker/policy.py` | Policy engine with per-tool evaluation |
| `sandbox/atom-command-broker/registry.py` | Executable registry with auto-discovery |
| `sandbox/atom-command-broker/default-policy.json` | Default policy configuration |
| `sandbox/atom-command-broker/adapters/__init__.py` | Adapter registry |
| `sandbox/atom-command-broker/adapters/base.py` | Base adapter interface |
| `sandbox/atom-command-broker/adapters/gsutil_adapter.py` | gsutil adapter |
| `sandbox/atom-command-broker/adapters/gcloud_adapter.py` | gcloud adapter |
| `sandbox/atom-command-broker/adapters/kafka_adapter.py` | Kafka CLI adapter |

### Infrastructure

| File | Purpose |
|---|---|
| `sandbox/Dockerfile` | Container image (includes atom-command-proxy) |
| `sandbox/sandbox.sh` | Docker sandbox manager (mounts socket directory) |
| `run.sh` | Main launcher (starts broker + sandbox + agent) |



---

## 16. Migration from gsutil-proxy

### What Changed

| Before | After |
|---|---|
| `gsutil-proxy.py` (host) | `atom-command-broker/broker.py` (host) |
| `gsutil-wrapper.sh` (container) | `atom-command-proxy.py` (container) |
| `gsutil-proxy-ctl.sh` | `atom-command-broker/broker-ctl.sh` |
| `gsutil-policy.json` | `broker-policy.json` (multi-tool) |
| `/tmp/gsutil-proxy/gsutil-proxy.sock` | `/tmp/atom-command-proxy/command-broker.sock` |
| gsutil only | gsutil + gcloud + Kafka + extensible |
| `run.sh` calls `gsutil-proxy-ctl.sh` | `run.sh` calls `broker-ctl.sh` |

### Migration Steps

2. Update `broker-policy.json` from your old `gsutil-policy.json`:
   - Move `allowed_commands` → `tools.gsutil.allowed_subcommands`
   - Move `allowed_buckets` → `tools.gsutil.allowed_buckets`
   - Move `blocked_flags` → `tools.gsutil.blocked_flags`
3. `run.sh` already starts the new broker instead of the old proxy
4. Old files remain for reference but are not used by default

### Policy Migration Example

**Old `gsutil-policy.json`:**
```json
{
  "allowed_commands": ["ls", "cat", "cp", "stat"],
  "allowed_buckets": ["gs://my-bucket"],
  "blocked_flags": ["-d", "--delete"]
}
```

**New `broker-policy.json`:**
```json
{
  "global": {
    "rate_limit_per_minute": 120
  },
  "tools": {
    "gsutil": {
      "enabled": true,
      "allowed_subcommands": ["ls", "cat", "cp", "stat"],
      "allowed_buckets": ["gs://my-bucket"],
      "blocked_flags": ["-d", "--delete"],
      "cp_download_only": true
    }
  }
}
```
