"""Score a strategy the way the research log judges one: every check a new signal must pass, one row per symbol.

A single backtest answers "did it make money here?". The scorecard adds the
questions that decide whether the result is real and tradable:

- **Holdout:** does the in-sample result survive on data the choice never saw?
- **Buy-and-hold:** does it beat just holding the coin over the same bars?
- **Costs:** does it survive maker fees, taker fees and 3x funding? At zero
  cost it shows whether the signal exists at all.
- **Placebo:** does the *timing* matter? The same position series is shifted
  in time (a circular shift keeps the trade count, holding periods and
  exposure, and breaks the link to prices), and the real Sharpe is ranked
  against those placebos. A trend strategy in a bull market can beat buy-
  and-hold on exposure alone; the placebo catches that.
- **Consistency:** how many calendar years were positive, and the worst one.

`checks(card)` turns the numbers into pass/fail columns with the thresholds
used in `docs/research_guide.md`. Passing them is the bar for a portfolio
candidate, not a guarantee. The last test is whether it improves the book
(`src.portfolio.backtest.candidate_report`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.simple_backtest import StrategyFn, _normalize_signal
from src.research.catalog import build_strategy
from src.research.engine import CostSettings, ResearchRun, _infer_interval_seconds, run_strategy

SECONDS_PER_YEAR = 365 * 86400


def positions_for(strategy: StrategyFn, bars: Sequence[Any]) -> np.ndarray:
    """The position (-1/0/1) the strategy holds after each bar's close; vectorized when it has `signal_series`."""
    signal_series = getattr(strategy, "signal_series", None)
    if callable(signal_series):
        return np.sign(np.nan_to_num(np.asarray(signal_series(bars), dtype=float)))
    return np.array([float(np.sign(_normalize_signal(strategy(bars[: index + 1], index, bar)))) for index, bar in enumerate(bars)])


def position_returns(positions: np.ndarray, close: np.ndarray, *, cost_per_side: float, funding_per_bar: float = 0.0) -> np.ndarray:
    """Per-bar returns of holding `positions` (decided at each close, held over the next bar), net of costs and funding.

    A position change at a close costs `cost_per_side` per unit changed (a
    flip costs twice). Longs pay `funding_per_bar` and shorts receive it.
    """
    positions = np.asarray(positions, dtype=float)
    close = np.asarray(close, dtype=float)
    held = np.concatenate([[0.0], positions[:-1]])
    price_return = np.zeros(len(close))
    price_return[1:] = close[1:] / close[:-1] - 1.0
    traded = np.abs(np.diff(np.concatenate([[0.0], positions])))
    return held * price_return - traded * cost_per_side - held * funding_per_bar


def _sharpe(returns: np.ndarray, periods_per_year: float) -> float:
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    return float(np.mean(returns) / std * np.sqrt(periods_per_year)) if std > 0 else 0.0


