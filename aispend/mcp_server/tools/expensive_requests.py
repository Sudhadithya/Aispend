from __future__ import annotations

from datetime import datetime

from aispend.storage import db


def get_expensive_requests(
    limit: int, since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    with db.get_connection() as conn:
        rows = db.get_expensive_requests(conn, limit=limit, since=since, until=until)

    return [
        {
            "id": row["id"],
            "model": row["model"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cost_usd": float(row["cost_usd"]),
            "latency_ms": row["latency_ms"],
            "source_tool": row["source_tool"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
