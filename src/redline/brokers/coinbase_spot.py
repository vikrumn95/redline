"""Coinbase Advanced Trade spot broker."""
from __future__ import annotations

from redline.brokers.base import BrokerBase
from redline.core.types import Balance, PlannedOrder, SubmittedOrder
from redline.data.providers.coinbase import CoinbaseDataProvider


class CoinbaseSpotBroker(BrokerBase):
    def __init__(self, api_key: str, api_secret: str, mock: bool = False) -> None:
        self._provider = CoinbaseDataProvider(api_key, api_secret, mock)

    def fetch_balances(self) -> dict[str, Balance]:
        return self._provider.fetch_balances()

    def submit_order(self, run_id: str, order: PlannedOrder) -> SubmittedOrder:
        return self._provider.submit_order(run_id, order)

    def get_order(self, order_id: str) -> dict:
        return self._provider.get_order(order_id)
