from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from main import build_runtime_orchestrator
from config import settings
from src.data.exchanges import MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
from src.execution.adapters import KrakenExecutionAdapter
from src.execution.paper_trading import PortfolioSnapshot
from src.runtime.config import RuntimeConfig
from src.runtime.orchestrator import RuntimeCycleResult, RuntimeOrchestrator
from src.storage.market_store import MarketStore
from src.storage.trade_logger import TradeLogger


def test_build_runtime_orchestrator_uses_runtime_interval_for_market_data_pipeline(tmp_path: Path) -> None:
    """The runtime pipeline should use the configured interval for bar aggregation."""
    runtime_config = RuntimeConfig(interval_seconds=3.0, use_mock_connector=True, trading_symbol="ETH/EUR", state_path=tmp_path / "runtime.state.json")

    orchestrator, pipeline = build_runtime_orchestrator(config=runtime_config, mode="paper")

    assert orchestrator.interval_seconds == 3.0
    assert pipeline.aggregator.interval_seconds == 3
    assert orchestrator.trading_symbol == "ETH/EUR"
    assert orchestrator.account_state_tracker.base_currency == "EUR"


def test_build_runtime_orchestrator_defaults_live_dry_run_to_kraken_exchange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live dry run should default to Kraken-shaped adapter state when no exchange is explicitly chosen."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")
    runtime_config = RuntimeConfig(mode="live_dry_run", use_mock_connector=True, state_path=tmp_path / "runtime.state.json")

    orchestrator, pipeline = build_runtime_orchestrator(config=runtime_config, mode="live_dry_run")

    assert pipeline.connectors[0].symbol == "BTC/EUR"
    assert orchestrator.execution_engine.execution_adapter is not None
    assert orchestrator.execution_engine.execution_adapter.name == "sandbox"
    assert orchestrator.execution_engine.execution_adapter.exchange_name == "kraken"
    assert orchestrator.execution_engine.exchange_name == "kraken"
    assert orchestrator.trading_symbol == "BTC/EUR"
    assert orchestrator.get_health_report()["account_state"]["exchange"] == "kraken"


def test_build_runtime_orchestrator_falls_back_to_kraken_dry_run_when_firi_credentials_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dry-run lane should align adapter and market-data exchange after Firi fallback."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")
    monkeypatch.setattr(settings, "firi_api_key", "")
    runtime_config = RuntimeConfig(mode="live_dry_run", exchange="firi", state_path=tmp_path / "runtime.state.json")

    orchestrator, pipeline = build_runtime_orchestrator(config=runtime_config, mode="live_dry_run")

    assert getattr(pipeline.connectors[0], "name", None) == "kraken"
    assert pipeline.connectors[0].symbol == "BTC/EUR"
    assert orchestrator.execution_engine.execution_adapter is not None
    assert orchestrator.execution_engine.execution_adapter.exchange_name == "kraken"


def test_build_runtime_orchestrator_promotes_paper_to_live_dry_run_without_live_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promotion to live_dry_run should add exchange-shaped dry-run routing without enabling live execution."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")
    paper_config = RuntimeConfig(mode="paper", exchange="kraken", use_mock_connector=True, state_path=tmp_path / "paper.state.json")
    dry_run_config = RuntimeConfig(mode="live_dry_run", exchange="kraken", use_mock_connector=True, state_path=tmp_path / "dry.state.json")

    paper_orchestrator, _paper_pipeline = build_runtime_orchestrator(config=paper_config, mode="paper")
    dry_run_orchestrator, _dry_run_pipeline = build_runtime_orchestrator(config=dry_run_config, mode="live_dry_run")

    assert paper_orchestrator.execution_engine.execution_adapter is None
    assert dry_run_orchestrator.execution_engine.execution_adapter is not None
    assert dry_run_orchestrator.execution_engine.execution_adapter.name == "sandbox"
    assert dry_run_orchestrator.mode == "live_dry_run"


@pytest.mark.asyncio
async def test_runtime_orchestrator_live_dry_run_tracks_exchange_account_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live dry run should persist exchange-shaped adapter state without enabling live execution."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")
    runtime_config = RuntimeConfig(
        mode="live_dry_run",
        exchange="kraken",
        use_mock_connector=True,
        trading_symbol="BTC/EUR",
        state_path=tmp_path / "runtime.state.json",
    )
    orchestrator, _pipeline = build_runtime_orchestrator(config=runtime_config, mode="live_dry_run")

    cycle = await orchestrator.run_once()
    health_report = orchestrator.get_health_report()

    assert cycle.mode == "live_dry_run"
    assert orchestrator.execution_engine.execution_adapter is not None
    assert health_report["account_state"]["exchange"] == "kraken"
    assert health_report["account_state"]["base_currency"] == "EUR"
    assert health_report["account_state"]["balances"]["EUR"] >= 0.0
    assert health_report["account_state"]["reconciliation_status"] == "matched"


