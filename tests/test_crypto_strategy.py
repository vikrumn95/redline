"""Unit tests: equity calculation and strategy signal helpers."""
from __future__ import annotations

import pytest

from redline.core.reconcile import compute_equity, reconcile_check
from redline.core.types import Balance


# -----------------------------------------------------------------------
# Equity calculation
# -----------------------------------------------------------------------


def test_compute_equity_usd_only():
    balances = {"USD": Balance("USD", 5000.0)}
    prices = {"BTC": 50000.0, "ETH": 3000.0}
    assert compute_equity(balances, prices) == pytest.approx(5000.0)


def test_compute_equity_full():
    balances = {
        "USD": Balance("USD", 1000.0),
        "BTC": Balance("BTC", 0.1),
        "ETH": Balance("ETH", 2.0),
    }
    prices = {"BTC": 50000.0, "ETH": 3000.0}
    # 1000 + 0.1*50000 + 2*3000 = 1000 + 5000 + 6000 = 12000
    assert compute_equity(balances, prices) == pytest.approx(12000.0)


def test_compute_equity_uses_available_not_hold():
    """Only available balance is used, hold is excluded."""
    balances = {"BTC": Balance("BTC", available=0.5, hold=1.0)}
    prices = {"BTC": 40000.0}
    # Only 0.5 BTC available; hold of 1.0 is ignored
    assert compute_equity(balances, prices) == pytest.approx(0.5 * 40000.0)


def test_compute_equity_missing_balance():
    """Missing assets default to zero quantity."""
    balances = {"USD": Balance("USD", 500.0)}
    prices = {"BTC": 60000.0, "ETH": 3500.0}
    assert compute_equity(balances, prices) == pytest.approx(500.0)


def test_compute_equity_zero_price():
    """Zero price for an asset contributes zero to equity."""
    balances = {"BTC": Balance("BTC", 1.0), "USD": Balance("USD", 100.0)}
    prices = {"BTC": 0.0}
    assert compute_equity(balances, prices) == pytest.approx(100.0)


# -----------------------------------------------------------------------
# Reconcile check
# -----------------------------------------------------------------------


def test_reconcile_check_within_usd_tolerance():
    assert reconcile_check(10000.0, 10003.0, tolerance_usd=5.0) is True


def test_reconcile_check_within_pct_tolerance():
    assert reconcile_check(10000.0, 10009.0, tolerance_usd=5.0, tolerance_pct=0.001) is True


def test_reconcile_check_exceeds_both():
    assert reconcile_check(10000.0, 10100.0, tolerance_usd=5.0, tolerance_pct=0.001) is False


def test_reconcile_check_no_history():
    """Zero db_equity (no history) always passes."""
    assert reconcile_check(0.0, 50000.0) is True
