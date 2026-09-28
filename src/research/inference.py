"""Statistics for the hypothesis studies: HAC regressions, block bootstrap, matched-baseline event studies, placebos,
and a per-hypothesis trial ledger feeding the deflated Sharpe ratio.

- `newey_west_ols`: OLS with Newey-West (Bartlett) standard errors, needed because forward returns over h days
  sampled daily overlap and are autocorrelated up to lag h - 1.
- `driscoll_kraay_ols`: the pooled panel version (several coins, one regression). It sums the score across coins at
  each date before applying Newey-West, so it is robust to BTC, ETH and SOL moving together on the same day as well
  as to the overlap.
- `block_bootstrap_ci`: a circular block bootstrap of a mean, for autocorrelated series.
- `matched_baseline`: for each event, the average outcome over random non-event times with similar trailing
  volatility, so an event study compares like with like (big moves happen in volatile markets).
- `placebo_share`: the share of placebo statistics the real one beats.
- `log_trial` / `deflated_sharpe`: every configuration run goes into the repo's append-only trial ledger
  (`governance.record_trials`) with its family (H1, H2, H3) and Sharpe, and the deflated Sharpe uses both that
  family's count and the global count.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from src.research import governance
from src.research.stats import deflated_sharpe_ratio, sharpe_per_period


def _add_constant(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    x = x.reshape(-1, 1) if x.ndim == 1 else x
    return np.column_stack([np.ones(len(x)), x])


def _hac(scores: np.ndarray, lags: int) -> np.ndarray:
    """Newey-West long-run covariance of the rows of `scores` (T x k) with a Bartlett kernel."""
    count = len(scores)
    omega = scores.T @ scores
    for lag in range(1, min(lags, count - 1) + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = scores[lag:].T @ scores[:-lag]
        omega += weight * (gamma + gamma.T)
    return omega


def newey_west_ols(y: Sequence[float], x: Sequence[float] | np.ndarray, *, lags: int) -> pd.DataFrame:
    """OLS of y on a constant and x, with Newey-West standard errors (rows: const, x1, x2, ...)."""
    y = np.asarray(y, dtype=float)
    design = _add_constant(np.asarray(x, dtype=float))
    keep = np.isfinite(y) & np.all(np.isfinite(design), axis=1)
    y, design = y[keep], design[keep]
    bread = np.linalg.inv(design.T @ design)
    beta = bread @ design.T @ y
    resid = y - design @ beta
    cov = bread @ _hac(design * resid[:, None], lags) @ bread
    se = np.sqrt(np.diag(cov))
    names = ["const"] + [f"x{i}" for i in range(1, design.shape[1])]
    return pd.DataFrame({"coef": beta, "se": se, "t": beta / se, "n": len(y)}, index=names)


def driscoll_kraay_ols(frame: pd.DataFrame, y: str, xs: Sequence[str], *, time: str, lags: int) -> pd.DataFrame:
    """Pooled OLS over a panel with Driscoll-Kraay standard errors (cross-sectionally and serially robust)."""
    data = frame.dropna(subset=[y, *xs]).sort_values(time)
    design = _add_constant(data[list(xs)].to_numpy(dtype=float))
    target = data[y].to_numpy(dtype=float)
    bread = np.linalg.inv(design.T @ design)
    beta = bread @ design.T @ target
    resid = target - design @ beta
    scores = pd.DataFrame(design * resid[:, None]).groupby(data[time].to_numpy()).sum().to_numpy()
    cov = bread @ _hac(scores, lags) @ bread
    se = np.sqrt(np.diag(cov))
    return pd.DataFrame({"coef": beta, "se": se, "t": beta / se, "n": len(target), "dates": len(scores)}, index=["const", *xs])


def block_bootstrap_ci(values: Sequence[float], *, block: int, runs: int = 2000, alpha: float = 0.05, seed: int = 0,
                       statistic: Callable[[np.ndarray], float] = np.mean) -> tuple[float, float, float]:
    """(estimate, lower, upper) of `statistic` with a circular block bootstrap confidence interval."""
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    count = len(data)
    if count < 3:
        return float("nan"), float("nan"), float("nan")
    block = max(1, min(block, count))
    rng = np.random.default_rng(seed)
    blocks = int(np.ceil(count / block))
    starts = rng.integers(0, count, size=(runs, blocks))
    index = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(runs, -1)[:, :count] % count
    draws = np.array([statistic(data[row]) for row in index])
    return float(statistic(data)), float(np.quantile(draws, alpha / 2)), float(np.quantile(draws, 1 - alpha / 2))


def matched_baseline(event_positions: Sequence[int], outcome: np.ndarray, matching: np.ndarray, *, exclude: np.ndarray,
                     samples: int = 20, tolerance: float = 0.2, seed: int = 0) -> tuple[np.ndarray, list[np.ndarray]]:
    """For each event, the mean `outcome` over up to `samples` random eligible times with `matching` within ±tolerance.

    Args:
        event_positions: Positional indices of the events.
        outcome: The outcome (e.g. a 24h forward return) at every position; NaN where undefined.
        matching: The matching variable (e.g. trailing realized vol) at every position, known at that time.
        exclude: Boolean mask of positions that may not serve as controls (near any event).
        samples: Controls per event.
        tolerance: Relative band on `matching` (0.2 = within 20%).

    Returns:
        (mean control outcome per event, the control positions drawn for each event) — NaN if no control matched.
    """
    rng = np.random.default_rng(seed)
    eligible = np.flatnonzero(~exclude & np.isfinite(outcome) & np.isfinite(matching) & (matching > 0))
    means, drawn = np.full(len(event_positions), np.nan), []
    for i, position in enumerate(event_positions):
        level = matching[position]
        if not np.isfinite(level) or level <= 0:
            drawn.append(np.array([], dtype=int))
            continue
        pool = eligible[np.abs(matching[eligible] / level - 1.0) <= tolerance]
        pick = rng.choice(pool, size=min(samples, len(pool)), replace=False) if len(pool) else np.array([], dtype=int)
        drawn.append(pick)
        if len(pick):
            means[i] = float(np.mean(outcome[pick]))
    return means, drawn


def placebo_share(real: float, placebos: Sequence[float]) -> float:
    """Share of placebo statistics strictly below the real one (0.95+ = beats placebo at 95%)."""
    values = np.asarray(placebos, dtype=float)
    values = values[np.isfinite(values)]
    return float(np.mean(values < real)) if len(values) else float("nan")


def circular_shifts(count: int, runs: int, *, min_shift: int, seed: int = 0) -> np.ndarray:
    """Random circular shifts of at least `min_shift` steps in either direction, for time-shift placebos."""
    rng = np.random.default_rng(seed)
    return rng.integers(min_shift, max(min_shift + 1, count - min_shift), size=runs)


# --- the trial ledger, per hypothesis -------------------------------------------------------------------------------

def log_trial(hypothesis: str, study: str, params: Mapping[str, Any], *, data_range: str, gross_sharpe: float,
              net_sharpe: float, net_sharpe_2x: float, trades: int, sharpe_per_period_net: float, extra: Mapping[str, Any] | None = None) -> None:
    """Append one backtested configuration to the trial ledger under `hypothesis` (the family)."""
    governance.record_trials(study, 1, family=hypothesis, data=data_range, details={
        "params": dict(params), "gross_sharpe": gross_sharpe, "net_sharpe": net_sharpe, "net_sharpe_2x": net_sharpe_2x,
        "trades": trades, "sr_per_period": sharpe_per_period_net, **dict(extra or {})})


def family_trials(hypothesis: str, *, rule: str | None = None) -> tuple[int, float]:
    """(configurations tried under `hypothesis`, variance of their per-period net Sharpes) from the ledger.

    With `rule`, the variance comes only from trials of that rule (params["rule"]), while the count stays the whole
    family's. Mixing rules with very different Sharpe levels (e.g. a carry trade and a directional tilt) inflates the
    variance and so the luck benchmark; the per-rule variance is the like-for-like one. Both are reported.
    """
    entries = [e for e in governance.ledger_entries() if e.get("type") == "trials" and e.get("family") == hypothesis]
    matching = [e for e in entries if rule is None or e.get("details", {}).get("params", {}).get("rule") == rule]
    srs = [e.get("details", {}).get("sr_per_period") for e in matching]
    srs = np.array([s for s in srs if s is not None and np.isfinite(s)], dtype=float)
    return sum(int(e["configurations"]) for e in entries), float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0


def deflated_sharpe(returns: pd.Series, hypothesis: str, *, rule: str | None = None) -> dict[str, float]:
    """Deflated Sharpe of `returns` against the hypothesis's own trial count and against the global count.

    The Sharpe variance across trials comes from the hypothesis's ledger entries, floored at 1/T: the sampling
    variance of a per-period Sharpe estimated from T returns when there is no skill. A handful of pre-registered,
    highly correlated configurations can have near-identical Sharpes, which would otherwise deflate nothing.
    """
    values = returns.dropna().to_numpy()
    floor = 1.0 / max(len(values), 1)
    trials, variance = family_trials(hypothesis)
    variance = max(variance, floor)
    result = {
        "sr_per_period": sharpe_per_period(values),
        "family_trials": trials,
        "global_trials": governance.total_trials(),
        "dsr_family": deflated_sharpe_ratio(values, trials=max(trials, 2), sharpe_variance=variance),
        "dsr_global": deflated_sharpe_ratio(values, trials=max(governance.total_trials(), 2), sharpe_variance=variance),
    }
    if rule is not None:
        rule_variance = max(family_trials(hypothesis, rule=rule)[1], floor)
        result["dsr_family_rule_var"] = deflated_sharpe_ratio(values, trials=max(trials, 2), sharpe_variance=rule_variance)
        result["dsr_global_rule_var"] = deflated_sharpe_ratio(values, trials=max(governance.total_trials(), 2), sharpe_variance=rule_variance)
    return result
