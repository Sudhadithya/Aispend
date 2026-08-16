"""Connection pool and query functions for the `requests` table.

Metadata only — never pass prompt/response content into these functions.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None

# psycopg_pool defaults to min_size=4 and max_size=min_size, so the pool is
# four connections wide unless you say otherwise — a ceiling nobody chose.
# Measured, that caps writes at ~780/sec and, at 32 concurrent writers, pushes
# per-write latency from 5ms to 41ms queueing for a slot (see docs/BENCHMARK.md).
# Sized for a local single-user tool; raise POOL_MAX_SIZE before putting this
# in front of a team.
POOL_MIN_SIZE = int(os.environ.get("AISPEND_POOL_MIN_SIZE", "4"))
POOL_MAX_SIZE = int(os.environ.get("AISPEND_POOL_MAX_SIZE", "16"))

_GROUP_BY_COLUMNS = {
    "model": "model",
    "source_tool": "source_tool",
    "day": "date_trunc('day', created_at)",
}

_REQUEST_COLUMNS = (
    "id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_5m_tokens, "
    "cache_write_1h_tokens, cost_usd, latency_ms, source_tool, created_at"
)

# Every token counter that contributes to the billable size of a request.
_TOTAL_TOKENS_SQL = (
    "(input_tokens + output_tokens + cache_read_tokens "
    "+ cache_write_5m_tokens + cache_write_1h_tokens)"
)


def get_pool(database_url: str | None = None) -> ConnectionPool:
    """Returns the process-wide pool, opening it on first use.

    The first call wins: a later call with a different `database_url` gets the
    already-open pool, not a second one.
    """
    global _pool
    if _pool is None:
        url = database_url or os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy .env.example to .env and export it, "
                "or pass database_url explicitly."
            )
        _pool = ConnectionPool(
            url, open=True, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_connection(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    with get_pool(database_url).connection() as conn:
        yield conn


def init_schema(conn: psycopg.Connection) -> None:
    schema_path = Path(__file__).parent / "schema.sql"
    conn.execute(schema_path.read_text())


def insert_request(
    conn: psycopg.Connection,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    source_tool: str | None,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> None:
    """Metadata only — never pass prompt/response content into this function.

    `input_tokens` is the uncached remainder the API reports; the cache
    counters are stored alongside it rather than summed into it.
    """
    conn.execute(
        """
        INSERT INTO requests
            (model, input_tokens, output_tokens, cache_read_tokens, cache_write_5m_tokens,
             cache_write_1h_tokens, cost_usd, latency_ms, source_tool, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        """,
        (
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_5m_tokens,
            cache_write_1h_tokens,
            cost_usd,
            latency_ms,
            source_tool,
        ),
    )


def get_spend_summary(
    conn: psycopg.Connection,
    *,
    since: datetime,
    until: datetime,
    group_by: str | None = None,
) -> list[dict]:
    if group_by is None:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS total_cost_usd, COUNT(*) AS request_count
                FROM requests WHERE created_at >= %s AND created_at < %s
                """,
                (since, until),
            )
            return [cur.fetchone()]

    if group_by not in _GROUP_BY_COLUMNS:
        valid = sorted(_GROUP_BY_COLUMNS)
        raise ValueError(f"Invalid group_by: {group_by!r}. Must be one of {valid}")

    column = _GROUP_BY_COLUMNS[group_by]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {column} AS group_value, SUM(cost_usd) AS total_cost_usd,
                   COUNT(*) AS request_count
            FROM requests WHERE created_at >= %s AND created_at < %s
            GROUP BY {column}
            ORDER BY total_cost_usd DESC
            """,
            (since, until),
        )
        return cur.fetchall()


def get_expensive_requests(
    conn: psycopg.Connection,
    *,
    limit: int,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_REQUEST_COLUMNS}
            FROM requests
            WHERE (%s::timestamptz IS NULL OR created_at >= %s)
              AND (%s::timestamptz IS NULL OR created_at < %s)
            ORDER BY cost_usd DESC
            LIMIT %s
            """,
            (since, since, until, until, limit),
        )
        return cur.fetchall()


def get_small_requests(
    conn: psycopg.Connection,
    *,
    model_prefix: str,
    max_total_tokens: int,
    max_latency_ms: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Requests on models matching `model_prefix` whose total tokens fall below
    `max_total_tokens`.

    The filter runs in SQL rather than in Python so the whole table doesn't
    have to cross the wire to find what is normally a handful of rows.
    `model_prefix` is matched with LIKE, so it must not contain % or _.

    `max_latency_ms`, if given, excludes requests that took at least that
    long: a slow response despite a low token count likely reflects real
    compute (extended thinking, tool calls) that the token count alone
    doesn't capture, not an oversized model on a trivial prompt.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_REQUEST_COLUMNS}, {_TOTAL_TOKENS_SQL} AS total_tokens
            FROM requests
            WHERE model LIKE %s
              AND {_TOTAL_TOKENS_SQL} < %s
              AND (%s::int IS NULL OR latency_ms < %s)
              AND (%s::timestamptz IS NULL OR created_at >= %s)
              AND (%s::timestamptz IS NULL OR created_at < %s)
            ORDER BY created_at
            """,
            (
                f"{model_prefix}%",
                max_total_tokens,
                max_latency_ms,
                max_latency_ms,
                since,
                since,
                until,
                until,
            ),
        )
        return cur.fetchall()


def get_adjacent_request_pairs(
    conn: psycopg.Connection,
    *,
    max_gap_seconds: int,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Consecutive request pairs on the same `source_tool`, ordered by time,
    where the gap between them is at most `max_gap_seconds`.

    There's no session/user id in this schema, so "consecutive on the same
    tool within a short gap" is the closest available proxy for "the same
    task, retried." Whether the second request escalated to a pricier model
    is left to the caller, since per-token price isn't known to the database.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, model, created_at, next_id, next_model, next_created_at
            FROM (
                SELECT
                    id, model, created_at,
                    LEAD(id) OVER w AS next_id,
                    LEAD(model) OVER w AS next_model,
                    LEAD(created_at) OVER w AS next_created_at
                FROM requests
                WHERE (%s::timestamptz IS NULL OR created_at >= %s)
                  AND (%s::timestamptz IS NULL OR created_at < %s)
                WINDOW w AS (PARTITION BY source_tool ORDER BY created_at)
            ) pairs
            WHERE next_id IS NOT NULL
              AND next_created_at - created_at <= make_interval(secs => %s)
            ORDER BY created_at
            """,
            (since, since, until, until, max_gap_seconds),
        )
        return cur.fetchall()


def get_total_spend(
    conn: psycopg.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> float:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM requests
            WHERE (%s::timestamptz IS NULL OR created_at >= %s)
              AND (%s::timestamptz IS NULL OR created_at < %s)
            """,
            (since, since, until, until),
        )
        return float(cur.fetchone()[0])
