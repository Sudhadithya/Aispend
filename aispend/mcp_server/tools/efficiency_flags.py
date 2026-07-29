"""Heuristic 'cheaper model likely fine' report.

Advisory only, not a validated savings guarantee. Rule: flag requests sent to
an Opus-tier model where total tokens (input + output) fall under
SMALL_REQUEST_TOKEN_THRESHOLD — a proxy for "this looked like a simple prompt
that didn't need the most expensive model."
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
        rows = db.get_all_requests(conn, since=since, until=until)

    flags = []
    for row in rows:
        total_tokens = row["input_tokens"] + row["output_tokens"]
        is_opus = row["model"].startswith(OPUS_MODEL_PREFIX)
        if is_opus and total_tokens < SMALL_REQUEST_TOKEN_THRESHOLD:
            flags.append(
                {
                    "id": row["id"],
                    "model": row["model"],
                    "total_tokens": total_tokens,
                    "cost_usd": float(row["cost_usd"]),
                    "created_at": row["created_at"].isoformat(),
                    "reason": (
                        f"Opus used for a {total_tokens}-token request "
                        f"(< {SMALL_REQUEST_TOKEN_THRESHOLD} tokens) — "
                        "a cheaper model may have sufficed."
                    ),
                }
            )
    return flags
