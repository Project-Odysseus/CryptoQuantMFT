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
- `risk_parity`: equal risk contribution (ERC). Scales such that each sleeve
  contributes its budget's share of the book's variance, using the covariance
  of the sleeves' own returns (their position times their instrument's return),
  so correlated sleeves share one risk budget. Budgets are relative risk
  budgets; scales sum to 1.
- `hrp`: hierarchical risk parity (Lopez de Prado, 2016). Clusters sleeves by
  the correlation of their returns, then splits capital top-down between
  clusters by inverse cluster variance. More stable than ERC when sleeves are
  highly correlated, since it never inverts the covariance matrix. Budgets tilt
  the result multiplicatively; scales sum to 1.

Why sleeve returns and not instrument returns for the last two: several sleeves
can trade the same instrument (the live book runs three on BTC), which makes the
instrument covariance singular, and what the book is exposed to is the sleeves'
positions, not the instrument itself. Trend sleeves are often flat for weeks, and
a flat stretch would make a sleeve look riskless and hand it most of the book.
So each sleeve's volatility is measured only over the bars it held a position
(its risk when it is in the market); correlations come from the whole window. A
sleeve with fewer than `MIN_ACTIVE_BARS` positioned bars gets the median
volatility of the others, as `inverse_vol` does for a sleeve without data.
Before enough history exists, scales follow the budgets alone.

A basket (`basket.py`) is one allocation unit: its member sleeves share one
scale. `fixed` and `equal` give it its budget or an equal share like any sleeve.
`risk_parity` and `hrp` use the basket's own return, the sum of its members'
(each member's weight times its instrument's return), so a market-neutral basket
is measured by what it actually earns and loses, not by its coins' volatility.
`inverse_vol` can't be used with baskets: it sizes by one instrument's volatility.

Volatilities and covariances come from past data only and are refreshed every
`refit_every` bars, so budgets don't churn every bar. The refreshes follow the
calendar, not a count from wherever a run happened to start: a bar is a refit
bar when its number since 1970-01-01 (its open time divided by the bar length)
is divisible by `refit_every`. A research backtest and a runtime that started
on different days therefore refit on the same bars, from the same lookback of
returns, and give the same scales once each has a lookback of history behind
it. The first bar of any run sets scales too, from whatever history exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import numpy as np
import pandas as pd

ALLOCATION_METHODS = ("fixed", "equal", "inverse_vol", "risk_parity", "hrp")
COVARIANCE_METHODS = ("risk_parity", "hrp")
MIN_ACTIVE_BARS = 10


