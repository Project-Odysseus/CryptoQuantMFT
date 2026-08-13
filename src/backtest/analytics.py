"""Simple performance analytics for backtest results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(slots=True)
class PerformanceMetrics:
    """Lightweight summary statistics for a completed backtest."""

    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_trade_return: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0


def build_performance_metrics(*, equity_series: Sequence[float], trade_returns: Sequence[float], total_return: float, max_drawdown: float, win_rate: float) -> PerformanceMetrics:
    """Compute a first-pass analytics bundle from equity and trade history."""
    periodic_returns = [
        (equity_series[index] / equity_series[index - 1] - 1.0)
        for index in range(1, len(equity_series))
        if equity_series[index - 1] > 0.0
    ]

    sharpe_ratio = _safe_sharpe(periodic_returns)
    sortino_ratio = _safe_sortino(periodic_returns)
    calmar_ratio = total_return / max_drawdown if max_drawdown > 0.0 else 0.0

    positive_returns = [value for value in trade_returns if value > 0.0]
    negative_returns = [abs(value) for value in trade_returns if value < 0.0]
    avg_trade_return = float(np.mean(trade_returns)) if trade_returns else 0.0
    avg_win = float(np.mean(positive_returns)) if positive_returns else 0.0
    avg_loss = float(np.mean(negative_returns)) if negative_returns else 0.0
    profit_factor = sum(positive_returns) / sum(negative_returns) if negative_returns else 0.0
    expectancy = (win_rate * avg_win) - ((1.0 - win_rate) * avg_loss) if trade_returns else 0.0

    return PerformanceMetrics(
        sharpe_ratio=round(sharpe_ratio, 10),
        sortino_ratio=round(sortino_ratio, 10),
        calmar_ratio=round(calmar_ratio, 10),
        max_drawdown=round(max_drawdown, 10),
        win_rate=round(win_rate, 10),
        profit_factor=round(profit_factor, 10),
        expectancy=round(expectancy, 10),
        avg_trade_return=round(avg_trade_return, 10),
        avg_win=round(avg_win, 10),
        avg_loss=round(avg_loss, 10),
    )


def _safe_sharpe(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    stddev = float(np.std(returns, ddof=0))
    if stddev <= 0.0:
        return 0.0
    return float(np.mean(returns) / stddev * (len(returns) ** 0.5))


def _safe_sortino(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    downside_returns = [value for value in returns if value < 0.0]
    if not downside_returns:
        return 0.0
    downside_stddev = float(np.std(downside_returns, ddof=0))
    if downside_stddev <= 0.0:
        return 0.0
    return float(np.mean(returns) / downside_stddev * (len(returns) ** 0.5))
