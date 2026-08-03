"""Eyeball get_efficiency_flags / get_escalation_flags against a live DB.

Not a pytest suite — nothing here asserts. It inserts a handful of rows
covering every branch in efficiency_flags.py and escalation_flags.py, then
prints what the two MCP tools actually return, so the reasoning strings and
thresholds can be checked by reading, not just by a green pytest run.

Scoped to its own time window, so it neither touches nor needs to touch any
pre-existing rows in the table.

Run with the project's .venv:
    DATABASE_URL=postgresql://aispend:aispend@localhost:5555/aispend \
        .venv/Scripts/python.exe tests/manual_check.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from aispend.mcp_server.tools.efficiency_flags import get_efficiency_flags
from aispend.mcp_server.tools.escalation_flags import get_escalation_flags
from aispend.storage import db

with db.get_connection() as conn:
    conn.autocommit = True
    db.init_schema(conn)

    window_start = datetime.now(UTC)

    # Efficiency-flag scenarios
    db.insert_request(  # tiny opus -> should suggest Haiku
        conn, model="claude-opus-5", input_tokens=10, output_tokens=10,
        cost_usd=0.001, latency_ms=800, source_tool=None,
    )
    db.insert_request(  # moderately small opus -> should suggest Sonnet
        conn, model="claude-opus-5", input_tokens=200, output_tokens=100,
        cost_usd=0.003, latency_ms=1200, source_tool=None,
    )
    db.insert_request(  # large opus -> not flagged
        conn, model="claude-opus-5", input_tokens=2000, output_tokens=1000,
        cost_usd=0.035, latency_ms=4000, source_tool=None,
    )
    db.insert_request(  # small but slow opus -> excluded, not flagged
        conn, model="claude-opus-5", input_tokens=50, output_tokens=20,
        cost_usd=0.0009, latency_ms=15_000, source_tool=None,
    )
    db.insert_request(  # small sonnet -> should suggest Haiku
        conn, model="claude-sonnet-5", input_tokens=50, output_tokens=20,
        cost_usd=0.0005, latency_ms=900, source_tool=None,
    )

    # Escalation scenario: haiku immediately followed by opus, same tool
    db.insert_request(
        conn, model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.0004, latency_ms=600, source_tool="manual-test",
    )
    db.insert_request(
        conn, model="claude-opus-5", input_tokens=100, output_tokens=300,
        cost_usd=0.008, latency_ms=3000, source_tool="manual-test",
    )

    # Non-escalation: haiku followed by opus outside the window
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO requests
                (model, input_tokens, output_tokens, cost_usd, latency_ms,
                 source_tool, created_at)
            VALUES
                ('claude-haiku-4-5', 80, 40, 0.0003, 500, 'manual-test-2',
                 now() - interval '10 minutes'),
                ('claude-opus-5', 80, 200, 0.006, 2500, 'manual-test-2', now())
            """
        )

    window_end = datetime.now(UTC) + timedelta(seconds=5)

print("=== get_efficiency_flags ===")
for flag in get_efficiency_flags(since=window_start, until=window_end):
    print(json.dumps(flag, indent=2))

print("\n=== get_escalation_flags ===")
for flag in get_escalation_flags(since=window_start, until=window_end):
    print(json.dumps(flag, indent=2))

db.close_pool()
