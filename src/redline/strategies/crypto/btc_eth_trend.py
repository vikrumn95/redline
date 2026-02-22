"""BTC/ETH SMA200 trend signal.

Rule: risk-on if Close > SMA200 on daily closes aligned to rebalance cutoff.
"""
from __future__ import annotations

from redline.core.types import Candle
from redline.data.candles import close_prices


def sma(prices: list[float], n: int) -> float:
    """Simple moving average of the last n prices."""
    if len(prices) < n:
        raise ValueError(f"Need at least {n} prices for SMA{n}, got {len(prices)}")
    return sum(prices[-n:]) / n


def sma200_signal(prices: list[float]) -> bool:
    """True (risk-on) if most recent close > SMA200, else False (risk-off).

    Requires at least 200 prices.
    """
    return prices[-1] > sma(prices, 200)


def compute_signals(candles: dict[str, list[Candle]]) -> dict[str, bool]:
    """Compute risk-on/off signal for each product."""
    return {pid: sma200_signal(close_prices(c)) for pid, c in candles.items()}
