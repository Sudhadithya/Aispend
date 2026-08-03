"""Heuristic 'cheaper model likely fine' report.

Advisory only, not a validated savings guarantee. Rule: flag requests sent to
a model above the cheapest tier whose total tokens fall under
SMALL_REQUEST_TOKEN_THRESHOLD — a proxy for "this looked like a simple prompt
that didn't need a pricier model." Each tier above Haiku is checked against
the same threshold and names the next cheaper tier down as its suggestion, so
the flag stays actionable rather than a blanket "use something cheaper."

"Total tokens" counts cached tokens as well as uncached ones. A short question
asked against a large cached system prompt is not a small request, and
shouldn't be flagged as one just because its uncached remainder is tiny.

Token count is a proxy for size, not difficulty, and can't tell a short-but-hard
prompt from a trivial one. Two cheap mitigations, both using data already on
hand rather than new instrumentation:

- Below TINY_REQUEST_TOKEN_THRESHOLD, the request is small enough that
  suggesting Haiku outright is fair, regardless of starting tier, rather than
  just the next tier down.
- A request that took longer than HIGH_LATENCY_MS_THRESHOLD to answer despite
  its low token count was likely doing real work (extended thinking, tool
  calls) that the token count doesn't show, so it's excluded rather than
  flagged.
"""

from __future__ import annotations

from datetime import datetime

from aispend.storage import db

SMALL_REQUEST_TOKEN_THRESHOLD = 500
TINY_REQUEST_TOKEN_THRESHOLD = 150
HIGH_LATENCY_MS_THRESHOLD = 10_000

# Tiers above the cheapest model, each paired with the next cheaper tier a
# small request on it should have used instead. Haiku itself is the floor and
# is never flagged.
FLAGGED_TIERS: list[tuple[str, str]] = [
    ("claude-fable", "Opus"),
    ("claude-mythos", "Opus"),
    ("claude-opus", "Sonnet"),
    ("claude-sonnet", "Haiku"),
]


def get_efficiency_flags(
    since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    flags = []
    with db.get_connection() as conn:
        for model_prefix, suggested_model in FLAGGED_TIERS:
            rows = db.get_small_requests(
                conn,
                model_prefix=model_prefix,
                max_total_tokens=SMALL_REQUEST_TOKEN_THRESHOLD,
                max_latency_ms=HIGH_LATENCY_MS_THRESHOLD,
                since=since,
                until=until,
            )
            flags.extend(
                {
                    "id": row["id"],
                    "model": row["model"],
                    "total_tokens": row["total_tokens"],
                    "cost_usd": float(row["cost_usd"]),
                    "created_at": row["created_at"].isoformat(),
                    "reason": (
                        f"{row['model']} used for a {row['total_tokens']}-token "
                        f"request (< {SMALL_REQUEST_TOKEN_THRESHOLD} tokens) — "
                        + (
                            "Haiku"
                            if row["total_tokens"] < TINY_REQUEST_TOKEN_THRESHOLD
                            else suggested_model
                        )
                        + " may have sufficed."
                    ),
                }
                for row in rows
            )

    flags.sort(key=lambda flag: flag["created_at"])
    return flags
