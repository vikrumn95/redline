"""Unit tests: sizing caps, portfolio vol scaling, and order planning."""
from __future__ import annotations

import pytest

from redline.core.portfolio import plan_orders, plan_liquidation_orders
from redline.core.types import Balance, ProductConstraints
from redline.sizing.risk_parity import (
    compute_target_notionals,
    inverse_vol_weights,
    portfolio_vol,
)


# -----------------------------------------------------------------------
# Inverse-vol weights
# -----------------------------------------------------------------------


def test_inverse_vol_weights_sum_to_one():
    vols = {"BTC-USD": 0.65, "ETH-USD": 0.80}
    w = inverse_vol_weights(vols)
    assert sum(w.values()) == pytest.approx(1.0)


def test_inverse_vol_weights_lower_vol_gets_higher_weight():
    vols = {"BTC-USD": 0.50, "ETH-USD": 1.00}
    w = inverse_vol_weights(vols)
    assert w["BTC-USD"] > w["ETH-USD"]


def test_inverse_vol_weights_single_asset():
    vols = {"BTC-USD": 0.70}
    w = inverse_vol_weights(vols)
    assert w == pytest.approx({"BTC-USD": 1.0})


def test_inverse_vol_weights_equal_vols():
    vols = {"BTC-USD": 0.60, "ETH-USD": 0.60}
    w = inverse_vol_weights(vols)
    assert w["BTC-USD"] == pytest.approx(0.5)
    assert w["ETH-USD"] == pytest.approx(0.5)


def test_inverse_vol_weights_empty():
    assert inverse_vol_weights({}) == {}


# -----------------------------------------------------------------------
# Portfolio vol (correlation = 1, weighted sum — no covariance matrix)
# -----------------------------------------------------------------------


def test_portfolio_vol_weighted_sum():
    """sigma_port = sum(w_i * sigma_i), NOT sqrt of covariance matrix."""
    weights = {"BTC-USD": 0.6, "ETH-USD": 0.4}
    vols = {"BTC-USD": 0.50, "ETH-USD": 0.75}
    expected = 0.6 * 0.50 + 0.4 * 0.75
    assert portfolio_vol(weights, vols) == pytest.approx(expected)


def test_portfolio_vol_single_asset_equals_its_vol():
    weights = {"BTC-USD": 1.0}
    vols = {"BTC-USD": 0.65}
    assert portfolio_vol(weights, vols) == pytest.approx(0.65)


# -----------------------------------------------------------------------
# compute_target_notionals — sizing caps
# -----------------------------------------------------------------------


def _btc_eth_vols():
    return {"BTC-USD": 0.65, "ETH-USD": 0.80}


def test_target_notionals_apply_gross_cap():
    """Gross notional must not exceed equity * max_gross."""
    equity = 10000.0
    vols = {"BTC-USD": 0.01, "ETH-USD": 0.01}  # very low vol → huge uncapped scaling
    weights = inverse_vol_weights(vols)
    notionals = compute_target_notionals(
        equity, weights, vols, target_vol=0.25, max_gross=1.0, max_per_asset=0.70
    )
    assert sum(notionals.values()) <= equity * 1.0 + 1e-9


def test_target_notionals_apply_per_asset_cap():
    """Per-asset notional must not exceed equity * max_per_asset."""
    equity = 10000.0
    vols = {"BTC-USD": 0.01}  # single asset, low vol → big uncapped notional
    weights = {"BTC-USD": 1.0}
    notionals = compute_target_notionals(
        equity, weights, vols, target_vol=0.25, max_gross=1.0, max_per_asset=0.70
    )
    assert notionals["BTC-USD"] <= equity * 0.70 + 1e-9


