"""How big should the book be? Historical risk of a portfolio config in money terms, and the scale that fits a limit.

Deciding capital and size is a judgement about how much loss you can sit
through without switching the system off at the worst moment. This module
gives the facts for that judgement from the research backtest of the exact
config: the worst day, week and month, value-at-risk and expected shortfall,
the deepest and longest drawdown, the exposure and margin the book used, all
for a given capital. `scale_for_max_drawdown` then finds the `[portfolio]
scale` at which the historical drawdown stays inside a limit you choose.

History understates future risk: the worst drawdown ahead is usually worse
than the worst one behind. Pick a limit with a margin (the report's default
safety factor is 1.5x the historical drawdown).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from src.portfolio.backtest import PortfolioInputs, run_book
from src.portfolio.config import PortfolioConfig


def drawdown_stats(equity: pd.Series) -> dict[str, Any]:
    """Deepest drawdown from a peak, when it happened, and the longest time spent below a previous peak."""
    peak = equity.cummax()
    drawdown = 1.0 - equity / peak
    underwater = drawdown > 0
    longest, current, start = pd.Timedelta(0), None, None
    for stamp, below in underwater.items():
        if below and current is None:
            current = stamp
        elif not below and current is not None:
            longest, current = max(longest, stamp - current), None
    if current is not None:
        longest = max(longest, equity.index[-1] - current)
    trough = drawdown.idxmax() if len(drawdown) else None
    return {"max_drawdown": float(drawdown.max()) if len(drawdown) else 0.0, "max_drawdown_at": trough,
            "longest_underwater_days": longest.total_seconds() / 86400.0, "current_drawdown": float(drawdown.iloc[-1]) if len(drawdown) else 0.0}


def risk_report(config: PortfolioConfig, inputs: PortfolioInputs, *, capital: float, scale: float | None = None,
                funding_pct_per_day: float = 0.01, start: pd.Timestamp | None = None) -> dict[str, Any]:
    """The book's historical risk at `scale` (default: the config's) for `capital` in the base currency.

    Returns fractions of equity and the same numbers in money. Daily figures
    come from daily closes of the book's equity; VaR and expected shortfall
    are historical (the loss exceeded on 5% and 1% of days, and the average
    loss on those days).
    """
    config = replace(config, scale=config.scale if scale is None else scale)
    book = run_book(config, inputs, funding_pct_per_day=funding_pct_per_day)
    equity = book.result.equity
    if start is not None:
        equity = equity[equity.index >= start]
    daily = equity.resample("1D").last().dropna()
    returns = daily.pct_change().dropna()
    weekly = daily.resample("W").last().pct_change().dropna()
    monthly = daily.resample("ME").last().pct_change().dropna()
    stats = drawdown_stats(daily)
    var95, var99 = float(-np.quantile(returns, 0.05)), float(-np.quantile(returns, 0.01))
    es95 = float(-returns[returns <= -var95].mean()) if (returns <= -var95).any() else var95
    es99 = float(-returns[returns <= -var99].mean()) if (returns <= -var99).any() else var99
    gross = book.result.gross_exposure
    notional = book.targets.abs().max()  # the largest share of equity any instrument was asked to hold
    leverage_caps = {instrument: spec.max_leverage for instrument, spec in config.instruments.items()}
    margin_share = float(max((book.targets.abs() / pd.Series(leverage_caps)).sum(axis=1).max(), 0.0))
    years = len(returns) / 365.0
    growth = float(daily.iloc[-1] / daily.iloc[0]) if len(daily) > 1 else 1.0
    fractions = {
        "scale": config.scale,
        "cagr": growth ** (1 / years) - 1 if years > 0 and growth > 0 else float("nan"),
        "annual_vol": float(returns.std() * np.sqrt(365)),
        "sharpe": float(returns.mean() / returns.std() * np.sqrt(365)) if returns.std() > 0 else 0.0,
        "worst_day": float(-returns.min()), "worst_week": float(-weekly.min()), "worst_month": float(-monthly.min()),
        "var_95_day": var95, "es_95_day": es95, "var_99_day": var99, "es_99_day": es99,
        "share_of_months_losing": float((monthly < 0).mean()),
        "avg_gross_exposure": float(gross.mean()), "max_gross_exposure": float(gross.max()),
        "max_instrument_weight": float(notional.max()),
        "max_margin_share": margin_share,  # initial margin needed at the instruments' leverage caps, as a share of equity
        **{key: value for key, value in stats.items() if key != "max_drawdown_at"},
    }
    money = {f"{key}_money": value * capital for key, value in fractions.items()
             if key in {"worst_day", "worst_week", "worst_month", "var_95_day", "es_95_day", "var_99_day", "es_99_day", "max_drawdown"}}
    return {**fractions, **money, "capital": capital, "max_drawdown_at": stats["max_drawdown_at"], "max_gross_notional_money": fractions["max_gross_exposure"] * capital}


def scale_for_max_drawdown(config: PortfolioConfig, inputs: PortfolioInputs, target: float, *, safety: float = 1.5,
                           funding_pct_per_day: float = 0.01, low: float = 0.05, high: float = 3.0, tolerance: float = 0.005) -> float:
    """The largest scale whose historical max drawdown, times `safety`, stays within `target` (bisection).

    Drawdown grows with scale but not exactly in proportion (costs, the
    rebalance band and the risk caps interact), so each candidate scale is
    re-simulated rather than extrapolated.
    """
    if not 0 < target < 1:
        raise ValueError("target must be a drawdown between 0 and 1 (0.2 = 20%)")

    def drawdown(scale: float) -> float:
        book = run_book(replace(config, scale=scale), inputs, funding_pct_per_day=funding_pct_per_day)
        return drawdown_stats(book.result.equity.resample("1D").last().dropna())["max_drawdown"] * safety

    if drawdown(low) > target:
        return low
    if drawdown(high) <= target:
        return high
    while high - low > tolerance:
        middle = (low + high) / 2.0
        low, high = (middle, high) if drawdown(middle) <= target else (low, middle)
    return low
