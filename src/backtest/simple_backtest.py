"""A lightweight vectorized-style backtester for simple signal research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.signals import VolatilityTrendFilter
from src.storage.bar_aggregator import OHLCVBar


@dataclass(slots=True)
class BacktestResult:
    """Simple performance summary for a single strategy run."""

    total_return: float
    win_rate: float
    max_drawdown: float
    trades: int
    final_equity: float


class SimpleBacktester:
    """Run a trivial signal-driven backtest over OHLCV bars."""

    def __init__(self, signal_name: str = "trend") -> None:
        self.signal_name = signal_name

    def run(self, bars: Sequence[OHLCVBar]) -> BacktestResult:
        """Backtest a simple signal strategy over a sequence of bars."""
        if len(bars) < 2:
            raise ValueError("At least two bars are required")

        equity = 100.0
        peak_equity = equity
        trades = 0
        wins = 0
        equity_series: list[float] = []

        filter_signal = VolatilityTrendFilter(lookback=3, atr_multiplier=1.0)

        for index in range(1, len(bars)):
            signal = filter_signal.compute(list(bars[: index + 1]))
            if signal.direction == "up" and bars[index].close > bars[index - 1].close:
                if equity > 0:
                    trades += 1
                    if bars[index].close > bars[index - 1].close:
                        wins += 1
                    equity *= 1 + (bars[index].close - bars[index - 1].close) / bars[index - 1].close
            elif signal.direction == "down" and bars[index].close < bars[index - 1].close:
                if equity > 0:
                    trades += 1
                    if bars[index].close < bars[index - 1].close:
                        wins += 1
                    equity *= 1 + (bars[index].close - bars[index - 1].close) / bars[index - 1].close

            peak_equity = max(peak_equity, equity)
            equity_series.append(equity)

        drawdown = 0.0
        if equity_series:
            drawdown = max((peak_equity - value) / peak_equity if peak_equity > 0 else 0.0 for value in equity_series)

        return BacktestResult(
            total_return=round(equity / 100.0 - 1.0, 10),
            win_rate=round(wins / trades, 10) if trades else 0.0,
            max_drawdown=round(drawdown, 10),
            trades=trades,
            final_equity=round(equity, 10),
        )
