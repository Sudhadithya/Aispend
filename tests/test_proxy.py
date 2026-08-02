"""Proxy-layer tests.

Upstream is an httpx.MockTransport, so nothing here touches the network, and
capture is stubbed out so nothing touches Postgres either.
"""

import gzip
import json

import httpx
import pytest
from starlette.testclient import TestClient

from aispend.proxy import app as proxy_app

MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "hi"}],
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


@pytest.fixture
def captured(monkeypatch):
    """Records what would have been captured, without writing to the database."""
    calls = []

    async def _fake_capture(*, body, streaming, latency_ms):
        calls.append({"body": body, "streaming": streaming, "latency_ms": latency_ms})

    monkeypatch.setattr(proxy_app, "_capture_safely", _fake_capture)
    return calls


def _client(handler, monkeypatch):
    monkeypatch.setattr(
        proxy_app,
        "_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None),
    )
    return TestClient(proxy_app.app)


def test_gzipped_response_is_not_labelled_gzip_after_httpx_decodes_it(captured, monkeypatch):
    """httpx decompresses the body, so relaying content-encoding would break the client."""
    payload = json.dumps(MESSAGE).encode()

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(payload),
        )

    response = _client(handler, monkeypatch).post("/v1/messages", json={"model": "claude-opus-5"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.json() == MESSAGE


def test_non_streaming_response_is_captured(captured, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=MESSAGE)

    _client(handler, monkeypatch).post("/v1/messages", json={})

    assert len(captured) == 1
    assert captured[0]["streaming"] is False
    assert json.loads(captured[0]["body"]) == MESSAGE


def test_streaming_response_passes_through_and_is_captured(captured, monkeypatch):
    sse = (
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"model":"claude-opus-5","usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    response = _client(handler, monkeypatch).post("/v1/messages", json={"stream": True})

    assert response.content == sse
    assert len(captured) == 1
    assert captured[0]["streaming"] is True
    assert captured[0]["body"] == sse


def test_error_responses_are_not_captured(captured, monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error"}})

    response = _client(handler, monkeypatch).post("/v1/messages", json={})

    assert response.status_code == 429
    assert captured == []


def test_upstream_receives_anthropic_host_not_the_proxys(captured, monkeypatch):
    seen = {}

    def handler(request):
        seen["host"] = request.headers.get("host")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=MESSAGE)

    _client(handler, monkeypatch).post("/v1/messages?beta=true", json={})

    assert seen["host"] == "api.anthropic.com"
    assert seen["url"] == "https://api.anthropic.com/v1/messages?beta=true"


def test_unreachable_upstream_returns_502(captured, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    response = _client(handler, monkeypatch).post("/v1/messages", json={})

    assert response.status_code == 502
    assert captured == []
