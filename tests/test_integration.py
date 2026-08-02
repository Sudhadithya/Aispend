"""End-to-end: a request through the proxy lands as a correctly priced row.

This is the path that silently broke when the price table went stale — the
proxy kept serving traffic while capture failed and wrote nothing. Upstream is
mocked; Postgres is real.
"""

import json
import os

import httpx
import pytest
from starlette.testclient import TestClient

from aispend.proxy import app as proxy_app
from aispend.storage import db

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aispend:aispend@localhost:5555/aispend")


@pytest.fixture
def conn():
    # The proxy's capture path opens its own pooled connection, so writes must
    # be committed to be visible here — hence autocommit on the read side too.
    with db.get_connection(DATABASE_URL) as connection:
        connection.autocommit = True
        db.init_schema(connection)
        connection.execute("TRUNCATE requests RESTART IDENTITY")
        yield connection
        connection.execute("TRUNCATE requests RESTART IDENTITY")


def _proxy_client(handler, monkeypatch):
    monkeypatch.setattr(
        proxy_app,
        "_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None),
    )
    return TestClient(proxy_app.app)


def test_request_through_proxy_is_stored_with_cache_aware_cost(conn, monkeypatch):
    body = {
        "id": "msg_1",
        "type": "message",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": 1_000,
            "output_tokens": 500,
            "cache_read_input_tokens": 200_000,
            "cache_creation_input_tokens": 10_000,
        },
    }

    def handler(request):
        return httpx.Response(200, json=body)

    response = _proxy_client(handler, monkeypatch).post("/v1/messages", json={})
    assert response.json() == body

    row = conn.execute(
        "SELECT model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_5m_tokens, cost_usd FROM requests"
    ).fetchone()

    model, input_tokens, output_tokens, cache_read, cache_write, cost = row
    assert (model, input_tokens, output_tokens) == ("claude-opus-5", 1_000, 500)
    assert (cache_read, cache_write) == (200_000, 10_000)

    expected = (
        1_000 * 5.0  # uncached input
        + 500 * 25.0  # output
        + 200_000 * 5.0 * 0.1  # cache reads at 0.1x
        + 10_000 * 5.0 * 1.25  # 5-minute cache writes at 1.25x
    ) / 1_000_000
    assert float(cost) == pytest.approx(expected, abs=1e-6)


def test_unpriceable_model_is_dropped_without_breaking_the_response(conn, monkeypatch, caplog):
    """Capture failing must never degrade what the client gets back."""
    body = {
        "id": "msg_1",
        "type": "message",
        "model": "claude-from-the-future-9",
        "content": [],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    def handler(request):
        return httpx.Response(200, json=body)

    response = _proxy_client(handler, monkeypatch).post("/v1/messages", json={})

    assert response.status_code == 200
    assert response.json() == body
    assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert "claude-from-the-future-9" in caplog.text


def test_streaming_request_through_proxy_is_stored(conn, monkeypatch):
    start = {
        "type": "message_start",
        "message": {
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": 80, "output_tokens": 1, "cache_read_input_tokens": 4_000},
        },
    }
    sse = (
        f"event: message_start\ndata: {json.dumps(start)}\n\n"
        'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":120}}\n\n'
    ).encode()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    _proxy_client(handler, monkeypatch).post("/v1/messages", json={"stream": True})

    row = conn.execute(
        "SELECT model, input_tokens, output_tokens, cache_read_tokens FROM requests"
    ).fetchone()
    assert row == ("claude-sonnet-5", 80, 120, 4_000)
