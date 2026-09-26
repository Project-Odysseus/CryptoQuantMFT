"""Funding carry: long spot and short the perpetual, so the price risk cancels and the position collects funding.

When funding is positive, longs pay shorts every settlement. A position that is
long 1 unit of spot and short 1 unit of perp has almost no price exposure and
receives that funding. What it earns after fees depends on how often it must
get in and out (funding turns negative in bear markets) and on each venue's
fees. Both legs pay a fee on the way in and again on the way out.

Everything here is per unit of notional. The capital needed is the spot leg
plus margin for the short perp, so the return on capital is lower. At 2x perp
leverage it is 1.5 units of capital per unit of notional (`capital_per_notional`).
Not modelled: the basis (the perp's premium over spot when entering and
exiting), and moving collateral between the legs when the price moves a lot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365.0


def daily_funding(timestamps: Sequence[Any], rates: Sequence[float], *, payments_per_value: float = 1.0) -> pd.Series:
    """Funding paid per unit of long notional over each UTC day, from settlement times and rates.

    A daily holding runs from 00:00 to 24:00 UTC and pays the settlements after
    00:00 up to and including 24:00, so a settlement at midnight belongs to the
    day that just ended. `payments_per_value` scales a quoted rate into what
    was actually paid at that timestamp (e.g. 1/8 for an hourly series quoted as
    an 8-hour rate).
    """
    times = pd.DatetimeIndex(pd.to_datetime(list(timestamps), utc=True))
    frame = pd.DataFrame({"funding": np.asarray(rates, dtype=float) * payments_per_value}, index=times).dropna()
    days = (frame.index - pd.Timedelta(milliseconds=1)).floor("D")
    return frame.groupby(days)["funding"].sum().rename("funding")


@dataclass(frozen=True, slots=True)
class CarryResult:
    """Daily net carry (per unit of notional) and what drove it."""

    net: pd.Series
    funding: pd.Series
    costs: pd.Series
    in_position: pd.Series

    def summary(self, capital_per_notional: float = 1.5) -> dict[str, float]:
        """Yearly figures in % of notional, and net on capital (spot plus perp margin)."""
        years = len(self.net) / DAYS_PER_YEAR
        worst_30d = self.net.rolling(30).sum().min() if len(self.net) >= 30 else float("nan")
        switches = float(self.in_position.astype(int).diff().abs().sum())
        net_pct = float(self.net.sum() / years * 100) if years > 0 else float("nan")
        return {
            "funding_pct_per_year": float(self.funding.sum() / years * 100) if years > 0 else float("nan"),
            "cost_pct_per_year": float(self.costs.sum() / years * 100) if years > 0 else float("nan"),
            "net_pct_per_year": net_pct,
            "net_on_capital_pct_per_year": net_pct / capital_per_notional,
            "time_in_position": float(self.in_position.mean()),
            "entries_per_year": switches / 2 / years if years > 0 else float("nan"),
            "worst_30d_pct": float(worst_30d * 100),
            "years": years,
        }


def carry_backtest(
    funding: pd.Series,
    *,
    round_trip_cost: float,
    enter_above: float | None = None,
    exit_below: float = 0.0,
    lookback_days: int = 7,
) -> CarryResult:
    """Hold the carry position (short perp, long spot) and collect `funding` each day it is held.

    With `enter_above` None the position is always held (one entry and one
    exit). Otherwise it is opened when the trailing `lookback_days` mean funding,
    annualised, rises above `enter_above` (e.g. 0.10 = 10% a year), and closed
    when it falls below `exit_below`. The decision is made at a day's close and
    applies from the next day. Half of `round_trip_cost` (both legs, fees plus
    slippage, as a fraction of notional) is paid at each entry and each exit.
    """
    funding = funding.sort_index().astype(float)
    if enter_above is None:
        held = pd.Series(True, index=funding.index)
    else:
        trailing = funding.rolling(lookback_days, min_periods=lookback_days).mean() * DAYS_PER_YEAR
        state, decisions = False, []
        for value in trailing.to_numpy():
            if np.isfinite(value):
                if not state and value > enter_above:
                    state = True
                elif state and value < exit_below:
                    state = False
            decisions.append(state)
        held = pd.Series(decisions, index=funding.index).shift(1, fill_value=False)
    changes = held.astype(int).diff().abs().fillna(held.astype(int))
    costs = changes * round_trip_cost / 2.0
    if len(held) and held.iloc[-1]:
        costs.iloc[-1] += round_trip_cost / 2.0  # close at the end so every entry pays its exit
    earned = funding.where(held, 0.0)
    return CarryResult(net=earned - costs, funding=earned, costs=costs, in_position=held)
