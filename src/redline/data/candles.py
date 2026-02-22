"""Candle data utilities."""
from __future__ import annotations

from redline.core.types import Candle


def close_prices(candles: list[Candle]) -> list[float]:
    """Return close prices sorted ascending by time."""
    return [c.close for c in sorted(candles, key=lambda c: c.ts_utc)]
