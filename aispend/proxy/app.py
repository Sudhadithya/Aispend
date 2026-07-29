"""Starlette app: forwards requests to api.anthropic.com, streams SSE through unmodified.

Pure passthrough — never mutates the request or response Anthropic sees. Spend
capture is best-effort: if it fails (e.g. Postgres is down), the response to
the client still succeeds untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from aispend.proxy.capture import capture_request
from aispend.storage.db import get_connection

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_HOP_BY_HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}

_client = httpx.AsyncClient(timeout=None)


def _filter_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


def _capture_sync(*, body: bytes, streaming: bool, latency_ms: int) -> None:
    with get_connection() as conn:
        capture_request(
            conn, body=body, streaming=streaming, latency_ms=latency_ms, source_tool=None
        )


async def _capture_safely(*, body: bytes, streaming: bool, latency_ms: int) -> None:
    try:
        await asyncio.to_thread(
            _capture_sync, body=body, streaming=streaming, latency_ms=latency_ms
        )
    except Exception:
        logger.exception("Failed to capture spend for request (streaming=%s)", streaming)


async def _proxy(request: Request) -> Response:
    url = f"{ANTHROPIC_BASE_URL}{request.url.path}"
    body = await request.body()
    upstream_request = _client.build_request(
        request.method,
        url,
        params=request.url.query,
        headers=_filter_headers(request.headers),
        content=body,
    )

    start = time.monotonic()
    upstream = await _client.send(upstream_request, stream=True)
    content_type = upstream.headers.get("content-type", "")
    response_headers = _filter_headers(upstream.headers)

    if "text/event-stream" in content_type:
        return StreamingResponse(
            _stream_and_capture(upstream, start),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=content_type,
        )

    response_body = await upstream.aread()
    await upstream.aclose()
    latency_ms = int((time.monotonic() - start) * 1000)
    if upstream.status_code == 200:
        await _capture_safely(body=response_body, streaming=False, latency_ms=latency_ms)
    return Response(
        content=response_body,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type,
    )


async def _stream_and_capture(upstream: httpx.Response, start: float):
    chunks: list[bytes] = []
    async for chunk in upstream.aiter_bytes():
        chunks.append(chunk)
        yield chunk
    await upstream.aclose()
    latency_ms = int((time.monotonic() - start) * 1000)
    if upstream.status_code == 200:
        await _capture_safely(body=b"".join(chunks), streaming=True, latency_ms=latency_ms)


app = Starlette(
    routes=[
        Route(
            "/{path:path}",
            _proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        )
    ]
)