@pytest.mark.asyncio
async def test_runtime_orchestrator_live_dry_run_records_order_rejection_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run rejection paths should persist order context instead of placing live trades."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")
    runtime_config = RuntimeConfig(
        mode="live_dry_run",
        exchange="kraken",
        use_mock_connector=True,
        trading_symbol="BTC/EUR",
        state_path=tmp_path / "runtime.state.json",
    )
    orchestrator, _pipeline = build_runtime_orchestrator(config=runtime_config, mode="live_dry_run")

    class RejectingAdapter:
        name = "sandbox"
        exchange_name = "kraken"

        def submit_order(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(status="REJECTED", message="risk gate rejected order", fill_price=None, filled_size=None, fee=0.0)

        def list_orders(self) -> list[SimpleNamespace]:
            return []

        def get_account_snapshot(self) -> dict[str, object]:
            return {"balances": {"EUR": 1000.0}, "positions": {}}

        def get_order_status(self, *, order_id: str) -> SimpleNamespace:
            return SimpleNamespace(status="REJECTED", fill_price=None, filled_size=None, fee=0.0, message="rejected")

        def reconcile_account_state(self, *, balances=None, positions=None) -> dict[str, object]:
            return {
                "matched": True,
                "balance_mismatches": {},
                "position_mismatches": {},
                "remote_balances": balances or {"EUR": 1000.0},
                "remote_positions": positions or {},
                "merged_balances": balances or {"EUR": 1000.0},
                "merged_positions": positions or {},
            }

        def recover_execution_state(self, *, remote_snapshot=None, remote_orders=None) -> dict[str, object]:
            return {
                "account_reconciliation": self.reconcile_account_state(
                    balances=(remote_snapshot or {}).get("balances"),
                    positions=(remote_snapshot or {}).get("positions"),
                ),
                "recovered_order_ids": [],
                "recovered_order_count": 0,
                "recovery_status": "idle",
            }

    orchestrator.execution_engine.execution_adapter = RejectingAdapter()
    orchestrator.execution_engine.risk_manager = None
    orchestrator.strategy = lambda history, index, current_bar: 1.0

    await orchestrator.run_once()
    report = orchestrator.get_operational_report()

    assert report["runtime"]["mode"] == "live_dry_run"
    assert report["latest_cycle"]["latest_order"]["status"] == "CANCELED"
    assert report["latest_cycle"]["latest_order"]["reason"] == "risk gate rejected order"
    assert report["reconciliation"]["status"] == "matched"


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
    assert "size=1.000000" in summary
    assert "notional=110.0000" in summary
    assert "realized_pnl=0.0000" in summary
    assert "unrealized_pnl=10.0000" in summary
    assert "total_pnl=10.0000" in summary
    assert "fees_paid=0.5000" in summary


def test_runtime_orchestrator_refreshes_latest_snapshot_prices_from_latest_market_snapshot(tmp_path: Path) -> None:
    """The latest portfolio snapshot should be revalued from the newest market snapshot instead of stale bar data."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = RuntimeCycleResult(
        mode="paper",
        snapshots=[SimpleNamespace(last=100.0), SimpleNamespace(last=120.0)],
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

    orchestrator._refresh_latest_snapshot_prices(cycle)
    snapshot = cycle.execution_result.portfolio_history[-1]

    assert snapshot.mark_price == 120.0
    assert snapshot.equity == 1020.0
    assert snapshot.unrealized_pnl == 20.0


def test_runtime_cycle_summary_reports_data_source_and_risk_details(tmp_path: Path) -> None:
    """The health summary should surface the active data source and the latest risk gate metrics."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[SimpleNamespace(name="kraken")]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    cycle = RuntimeCycleResult(
        mode="paper",
        bars=[SimpleNamespace(close=110.0)],
        signals=[1.0],
        execution_result=SimpleNamespace(
            entry_decisions=[{"allowed": False, "reason": "volatility_limit", "volatility_pct": 0.0123, "max_volatility_pct": 0.01}],
            portfolio_history=[
                PortfolioSnapshot(
                    timestamp=datetime.now(timezone.utc),
                    cash=1000.0,
                    position_size=0.0,
                    avg_entry_price=None,
                    equity=1000.0,
                    unrealized_pnl=0.0,
                    position_side="flat",
                    mark_price=110.0,
                    fees_paid=0.0,
                )
            ],
            trades=[],
            orders=[],
        ),
    )

    summary = orchestrator._build_cycle_summary(cycle)

    assert "data_source: kraken" in summary
    assert "risk_volatility=0.0123/0.0100" in summary


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


