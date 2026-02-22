"""Order planning: compute what trades are needed to reach target positions."""
from __future__ import annotations

import decimal

from redline.core.types import Balance, OrderSide, PlannedOrder, ProductConstraints


def _round_down(qty: float, increment: str) -> float:
    """Round qty DOWN to the nearest valid increment (e.g. '0.00000001')."""
    inc = decimal.Decimal(increment)
    qty_d = decimal.Decimal(str(qty))
    rounded = (qty_d / inc).to_integral_value(rounding=decimal.ROUND_DOWN) * inc
    return float(rounded)


def plan_orders(
    balances: dict[str, Balance],
    target_notionals: dict[str, float],   # {product_id: target_usd_notional}
    prices: dict[str, float],             # {base_asset: price_usd}  e.g. {"BTC": 50000}
    constraints: dict[str, ProductConstraints],
    equity: float,
    min_trade_usd: float = 50.0,
) -> list[PlannedOrder]:
    """Compute planned orders to move from current to target positions.

    BUY  orders use quote_size (USD amount).
    SELL orders use base_size (crypto qty).
    Orders below min_trade_usd notional are skipped.
    """
    orders: list[PlannedOrder] = []

    for product_id, target_notional in target_notionals.items():
        base_asset = product_id.split("-")[0]
        price = prices.get(base_asset, 0.0)
        if price <= 0:
            continue

        current_qty = balances.get(base_asset, Balance(base_asset, 0.0)).available
        current_notional = current_qty * price
        target_qty = target_notional / price

        delta_notional = target_notional - current_notional
        delta_qty = target_qty - current_qty

        if abs(delta_notional) < min_trade_usd:
            continue

        constraint = constraints.get(product_id)
        quote_increment = constraint.quote_increment if constraint else "0.01"
        base_increment = constraint.base_increment if constraint else "0.00000001"
        base_min_size = float(constraint.base_min_size) if constraint else 0.0

        if delta_notional > 0:
            # BUY: specify quote_size in USD
            quote_size = _round_down(delta_notional, quote_increment)
            if quote_size < min_trade_usd:
                continue
            orders.append(
                PlannedOrder(
                    product_id=product_id,
                    side=OrderSide.BUY,
                    base_size=None,
                    quote_size_usd=quote_size,
                    reason="rebalance",
                )
            )
        else:
            # SELL: specify base_size in crypto
            sell_qty = _round_down(abs(delta_qty), base_increment)
            if sell_qty < base_min_size:
                continue
            sell_notional = sell_qty * price
            if sell_notional < min_trade_usd:
                continue
            orders.append(
                PlannedOrder(
                    product_id=product_id,
                    side=OrderSide.SELL,
                    base_size=sell_qty,
                    quote_size_usd=None,
                    reason="rebalance",
                )
            )

    return orders


def plan_liquidation_orders(
    balances: dict[str, Balance],
    prices: dict[str, float],
    constraints: dict[str, ProductConstraints],
    min_trade_usd: float = 50.0,
) -> list[PlannedOrder]:
    """Plan SELL orders to liquidate all BTC/ETH to USD ('go flat')."""
    target_notionals = {
        f"{asset}-USD": 0.0
        for asset in ("BTC", "ETH")
        if balances.get(asset, Balance(asset, 0.0)).available > 0
    }
    return plan_orders(balances, target_notionals, prices, constraints, 0.0, min_trade_usd)
