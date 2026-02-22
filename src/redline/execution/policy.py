"""Execution policy: dry-run (Phase 1 — no live order submission)."""
from __future__ import annotations

from redline.core.types import PlannedOrder
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
