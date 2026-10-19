"""How much capital can a strategy run, and what does trading really cost? (plan 7.6)

- `capacity_curve(lab, item, adv_usd, sizes)`: the strategy re-run at each capital with square-root market impact
  (cost per unit traded = coefficient x sqrt(traded notional / daily traded value)), giving net Sharpe, impact cost
  and participation per size. Two capacities come out: where the Sharpe falls to half its small-size value, and
  where the 99th-percentile daily participation reaches the limit.
- `implementation_shortfall(fills)`: realised cost of paper or live fills against the decision price, in basis
  points of notional, per instrument and in total, next to the cost research assumed.

The impact coefficient is about Y x daily volatility with Y near 1 (Almgren et al. 2005; Toth et al. 2011): 0.01 for
an equity index with 1% daily moves, so 1% of a day's volume costs about 10 bps. Daily traded values are assumptions
stated in the spec; check them against the venue before relying on a capacity figure.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from quantlab.analytics.performance import metrics
from quantlab.engine.backtest import run_backtest
from quantlab.engine.components import Targets

SIZES_USD = (1e6, 1e7, 1e8, 1e9, 1e10)


def capacity_curve(lab: Any, item: Any, adv_usd: Mapping[str, float], *, sizes: Sequence[float] = SIZES_USD, impact_coefficient: float = 0.01,
                   max_participation: float = 0.01) -> dict[str, Any]:
    """Net Sharpe, impact cost and participation at each capital (see the module docstring)."""
    targets = item if isinstance(item, Targets) else Targets(lab._as_idea(item).forecast)
    instruments = lab.instruments
    missing = [name for name in instruments if name not in adv_usd]
    if missing:
        raise ValueError(f"no daily traded value for {missing}: add them to the spec's [capacity] adv_usd")
    volume = pd.DataFrame({name: float(adv_usd[name]) for name in instruments}, index=lab.panel.index)
    weights = 1.0 / max(len(instruments), 1)
    rows = {}
    for size in sizes:
        config = dataclasses.replace(lab.engine, impact_coefficient=impact_coefficient, capital=float(size))
        result = run_backtest(targets, lab.returns, costs_bps=lab.costs, sizer=lab.sizer, weights=weights, config=config, volume_notional=volume)
        participation = (result.traded * size / volume).replace([np.inf, -np.inf], np.nan)
        base = run_backtest(targets, lab.returns, costs_bps=lab.costs, sizer=lab.sizer, weights=weights, config=dataclasses.replace(lab.engine, capital=float(size)))
        years = len(result.pnl) / lab.engine.periods
        rows[size] = {"Sharpe": metrics(result).get("Sharpe"), "impact cost a year": float((result.costs.sum().sum() - base.costs.sum().sum()) / years),
                      "participation, 99th pct day": float(np.nanquantile(participation.to_numpy().ravel(), 0.99)),
                      "days over the participation limit": float((participation > max_participation).any(axis=1).mean())}
    table = pd.DataFrame(rows).T
    table.index.name = "capital (USD)"
    small = table["Sharpe"].iloc[0]
    halved = table.index[table["Sharpe"] < 0.5 * small] if np.isfinite(small) and small > 0 else table.index[:0]
    limited = table.index[table["participation, 99th pct day"] > max_participation]
    return {"table": table, "capacity (Sharpe halves)": float(halved[0]) if len(halved) else float("inf"),
            "capacity (participation limit)": float(limited[0]) if len(limited) else float("inf")}


def implementation_shortfall(fills: pd.DataFrame, *, assumed_bps: Mapping[str, float] | float | None = None) -> pd.DataFrame:
    """Realised cost of fills against decision prices.

    `fills` columns: instrument, side (+1 buy, -1 sell), quantity, fill_price, decision_price, and optionally
    multiplier (default 1). Shortfall in bps = side x (fill - decision) / decision x 1e4, weighted by notional.
    """
    frame = fills.copy()
    frame["multiplier"] = frame["multiplier"] if "multiplier" in frame else 1.0
    frame["notional"] = (frame["quantity"].abs() * frame["decision_price"] * frame["multiplier"]).astype(float)
    frame["bps"] = frame["side"] * (frame["fill_price"] - frame["decision_price"]) / frame["decision_price"] * 1e4
    rows = {}
    for instrument, group in frame.groupby("instrument"):
        weight = group["notional"] / group["notional"].sum()
        rows[instrument] = {"fills": len(group), "notional": float(group["notional"].sum()), "shortfall bps (notional-weighted)": float((group["bps"] * weight).sum()),
                            "median bps": float(group["bps"].median()), "worst bps": float(group["bps"].max())}
    table = pd.DataFrame(rows).T
    weight = frame["notional"] / frame["notional"].sum()
    table.loc["all"] = {"fills": len(frame), "notional": float(frame["notional"].sum()), "shortfall bps (notional-weighted)": float((frame["bps"] * weight).sum()),
                        "median bps": float(frame["bps"].median()), "worst bps": float(frame["bps"].max())}
    if assumed_bps is not None:
        table["assumed bps"] = [float(assumed_bps.get(name, np.nan)) if isinstance(assumed_bps, Mapping) else float(assumed_bps) for name in table.index]
    return table
