"""Is a Sharpe ratio real, given how many things were tried? Probabilistic and deflated Sharpe ratios.

Every configuration tested is another chance to find a high Sharpe by luck.
After thousands of backtests, the best in-sample Sharpe is inflated. These
are the standard corrections (Bailey and López de Prado, 2012 and 2014):

- `probabilistic_sharpe_ratio`: the probability that the true Sharpe exceeds a
  benchmark, given the sample length and the returns' skew and fat tails
  (both widen the error of an estimated Sharpe).
- `expected_max_sharpe`: the best Sharpe you'd expect from `trials`
  strategies with no skill at all, given how much the trials' Sharpes vary.
- `deflated_sharpe_ratio`: the probabilistic Sharpe against that luck
  benchmark. Above 0.95 means the result very probably isn't a lucky draw
  from the search that found it.

Sharpe ratios here are per period (not annualised). Pass the same frequency
the returns are in.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.stats import kurtosis, norm, skew

EULER_GAMMA = 0.5772156649015329


def sharpe_per_period(returns: Sequence[float]) -> float:
    """Mean over standard deviation of the returns (not annualised)."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    std = values.std(ddof=1) if len(values) > 1 else 0.0
    return float(values.mean() / std) if std > 0 else 0.0


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark: float = 0.0) -> float:
    """Probability that the true per-period Sharpe exceeds `benchmark`, allowing for skew and fat tails."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    count = len(values)
    if count < 3:
        return float("nan")
    estimate = sharpe_per_period(values)
    gamma3, gamma4 = float(skew(values)), float(kurtosis(values, fisher=False))
    denominator = np.sqrt(max(1e-12, 1.0 - gamma3 * estimate + (gamma4 - 1.0) / 4.0 * estimate**2))
    return float(norm.cdf((estimate - benchmark) * np.sqrt(count - 1) / denominator))


def expected_max_sharpe(trials: int, sharpe_variance: float) -> float:
    """The expected best per-period Sharpe among `trials` skill-less strategies whose Sharpes vary with `sharpe_variance`."""
    if trials < 2 or sharpe_variance <= 0:
        return 0.0
    spread = np.sqrt(sharpe_variance)
    return float(spread * ((1 - EULER_GAMMA) * norm.ppf(1 - 1.0 / trials) + EULER_GAMMA * norm.ppf(1 - 1.0 / (trials * np.e))))


def deflated_sharpe_ratio(returns: Sequence[float], *, trials: int, sharpe_variance: float) -> float:
    """Probability that the chosen strategy's Sharpe beats what the best of `trials` lucky draws would show.

    Args:
        returns: The chosen strategy's per-period returns.
        trials: How many configurations were tried to find it (be honest).
        sharpe_variance: Variance of the per-period Sharpe ratios across those
            trials (from the sweep's results).
    """
    return probabilistic_sharpe_ratio(returns, benchmark=expected_max_sharpe(trials, sharpe_variance))
