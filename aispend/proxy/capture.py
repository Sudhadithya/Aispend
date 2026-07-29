"""Extracts model/token usage from an Anthropic API response and writes a spend row.

Only ever reads `model` and `usage` fields — never touches `content`, so prompt
and response text never reach this module, let alone the database.
"""

from __future__ import annotations

import json

import psycopg

from aispend.proxy.pricing import calculate_cost
from aispend.storage.db import insert_request


def parse_usage(body: bytes, *, streaming: bool) -> dict:
    """Returns {"model": str, "input_tokens": int, "output_tokens": int}."""
    if streaming:
        return _parse_streaming_usage(body)
    return _parse_non_streaming_usage(body)


def _parse_non_streaming_usage(body: bytes) -> dict:
    payload = json.loads(body)
    usage = payload["usage"]
    return {
        "model": payload["model"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
    }


def _parse_streaming_usage(body: bytes) -> dict:
    model: str | None = None
    input_tokens = 0
    output_tokens = 0

    for raw_line in body.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = json.loads(line.removeprefix("data:").strip())
        event_type = data.get("type")

        if event_type == "message_start":
            message = data["message"]
            model = message["model"]
            input_tokens = message["usage"]["input_tokens"]
            output_tokens = message["usage"].get("output_tokens", 0)
        elif event_type == "message_delta":
            output_tokens = data["usage"]["output_tokens"]

    if model is None:
        raise ValueError("No message_start event found in streaming response body")

    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens}


def capture_request(
    conn: psycopg.Connection,
    *,
    body: bytes,
    streaming: bool,
    latency_ms: int,
    source_tool: str | None,
) -> None:
    usage = parse_usage(body, streaming=streaming)
    cost_usd = calculate_cost(
        usage["model"], input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
    )
    insert_request(
        conn,
        model=usage["model"],
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        source_tool=source_tool,
    )
