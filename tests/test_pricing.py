import pytest

from aispend.proxy.pricing import calculate_cost


def test_calculate_cost_known_model():
    cost = calculate_cost(
        "claude-sonnet-4-5-20250929", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost == pytest.approx(3.0 + 15.0)


def test_calculate_cost_zero_tokens():
    cost = calculate_cost("claude-opus-4-1-20250805", input_tokens=0, output_tokens=0)
    assert cost == 0.0


def test_calculate_cost_fractional():
    cost = calculate_cost("claude-3-5-haiku-20241022", input_tokens=500, output_tokens=200)
    expected = (500 * 0.8 + 200 * 4.0) / 1_000_000
    assert cost == pytest.approx(expected)


def test_calculate_cost_unknown_model_raises():
    with pytest.raises(ValueError):
        calculate_cost("claude-nonexistent-model", input_tokens=100, output_tokens=100)
