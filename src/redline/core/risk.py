"""Circuit breakers (evaluated once per run at startup, before strategy)."""
from __future__ import annotations

from redline.core.types import EquitySnapshot


def check_daily_loss(
    equity: float,
    equity_history: list[EquitySnapshot],
    max_daily_loss: float = 0.05,
) -> bool:
    """Return True if daily loss exceeds the threshold.

    daily_loss = (prior_equity - equity) / prior_equity
    Triggered when daily_loss > max_daily_loss (e.g. 5%).
    """
    if not equity_history:
        return False
    prior_equity = equity_history[0].equity_usd  # most recent prior snapshot
    if prior_equity <= 0:
        return False
    daily_loss = (prior_equity - equity) / prior_equity
    return daily_loss > max_daily_loss


def check_drawdown(
    equity: float,
    peak_equity: float,
    max_drawdown: float = 0.30,
) -> bool:
    """Return True if peak-to-trough drawdown exceeds the threshold.

    drawdown = (peak_equity - equity) / peak_equity
    Triggered when drawdown > max_drawdown (e.g. 30%).
    """
    if peak_equity <= 0:
        return False
    drawdown = (peak_equity - equity) / peak_equity
    return drawdown > max_drawdown


def evaluate_circuit_breakers(
    equity: float,
    peak_equity: float,
    equity_history: list[EquitySnapshot],
    max_daily_loss: float = 0.05,
    max_drawdown: float = 0.30,
) -> tuple[bool, str | None]:
    """Evaluate all circuit breakers.

    Returns (triggered: bool, reason: str | None).
    Evaluated at start of each run, before SMA strategy logic.
    """
    if check_daily_loss(equity, equity_history, max_daily_loss):
        prior = equity_history[0].equity_usd
        loss_pct = (prior - equity) / prior
        return True, f"daily_loss_exceeded({loss_pct:.2%}): equity={equity:.2f}, prior={prior:.2f}"

    if check_drawdown(equity, peak_equity, max_drawdown):
        dd = (peak_equity - equity) / peak_equity
        return True, f"drawdown_exceeded({dd:.2%}): equity={equity:.2f}, peak={peak_equity:.2f}"

    return False, None
