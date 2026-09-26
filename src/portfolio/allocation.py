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
            usable value is treated as having the average of the others, and
            if none has one the scales follow the budgets alone.
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


class Allocator:
    """`allocate_history` one bar at a time, for the runtime: the same scales on the same schedule.

    The runtime sees one grid bar at a time, not a whole history. Call `step`
    once per grid bar with each sleeve's instrument return over that bar. The
    scales are refreshed every `refit_every` bars (counting from the first
    bar) from the returns *before* the current bar, exactly as
    `allocate_history` does with `rolling(lookback).std().shift(1)`. The
    state round-trips through `to_dict` / `from_dict` for checkpoints.
    """

    def __init__(self, budgets: Mapping[str, float], method: str, *, lookback: int = 90, refit_every: int = 30) -> None:
        """Validate the method now, so a bad config fails at startup rather than at the first refit."""
        if method not in ALLOCATION_METHODS:
            raise ValueError(f"unknown allocation {method!r}; choose from {ALLOCATION_METHODS}")
        self.budgets = dict(budgets)
        self.method = method
        self.lookback = lookback
        self.refit_every = max(1, refit_every)
        self.min_periods = max(10, lookback // 3)
        self.bars_seen = 0
        self.scales: dict[str, float] = {}
        self.history: dict[str, list[float]] = {sleeve: [] for sleeve in self.budgets}

    def _volatility(self) -> dict[str, float]:
        out = {}
        for sleeve, values in self.history.items():
            window = np.asarray(values[-self.lookback :], dtype=float)
            finite = window[np.isfinite(window)]
            out[sleeve] = float(np.std(finite, ddof=1)) if len(finite) >= self.min_periods else float("nan")
        return out

    def step(self, instrument_returns: Mapping[str, float] | None = None) -> dict[str, float]:
        """The scales for this bar; `instrument_returns` maps sleeve id to its instrument's return over the bar."""
        if self.bars_seen % self.refit_every == 0:
            volatility = self._volatility() if self.method == "inverse_vol" else None
            self.scales = sleeve_scales(self.budgets, self.method, volatility=volatility)
        for sleeve in self.history:
            value = (instrument_returns or {}).get(sleeve, float("nan"))
            self.history[sleeve].append(float(value) if value is not None else float("nan"))
            del self.history[sleeve][: -self.lookback]
        self.bars_seen += 1
        return dict(self.scales)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready state (NaN returns become None)."""
        return {
            "budgets": self.budgets, "method": self.method, "lookback": self.lookback, "refit_every": self.refit_every,
            "bars_seen": self.bars_seen, "scales": self.scales,
            "history": {sleeve: [value if np.isfinite(value) else None for value in values] for sleeve, values in self.history.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Allocator":
        """Rebuild from `to_dict` output."""
        allocator = cls(payload["budgets"], payload["method"], lookback=payload["lookback"], refit_every=payload["refit_every"])  # type: ignore[arg-type]
        allocator.bars_seen = int(payload["bars_seen"])  # type: ignore[arg-type]
        allocator.scales = dict(payload["scales"])  # type: ignore[arg-type]
        allocator.history = {sleeve: [float("nan") if value is None else float(value) for value in values] for sleeve, values in dict(payload["history"]).items()}  # type: ignore[union-attr]
        return allocator
