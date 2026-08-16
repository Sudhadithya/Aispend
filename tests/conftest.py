"""Shared fixtures, and the guard that stops a test run from wiping real data.

Every test module here runs `TRUNCATE requests RESTART IDENTITY`, so whichever
database the suite is pointed at gets emptied. The modules read `DATABASE_URL`
and default it to the *dev* database, which means the destructive path was the
default one — running `pytest` with no environment set at all deleted whatever
was in the developer's own table. That is not hypothetical; it happened.

So the suite no longer trusts `DATABASE_URL`. This module rewrites it, before
any test module is imported, to a sibling database with `_test` appended to the
name — `aispend` becomes `aispend_test` — creating it on first use. Everything
else about the connection is preserved, so this needs no configuration locally
or in CI, and pointing `DATABASE_URL` at something precious is no longer enough
to destroy it.

Set `AISPEND_TEST_DATABASE_URL` to override the target explicitly. It still has
to name a database ending in `_test`.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

from aispend.storage import db

DEFAULT_DATABASE_URL = "postgresql://aispend:aispend@localhost:5555/aispend"

# A database whose name ends in this is assumed disposable. The suffix is the
# entire safety contract, so it is checked rather than assumed.
_TEST_SUFFIX = "_test"


def _with_database(url: str, name: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{name}"))


def _database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _derive_test_url(url: str) -> str:
    name = _database_name(url)
    if name.endswith(_TEST_SUFFIX):
        return url
    return _with_database(url, f"{name}{_TEST_SUFFIX}")


def _ensure_database_exists(url: str) -> None:
    """Creates the test database if it isn't there yet.

    Connects to the `postgres` maintenance database on the same server, since
    you cannot create a database from inside the one being created. CREATE
    DATABASE has no IF NOT EXISTS, so existence is checked first.
    """
    name = _database_name(url)
    with psycopg.connect(_with_database(url, "postgres"), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            # The name is derived from a URL, so it is quoted as an identifier
            # rather than interpolated raw.
            conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))


def _resolve_test_database_url() -> str:
    explicit = os.environ.get("AISPEND_TEST_DATABASE_URL")
    url = explicit or _derive_test_url(os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL)

    name = _database_name(url)
    if not name.endswith(_TEST_SUFFIX):
        raise pytest.UsageError(
            f"Refusing to run: the test suite truncates its database, and "
            f"{name!r} is not named like a test database (expected a name "
            f"ending in {_TEST_SUFFIX!r}). Unset AISPEND_TEST_DATABASE_URL to "
            f"let one be derived from DATABASE_URL automatically."
        )
    return url


# Runs at import, which pytest does before collecting any test module — so the
# module-level `DATABASE_URL` constants they read pick this up rather than the
# developer's own database.
TEST_DATABASE_URL = _resolve_test_database_url()
_ensure_database_exists(TEST_DATABASE_URL)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
def _close_pool_at_session_end():
    yield
    db.close_pool()