def bar_number(stamp: datetime | pd.Timestamp, bar_seconds: float) -> int:
    """Whole bars of `bar_seconds` between 1970-01-01 UTC and a bar's open `stamp`: the calendar the refits follow."""
    return int(pd.Timestamp(stamp).timestamp() // bar_seconds)


def refit_rows(index: pd.DatetimeIndex, refit_every: int, *, bar_seconds: float | None = None) -> list[int]:
    """The rows of `index` at which scales are refreshed: the first row and every calendar refit bar after it.

    `bar_seconds` defaults to the index's usual spacing.
    """
    if not len(index):
        return []
    if bar_seconds is None:
        spacing = pd.Series(index).diff().median()
        bar_seconds = spacing.total_seconds() if pd.notna(spacing) and spacing > pd.Timedelta(0) else 86400.0
    return [0] + [row for row in range(1, len(index)) if bar_number(index[row], bar_seconds) % max(1, refit_every) == 0]


def sleeve_covariance(block: np.ndarray) -> np.ndarray | None:
    """Covariance of sleeve returns built from each sleeve's volatility while positioned and the full-window correlation.

    `block` has complete rows only (bars x sleeves). A zero return means the sleeve was flat that bar. Returns None when
    no sleeve has enough positioned bars to measure.
    """
    count = block.shape[1]
    std = np.full(count, np.nan)
    for j in range(count):
        active = block[block[:, j] != 0.0, j]
        if len(active) >= MIN_ACTIVE_BARS:
            std[j] = float(np.sqrt(np.mean(active**2)))  # zero-mean, like the EWMA sizing forecasts
    known = std[np.isfinite(std) & (std > 0)]
    if not len(known):
        return None
    std = np.where(np.isfinite(std) & (std > 0), std, float(np.median(known)))
    with np.errstate(invalid="ignore", divide="ignore"):
        correlation = np.corrcoef(block, rowvar=False) if count > 1 else np.ones((1, 1))
    correlation = np.nan_to_num(np.atleast_2d(correlation), nan=0.0)  # a sleeve flat all window: uncorrelated
    np.fill_diagonal(correlation, 1.0)
    return correlation * np.outer(std, std)


def risk_parity_weights(covariance: np.ndarray, budgets: np.ndarray, *, tolerance: float = 1e-12, max_iterations: int = 10_000) -> np.ndarray:
    """Long-only weights (summing to 1) whose risk contributions w_i (Cov w)_i are proportional to `budgets`.

    Cyclical coordinate descent on the convex problem min 1/2 x'Cov x - sum b_i log x_i (Griveau-Billion, Richard and
    Roncalli, 2013); its solution, normalised, is the risk-budgeting portfolio. Each coordinate update is closed form.
    """
    budgets = np.asarray(budgets, dtype=float) / float(np.sum(budgets))
    variances = np.diag(covariance)
    x = budgets / np.sqrt(variances)
    for _ in range(max_iterations):
        previous = x.copy()
        for i in range(len(x)):
            others = float(covariance[i] @ x - variances[i] * x[i])
            x[i] = (-others + np.sqrt(others**2 + 4.0 * variances[i] * budgets[i])) / (2.0 * variances[i])
        if np.max(np.abs(x - previous)) <= tolerance * np.max(np.abs(x)):
            break
    return x / x.sum()


def hrp_weights(covariance: np.ndarray) -> np.ndarray:
    """Hierarchical risk parity weights (summing to 1) from a covariance matrix.

    Distances sqrt((1 - correlation) / 2) are clustered with single linkage. Capital is then split top-down at each
    branch of that tree between its two sub-clusters, in inverse proportion to each sub-cluster's variance (with
    inverse-variance weights inside it). Splitting at the tree's own branches, rather than at halves of the sorted leaf
    list as in the original paper, never cuts a tight cluster in two.
    """
    from scipy.cluster.hierarchy import linkage, to_tree
    from scipy.spatial.distance import squareform

    count = len(covariance)
    if count == 1:
        return np.ones(1)
    std = np.sqrt(np.diag(covariance))
    correlation = np.clip(covariance / np.outer(std, std), -1.0, 1.0)
    distance = np.sqrt(np.clip((1.0 - correlation) / 2.0, 0.0, None))
    np.fill_diagonal(distance, 0.0)
    root = to_tree(linkage(squareform(distance, checks=False), method="single"))
    weights = np.ones(count)

    def cluster_variance(items: list[int]) -> float:
        block = covariance[np.ix_(items, items)]
        inverse = 1.0 / np.diag(block)
        inverse /= inverse.sum()
        return float(inverse @ block @ inverse)

    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_leaf():
            continue
        left, right = node.get_left(), node.get_right()
        left_items, right_items = left.pre_order(), right.pre_order()
        left_var, right_var = cluster_variance(left_items), cluster_variance(right_items)
        alpha = 1.0 - left_var / (left_var + right_var)
        weights[left_items] *= alpha
        weights[right_items] *= 1.0 - alpha
        stack += [left, right]
    return weights / weights.sum()


def covariance_scales(budgets: Mapping[str, float], method: str, sleeve_returns: np.ndarray, *, min_periods: int) -> dict[str, float]:
    """`risk_parity` or `hrp` scales from a block of past sleeve returns (rows = bars, columns in `budgets` order).

    Rows with a missing value are dropped; with fewer than `min_periods` complete rows the scales follow the budgets.
    Shared by the research backtest and the runtime `Allocator`, so both compute exactly the same numbers.
    """
    names = list(budgets)
    budget = np.array([float(budgets[name]) for name in names])
    block = np.asarray(sleeve_returns, dtype=float).reshape(-1, len(names))
    block = block[np.all(np.isfinite(block), axis=1)]
    covariance = sleeve_covariance(block) if len(block) >= max(min_periods, 2) else None
    if covariance is None:
        return {name: value / budget.sum() for name, value in zip(names, budget)}
    raw = risk_parity_weights(covariance, budget) if method == "risk_parity" else hrp_weights(covariance) * budget
    raw = raw / raw.sum()
    return {name: float(value) for name, value in zip(names, raw)}


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
    if method in COVARIANCE_METHODS:
        raise ValueError(f"{method} needs the sleeves' return history: use covariance_scales, allocate_history or Allocator")
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
    groups: Mapping[str, str] | None = None,
    bar_seconds: float | None = None,
) -> pd.DataFrame:
    """Scale each sleeve's weight history (columns = sleeve ids) by its allocation over time.

    `groups` maps a sleeve to its allocation unit (a basket's members share
    one; default: every sleeve is its own). Only `risk_parity` and `hrp` take
    it: a unit's return is the sum of its members', its budget its first
    member's, and its scale goes to every member.

    `instrument_returns` has one column per sleeve with the returns of that
    sleeve's instrument on the same index; `inverse_vol`, `risk_parity` and
    `hrp` need it. Scales are set from returns before each refit bar and held
    until the next refit (`refit_rows`: the calendar schedule; `bar_seconds`
    defaults to the index's spacing). For the covariance methods, a sleeve's
    return at a bar is its weight from the previous bar times its instrument's
    return.
    """
    sleeves = [sleeve for sleeve in sleeve_weights.columns if sleeve in budgets]
    scales = pd.DataFrame(index=sleeve_weights.index, columns=sleeves, dtype=float)
    starts = refit_rows(sleeve_weights.index, refit_every, bar_seconds=bar_seconds)
    spans = list(zip(starts, starts[1:] + [len(scales)]))
    if method in COVARIANCE_METHODS:
        if instrument_returns is None:
            raise ValueError(f"{method} allocation needs instrument_returns")
        unit_of = {sleeve: (groups or {}).get(sleeve, sleeve) for sleeve in sleeves}
        earned = sleeve_weights[sleeves].shift(1) * instrument_returns[sleeves]
        ordered: dict[str, float] = {}
        for sleeve in sleeves:
            ordered.setdefault(unit_of[sleeve], budgets[sleeve])
        # a unit's return: the sum of its members' (NaN only when none of them has one, e.g. the first bar)
        unit_returns = np.column_stack([earned[[sleeve for sleeve in sleeves if unit_of[sleeve] == unit]].sum(axis=1, min_count=1).to_numpy(dtype=float)
                                        for unit in ordered])
        for start, end in spans:
            values = covariance_scales(ordered, method, unit_returns[max(0, start - lookback) : start], min_periods=max(10, lookback // 3))
            scales.iloc[start:end] = [values[unit_of[sleeve]] for sleeve in sleeves]
        return sleeve_weights[sleeves] * scales
    if groups and any(groups.get(sleeve, sleeve) != sleeve for sleeve in sleeves):
        raise ValueError(f"{method} allocation takes no groups: scale baskets with sleeve_scales on their unit budgets")
    volatility = None
    if method == "inverse_vol":
        if instrument_returns is None:
            raise ValueError("inverse_vol allocation needs instrument_returns")
        volatility = instrument_returns[sleeves].rolling(lookback, min_periods=max(10, lookback // 3)).std().shift(1)
    for start, end in spans:
        row_vol = volatility.iloc[start].to_dict() if volatility is not None else None
        values = sleeve_scales({sleeve: budgets[sleeve] for sleeve in sleeves}, method, volatility=row_vol)
        scales.iloc[start:end] = [values[sleeve] for sleeve in sleeves]
    return sleeve_weights[sleeves] * scales


class Allocator:
    """`allocate_history` one bar at a time, for the runtime: the same scales on the same schedule.

    The runtime sees one grid bar at a time, not a whole history. Call `step`
    once per grid bar with the bar's calendar number (`bar_number`) and each
    sleeve's instrument return over that bar (and, for `risk_parity` and
    `hrp`, each sleeve's current own weight: the return it earned over the bar
    is the weight passed on the previous step times the instrument return).
    The scales are refreshed on the first step and on every calendar refit bar
    from the returns *before* the current bar, exactly as `allocate_history`
    does. The state round-trips through
    `to_dict` / `from_dict` for checkpoints.
    """

    def __init__(self, budgets: Mapping[str, float], method: str, *, lookback: int = 90, refit_every: int = 30,
                 groups: Mapping[str, str] | None = None) -> None:
        """Validate the method now, so a bad config fails at startup rather than at the first refit.

        `budgets` is per allocation unit. `groups` maps each sleeve to its
        unit when some units are baskets; `step` then takes returns and
        weights per sleeve and, for `risk_parity` and `hrp`, sums a unit's
        members into its return.
        """
        if method not in ALLOCATION_METHODS:
            raise ValueError(f"unknown allocation {method!r}; choose from {ALLOCATION_METHODS}")
        self.budgets = dict(budgets)
        self.groups = dict(groups) if groups is not None else {unit: unit for unit in self.budgets}
        self.method = method
        self.lookback = lookback
        self.refit_every = max(1, refit_every)
        self.min_periods = max(10, lookback // 3)
        self.fitted = False
        self.scales: dict[str, float] = {}
        self.history: dict[str, list[float]] = {sleeve: [] for sleeve in self.budgets}  # instrument returns, or sleeve returns for the covariance methods
        self.previous_weights: dict[str, float] = {}

    def _volatility(self) -> dict[str, float]:
        out = {}
        for sleeve, values in self.history.items():
            window = np.asarray(values[-self.lookback :], dtype=float)
            finite = window[np.isfinite(window)]
            out[sleeve] = float(np.std(finite, ddof=1)) if len(finite) >= self.min_periods else float("nan")
        return out

    def step(self, instrument_returns: Mapping[str, float] | None = None, weights: Mapping[str, float] | None = None, *, bar: int) -> dict[str, float]:
        """The scales for this bar.

        Args:
            bar: The bar's calendar number (`bar_number(stamp, bar_seconds)`); it decides whether this is a refit bar.
            instrument_returns: Sleeve id to its instrument's return over the bar that just closed.
            weights: Sleeve id to the sleeve's own (unallocated) weight now, held over the next bar. Needed for
                `risk_parity` and `hrp`; ignored otherwise.
        """
        covariance = self.method in COVARIANCE_METHODS
        if not self.fitted or bar % self.refit_every == 0:
            self.fitted = True
            if covariance:
                block = np.column_stack([self.history[sleeve] for sleeve in self.budgets]) if any(self.history.values()) else np.empty((0, len(self.budgets)))
                self.scales = covariance_scales(self.budgets, self.method, block, min_periods=self.min_periods)
            else:
                volatility = self._volatility() if self.method == "inverse_vol" else None
                self.scales = sleeve_scales(self.budgets, self.method, volatility=volatility)
        earned: dict[str, list[float]] = {}
        if covariance:  # each member's weight held into the bar times its instrument's return, collected per unit
            for sleeve, unit in self.groups.items():
                value = (instrument_returns or {}).get(sleeve)
                earned.setdefault(unit, []).append((float(value) if value is not None else float("nan")) * self.previous_weights.get(sleeve, float("nan")))
        for unit in self.history:
            if covariance:
                finite = [value for value in earned.get(unit, []) if np.isfinite(value)]
                value = float(sum(finite)) if finite else float("nan")
            else:
                value = (instrument_returns or {}).get(unit, float("nan"))
                value = float(value) if value is not None else float("nan")
            self.history[unit].append(value)
            del self.history[unit][: -self.lookback]
        if covariance:
            self.previous_weights = {sleeve: float((weights or {}).get(sleeve, 0.0)) for sleeve in self.groups}
        return dict(self.scales)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready state (NaN returns become None)."""
        return {
            "budgets": self.budgets, "method": self.method, "lookback": self.lookback, "refit_every": self.refit_every,
            "fitted": self.fitted, "scales": self.scales, "previous_weights": self.previous_weights, "groups": self.groups,
            "history": {sleeve: [value if np.isfinite(value) else None for value in values] for sleeve, values in self.history.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Allocator":
        """Rebuild from `to_dict` output."""
        allocator = cls(payload["budgets"], payload["method"], lookback=payload["lookback"], refit_every=payload["refit_every"],  # type: ignore[arg-type]
                        groups=payload.get("groups"))  # type: ignore[arg-type]
        # older checkpoints counted bars instead: with scales present, carry on and refit at the next calendar bar
        allocator.fitted = bool(payload.get("fitted", bool(payload.get("scales"))))
        allocator.scales = dict(payload["scales"])  # type: ignore[arg-type]
        allocator.history = {sleeve: [float("nan") if value is None else float(value) for value in values] for sleeve, values in dict(payload["history"]).items()}  # type: ignore[union-attr]
        allocator.previous_weights = {sleeve: float(value) for sleeve, value in dict(payload.get("previous_weights") or {}).items()}  # type: ignore[union-attr]
        return allocator
