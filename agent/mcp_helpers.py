"""
mcp.py — MCP server helpers for Atom Agent.
============================================
Encapsulates MCP server construction, connectivity checks, and defaults.
"""
from __future__ import annotations

import httpx
from pydantic_ai.mcp import MCPServerSSE

DEFAULT_MCP_URL = "http://127.0.0.1:9100/sse"


def build_tcp_mcp_server(url: str) -> MCPServerSSE:
    """MCP over plain TCP — the normal case."""
    return MCPServerSSE(
        url=url,
        http_client=httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=300.0),
        ),
    )


async def check_mcp_reachable(mcp_url: str) -> bool:
    """Return True if the MCP server is reachable.

    SSE endpoints stream forever, so a successful TCP connect is proof
    enough.  We use a very short read timeout (0.3 s) to avoid blocking
    startup — a ReadTimeout means "connected but streaming", which is fine.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, read=0.3),
        ) as c:
            await c.get(mcp_url)
    except httpx.ReadTimeout:
        return True  # SSE streams forever — timeout == connected
    except Exception:
        return False
    return True
