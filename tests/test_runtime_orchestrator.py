from __future__ import annotations

import pytest

from src.data.exchanges import MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
from src.runtime.orchestrator import RuntimeOrchestrator
from src.storage.market_store import MarketStore


@pytest.mark.asyncio
async def test_runtime_orchestrator_runs_cycle_and_collects_results() -> None:
    store = MarketStore(database_path="/tmp/cryptoquantmft-runtime-test.db")
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)
    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    orchestrator = RuntimeOrchestrator(pipeline=pipeline, mode="paper")
    cycle = await orchestrator.run_once()

    assert cycle.mode == "paper"
    assert len(cycle.snapshots) == 1
    assert len(cycle.bars) == 1
    assert len(cycle.signals) == 1
    assert orchestrator.last_cycle is cycle
    assert len(orchestrator.history) == 1