def test_runtime_operational_report_surfaces_latest_debug_context(tmp_path: Path) -> None:
    """The operational report should expose the latest bar, signal, decision, and order context."""
    orchestrator = RuntimeOrchestrator(pipeline=SimpleNamespace(connectors=[SimpleNamespace(name="mock")]), mode="paper", kill_switch_state_file=tmp_path / "kill-switch.json")
    orchestrator.last_cycle = RuntimeCycleResult(
        mode="paper",
        bars=[SimpleNamespace(timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc), symbol="BTC/NOK", close=101.5)],
        signals=[1.0],
        execution_result=SimpleNamespace(
            entry_decisions=[{"allowed": False, "reason": "spread_limit", "timestamp": datetime(2024, 1, 1, tzinfo=timezone.utc)}],
            trades=[],
            orders=[SimpleNamespace(status="CANCELED", execution_status="SUBMITTED", side="buy", size=0.5, last_reason="adapter_rejected", execution_message="staged locally")],
            portfolio_history=[],
        ),
    )

    report = orchestrator.get_operational_report()
    latest_cycle = report["latest_cycle"]

    assert latest_cycle["latest_bar"]["symbol"] == "BTC/NOK"
    assert latest_cycle["latest_bar"]["close"] == 101.5
    assert latest_cycle["latest_signal"] == 1.0
    assert latest_cycle["latest_entry_decision"]["reason"] == "spread_limit"
    assert latest_cycle["latest_order"]["status"] == "CANCELED"
    assert latest_cycle["latest_order"]["execution_status"] == "SUBMITTED"


@pytest.mark.asyncio
async def test_runtime_orchestrator_persists_execution_context_event(tmp_path: Path) -> None:
    """Each runtime cycle should persist a concise execution context event for debugging."""
    store = MarketStore(database_path=tmp_path / "runtime.db")
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)
    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    orchestrator = RuntimeOrchestrator(
        pipeline=pipeline,
        mode="paper",
        kill_switch_state_file=tmp_path / "kill-switch.json",
        trade_logger=TradeLogger(database_path=tmp_path / "events.db"),
        checkpoint_path=tmp_path / "runtime.state.json",
    )
    await orchestrator.run_once()

    events = orchestrator.trade_logger.list_events(limit=20)
    context_events = [event for event in events if event["event_type"] == "runtime_execution_context"]

    assert context_events
    assert "strategy_name" in context_events[0]["metadata"]
    assert "latest_signal" in context_events[0]["metadata"]


@pytest.mark.asyncio
async def test_runtime_orchestrator_live_mode_uses_exchange_cycle_without_fake_fill_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live mode should keep staged exchange orders open instead of treating them as canceled fake fills."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")

    def fake_fetch_balance_snapshot(self: KrakenExecutionAdapter) -> dict[str, object]:
        return {"balances": {"EUR": 1000.0}, "positions": {}}

    monkeypatch.setattr(KrakenExecutionAdapter, "fetch_balance_snapshot", fake_fetch_balance_snapshot)

    runtime_config = RuntimeConfig(
        mode="live",
        exchange="kraken",
        use_mock_connector=True,
        trading_symbol="BTC/EUR",
        state_path=tmp_path / "runtime.state.json",
    )
    orchestrator, _pipeline = build_runtime_orchestrator(config=runtime_config, mode="live")
    orchestrator.execution_engine.risk_manager = None
    orchestrator.strategy = lambda history, index, current_bar: 1.0
    orchestrator.execution_engine.execution_adapter.api_key = ""
    orchestrator.execution_engine.execution_adapter.api_secret = ""
    orchestrator.execution_engine.execution_adapter._balances = {"EUR": 1000.0}
    orchestrator.execution_engine.execution_adapter._remote_balances = {"EUR": 1000.0}

    cycle = await orchestrator.run_once()
    latest_order = cycle.execution_result.orders[-1]

    assert cycle.mode == "live"
    assert latest_order.status == "SUBMITTED"
    assert latest_order.execution_status == "SUBMITTED"
    assert "not configured" in (latest_order.execution_message or "").lower()


def test_build_runtime_orchestrator_live_mode_seeds_equity_from_real_balance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--runtime live must use the real fetched balance as its equity reference, not a hardcoded default."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")

    def fake_fetch_balance_snapshot(self: KrakenExecutionAdapter) -> dict[str, object]:
        return {"balances": {"EUR": 14.14}, "positions": {}}

    monkeypatch.setattr(KrakenExecutionAdapter, "fetch_balance_snapshot", fake_fetch_balance_snapshot)

    runtime_config = RuntimeConfig(
        mode="live",
        exchange="kraken",
        use_mock_connector=True,
        trading_symbol="BTC/EUR",
        state_path=tmp_path / "runtime.state.json",
    )
    orchestrator, _pipeline = build_runtime_orchestrator(config=runtime_config, mode="live")

    assert orchestrator.execution_engine.initial_cash == 14.14
    assert orchestrator.execution_engine.execution_adapter._balances.get("EUR") == 14.14


