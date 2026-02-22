"""Unit tests: circuit breakers."""
from __future__ import annotations

import pytest

from redline.core.risk import check_daily_loss, check_drawdown, evaluate_circuit_breakers
from redline.core.types import EquitySnapshot


def _snap(equity: float) -> EquitySnapshot:
    return EquitySnapshot(run_id="test", ts_utc="2025-01-01T00:00:00+00:00", equity_usd=equity)


# -----------------------------------------------------------------------
# Daily loss
# -----------------------------------------------------------------------


def test_daily_loss_not_triggered():
    history = [_snap(10000.0)]
    # equity dropped by 3% < 5% threshold
    assert check_daily_loss(9700.0, history, max_daily_loss=0.05) is False


def test_daily_loss_triggered():
    history = [_snap(10000.0)]
    # equity dropped 6% > 5% threshold
    assert check_daily_loss(9400.0, history, max_daily_loss=0.05) is True


def test_daily_loss_no_history():
    assert check_daily_loss(9000.0, [], max_daily_loss=0.05) is False


def test_daily_loss_exactly_at_threshold_not_triggered():
    history = [_snap(10000.0)]
    # exactly 5% loss → not strictly greater-than → not triggered
    assert check_daily_loss(9500.0, history, max_daily_loss=0.05) is False


def test_daily_loss_gain_not_triggered():
    history = [_snap(10000.0)]
    assert check_daily_loss(11000.0, history, max_daily_loss=0.05) is False


# -----------------------------------------------------------------------
# Drawdown
# -----------------------------------------------------------------------


def test_drawdown_not_triggered():
    # 10% drawdown from peak, threshold 30%
    assert check_drawdown(9000.0, 10000.0, max_drawdown=0.30) is False


def test_drawdown_triggered():
    # 35% drawdown > 30% threshold
    assert check_drawdown(6500.0, 10000.0, max_drawdown=0.30) is True


def test_drawdown_zero_peak():
    assert check_drawdown(9000.0, 0.0, max_drawdown=0.30) is False


def test_drawdown_exactly_at_threshold_not_triggered():
    assert check_drawdown(7000.0, 10000.0, max_drawdown=0.30) is False


# -----------------------------------------------------------------------
# evaluate_circuit_breakers
# -----------------------------------------------------------------------


def test_evaluate_no_breach():
    history = [_snap(10000.0)]
    triggered, reason = evaluate_circuit_breakers(9700.0, 10000.0, history)
    assert triggered is False
    assert reason is None


def test_evaluate_daily_loss_breach():
    history = [_snap(10000.0)]
    triggered, reason = evaluate_circuit_breakers(9400.0, 10000.0, history)
    assert triggered is True
    assert reason is not None
    assert "daily_loss" in reason


def test_evaluate_drawdown_breach():
    # Large drawdown from peak, small daily move
    history = [_snap(6500.0)]
    triggered, reason = evaluate_circuit_breakers(6500.0, 10000.0, history)
    assert triggered is True
    assert reason is not None
    assert "drawdown" in reason


def test_evaluate_daily_loss_takes_precedence():
    """Daily loss is checked first."""
    history = [_snap(10000.0)]
    # Both breakers would trigger; expect daily_loss reason
    triggered, reason = evaluate_circuit_breakers(6000.0, 20000.0, history)
    assert triggered is True
    assert "daily_loss" in (reason or "")
