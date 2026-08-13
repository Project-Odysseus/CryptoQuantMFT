"""A lightweight vectorized-style backtester for simple signal research."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from src.signals import OrderBookSignalEngine, VolatilityTrendFilter
from src.storage.bar_aggregator import OHLCVBar
from src.storage.order_book import OrderBookSnapshot


@dataclass(slots=True)
class BacktestResult:
    """Simple performance summary for a single strategy run."""

    total_return: float
    win_rate: float
    max_drawdown: float
    trades: int
    final_equity: float
    equity_series: list[float] = field(default_factory=list)
    trade_prices: list[float] = field(default_factory=list)


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
        equity_series: list[float] = [equity]
        trade_prices: list[float] = []

        filter_signal = VolatilityTrendFilter(lookback=3, atr_multiplier=1.0)
        order_book_engine = OrderBookSignalEngine(depth_levels=3)

        for index in range(1, len(bars)):
            signal = filter_signal.compute(list(bars[: index + 1]))
            previous_bar = bars[index - 1]
            current_bar = bars[index]

            snapshot = OrderBookSnapshot(
                bids=[(previous_bar.close, 1.0), (previous_bar.close - 0.1, 0.5)],
                asks=[(previous_bar.close + 0.1, 0.7), (previous_bar.close + 0.2, 0.3)],
            )
            obi_signal = order_book_engine.compute_obi(snapshot)
            volume_signal = order_book_engine.compute_volume_delta(snapshot)

            trend_signal = signal.direction
            if trend_signal == "up" and obi_signal.value > 0 and volume_signal.value > 0 and current_bar.close > previous_bar.close:
                if equity > 0:
                    trades += 1
                    if current_bar.close > previous_bar.close:
                        wins += 1
                    equity *= 1 + (current_bar.close - previous_bar.close) / previous_bar.close
                    trade_prices.append(current_bar.close)
            elif trend_signal == "down" and obi_signal.value < 0 and volume_signal.value < 0 and current_bar.close < previous_bar.close:
                if equity > 0:
                    trades += 1
                    if current_bar.close < previous_bar.close:
                        wins += 1
                    equity *= 1 + (current_bar.close - previous_bar.close) / previous_bar.close
                    trade_prices.append(current_bar.close)

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
            equity_series=equity_series,
            trade_prices=trade_prices,
        )
