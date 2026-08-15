"""A small runtime orchestrator for driving the data pipeline and execution engine together."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from src.backtest.simple_backtest import moving_average_crossover_strategy
from src.runtime.config import RuntimeConfig
from src.execution.paper_trading import PaperTradingEngine
from src.execution.reconciliation import SessionAccountStateTracker
from src.risk.controls import CircuitBreaker, RiskManager
from src.risk.kill_switch import KillSwitchController
from src.storage.trade_logger import TradeLogger
from src.utils.logger import logger
from src.utils.telegram import TelegramNotifier

StrategyFn = Callable[[Sequence[Any], int, Any], float | int | str | None]


@dataclass(slots=True)
class RuntimeCycleResult:
    """Container for one orchestrated runtime cycle."""

    mode: str
    snapshots: list[Any] = field(default_factory=list)
    bars: list[Any] = field(default_factory=list)
    signals: list[float] = field(default_factory=list)
    execution_result: Any | None = None


@dataclass(slots=True)
class RuntimeHealth:
    """Simple health snapshot for the runtime loop."""

    healthy: bool = True
    startup_checks_passed: bool = False
    last_error: str | None = None
    cycles_completed: int = 0
    shutdown_requested: bool = False
    shutdown_reason: str | None = None
    connector_status: dict[str, str] = field(default_factory=dict)
    watchdog_triggered: bool = False
    watchdog_restarts_completed: int = 0


class RuntimeWatchdogError(RuntimeError):
    """Raised when the runtime exceeds its heartbeat timeout."""


class RuntimeWatchdog:
    """Track runtime and market-data heartbeats to detect stalls."""

    def __init__(self, timeout_seconds: float) -> None:
        """Initialize the object with its runtime state."""
        self.timeout_seconds = timeout_seconds
        self.last_cycle_at: float | None = None
        self.last_data_at: float | None = None

    def start(self) -> None:
        """Start the watchdog heartbeats for the runtime loop."""
        self.last_cycle_at = time.monotonic()
        self.last_data_at = self.last_cycle_at

    def checkin_cycle(self) -> None:
        """Record a successful runtime cycle heartbeat."""
        self.last_cycle_at = time.monotonic()

    def checkin_data(self) -> None:
        """Record a successful market-data heartbeat."""
        self.last_data_at = time.monotonic()

    def check(self) -> bool:
        """Check whether the runtime has exceeded its heartbeat timeout."""
        if self.last_cycle_at is None or self.last_data_at is None:
            return False
        now = time.monotonic()
        return (now - self.last_cycle_at) > self.timeout_seconds or (now - self.last_data_at) > self.timeout_seconds


class RuntimeOrchestrator:
    """Coordinate snapshots, signal generation, and paper execution in a loop."""

    def __init__(
        self,
        pipeline: Any,
        *,
        execution_engine: PaperTradingEngine | None = None,
        strategy: StrategyFn | None = None,
        mode: str = "paper",
        interval_seconds: float = 1.0,
        watchdog_timeout_seconds: float = 5.0,
        trade_logger: TradeLogger | None = None,
        kill_switch_state_file: str | Path | None = None,
        strategy_name: str | None = None,
        strategy_params: dict[str, Any] | None = None,
        runtime_config: RuntimeConfig | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        """Initialize the object with its runtime state."""
        if mode not in {"paper", "live_dry_run", "live"}:
            raise ValueError("mode must be one of: paper, live_dry_run, live")
        self.pipeline = pipeline
        self.mode = mode
        self.interval_seconds = interval_seconds
        self.strategy = strategy or moving_average_crossover_strategy(short_window=3, long_window=6)
        self.strategy_name = strategy_name or "moving_average_crossover"
        self.strategy_params = dict(strategy_params or {})
        self.circuit_breaker = CircuitBreaker()
        self.kill_switch_controller = KillSwitchController(state_file=kill_switch_state_file, trade_logger=trade_logger)
        self.execution_engine = execution_engine or PaperTradingEngine(
            initial_cash=1000.0,
            default_order_size=1.0,
            risk_manager=RiskManager(),
            circuit_breaker=self.circuit_breaker,
            kill_switch_controller=self.kill_switch_controller,
        )
        self.last_cycle: RuntimeCycleResult | None = None
        self.history: list[RuntimeCycleResult] = []
        self._bar_history: list[Any] = []
        self.health = RuntimeHealth()
        self.watchdog = RuntimeWatchdog(watchdog_timeout_seconds)
        self.trade_logger = trade_logger
        self.runtime_config = runtime_config
        self.checkpoint_path = Path(checkpoint_path or getattr(runtime_config, "state_path", None) or "data/runtime_state.json")
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.alert_notifier = TelegramNotifier()
        self._active_alerts: set[str] = set()
        self.stale_quote_threshold_seconds = max(30.0, float(interval_seconds) * 10.0)
        self.heartbeat_timeout_seconds = max(2 * self.stale_quote_threshold_seconds, float(watchdog_timeout_seconds))
        self.last_heartbeat_at: datetime | None = None
        self.account_state_tracker = SessionAccountStateTracker(exchange_name="paper" if mode == "paper" else None)
        if getattr(self.execution_engine, "circuit_breaker", None) is None:
            self.execution_engine.circuit_breaker = self.circuit_breaker
        if getattr(self.execution_engine, "kill_switch_controller", None) is None:
            self.execution_engine.kill_switch_controller = self.kill_switch_controller

    def load_checkpoint(self) -> bool:
        """Restore runtime health and config from the persisted checkpoint, if present."""
        if not self.checkpoint_path.exists():
            return False

        with self.checkpoint_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        config_payload = payload.get("config") or {}
        if config_payload:
            self.runtime_config = RuntimeConfig.from_dict(config_payload)
            self.mode = str(self.runtime_config.mode)
            self.strategy_name = str(self.runtime_config.strategy_name)
            self.strategy_params = dict(self.runtime_config.strategy_params or {})
            if self.runtime_config.state_path:
                self.checkpoint_path = Path(self.runtime_config.state_path)
                self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        health_payload = payload.get("health") or {}
        self.health.healthy = bool(health_payload.get("healthy", True))
        self.health.startup_checks_passed = bool(health_payload.get("startup_checks_passed", False))
        self.health.last_error = health_payload.get("last_error")
        self.health.cycles_completed = int(health_payload.get("cycles_completed", 0))
        self.health.shutdown_requested = bool(health_payload.get("shutdown_requested", False))
        self.health.shutdown_reason = health_payload.get("shutdown_reason")
        self.health.connector_status = dict(health_payload.get("connector_status", {}))
        self.health.watchdog_triggered = bool(health_payload.get("watchdog_triggered", False))
        self.health.watchdog_restarts_completed = int(health_payload.get("watchdog_restarts_completed", 0))
        self._active_alerts = set(payload.get("active_alerts", []))

        last_cycle_payload = payload.get("last_cycle") or {}
        if last_cycle_payload:
            self.last_cycle = RuntimeCycleResult(
                mode=str(last_cycle_payload.get("mode", self.mode)),
                signals=list(last_cycle_payload.get("signals", []) or []),
                bars=[],
                execution_result=None,
            )
            self.history = [self.last_cycle]
        return True

    def save_checkpoint(self) -> None:
        """Persist the current runtime state for restart-safe recovery."""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "config": self._serialize_runtime_config(),
            "health": {
                "healthy": self.health.healthy,
                "startup_checks_passed": self.health.startup_checks_passed,
                "last_error": self.health.last_error,
                "cycles_completed": self.health.cycles_completed,
                "shutdown_requested": self.health.shutdown_requested,
                "shutdown_reason": self.health.shutdown_reason,
                "connector_status": dict(self.health.connector_status),
                "watchdog_triggered": self.health.watchdog_triggered,
                "watchdog_restarts_completed": self.health.watchdog_restarts_completed,
            },
            "active_alerts": sorted(self._active_alerts),
            "last_cycle": self._serialize_last_cycle(),
        }

        with self.checkpoint_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    def _serialize_runtime_config(self) -> dict[str, Any]:
        if self.runtime_config is not None:
            return self.runtime_config.to_dict()
        return RuntimeConfig(
            mode=self.mode,
            strategy_name=self.strategy_name,
            strategy_params=dict(self.strategy_params),
            state_path=self.checkpoint_path,
        ).to_dict()

    def _serialize_last_cycle(self) -> dict[str, Any]:
        if self.last_cycle is None:
            return {}

        execution_result = getattr(self.last_cycle, "execution_result", None)
        portfolio_history = list(getattr(execution_result, "portfolio_history", []) or [])
        final_equity = None
        if portfolio_history:
            final_equity = float(getattr(portfolio_history[-1], "equity", 0.0))

        return {
            "mode": getattr(self.last_cycle, "mode", self.mode),
            "signals": list(getattr(self.last_cycle, "signals", []) or []),
            "bar_count": len(getattr(self.last_cycle, "bars", []) or []),
            "trade_count": len(getattr(execution_result, "trades", []) or []),
            "order_count": len(getattr(execution_result, "orders", []) or []),
            "final_equity": final_equity,
        }

    async def run_startup_checks(self) -> bool:
        """Validate that the configured connectors can fetch a snapshot before starting the loop."""
        self.health.startup_checks_passed = False
        self.health.connector_status = {}
        self.watchdog.start()
        self.last_heartbeat_at = datetime.now(timezone.utc)
        self._emit_startup_banner()
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_startup",
                message="starting runtime startup checks",
                source="runtime",
                metadata={"mode": self.mode, "connectors": len(getattr(self.pipeline, "connectors", []))},
            )
        connectors = getattr(self.pipeline, "connectors", [])
        if not connectors:
            self._mark_unhealthy("no market-data connectors configured")
            return False

        for connector in connectors:
            connector_name = getattr(connector, "name", connector.__class__.__name__)
            try:
                await connector.connect()
                await connector.fetch_snapshot()
                await connector.disconnect()
            except Exception as exc:  # pragma: no cover - exercised through runtime smoke tests
                self.health.connector_status[connector_name] = f"error:{exc}"
                self._mark_unhealthy(f"{connector_name} health check failed: {exc}")
                logger.warning("runtime_startup_failed connector={} error={}", connector_name, exc)
                self.trigger_hard_stop(f"connector_error:{connector_name}", metadata={"error": str(exc)})
                return False
            self.health.connector_status[connector_name] = "ok"

        self.health.startup_checks_passed = True
        self.health.healthy = True
        self.health.last_error = None
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_ready",
                message="runtime startup checks passed",
                source="runtime",
                metadata={"mode": self.mode, "connectors": len(connectors)},
            )
        logger.info("runtime_startup_checks_passed connectors={}", len(connectors))
        return True

    async def run_once(self) -> RuntimeCycleResult:
        """Fetch one batch of market data, derive signals, and execute them."""
        if self.health.shutdown_requested:
            raise RuntimeError("runtime shutdown requested")

        if self.circuit_breaker.is_active() or self.kill_switch_controller.is_active():
            self._mark_unhealthy("circuit breaker active")
            self.request_shutdown(reason="circuit_breaker_active")
            raise RuntimeError("circuit breaker active")

        try:
            snapshots = await self.pipeline.run_once()
            new_bars = self.pipeline.flush_bars()
        except Exception as exc:
            self._mark_unhealthy(f"pipeline cycle failed: {exc}")
            self.request_shutdown(reason=f"pipeline_error:{exc}")
            if self.trade_logger is not None:
                self.trade_logger.log_event(
                    timestamp=datetime.now(timezone.utc),
                    level="ERROR",
                    event_type="pipeline_error",
                    message=str(exc),
                    source="runtime",
                    metadata={"mode": self.mode},
                )
            raise

        if snapshots:
            self.watchdog.checkin_data()
        if new_bars:
            self.watchdog.checkin_data()

        self._bar_history.extend(new_bars)
        signals = self._build_signals(self._bar_history)
        if self.mode == "live":
            self._mark_unhealthy("live execution adapters are not implemented")
            self.request_shutdown(reason="live_not_implemented")
            raise NotImplementedError("Live execution adapters are not implemented yet; use paper or live_dry_run")

        try:
            execution_result = self.execution_engine.run(self._bar_history, signals)
        except Exception as exc:
            self._mark_unhealthy(f"execution cycle failed: {exc}")
            self.request_shutdown(reason=f"execution_error:{exc}")
            if self.trade_logger is not None:
                self.trade_logger.log_event(
                    timestamp=datetime.now(timezone.utc),
                    level="ERROR",
                    event_type="execution_error",
                    message=str(exc),
                    source="runtime",
                    metadata={"mode": self.mode},
                )
            raise

        adapter = getattr(self.execution_engine, "execution_adapter", None)
        exchange_name = getattr(adapter, "exchange_name", None)
        account_state_summary = self.account_state_tracker.update_from_runtime(
            execution_result=execution_result,
            adapter=adapter,
            exchange_name=exchange_name or ("paper" if self.mode == "paper" else None),
        )

        cycle = RuntimeCycleResult(
            mode=self.mode,
            snapshots=snapshots,
            bars=list(self._bar_history),
            signals=signals,
            execution_result=execution_result,
        )
        previous_trade_count = 0
        if self.last_cycle is not None:
            previous_result = getattr(self.last_cycle, "execution_result", None)
            previous_trade_count = len(getattr(previous_result, "trades", []) or [])

        self.last_cycle = cycle
        self.history.append(cycle)
        self.health.cycles_completed += 1
        self.health.healthy = True
        self.health.last_error = None
        self.watchdog.checkin_cycle()
        self.last_heartbeat_at = datetime.now(timezone.utc)
        self._evaluate_runtime_alerts(cycle=cycle, account_state_summary=account_state_summary, execution_result=execution_result)
        self._maybe_notify_new_trades(execution_result, previous_trade_count=previous_trade_count)
        self._emit_cycle_health_snapshot(cycle)
        self._write_daily_summary()
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_cycle_completed",
                message="runtime cycle completed",
                source="runtime",
                metadata={"mode": self.mode, "cycle": self.health.cycles_completed},
            )
        self.save_checkpoint()
        return cycle

    async def run_loop(self, iterations: int = 3, *, interval_seconds: float | None = None, resume_from_checkpoint: bool = False) -> list[RuntimeCycleResult]:
        """Run the orchestrator for a number of iterations with a small sleep between cycles."""
        if iterations < 1:
            raise ValueError("iterations must be at least 1")

        if resume_from_checkpoint:
            self.load_checkpoint()

        if not self.health.startup_checks_passed and not await self.run_startup_checks():
            return self.history

        for index in range(iterations):
            if self.health.shutdown_requested:
                logger.warning("runtime_shutdown_requested reason={}", self.health.shutdown_reason)
                break
            if self.watchdog.check():
                self.health.watchdog_triggered = True
                self._mark_unhealthy("watchdog timeout exceeded")
                self.request_shutdown(reason="watchdog_timeout")
                raise RuntimeWatchdogError("watchdog timeout exceeded")
            try:
                await self.run_once()
            except RuntimeWatchdogError:
                raise
            except (asyncio.CancelledError, KeyboardInterrupt) as exc:
                self.request_shutdown(reason="interrupted")
                logger.warning("runtime_interrupted error={}", exc)
                break
            except Exception as exc:
                logger.exception("runtime_cycle_failed error={}", exc)
                break

            if index < iterations - 1 and not self.health.shutdown_requested:
                await asyncio.sleep(interval_seconds if interval_seconds is not None else self.interval_seconds)
        return self.history

    def request_shutdown(self, *, reason: str | None = None) -> None:
        """Request a graceful shutdown for the runtime loop."""
        self.health.shutdown_requested = True
        self.health.shutdown_reason = reason or "requested"
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_shutdown",
                message=reason or "requested",
                source="runtime",
                metadata={"mode": self.mode},
            )
        self.save_checkpoint()

    def trigger_hard_stop(self, reason: str, *, metadata: dict[str, Any] | None = None) -> None:
        """Activate the circuit breaker and request a shutdown."""
        self.circuit_breaker.activate(reason, **(metadata or {}))
        self._mark_unhealthy(f"hard_stop:{reason}")
        self.request_shutdown(reason=f"hard_stop:{reason}")
        self._set_alert_state(
            "circuit_breaker",
            active=True,
            message=f"hard stop triggered: {reason}",
            metadata={"mode": self.mode, **(metadata or {})},
            level="WARNING",
        )
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="WARNING",
                event_type="hard_stop",
                message=reason,
                source="runtime",
                metadata={"mode": self.mode, **(metadata or {})},
            )

    def activate_kill_switch(self, reason: str) -> dict[str, Any]:
        """Activate the kill switch and cancel any open orders through the execution adapter."""
        state = self.kill_switch_controller.activate(
            reason,
            execution_adapter=getattr(self.execution_engine, "execution_adapter", None),
            trade_logger=self.trade_logger,
        )
        self._set_alert_state(
            "kill_switch",
            active=True,
            message=f"kill switch activated: {reason}",
            metadata={"mode": self.mode, "state": state},
            level="WARNING",
        )
        self.trigger_hard_stop(reason, metadata={"kill_switch": True, "state": state})
        return state

    def _mark_unhealthy(self, message: str) -> None:
        self.health.healthy = False
        self.health.last_error = message

    def get_health_report(self) -> dict[str, Any]:
        """Return a simple health summary for the runtime loop."""
        return {
            "healthy": self.health.healthy,
            "startup_checks_passed": self.health.startup_checks_passed,
            "cycles_completed": self.health.cycles_completed,
            "shutdown_requested": self.health.shutdown_requested,
            "shutdown_reason": self.health.shutdown_reason,
            "last_error": self.health.last_error,
            "connector_status": dict(self.health.connector_status),
            "watchdog_triggered": self.health.watchdog_triggered,
            "watchdog_restarts_completed": self.health.watchdog_restarts_completed,
            "account_state": self.account_state_tracker.get_summary(),
            "circuit_breaker": self.circuit_breaker.snapshot(),
            "kill_switch": self.kill_switch_controller.get_state(),
        }

    def _emit_startup_banner(self) -> None:
        """Emit a startup banner for the active runtime configuration."""
        summary = self._build_startup_summary()
        logger.info("runtime_startup_banner\n{}", summary)
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_startup_banner",
                message="runtime startup banner",
                source="runtime",
                metadata={"summary": summary},
            )

    def _evaluate_runtime_alerts(self, *, cycle: RuntimeCycleResult, account_state_summary: dict[str, Any], execution_result: Any | None) -> None:
        """Emit runtime alerts for stale data, reconciliation mismatches, blocked entries, and heartbeat lapses."""
        self._evaluate_stale_market_data_alert(cycle)
        self._evaluate_reconciliation_alert(account_state_summary)
        self._evaluate_entry_decision_alert(execution_result)
        self._evaluate_heartbeat_alert()

    def _evaluate_stale_market_data_alert(self, cycle: RuntimeCycleResult) -> None:
        """Raise an alert if the latest market-data bar is older than the configured threshold."""
        bars = list(getattr(cycle, "bars", []) or [])
        if not bars:
            self._clear_alert("stale_data")
            return

        latest_bar = bars[-1]
        timestamp = getattr(latest_bar, "timestamp", None)
        if timestamp is None:
            self._clear_alert("stale_data")
            return

        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                self._clear_alert("stale_data")
                return

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        age_seconds = max(0.0, (now - timestamp).total_seconds())
        self._set_alert_state(
            "stale_data",
            active=age_seconds > self.stale_quote_threshold_seconds,
            message=f"market data appears stale (latest bar age={age_seconds:.1f}s)",
            metadata={"latest_bar_age_seconds": round(age_seconds, 2), "threshold_seconds": round(self.stale_quote_threshold_seconds, 2)},
            level="WARNING",
        )

    def _evaluate_reconciliation_alert(self, account_state_summary: dict[str, Any]) -> None:
        """Raise an alert when reconciliation reports an unmatched or inconsistent state."""
        mismatches = account_state_summary.get("reconciliation_mismatches", []) or []
        status = account_state_summary.get("reconciliation_status", "unknown")
        self._set_alert_state(
            "reconciliation_mismatch",
            active=status != "matched" or bool(mismatches),
            message=f"reconciliation mismatch detected (status={status}, mismatches={len(mismatches)})",
            metadata={"status": status, "mismatch_count": len(mismatches), "mismatches": mismatches},
            level="WARNING",
        )

    def _evaluate_entry_decision_alert(self, execution_result: Any | None) -> None:
        """Raise an alert when the execution engine blocked entries due to risk controls."""
        entry_decisions = getattr(execution_result, "entry_decisions", None) or []
        blocked_entries = [decision for decision in entry_decisions if not bool(decision.get("allowed", True))]
        reasons = Counter(str(decision.get("reason") or "unknown") for decision in blocked_entries)
        self._set_alert_state(
            "risk_stop",
            active=bool(blocked_entries),
            message=f"risk controls blocked entries ({dict(reasons)})",
            metadata={"blocked_entry_count": len(blocked_entries), "reasons": dict(reasons)},
            level="WARNING",
        )

    def _evaluate_heartbeat_alert(self) -> None:
        """Alert if the runtime has not emitted a heartbeat recently."""
        if self.last_heartbeat_at is None:
            return

        since_last_heartbeat = (datetime.now(timezone.utc) - self.last_heartbeat_at).total_seconds()
        self._set_alert_state(
            "heartbeat_lost",
            active=since_last_heartbeat > self.heartbeat_timeout_seconds,
            message=f"runtime heartbeat stalled for {since_last_heartbeat:.1f}s",
            metadata={"seconds_since_last_heartbeat": round(since_last_heartbeat, 2), "threshold_seconds": round(self.heartbeat_timeout_seconds, 2)},
            level="WARNING",
        )

    def _set_alert_state(self, alert_key: str, *, active: bool, message: str, metadata: dict[str, Any] | None = None, level: str = "WARNING") -> None:
        """Ensure a runtime alert is emitted once and then tracked as active until conditions clear."""
        if active:
            if alert_key in self._active_alerts:
                return
            self._emit_alert(alert_key, message, metadata=metadata, level=level)
            self._active_alerts.add(alert_key)
            return

        if alert_key in self._active_alerts:
            self._active_alerts.remove(alert_key)

    def _emit_alert(self, alert_key: str, message: str, *, metadata: dict[str, Any] | None = None, level: str = "WARNING") -> None:
        """Emit a runtime alert through logs, the persisted event log, and theTelegram placeholder notifier."""
        logger.warning("runtime_alert alert={} {}", alert_key, message)
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level=level,
                event_type="runtime_alert",
                message=message,
                source="runtime",
                metadata={"alert": alert_key, **(metadata or {})},
            )
        if self.alert_notifier is not None:
            self.alert_notifier.send_alert(event_type=alert_key, message=message, metadata=metadata)

    def _clear_alert(self, alert_key: str) -> None:
        """Clear an alert from the active set without re-emitting."""
        if alert_key in self._active_alerts:
            self._active_alerts.remove(alert_key)

    def _emit_cycle_health_snapshot(self, cycle: RuntimeCycleResult) -> None:
        """Emit a compact health snapshot after each runtime cycle."""
        summary = self._build_cycle_summary(cycle)
        logger.info("runtime_health_snapshot\n{}", summary)
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_health_snapshot",
                message="runtime health snapshot",
                source="runtime",
                metadata={"summary": summary, "cycle": self.health.cycles_completed},
            )

    def _write_daily_summary(self) -> None:
        """Write a persisted daily summary for the active runtime session."""
        if self.trade_logger is None:
            return

        health_report = self.get_health_report()
        active_alerts = sorted(self._active_alerts)
        runtime_status = "healthy" if health_report["healthy"] and not active_alerts else "degraded"
        self.trade_logger.get_daily_summary(
            report_date=datetime.now(timezone.utc),
            runtime_status=runtime_status,
            research_status="parallel_lane_pending",
            active_alerts=active_alerts,
        )

    def _maybe_notify_new_trades(self, execution_result: Any | None, *, previous_trade_count: int = 0) -> None:
        """Send a Telegram trade update when the runtime records a new paper-trade event."""
        if execution_result is None:
            return

        current_trades = list(getattr(execution_result, "trades", []) or [])
        if len(current_trades) <= previous_trade_count:
            return

        latest_trade = current_trades[-1]
        portfolio_history = list(getattr(execution_result, "portfolio_history", []) or [])
        if not portfolio_history:
            return

        latest_snapshot = portfolio_history[-1]
        initial_equity = float(getattr(self.execution_engine, "initial_cash", 1000.0))
        current_equity = float(getattr(latest_snapshot, "equity", initial_equity))
        current_pnl = current_equity - initial_equity

        peak_equity = initial_equity
        worst_equity = current_equity
        for snapshot in portfolio_history:
            peak_equity = max(peak_equity, float(getattr(snapshot, "equity", initial_equity)))
            worst_equity = min(worst_equity, float(getattr(snapshot, "equity", initial_equity)))

        current_drawdown_pct = 0.0 if peak_equity <= 0.0 else max(0.0, (peak_equity - current_equity) / peak_equity)
        distance_to_max_drawdown_point = current_equity - worst_equity
        position_side = "flat"
        position_size = float(getattr(latest_snapshot, "position_size", 0.0))
        if position_size > 0.0:
            position_side = "long"
        elif position_size < 0.0:
            position_side = "short"

        pnl_last_hour = self._calculate_pnl_last_hour()
        self.alert_notifier.send_trade_update(
            strategy_name=self.strategy_name,
            trade_side=str(getattr(latest_trade, "side", "unknown") or "unknown"),
            current_pnl=current_pnl,
            pnl_last_hour=pnl_last_hour,
            position_side=position_side,
            max_drawdown_pct=current_drawdown_pct,
            distance_to_max_drawdown_point=distance_to_max_drawdown_point,
        )

    def _calculate_pnl_last_hour(self) -> float:
        """Estimate recent equity change over the last hour using persisted equity snapshots."""
        if self.trade_logger is None:
            return 0.0

        snapshots = self.trade_logger.list_equity_snapshots(limit=200)
        if not snapshots:
            return 0.0

        parsed_snapshots: list[tuple[datetime, float]] = []
        now = datetime.now(timezone.utc)
        for snapshot in snapshots:
            timestamp = snapshot.get("timestamp")
            if not timestamp:
                continue
            try:
                parsed_timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
            if now - parsed_timestamp <= timedelta(hours=1):
                parsed_snapshots.append((parsed_timestamp, float(snapshot.get("equity", 0.0))))

        if len(parsed_snapshots) < 2:
            return 0.0

        oldest_equity = parsed_snapshots[-1][1]
        newest_equity = parsed_snapshots[0][1]
        return newest_equity - oldest_equity

    def _build_startup_summary(self) -> str:
        """Build a human-readable summary of the runtime configuration."""
        health_report = self.get_health_report()
        account_state = health_report["account_state"]
        kill_switch_state = health_report["kill_switch"]
        risk_manager = getattr(getattr(self.execution_engine, "risk_manager", None), "config", None)

        lines = [
            "=== Runtime startup summary ===",
            f"mode: {self.mode}",
            f"strategy: {self.strategy_name}",
            f"strategy_params: {self.strategy_params}",
            f"exchange: {account_state.get('exchange') or 'unknown'}",
            f"connectors: {len(getattr(self.pipeline, 'connectors', []))}",
            f"risk_limits: max_drawdown={getattr(risk_manager, 'max_drawdown_pct', 'n/a')} volatility={getattr(risk_manager, 'max_volatility_pct', 'n/a')} risk_per_trade={getattr(risk_manager, 'risk_per_trade_pct', 'n/a')} max_position={getattr(risk_manager, 'max_position_size', 'n/a')}",
            f"kill_switch: active={kill_switch_state.get('active', False)} reason={kill_switch_state.get('reason') or 'none'}",
        ]
        return "\n".join(lines)

    def _build_cycle_summary(self, cycle: RuntimeCycleResult) -> str:
        """Build a compact health summary for the latest runtime cycle."""
        health_report = self.get_health_report()
        account_state = health_report["account_state"]
        execution_result = getattr(cycle, "execution_result", None)
        no_trade_summary = self._build_no_trade_summary(getattr(execution_result, "entry_decisions", None))
        portfolio_history = getattr(execution_result, "portfolio_history", None) or []
        final_equity = portfolio_history[-1].equity if portfolio_history else 1000.0
        trades = list(getattr(execution_result, "trades", []) or [])
        orders = list(getattr(execution_result, "orders", []) or [])

        lines = [
            "=== Runtime health snapshot ===",
            f"cycle: {self.health.cycles_completed}",
            f"mode: {self.mode}",
            f"healthy: {health_report['healthy']}",
            f"strategy: {self.strategy_name}",
            f"signals: {len(cycle.signals)} bars: {len(cycle.bars)} trades: {len(trades)} orders: {len(orders)}",
            f"equity: {final_equity:.4f} cash: {account_state.get('balances', {}).get('USD', 0.0):.4f}",
            f"reconciliation: {account_state.get('reconciliation_status', 'unknown')}",
            f"circuit_breaker: active={health_report['circuit_breaker']['active']} reason={health_report['circuit_breaker']['reason'] or 'none'}",
            f"kill_switch: active={health_report['kill_switch']['active']} reason={health_report['kill_switch']['reason'] or 'none'}",
            f"no_trade_reason: {no_trade_summary.get('reason', 'none')}",
        ]
        return "\n".join(lines)

    def get_operational_report(self, *, limit: int = 10) -> dict[str, Any]:
        """Return a consolidated operational report for the current runtime session."""
        health_report = self.get_health_report()
        account_state = health_report["account_state"]
        last_cycle = self.last_cycle
        execution_result = getattr(last_cycle, "execution_result", None) if last_cycle is not None else None
        entry_decisions = getattr(execution_result, "entry_decisions", None) if execution_result is not None else None
        decision_summary = self._summarize_entry_decisions(entry_decisions)
        no_trade_summary = self._build_no_trade_summary(entry_decisions)

        recent_trades: list[dict[str, Any]] = []
        recent_events: list[dict[str, Any]] = []
        if self.trade_logger is not None:
            recent_trades = self.trade_logger.list_trades(limit=limit)
            recent_events = self.trade_logger.list_events(limit=limit)

        last_heartbeat = self.last_heartbeat_at
        seconds_since_heartbeat = None
        if last_heartbeat is not None:
            seconds_since_heartbeat = round((datetime.now(timezone.utc) - last_heartbeat).total_seconds(), 2)

        return {
            "runtime": {
                "mode": self.mode,
                "strategy_name": self.strategy_name,
                "strategy_params": self.strategy_params,
                "exchange": account_state.get("exchange"),
                "cycles_completed": health_report["cycles_completed"],
                "shutdown_requested": health_report["shutdown_requested"],
                "shutdown_reason": health_report["shutdown_reason"],
                "last_error": health_report["last_error"],
            },
            "heartbeat": {
                "last_heartbeat_at": last_heartbeat.isoformat() if last_heartbeat is not None else None,
                "seconds_since_last_heartbeat": seconds_since_heartbeat,
                "threshold_seconds": round(self.heartbeat_timeout_seconds, 2),
                "stalled": seconds_since_heartbeat is not None and seconds_since_heartbeat > self.heartbeat_timeout_seconds,
            },
            "connectors": health_report["connector_status"],
            "safety": {
                "circuit_breaker": health_report["circuit_breaker"],
                "kill_switch": health_report["kill_switch"],
            },
            "account_state": account_state,
            "reconciliation": {
                "status": account_state.get("reconciliation_status"),
                "account_reconciliation_status": account_state.get("account_reconciliation_status"),
                "mismatches": account_state.get("reconciliation_mismatches", []),
                "unsettled_order_count": account_state.get("unsettled_order_count", 0),
                "reconciled_order_count": account_state.get("reconciled_order_count", 0),
                "last_reconciled_at": account_state.get("last_reconciled_at"),
            },
            "latest_cycle": {
                "signals": list(getattr(last_cycle, "signals", []) or []),
                "bars": len(getattr(last_cycle, "bars", []) or []),
                "trades": len(getattr(execution_result, "trades", []) or []),
                "orders": len(getattr(execution_result, "orders", []) or []),
            },
            "entry_decisions": decision_summary,
            "no_trade_summary": no_trade_summary,
            "active_alerts": sorted(self._active_alerts),
            "recent_trades": recent_trades,
            "recent_events": recent_events,
        }

    def _summarize_entry_decisions(self, entry_decisions: Sequence[dict[str, Any]] | None) -> dict[str, Any]:
        if not entry_decisions:
            return {"total": 0, "allowed": 0, "blocked": 0, "reasons": {}}

        reasons = Counter(str(decision.get("reason") or "unknown") for decision in entry_decisions)
        return {
            "total": len(entry_decisions),
            "allowed": sum(1 for decision in entry_decisions if decision.get("allowed")),
            "blocked": sum(1 for decision in entry_decisions if not decision.get("allowed")),
            "reasons": dict(reasons),
        }

    def _build_no_trade_summary(self, entry_decisions: Sequence[dict[str, Any]] | None) -> dict[str, Any]:
        if not entry_decisions:
            return {"status": "no_decisions", "reason": "no_entry_decisions_recorded"}

        blocked_reasons = Counter(str(decision.get("reason") or "unknown") for decision in entry_decisions if not decision.get("allowed"))
        if blocked_reasons:
            most_common = blocked_reasons.most_common(1)[0]
            return {
                "status": "blocked",
                "reason": most_common[0],
                "reason_counts": dict(blocked_reasons),
            }

        return {"status": "allowed_but_no_trades", "reason": "entries_were_allowed_but_no_fill_was_recorded"}

    def _build_signals(self, bars: Sequence[Any]) -> list[float]:
        signals: list[float] = []
        for index, bar in enumerate(bars):
            history = list(bars[: index + 1])
            signal_value = self.strategy(history, index, bar)
            signals.append(_normalize_signal(signal_value))
        return signals


def _normalize_signal(raw_signal: float | int | str | None) -> float:
    if raw_signal is None:
        return 0.0
    if isinstance(raw_signal, str):
        normalized = raw_signal.strip().lower()
        if normalized in {"buy", "long", "1", "true", "enter"}:
            return 1.0
        if normalized in {"sell", "short", "-1", "false", "exit"}:
            return -1.0
        return 0.0
    return float(raw_signal)
