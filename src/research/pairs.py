"""Pairs trading by the distance method: pick the pairs that tracked each other, trade their divergences back to zero.

The rule is Gatev, Goetzmann and Rouwenhorst's (2006) and is written out in research/prereg/H8.txt. `pair_weights`
turns daily closes into target weights (date x coin), which `src/research/portfolio.simulate_portfolio` then trades
with fees, slippage and funding like any other book. Nothing at day t uses a close after t: pairs are formed from
the days before a period, and a position decided at t's close is held from t (or, with `delay=1`, from the next day).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
import pandas as pd


@dataclass(slots=True)
class PairsResult:
    """Target weights and one row per trade."""

    weights: pd.DataFrame
    trades: pd.DataFrame
    periods: list[dict[str, object]] = field(default_factory=list)


def pair_weights(close: pd.DataFrame, eligible: pd.DataFrame, volume: pd.DataFrame, *, formation: int = 90, trading: int = 30, entry: float = 2.0, coins: int = 10,
                 pairs: int = 5, leg: float = 0.10, delay: int = 1, start: pd.Timestamp | None = None) -> PairsResult:
    """Target weights of the pairs book.

    Args:
        close: Daily closes, date x coin (NaN when not trading).
        eligible: Whether each coin may be traded on each date (e.g. listed on the venue by then).
        volume: Average daily quote volume used to pick the `coins` most liquid at each formation date.
        formation, trading: Days used to pick pairs, and days they are then traded.
        entry: Open when the spread is this many formation standard deviations from zero.
        leg: Share of equity held long, and short, per open pair.
        delay: Days between a decision and its trade (1: decided at a close, traded at the next).
        start: First formation date considered (default: as early as `formation` allows).
    """
    dates = close.index
    weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
    trades: list[dict[str, object]] = []
    periods: list[dict[str, object]] = []
    first = formation if start is None else max(formation, int(dates.searchsorted(start)))
    for begin in range(first, len(dates), trading):
        window = close.iloc[begin - formation: begin]
        ranked = volume.iloc[begin - 1].where(eligible.iloc[begin - 1] & window.notna().all()).dropna().sort_values(ascending=False)
        names = list(ranked.index[:coins])
        if len(names) < coins:
            continue
        scaled = window[names] / window[names].iloc[0]
        distances = sorted((float(((scaled[a] - scaled[b]) ** 2).sum()), a, b) for a, b in combinations(names, 2))
        chosen = distances[:pairs]
        period = close.iloc[begin: begin + trading]
        periods.append({"start": dates[begin], "coins": names, "pairs": [(a, b) for _distance, a, b in chosen]})
        base = window.iloc[-1]
        for _distance, a, b in chosen:
            sigma = float((scaled[a] - scaled[b]).std())
            spread = (period[a] / base[a] - period[b] / base[b]).to_numpy()
            side, opened = 0, None  # +1: long a, short b
            for step, value in enumerate(spread):
                last = step == len(spread) - 1
                if not np.isfinite(value):  # a coin stopped trading: stand aside
                    wanted = 0
                elif side == 0:
                    wanted = 0 if last or sigma <= 0 else (-1 if value > entry * sigma else 1 if value < -entry * sigma else 0)
                else:
                    converged = (side > 0 and value >= 0) or (side < 0 and value <= 0)
                    wanted = 0 if converged or last else side
                if wanted != side:
                    if side != 0:
                        trades.append({"a": a, "b": b, "side": side, "opened": opened, "closed": period.index[step], "days": (period.index[step] - opened).days,
                                       "converged": bool(np.isfinite(value) and not (last and not ((side > 0 and value >= 0) or (side < 0 and value <= 0))))})
                    opened = period.index[step] if wanted != 0 else None
                    side = wanted
                if side != 0:
                    position = begin + step + delay
                    if position < len(dates):
                        weights.iat[position, weights.columns.get_loc(a)] += side * leg
                        weights.iat[position, weights.columns.get_loc(b)] -= side * leg
    return PairsResult(weights=weights, trades=pd.DataFrame(trades, columns=["a", "b", "side", "opened", "closed", "days", "converged"]), periods=periods)
