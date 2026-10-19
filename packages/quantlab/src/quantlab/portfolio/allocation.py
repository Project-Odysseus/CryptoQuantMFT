"""Allocation math: how much of a portfolio each sleeve gets (fixed, equal, inverse-volatility, risk parity, HRP).

Ported from the host's src/portfolio/allocation.py (the pure functions) (docs/quantlab_migration.md); behaviour unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

ALLOCATION_METHODS = ("fixed", "equal", "inverse_vol", "risk_parity", "hrp")
COVARIANCE_METHODS = ("risk_parity", "hrp")
MIN_ACTIVE_BARS = 10


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
