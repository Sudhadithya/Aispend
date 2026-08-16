"""Starlette app: forwards requests to api.anthropic.com, streams SSE through unmodified.

Pure passthrough — never mutates the request or response Anthropic sees. Spend
capture is best-effort: if it fails (e.g. Postgres is down), the response to
the client still succeeds untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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

# Overridable so the proxy can be pointed at a stub upstream for benchmarking
# (see bench/) without touching code. Defaults to the real API.
ANTHROPIC_BASE_URL = os.environ.get("AISPEND_UPSTREAM_URL", "https://api.anthropic.com")

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

# httpx's own defaults, made explicit and tunable rather than changed. Raising
# keepalive to match max_connections looks like the obvious win for a proxy —
# fewer reconnects above 20 concurrent — and it was measured as a loss: neutral
# at concurrency 1, and 1.10-1.28x *slower* at 16 and 50, where holding many
# idle sockets costs more than recycling them. Left at 20 on the evidence.
# See docs/BENCHMARK.md §5.
UPSTREAM_MAX_CONNECTIONS = int(os.environ.get("AISPEND_UPSTREAM_MAX_CONNECTIONS", "100"))
UPSTREAM_MAX_KEEPALIVE = int(os.environ.get("AISPEND_UPSTREAM_MAX_KEEPALIVE", "20"))

_client = httpx.AsyncClient(
    timeout=None,
    limits=httpx.Limits(
        max_connections=UPSTREAM_MAX_CONNECTIONS,
        max_keepalive_connections=UPSTREAM_MAX_KEEPALIVE,
    ),
)

# In-flight capture tasks, held so they cannot be garbage-collected before they
# finish, and so shutdown can wait for them. See _capture_off_path.
_pending_captures: set[asyncio.Task] = set()

# Awaits capture before responding, the way this used to work. Kept as a flag
# purely so bench/ can run both behaviours side by side in one measurement —
# this machine drifts more between runs than the difference being measured.
INLINE_CAPTURE = os.environ.get("AISPEND_INLINE_CAPTURE") == "1"


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


def _capture_off_path(*, body: bytes, streaming: bool, latency_ms: int) -> None:
    """Schedules capture without holding the response back for it.

    Capture costs a Postgres commit, and awaiting it inline puts that commit in
    the critical path of every request — measurably so: it was roughly half the
    latency the proxy added (see bench/). Scheduling it instead means the
    client's response goes out as soon as the upstream body is in hand.

    The task is parked in a module-level set because asyncio holds only a weak
    reference to a running task; without a strong one the interpreter is free
    to garbage-collect a capture mid-flight and the row silently never lands.
    """
    task = asyncio.create_task(
        _capture_safely(body=body, streaming=streaming, latency_ms=latency_ms)
    )
    _pending_captures.add(task)
    task.add_done_callback(_pending_captures.discard)


# Failures where the request provably never reached Anthropic, so replaying it
# cannot double-send — and therefore cannot double-charge. A pooled keep-alive
# connection reaped by the far end lands here, which is the common case now
# that connections are held open for longer. Anything that fails *after* a
# response has begun is deliberately not in this set.
_RETRIABLE_SEND_ERRORS = (httpx.ConnectError, httpx.RemoteProtocolError)


async def _proxy(request: Request) -> Response:
    url = f"{ANTHROPIC_BASE_URL}{request.url.path}"
    body = await request.body()

    def build() -> httpx.Request:
        return _client.build_request(
            request.method,
            url,
            params=request.url.query,
            headers=_filter_headers(request.headers, drop=_STRIP_FROM_REQUEST),
            content=body,
        )

    start = time.monotonic()
    try:
        try:
            upstream = await _client.send(build(), stream=True)
        except _RETRIABLE_SEND_ERRORS as exc:
            # One retry on a fresh connection. Turning a dead pooled socket into
            # a client-visible 502 makes the proxy less reliable than the API it
            # fronts, which defeats the point of being transparent.
            logger.info("Retrying upstream request to %s after %r", url, exc)
            upstream = await _client.send(build(), stream=True)
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
        if INLINE_CAPTURE:
            await _capture_safely(body=response_body, streaming=False, latency_ms=latency_ms)
        else:
            _capture_off_path(body=response_body, streaming=False, latency_ms=latency_ms)
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
            body = b"".join(chunks)
            if INLINE_CAPTURE:
                await _capture_safely(body=body, streaming=True, latency_ms=latency_ms)
            else:
                _capture_off_path(body=body, streaming=True, latency_ms=latency_ms)
    finally:
        await upstream.aclose()


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    yield
    # Captures scheduled off the request path may still be in flight; without
    # this, shutting down mid-request drops their rows and closes the pool out
    # from under them.
    if _pending_captures:
        await asyncio.gather(*_pending_captures, return_exceptions=True)
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
