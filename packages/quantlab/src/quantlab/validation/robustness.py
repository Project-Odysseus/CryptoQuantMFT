"""Does the result survive small changes? The robustness suite (plan 4.5).

A real effect sits on a plateau: nearby parameters, higher costs, other start dates, other markets and other
environments all show it, weaker perhaps, with the same sign. A fluke sits on a spike. Each function takes either a
return series or a `run` callable that produces one (or a BacktestResult), so it works with any strategy.

- `parameter_plateau(run, grid)`: Sharpe over a parameter grid, each point's neighbourhood mean, and the sweep of
  returns for PBO (quantlab.validation.strategy_card.pbo_cscv).
- `cost_sensitivity(run, multipliers)`: Sharpe as costs scale, and the multiplier at which it reaches zero.
- `start_dates(returns)`, `subperiods(returns)`: Sharpe from each start year onward, and in equal sub-periods.
- `environments(returns, labels)` with `market_environments(market)`: Sharpe in bull/bear, calm/volatile markets.
- `cross_market(run, instruments)`: the same rules, unchanged, on other markets.
- `episode_dependence(returns)`: Sharpe without the best months, and the share of P&L from the best days.
"""

from __future__ import annotations

import itertools
import math
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


def _pnl(item: Any) -> pd.Series:
    return item.pnl if hasattr(item, "pnl") else item


def annual_sharpe(returns: pd.Series, periods_per_year: float = 252.0) -> float:
    values = returns.dropna()
    spread = values.std()
    return float(values.mean() / spread * math.sqrt(periods_per_year)) if len(values) > 2 and spread > 0 else float("nan")


def parameter_plateau(run: Callable[..., Any], grid: Mapping[str, Sequence[Any]], *, chosen: Mapping[str, Any] | None = None,
                      periods_per_year: float = 252.0) -> dict[str, Any]:
    """Run every grid point; report each point's Sharpe and the mean Sharpe of its neighbours (one step on any axis).

    `plateau ratio` = neighbourhood mean / the point's own Sharpe: near 1 is a plateau, near 0 or negative a spike.
    """
    names = list(grid)
    points = list(itertools.product(*(grid[name] for name in names)))
    returns = {point: _pnl(run(**dict(zip(names, point)))) for point in points}
    sharpe = {point: annual_sharpe(series, periods_per_year) for point, series in returns.items()}
    rows = []
    for point in points:
        position = [list(grid[name]).index(value) for name, value in zip(names, point)]
        neighbours = []
        for axis in range(len(names)):
            for step in (-1, 1):
                moved = list(position)
                moved[axis] += step
                if 0 <= moved[axis] < len(grid[names[axis]]):
                    neighbours.append(sharpe[tuple(grid[names[i]][moved[i]] for i in range(len(names)))])
        mean = float(np.nanmean(neighbours)) if neighbours else float("nan")
        rows.append({**dict(zip(names, point)), "Sharpe": sharpe[point], "neighbourhood Sharpe": mean,
                     "plateau ratio": mean / sharpe[point] if sharpe[point] and np.isfinite(sharpe[point]) else float("nan")})
    table = pd.DataFrame(rows)
    sweep = pd.DataFrame({", ".join(f"{n}={v}" for n, v in zip(names, point)): series for point, series in returns.items()})
    out: dict[str, Any] = {"table": table, "sweep": sweep, "share of grid above 0": float((table["Sharpe"] > 0).mean())}
    if chosen is not None:
        mask = np.logical_and.reduce([table[name] == value for name, value in chosen.items()])
        out["chosen"] = table[mask].iloc[0].to_dict() if mask.any() else None
    return out


def cost_sensitivity(run: Callable[[float], Any], multipliers: Iterable[float] = (0.0, 1.0, 2.0, 3.0, 5.0), *, periods_per_year: float = 252.0) -> dict[str, Any]:
    """Sharpe at each cost multiplier, and where it crosses zero (linear interpolation; inf if it never does)."""
    values = list(multipliers)
    sharpes = pd.Series({m: annual_sharpe(_pnl(run(m)), periods_per_year) for m in values}, name="Sharpe")
    sharpes.index.name = "cost multiplier"
    breakeven = float("inf")
    for (m0, s0), (m1, s1) in zip(sharpes.items(), list(sharpes.items())[1:]):
        if s0 > 0 >= s1:
            breakeven = float(m0 + (m1 - m0) * s0 / (s0 - s1))
            break
    if sharpes.iloc[0] <= 0:
        breakeven = 0.0
    return {"table": sharpes, "breakeven multiplier": breakeven}


