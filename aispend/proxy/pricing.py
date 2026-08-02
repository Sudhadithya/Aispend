"""Hardcoded $/token table for Anthropic models, plus the cost arithmetic.

This table is a manually maintained snapshot of Anthropic's published pricing.
It will go stale whenever Anthropic changes prices or ships a model — there is
no pricing API — so update PRICING_PER_MTOK by hand when that happens.
Source: https://platform.claude.com/docs/en/about-claude/pricing (as of 2026-07-30).

Prices are USD per million tokens, as (input, output).

Two things make the arithmetic more than tokens x rate:

1. Cached tokens bill at their own rates. `input_tokens` in an API response is
   only the *uncached* remainder; cache reads and cache writes are reported
   separately and priced off the base input rate by a fixed multiplier.
2. Pricing is temporal. Claude Sonnet 5 is under introductory pricing until
   2026-09-01, so a cost has to be computed with the rate in effect when the
   request was made, not the rate in effect when the table was last edited.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

# Multipliers applied to a model's base input rate. Anthropic publishes these
# as multipliers rather than per-model prices, and they hold for every model,
# so deriving the cache rates keeps the table small and avoids transcription
# errors when a new model is added.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1

# Model IDs from the 4.6 generation onward are dateless but still pinned
# snapshots, and the API echoes them back verbatim. Earlier aliases resolve to
# a dated ID, and it is the *dated* form that comes back on the response, so
# both forms are listed where they differ.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    # Current generation
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),  # see SONNET_5_INTRODUCTORY below
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # Legacy, still callable
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-5-20251101": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-1-20250805": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
}

# Introductory pricing runs through 2026-08-31; standard rates (the value in
# PRICING_PER_MTOK above) take effect 2026-09-01. Delete this block once the
# cutover has passed and the table entry is correct on its own.
SONNET_5_INTRODUCTORY = (2.0, 10.0)
SONNET_5_STANDARD_PRICING_FROM = date(2026, 9, 1)


class UnknownModelError(ValueError):
    """Raised when a model ID has no entry in PRICING_PER_MTOK.

    Subclasses ValueError so callers that only care that pricing failed can
    still catch broadly, while the proxy can catch this specifically and log
    the missing model ID as an actionable "add this to the table" message.
    """


def _rates_for(model: str, as_of: date) -> tuple[float, float]:
    if model not in PRICING_PER_MTOK:
        raise UnknownModelError(
            f"Unknown model for pricing: {model!r}. Add it to PRICING_PER_MTOK."
        )
    if model == "claude-sonnet-5" and as_of < SONNET_5_STANDARD_PRICING_FROM:
        return SONNET_5_INTRODUCTORY
    return PRICING_PER_MTOK[model]


def calculate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    as_of: datetime | None = None,
) -> float:
    """Cost in USD for one request.

    `input_tokens` must be the uncached remainder as reported by the API — the
    cached portions are passed separately and priced at their own rates.
    `as_of` is the moment the request was made, and defaults to now; it only
    matters while a model is under time-limited introductory pricing.

    Raises UnknownModelError if `model` isn't in PRICING_PER_MTOK.
    """
    moment = (as_of or datetime.now(UTC)).date()
    input_price, output_price = _rates_for(model, moment)

    total_per_mtok = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_read_tokens * input_price * CACHE_READ_MULTIPLIER
        + cache_write_5m_tokens * input_price * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h_tokens * input_price * CACHE_WRITE_1H_MULTIPLIER
    )
    return total_per_mtok / 1_000_000
