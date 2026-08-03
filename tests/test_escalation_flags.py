import os

import pytest

from aispend.mcp_server.tools.escalation_flags import get_escalation_flags
from aispend.storage import db

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aispend:aispend@localhost:5555/aispend")


@pytest.fixture
def conn():
    # autocommit: get_escalation_flags() opens its own pooled connection, so
    # writes made here must commit immediately to be visible to it.
    with db.get_connection(DATABASE_URL) as connection:
        connection.autocommit = True
        db.init_schema(connection)
        connection.execute("TRUNCATE requests RESTART IDENTITY")
        yield connection
        connection.execute("TRUNCATE requests RESTART IDENTITY")


def _insert(conn, **overrides):
    defaults = dict(
        model="claude-haiku-4-5",
        input_tokens=50,
        output_tokens=20,
        cost_usd=0.01,
        latency_ms=100,
        source_tool="cli",
    )
    defaults.update(overrides)
    db.insert_request(conn, **defaults)


def test_flags_haiku_followed_quickly_by_opus_on_same_tool(conn):
    _insert(conn, model="claude-haiku-4-5")
    _insert(conn, model="claude-opus-5")

    flags = get_escalation_flags()

    assert len(flags) == 1
    assert flags[0]["model"] == "claude-haiku-4-5"
    assert flags[0]["escalated_to_model"] == "claude-opus-5"


def test_does_not_flag_same_tier_followed_by_same_tier(conn):
    _insert(conn, model="claude-sonnet-5")
    _insert(conn, model="claude-sonnet-5")

    flags = get_escalation_flags()

    assert flags == []


def test_does_not_flag_downgrade(conn):
    _insert(conn, model="claude-opus-5")
    _insert(conn, model="claude-haiku-4-5")

    flags = get_escalation_flags()

    assert flags == []


def test_does_not_flag_escalation_on_different_tool(conn):
    _insert(conn, model="claude-haiku-4-5", source_tool="cli")
    _insert(conn, model="claude-opus-5", source_tool="ide")

    flags = get_escalation_flags()

    assert flags == []


def test_does_not_flag_escalation_outside_window(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO requests
                (model, input_tokens, output_tokens, cost_usd, latency_ms, source_tool, created_at)
            VALUES
                ('claude-haiku-4-5', 50, 20, 0.01, 100, 'cli', now() - interval '10 minutes'),
                ('claude-opus-5', 50, 20, 0.01, 100, 'cli', now())
            """
        )

    flags = get_escalation_flags()

    assert flags == []
