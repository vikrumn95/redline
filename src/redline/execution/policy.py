"""Execution policies: dry-run (Phase 1) and live (Phase 2)."""
from __future__ import annotations

import time
from typing import Any, Callable

from redline.core.types import PlannedOrder, SubmittedOrder
from redline.execution.errors import ExecutionHaltError
from redline.logging import RunLogger


class DryRunPolicy:
    """Logs planned orders to structured output without submitting them."""

    def __init__(self, logger: RunLogger) -> None:
        self._logger = logger

    def execute(self, orders: list[PlannedOrder]) -> list[PlannedOrder]:
        """Log all planned orders and return them unchanged."""
        if not orders:
            self._logger.info("dry_run_no_orders")
            return []

        for order in orders:
            self._logger.info(
                "dry_run_planned_order",
                product_id=order.product_id,
                side=order.side.value,
                base_size=order.base_size,
                quote_size_usd=order.quote_size_usd,
                reason=order.reason,
            )

        return orders


class LiveExecutionPolicy:
    """Submits planned orders to Coinbase and polls until filled or timeout.

    Safety gate: raises AssertionError if dry_run=True is passed.
    Raises ExecutionHaltError on submit failure, fill failure, or timeout.

    The _sleep and _clock callables are injectable for testing:
        _sleep=lambda _: None   — skip real waiting
        _clock=lambda: 0.0      — freeze time (use timeout_s=0.0 to force immediate timeout)
    """

    def __init__(
        self,
        store: Any,      # Store — typed Any to allow FakeBroker test doubles
        broker: Any,     # CoinbaseSpotBroker or duck-typed test double
        run_id: str,
        logger: RunLogger,
        dry_run: bool = False,
        timeout_s: float = 60.0,
        _sleep: Callable[[float], None] = time.sleep,
        _clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._broker = broker
        self._run_id = run_id
        self._logger = logger
        self._dry_run = dry_run
        self._timeout_s = timeout_s
        self._sleep = _sleep
        self._clock = _clock

    def execute(self, orders: list[PlannedOrder]) -> list[SubmittedOrder]:
        """Submit each order, persist crash-safely, then poll until filled."""
        if self._dry_run:
            raise AssertionError("LiveExecutionPolicy must not be called in dry-run mode")

        if not orders:
            self._logger.info("live_no_orders")
            return []

        submitted: list[SubmittedOrder] = []

        for order in orders:
            # Two-layer idempotency: SQLite pre-check before every POST
            if self._store.order_exists_for_run(self._run_id, order.product_id):
                self._logger.warning(
                    "order_skipped_idempotent",
                    run_id=self._run_id,
                    product_id=order.product_id,
                )
                continue

            # Submit to exchange
            try:
                sub = self._broker.submit_order(self._run_id, order)
            except Exception as exc:
                self._logger.error(
                    "order_submit_failed",
                    product_id=order.product_id,
                    error=str(exc),
                )
                raise ExecutionHaltError(f"submit_failed:{order.product_id}") from exc

            # Crash-safe: persist immediately after successful POST
            self._store.save_order(sub)
            submitted.append(sub)
            self._logger.info(
                "order_submitted",
                order_id=sub.order_id,
                product_id=sub.product_id,
                side=sub.side.value,
            )

            # Poll for fill
            deadline = self._clock() + self._timeout_s
            poll_interval = 2.0

            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._store.update_order_status(sub.order_id, "timeout")
                    sub.status = "timeout"
                    self._logger.warning("order_timeout", order_id=sub.order_id)
                    raise ExecutionHaltError(f"order_timeout:{sub.order_id}")

                self._sleep(min(poll_interval, remaining))

                try:
                    result = self._broker.get_order(sub.order_id)
                except Exception as exc:
                    self._logger.warning(
                        "order_poll_error", order_id=sub.order_id, error=str(exc)
                    )
                    continue

                status = result.get("status", "")

                if status == "FILLED":
                    from redline.timeutil import utcnow_iso

                    filled_at = result.get("last_fill_time") or utcnow_iso()
                    self._store.update_order_status(sub.order_id, "filled", filled_at)
                    sub.status = "filled"
                    sub.filled_at = filled_at
                    self._logger.info(
                        "order_filled",
                        order_id=sub.order_id,
                        product_id=sub.product_id,
                    )
                    break

                if status in ("FAILED", "CANCELLED", "EXPIRED"):
                    self._store.update_order_status(sub.order_id, "failed")
                    sub.status = "failed"
                    self._logger.error(
                        "order_failed", order_id=sub.order_id, status=status
                    )
                    raise ExecutionHaltError(f"order_failed:{sub.order_id}:{status}")

                # Still OPEN/PENDING — back off and retry
                poll_interval = min(poll_interval * 1.5, 30.0)

        return submitted
