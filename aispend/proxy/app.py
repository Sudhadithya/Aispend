"""Starlette app: forwards requests to api.anthropic.com, streams SSE through unmodified.

Pure passthrough — never mutates the request or response Anthropic sees. Spend
capture is best-effort: if it fails (e.g. Postgres is down), the response to
the client still succeeds untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from aispend.proxy.capture import capture_request
from aispend.proxy.pricing import UnknownModelError
from aispend.storage.db import close_pool, get_connection

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Headers that describe a single hop and must not be relayed to the next one.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# `host` would point at the proxy, and `content-length` is recomputed downstream.
_STRIP_FROM_REQUEST = _HOP_BY_HOP_HEADERS | {"host", "content-length"}

# httpx transparently decompresses the upstream body, so by the time bytes
# reach us they are plain text. Relaying the original `content-encoding` would
# tell the client to gunzip data that is no longer gzipped, and it would fail
# to decode the response.
_STRIP_FROM_RESPONSE = _HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}

_client = httpx.AsyncClient(timeout=None)


def _filter_headers(headers, *, drop: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in drop}


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
    except UnknownModelError as exc:
        # Much the most likely capture failure: Anthropic shipped a model that
        # the hardcoded price table doesn't know about yet. Log it as the
        # one-line fix it is, rather than burying it in a stack trace.
        logger.error("Skipped spend capture — %s See aispend/proxy/pricing.py.", exc)
    except Exception:
        logger.exception("Failed to capture spend for request (streaming=%s)", streaming)


async def _proxy(request: Request) -> Response:
    url = f"{ANTHROPIC_BASE_URL}{request.url.path}"
    body = await request.body()
    upstream_request = _client.build_request(
        request.method,
        url,
        params=request.url.query,
        headers=_filter_headers(request.headers, drop=_STRIP_FROM_REQUEST),
        content=body,
    )

    start = time.monotonic()
    try:
        upstream = await _client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        logger.warning("Upstream request to %s failed: %s", url, exc)
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream unreachable: {exc}"},
            },
            status_code=502,
        )

    content_type = upstream.headers.get("content-type", "")
    response_headers = _filter_headers(upstream.headers, drop=_STRIP_FROM_RESPONSE)

    if "text/event-stream" in content_type:
        return StreamingResponse(
            _stream_and_capture(upstream, start),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=content_type,
        )

    try:
        response_body = await upstream.aread()
    finally:
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


async def _stream_and_capture(upstream: httpx.Response, start: float) -> AsyncIterator[bytes]:
    """Relays the SSE stream verbatim, capturing usage once it completes.

    Capture is deliberately inside the try: if the client disconnects partway,
    the buffered body has no final `message_delta` and its token counts would
    understate the request, so the row is skipped rather than written wrong.
    The upstream connection is closed either way.
    """
    chunks: list[bytes] = []
    try:
        async for chunk in upstream.aiter_bytes():
            chunks.append(chunk)
            yield chunk

        latency_ms = int((time.monotonic() - start) * 1000)
        if upstream.status_code == 200:
            await _capture_safely(body=b"".join(chunks), streaming=True, latency_ms=latency_ms)
    finally:
        await upstream.aclose()


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    yield
    await _client.aclose()
    close_pool()


app = Starlette(
    lifespan=_lifespan,
    routes=[
        Route(
            "/{path:path}",
            _proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        )
    ],
)