def placebo_beaten(
    positions: np.ndarray,
    close: np.ndarray,
    *,
    runs: int = 200,
    seed: int = 0,
    cost_per_side: float = 0.0,
    funding_per_bar: float = 0.0,
    periods_per_year: float = 365.0,
) -> float:
    """Share of time-shifted copies of `positions` whose Sharpe is below the real one (0.9+ suggests the timing is real).

    Shifts are at least a tenth of the window, so no placebo is a near copy.
    A strategy that is always in the market scores 0: shifting changes
    nothing, so its result says nothing about timing.
    """
    positions = np.asarray(positions, dtype=float)
    count = len(positions)
    if count < 20 or runs < 1:
        return float("nan")
    real = _sharpe(position_returns(positions, close, cost_per_side=cost_per_side, funding_per_bar=funding_per_bar), periods_per_year)
    margin = max(1, count // 10)
    shifts = np.random.default_rng(seed).integers(margin, count - margin, size=runs)
    placebos = [
        _sharpe(position_returns(np.roll(positions, int(shift)), close, cost_per_side=cost_per_side, funding_per_bar=funding_per_bar), periods_per_year)
        for shift in shifts
    ]
    return float(np.mean(np.asarray(placebos) < real))


def yearly_returns(run: ResearchRun) -> pd.Series:
    """Calendar-year returns of a run's mark-to-market equity, from its measurement start (the first year may be partial)."""
    timestamps = pd.DatetimeIndex(pd.to_datetime([bar.timestamp for bar in run.bars], utc=True))
    equity = pd.Series(run.result.mtm_equity_series[: len(timestamps)], index=timestamps[: len(run.result.mtm_equity_series)])
    equity = equity.iloc[run.measure_start - 1 :]
    year_end = equity.groupby(equity.index.year).last()
    start = pd.Series([equity.iloc[0]], index=[year_end.index[0] - 1])
    return pd.concat([start, year_end]).pct_change().dropna().rename("return")


def scorecard(
    data: Mapping[str, Sequence[Any]],
    strategy: str | StrategyFn,
    *,
    params: dict[str, Any] | None = None,
    costs: CostSettings | None = None,
    holdout_fraction: float = 0.3,
    measure_start: int | None = None,
    placebo_runs: int = 200,
    seed: int = 0,
    label: str | None = None,
) -> pd.DataFrame:
    """Every check for one strategy on each symbol in `data` (symbol -> bars); one row per symbol.

    Args:
        strategy: A registered name (with `params`, which may include
            `long_only`) or a ready StrategyFn, e.g. from `rule_strategy`.
        costs: Defaults to Kraken perp costs (`CostSettings.perp()`).
        measure_start: First measured bar; a StrategyFn needs it set to its
            warmup (names use the catalog's).
    """
    costs = costs or CostSettings.perp()
    params = dict(params or {})
    strategy_fn = build_strategy(strategy, **params) if isinstance(strategy, str) else strategy
    name = label or (strategy if isinstance(strategy, str) else getattr(strategy, "__name__", "custom"))
    maker = replace(costs, fee_pct=costs.maker_fee_pct if costs.maker_fee_pct is not None else costs.fee_pct, slippage_bps=0.0)
    zero = replace(costs, fee_pct=0.0, slippage_bps=0.0, funding_pct_per_day=0.0)
    stressed = replace(costs, funding_pct_per_day=costs.funding_pct_per_day * 3.0) if costs.funding_pct_per_day else None

    def run(bars: Sequence[Any], cost: CostSettings) -> ResearchRun:
        if isinstance(strategy, str):
            return run_strategy(bars, strategy, params=params, costs=cost, holdout_fraction=holdout_fraction, measure_start=measure_start)
        return run_strategy(bars, strategy_fn, costs=cost, holdout_fraction=holdout_fraction, measure_start=measure_start, label=name)

    rows = []
    for symbol, bars in data.items():
        bars = list(bars)
        base = run(bars, costs)
        in_sample, holdout = base.metrics["in_sample"], base.metrics.get("holdout", {})
        interval = _infer_interval_seconds(bars)
        per_year = SECONDS_PER_YEAR / interval
        window = slice(base.measure_start, base.split_index)
        close = np.array([float(bar.close) for bar in bars])
        years = yearly_returns(base)
        rows.append({
            "strategy": name,
            "symbol": symbol,
            "is_sharpe": in_sample["sharpe"],
            "ho_sharpe": holdout.get("sharpe", np.nan),
            "is_buy_hold_sharpe": in_sample["buy_hold_sharpe"],
            "ho_buy_hold_sharpe": holdout.get("buy_hold_sharpe", np.nan),
            "is_max_drawdown": in_sample["max_drawdown"],
            "ho_max_drawdown": holdout.get("max_drawdown", np.nan),
            "trades_per_year": in_sample["trades"] / (in_sample["bars"] / per_year) if in_sample["bars"] else np.nan,
            "is_exposure": in_sample["exposure"],
            "is_avg_trade": in_sample["avg_trade"],
            "is_sharpe_maker": run(bars, maker).metrics["in_sample"]["sharpe"],
            "is_sharpe_zero_cost": run(bars, zero).metrics["in_sample"]["sharpe"],
            "is_sharpe_funding_x3": run(bars, stressed).metrics["in_sample"]["sharpe"] if stressed else np.nan,
            "placebo_beaten_is": placebo_beaten(
                positions_for(strategy_fn, bars)[window], close[window], runs=placebo_runs, seed=seed,
                cost_per_side=costs.fee_pct / 100.0 + costs.slippage_bps / 10_000.0,
                funding_per_bar=costs.funding_pct_per_day / 100.0 * interval / 86400, periods_per_year=per_year,
            ),
            "years_positive": float((years > 0).mean()) if len(years) else np.nan,
            "worst_year": float(years.min()) if len(years) else np.nan,
        })
    return pd.DataFrame(rows)


CHECKS = {
    "beats_buy_hold_is": "in-sample Sharpe above buy-and-hold's",
    "holds_up_ho": "holdout Sharpe above 0 and at least half the in-sample Sharpe",
    "timing_is_real": "beats at least 90% of time-shifted placebos in-sample",
    "survives_costs": "with 3x funding (taker costs on spot) the in-sample Sharpe stays above 0 and keeps half the zero-cost Sharpe",
    "consistent": "at least 60% of calendar years positive",
}


def checks(card: pd.DataFrame) -> pd.DataFrame:
    """Pass/fail per check (see `CHECKS`) for each scorecard row, plus how many passed."""
    stressed = card["is_sharpe_funding_x3"].fillna(card["is_sharpe"])
    out = pd.DataFrame({
        "strategy": card["strategy"],
        "symbol": card["symbol"],
        "beats_buy_hold_is": card["is_sharpe"] > card["is_buy_hold_sharpe"],
        "holds_up_ho": (card["ho_sharpe"] > 0) & (card["ho_sharpe"] >= 0.5 * card["is_sharpe"]),
        "timing_is_real": card["placebo_beaten_is"] >= 0.9,
        "survives_costs": (stressed > 0) & (stressed >= 0.5 * card["is_sharpe_zero_cost"]),
        "consistent": card["years_positive"] >= 0.6,
    })
    out["passed"] = out[list(CHECKS)].sum(axis=1).astype(str) + f"/{len(CHECKS)}"
    return out
