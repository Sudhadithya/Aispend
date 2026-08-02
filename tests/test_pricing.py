from datetime import UTC, datetime

import pytest

from aispend.proxy.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    PRICING_PER_MTOK,
    UnknownModelError,
    calculate_cost,
)

# Models a current Claude Code session actually routes through the proxy. If
# the price table stops covering these, capture fails closed and the tool
# silently records nothing — which is exactly how it broke before.
CURRENTLY_CALLABLE_MODELS = [
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
]


@pytest.mark.parametrize("model", CURRENTLY_CALLABLE_MODELS)
def test_current_models_are_priced(model):
    assert model in PRICING_PER_MTOK
    assert calculate_cost(model, input_tokens=1000, output_tokens=1000) > 0


def test_calculate_cost_known_model():
    cost = calculate_cost("claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(5.0 + 25.0)


def test_calculate_cost_zero_tokens():
    assert calculate_cost("claude-opus-4-1-20250805", input_tokens=0, output_tokens=0) == 0.0


def test_calculate_cost_fractional():
    cost = calculate_cost("claude-haiku-4-5", input_tokens=500, output_tokens=200)
    assert cost == pytest.approx((500 * 1.0 + 200 * 5.0) / 1_000_000)


def test_calculate_cost_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        calculate_cost("claude-nonexistent-model", input_tokens=100, output_tokens=100)


def test_unknown_model_error_is_a_value_error():
    assert issubclass(UnknownModelError, ValueError)


def test_cache_reads_are_billed_at_a_tenth_of_input():
    cost = calculate_cost(
        "claude-opus-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert cost == pytest.approx(5.0 * CACHE_READ_MULTIPLIER)


def test_cache_writes_are_billed_by_ttl():
    write_5m = calculate_cost(
        "claude-opus-5", input_tokens=0, output_tokens=0, cache_write_5m_tokens=1_000_000
    )
    write_1h = calculate_cost(
        "claude-opus-5", input_tokens=0, output_tokens=0, cache_write_1h_tokens=1_000_000
    )
    assert write_5m == pytest.approx(5.0 * CACHE_WRITE_5M_MULTIPLIER)
    assert write_1h == pytest.approx(5.0 * CACHE_WRITE_1H_MULTIPLIER)


def test_ignoring_cache_tokens_would_understate_cost():
    """The bug this guards: a cached request costs more than its uncached remainder."""
    uncached_only = calculate_cost("claude-opus-5", input_tokens=1_000, output_tokens=500)
    with_cache = calculate_cost(
        "claude-opus-5",
        input_tokens=1_000,
        output_tokens=500,
        cache_read_tokens=200_000,
        cache_write_5m_tokens=20_000,
    )
    assert with_cache > uncached_only


def test_worked_example_from_anthropic_pricing_docs():
    """10k uncached + 40k cache reads + 15k output on Opus 5 = $0.445 of tokens."""
    cost = calculate_cost(
        "claude-opus-5", input_tokens=10_000, output_tokens=15_000, cache_read_tokens=40_000
    )
    assert cost == pytest.approx(0.05 + 0.02 + 0.375)


def test_sonnet_5_uses_introductory_pricing_before_cutover():
    cost = calculate_cost(
        "claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        as_of=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert cost == pytest.approx(2.0 + 10.0)


def test_sonnet_5_uses_standard_pricing_after_cutover():
    cost = calculate_cost(
        "claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert cost == pytest.approx(3.0 + 15.0)
