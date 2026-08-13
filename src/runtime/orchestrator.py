"""A small runtime orchestrator for driving the data pipeline and execution engine together."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from src.backtest.simple_backtest import moving_average_crossover_strategy
from src.execution.paper_trading import PaperTradingEngine
from src.risk.controls import RiskManager

StrategyFn = Callable[[Sequence[Any], int, Any], float | int | str | None]


@dataclass(slots=True)
class RuntimeCycleResult:
    """Container for one orchestrated runtime cycle."""

    mode: str
    snapshots: list[Any] = field(default_factory=list)
    bars: list[Any] = field(default_factory=list)
    signals: list[float] = field(default_factory=list)
    execution_result: Any | None = None


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

    async def run_once(self) -> RuntimeCycleResult:
        """Fetch one batch of market data, derive signals, and execute them."""
        snapshots = await self.pipeline.run_once()
        new_bars = self.pipeline.flush_bars()
        self._bar_history.extend(new_bars)
        signals = self._build_signals(self._bar_history)
        if self.mode == "live":
            raise NotImplementedError("Live execution adapters are not implemented yet; use paper or live_dry_run")
        execution_result = self.execution_engine.run(self._bar_history, signals)
        cycle = RuntimeCycleResult(
            mode=self.mode,
            snapshots=snapshots,
            bars=list(self._bar_history),
            signals=signals,
            execution_result=execution_result,
        )
        self.last_cycle = cycle
        self.history.append(cycle)
        return cycle

    async def run_loop(self, iterations: int = 3, *, interval_seconds: float | None = None) -> list[RuntimeCycleResult]:
        """Run the orchestrator for a number of iterations with a small sleep between cycles."""
        if iterations < 1:
            raise ValueError("iterations must be at least 1")

        for _ in range(iterations):
            await self.run_once()
            if _ < iterations - 1:
                await asyncio.sleep(interval_seconds if interval_seconds is not None else self.interval_seconds)
        return self.history

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
