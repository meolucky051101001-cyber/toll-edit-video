"""Tests for live gate configuration validation."""

import pytest

from backend.services.ranking_service import validate_live_gate_config


def test_live_gate_config_valid_values():
    """Accepts valid sample counts (>= 3) and valid relevance rates in (0.0, 1.0]."""
    samples, rate = validate_live_gate_config(3, 0.80)
    assert samples == 3
    assert rate == 0.80

    samples, rate = validate_live_gate_config("5", "0.95")
    assert samples == 5
    assert rate == 0.95

    samples, rate = validate_live_gate_config(10, 1.0)
    assert samples == 10
    assert rate == 1.0


def test_live_gate_config_invalid_samples():
    """Rejects sample counts below 3, zero, negative, or non-integer values."""
    bad_samples = [0, -1, -10, 1, 2, "abc", None, 2.5]
    for s in bad_samples:
        with pytest.raises(ValueError) as exc_info:
            validate_live_gate_config(s, 0.80)
        assert "MIN_LIVE_SAMPLES" in str(exc_info.value)


def test_live_gate_config_invalid_rates():
    """Rejects relevance rates <= 0.0, > 1.0, NaN, infinity, or non-numeric values."""
    bad_rates = [0.0, -0.01, -0.5, 1.0001, 1.5, 2.0, float("nan"), float("inf"), float("-inf"), "xyz", None]
    for r in bad_rates:
        with pytest.raises(ValueError) as exc_info:
            validate_live_gate_config(3, r)
        assert "MIN_LIVE_RELEVANCE_RATE" in str(exc_info.value)
