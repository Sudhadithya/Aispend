"""Heuristic 'cheaper model likely fine' report.

Advisory only, not a validated savings guarantee. Rule: flag requests sent to
an Opus-tier model whose total tokens fall under SMALL_REQUEST_TOKEN_THRESHOLD
— a proxy for "this looked like a simple prompt that didn't need the most
expensive model."

"Total tokens" counts cached tokens as well as uncached ones. A short question
asked against a large cached system prompt is not a small request, and
shouldn't be flagged as one just because its uncached remainder is tiny.
"""

from __future__ import annotations

from datetime import datetime

from aispend.storage import db

OPUS_MODEL_PREFIX = "claude-opus"
SMALL_REQUEST_TOKEN_THRESHOLD = 500


def get_efficiency_flags(
    since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    with db.get_connection() as conn:
        rows = db.get_small_requests(
            conn,
            model_prefix=OPUS_MODEL_PREFIX,
            max_total_tokens=SMALL_REQUEST_TOKEN_THRESHOLD,
            since=since,
            until=until,
        )

    return [
        {
            "id": row["id"],
            "model": row["model"],
            "total_tokens": row["total_tokens"],
            "cost_usd": float(row["cost_usd"]),
            "created_at": row["created_at"].isoformat(),
            "reason": (
                f"Opus used for a {row['total_tokens']}-token request "
                f"(< {SMALL_REQUEST_TOKEN_THRESHOLD} tokens) — "
                "a cheaper model may have sufficed."
            ),
        }
        for row in rows
    ]
