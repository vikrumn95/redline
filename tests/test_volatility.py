"""Unit tests: SMA200 computation and volatility math."""
from __future__ import annotations

import math

import pytest

from redline.sizing.volatility import annualized_vol, log_returns, sample_daily_vol
from redline.strategies.crypto.btc_eth_trend import sma, sma200_signal


# -----------------------------------------------------------------------
# SMA tests
# -----------------------------------------------------------------------


def test_sma_basic():
    prices = list(range(1, 11))  # [1, 2, ..., 10]
    assert sma(prices, 5) == pytest.approx(8.0)  # mean of [6,7,8,9,10]


def test_sma_all_same():
    prices = [100.0] * 200
    assert sma(prices, 200) == pytest.approx(100.0)


def test_sma_insufficient_raises():
    with pytest.raises(ValueError):
        sma([1.0, 2.0], 5)


def test_sma200_signal_risk_on():
    # Last price above SMA200: risk-on
    prices = [100.0] * 200
    prices[-1] = 110.0  # well above SMA(199 * 100 + 110) / 200 ≈ 100.05
    assert sma200_signal(prices) is True


def test_sma200_signal_risk_off():
    # Last price below SMA200: risk-off
    prices = [100.0] * 200
    prices[-1] = 90.0
    assert sma200_signal(prices) is False


def test_sma200_signal_exactly_equal_is_risk_off():
    # Strict greater-than: equal is risk-off
    prices = [100.0] * 200
    assert sma200_signal(prices) is False


def test_sma200_signal_insufficient_raises():
    with pytest.raises(ValueError):
        sma200_signal([100.0] * 199)


# -----------------------------------------------------------------------
# Log returns tests
# -----------------------------------------------------------------------


def test_log_returns_basic():
    prices = [100.0, 110.0, 99.0]
    r = log_returns(prices)
    assert len(r) == 2
    assert r[0] == pytest.approx(math.log(110.0 / 100.0))
    assert r[1] == pytest.approx(math.log(99.0 / 110.0))


def test_log_returns_flat():
    prices = [50.0] * 5
    r = log_returns(prices)
    assert all(v == pytest.approx(0.0) for v in r)


def test_log_returns_single_price():
    assert log_returns([100.0]) == []


def test_log_returns_empty():
    assert log_returns([]) == []


# -----------------------------------------------------------------------
# Sample daily vol tests
# -----------------------------------------------------------------------


def test_sample_daily_vol_known_value():
    # 20 identical returns → stdev = 0
    returns = [0.01] * 20
    assert sample_daily_vol(returns) == pytest.approx(0.0)


def test_sample_daily_vol_denominator_n_minus_1():
    """Denominator must be N-1=19 (sample stdev, not population stdev)."""
    returns = [0.0] * 19 + [1.0]  # 19 zeros and one 1.0
    mean = 1.0 / 20
    variance = (19 * (0.0 - mean) ** 2 + (1.0 - mean) ** 2) / 19
    expected = math.sqrt(variance)
    assert sample_daily_vol(returns, n=20) == pytest.approx(expected)


def test_sample_daily_vol_uses_last_n():
    """Uses the most recent n returns, not the entire list."""
    # Append 20 zeros at the end; earlier values are large and should be ignored.
    returns = [100.0] * 30 + [0.0] * 20
    assert sample_daily_vol(returns, n=20) == pytest.approx(0.0)


def test_sample_daily_vol_insufficient_raises():
    with pytest.raises(ValueError):
        sample_daily_vol([0.01] * 5, n=20)


# -----------------------------------------------------------------------
# Annualized vol tests
# -----------------------------------------------------------------------


def test_annualized_vol_scaling():
    """sigma_ann = sigma_d * sqrt(365)."""
    # Construct prices with known log returns
    returns_21 = [0.01] * 19 + [0.03, 0.0]  # 21 returns → 22 prices
    prices = [100.0]
    for r in returns_21:
        prices.append(prices[-1] * math.exp(r))

    sigma_d = sample_daily_vol(log_returns(prices), n=20)
    expected_ann = sigma_d * math.sqrt(365)

    assert annualized_vol(prices, n=20) == pytest.approx(expected_ann)


def test_annualized_vol_insufficient_raises():
    with pytest.raises(ValueError):
        annualized_vol([100.0] * 20, n=20)  # need 21, got 20


def test_annualized_vol_zero_variance():
    # Flat prices → 0 volatility
    prices = [100.0] * 21
    assert annualized_vol(prices, n=20) == pytest.approx(0.0)
