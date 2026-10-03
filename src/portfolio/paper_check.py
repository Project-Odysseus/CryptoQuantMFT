"""Paper against backtest: did the running book hold what the research backtest says it should have, and earn the same?

The backtest and the runtime share their decision code, and tests keep them equal on synthetic bars. This checks the
same thing on the days a book actually ran: the backtest (`run_book`, with whole lots) is replayed over the candles
up to now, and at every bar the running book decided on, its position (from its stored snapshots) is put next to the
backtest's position at that bar, and its return since the first compared bar next to the backtest's.

What a difference means:

- positions differ by more than a lot or the rebalance band: a decision differed (different candles, a missed bar, a
  bug). This is the first thing to look at.
- positions agree but returns drift apart: costs, slippage or funding differ from what the backtest assumes. For a
  live book this gap *is* the real cost model, and its trend is the first sign of decay.

The backtest is started at the book's own first decision bar with the book's own capital (`start_at`), so both
begin flat at the same moment and one lot is the same share of both. Expect agreement within one lot or the
rebalance band; the first bar is reported separately, since a restart or an adopted position can differ there.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from src.portfolio.backtest import PortfolioBacktest, PortfolioInputs
from src.runtime.config import BAR_INTERVALS


def decision_frame(snapshots: Sequence[Mapping[str, Any]], grid_interval: str) -> pd.DataFrame:
    """The book's snapshots taken at decisions, one row per grid bar: `equity` and `w:<instrument>` (weight held after trading).

    A decision made at time t is for the grid bar that had just closed, stamped (as bars are) at its open.
    """
    step = pd.Timedelta(seconds=BAR_INTERVALS[grid_interval])
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for snap in sorted(snapshots, key=lambda item: pd.Timestamp(item["timestamp"])):
        instruments = snap.get("instruments") or {}
        if not any(row.get("target") is not None for row in instruments.values()):
            continue  # an hourly snapshot between decisions
        stamp = pd.Timestamp(snap["timestamp"])
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        bar = stamp.floor(step) - step
        rows[bar] = {"equity": float(snap["equity"]), **{f"w:{name}": float(row.get("weight") or 0.0) for name, row in instruments.items()}}
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def start_at(inputs: PortfolioInputs, first_bar: pd.Timestamp) -> PortfolioInputs:
    """`inputs` with the backtest starting at `first_bar`; everything before it becomes warmup history.

    The running book started flat at its first decision with its own capital. A backtest that has been compounding
    since 2020 has a different equity by then, so one lot is a different share of it and the weights can't be
    compared. Starting the backtest at the same bar with the same capital makes them the same experiment.
    """
    position = int(inputs.prices.index.searchsorted(first_bar))
    if position <= 0 or position >= len(inputs.prices):
        return inputs
    warm_prices = inputs.prices.iloc[:position] if inputs.warmup_prices is None else pd.concat([inputs.warmup_prices, inputs.prices.iloc[:position]])
    warm_weights = inputs.sleeve_weights.iloc[:position] if inputs.warmup_sleeve_weights is None else pd.concat([inputs.warmup_sleeve_weights, inputs.sleeve_weights.iloc[:position]])
    return replace(inputs, prices=inputs.prices.iloc[position:], sleeve_weights=inputs.sleeve_weights.iloc[position:], measure_start=inputs.prices.index[position],
                   warmup_prices=warm_prices, warmup_sleeve_weights=warm_weights)


def compare(snapshots: Sequence[Mapping[str, Any]], book: PortfolioBacktest, grid_interval: str) -> dict[str, Any]:
    """The running book next to `book` (a `run_book(..., lots=True)` result over the same candles).

    Returns `positions` (per bar and instrument: paper weight, backtest weight, difference), `returns` (per bar:
    each side's cumulative return since the first compared bar and the gap), and `summary`.
    """
    paper = decision_frame(snapshots, grid_interval)
    held = book.result.weights
    common = paper.index.intersection(held.index) if len(paper) else paper.index
    if not len(common):
        return {"positions": pd.DataFrame(), "returns": pd.DataFrame(), "summary": {"bars": 0}}
    rows = []
    for instrument in held.columns:
        column = f"w:{instrument}"
        for bar in common:
            ours = float(paper.at[bar, column]) if column in paper and pd.notna(paper.at[bar, column]) else 0.0
            theirs = float(held.at[bar, instrument])
            if ours or theirs:
                rows.append({"bar": bar, "instrument": instrument, "paper": ours, "backtest": theirs, "difference": ours - theirs})
    positions = pd.DataFrame(rows, columns=["bar", "instrument", "paper", "backtest", "difference"])
    equity = book.result.equity
    returns = pd.DataFrame({"paper": paper.loc[common, "equity"] / paper.loc[common[0], "equity"] - 1.0, "backtest": equity.loc[common] / equity.loc[common[0]] - 1.0})
    returns["gap"] = returns["paper"] - returns["backtest"]
    after_first = positions[positions["bar"] > common[0]] if len(common) > 1 else positions.iloc[:0]
    summary = {
        "bars": int(len(common)), "first_bar": common[0], "last_bar": common[-1],
        "mean_abs_position_gap": float(positions["difference"].abs().mean()) if len(positions) else 0.0,
        "max_abs_position_gap": float(positions["difference"].abs().max()) if len(positions) else 0.0,
        "max_abs_position_gap_after_first_bar": float(after_first["difference"].abs().max()) if len(after_first) else float("nan"),
        "paper_return": float(returns["paper"].iloc[-1]), "backtest_return": float(returns["backtest"].iloc[-1]), "return_gap": float(returns["gap"].iloc[-1]),
        "worst_instrument": str(positions.loc[positions["difference"].abs().idxmax(), "instrument"]) if len(positions) else None,
    }
    return {"positions": positions, "returns": returns, "summary": summary}


def one_lot_weight(config: Any, prices: Mapping[str, float], equity: float) -> float:
    """The largest share of equity one lot of any instrument is: position gaps below this are rounding, not decisions."""
    if equity <= 0:
        return float("nan")
    return float(max((float(spec.lot_step or 0.0) * float(prices.get(name, 0.0)) / equity for name, spec in config.instruments.items()), default=0.0))


def verdict(summary: Mapping[str, Any], *, tolerance: float) -> str:
    """One line: do the book and the backtest agree, given a position tolerance (a lot, or the rebalance band)?"""
    if not summary.get("bars"):
        return "No decision bar of the running book is in the backtest yet."
    gap = summary["max_abs_position_gap_after_first_bar"]
    if not np.isfinite(gap):
        return "One bar so far: the first bar is the cold start, so nothing can be compared yet."
    if gap <= tolerance:
        return f"Positions agree at every bar after the first (largest gap {gap:.1%} of equity, tolerance {tolerance:.1%}); returns differ by {summary['return_gap']:+.2%}."
    return (f"Positions DIFFER: up to {gap:.1%} of equity ({summary['worst_instrument']}), tolerance {tolerance:.1%}. A decision differed between "
            "the running book and the backtest: check the bars it saw and its events for that time.")
