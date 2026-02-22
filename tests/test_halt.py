"""Unit tests: hard-gating HALT, reset-halt CLI, halt_events audit trail."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from redline.config import Config
from redline.core.store import Store
from redline.main import cmd_reset_halt, cmd_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def _cfg(tmp_path: Path) -> Config:
    return Config(
        coinbase_api_key="",
        coinbase_api_secret="",
        coinbase_portfolio_id=None,
        dry_run=True,
        db_path=tmp_path / "test.db",
        log_level="DEBUG",
        env="test",
        rebalance_time_utc="00:00",
        target_vol=0.25,
        max_drawdown=0.30,
        max_daily_loss=0.05,
        min_trade_usd=50.0,
        max_gross_notional=1.0,
        max_per_asset_notional=0.70,
        slack_webhook_url=None,
        mock=True,
    )


# ---------------------------------------------------------------------------
# Test 1: Hard-gating — halted bot must exit before candle fetch
# ---------------------------------------------------------------------------


def test_hard_gating_exits_without_fetching_candles(tmp_path):
    """If halt_flag is set, cmd_run must exit 0 and never call fetch_candles or fetch_balances."""
    store = _store(tmp_path)
    store.set_halt("drawdown_exceeded(35.00%)", run_id="prior_run")

    cfg = _cfg(tmp_path)
    args = argparse.Namespace(dry_run=True, mock=True)

    mock_provider = MagicMock()
    mock_broker = MagicMock()

    with (
        patch("redline.main.Store", return_value=store),
        patch("redline.main.CoinbaseDataProvider", return_value=mock_provider),
        patch("redline.main.CoinbaseSpotBroker", return_value=mock_broker),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_run(args, cfg)

    assert exc_info.value.code == 0
    mock_provider.fetch_candles.assert_not_called()
    mock_broker.fetch_balances.assert_not_called()


def test_hard_gating_saves_skipped_run_record(tmp_path):
    """Hard-gated run must persist a SKIPPED run record with skip_reason='HALTED'."""
    from redline.core.types import RunStatus

    store = _store(tmp_path)
    store.set_halt("daily_loss_exceeded(6.00%)", run_id="prior_run")

    cfg = _cfg(tmp_path)
    args = argparse.Namespace(dry_run=True, mock=True)

    with (
        patch("redline.main.Store", return_value=store),
        patch("redline.main.CoinbaseDataProvider"),
        patch("redline.main.CoinbaseSpotBroker"),
        pytest.raises(SystemExit),
    ):
        cmd_run(args, cfg)

    last = store.load_last_run()
    assert last is not None
    assert last.status == RunStatus.SKIPPED
    assert last.skip_reason == "HALTED"
    assert last.halt_flag is True
    assert last.halt_reason == "daily_loss_exceeded(6.00%)"


# ---------------------------------------------------------------------------
# Test 2: reset-halt when NOT halted — no DB changes
# ---------------------------------------------------------------------------


def test_reset_halt_when_not_halted(tmp_path):
    """reset-halt on a non-halted bot prints 'Not halted' and writes no halt_events row."""
    store = _store(tmp_path)
    cfg = _cfg(tmp_path)
    args = argparse.Namespace(reason="precautionary clear", require_reconcile=False, mock=True)

    with (
        patch("redline.main.Store", return_value=store),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_reset_halt(args, cfg)

    assert exc_info.value.code == 0
    events = store.load_halt_events()
    assert events == []


# ---------------------------------------------------------------------------
# Test 3: reset-halt when halted — clears halt + inserts HALT_RESET
# ---------------------------------------------------------------------------


def test_reset_halt_when_halted(tmp_path):
    """reset-halt on a halted bot clears halt state and inserts a HALT_RESET audit row."""
    store = _store(tmp_path)
    store.set_halt("drawdown_exceeded(35.00%)", run_id="r1")

    cfg = _cfg(tmp_path)
    args = argparse.Namespace(reason="manually reviewed, OK to resume", require_reconcile=False, mock=True)

    with patch("redline.main.Store", return_value=store):
        cmd_reset_halt(args, cfg)

    halt_flag, halt_reason, _ = store.get_halt_state_full()
    assert halt_flag is False
    assert halt_reason is None

    events = store.load_halt_events()
    reset_events = [e for e in events if e["event_type"] == "HALT_RESET"]
    assert len(reset_events) == 1
    assert reset_events[0]["reason"] == "manually reviewed, OK to resume"


# ---------------------------------------------------------------------------
# Test 4: reset-halt --require-reconcile with failing reconcile
# ---------------------------------------------------------------------------


def test_reset_halt_require_reconcile_failure(tmp_path):
    """When --require-reconcile and reconcile fails: halt stays, no HALT_RESET, exit code 2."""
    store = _store(tmp_path)
    store.set_halt("drawdown_exceeded(35.00%)", run_id="r1")

    cfg = _cfg(tmp_path)
    args = argparse.Namespace(reason="clear after review", require_reconcile=True, mock=True)

    mock_provider = MagicMock()
    mock_provider.fetch_candles.side_effect = RuntimeError("connection refused")

    with (
        patch("redline.main.Store", return_value=store),
        patch("redline.main.CoinbaseDataProvider", return_value=mock_provider),
        patch("redline.main.CoinbaseSpotBroker"),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_reset_halt(args, cfg)

    assert exc_info.value.code == 2

    # Halt must remain set
    halt_flag, _, _ = store.get_halt_state_full()
    assert halt_flag is True

    # No HALT_RESET should have been written
    events = store.load_halt_events()
    reset_events = [e for e in events if e["event_type"] == "HALT_RESET"]
    assert reset_events == []


# ---------------------------------------------------------------------------
# Test 5: set_halt writes a HALT_TRIGGERED row to halt_events
# ---------------------------------------------------------------------------


def test_set_halt_writes_halt_triggered_audit_row(tmp_path):
    """Every store.set_halt() call must insert a HALT_TRIGGERED row into halt_events."""
    store = _store(tmp_path)
    store.set_halt("drawdown_exceeded(35.00%)", run_id="abc123")

    events = store.load_halt_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "HALT_TRIGGERED"
    assert events[0]["reason"] == "drawdown_exceeded(35.00%)"
    assert events[0]["run_id"] == "abc123"


def test_multiple_halts_and_reset_audit_trail(tmp_path):
    """Full audit trail: HALT_TRIGGERED then HALT_RESET are both recorded in order."""
    store = _store(tmp_path)

    store.set_halt("daily_loss_exceeded(6.00%)", run_id="r1")

    args = argparse.Namespace(reason="reviewed and cleared", require_reconcile=False, mock=True)
    cfg = _cfg(tmp_path)
    with patch("redline.main.Store", return_value=store):
        cmd_reset_halt(args, cfg)

    events = store.load_halt_events()
    # newest first
    assert events[0]["event_type"] == "HALT_RESET"
    assert events[1]["event_type"] == "HALT_TRIGGERED"
    assert len(events) == 2
