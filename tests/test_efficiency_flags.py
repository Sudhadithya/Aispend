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


def test_flags_sonnet_with_small_prompt_suggesting_haiku(conn):
    _insert(conn, model="claude-sonnet-5", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert len(flags) == 1
    assert flags[0]["model"] == "claude-sonnet-5"
    assert "Haiku" in flags[0]["reason"]


def test_does_not_flag_haiku_small_prompt(conn):
    _insert(conn, model="claude-haiku-4-5", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert flags == []


def test_does_not_flag_sonnet_with_large_prompt(conn):
    _insert(conn, model="claude-sonnet-5", input_tokens=2000, output_tokens=1000)

    flags = get_efficiency_flags()

    assert flags == []


def test_flags_opus_and_sonnet_together_ordered_by_time(conn):
    _insert(conn, model="claude-sonnet-5", input_tokens=50, output_tokens=20)
    _insert(conn, model="claude-opus-4-20250514", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert [f["model"] for f in flags] == ["claude-sonnet-5", "claude-opus-4-20250514"]


def test_flags_fable_with_small_prompt_suggesting_opus(conn):
    _insert(conn, model="claude-fable-5", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert len(flags) == 1
    assert flags[0]["model"] == "claude-fable-5"
    assert "Opus" in flags[0]["reason"]


def test_flags_mythos_with_small_prompt_suggesting_opus(conn):
    _insert(conn, model="claude-mythos-5", input_tokens=50, output_tokens=20)

    flags = get_efficiency_flags()

    assert len(flags) == 1
    assert flags[0]["model"] == "claude-mythos-5"
    assert "Opus" in flags[0]["reason"]


def test_does_not_flag_small_prompt_against_large_cached_context_sonnet(conn):
    """A short question over 50k of cached context is not a small request."""
    _insert(
        conn,
        model="claude-sonnet-5",
        input_tokens=20,
        output_tokens=30,
        cache_read_tokens=50_000,
    )

    flags = get_efficiency_flags()

    assert flags == []


def test_flags_current_generation_opus(conn):
    _insert(conn, model="claude-opus-5", input_tokens=40, output_tokens=10)

    flags = get_efficiency_flags()

    assert [f["model"] for f in flags] == ["claude-opus-5"]


def test_does_not_flag_small_prompt_against_large_cached_context(conn):
    """A short question over 50k of cached context is not a small request."""
    _insert(
        conn,
        model="claude-opus-5",
        input_tokens=20,
        output_tokens=30,
        cache_read_tokens=50_000,
    )

    flags = get_efficiency_flags()

    assert flags == []


def test_total_tokens_counts_cached_tokens(conn):
    _insert(
        conn,
        model="claude-opus-5",
        input_tokens=20,
        output_tokens=30,
        cache_read_tokens=100,
        cache_write_5m_tokens=50,
    )

    flags = get_efficiency_flags()

    assert flags[0]["total_tokens"] == 200
