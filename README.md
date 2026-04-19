# atom-agentic-ai

Agentic AI for Enterprise with built-in Security — tools run inside a
hardened Docker sandbox, never on the host machine.

Built on [pydantic-ai](https://ai.pydantic.dev/) with support for
**Anthropic** (via enterprise LLM Gateway) and **OpenAI** models.

---

## How it works

```
./run.sh
   │
   ├── 1. starts atom-command-broker on host (if not running)
   │       └─ policy-driven command broker for gsutil, gcloud, Kafka, etc.
   │       └─ forwards approved commands from sandbox → real host tools
   │
   ├── 2. starts sandbox-mcp Docker container (if not already running)
   │       └─ hardened: --cap-drop=ALL, --read-only, seccomp, uid=1000
   │       └─ MCP server on 127.0.0.1:9100 (tools jailed to /workspace)
   │
   └── 3. starts the agent REPL (connects to sandbox via HTTP/SSE)
           └─ tools: execute_command, read_file, write_file, append_file,
           │         delete_file, list_dir
           └─ streaming responses with thinking, text, and tool calls
           └─ session persistence, turn logging, cost tracking
```

### Root mode (`--root`)

Bypasses the sandbox entirely — tools run **directly on the host** via
`local_tools.py`. Useful for development, but **use with caution**.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              HOST                                        │
│                                                                          │
│   ┌──────────┐      ┌──────────────────────────────────────────────┐     │
│   │ run.sh   │──────▶  agent.py  — Agent factory & CLI              │     │
│   └──────────┘      │    ├─ build_agent()   (Anthropic / OpenAI)    │     │
│                     │    ├─ get_system_prompt()                      │     │
│                     │    └─ parse_args() → run_repl()                │     │
│                     │                                                │     │
│                     │  repl.py  — Interactive REPL                    │     │
│                     │    ├─ Streaming response display               │     │
│                     │    ├─ Ctrl+C cancellation & recovery           │     │
│                     │    ├─ Multi-line paste (prompt_toolkit)        │     │
│                     │    ├─ Session persistence (JSON)               │     │
│                     │    ├─ Turn logging (MIME multipart)            │     │
│                     │    ├─ GCS audit logging                        │     │
│                     │    └─ Token usage & cost tracking              │     │
│                     │                                                │     │
│                     │  model.py — LLM model factory                  │     │
│                     │    ├─ build_model()         → Anthropic/GW     │     │
│                     │    └─ build_openai_model()  → OpenAI direct    │     │
│                     │                                                │     │
│                     │  Supporting modules:                           │     │
│                     │    ├─ local_tools.py      (root-mode tools)    │     │
│                     │    ├─ mcp_helpers.py      (MCP connection)     │     │
│                     │    ├─ session_store.py    (save/load sessions) │     │
│                     │    ├─ usage_helpers.py    (cost calculation)   │     │
│                     │    ├─ logging_config.py   (rotating file logs) │     │
│                     │    ├─ turn_logger.py      (turn-by-turn logs)  │     │
│                     │    ├─ turn_log_to_html.py (HTML report gen)    │     │
│                     │    └─ gcs_audit_logger.py (GCS audit trail)    │     │
│                     └──────────────────────────────────────────────┘     │
│                            │ HTTP/SSE                                    │
│                     ┌──────▼───────────────────────────────────────┐     │
│                     │  CONTAINER (sandbox-mcp)                     │     │
│                     │    mcp_server.py  on 127.0.0.1:9100          │     │
│                     │    atom-command-proxy → broker via socket     │     │
│                     │    tools jailed to /workspace                 │     │
│                     │    hardened: cap-drop, seccomp, read-only     │     │
│                     └──────────────────────────────────────────────┘     │
│                                                                          │
│   ┌──────────────────────────────────────────────────────────────┐       │
│   │  atom-command-broker — host-side command execution engine     │       │
│   │    policy enforcement, tool adapters, audit logging           │       │
│   │    gsutil / gcloud / Kafka CLI tools / extensible             │       │
│   └──────────────────────────────────────────────────────────────┘       │
│                                                                          │
│   ┌──────────────────────────────────────────────────────────────┐       │
│   │  connectors/slackbot/  — Slack Bot connector                  │       │
│   │    Uses the same agent factory, runs as a Slack event listener│       │
│   └──────────────────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Quick start

### Step 1 — Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Colima running
- [uv](https://docs.astral.sh/uv/) installed (Python 3.13)
- LLM Gateway key (or OpenAI key)

### Step 2 — Configure your API key

Create `~/.config/atom-agentic-ai/env.sh`:

```bash
# LLM Gateway (default, requires corporate VPN)
export LLM_API_KEY=<your-llm-gateway-key>
export LLM_GATEWAY_URL=<gateway-base-url>
export MODEL_NAME=<model-name>          # e.g. claude-sonnet-4-5
export LLM_GATEWAY_HEADER='{"x-header": "value"}'  # optional

# OR: OpenAI direct
# export PERSONAL_OPENAI_API_KEY=sk-...
```

### Step 3 — Build the sandbox image

Only needed once (or after changes to `sandbox/`):

```bash
./sandbox/sandbox.sh build
```

### Step 4 — Run the agent

```bash
./run.sh
```

That's it. The script auto-starts the command broker and sandbox container,
syncs dependencies, and drops you into the agent REPL.

---

## Flags

| Flag | Description |
|---|---|
| `--openai` | Use OpenAI via `PERSONAL_OPENAI_API_KEY` |
| `--verbose` / `-v` | Show full node-by-node agent execution |
| `--skip-update` / `-s` | Skip `uv sync` for faster startup |
| `--no-sandbox` | Skip sandbox auto-start (must already be running) |
| `--root` | **Root mode** — local tools on host, no sandbox |
| `--session FILE` | Resume a specific session file |
| `--system-prompt FILE` | Custom system prompt file |
| `--mcp-url URL` | Override MCP server URL (default: `http://127.0.0.1:9100/sse`) |
| `--slackbot` | Run as Slack bot connector instead of REPL |

```bash
./run.sh --openai --verbose
./run.sh --skip-update
./run.sh --root                           # local tools, no Docker needed
./run.sh --session my-session.json        # resume previous session
./run.sh --system-prompt custom.md        # custom system prompt
./run.sh --slackbot                       # run as Slack bot
```

---

## Key modules

### `agent/agent.py` — Agent factory & CLI entry point

- **`build_agent()`** — Creates a pydantic-ai `Agent` with either MCP sandbox
  tools or local host tools (root mode).
- **System prompt resolution** — Priority: CLI `--system-prompt` flag →
  `~/.config/atom-agentic-ai/system_prompt.md` → built-in default.
- **Model settings** — Anthropic: extended thinking (adaptive), prompt/tool/message
  caching, fine-grained tool streaming. OpenAI: standard settings.
- **History processors** — Strips `ThinkingPart` blocks from older messages to
  avoid stale signature errors and reduce token usage.

### `agent/repl.py` — Interactive REPL

The heart of the user experience. Orchestrates the full prompt → response cycle:

| Feature | Details |
|---|---|
| **Streaming display** | Real-time rendering of thinking 💭, text 💬, tool plans 🔧, tool execution ⚙️ |
| **Ctrl+C cancellation** | Graceful mid-turn cancellation with history recovery and partial-message preservation |
| **Multi-line paste** | `prompt_toolkit` with bracketed paste — paste multi-line text without submitting early |
| **Session persistence** | Auto-saves conversation history + usage stats to JSON after every turn |
| **Turn logging** | Every LLM output (thinking, text, tool plan, tool exec, usage) is logged as a MIME multipart file |
| **HTML reports** | At session end, generates an interactive HTML report of the full conversation |
| **GCS audit logging** | Optional per-turn JSONL audit trail uploaded to Google Cloud Storage |
| **Cost tracking** | Per-turn and per-session token usage with USD cost breakdown (cache read/write/new/output) |
| **History sanitization** | Repairs orphaned tool-call/tool-result pairs after cancellation to keep the API happy |
| **Transport recovery** | Automatically recovers from transient HTTP/SSE connection drops |

### `agent/model.py` — LLM model factory

- **`build_model()`** — Anthropic model via enterprise LLM Gateway. Reads
  `LLM_API_KEY`, `LLM_GATEWAY_URL`, `MODEL_NAME` from environment. Falls back
  to `~/.zshrc` for API key resolution.
- **`build_openai_model()`** — OpenAI Responses model via `PERSONAL_OPENAI_API_KEY`.

### `agent/local_tools.py` — Root-mode tools

Provides the same 6 tools as the MCP sandbox but running directly on the host:
`execute_command`, `read_file`, `write_file`, `append_file`, `delete_file`, `list_dir`.

- **Path safety** — All paths resolved against `SAFE_ROOT` (defaults to CWD); escapes are rejected.
- **Command timeout** — Configurable via `LOCAL_CMD_TIMEOUT` env var (default: 120s).

### `agent/session_store.py` — Session persistence

- Serialises conversation history using pydantic-ai's `ModelMessagesTypeAdapter`.
- Atomic writes via temp-file + rename to prevent corruption on kill.
- Strips `ThinkingPart` blocks on load to avoid stale Anthropic signature errors.
- Auto-generates timestamped session files under `~/atom-agentic-ai/logs/sessions/`.

### `agent/usage_helpers.py` — Token usage & cost tracking

- Per-turn and per-session accumulators for input/output/cache tokens.
- USD cost calculation based on Claude Sonnet 4 pricing:
  - Input: $3.00/1M tokens
  - Output: $15.00/1M tokens
  - Cache read: 10% of input price
  - Cache write: 125% of input price
- Detailed cost breakdown in every usage line.

### `agent/turn_logger.py` — Structured turn logging

Logs every LLM interaction in a hierarchical format:

```
Session > Query > Turn > Sequence

File naming: q{QQ}.t{TT}.s{SS}.{type}.{label}.txt
Directory:   LOG_DIR / YYYY-MM-DD / username / HH-MM-SS.mmmZ /
```

Types: `thinking`, `text`, `plan`, `exec`, `usage`, `system_prompt`, `user_prompt`, `session_metadata`

Each file uses MIME multipart format with headers (timestamp, query, turn, tool name, etc.)
and content parts. Optionally mirrors to GCS.

### `agent/turn_log_to_html.py` — HTML report generator

Converts turn log directories into interactive HTML reports with:
- Tailwind CSS styling with type-specific color coding
- Click-to-filter by type (THINKING, TEXT, PLAN, EXEC, COST, etc.)
- Full-text search with highlighting
- JSON prettify toggle for tool arguments
- Responsive layout

### `agent/gcs_audit_logger.py` — GCS audit trail

- Per-turn JSONL event logging to Google Cloud Storage.
- Lazy GCS client with auto-refreshing `gcloud auth` tokens (25-min TTL).
- Retry logic with exponential backoff for token fetching.
- Fire-and-forget design — GCS failures never crash the agent.
- Session-end sentinel blob (`999-EXIT`) with cumulative usage stats.

### `agent/logging_config.py` — Application logging

- Rotating file handler (DEBUG+) at `~/atom-agentic-ai/logs/atom.log` (20MB × 5 files).
- Console handler (WARNING+) to keep the REPL clean.
- Configurable log directory via `ATOM_LOG_DIR` env var.
- All loggers under the `atom.*` namespace.

### `connectors/slackbot/` — Slack bot connector

Run the agent as a Slack bot that listens to events and responds in channels/DMs.
Uses the same agent factory and model configuration. Start with `./run.sh --slackbot`.

### `agent/agent_mini.py` & `agent/agent_mini_v2.py` — Minimal agent examples

Simplified single-file agents for experimentation:
- `agent_mini.py` — Bare-bones agent with `execute_script` tool (~30 lines).
- `agent_mini_v2.py` — Adds retry logic, custom system prompts from file, and error handling.

---

## Sandbox management

```bash
./sandbox/sandbox.sh build              # (re)build the image
./sandbox/sandbox.sh start              # start MCP server
./sandbox/sandbox.sh start --port 8811  # custom port
./sandbox/sandbox.sh stop               # stop MCP server
./sandbox/sandbox.sh status             # running? port? uptime?
./sandbox/sandbox.sh shell              # exec into running container
./sandbox/sandbox.sh run -- python3 foo.py  # one-off command
./sandbox/sandbox.sh clean              # stop container + remove image
```

### Nuke and rebuild

```bash
./sandbox/sandbox.sh clean && ./sandbox/sandbox.sh build && ./sandbox/sandbox.sh start
```

---

## Command broker

The sandbox has no credentials or network access. Host-side commands (gsutil,
gcloud, Kafka CLI tools) are forwarded via Unix socket to **atom-command-broker**
running on the host.

```bash
./sandbox/atom-command-broker/broker-ctl.sh start     # start broker
./sandbox/atom-command-broker/broker-ctl.sh stop      # stop broker
./sandbox/atom-command-broker/broker-ctl.sh status    # check status
./sandbox/atom-command-broker/broker-ctl.sh restart   # restart without container restart
```

`run.sh` starts the broker automatically. You can restart it anytime without
touching the sandbox container. See [`docs/command-proxy-broker-design.md`](docs/command-proxy-broker-design.md)
for full architecture, protocol, and policy documentation.

---

## Project layout

```
atom-agentic-ai/
├── run.sh                              # ⭐ start here
├── Makefile                            # lock / install / update-requirements
├── pyproject.toml                      # project metadata & dependencies
├── uv.lock                            # locked dependency versions
│
├── agent/                              # 🧠 core agent package
│   ├── agent.py                        # agent factory, system prompt, CLI
│   ├── repl.py                         # interactive REPL (streaming, cancel, sessions)
│   ├── model.py                        # LLM model factory (Anthropic GW / OpenAI)
│   ├── local_tools.py                  # host-local tools for --root mode
│   ├── mcp_helpers.py                  # MCP server connection & health check
│   ├── session_store.py                # JSON session save/load with type adapters
│   ├── usage_helpers.py                # token usage tracking & cost calculation
│   ├── logging_config.py               # rotating file + console logging
│   ├── turn_logger.py                  # structured turn-by-turn MIME logging
│   ├── turn_log_to_html.py             # interactive HTML report generator
│   ├── gcs_audit_logger.py             # GCS JSONL audit trail
│   ├── agent_mini.py                   # minimal agent example
│   ├── agent_mini_v2.py                # minimal agent with retries
│   └── __init__.py
│
├── connectors/                         # 🔌 external connectors
│   └── slackbot/                       # Slack bot connector
│
├── sandbox/                            # 🐳 hardened Docker sandbox
│   ├── sandbox.sh                      # sandbox manager (build/start/stop/shell/...)
│   ├── mcp_server.py                   # MCP server (runs inside Docker)
│   ├── Dockerfile                      # hardened container image
│   ├── seccomp-strict.json             # syscall allowlist
│   ├── atom-command-proxy.py           # container-side thin command relay
│   ├── atom-command-broker/            # host-side command broker
│   │   ├── broker.py                   # broker daemon
│   │   ├── broker-ctl.sh              # start/stop/restart/status
│   │   ├── protocol.py                # versioned protocol definitions
│   │   ├── policy.py                  # centralized policy engine
│   │   ├── registry.py               # executable registry
│   │   ├── default-policy.json        # default policy config
│   │   └── adapters/                  # tool adapters (gsutil, gcloud, kafka)
│   ├── test-mcp.py                     # MCP smoke test
│   └── test-security.sh                # security boundary tests
│
├── docs/                               # 📚 design documents
│   ├── command-proxy-broker-design.md  # atom-command-proxy/broker architecture
│   ├── proxy-design.md                 # original proxy design (historical)
│   ├── mcp-design.md                   # MCP transport options & curl guide
│   ├── logging.md                      # logging architecture
│   ├── logging-v2.md                   # turn-logger design (v2)
│   ├── turn-log-html.md                # HTML report generator docs
│   └── token-usage-design.md           # token usage & cost tracking design
│
├── deploy/                             # deployment configs
├── logs/                               # local log output
└── dist/                               # build artifacts
```

---

## Environment variables

### Required (pick one)

| Variable | Description |
|---|---|
| `LLM_API_KEY` | Enterprise LLM Gateway API key |
| `LLM_GATEWAY_URL` | Gateway base URL (for Anthropic) |
| `MODEL_NAME` | Model name (e.g. `claude-sonnet-4-5`) |
| `PERSONAL_OPENAI_API_KEY` | OpenAI API key (with `--openai` flag) |

### Optional

| Variable | Description |
|---|---|
| `LLM_GATEWAY_HEADER` | JSON object of extra gateway headers |
| `ATOM_AUDIT_LOG_GCS_PATH` | GCS path for audit logs (e.g. `gs://bucket/prefix`) |
| `ATOM_LOG_DIR` | Override log directory (default: `~/atom-agentic-ai/logs/`) |
| `ATOM_LOG_URL_PREFIX` | Web URL prefix for GCS log links |
| `ATOM_LOG_URL_GCS_PREFIX` | GCS path prefix to strip for web URL mapping |
| `LOCAL_TOOLS_ROOT` | Root directory for local tools in root mode (default: CWD) |
| `LOCAL_CMD_TIMEOUT` | Command timeout in seconds for root mode (default: 120) |
| `STRIP_ALL_THINKING` | Set to `1` to strip all thinking blocks from history |
| `UV_INDEX_URL` | Custom PyPI index URL |
| `UV_INSECURE_HOST` | PyPI host to allow insecure connections |

---

## Dependencies

```bash
uv sync          # install deps
uv add <pkg>     # add a new dependency
```

If your network requires a custom PyPI index, add to `~/.config/atom-agentic-ai/env.sh`:

```bash
export UV_INDEX_URL=https://your-internal-pypi-mirror/simple
export UV_INSECURE_HOST=your-internal-pypi-mirror
```

---

## Further reading

- [`docs/command-proxy-broker-design.md`](docs/command-proxy-broker-design.md) — atom-command-proxy/broker architecture, protocol, policy, security model
- [`docs/mcp-design.md`](docs/mcp-design.md) — MCP transport options, tradeoffs, SSE internals, curl testing guide
- [`docs/proxy-design.md`](docs/proxy-design.md) — Original proxy design rationale & directory-mount lessons (historical)
- [`docs/logging.md`](docs/logging.md) — Logging architecture overview
- [`docs/logging-v2.md`](docs/logging-v2.md) — Turn-logger v2 design (Session > Query > Turn > Sequence)
- [`docs/turn-log-html.md`](docs/turn-log-html.md) — HTML report generator features & usage
- [`docs/token-usage-design.md`](docs/token-usage-design.md) — Token usage tracking & cost calculation design
- [`connectors/slackbot/README.md`](connectors/slackbot/README.md) — Slack bot connector setup & usage
- [`sandbox/README.md`](sandbox/README.md) — Sandbox internals & security model
