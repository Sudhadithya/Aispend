"""Heuristic 'cheaper model likely fine' report.

Advisory only, not a validated savings guarantee. Rule: flag requests sent to
a model above the cheapest tier whose total tokens fall under
SMALL_REQUEST_TOKEN_THRESHOLD — a proxy for "this looked like a simple prompt
that didn't need a pricier model." Each tier above Haiku is checked against
the same threshold and names the specific cheaper model it suggests, so the
flag stays actionable rather than a blanket "use something cheaper."

"Total tokens" counts cached tokens as well as uncached ones. A short question
asked against a large cached system prompt is not a small request, and
shouldn't be flagged as one just because its uncached remainder is tiny.
"""

from __future__ import annotations

from datetime import datetime

from aispend.storage import db

SMALL_REQUEST_TOKEN_THRESHOLD = 500

# Tiers above the cheapest model, each paired with what a small request on it
# should have used instead. Haiku itself is the floor and is never flagged.
FLAGGED_TIERS: list[tuple[str, str]] = [
    ("claude-fable", "Opus"),
    ("claude-mythos", "Opus"),
    ("claude-opus", "a cheaper model"),
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
                        f"{suggested_model} may have sufficed."
                    ),
                }
                for row in rows
            )

    flags.sort(key=lambda flag: flag["created_at"])
    return flags
