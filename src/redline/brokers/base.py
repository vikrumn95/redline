"""Abstract broker interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

from redline.core.types import Balance


class BrokerBase(ABC):
    @abstractmethod
    def fetch_balances(self) -> dict[str, Balance]:
        """Return {asset: Balance} for all accounts."""
        ...
