"""Hardcoded $/token table for Anthropic models.

This table is a manually maintained snapshot of Anthropic's published pricing.
It will go stale whenever Anthropic changes prices — there is no API to fetch
current pricing, so update PRICING_PER_MTOK by hand when prices change.
Source: https://www.anthropic.com/pricing (as of 2026-07-29).

Prices are USD per million tokens, as (input, output).
"""

from __future__ import annotations

PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-1-20250805": (15.0, 75.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-7-sonnet-20250219": (3.0, 15.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-3-5-haiku-20241022": (0.8, 4.0),
    "claude-3-haiku-20240307": (0.25, 1.25),
    "claude-3-opus-20240229": (15.0, 75.0),
}


def calculate_cost(model: str, *, input_tokens: int, output_tokens: int) -> float:
    """Raises ValueError if `model` isn't in PRICING_PER_MTOK (add it by hand)."""
    if model not in PRICING_PER_MTOK:
        raise ValueError(f"Unknown model for pricing: {model!r}. Add it to PRICING_PER_MTOK.")
    input_price, output_price = PRICING_PER_MTOK[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
