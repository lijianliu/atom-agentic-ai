# atom-agentic-ai

Agentic AI for Enterprise with built-in Security — tools run inside a
hardened Docker sandbox, never on the host machine.

---

## How it works

```
./run.sh
   │
   ├── 1. starts gsutil proxy on host (if not running)
   │       └─ forwards gsutil commands from sandbox → real gcloud credentials
   │
   ├── 2. starts sandbox-mcp Docker container (if not already running)
   │       └─ hardened: --cap-drop=ALL, --read-only, seccomp, uid=1000
   │       └─ MCP server on 127.0.0.1:9100 (tools jailed to /workspace)
   │
   └── 3. starts the agent (connects to sandbox via HTTP/SSE)
           └─ tools: execute_command, read_file, write_file, list_dir, ...
           └─ nothing executes on the host
```

---

## Quick start

### Step 1 — Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Colima running
- [uv](https://docs.astral.sh/uv/) installed
- LLM Gateway key (or OpenAI key)

### Step 2 — Configure your API key

Create `~/.config/atom-agentic-ai/env.sh`:

```bash
# LLM Gateway (default, requires corporate VPN)
export LLM_API_KEY=<your-element-llm-gateway-key>
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

That's it. The script auto-starts the gsutil proxy and sandbox container,
syncs dependencies, and drops you into the agent REPL.

---

## Flags

| Flag | Description |
|---|---|
| `--openai` | Use OpenAI via `PERSONAL_OPENAI_API_KEY` |
| `--verbose` / `-v` | Show full node-by-node agent execution |
| `--skip-update` / `-s` | Skip `uv sync` for faster startup |
| `--no-sandbox` | Skip sandbox auto-start (must already be running) |
| `--mcp-url URL` | Override MCP server URL (default: `http://127.0.0.1:9100/sse`) |

```bash
./run.sh --openai --verbose
./run.sh --skip-update
```

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

## gsutil proxy

The sandbox has no credentials or network access. gsutil commands are
forwarded via Unix socket to a proxy running on the host.

```bash
./sandbox/gsutil-proxy-ctl.sh start    # start proxy
./sandbox/gsutil-proxy-ctl.sh stop     # stop proxy
./sandbox/gsutil-proxy-ctl.sh status   # check status
```

`run.sh` starts the proxy automatically. You can restart it anytime without
touching the sandbox container. See [`docs/proxy-design.md`](docs/proxy-design.md).

---

## Project layout

```
atom-agentic-ai/
├── run.sh                          # ⭐ start here
├── agent/
│   ├── agent.py                    # agent REPL — connects to MCP sandbox
│   ├── model.py                    # LLM model factory
│   └── __init__.py
├── sandbox/
│   ├── sandbox.sh                  # sandbox manager (build/start/stop/shell/...)
│   ├── mcp_server.py               # MCP server (runs inside Docker)
│   ├── Dockerfile                  # hardened container image
│   ├── gsutil-proxy.py             # host-side gsutil proxy daemon
│   ├── gsutil-proxy-ctl.sh         # start/stop/status for gsutil proxy
│   ├── gsutil-wrapper.sh           # in-container gsutil thin client
│   ├── gsutil-policy.json          # allowed commands + buckets
│   ├── test-mcp.py                 # smoke test (stdlib only, ~0.1s)
│   └── seccomp-strict.json         # syscall allowlist
├── docs/
│   ├── mcp-design.md               # MCP transport options + curl guide
│   └── proxy-design.md             # gsutil proxy design options
├── pyproject.toml
└── uv.lock
```

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

- [`docs/mcp-design.md`](docs/mcp-design.md) — all MCP transport options,
  tradeoffs, how SSE works, and a full curl testing guide
- [`docs/proxy-design.md`](docs/proxy-design.md) — gsutil proxy design options
  and why we mount a directory instead of a socket file
