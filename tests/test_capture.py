import json
from unittest.mock import MagicMock

import pytest

from aispend.proxy.capture import Usage, capture_request, parse_usage

MODEL = "claude-opus-5"


def _non_streaming_body(model=MODEL, input_tokens=100, output_tokens=50, **usage_extra):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    usage.update(usage_extra)
    return json.dumps(
        {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "hi"}],
            "usage": usage,
        }
    ).encode()


def _sse(events):
    lines = []
    for name, data in events:
        lines.append(f"event: {name}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return "\n".join(lines).encode()


def _streaming_body(model=MODEL, input_tokens=100, output_tokens=50, **start_usage_extra):
    start_usage = {"input_tokens": input_tokens, "output_tokens": 1}
    start_usage.update(start_usage_extra)
    return _sse(
        [
            (
                "message_start",
                {"type": "message_start", "message": {"model": model, "usage": start_usage}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            ),
            ("message_delta", {"type": "message_delta", "usage": {"output_tokens": output_tokens}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def test_parse_usage_non_streaming():
    assert parse_usage(_non_streaming_body(), streaming=False) == Usage(
        model=MODEL, input_tokens=100, output_tokens=50
    )


def test_parse_usage_streaming():
    assert parse_usage(_streaming_body(), streaming=True) == Usage(
        model=MODEL, input_tokens=100, output_tokens=50
    )


def test_parse_usage_streaming_no_message_start_raises():
    with pytest.raises(ValueError):
        parse_usage(b'event: ping\ndata: {"type": "ping"}\n\n', streaming=True)


def test_parse_usage_reads_cache_counters():
    usage = parse_usage(
        _non_streaming_body(cache_read_input_tokens=3000, cache_creation_input_tokens=800),
        streaming=False,
    )
    assert usage.cache_read_tokens == 3000
    # No TTL breakdown present, so writes fall to the default 5-minute rate.
    assert usage.cache_write_5m_tokens == 800
    assert usage.cache_write_1h_tokens == 0


def test_parse_usage_splits_cache_writes_by_ttl_when_reported():
    usage = parse_usage(
        _non_streaming_body(
            cache_creation_input_tokens=1000,
            cache_creation={"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600},
        ),
        streaming=False,
    )
    assert usage.cache_write_5m_tokens == 400
    assert usage.cache_write_1h_tokens == 600


def test_parse_usage_streaming_reads_cache_counters():
    usage = parse_usage(
        _streaming_body(cache_read_input_tokens=5000, cache_creation_input_tokens=250),
        streaming=True,
    )
    assert usage.cache_read_tokens == 5000
    assert usage.cache_write_5m_tokens == 250
    assert usage.output_tokens == 50


def test_parse_usage_streaming_merges_later_usage_over_earlier():
    """message_delta repeats the input counters on newer API versions."""
    body = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "model": MODEL,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 900,
                        },
                    },
                },
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "usage": {"output_tokens": 77, "cache_read_input_tokens": 950},
                },
            ),
        ]
    )
    usage = parse_usage(body, streaming=True)
    assert usage.output_tokens == 77
    assert usage.cache_read_tokens == 950
    assert usage.input_tokens == 10


def test_parse_usage_streaming_skips_non_json_data_lines():
    body = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"model": MODEL, "usage": {"input_tokens": 5, "output_tokens": 2}},
                },
            )
        ]
    )
    body += b"data: [DONE]\n\n"
    assert parse_usage(body, streaming=True).input_tokens == 5


def test_capture_request_inserts_computed_cost():
    conn = MagicMock()
    capture_request(
        conn,
        body=_non_streaming_body(input_tokens=1000, output_tokens=1000),
        streaming=False,
        latency_ms=250,
        source_tool="claude-code",
    )

    conn.execute.assert_called_once()
    params = conn.execute.call_args.args[1]
    model, input_tokens, output_tokens, cache_read, write_5m, write_1h, cost, latency, tool = params
    assert (model, input_tokens, output_tokens) == (MODEL, 1000, 1000)
    assert (cache_read, write_5m, write_1h) == (0, 0, 0)
    assert cost == pytest.approx((1000 * 5.0 + 1000 * 25.0) / 1_000_000)
    assert (latency, tool) == (250, "claude-code")


def test_capture_request_prices_cached_tokens():
    conn = MagicMock()
    capture_request(
        conn,
        body=_non_streaming_body(
            input_tokens=1000, output_tokens=1000, cache_read_input_tokens=100_000
        ),
        streaming=False,
        latency_ms=250,
        source_tool=None,
    )

    params = conn.execute.call_args.args[1]
    assert params[3] == 100_000
    expected = (1000 * 5.0 + 1000 * 25.0 + 100_000 * 5.0 * 0.1) / 1_000_000
    assert params[6] == pytest.approx(expected)
