"""Simple walk-forward / out-of-sample evaluation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, median
from typing import Any, Sequence

from src.backtest.runner import BacktestConfig, StrategyRegistry, run_backtest


@dataclass(slots=True)
class WalkForwardFoldResult:
    """Summary for a single walk-forward fold."""

    start_index: int
    train_window: int
    test_window: int
    initial_equity: float
    final_equity: float
    return_pct: float
    trades: int
    win_rate: float


@dataclass(slots=True)
class WalkForwardResult:
    """Aggregate results across several walk-forward folds."""

    folds: list[WalkForwardFoldResult] = field(default_factory=list)
    summary: dict[str, float | int] = field(default_factory=dict)


def evaluate_walk_forward(
    bars: Sequence[Any],
    config: BacktestConfig | None = None,
    registry: StrategyRegistry | None = None,
    *,
    train_window: int = 40,
    test_window: int = 20,
    step_size: int = 20,
) -> WalkForwardResult:
    """Evaluate a strategy over sequential walk-forward test windows."""
    if train_window <= 0:
        raise ValueError("train_window must be positive")
    if test_window <= 0:
        raise ValueError("test_window must be positive")
    if step_size <= 0:
        raise ValueError("step_size must be positive")

    resolved_bars = list(bars)
    if len(resolved_bars) < train_window + test_window:
        raise ValueError("Not enough bars for the requested train/test windows")

    folds: list[WalkForwardFoldResult] = []
    for start_index in range(0, len(resolved_bars) - train_window - test_window + 1, step_size):
        fold_bars = resolved_bars[start_index : start_index + train_window + test_window]
        result = run_backtest(fold_bars, config=config, registry=registry)

        warmup_index = train_window - 1
        if warmup_index < 0 or warmup_index >= len(result.equity_series):
            warmup_index = 0

        start_equity = float(result.equity_series[warmup_index])
        end_equity = float(result.equity_series[-1])
        fold_return = (end_equity / start_equity - 1.0) if start_equity > 0.0 else 0.0

        test_bars = fold_bars[train_window:]
        if test_bars:
            first_test_timestamp = test_bars[0].timestamp if hasattr(test_bars[0], "timestamp") else None
            filtered_trades = [
                trade for trade in result.trade_records if trade.timestamp is not None and first_test_timestamp is not None and trade.timestamp >= first_test_timestamp
            ]
        else:
            filtered_trades = []

        folds.append(
            WalkForwardFoldResult(
                start_index=start_index,
                train_window=train_window,
                test_window=test_window,
                initial_equity=start_equity,
                final_equity=end_equity,
                return_pct=fold_return,
                trades=len(filtered_trades),
                win_rate=0.0 if not filtered_trades else sum(1 for trade in filtered_trades if trade.return_pct > 0.0) / len(filtered_trades),
            )
        )

    if not folds:
        return WalkForwardResult(folds=[], summary={"fold_count": 0, "avg_return": 0.0, "median_return": 0.0, "positive_folds": 0})

    returns = [fold.return_pct for fold in folds]
    summary = {
        "fold_count": len(folds),
        "avg_return": round(mean(returns), 6),
        "median_return": round(median(returns), 6),
        "positive_folds": sum(1 for value in returns if value > 0.0),
        "cumulative_return": round((1.0 + mean(returns)) ** len(returns) - 1.0, 6),
    }
    return WalkForwardResult(folds=folds, summary=summary)
