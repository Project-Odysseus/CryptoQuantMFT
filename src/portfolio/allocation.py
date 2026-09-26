"""Allocation: how much of the portfolio each sleeve gets.

A sleeve's target weight assumes it has the whole portfolio. Allocation turns
that into its share with a scale per sleeve:

- `fixed`: scale = the sleeve's `budget` (an absolute share; budgets should sum
  to at most 1, the rest stays in cash).
- `equal`: scale = 1 / number of enabled sleeves (budgets ignored).
- `inverse_vol`: scale in proportion to budget / volatility of the sleeve's
  instrument, normalised to sum to 1. Each sleeve then carries similar risk,
  with budgets as relative risk weights. Use it with sizers that don't already
  scale by volatility (`fixed_fraction`). With `vol_target` sizing each sleeve
  is already risk-sized, so `equal` or `fixed` is the natural choice.

Volatilities come from past data only and are refreshed every
`refit_every` bars, so budgets don't churn every bar.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

ALLOCATION_METHODS = ("fixed", "equal", "inverse_vol")


def sleeve_scales(budgets: Mapping[str, float], method: str, *, volatility: Mapping[str, float] | None = None) -> dict[str, float]:
    """The scale per sleeve at one point in time.

    Args:
        budgets: Sleeve id to budget, for enabled sleeves only.
        method: One of `ALLOCATION_METHODS`.
        volatility: Sleeve id to its instrument's recent volatility (any
            consistent unit). Needed for `inverse_vol`; a sleeve without a
            usable value gets the average of the others, or equal weight if
            none has one.
    """
    if method not in ALLOCATION_METHODS:
        raise ValueError(f"unknown allocation {method!r}; choose from {ALLOCATION_METHODS}")
    if not budgets:
        return {}
    if method == "fixed":
        return {sleeve: float(budget) for sleeve, budget in budgets.items()}
    if method == "equal":
        return {sleeve: 1.0 / len(budgets) for sleeve in budgets}
    usable = {sleeve: float(value) for sleeve, value in (volatility or {}).items() if sleeve in budgets and value is not None and np.isfinite(value) and value > 0}
    if not usable:
        total = sum(budgets.values())
        return {sleeve: budget / total for sleeve, budget in budgets.items()}
    fallback = float(np.mean(list(usable.values())))
    raw = {sleeve: budgets[sleeve] / usable.get(sleeve, fallback) for sleeve in budgets}
    total = sum(raw.values())
    return {sleeve: value / total for sleeve, value in raw.items()}


def allocate_history(
    sleeve_weights: pd.DataFrame,
    budgets: Mapping[str, float],
    method: str,
    *,
    instrument_returns: pd.DataFrame | None = None,
    lookback: int = 90,
    refit_every: int = 30,
) -> pd.DataFrame:
    """Scale each sleeve's weight history (columns = sleeve ids) by its allocation over time.

    `instrument_returns` has one column per sleeve with the returns of that
    sleeve's instrument on the same index; `inverse_vol` needs it. Scales are
    set from returns before each refit bar and held until the next refit.
    """
    sleeves = [sleeve for sleeve in sleeve_weights.columns if sleeve in budgets]
    scales = pd.DataFrame(index=sleeve_weights.index, columns=sleeves, dtype=float)
    volatility = None
    if method == "inverse_vol":
        if instrument_returns is None:
            raise ValueError("inverse_vol allocation needs instrument_returns")
        volatility = instrument_returns[sleeves].rolling(lookback, min_periods=max(10, lookback // 3)).std().shift(1)
    for start in range(0, len(scales), refit_every):
        row_vol = volatility.iloc[start].to_dict() if volatility is not None else None
        values = sleeve_scales({sleeve: budgets[sleeve] for sleeve in sleeves}, method, volatility=row_vol)
        scales.iloc[start : start + refit_every] = [values[sleeve] for sleeve in sleeves]
    return sleeve_weights[sleeves] * scales
