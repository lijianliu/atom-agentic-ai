"""LLM Gateway model for the agent."""

from __future__ import annotations

import json
import os
import sys

import httpx
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — taken from environment
# ---------------------------------------------------------------------------

# Exact model name the gateway expects (models.json → "name" field)
_MODEL_NAME = os.environ.get("MODEL_NAME", "")

# Gateway base URL for Anthropic models (no /v1 — different from OpenAI endpoint)
_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "")



# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_model() -> AnthropicModel:
    """Build an AnthropicModel wired to the enterprice LLM Gateway.

    Mirrors atom's ``custom_anthropic`` model construction exactly:
      - Reads LLM_API_KEY from environment (same variable atom uses).
      - Creates httpx.AsyncClient with verify=False (ca_certs_path="false"
        in models.json) and timeout=180 (same as atom).
      - Injects gateway headers: anthropic-version + wm_llm_gw.*
      - Returns AnthropicModel(model_name, provider=AnthropicProvider(...)).

    Environment variables:
        LLM_API_KEY   Required. Your LLM Gateway API key.

    Raises:
        SystemExit: When LLM_API_KEY is not set.
    """
    api_key = _resolve_api_key()

    # atom: httpx.AsyncClient(headers=headers, verify=False, timeout=180)
    # verify=False mirrors ca_certs_path="false" in models.json
    http_client = httpx.AsyncClient(
        headers=_gateway_headers(),
        verify=False,
        timeout=180,
    )

    # atom: AsyncAnthropic(base_url=url, http_client=client, api_key=api_key)
    anthropic_client = AsyncAnthropic(
        base_url=_GATEWAY_URL,
        api_key=api_key,
        http_client=http_client,
    )

    # atom: AnthropicProvider(anthropic_client=...) → AnthropicModel(name, provider)
    provider = AnthropicProvider(anthropic_client=anthropic_client)
    return AnthropicModel(model_name=_MODEL_NAME, provider=provider)


def build_openai_model(model_name: str = "gpt-4.1"):
    """Build an OpenAIResponsesModel using PERSONAL_OPENAI_API_KEY.

    Environment variables:
        PERSONAL_OPENAI_API_KEY   Required. Your personal OpenAI API key.

    Raises:
        SystemExit: When PERSONAL_OPENAI_API_KEY is not set.
    """
    try:
        from pydantic_ai.models.openai import OpenAIResponsesModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError:
        logger.critical(
            "OpenAI support requires the 'openai' package. "
            "Install it with:  pip install pydantic-ai-slim[openai]"
        )
        sys.exit(1)

    api_key = os.environ.get("PERSONAL_OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.critical(
            "PERSONAL_OPENAI_API_KEY is not set. "
            "Export it in your shell:  export PERSONAL_OPENAI_API_KEY=<your-key>"
        )
        sys.exit(1)
    provider = OpenAIProvider(api_key=api_key)
    return OpenAIResponsesModel(model_name, provider=provider)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_api_key() -> str:
    """Read LLM_API_KEY from the environment.

    atom's get_api_key() checks (in order):
      1. Atom config store  (skipped — not relevant here)
      2. os.environ
      3. ~/.zshrc parse
    We replicate steps 2 and 3.
    """
    key = os.environ.get("LLM_API_KEY", "").strip()
    if key:
        return key

    key = _read_from_zshrc("LLM_API_KEY")
    if key:
        return key

    logger.critical(
        "LLM_API_KEY is not set. "
        "Export it in your shell:  export LLM_API_KEY=<your-key>  "
        "Or add it to ~/.zshrc and re-source it."
    )
    sys.exit(1)


def _read_from_zshrc(var_name: str) -> str:
    """Source ~/.zshrc and extract an exported variable value.

    Mirrors atom's get_api_key() fallback in model_factory.py.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["bash", "-c", f"source ~/.zshrc 2>/dev/null && echo ${var_name}"],
            capture_output=True, text=True, timeout=5,
        )
        val = result.stdout.strip()
        return val if val else ""
    except Exception:
        return ""


def _gateway_headers() -> dict[str, str]:
    """Return headers parsed from ``LLM_GATEWAY_HEADER`` (JSON object), or {}."""
    raw = os.environ.get("LLM_GATEWAY_HEADER")
    if not raw:
        return {}
    try:
        headers = json.loads(raw)
        return {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
    except json.JSONDecodeError:
        return {}
