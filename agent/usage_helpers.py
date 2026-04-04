"""
usage_helpers.py — Token usage formatting utilities.

Terminology hierarchy: Session > Query > Turn > Sequence
- Session: one REPL session
- Query: one user prompt
- Turn: one model request within a query
- Sequence: one logged item within a turn
"""
from __future__ import annotations


# Pricing per 1M tokens (Claude Sonnet 4)
PRICE_INPUT_BASE = 3.00  # $/1M tokens
PRICE_OUTPUT = 15.00  # $/1M tokens
PRICE_CACHE_READ_RATE = 0.10  # 10% of base
PRICE_CACHE_WRITE_RATE = 1.25  # 125% of base


def _calc_cost(cache_read: int, cache_write: int, new: int, out: int) -> tuple[float, float, float, float, float]:
    """Calculate cost for each token category. Returns (read_cost, write_cost, new_cost, out_cost, total)."""
    read_cost = cache_read * (PRICE_INPUT_BASE * PRICE_CACHE_READ_RATE) / 1_000_000
    write_cost = cache_write * (PRICE_INPUT_BASE * PRICE_CACHE_WRITE_RATE) / 1_000_000
    new_cost = new * PRICE_INPUT_BASE / 1_000_000
    out_cost = out * PRICE_OUTPUT / 1_000_000
    total = read_cost + write_cost + new_cost + out_cost
    return read_cost, write_cost, new_cost, out_cost, total


def format_usage_line(usage, query: int = 0, turn: int = 0) -> str:
    """Format a pydantic-ai Usage object into a human-readable stats string.
    
    Args:
        usage: pydantic-ai Usage object
        query: current query number (for display label)
        turn: current turn number within query (for display label)
    """
    in_t = usage.input_tokens or 0
    out_t = usage.output_tokens or 0
    cache_write = getattr(usage, 'cache_write_tokens', 0) or 0
    cache_read = getattr(usage, 'cache_read_tokens', 0) or 0
    new_t = in_t - cache_write - cache_read
    reqs = getattr(usage, 'requests', 0) or 0
    tools = getattr(usage, 'tool_calls', 0) or 0
    
    # Calculate costs
    read_cost, write_cost, new_cost, out_cost, total_cost = _calc_cost(cache_read, cache_write, new_t, out_t)
    
    # Derived pricing strings
    cache_read_price = PRICE_INPUT_BASE * PRICE_CACHE_READ_RATE
    cache_write_price = PRICE_INPUT_BASE * PRICE_CACHE_WRITE_RATE
    
    # Line 1: Main stats (caller adds 📊 [Usage prefix)
    if query > 0 and turn > 0:
        label = f"Query {query} Turn {turn}"
    elif query > 0:
        label = f"Query {query}"
    else:
        label = f"#{reqs}"
    
    line1 = (
        f"{label}] | ${total_cost:.2f} | "
        f"{in_t:,} in (= {cache_read:,} cache read + {cache_write:,} cache write + {new_t:,} new) "
        f"\u2192 {out_t:,} out | {tools} tools"
    )
    
    # Line 2: Cost breakdown
    line2 = (
        f"   \u2514\u2500 cost: "
        f"{cache_read:,} \u00d7 ${cache_read_price:.2f}/1M + "
        f"{cache_write:,} \u00d7 ${cache_write_price:.2f}/1M + "
        f"{new_t:,} \u00d7 ${PRICE_INPUT_BASE:.0f}/1M + "
        f"{out_t:,} \u00d7 ${PRICE_OUTPUT:.0f}/1M = "
        f"${read_cost:.2f} + ${write_cost:.2f} + ${new_cost:.2f} + ${out_cost:.2f} = ${total_cost:.2f}"
    )
    
    return f"{line1}\n{line2}"


def build_usage_dict(usage) -> dict:
    """Build a dict of token usage stats suitable for audit logging."""
    in_t = usage.input_tokens or 0
    out_t = usage.output_tokens or 0
    cache_write = getattr(usage, 'cache_write_tokens', 0) or 0
    cache_read = getattr(usage, 'cache_read_tokens', 0) or 0
    new_t = in_t - cache_write - cache_read
    read_cost, write_cost, new_cost, out_cost, total_cost = _calc_cost(cache_read, cache_write, new_t, out_t)
    return {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "new_tokens": new_t,
        "requests": getattr(usage, 'requests', 0) or 0,
        "tool_calls": getattr(usage, 'tool_calls', 0) or 0,
        "cache_hit_pct": round((cache_read / in_t * 100) if in_t > 0 else 0, 1),
        "cost_usd": round(total_cost, 4),
    }


def new_session_usage() -> dict:
    """Create a fresh session-level usage accumulator."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "requests": 0,
        "tool_calls": 0,
        "queries": 0,
    }


def accumulate_session_usage(session: dict, usage) -> None:
    """Add a single run's usage into the session accumulator."""
    session["input_tokens"] += usage.input_tokens or 0
    session["output_tokens"] += usage.output_tokens or 0
    session["cache_write_tokens"] += getattr(usage, 'cache_write_tokens', 0) or 0
    session["cache_read_tokens"] += getattr(usage, 'cache_read_tokens', 0) or 0
    session["requests"] += getattr(usage, 'requests', 0) or 0
    session["tool_calls"] += getattr(usage, 'tool_calls', 0) or 0
    session["queries"] += 1
    session["total_tokens"] = session["input_tokens"] + session["output_tokens"]


def format_session_usage(session: dict) -> str:
    """Format session accumulator into a human-readable string."""
    inp = session["input_tokens"]
    out = session["output_tokens"]
    cache_read = session["cache_read_tokens"]
    cache_write = session["cache_write_tokens"]
    queries = session["queries"]
    reqs = session["requests"]
    tools = session["tool_calls"]
    new_t = inp - cache_write - cache_read
    
    # Calculate costs
    read_cost, write_cost, new_cost, out_cost, total_cost = _calc_cost(cache_read, cache_write, new_t, out)
    
    # Derived pricing
    cache_read_price = PRICE_INPUT_BASE * PRICE_CACHE_READ_RATE
    cache_write_price = PRICE_INPUT_BASE * PRICE_CACHE_WRITE_RATE
    
    # Line 1: Main stats (caller adds 📊 [Session prefix)
    line1 = (
        f"{queries} queries, {reqs} reqs] | ${total_cost:.2f} | "
        f"{inp:,} in (= {cache_read:,} cache read + {cache_write:,} cache write + {new_t:,} new) "
        f"\u2192 {out:,} out | {tools} tools"
    )
    
    # Line 2: Cost breakdown
    line2 = (
        f"   \u2514\u2500 cost: "
        f"{cache_read:,} \u00d7 ${cache_read_price:.2f}/1M + "
        f"{cache_write:,} \u00d7 ${cache_write_price:.2f}/1M + "
        f"{new_t:,} \u00d7 ${PRICE_INPUT_BASE:.0f}/1M + "
        f"{out:,} \u00d7 ${PRICE_OUTPUT:.0f}/1M = "
        f"${read_cost:.2f} + ${write_cost:.2f} + ${new_cost:.2f} + ${out_cost:.2f} = ${total_cost:.2f}"
    )
    
    return f"{line1}\n{line2}"