def start_dates(returns: pd.Series, *, periods_per_year: float = 252.0, min_years: float = 3.0) -> pd.Series:
    """Sharpe from the start of each calendar year to the end (only starts leaving `min_years`)."""
    returns = returns.dropna()
    out = {}
    for year in sorted(set(returns.index.year)):
        tail = returns[returns.index.year >= year]
        if len(tail) >= min_years * periods_per_year:
            out[year] = annual_sharpe(tail, periods_per_year)
    return pd.Series(out, name="Sharpe from this year on")


def subperiods(returns: pd.Series, parts: int = 4, *, periods_per_year: float = 252.0) -> pd.DataFrame:
    """Sharpe, annual return and worst drawdown in `parts` equal sub-periods."""
    returns = returns.dropna()
    rows = {}
    for chunk in np.array_split(np.arange(len(returns)), parts):
        piece = returns.iloc[chunk]
        growth = (1.0 + piece).cumprod()
        rows[f"{piece.index[0]:%Y-%m} to {piece.index[-1]:%Y-%m}"] = {"Sharpe": annual_sharpe(piece, periods_per_year), "annual return": float(piece.mean() * periods_per_year),
                                                                       "max drawdown": float((growth / growth.cummax() - 1.0).min())}
    return pd.DataFrame(rows).T


def market_environments(market: pd.Series, *, trend_window: int = 252, vol_window: int = 63) -> dict[str, pd.Series]:
    """Labels known at each close: "bull"/"bear" (trailing-year return), "calm"/"volatile" (vol vs its expanding median)."""
    trailing = (1.0 + market).rolling(trend_window).apply(np.prod, raw=True) - 1.0
    vol = market.rolling(vol_window).std()
    median = vol.expanding(min_periods=vol_window * 4).median()
    return {"trend": pd.Series(np.where(trailing > 0, "bull", "bear"), index=market.index).where(trailing.notna()),
            "volatility": pd.Series(np.where(vol > median, "volatile", "calm"), index=market.index).where(median.notna())}


def environments(returns: pd.Series, labels: Mapping[str, pd.Series], *, periods_per_year: float = 252.0) -> pd.DataFrame:
    """Sharpe and share of time in each environment of each labelling. Labels are lagged one bar (known before)."""
    rows = {}
    for name, label in labels.items():
        lagged = label.shift(1).reindex(returns.index)
        for state in pd.unique(lagged.dropna()):
            piece = returns[lagged == state]
            rows[(name, state)] = {"Sharpe": annual_sharpe(piece, periods_per_year), "annual return": float(piece.mean() * periods_per_year),
                                   "share of time": float(len(piece) / max(lagged.notna().sum(), 1))}
    return pd.DataFrame(rows).T


def cross_market(run: Callable[[str], Any], instruments: Iterable[str], *, periods_per_year: float = 252.0) -> pd.DataFrame:
    """The same rules on each market: Sharpe, years, and the sign agreement across markets."""
    rows = {}
    for instrument in instruments:
        series = _pnl(run(instrument)).dropna()
        rows[instrument] = {"Sharpe": annual_sharpe(series, periods_per_year), "years": len(series) / periods_per_year}
    table = pd.DataFrame(rows).T
    table.attrs["share positive"] = float((table["Sharpe"] > 0).mean())
    return table


def episode_dependence(returns: pd.Series, *, drop_months: Sequence[int] = (1, 3, 5, 10), periods_per_year: float = 252.0) -> dict[str, Any]:
    """Sharpe after removing the best k calendar months, and the share of total P&L from the best 1% of days."""
    returns = returns.dropna()
    months = returns.groupby(returns.index.to_period("M")).sum().sort_values(ascending=False)
    table = {0: annual_sharpe(returns, periods_per_year)}
    for k in drop_months:
        keep = ~returns.index.to_period("M").isin(months.index[:k])
        table[k] = annual_sharpe(returns[keep], periods_per_year)
    best_days = returns.sort_values(ascending=False).iloc[: max(1, len(returns) // 100)]
    total = returns.sum()
    return {"Sharpe without best months": pd.Series(table, name="Sharpe").rename_axis("best months removed"),
            "share of P&L from best 1% of days": float(best_days.sum() / total) if total else float("nan")}
