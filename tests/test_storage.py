import os
from datetime import UTC, datetime, timedelta

import pytest

from aispend.storage import db

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aispend:aispend@localhost:5555/aispend")


@pytest.fixture
def conn():
    with db.get_connection(DATABASE_URL) as connection:
        db.init_schema(connection)
        connection.execute("TRUNCATE requests RESTART IDENTITY")
        yield connection
        connection.execute("TRUNCATE requests RESTART IDENTITY")


def _insert_sample(conn, **overrides):
    defaults = dict(
        model="claude-opus-4",
        input_tokens=100,
        output_tokens=50,
        cost_usd=1.23,
        latency_ms=500,
        source_tool="claude-code",
    )
    defaults.update(overrides)
    db.insert_request(conn, **defaults)


def test_insert_and_summary_total(conn):
    _insert_sample(conn, cost_usd=1.0)
    _insert_sample(conn, cost_usd=2.5)

    since = datetime.now(UTC) - timedelta(days=1)
    until = datetime.now(UTC) + timedelta(days=1)
    [summary] = db.get_spend_summary(conn, since=since, until=until)

    assert summary["request_count"] == 2
    assert float(summary["total_cost_usd"]) == pytest.approx(3.5)


def test_summary_group_by_model(conn):
    _insert_sample(conn, model="claude-opus-4", cost_usd=2.0)
    _insert_sample(conn, model="claude-haiku-4", cost_usd=0.1)

    since = datetime.now(UTC) - timedelta(days=1)
    until = datetime.now(UTC) + timedelta(days=1)
    rows = db.get_spend_summary(conn, since=since, until=until, group_by="model")

    by_model = {row["group_value"]: float(row["total_cost_usd"]) for row in rows}
    assert by_model["claude-opus-4"] == pytest.approx(2.0)
    assert by_model["claude-haiku-4"] == pytest.approx(0.1)


def test_summary_invalid_group_by_raises(conn):
    since = datetime.now(UTC) - timedelta(days=1)
    until = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(ValueError):
        db.get_spend_summary(conn, since=since, until=until, group_by="; DROP TABLE requests;--")


def test_get_expensive_requests_ordering(conn):
    _insert_sample(conn, cost_usd=1.0)
    _insert_sample(conn, cost_usd=5.0)
    _insert_sample(conn, cost_usd=3.0)

    rows = db.get_expensive_requests(conn, limit=2)

    assert [float(r["cost_usd"]) for r in rows] == [5.0, 3.0]


def test_get_total_spend(conn):
    _insert_sample(conn, cost_usd=1.5)
    _insert_sample(conn, cost_usd=2.5)

    total = db.get_total_spend(conn)

    assert total == pytest.approx(4.0)


def test_cache_token_columns_round_trip(conn):
    _insert_sample(conn, cache_read_tokens=1234, cache_write_5m_tokens=56, cache_write_1h_tokens=78)

    [row] = db.get_expensive_requests(conn, limit=1)

    assert row["cache_read_tokens"] == 1234
    assert row["cache_write_5m_tokens"] == 56
    assert row["cache_write_1h_tokens"] == 78


def test_cache_token_columns_default_to_zero(conn):
    _insert_sample(conn)

    [row] = db.get_expensive_requests(conn, limit=1)

    assert (row["cache_read_tokens"], row["cache_write_5m_tokens"]) == (0, 0)


def test_summary_group_by_day(conn):
    _insert_sample(conn, cost_usd=1.0)
    _insert_sample(conn, cost_usd=2.0)

    since = datetime.now(UTC) - timedelta(days=1)
    until = datetime.now(UTC) + timedelta(days=1)
    rows = db.get_spend_summary(conn, since=since, until=until, group_by="day")

    assert len(rows) == 1
    assert float(rows[0]["total_cost_usd"]) == pytest.approx(3.0)
