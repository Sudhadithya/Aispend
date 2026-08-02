"""Extracts model/token usage from an Anthropic API response and writes a spend row.

Only ever reads `model` and `usage` fields — never touches `content`, so prompt
and response text never reach this module, let alone the database.

Note on `input_tokens`: the API reports it as the *uncached remainder* only.
The cached portions of a prompt arrive as separate counters and bill at their
own rates, so all four counters have to be carried through to the cost
calculation. Summing them into one number would both over-bill the cached
tokens and lose the ability to show a cache-hit rate later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg

from aispend.proxy.pricing import calculate_cost
from aispend.storage.db import insert_request


@dataclass(frozen=True)
class Usage:
    """Token counts for a single request, as reported by the API."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0


def parse_usage(body: bytes, *, streaming: bool) -> Usage:
    if streaming:
        return _parse_streaming_usage(body)
    return _parse_non_streaming_usage(body)


def _usage_from(model: str, usage: dict) -> Usage:
    """Builds a Usage from an API `usage` object.

    `cache_creation` breaks cache writes down by TTL, which matters because the
    5-minute and 1-hour rates differ (1.25x vs 2x base input). It is absent
    unless the 1-hour TTL is in play, so when it's missing the whole write
    total is attributed to the 5-minute rate — that being the default TTL.
    """
    breakdown = usage.get("cache_creation") or {}
    if breakdown:
        write_5m = breakdown.get("ephemeral_5m_input_tokens", 0)
        write_1h = breakdown.get("ephemeral_1h_input_tokens", 0)
    else:
        write_5m = usage.get("cache_creation_input_tokens", 0)
        write_1h = 0

    return Usage(
        model=model,
        input_tokens=usage["input_tokens"],
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def _parse_non_streaming_usage(body: bytes) -> Usage:
    payload = json.loads(body)
    return _usage_from(payload["model"], payload["usage"])


def _parse_streaming_usage(body: bytes) -> Usage:
    """Reassembles usage from an SSE body.

    `message_start` carries the model and the input/cache counters;
    `message_delta` carries the running output token count and, depending on
    API version, repeats the input counters. Later events therefore merge over
    earlier ones rather than replacing them wholesale.
    """
    model: str | None = None
    usage: dict = {}

    for raw_line in body.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            # Non-JSON keepalive/sentinel payloads aren't usage-bearing.
            continue
        if not isinstance(data, dict):
            continue

        if data.get("type") == "message_start":
            message = data["message"]
            model = message["model"]
            usage.update(message.get("usage", {}))
        elif data.get("type") == "message_delta":
            usage.update(data.get("usage", {}))

    if model is None:
        raise ValueError("No message_start event found in streaming response body")

    return _usage_from(model, usage)


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
        usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_5m_tokens=usage.cache_write_5m_tokens,
        cache_write_1h_tokens=usage.cache_write_1h_tokens,
    )
    insert_request(
        conn,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_5m_tokens=usage.cache_write_5m_tokens,
        cache_write_1h_tokens=usage.cache_write_1h_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        source_tool=source_tool,
    )
