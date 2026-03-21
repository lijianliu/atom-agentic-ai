# atom-agentic-ai

Agentic AI agent for Enterprise with built-in Security and Audit

## Running the agent

The agent supports two modes: **LLM Gateway** (default, for company corporate network) and **OpenAI** (for use outside corporate network).

### Mode 1 — LLM Gateway (default)

Requires company VPN. Set the following in `~/.config/atom-agentic-ai/env.sh`:

```bash
export LLM_API_KEY=<your-element-llm-gateway-key>
export LLM_GATEWAY_URL=<gateway-base-url>
export MODEL_NAME=<model-name>          # e.g. claude-sonnet-4-5
export LLM_GATEWAY_HEADER='{"x-header": "value"}'  # optional, JSON object
```

Then run:

```bash
./run-atom-ai-cli.sh
```

> Need a key? Reach out to your internal LLM Gateway team.

### Mode 2 — OpenAI direct (`--openai`)

Requires a personal OpenAI API key. **Only works outside the company's corporate network** (e.g. personal hotspot) as the proxy blocks direct access to `api.openai.com`.

Set your key:

```bash
export PERSONAL_OPENAI_API_KEY=sk-...
```

Or inline:

```bash
PERSONAL_OPENAI_API_KEY=sk-... ./run-atom-ai-cli.sh --openai
```

### Additional flags

| Flag | Description |
|---|---|
| `--openai` | Use OpenAI directly via `PERSONAL_OPENAI_API_KEY` |
| `--verbose` / `-v` | Show full node-by-node agent execution details |
| `--skip-update` / `-s` | Skip `uv sync` step for faster startup |

Flags are composable:

```bash
PERSONAL_OPENAI_API_KEY=sk-... ./run-atom-ai-cli.sh --openai --verbose --skip-update
```

---

## Setup

```bash
uv lock    # generates local uv.lock for your environment
uv sync    # installs deps
```

If your network requires a custom PyPI index, add these to `~/.config/atom-agentic-ai/env.sh`:

```bash
export UV_INDEX_URL=https://your-internal-pypi-mirror/simple
export UV_INSECURE_HOST=your-internal-pypi-mirror  # if needed
```

Both `make` and `run-atom-ai-cli.sh` will source this file automatically if it exists.

## File overview

| File | Committed | Purpose |
|---|---|---|
| `pyproject.toml` | ✅ | Dependency constraints |
| `requirements.txt` | ✅ | Exact pins + hashes, universal install |
| `uv.toml` | ✅ | uv config (python version etc.) |
| `uv.lock` | ❌ | Generated locally — contains env-specific URLs |

## Updating dependencies

```bash
uv add <package>          # updates pyproject.toml + uv.lock
make update-requirements  # regenerates requirements.txt
git add pyproject.toml requirements.txt && git commit -m "chore: update deps"
```
