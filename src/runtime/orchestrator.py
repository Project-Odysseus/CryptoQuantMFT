"""A small runtime orchestrator for driving the data pipeline and execution engine together."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from src.backtest.simple_backtest import moving_average_crossover_strategy
from src.execution.paper_trading import PaperTradingEngine
from src.execution.reconciliation import SessionAccountStateTracker
from src.risk.controls import RiskManager
from src.storage.trade_logger import TradeLogger
from src.utils.logger import logger

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
        self.timeout_seconds = timeout_seconds
        self.last_cycle_at: float | None = None
        self.last_data_at: float | None = None

    def start(self) -> None:
        self.last_cycle_at = time.monotonic()
        self.last_data_at = self.last_cycle_at

    def checkin_cycle(self) -> None:
        self.last_cycle_at = time.monotonic()

    def checkin_data(self) -> None:
        self.last_data_at = time.monotonic()

    def check(self) -> bool:
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
    ) -> None:
        if mode not in {"paper", "live_dry_run", "live"}:
            raise ValueError("mode must be one of: paper, live_dry_run, live")
        self.pipeline = pipeline
        self.mode = mode
        self.interval_seconds = interval_seconds
        self.strategy = strategy or moving_average_crossover_strategy(short_window=3, long_window=6)
        self.execution_engine = execution_engine or PaperTradingEngine(
            initial_cash=1000.0,
            default_order_size=1.0,
            risk_manager=RiskManager(),
        )
        self.last_cycle: RuntimeCycleResult | None = None
        self.history: list[RuntimeCycleResult] = []
        self._bar_history: list[Any] = []
        self.health = RuntimeHealth()
        self.watchdog = RuntimeWatchdog(watchdog_timeout_seconds)
        self.trade_logger = trade_logger
        self.account_state_tracker = SessionAccountStateTracker(exchange_name="paper" if mode == "paper" else None)

    async def run_startup_checks(self) -> bool:
        """Validate that the configured connectors can fetch a snapshot before starting the loop."""
        self.health.startup_checks_passed = False
        self.health.connector_status = {}
        self.watchdog.start()
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
        self.account_state_tracker.update_from_runtime(
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
        self.last_cycle = cycle
        self.history.append(cycle)
        self.health.cycles_completed += 1
        self.health.healthy = True
        self.health.last_error = None
        self.watchdog.checkin_cycle()
        if self.trade_logger is not None:
            self.trade_logger.log_event(
                timestamp=datetime.now(timezone.utc),
                level="INFO",
                event_type="runtime_cycle_completed",
                message="runtime cycle completed",
                source="runtime",
                metadata={"mode": self.mode, "cycle": self.health.cycles_completed},
            )
        return cycle

    async def run_loop(self, iterations: int = 3, *, interval_seconds: float | None = None) -> list[RuntimeCycleResult]:
        """Run the orchestrator for a number of iterations with a small sleep between cycles."""
        if iterations < 1:
            raise ValueError("iterations must be at least 1")

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
        }

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
