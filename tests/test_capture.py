import json
from unittest.mock import MagicMock

import pytest

from aispend.proxy.capture import capture_request, parse_usage


def _non_streaming_body(model="claude-sonnet-4-5-20250929", input_tokens=100, output_tokens=50):
    return json.dumps(
        {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    ).encode()


def _streaming_body(model="claude-sonnet-4-5-20250929", input_tokens=100, output_tokens=50):
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "model": model,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
        ),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": output_tokens}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    lines = []
    for event_name, data in events:
        lines.append(f"event: {event_name}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return "\n".join(lines).encode()


def test_parse_usage_non_streaming():
    result = parse_usage(_non_streaming_body(), streaming=False)
    assert result == {
        "model": "claude-sonnet-4-5-20250929",
        "input_tokens": 100,
        "output_tokens": 50,
    }


def test_parse_usage_streaming():
    result = parse_usage(_streaming_body(), streaming=True)
    assert result == {
        "model": "claude-sonnet-4-5-20250929",
        "input_tokens": 100,
        "output_tokens": 50,
    }


def test_parse_usage_streaming_no_message_start_raises():
    body = b'event: ping\ndata: {"type": "ping"}\n\n'
    with pytest.raises(ValueError):
        parse_usage(body, streaming=True)


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
    args = conn.execute.call_args.args
    params = args[1]
    assert params[0] == "claude-sonnet-4-5-20250929"
    assert params[1] == 1000
    assert params[2] == 1000
    assert params[3] == pytest.approx((1000 * 3.0 + 1000 * 15.0) / 1_000_000)
    assert params[4] == 250
    assert params[5] == "claude-code"
