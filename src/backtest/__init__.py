"""Backtesting and simulation components."""

from __future__ import annotations

from src.backtest.plotting import StrategyPlotter
from src.backtest.simple_backtest import BacktestResult, SimpleBacktester, moving_average_crossover_strategy
from src.backtest.simulator import EventDrivenSimulator, EventDrivenTrade

__all__ = [
    "BacktestResult",
    "SimpleBacktester",
    "StrategyPlotter",
    "moving_average_crossover_strategy",
    "EventDrivenSimulator",
    "EventDrivenTrade",
]
