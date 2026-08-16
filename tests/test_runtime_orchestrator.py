from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.exchanges import MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
from src.execution.paper_trading import PortfolioSnapshot
from src.runtime.config import RuntimeConfig
from src.runtime.orchestrator import RuntimeCycleResult, RuntimeOrchestrator
from src.storage.market_store import MarketStore


@pytest.mark.asyncio
async def test_runtime_orchestrator_runs_cycle_and_collects_results(tmp_path: Path) -> None:
    """Test test runtime orchestrator runs cycle and collects results."""
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
    """Test test runtime orchestrator includes account state in health report."""
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
    """Test test runtime orchestrator marks unhealthy when startup checks fail."""
    class FailingConnector:
        """Represent a FailingConnector."""
        name = "failing"

        async def connect(self) -> None:
            """Connect the component to its backing source."""
            raise RuntimeError("connector unavailable")

        async def disconnect(self) -> None:
            """Disconnect the component from its backing source."""
            return None

        async def fetch_snapshot(self) -> None:
            """Fetch a fresh snapshot from the backing source."""
            return None

    pipeline = SimpleNamespace(connectors=[FailingConnector()])
    orchestrator = RuntimeOrchestrator(pipeline=pipeline, mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")

    healthy = await orchestrator.run_startup_checks()

    assert healthy is False
    assert orchestrator.health.healthy is False
    assert orchestrator.health.startup_checks_passed is False


@pytest.mark.asyncio
async def test_runtime_orchestrator_raises_when_watchdog_times_out(tmp_path: Path) -> None:
    """Test test runtime orchestrator raises when watchdog times out."""
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


def test_runtime_orchestrator_emits_stale_data_alert(tmp_path: Path) -> None:
    """A stale market-data bar should trigger a runtime alert."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = RuntimeCycleResult(
        mode="paper",
        bars=[SimpleNamespace(timestamp=datetime.now(timezone.utc) - timedelta(seconds=120))],
        signals=[0.0],
        execution_result=SimpleNamespace(entry_decisions=[], portfolio_history=[], trades=[], orders=[]),
    )

    orchestrator._evaluate_runtime_alerts(
        cycle=cycle,
        account_state_summary={"reconciliation_status": "matched", "reconciliation_mismatches": []},
        execution_result=cycle.execution_result,
    )

    assert "stale_data" in orchestrator._active_alerts


def test_runtime_cycle_summary_reports_entry_and_mark_price(tmp_path: Path) -> None:
    """The runtime health summary should include entry and current prices for the open position."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = RuntimeCycleResult(
        mode="paper",
        bars=[SimpleNamespace(close=110.0)],
        signals=[1.0],
        execution_result=SimpleNamespace(
            entry_decisions=[],
            portfolio_history=[
                PortfolioSnapshot(
                    timestamp=datetime.now(timezone.utc),
                    cash=900.0,
                    position_size=1.0,
                    avg_entry_price=100.0,
                    equity=1010.0,
                    unrealized_pnl=10.0,
                    position_side="long",
                    mark_price=110.0,
                    fees_paid=0.5,
                )
            ],
            trades=[],
            orders=[],
        ),
    )

    summary = orchestrator._build_cycle_summary(cycle)

    assert "entry_price=100.0000" in summary
    assert "mark_price=110.0000" in summary
    assert "realized_pnl=0.0000" in summary
    assert "unrealized_pnl=10.0000" in summary
    assert "total_pnl=10.0000" in summary
    assert "fees_paid=0.5000" in summary


def test_runtime_orchestrator_emits_reconciliation_alert(tmp_path: Path) -> None:
    """A reconciliation mismatch should trigger a runtime alert."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = RuntimeCycleResult(mode="paper", bars=[SimpleNamespace(timestamp=datetime.now(timezone.utc))], signals=[0.0], execution_result=None)

    orchestrator._evaluate_runtime_alerts(
        cycle=cycle,
        account_state_summary={"reconciliation_status": "mismatched", "reconciliation_mismatches": ["order-1"]},
        execution_result=cycle.execution_result,
    )

    assert "reconciliation_mismatch" in orchestrator._active_alerts


def test_runtime_orchestrator_emits_risk_stop_alert(tmp_path: Path) -> None:
    """Blocked entry decisions should trigger a runtime risk-stop alert."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = RuntimeCycleResult(mode="paper", bars=[SimpleNamespace(timestamp=datetime.now(timezone.utc))], signals=[0.0], execution_result=None)
    execution_result = SimpleNamespace(entry_decisions=[{"allowed": False, "reason": "spread_limit"}], portfolio_history=[], trades=[], orders=[])

    orchestrator._evaluate_runtime_alerts(
        cycle=cycle,
        account_state_summary={"reconciliation_status": "matched", "reconciliation_mismatches": []},
        execution_result=execution_result,
    )

    assert "risk_stop" in orchestrator._active_alerts


def test_runtime_orchestrator_emits_heartbeat_alert(tmp_path: Path) -> None:
    """A stale heartbeat should trigger an alert."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    orchestrator.heartbeat_timeout_seconds = 1.0
    orchestrator.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=20)
    cycle = RuntimeCycleResult(mode="paper", bars=[SimpleNamespace(timestamp=datetime.now(timezone.utc))], signals=[0.0], execution_result=None)

    orchestrator._evaluate_runtime_alerts(
        cycle=cycle,
        account_state_summary={"reconciliation_status": "matched", "reconciliation_mismatches": []},
        execution_result=None,
    )

    assert "heartbeat_lost" in orchestrator._active_alerts


def test_runtime_orchestrator_saves_and_loads_checkpoint(tmp_path: Path) -> None:
    """The orchestrator should persist and restore runtime state from a checkpoint file."""
    checkpoint_path = tmp_path / "runtime.state.json"
    orchestrator = RuntimeOrchestrator(
        pipeline=SimpleNamespace(connectors=[]),
        mode="paper",
        kill_switch_state_file=tmp_path / "kill-switch.json",
        runtime_config=RuntimeConfig(mode="paper", strategy_name="momentum_breakout", state_path=checkpoint_path),
        checkpoint_path=checkpoint_path,
    )
    orchestrator.health.cycles_completed = 2
    orchestrator.health.healthy = False
    orchestrator._active_alerts.add("heartbeat_lost")
    orchestrator.last_cycle = RuntimeCycleResult(mode="paper", signals=[1.0], bars=[], execution_result=None)
    orchestrator.save_checkpoint()

    restored = RuntimeOrchestrator(
        pipeline=SimpleNamespace(connectors=[]),
        mode="paper",
        kill_switch_state_file=tmp_path / "kill-switch.json",
        runtime_config=RuntimeConfig(mode="paper", strategy_name="moving_average_crossover", state_path=checkpoint_path),
        checkpoint_path=checkpoint_path,
    )
    restored.load_checkpoint()

    assert restored.health.cycles_completed == 2
    assert restored.health.healthy is False
    assert "heartbeat_lost" in restored._active_alerts
