from __future__ import annotations

from datetime import datetime

from aispend.storage import db


def get_expensive_requests(
    limit: int, since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    with db.get_connection() as conn:
        rows = db.get_expensive_requests(conn, limit=limit, since=since, until=until)

    # Cache counters are surfaced alongside the raw token counts because they
    # are usually the answer to "why was this request expensive" — a large
    # cache read costs a tenth of fresh input, a large cache write costs more.
    return [
        {
            "id": row["id"],
            "model": row["model"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_write_5m_tokens": row["cache_write_5m_tokens"],
            "cache_write_1h_tokens": row["cache_write_1h_tokens"],
            "cost_usd": float(row["cost_usd"]),
            "latency_ms": row["latency_ms"],
            "source_tool": row["source_tool"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