def test_build_runtime_orchestrator_live_mode_refuses_to_start_when_balance_fetch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed real-balance fetch must refuse to start live trading rather than fall back to a fake default."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")

    def fake_fetch_balance_snapshot(self: KrakenExecutionAdapter) -> dict[str, object]:
        raise RuntimeError("Kraken API unavailable")

    monkeypatch.setattr(KrakenExecutionAdapter, "fetch_balance_snapshot", fake_fetch_balance_snapshot)

    runtime_config = RuntimeConfig(
        mode="live",
        exchange="kraken",
        use_mock_connector=True,
        trading_symbol="BTC/EUR",
        state_path=tmp_path / "runtime.state.json",
    )

    with pytest.raises(SystemExit):
        build_runtime_orchestrator(config=runtime_config, mode="live")


def test_build_runtime_orchestrator_live_mode_refuses_to_start_with_zero_balance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real but empty balance must refuse to start live trading rather than proceed with nothing to trade."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "runtime.db")

    def fake_fetch_balance_snapshot(self: KrakenExecutionAdapter) -> dict[str, object]:
        return {"balances": {"EUR": 0.0}, "positions": {}}

    monkeypatch.setattr(KrakenExecutionAdapter, "fetch_balance_snapshot", fake_fetch_balance_snapshot)

    runtime_config = RuntimeConfig(
        mode="live",
        exchange="kraken",
        use_mock_connector=True,
        trading_symbol="BTC/EUR",
        state_path=tmp_path / "runtime.state.json",
    )

    with pytest.raises(SystemExit):
        build_runtime_orchestrator(config=runtime_config, mode="live")


def test_trade_alerts_report_each_new_trade_once_with_its_reason(tmp_path: Path, _no_real_telegram_messages: list[str]) -> None:
    """Paper mode replays history every cycle: old trades must not be re-sent, and warmup trades not sent at all."""
    from src.execution.paper_trading import PaperTrade
    from src.utils.telegram import TelegramNotifier

    runtime_config = RuntimeConfig(use_mock_connector=True, trading_symbol="BTC/EUR", state_path=tmp_path / "runtime.state.json",
                                   strategy_name="moving_average_crossover", strategy_params={"short_window": 4, "long_window": 48})
    orchestrator, _ = build_runtime_orchestrator(config=runtime_config, mode="paper")
    orchestrator.alert_notifier = TelegramNotifier(bot_token="token", chat_id="chat")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def result(trades: list[PaperTrade], bars: int) -> SimpleNamespace:
        history = [PortfolioSnapshot(timestamp=start + timedelta(hours=i), cash=900.0, position_size=0.002, avg_entry_price=50000.0, equity=1005.0) for i in range(bars)]
        return SimpleNamespace(trades=trades, portfolio_history=history)

    def trade(order_id: str, hour: int, side: str, intent: str, signal: float) -> PaperTrade:
        return PaperTrade(order_id=order_id, timestamp=start + timedelta(hours=hour), side=side, price=50000.0 + hour, size=0.002, fee=0.1, cost=0.1,
                          symbol="BTC/EUR", intent=intent, signal=signal)

    warmup = [trade("order-1", 0, "buy", "enter_long", 1.0), trade("order-2", 1, "sell", "exit_long", 0.0)]
    orchestrator._maybe_notify_new_trades(result([*warmup, trade("order-3", 2, "buy", "enter_long", 1.0)], bars=3))
    assert len(_no_real_telegram_messages) == 1
    first = _no_real_telegram_messages[0]
    assert first.startswith("[PAPER] BUY 0.002 BTC/EUR @ 50,002.00") and "Why: entry: the signal turned long (moving_average_crossover signal +1)" in first
    assert "(short_window=4, long_window=48)" in first and "Position now: long 0.002 BTC/EUR" in first

    replayed = [trade("order-7", 0, "buy", "enter_long", 1.0), trade("order-8", 1, "sell", "exit_long", 0.0), trade("order-9", 2, "buy", "enter_long", 1.0)]
    orchestrator._maybe_notify_new_trades(result([*replayed, trade("order-10", 3, "sell", "time_stop", 1.0)], bars=4))
    assert len(_no_real_telegram_messages) == 2
    assert _no_real_telegram_messages[1].startswith("[PAPER] SELL") and "risk stop: held for the maximum number of bars" in _no_real_telegram_messages[1]

    orchestrator._maybe_notify_new_trades(result(replayed, bars=4))  # nothing new
    assert len(_no_real_telegram_messages) == 2
