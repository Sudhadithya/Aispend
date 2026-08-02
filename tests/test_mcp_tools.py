"""Tests for the MCP tool wrappers — the layer Claude Code actually calls.

These are thin functions over db.py, but they own the shaping of the response
(Decimal -> float, datetime -> ISO string) that has to survive JSON
serialisation across the MCP boundary.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

from aispend.mcp_server.tools.budget_check import check_budget
from aispend.mcp_server.tools.expensive_requests import get_expensive_requests
from aispend.mcp_server.tools.spend_summary import get_spend_summary
from aispend.storage import db

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aispend:aispend@localhost:5555/aispend")

SINCE = datetime.now(UTC) - timedelta(days=1)
UNTIL = datetime.now(UTC) + timedelta(days=1)


@pytest.fixture
def conn():
    # The tools open their own pooled connections, so writes here must commit
    # immediately to be visible to them.
    with db.get_connection(DATABASE_URL) as connection:
        connection.autocommit = True
        db.init_schema(connection)
        connection.execute("TRUNCATE requests RESTART IDENTITY")
        yield connection
        connection.execute("TRUNCATE requests RESTART IDENTITY")


def _insert(conn, **overrides):
    defaults = dict(
        model="claude-opus-5",
        input_tokens=100,
        output_tokens=50,
        cost_usd=1.0,
        latency_ms=500,
        source_tool=None,
    )
    defaults.update(overrides)
    db.insert_request(conn, **defaults)


def test_spend_summary_total(conn):
    _insert(conn, cost_usd=1.25)
    _insert(conn, cost_usd=2.50)

    result = get_spend_summary(SINCE, UNTIL)

    assert result == {"total_cost_usd": pytest.approx(3.75), "request_count": 2}


def test_spend_summary_is_json_serialisable(conn):
    """Costs come back from Postgres as Decimal, which json.dumps rejects."""
    _insert(conn, cost_usd=1.25)

    result = get_spend_summary(SINCE, UNTIL)

    assert isinstance(result["total_cost_usd"], float)


def test_spend_summary_empty_window_returns_zero(conn):
    result = get_spend_summary(SINCE, UNTIL)

    assert result == {"total_cost_usd": 0.0, "request_count": 0}


def test_spend_summary_group_by_model(conn):
    _insert(conn, model="claude-opus-5", cost_usd=3.0)
    _insert(conn, model="claude-haiku-4-5", cost_usd=0.5)

    result = get_spend_summary(SINCE, UNTIL, group_by="model")

    by_model = {row["group"]: row["cost_usd"] for row in result["breakdown"]}
    assert by_model == {"claude-opus-5": pytest.approx(3.0), "claude-haiku-4-5": pytest.approx(0.5)}


def test_spend_summary_breakdown_is_ordered_by_cost(conn):
    _insert(conn, model="claude-haiku-4-5", cost_usd=0.5)
    _insert(conn, model="claude-opus-5", cost_usd=3.0)

    result = get_spend_summary(SINCE, UNTIL, group_by="model")

    assert [row["group"] for row in result["breakdown"]] == ["claude-opus-5", "claude-haiku-4-5"]


def test_expensive_requests_ordering_and_limit(conn):
    _insert(conn, cost_usd=1.0)
    _insert(conn, cost_usd=9.0)
    _insert(conn, cost_usd=5.0)

    rows = get_expensive_requests(limit=2)

    assert [row["cost_usd"] for row in rows] == [pytest.approx(9.0), pytest.approx(5.0)]


def test_expensive_requests_serialises_timestamps_and_cache_counts(conn):
    _insert(conn, cache_read_tokens=4321)

    [row] = get_expensive_requests(limit=1)

    assert isinstance(row["created_at"], str)
    assert datetime.fromisoformat(row["created_at"])  # round-trips
    assert row["cache_read_tokens"] == 4321


def test_check_budget_under_threshold(conn):
    _insert(conn, cost_usd=4.0)

    result = check_budget(threshold=10.0)

    assert result["over_budget"] is False
    assert result["total_cost_usd"] == pytest.approx(4.0)
    assert result["remaining_usd"] == pytest.approx(6.0)


def test_check_budget_over_threshold(conn):
    _insert(conn, cost_usd=12.0)

    result = check_budget(threshold=10.0)

    assert result["over_budget"] is True
    assert result["remaining_usd"] == pytest.approx(-2.0)


def test_check_budget_on_empty_table(conn):
    result = check_budget(threshold=10.0)

    assert result == {
        "total_cost_usd": 0.0,
        "threshold_usd": 10.0,
        "over_budget": False,
        "remaining_usd": 10.0,
    }
