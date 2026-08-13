from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_runtime_orchestrator_marks_unhealthy_when_startup_checks_fail() -> None:
    class FailingConnector:
        name = "failing"

        async def connect(self) -> None:
            raise RuntimeError("connector unavailable")

        async def disconnect(self) -> None:
            return None

        async def fetch_snapshot(self) -> None:
            return None

    pipeline = SimpleNamespace(connectors=[FailingConnector()])
    orchestrator = RuntimeOrchestrator(pipeline=pipeline, mode="paper")

    healthy = await orchestrator.run_startup_checks()

    assert healthy is False
    assert orchestrator.health.healthy is False
    assert orchestrator.health.startup_checks_passed is False
