import os

import pytest

from aispend.mcp_server.tools.efficiency_flags import get_efficiency_flags
from aispend.storage import db

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aispend:aispend@localhost:5555/aispend")


@pytest.fixture
def conn():
    # autocommit: get_efficiency_flags() opens its own pooled connection, so
    # writes made here must commit immediately to be visible to it.
    with db.get_connection(DATABASE_URL) as connection:
        connection.autocommit = True
        db.init_schema(connection)
        connection.execute("TRUNCATE requests RESTART IDENTITY")
        yield connection
        connection.execute("TRUNCATE requests RESTART IDENTITY")


def _insert(conn, **overrides):
    defaults = dict(
        model="claude-opus-4-20250514",
        input_tokens=50,
        output_tokens=20,
        cost_usd=0.01,
        latency_ms=100,
        source_tool=None,
    )
    defaults.update(overrides)
    db.insert_request(conn, **defaults)


def test_flags_opus_with_small_prompt(conn):
    _insert(conn, model="claude-opus-4-20250514", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert len(flags) == 1
    assert flags[0]["model"] == "claude-opus-4-20250514"
    assert flags[0]["total_tokens"] == 70


def test_does_not_flag_opus_with_large_prompt(conn):
    _insert(conn, model="claude-opus-4-20250514", input_tokens=2000, output_tokens=1000)

    flags = get_efficiency_flags()

    assert flags == []


def test_does_not_flag_non_opus_small_prompt(conn):
    _insert(conn, model="claude-sonnet-4-5-20250929", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert flags == []
