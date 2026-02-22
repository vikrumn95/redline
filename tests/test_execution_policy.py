"""Unit tests: LiveExecutionPolicy (Phase 2)."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from redline.core.store import Store
from redline.core.types import OrderSide, PlannedOrder, SubmittedOrder
from redline.execution.errors import ExecutionHaltError
from redline.execution.policy import DryRunPolicy, LiveExecutionPolicy
from redline.logging import RunLogger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def _logger() -> RunLogger:
    return RunLogger("test_run", "DEBUG")


def _buy(product_id: str = "BTC-USD") -> PlannedOrder:
    return PlannedOrder(
        product_id=product_id,
        side=OrderSide.BUY,
        base_size=None,
        quote_size_usd=100.0,
        reason="test_buy",
    )


def _sell(product_id: str = "ETH-USD") -> PlannedOrder:
    return PlannedOrder(
        product_id=product_id,
        side=OrderSide.SELL,
        base_size=0.05,
        quote_size_usd=None,
        reason="test_sell",
    )


class FakeBroker:
    """Duck-typed broker test double."""

    def __init__(
        self,
        poll_responses: list[dict] | None = None,
        raise_on_submit: bool = False,
    ) -> None:
        self.orders_submitted: list[SubmittedOrder] = []
        self._poll_responses = poll_responses or [
            {"status": "FILLED", "last_fill_time": "2025-01-01T00:00:00Z"}
        ]
        self._raise_on_submit = raise_on_submit
        self._poll_calls = 0

    def submit_order(self, run_id: str, order: PlannedOrder) -> SubmittedOrder:
        if self._raise_on_submit:
            raise RuntimeError("simulated submit failure")
        so = SubmittedOrder(
            order_id=f"fake_{len(self.orders_submitted)}",
            client_order_id=uuid.uuid4().hex,
            run_id=run_id,
            product_id=order.product_id,
            side=order.side,
            base_size=order.base_size,
            quote_size_usd=order.quote_size_usd,
            status="pending",
            submitted_at="2025-01-01T00:00:00+00:00",
        )
        self.orders_submitted.append(so)
        return so

    def get_order(self, order_id: str) -> dict:
        idx = min(self._poll_calls, len(self._poll_responses) - 1)
        resp = self._poll_responses[idx]
        self._poll_calls += 1
        return resp


# ---------------------------------------------------------------------------
# DryRunPolicy — still exercised to guard against regression
# ---------------------------------------------------------------------------


def test_dry_run_policy_no_orders():
    policy = DryRunPolicy(_logger())
    assert policy.execute([]) == []


def test_dry_run_policy_returns_orders():
    orders = [_buy(), _sell()]
    policy = DryRunPolicy(_logger())
    assert policy.execute(orders) == orders


# ---------------------------------------------------------------------------
# LiveExecutionPolicy — safety gate
# ---------------------------------------------------------------------------


def test_live_policy_dry_run_gate(tmp_path):
    """Must raise AssertionError if called with dry_run=True."""
    policy = LiveExecutionPolicy(
        store=_store(tmp_path),
        broker=FakeBroker(),
        run_id="r1",
        logger=_logger(),
        dry_run=True,
    )
    with pytest.raises(AssertionError):
        policy.execute([_buy()])


# ---------------------------------------------------------------------------
# Empty orders
# ---------------------------------------------------------------------------


def test_live_empty_orders_returns_empty(tmp_path):
    policy = LiveExecutionPolicy(
        store=_store(tmp_path),
        broker=FakeBroker(),
        run_id="r1",
        logger=_logger(),
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )
    assert policy.execute([]) == []


# ---------------------------------------------------------------------------
# Happy path — BUY and SELL
# ---------------------------------------------------------------------------


def test_happy_path_buy(tmp_path):
    store = _store(tmp_path)
    broker = FakeBroker(
        poll_responses=[{"status": "FILLED", "last_fill_time": "2025-01-01T00:00:00Z"}]
    )
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        timeout_s=60.0,
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    result = policy.execute([_buy()])

    assert len(result) == 1
    assert result[0].status == "filled"
    assert result[0].product_id == "BTC-USD"
    assert result[0].side == OrderSide.BUY

    # Persisted and updated in DB
    db_orders = store.load_orders_for_run("r1")
    assert len(db_orders) == 1
    assert db_orders[0].status == "filled"
    assert db_orders[0].filled_at == "2025-01-01T00:00:00Z"


def test_happy_path_sell(tmp_path):
    store = _store(tmp_path)
    broker = FakeBroker(
        poll_responses=[{"status": "FILLED", "last_fill_time": "2025-01-01T00:00:00Z"}]
    )
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        timeout_s=60.0,
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    result = policy.execute([_sell()])

    assert len(result) == 1
    assert result[0].status == "filled"
    assert result[0].side == OrderSide.SELL

    db_orders = store.load_orders_for_run("r1")
    assert db_orders[0].status == "filled"


# ---------------------------------------------------------------------------
# Two orders — both filled
# ---------------------------------------------------------------------------


def test_two_orders_both_filled(tmp_path):
    store = _store(tmp_path)
    broker = FakeBroker(
        poll_responses=[{"status": "FILLED", "last_fill_time": "2025-01-01T00:00:00Z"}]
    )
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        timeout_s=60.0,
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    result = policy.execute([_buy("BTC-USD"), _sell("ETH-USD")])

    assert len(result) == 2
    assert all(r.status == "filled" for r in result)

    db_orders = store.load_orders_for_run("r1")
    assert len(db_orders) == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_skips_existing_order(tmp_path):
    """If an order for run_id+product_id already exists, skip the submit."""
    store = _store(tmp_path)

    # Pre-insert an order as if a previous (possibly crashed) attempt submitted it
    store.save_order(
        SubmittedOrder(
            order_id="preexisting",
            client_order_id=uuid.uuid4().hex,
            run_id="r1",
            product_id="BTC-USD",
            side=OrderSide.BUY,
            base_size=None,
            quote_size_usd=100.0,
            status="filled",
            submitted_at="2025-01-01T00:00:00+00:00",
        )
    )

    broker = FakeBroker()
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    result = policy.execute([_buy()])

    assert result == []
    assert len(broker.orders_submitted) == 0  # submit_order was never called


# ---------------------------------------------------------------------------
# Submit failure → ExecutionHaltError
# ---------------------------------------------------------------------------


def test_submit_failure_raises_halt(tmp_path):
    broker = FakeBroker(raise_on_submit=True)
    policy = LiveExecutionPolicy(
        store=_store(tmp_path),
        broker=broker,
        run_id="r1",
        logger=_logger(),
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    with pytest.raises(ExecutionHaltError, match="submit_failed"):
        policy.execute([_buy()])


# ---------------------------------------------------------------------------
# Timeout → ExecutionHaltError + order persisted with status=timeout
# ---------------------------------------------------------------------------


def test_fill_timeout_raises_halt(tmp_path):
    """timeout_s=0.0, _clock=lambda: 0.0 → deadline=0.0, remaining=0.0 → immediate timeout."""
    store = _store(tmp_path)
    broker = FakeBroker(poll_responses=[{"status": "OPEN"}])
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        timeout_s=0.0,
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    with pytest.raises(ExecutionHaltError, match="timeout"):
        policy.execute([_buy()])

    # Order was submitted and persisted before polling started
    db_orders = store.load_orders_for_run("r1")
    assert len(db_orders) == 1
    assert db_orders[0].status == "timeout"


# ---------------------------------------------------------------------------
# FAILED / CANCELLED order → ExecutionHaltError + status=failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["FAILED", "CANCELLED", "EXPIRED"])
def test_order_failed_status_raises_halt(tmp_path, terminal_status):
    store = _store(tmp_path)
    broker = FakeBroker(poll_responses=[{"status": terminal_status}])
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        timeout_s=60.0,
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    with pytest.raises(ExecutionHaltError, match="order_failed"):
        policy.execute([_buy()])

    db_orders = store.load_orders_for_run("r1")
    assert db_orders[0].status == "failed"


# ---------------------------------------------------------------------------
# Crash-safe: order persisted before polling even if poll raises
# ---------------------------------------------------------------------------


def test_order_persisted_before_poll(tmp_path):
    """Even if the first poll call raises, the order must already be in the DB."""
    store = _store(tmp_path)

    call_count = 0

    class PollingErrorBroker(FakeBroker):
        def get_order(self, order_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network blip")
            return {"status": "FILLED", "last_fill_time": "2025-01-01T00:00:00Z"}

    broker = PollingErrorBroker()
    policy = LiveExecutionPolicy(
        store=store,
        broker=broker,
        run_id="r1",
        logger=_logger(),
        timeout_s=60.0,
        _sleep=lambda _: None,
        _clock=lambda: 0.0,
    )

    result = policy.execute([_buy()])
    assert result[0].status == "filled"
    # Two poll calls: first raised, second returned FILLED
    assert call_count == 2


# ---------------------------------------------------------------------------
# order_exists_for_run store helper
# ---------------------------------------------------------------------------


def test_order_exists_for_run_true(tmp_path):
    store = _store(tmp_path)
    store.save_order(
        SubmittedOrder(
            order_id="o1",
            client_order_id="c1",
            run_id="r1",
            product_id="BTC-USD",
            side=OrderSide.BUY,
            base_size=None,
            quote_size_usd=100.0,
            status="pending",
            submitted_at="2025-01-01T00:00:00+00:00",
        )
    )
    assert store.order_exists_for_run("r1", "BTC-USD") is True
    assert store.order_exists_for_run("r1", "ETH-USD") is False
    assert store.order_exists_for_run("r2", "BTC-USD") is False


def test_update_order_status(tmp_path):
    store = _store(tmp_path)
    store.save_order(
        SubmittedOrder(
            order_id="o1",
            client_order_id="c1",
            run_id="r1",
            product_id="BTC-USD",
            side=OrderSide.BUY,
            base_size=None,
            quote_size_usd=100.0,
            status="pending",
            submitted_at="2025-01-01T00:00:00+00:00",
        )
    )
    store.update_order_status("o1", "filled", "2025-01-02T00:00:00Z")
    orders = store.load_orders_for_run("r1")
    assert orders[0].status == "filled"
    assert orders[0].filled_at == "2025-01-02T00:00:00Z"
