"""A small bar-based backtester for the hypothesis studies: next-open fills, actual funding, intrabar stops.

Two entry points:

- `run_targets`: a target weight per bar, decided at that bar's close, becomes the position from the next bar's
  open. Returns are open-to-open, so a signal can never earn the bar it was computed on. Every change in position
  pays `cost_per_side` on the traded notional at the fill bar. A perp position pays (long) or receives (short)
  funding at each actual settlement time while it is held.
- `simulate_trade`: one discrete trade for event studies, entered at a bar's open, held a fixed number of bars,
  with a stop checked against each bar's high and low (not its close). A bar that opens through the stop fills at
  its open (a gap), otherwise at the stop price.

Weights are fractions of equity, rebalanced to target each bar (drift between bars is ignored, which is standard
for daily and hourly research and immaterial at these sizes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


def _ns(times: Any) -> np.ndarray:
    return pd.DatetimeIndex(pd.to_datetime(times, utc=True)).as_unit("ns").asi8  # same unit whatever the source's


@dataclass(frozen=True, slots=True)
class TargetRun:
    """Per-bar results of `run_targets`, indexed by bar open time (the bar's return runs open to next open)."""

    frame: pd.DataFrame  # position, gross, cost, funding, net, traded

    @property
    def net(self) -> pd.Series:
        return self.frame["net"]

    @property
    def gross(self) -> pd.Series:
        return self.frame["gross"]

    def trades(self) -> int:
        """Entries and flips (a position opened from flat, or reversed)."""
        pos = np.sign(self.frame["position"].to_numpy())
        prev = np.concatenate([[0.0], pos[:-1]])
        return int(np.sum((pos != 0) & (pos != prev)))


def funding_by_bar(bar_opens: pd.DatetimeIndex, settlements: pd.DataFrame) -> np.ndarray:
    """Sum of funding rates settled during each bar (a settlement exactly at a bar's end belongs to that bar)."""
    out = np.zeros(len(bar_opens))
    if settlements is None or settlements.empty or len(bar_opens) == 0:
        return out
    index = np.searchsorted(_ns(bar_opens), _ns(settlements["timestamp"]), side="left") - 1
    valid = (index >= 0) & (index < len(bar_opens) - 1)
    np.add.at(out, index[valid], settlements["rate"].to_numpy(dtype=float)[valid])
    return out


def run_targets(bars: pd.DataFrame, targets: pd.Series, *, cost_per_side: float | pd.Series | np.ndarray,
                funding: pd.DataFrame | None = None) -> TargetRun:
    """Backtest target weights decided at each bar's close and filled at the next bar's open.

    Args:
        bars: Candles indexed by open time with an `open` column.
        targets: Target weight per bar (missing bars count as 0), decided with data up to that bar's close.
        cost_per_side: Cost per unit of traded notional, a constant or one value per bar (the fill bar's).
        funding: Perp funding settlements (`timestamp`, `rate` per settlement); longs pay positive rates.
    """
    index = pd.DatetimeIndex(bars.index)
    opens = bars["open"].to_numpy(dtype=float)
    target = targets.reindex(index).fillna(0.0).to_numpy(dtype=float)
    position = np.concatenate([[0.0], target[:-1]])  # held from each bar's open
    returns = np.full(len(index), np.nan)
    returns[:-1] = opens[1:] / opens[:-1] - 1.0
    traded = np.abs(np.diff(position, prepend=0.0))
    cost = traded * (np.asarray(cost_per_side, dtype=float) if np.ndim(cost_per_side) else float(cost_per_side))
    paid = position * funding_by_bar(index, funding) if funding is not None else np.zeros(len(index))
    gross = position * returns
    frame = pd.DataFrame({"position": position, "gross": gross, "cost": cost, "funding": paid, "net": gross - cost - paid, "traded": traded}, index=index)
    return TargetRun(frame.iloc[:-1])  # the last bar has no next open to measure its return


def simulate_trade(bars: pd.DataFrame, entry: int, direction: int, hold: int, *, stop: float | None = None,
                   cost_per_side: float = 0.0, funding_rates: np.ndarray | None = None) -> dict[str, Any]:
    """One trade: enter at `bars.open[entry]`, exit at the open `hold` bars later or at the stop, whichever is first.

    Args:
        bars: Candles with open, high, low (positional indexing).
        entry: Positional index of the entry bar (fill at its open).
        direction: +1 long, -1 short.
        hold: Bars to hold; the exit fills at the open of bar `entry + hold`.
        stop: Stop price, checked against each held bar's low (long) or high (short).
        cost_per_side: Entry and exit each pay this fraction of notional.
        funding_rates: Funding settled during each bar (`funding_by_bar`), paid by longs.
    """
    opens, highs, lows = (bars[column].to_numpy(dtype=float) for column in ("open", "high", "low"))
    last = min(entry + hold, len(opens) - 1)
    if entry >= last:
        return {"complete": False}
    exit_price, exit_bar, stopped = opens[last], last, False
    for k in range(entry, last):
        if stop is not None:
            if direction > 0 and lows[k] <= stop:
                exit_price, exit_bar, stopped = (opens[k] if opens[k] <= stop else stop), k, True
                break
            if direction < 0 and highs[k] >= stop:
                exit_price, exit_bar, stopped = (opens[k] if opens[k] >= stop else stop), k, True
                break
    gross = direction * (exit_price / opens[entry] - 1.0)
    # Settlements during bar k fall at its end: a trade exiting at the open of `last` (or stopped inside bar k)
    # has been through the settlements of bars entry .. exit_bar - 1.
    funding = float(direction * np.sum(funding_rates[entry:exit_bar])) if funding_rates is not None else 0.0
    return {"complete": entry + hold <= len(opens) - 1, "gross": gross, "net": gross - 2 * cost_per_side - funding,
            "funding": funding, "stopped": stopped, "exit_bar": exit_bar, "bars_held": exit_bar - entry}


def performance(returns: pd.Series, *, periods_per_year: float) -> dict[str, float]:
    """Annualised Sharpe, return and vol, max drawdown and worst calendar month of simple per-period returns."""
    values = returns.dropna()
    if len(values) < 3 or values.std(ddof=1) == 0:
        return {"sharpe": 0.0, "ann_return": 0.0, "ann_vol": 0.0, "max_drawdown": 0.0, "worst_month": 0.0, "periods": len(values)}
    equity = (1.0 + values).cumprod()
    drawdown = float((1.0 - equity / equity.cummax()).max())
    monthly = (1.0 + values).groupby(pd.DatetimeIndex(values.index).tz_localize(None).to_period("M")).prod() - 1.0 if isinstance(values.index, pd.DatetimeIndex) else pd.Series(dtype=float)
    return {
        "sharpe": float(values.mean() / values.std(ddof=1) * np.sqrt(periods_per_year)),
        "ann_return": float(values.mean() * periods_per_year),
        "ann_vol": float(values.std(ddof=1) * np.sqrt(periods_per_year)),
        "max_drawdown": drawdown,
        "worst_month": float(monthly.min()) if len(monthly) else 0.0,
        "periods": len(values),
    }


def by_year(returns: pd.Series, *, periods_per_year: float) -> pd.DataFrame:
    """Sum of returns and Sharpe per calendar year."""
    values = returns.dropna()
    groups = values.groupby(pd.DatetimeIndex(values.index).year)
    return pd.DataFrame({"return": groups.sum(), "sharpe": groups.apply(lambda x: x.mean() / x.std(ddof=1) * np.sqrt(periods_per_year) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0)})
