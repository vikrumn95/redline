"""Shared domain types."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RunStatus(str, Enum):
    OK = "ok"
    HALTED = "halted"
    ERROR = "error"
    SKIPPED = "skipped"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Candle:
    ts_utc: datetime  # start of candle (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Balance:
    asset: str  # e.g. "USD", "BTC", "ETH"
    available: float
    hold: float = 0.0

    @property
    def total(self) -> float:
        return self.available + self.hold


@dataclass
class ProductConstraints:
    product_id: str          # e.g. "BTC-USD"
    base_increment: str      # e.g. "0.00000001"
    base_min_size: str       # e.g. "0.000016"
    quote_increment: str     # e.g. "0.01"
    quote_min_size: str      # e.g. "1"


@dataclass
class PlannedOrder:
    product_id: str
    side: OrderSide
    base_size: float | None       # for SELL (crypto qty)
    quote_size_usd: float | None  # for BUY (USD amount)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "side": self.side.value,
            "base_size": self.base_size,
            "quote_size_usd": self.quote_size_usd,
            "reason": self.reason,
        }


@dataclass
class EquitySnapshot:
    run_id: str
    ts_utc: str   # ISO8601 string
    equity_usd: float


@dataclass
class RunRecord:
    run_id: str
    ts_utc: str
    status: RunStatus
    dry_run: bool
    equity_usd: float
    peak_equity_usd: float
    drawdown: float
    halt_flag: bool = False
    halt_reason: str | None = None
    skip_reason: str | None = None  # null if >=1 order placed; e.g. "risk_off_both", "min_trade_threshold", "at_target", "halted"
    signals: dict[str, bool] = field(default_factory=dict)
    vols: dict[str, float] = field(default_factory=dict)
    targets: dict[str, float] = field(default_factory=dict)
    planned_orders: list[dict] = field(default_factory=list)
    positions: dict[str, float] = field(default_factory=dict)
