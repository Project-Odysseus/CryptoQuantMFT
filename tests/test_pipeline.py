"""Tests for the market data pipeline orchestration helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.data.exchanges import MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
from src.storage.market_store import MarketStore


@pytest.mark.asyncio
async def test_pipeline_runs_connectors_and_flushes_bars(tmp_path: Path) -> None:
    """The pipeline should run connected connectors and expose their aggregated bars."""
    store = MarketStore(database_path=tmp_path / "pipeline.db")
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)
    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    snapshots = await pipeline.run_once()
    flushed = pipeline.flush_bars()

    assert len(snapshots) == 1
    assert snapshots[0].symbol == "BTC/NOK"
    assert len(flushed) == 1
