"""Cost calculation from token counts.

Prices are USD per 1,000,000 tokens. The pricing table is loaded from
pricing.json so users can adjust to their billed rates without code changes.
"""

from __future__ import annotations

from .config import load_pricing


_PRICING_CACHE: dict | None = None


def _table() -> dict:
    global _PRICING_CACHE
    if _PRICING_CACHE is None:
        _PRICING_CACHE = load_pricing()
    return _PRICING_CACHE


def reload_pricing() -> None:
    global _PRICING_CACHE
    _PRICING_CACHE = None


def cost_for(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Return total USD cost for the given token mix.

    Unknown models fall through to the configured fallback model rates.
    """
    table = _table()
    models = table.get("models", {})
    fallback_key = table.get("fallback", "claude-sonnet-4-6")
    rates = models.get(model or "") or models.get(fallback_key) or {
        "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75,
    }
    return (
        (input_tokens / 1_000_000) * rates["input"]
        + (output_tokens / 1_000_000) * rates["output"]
        + (cache_read_tokens / 1_000_000) * rates["cache_read"]
        + (cache_creation_tokens / 1_000_000) * rates["cache_creation"]
    )
