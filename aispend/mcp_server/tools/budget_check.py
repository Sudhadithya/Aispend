from __future__ import annotations

from datetime import datetime

from aispend.storage import db


def check_budget(
    threshold: float, since: datetime | None = None, until: datetime | None = None
) -> dict:
    with db.get_connection() as conn:
        total = db.get_total_spend(conn, since=since, until=until)

    return {
        "total_cost_usd": total,
        "threshold_usd": threshold,
        "over_budget": total > threshold,
        "remaining_usd": threshold - total,
    }
