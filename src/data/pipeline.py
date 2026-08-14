"""Small orchestration helpers for running the market data pipeline end to end."""

from __future__ import annotations

from typing import Any

from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector
from src.storage.market_store import MarketStore
from src.storage.streaming_aggregator import StreamingAggregator


class MarketDataPipeline:
    """Run connectors against a shared store and streaming aggregator."""

    def __init__(self, store: MarketStore | None = None, interval_seconds: int = 60) -> None:
        """Initialize the object with its runtime state."""
        self.store = store or MarketStore()
        self.aggregator = StreamingAggregator(store=self.store, interval_seconds=interval_seconds)
        self.connectors: list[Any] = []

    def add_connector(self, connector: Any) -> None:
        """Attach a connector to this pipeline."""
        connector.store = self.store
        connector.aggregator = self.aggregator
        self.connectors.append(connector)

    async def run_once(self) -> list[Any]:
        """Fetch one snapshot from each attached connector."""
        results: list[Any] = []
        for connector in self.connectors:
            await connector.connect()
            results.append(await connector.fetch_snapshot())
            await connector.disconnect()
        return results

    def flush_bars(self) -> list[Any]:
        """Finalize any currently open bars and return them."""
        return self.aggregator.flush()
