"""Balance reconciliation and equity computation."""
from __future__ import annotations

from redline.core.types import Balance


def compute_equity(
    balances: dict[str, Balance],
    prices: dict[str, float],  # {"BTC": btc_usd_price, "ETH": eth_usd_price}
) -> float:
    """Compute total equity in USD using available balances only.

    equity_usd = available_usd
               + available_btc * btc_price
               + available_eth * eth_price

    Does NOT use Coinbase aggregate portfolio totals.
    """
    usd = balances.get("USD", Balance("USD", 0.0)).available
    btc = balances.get("BTC", Balance("BTC", 0.0)).available
    eth = balances.get("ETH", Balance("ETH", 0.0)).available
    btc_price = prices.get("BTC", 0.0)
    eth_price = prices.get("ETH", 0.0)
    return usd + btc * btc_price + eth * eth_price


def reconcile_check(
    db_equity: float,
    live_equity: float,
    tolerance_usd: float = 5.0,
    tolerance_pct: float = 0.001,
) -> bool:
    """Return True if equities match within tolerance (no mismatch).

    Passes if either the absolute difference <= tolerance_usd
    OR the relative difference <= tolerance_pct.
    """
    if db_equity <= 0:
        return True  # no prior history; nothing to compare
    diff = abs(db_equity - live_equity)
    pct = diff / db_equity
    return diff <= tolerance_usd or pct <= tolerance_pct
