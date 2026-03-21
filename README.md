# atom-agentic-ai

Agentic AI agent for Enterprise with built-in Security and Audit

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
