from __future__ import annotations

from datetime import datetime

from aispend.storage import db


def get_spend_summary(since: datetime, until: datetime, group_by: str | None = None) -> dict:
    with db.get_connection() as conn:
        rows = db.get_spend_summary(conn, since=since, until=until, group_by=group_by)

    if group_by is None:
        row = rows[0]
        return {
            "total_cost_usd": float(row["total_cost_usd"]),
            "request_count": row["request_count"],
        }

    return {
        "breakdown": [
            {
                "group": str(row["group_value"]),
                "cost_usd": float(row["total_cost_usd"]),
                "request_count": row["request_count"],
            }
            for row in rows
        ]
    }
