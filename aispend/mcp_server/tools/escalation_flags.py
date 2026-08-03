"""Heuristic 'this probably should have used a pricier model to start with' report.

Advisory only. Rule: if a request on model A is followed, on the same
source_tool within ESCALATION_WINDOW_SECONDS, by a request on a pricier model
B, that looks like a retry after A wasn't good enough. Flags the *cheap*
request, since that's the one that should have gone straight to B.

Unlike the token-count flags in efficiency_flags.py, this is a real outcome
signal rather than a guess. But it's still a proxy: the schema has no
session/user id, so pairing is inferred purely from time and source_tool —
two unrelated callers sharing the same source_tool at the same moment can
produce a false positive.
"""

from __future__ import annotations

from datetime import datetime

from aispend.proxy.pricing import PRICING_PER_MTOK
from aispend.storage import db

ESCALATION_WINDOW_SECONDS = 120


def get_escalation_flags(
    since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    flags = []
    with db.get_connection() as conn:
        pairs = db.get_adjacent_request_pairs(
            conn, max_gap_seconds=ESCALATION_WINDOW_SECONDS, since=since, until=until
        )
        for pair in pairs:
            rates = PRICING_PER_MTOK.get(pair["model"])
            next_rates = PRICING_PER_MTOK.get(pair["next_model"])
            if rates is None or next_rates is None or next_rates[0] <= rates[0]:
                continue

            gap_seconds = (pair["next_created_at"] - pair["created_at"]).total_seconds()
            flags.append(
                {
                    "id": pair["id"],
                    "model": pair["model"],
                    "escalated_to_id": pair["next_id"],
                    "escalated_to_model": pair["next_model"],
                    "gap_seconds": gap_seconds,
                    "reason": (
                        f"{pair['model']} was followed {gap_seconds:.0f}s later by "
                        f"{pair['next_model']} on the same tool — {pair['model']} "
                        "may not have been sufficient for this task."
                    ),
                }
            )

    return flags
