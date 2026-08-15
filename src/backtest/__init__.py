"""Backtesting and simulation components."""

from __future__ import annotations

from src.backtest.analytics import PerformanceMetrics, build_performance_metrics
from src.backtest.costs import CostModel, build_default_cost_model
from src.backtest.plotting import StrategyPlotter
from src.backtest.runner import BacktestConfig, BacktestComparison, StrategyRegistry, build_cost_model, compare_backtests, resolve_strategy, run_backtest
from src.backtest.simple_backtest import BacktestResult, SimpleBacktester, momentum_breakout_strategy, moving_average_crossover_strategy, signal_trend_strategy
from src.backtest.simulator import EventDrivenSimulator, EventDrivenTrade
from src.backtest.walk_forward import WalkForwardFoldResult, WalkForwardResult, evaluate_walk_forward

__all__ = [
    "BacktestResult",
    "SimpleBacktester",
    "StrategyPlotter",
    "moving_average_crossover_strategy",
    "momentum_breakout_strategy",
    "EventDrivenSimulator",
    "EventDrivenTrade",
    "CostModel",
    "PerformanceMetrics",
    "build_performance_metrics",
    "build_default_cost_model",
    "BacktestConfig",
    "BacktestComparison",
    "StrategyRegistry",
    "build_cost_model",
    "compare_backtests",
    "resolve_strategy",
    "run_backtest",
    "WalkForwardFoldResult",
    "WalkForwardResult",
    "evaluate_walk_forward",
]
