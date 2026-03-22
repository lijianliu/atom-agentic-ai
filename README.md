# atom-agentic-ai

Agentic AI for Enterprise with built-in Security — tools run inside a
hardened Docker sandbox, never on the host machine.

---

## How it works

```
./run.sh
   │
   ├── 1. starts sandbox-mcp Docker container (if not already running)
   │       └─ hardened: --cap-drop=ALL, --read-only, seccomp, uid=1000
   │       └─ MCP server on 127.0.0.1:9100 (tools jailed to /workspace)
   │
   └── 2. starts the agent (connects to sandbox via HTTP/SSE)
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

# OR: OpenAI direct (outside corporate network only)
# export PERSONAL_OPENAI_API_KEY=sk-...
```

### Step 3 — Build the sandbox image

Only needed once (or after changes to `sandbox/`):

```bash
docker build --platform linux/arm64 \
  -t hardened-sandbox-mcp:latest \
  -f sandbox/Dockerfile.mcp \
  sandbox/
```

> **Apple Intel?** Replace `linux/arm64` with `linux/amd64`.

### Step 4 — Start the sandbox

```bash
bash sandbox/run-mcp-macos.sh
```

You should see:

```
✅ Container sandbox-mcp started
   MCP endpoint: http://localhost:9100/sse
```

Verify everything is wired up:

```bash
python3 sandbox/test-mcp.py
# 🎉  All tests passed! (0.1s)
```

### Step 5 — Run the agent

```bash
./run.sh
```

That’s it. The script auto-starts the sandbox if it’s not running, syncs
dependencies, and drops you into the agent REPL.

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

## Entering the sandbox

To inspect the running container interactively:

```bash
docker exec -it sandbox-mcp /bin/sh
```

You land as `uid=1000(sandbox)` inside `/workspace`. The rootfs is read-only
— only `/workspace`, `/tmp`, and `/run` are writable tmpfs mounts.

---

## Sandbox management

```bash
# Status
docker ps --filter name=sandbox-mcp

# Logs
docker logs sandbox-mcp

# Stop
docker stop sandbox-mcp

# Restart
docker rm -f sandbox-mcp && bash sandbox/run-mcp-macos.sh
```

---

## Project layout

```
atom-agentic-ai/
├── run.sh                      # ⭐ start here
├── agent/
│   ├── agent.py                # agent REPL — connects to MCP sandbox
│   ├── model.py                # LLM model factory
│   └── __init__.py
├── sandbox/
│   ├── mcp_server.py           # MCP server (runs inside Docker)
│   ├── Dockerfile.mcp          # hardened container image
│   ├── run-mcp-macos.sh        # launch sandbox container
│   ├── test-mcp.py             # smoke test (stdlib only, ~0.1s)
│   └── seccomp-strict-macos.json
├── docs/
│   └── mcp-design.md           # MCP transport options + curl testing guide
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
