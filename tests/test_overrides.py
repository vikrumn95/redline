"""Unit tests: test-override CLI flags (signal overrides, forced gross, gating, observability)."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from redline.config import Config
from redline.core.store import Store
from redline.core.types import Balance
from redline.main import (
    _apply_signal_overrides,
    _compute_forced_targets,
    cmd_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def _cfg(tmp_path: Path, *, allow_overrides: bool = False, env: str = "dev") -> Config:
    return Config(
        coinbase_api_key="",
        coinbase_api_secret="",
        coinbase_portfolio_id=None,
        dry_run=True,
        db_path=tmp_path / "test.db",
        log_level="DEBUG",
        env=env,
        rebalance_time_utc="00:00",
        target_vol=0.25,
        max_drawdown=0.30,
        max_daily_loss=0.05,
        min_trade_usd=50.0,
        max_gross_notional=1.0,
        max_per_asset_notional=0.70,
        slack_webhook_url=None,
        mock=True,
        allow_test_overrides=allow_overrides,
    )


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        dry_run=True,
        mock=True,
        force_btc_signal="COMPUTED",
        force_eth_signal="COMPUTED",
        force_gross_usd=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Test 1: Gating — override flag with wrong env/config → exit non-zero
# ---------------------------------------------------------------------------


def test_gating_fails_when_env_not_dev(tmp_path):
    """Override flags rejected when REDLINE_ENV != dev, even if allow_test_overrides=True."""
    cfg = _cfg(tmp_path, allow_overrides=True, env="prod")
    args = _args(force_btc_signal="ON")

    mock_provider = MagicMock()

    with (
        patch("redline.main.Store", return_value=_store(tmp_path)),
        patch("redline.main.CoinbaseDataProvider", return_value=mock_provider),
        patch("redline.main.CoinbaseSpotBroker"),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_run(args, cfg)

    assert exc_info.value.code != 0
    mock_provider.fetch_candles.assert_not_called()


def test_gating_fails_when_allow_flag_not_set(tmp_path):
    """Override flags rejected when REDLINE_ALLOW_TEST_OVERRIDES is false, even in dev."""
    cfg = _cfg(tmp_path, allow_overrides=False, env="dev")
    args = _args(force_eth_signal="OFF")

    mock_provider = MagicMock()

    with (
        patch("redline.main.Store", return_value=_store(tmp_path)),
        patch("redline.main.CoinbaseDataProvider", return_value=mock_provider),
        patch("redline.main.CoinbaseSpotBroker"),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_run(args, cfg)

    assert exc_info.value.code != 0
    mock_provider.fetch_candles.assert_not_called()


def test_gating_passes_with_both_conditions_met(tmp_path):
    """When both conditions are met, override flags are accepted and run completes normally."""
    cfg = _cfg(tmp_path, allow_overrides=True, env="dev")
    args = _args(force_btc_signal="ON")
    # mock=True → uses synthetic candles/balances; dry_run=True → no real orders
    cmd_run(args, cfg)  # must NOT raise SystemExit


# ---------------------------------------------------------------------------
# Test 2: Signal override behavior (pure helper function)
# ---------------------------------------------------------------------------


def test_apply_signal_overrides_both_off():
    """Both OFF → both signals become False regardless of computed values."""
    signals = {"BTC-USD": True, "ETH-USD": True}
    result = _apply_signal_overrides(signals, "OFF", "OFF")
    assert result == {"BTC-USD": False, "ETH-USD": False}


def test_apply_signal_overrides_eth_on_btc_off():
    """ETH ON + BTC OFF → ETH True, BTC False."""
    signals = {"BTC-USD": True, "ETH-USD": False}
    result = _apply_signal_overrides(signals, "OFF", "ON")
    assert result == {"BTC-USD": False, "ETH-USD": True}


def test_apply_signal_overrides_computed_leaves_unchanged():
    """COMPUTED for both → original signals preserved."""
    signals = {"BTC-USD": True, "ETH-USD": False}
    result = _apply_signal_overrides(signals, "COMPUTED", "COMPUTED")
    assert result == {"BTC-USD": True, "ETH-USD": False}


def test_both_off_results_in_zero_targets_and_plans_sells(tmp_path):
    """With both signals forced OFF and a BTC position, plan_orders produces a SELL."""
    from redline.core.portfolio import plan_orders
    from redline.core.types import OrderSide, ProductConstraints

    balances = {
        "USD": Balance("USD", 0.0),
        "BTC": Balance("BTC", 0.01),   # holding BTC that must be sold
        "ETH": Balance("ETH", 0.0),
    }
    signals = _apply_signal_overrides({"BTC-USD": True, "ETH-USD": True}, "OFF", "OFF")
    target_notionals = {p: 0.0 for p in ["BTC-USD", "ETH-USD"]}
    prices = {"BTC": 50000.0, "ETH": 3000.0}
    constraints = {
        "BTC-USD": ProductConstraints("BTC-USD", "0.00000001", "0.000016", "0.01", "1"),
        "ETH-USD": ProductConstraints("ETH-USD", "0.00000001", "0.001", "0.01", "1"),
    }

    orders = plan_orders(balances, target_notionals, prices, constraints, 10000.0, 50.0)
    sell_orders = [o for o in orders if o.side == OrderSide.SELL]

    assert len(sell_orders) == 1
    assert sell_orders[0].product_id == "BTC-USD"


def test_eth_on_btc_off_plans_eth_buy(tmp_path):
    """ETH ON + BTC OFF + USD available → plan_orders produces an ETH BUY."""
    from redline.core.portfolio import plan_orders
    from redline.core.types import OrderSide, ProductConstraints

    equity = 10000.0
    signals = _apply_signal_overrides({"BTC-USD": False, "ETH-USD": False}, "OFF", "ON")
    # With ETH ON only: 100% of $100 forced gross goes to ETH
    target_notionals = _compute_forced_targets(100.0, signals, equity, 1.0, 0.70)

    balances = {
        "USD": Balance("USD", 10000.0),
        "BTC": Balance("BTC", 0.0),
        "ETH": Balance("ETH", 0.0),
    }
    prices = {"BTC": 50000.0, "ETH": 3000.0}
    constraints = {
        "BTC-USD": ProductConstraints("BTC-USD", "0.00000001", "0.000016", "0.01", "1"),
        "ETH-USD": ProductConstraints("ETH-USD", "0.00000001", "0.001", "0.01", "1"),
    }

    orders = plan_orders(balances, target_notionals, prices, constraints, equity, 50.0)
    buy_orders = [o for o in orders if o.side == OrderSide.BUY]

    assert len(buy_orders) == 1
    assert buy_orders[0].product_id == "ETH-USD"


# ---------------------------------------------------------------------------
# Test 3: Forced gross allocation rule (pure helper function)
# ---------------------------------------------------------------------------


def test_forced_targets_both_risk_on_50_50_split():
    """Both risk-on + force_gross_usd=100 → $50 each (before per-asset cap)."""
    signals = {"BTC-USD": True, "ETH-USD": True}
    targets = _compute_forced_targets(100.0, signals, equity=10000.0, max_gross_notional=1.0, max_per_asset_notional=0.70)
    assert targets["BTC-USD"] == pytest.approx(50.0)
    assert targets["ETH-USD"] == pytest.approx(50.0)


def test_forced_targets_one_risk_on_100_percent():
    """One risk-on + force_gross_usd=100 → $100 to that asset."""
    signals = {"BTC-USD": False, "ETH-USD": True}
    targets = _compute_forced_targets(100.0, signals, equity=10000.0, max_gross_notional=1.0, max_per_asset_notional=0.70)
    assert targets["BTC-USD"] == pytest.approx(0.0)
    assert targets["ETH-USD"] == pytest.approx(100.0)


def test_forced_targets_none_risk_on_all_zero():
    """No risk-on assets → all targets 0."""
    signals = {"BTC-USD": False, "ETH-USD": False}
    targets = _compute_forced_targets(100.0, signals, equity=10000.0, max_gross_notional=1.0, max_per_asset_notional=0.70)
    assert targets["BTC-USD"] == pytest.approx(0.0)
    assert targets["ETH-USD"] == pytest.approx(0.0)


def test_forced_targets_respects_gross_cap():
    """force_gross_usd larger than max_gross_notional * equity is capped."""
    signals = {"BTC-USD": True, "ETH-USD": True}
    # equity=100, max_gross=1.0 → max_gross_notional=$100; forcing $200 → capped to $100 → $50 each
    targets = _compute_forced_targets(200.0, signals, equity=100.0, max_gross_notional=1.0, max_per_asset_notional=0.70)
    assert targets["BTC-USD"] == pytest.approx(50.0)
    assert targets["ETH-USD"] == pytest.approx(50.0)


def test_forced_targets_respects_per_asset_cap():
    """Each leg is also capped at max_per_asset_notional * equity."""
    signals = {"BTC-USD": True, "ETH-USD": False}
    # equity=100, max_per_asset=0.70 → max=$70; forcing $200 → capped to $70 for BTC
    targets = _compute_forced_targets(200.0, signals, equity=100.0, max_gross_notional=2.0, max_per_asset_notional=0.70)
    assert targets["BTC-USD"] == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# Test 4: Observability — log event emitted + override fields persisted
# ---------------------------------------------------------------------------


def test_test_override_active_log_emitted(tmp_path, capfd):
    """TEST_OVERRIDE_ACTIVE must appear in stdout when overrides are active."""
    cfg = _cfg(tmp_path, allow_overrides=True, env="dev")
    args = _args(force_btc_signal="ON", force_eth_signal="OFF")

    cmd_run(args, cfg)

    captured = capfd.readouterr()
    assert "TEST_OVERRIDE_ACTIVE" in captured.out


def test_override_fields_persisted_to_sqlite(tmp_path):
    """Override values must be saved in the run record's SQLite row."""
    cfg = _cfg(tmp_path, allow_overrides=True, env="dev")
    args = _args(force_btc_signal="ON", force_eth_signal="OFF", force_gross_usd=150.0)

    cmd_run(args, cfg)

    store = _store(tmp_path)
    last = store.load_last_run()
    assert last is not None
    assert last.force_btc_signal == "ON"
    assert last.force_eth_signal == "OFF"
    assert last.force_gross_usd == pytest.approx(150.0)


def test_no_override_fields_default_when_not_active(tmp_path):
    """When no overrides are passed, persisted run record has COMPUTED defaults."""
    cfg = _cfg(tmp_path, allow_overrides=False, env="dev")
    args = _args()  # all defaults

    cmd_run(args, cfg)

    store = _store(tmp_path)
    last = store.load_last_run()
    assert last is not None
    assert last.force_btc_signal == "COMPUTED"
    assert last.force_eth_signal == "COMPUTED"
    assert last.force_gross_usd is None
