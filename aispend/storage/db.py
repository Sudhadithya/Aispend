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

_GROUP_BY_COLUMNS = {
    "model": "model",
    "source_tool": "source_tool",
    "day": "date_trunc('day', created_at)",
}

_REQUEST_COLUMNS = (
    "id, model, input_tokens, output_tokens, cost_usd, latency_ms, source_tool, created_at"
)


def get_pool(database_url: str | None = None) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(database_url or os.environ["DATABASE_URL"], open=True)
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
) -> None:
    """Metadata only — never pass prompt/response content into this function."""
    conn.execute(
        """
        INSERT INTO requests
            (model, input_tokens, output_tokens, cost_usd, latency_ms, source_tool, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        """,
        (model, input_tokens, output_tokens, cost_usd, latency_ms, source_tool),
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


def get_all_requests(
    conn: psycopg.Connection,
    *,
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
            ORDER BY created_at
            """,
            (since, since, until, until),
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
