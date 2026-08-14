from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.exchanges import MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
from src.runtime.orchestrator import RuntimeOrchestrator
from src.storage.market_store import MarketStore


@pytest.mark.asyncio
async def test_runtime_orchestrator_runs_cycle_and_collects_results(tmp_path: Path) -> None:
    store = MarketStore(database_path="/tmp/cryptoquantmft-runtime-test.db")
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)
    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    orchestrator = RuntimeOrchestrator(pipeline=pipeline, mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = await orchestrator.run_once()

    assert cycle.mode == "paper"
    assert len(cycle.snapshots) == 1
    assert len(cycle.bars) == 1
    assert len(cycle.signals) == 1
    assert orchestrator.last_cycle is cycle
    assert len(orchestrator.history) == 1


@pytest.mark.asyncio
async def test_runtime_orchestrator_includes_account_state_in_health_report(tmp_path: Path) -> None:
    store = MarketStore(database_path="/tmp/cryptoquantmft-runtime-health-test.db")
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)
    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    orchestrator = RuntimeOrchestrator(pipeline=pipeline, mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    await orchestrator.run_once()
    health_report = orchestrator.get_health_report()

    assert health_report["account_state"]["balances"]
    assert "positions" in health_report["account_state"]


@pytest.mark.asyncio
async def test_runtime_orchestrator_marks_unhealthy_when_startup_checks_fail(tmp_path: Path) -> None:
    class FailingConnector:
        name = "failing"

        async def connect(self) -> None:
            raise RuntimeError("connector unavailable")

        async def disconnect(self) -> None:
            return None

        async def fetch_snapshot(self) -> None:
            return None

    pipeline = SimpleNamespace(connectors=[FailingConnector()])
    orchestrator = RuntimeOrchestrator(pipeline=pipeline, mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")

    healthy = await orchestrator.run_startup_checks()

    assert healthy is False
    assert orchestrator.health.healthy is False
    assert orchestrator.health.startup_checks_passed is False


@pytest.mark.asyncio
async def test_runtime_orchestrator_raises_when_watchdog_times_out(tmp_path: Path) -> None:
    store = MarketStore(database_path="/tmp/cryptoquantmft-watchdog-test.db")
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)
    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    orchestrator = RuntimeOrchestrator(
        pipeline=pipeline,
        mode="paper",
        watchdog_timeout_seconds=0.001,
        kill_switch_state_file=tmp_path / "kill-switch.json",
    )
    await orchestrator.run_startup_checks()
    orchestrator.watchdog.last_cycle_at = time.monotonic() - 10.0
    orchestrator.watchdog.last_data_at = time.monotonic() - 10.0

    with pytest.raises(RuntimeError, match="watchdog timeout exceeded"):
        await orchestrator.run_loop(iterations=1, interval_seconds=0.0)

    assert orchestrator.health.watchdog_triggered is True
