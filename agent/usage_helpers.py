"""
usage_helpers.py — Token usage formatting utilities.
"""
from __future__ import annotations


def format_usage_line(usage) -> str:
    """Format a pydantic-ai Usage object into a human-readable stats string."""
    in_t = usage.input_tokens or 0
    out_t = usage.output_tokens or 0
    cache_write = getattr(usage, 'cache_write_tokens', 0) or 0
    cache_read = getattr(usage, 'cache_read_tokens', 0) or 0
    new_t = in_t - cache_write - cache_read
    reqs = getattr(usage, 'requests', 0) or 0
    tools = getattr(usage, 'tool_calls', 0) or 0
    cache_hit_pct = (cache_read / in_t * 100) if in_t > 0 else 0
    uncached = new_t + cache_write
    return (
        f"{in_t:,} in "
        f"({new_t:,} new \u00b7 {cache_write:,} cache write \u00b7 {cache_read:,} cache read)"
        f" [{cache_hit_pct:.0f}% hit \u00b7 {uncached:,} uncached]"
        f" / {out_t:,} out"
        f" | {reqs} reqs / {tools} tools"
    )


def build_usage_dict(usage) -> dict:
    """Build a dict of token usage stats suitable for audit logging."""
    in_t = usage.input_tokens or 0
    out_t = usage.output_tokens or 0
    cache_write = getattr(usage, 'cache_write_tokens', 0) or 0
    cache_read = getattr(usage, 'cache_read_tokens', 0) or 0
    return {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "new_tokens": in_t - cache_write - cache_read,
        "requests": getattr(usage, 'requests', 0) or 0,
        "tool_calls": getattr(usage, 'tool_calls', 0) or 0,
        "cache_hit_pct": round((cache_read / in_t * 100) if in_t > 0 else 0, 1),
    }


def new_session_usage() -> dict:
    """Create a fresh session-level usage accumulator."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "requests": 0,
        "tool_calls": 0,
        "turns": 0,
    }


def accumulate_session_usage(session: dict, usage) -> None:
    """Add a single run's usage into the session accumulator."""
    session["input_tokens"] += usage.input_tokens or 0
    session["output_tokens"] += usage.output_tokens or 0
    session["cache_write_tokens"] += getattr(usage, 'cache_write_tokens', 0) or 0
    session["cache_read_tokens"] += getattr(usage, 'cache_read_tokens', 0) or 0
    session["requests"] += getattr(usage, 'requests', 0) or 0
    session["tool_calls"] += getattr(usage, 'tool_calls', 0) or 0
    session["turns"] += 1


def format_session_usage(session: dict) -> str:
    """Format session accumulator into a human-readable string."""
    inp = session["input_tokens"]
    out = session["output_tokens"]
    cache_read = session["cache_read_tokens"]
    cache_write = session["cache_write_tokens"]
    total = inp + out
    turns = session["turns"]
    cache_hit_pct = (cache_read / inp * 100) if inp > 0 else 0
    return (
        f"{inp:,} in ({cache_read:,} cache read \u00b7 {cache_write:,} cache write "
        f"\u00b7 {cache_hit_pct:.0f}% hit) / {out:,} out / {total:,} total ({turns} turns)"
    )
