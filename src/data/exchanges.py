"""Exchange connector abstractions for live market data ingestion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class MarketTick:
    """Normalized market tick structure for downstream consumers."""

    exchange: str
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float | None = None
    volume: float | None = None
    raw: dict[str, Any] | None = None


class ExchangeConnector(ABC):
    """Base interface for exchange-specific live data adapters."""

    def __init__(self, name: str, symbol: str) -> None:
        self.name = name
        self.symbol = symbol

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection to the exchange feed."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the exchange connection cleanly."""

    @abstractmethod
    async def fetch_snapshot(self) -> MarketTick:
        """Fetch a single normalized market snapshot from the exchange."""


class MockExchangeConnector(ExchangeConnector):
    """Simple in-memory connector used for offline development and tests."""

    def __init__(self, symbol: str = "BTC/NOK") -> None:
        super().__init__(name="mock", symbol=symbol)
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def fetch_snapshot(self) -> MarketTick:
        if not self._connected:
            raise RuntimeError("connector is not connected")

        now = datetime.now(timezone.utc)
        return MarketTick(
            exchange=self.name,
            symbol=self.symbol,
            timestamp=now,
            bid=100.0,
            ask=101.0,
            last=100.5,
            volume=1.25,
            raw={"source": "mock"},
        )
