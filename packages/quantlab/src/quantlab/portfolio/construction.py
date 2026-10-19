"""Portfolios of sleeves: allocation, netting, whole contracts (plan 2.4-2.5).

A sleeve is one strategy's sized positions (weights of equity, dates x instruments), as if it had the whole book.
`build_portfolio` scales each sleeve by an allocation, refitted on a calendar from returns before each refit, nets the
sleeves per instrument, and returns instrument weights for the engine (run with `Sizer(target_vol=None)`, since the
weights are already sized), plus each sleeve's allocated weights for attribution.

Allocation methods (quantlab.portfolio.allocation, ported from the host): fixed, equal, inverse_vol, risk_parity, hrp.

`to_contracts` turns weights into whole contracts and reports what rounding does to the strategy, which matters at
small capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd

from quantlab.portfolio.allocation import ALLOCATION_METHODS, COVARIANCE_METHODS, covariance_scales, sleeve_scales


@dataclass
class PortfolioWeights:
    """Instrument weights after allocation and netting, with the pieces that made them."""

    weights: pd.DataFrame  # dates x instruments
    scales: pd.DataFrame  # dates x sleeves
    allocated: dict[str, pd.DataFrame] = field(default_factory=dict)  # sleeve -> its scaled weights


def sleeve_returns(sleeves: Mapping[str, pd.DataFrame], returns: pd.DataFrame, *, lag: int = 2) -> pd.DataFrame:
    """Each sleeve's own per-bar return at full size (positions earn `lag` bars after the decision, like the engine)."""
    return pd.DataFrame({name: (positions.shift(lag).fillna(0.0) * returns.reindex(columns=positions.columns).fillna(0.0)).sum(axis=1)
                         for name, positions in sleeves.items()})


def allocation_scales(sleeve_rets: pd.DataFrame, budgets: Mapping[str, float], method: str = "equal", *, lookback: int = 90,
                      refit_every: int = 21, min_periods: int = 30) -> pd.DataFrame:
    """Scale per sleeve and bar, refitted every `refit_every` bars from the `lookback` bars before the refit (causal)."""
    if method not in ALLOCATION_METHODS:
        raise ValueError(f"unknown allocation {method!r}; choose from {ALLOCATION_METHODS}")
    names = list(budgets)
    out = pd.DataFrame(np.nan, index=sleeve_rets.index, columns=names)
    for row in range(0, len(out), refit_every):
        past = sleeve_rets.iloc[max(0, row - lookback):row][names]
        if method in COVARIANCE_METHODS:
            scales = covariance_scales(budgets, method, past.to_numpy(), min_periods=min_periods)
        elif method == "inverse_vol":
            active = past.where(past != 0.0)
            volatility = {name: float(np.sqrt((active[name] ** 2).mean())) for name in names if active[name].count() >= min_periods}
            scales = sleeve_scales(budgets, method, volatility=volatility)
        else:
            scales = sleeve_scales(budgets, method)
        out.iloc[row] = [scales[name] for name in names]
    return out.ffill()


def build_portfolio(sleeves: Mapping[str, pd.DataFrame], returns: pd.DataFrame, *, budgets: Mapping[str, float] | None = None, method: str = "equal",
                    scale: float = 1.0, lookback: int = 90, refit_every: int = 21) -> PortfolioWeights:
    """Allocate and net sleeves into instrument weights (see the module docstring)."""
    budgets = dict(budgets or {name: 1.0 for name in sleeves})
    scales = allocation_scales(sleeve_returns(sleeves, returns), budgets, method, lookback=lookback, refit_every=refit_every)
    instruments = sorted(set().union(*(frame.columns for frame in sleeves.values())))
    allocated = {name: sleeves[name].reindex(index=returns.index, columns=instruments).fillna(0.0).mul(scales[name] * scale, axis=0) for name in budgets}
    weights = sum(allocated.values())
    return PortfolioWeights(weights, scales, allocated)


def to_contracts(weights: pd.DataFrame, *, equity: float, prices: pd.DataFrame, multipliers: Mapping[str, float], fx: pd.DataFrame | float = 1.0) -> dict[str, pd.DataFrame]:
    """Whole contracts for weights (rounded toward zero), the weights they really hold, and what rounding costs.

    Returns "contracts", "held_weights" and "rounded_to_zero" (True where a non-zero weight became no contract).
    """
    multiplier = pd.Series(multipliers).reindex(weights.columns)
    fx_frame = fx if isinstance(fx, pd.DataFrame) else pd.DataFrame(float(fx), index=weights.index, columns=weights.columns)
    notional = prices.reindex_like(weights) * multiplier * fx_frame.reindex_like(weights)
    exact = weights * equity / notional
    contracts = np.trunc(exact)
    held = contracts * notional / equity
    return {"contracts": contracts, "held_weights": held, "rounded_to_zero": (weights != 0) & (contracts == 0)}