def test_target_notionals_scaling_formula():
    """Verify the scaling formula: desired_gross = equity * (target_vol / sigma_port)."""
    equity = 10000.0
    vols = {"BTC-USD": 0.50}
    weights = {"BTC-USD": 1.0}
    sigma_port = 0.50
    expected_gross = equity * (0.25 / sigma_port)  # = 5000

    notionals = compute_target_notionals(
        equity, weights, vols, target_vol=0.25, max_gross=1.0, max_per_asset=0.70
    )
    assert notionals["BTC-USD"] == pytest.approx(expected_gross)


def test_target_notionals_two_assets_vol_scaling():
    """With two assets, sigma_port = w1*s1 + w2*s2 and notionals should reflect it."""
    equity = 10000.0
    vols = {"BTC-USD": 0.65, "ETH-USD": 0.80}
    weights = inverse_vol_weights(vols)
    sigma_port = portfolio_vol(weights, vols)
    scaling = 0.25 / sigma_port
    desired_gross = min(equity * scaling, equity * 1.0)

    notionals = compute_target_notionals(
        equity, weights, vols, target_vol=0.25, max_gross=1.0, max_per_asset=0.70
    )
    total = sum(notionals.values())
    assert total == pytest.approx(desired_gross, rel=1e-6)


def test_target_notionals_empty():
    assert compute_target_notionals(10000.0, {}, {}) == {}


# -----------------------------------------------------------------------
# Order planning
# -----------------------------------------------------------------------

_CONSTRAINTS = {
    "BTC-USD": ProductConstraints("BTC-USD", "0.00000001", "0.000016", "0.01", "1"),
    "ETH-USD": ProductConstraints("ETH-USD", "0.00000001", "0.00022", "0.01", "1"),
}


def test_plan_orders_buy_when_underweight():
    balances = {"USD": Balance("USD", 10000.0), "BTC": Balance("BTC", 0.0)}
    target_notionals = {"BTC-USD": 5000.0}
    prices = {"BTC": 50000.0}
    orders = plan_orders(balances, target_notionals, prices, _CONSTRAINTS, 10000.0, min_trade_usd=50.0)
    assert len(orders) == 1
    assert orders[0].side.value == "buy"
    assert orders[0].quote_size_usd == pytest.approx(5000.0)


def test_plan_orders_sell_when_overweight():
    balances = {"BTC": Balance("BTC", 0.2)}
    target_notionals = {"BTC-USD": 0.0}
    prices = {"BTC": 50000.0}
    orders = plan_orders(balances, target_notionals, prices, _CONSTRAINTS, 10000.0, min_trade_usd=50.0)
    assert len(orders) == 1
    assert orders[0].side.value == "sell"
    assert orders[0].base_size == pytest.approx(0.2)


def test_plan_orders_skip_below_min_trade():
    balances = {"BTC": Balance("BTC", 0.0001)}
    target_notionals = {"BTC-USD": 0.0}
    prices = {"BTC": 50000.0}
    # 0.0001 BTC * 50000 = $5 < $50 min
    orders = plan_orders(balances, target_notionals, prices, _CONSTRAINTS, 5.0, min_trade_usd=50.0)
    assert len(orders) == 0


def test_plan_orders_no_change_needed():
    balances = {"BTC": Balance("BTC", 0.1)}
    target_notionals = {"BTC-USD": 5000.0}  # 0.1 BTC @ 50000 = $5000 exactly
    prices = {"BTC": 50000.0}
    orders = plan_orders(balances, target_notionals, prices, _CONSTRAINTS, 10000.0, min_trade_usd=50.0)
    assert len(orders) == 0


def test_plan_liquidation_orders():
    balances = {
        "BTC": Balance("BTC", 0.5),
        "ETH": Balance("ETH", 5.0),
        "USD": Balance("USD", 1000.0),
    }
    prices = {"BTC": 50000.0, "ETH": 3000.0}
    orders = plan_liquidation_orders(balances, prices, _CONSTRAINTS, min_trade_usd=50.0)
    sides = {o.product_id: o.side.value for o in orders}
    assert sides.get("BTC-USD") == "sell"
    assert sides.get("ETH-USD") == "sell"
