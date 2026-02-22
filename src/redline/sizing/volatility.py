"""Volatility math: log returns, sample stdev, annualization.

Formulas (as per SPEC):
  R_t = ln(P_t / P_{t-1})
  sigma_d = sqrt( sum((R_i - R_bar)^2) / 19 )   [N=20, denominator=N-1=19]
  sigma_ann = sigma_d * sqrt(365)
"""
from __future__ import annotations

import math


def log_returns(prices: list[float]) -> list[float]:
    """Compute daily log returns: R_t = ln(P_t / P_{t-1})."""
    if len(prices) < 2:
        return []
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def sample_daily_vol(returns: list[float], n: int = 20) -> float:
    """Sample stdev of the most recent n log returns (denominator = n-1).

    sigma_d = sqrt( sum((R_i - R_bar)^2) / (n-1) )
    """
    if len(returns) < n:
        raise ValueError(f"Need at least {n} returns, got {len(returns)}")
    r = returns[-n:]
    mean = sum(r) / len(r)
    variance = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(variance)


def annualized_vol(prices: list[float], n: int = 20) -> float:
    """Annualized volatility: sigma_ann = sigma_d * sqrt(365).

    Requires at least n+1 prices to compute n log returns.
    """
    if len(prices) < n + 1:
        raise ValueError(f"Need at least {n + 1} prices, got {len(prices)}")
    r = log_returns(prices)
    sigma_d = sample_daily_vol(r, n)
    return sigma_d * math.sqrt(365)
