"""A canned Anthropic-shaped upstream, so proxy overhead can be measured without API spend.

Stands in for api.anthropic.com. It does as little work per request as it
possibly can — both response bodies are built once at import — so that the
difference between "straight to this stub" and "through the proxy to this
stub" is proxy overhead rather than stub jitter.

The bodies carry real `model` and `usage` fields, because the point of the
measurement is to time the code path that parses and prices them.

Run with the project's .venv:
    .venv/Scripts/python.exe -m uvicorn bench.stub_upstream:app --port 8788
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

MODEL = "claude-sonnet-5"

# Token counts in the range a real Claude Code turn produces, including the
# cache counters — those are what make pricing more than one multiply.
_USAGE = {
    "input_tokens": 412,
    "output_tokens": 260,
    "cache_read_input_tokens": 18_004,
    "cache_creation_input_tokens": 1_120,
}

_JSON_BODY = json.dumps(
    {
        "id": "msg_bench",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": _USAGE,
    }
).encode()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# Usage arrives split across message_start and message_delta, which is the
# shape capture.py has to reassemble.
_SSE_BODY = "".join(
    [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_bench",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "usage": {k: v for k, v in _USAGE.items() if k != "output_tokens"},
                },
            },
        ),
        _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
        *(
            _sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "token "}},
            )
            for _ in range(16)
        ),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": _USAGE["output_tokens"]}},
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]
).encode()


async def _messages(request: Request) -> Response:
    body = await request.body()
    streaming = b'"stream": true' in body or b'"stream":true' in body
    if streaming:
        return Response(_SSE_BODY, media_type="text/event-stream")
    return Response(_JSON_BODY, media_type="application/json")


app = Starlette(routes=[Route("/v1/messages", _messages, methods=["POST"])])
