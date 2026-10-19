"""Shared adapter validation, independent of native capabilities."""

import pytest

from conty.adapters.base import AdapterError, validate_timeout


@pytest.mark.parametrize(
    "timeout", [0, -1, True, False, float("nan"), float("inf"), -float("inf"), "5", None]
)
def test_timeout_rejects_invalid_values(timeout):
    with pytest.raises(AdapterError) as caught:
        validate_timeout(timeout)
    assert caught.value.code == "invalid_action"
    assert not caught.value.effects_possible


@pytest.mark.parametrize("timeout", [1, 0.001, 5.0])
def test_timeout_accepts_finite_positive_numbers(timeout):
    validate_timeout(timeout)
