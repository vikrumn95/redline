"""Coinbase Advanced Trade spot broker (read-only for Phase 1)."""
from __future__ import annotations

from redline.brokers.base import BrokerBase
from redline.core.types import Balance
from redline.data.providers.coinbase import CoinbaseDataProvider


class CoinbaseSpotBroker(BrokerBase):
    def __init__(self, api_key: str, api_secret: str, mock: bool = False) -> None:
        self._provider = CoinbaseDataProvider(api_key, api_secret, mock)

    def fetch_balances(self) -> dict[str, Balance]:
        return self._provider.fetch_balances()
