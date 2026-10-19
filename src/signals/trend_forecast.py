"""A continuous trend forecast: a volatility-normalised EMA difference, passed through an activation function.

From R. J. Martin, "Design and analysis of momentum trading strategies" (arXiv:2101.01006). The binary rule most trend
books run (+1 when the fast average is above the slow one, -1 below) has almost no skew, because it is always full size:
it loses as fast in a market without a trend as it gains in one with a trend. A position that grows with the strength
of the trend and stays small while the trend is weak is positively skewed over months, by construction.

The pieces, all causal (element i uses data up to i only):
- `ewma_volatility`: daily volatility from an exponential average of squared returns.
- `ema_difference`: the fast minus the slow exponential average of the volatility-adjusted price, scaled to unit
  variance when returns are independent (Martin, appendix A), so one activation means the same at every speed.
- `ACTIVATIONS`: what turns that z-score into a position, each scaled so E[psi(Z)^2] = 1 for a standard normal Z.
- `trend_forecast`: the average activation over several speed pairs.

Pre-registered and tested on equity indices in H13 (research/prereg/H13.txt).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np
from scipy.special import ndtr

DEFAULT_SPEEDS: tuple[tuple[int, int], ...] = ((10, 20), (20, 40), (40, 80))
VOL_SPAN = 20
VOL_FLOOR_ANNUAL = 0.05
DAYS_PER_YEAR = 252.0


def ewma_volatility(returns: np.ndarray, *, span: int = VOL_SPAN, floor_annual: float = VOL_FLOOR_ANNUAL) -> np.ndarray:
    """Daily volatility at each bar: the square root of an exponential average (decay 1 - 1/span) of squared returns.

    NaN returns (the first bar, gaps) leave the average unchanged. Floored at `floor_annual` a year so a quiet stretch
    can't blow up the position.
    """
    decay = 1.0 - 1.0 / span
    floor = floor_annual / math.sqrt(DAYS_PER_YEAR)
    out = np.full(len(returns), np.nan)
    variance = np.nan
    for index, value in enumerate(returns):
        if np.isfinite(value):
            variance = value * value if not np.isfinite(variance) else decay * variance + (1.0 - decay) * value * value
        if np.isfinite(variance):
            out[index] = max(math.sqrt(variance), floor)
    return out


def _ema(values: np.ndarray, decay: float) -> np.ndarray:
    out = np.empty(len(values))
    level = values[0]
    for index, value in enumerate(values):
        level = decay * level + (1.0 - decay) * value
        out[index] = level
    return out


def ema_difference_norm(fast: int, slow: int) -> float:
    """The standard deviation of the fast-minus-slow EMA difference of a cumulative unit-variance random walk."""
    a, b = 1.0 - 1.0 / fast, 1.0 - 1.0 / slow
    return math.sqrt((a - b) ** 2 * (1.0 + a * b) / ((1.0 - a * a) * (1.0 - b * b) * (1.0 - a * b)))


def ema_difference(adjusted: np.ndarray, fast: int, slow: int) -> np.ndarray:
    """z at each bar: (fast EMA - slow EMA) of the cumulative volatility-adjusted returns, in units of its own standard deviation."""
    if not 1 < fast < slow:
        raise ValueError("speeds must satisfy 1 < fast < slow")
    level = np.cumsum(np.nan_to_num(adjusted))
    return (_ema(level, 1.0 - 1.0 / fast) - _ema(level, 1.0 - 1.0 / slow)) / ema_difference_norm(fast, slow)


def _normalised(function: Callable[[np.ndarray], np.ndarray], second_moment: float | None = None) -> Callable[[np.ndarray], np.ndarray]:
    """`function` divided by the square root of E[function(Z)^2], Z standard normal.

    Smooth shapes use Gauss-Hermite quadrature; step functions pass their exact `second_moment`, because quadrature
    nodes sitting on a jump get it wrong.
    """
    if second_moment is None:
        nodes, weights = np.polynomial.hermite_e.hermegauss(201)
        second_moment = float(np.sum(weights * function(nodes) ** 2)) / math.sqrt(2.0 * math.pi)
    scale = math.sqrt(second_moment)
    return lambda z: function(np.asarray(z, dtype=float)) / scale


def compound_sigmoid(w_sigmoid: float = 0.30, w_reverting: float = 0.71, steepness: float = 0.75) -> Callable[[np.ndarray], np.ndarray]:
    """Martin's highlighted activation (figure 8): mostly a sigmoid, with some position taken off when the trend is extreme.

    It is close to the best Sharpe ratio in his table and keeps positive skew; the pure reverting form loses badly in
    long trends (his oil 2014 example), so the reverting weight is kept moderate.
    """
    lam = steepness
    c_sigmoid = (2.0 / math.pi * math.atan(lam * lam / math.sqrt(1.0 + 2.0 * lam * lam))) ** -0.5
    c_reverting = (1.0 + 2.0 * lam * lam) ** 0.75
    return _normalised(lambda z: w_sigmoid * c_sigmoid * (2.0 * ndtr(lam * z) - 1.0) + w_reverting * c_reverting * z * np.exp(-0.5 * lam * lam * z * z))


def double_step(dead_zone: float = 0.6) -> Callable[[np.ndarray], np.ndarray]:
    """Flat inside +/-`dead_zone`, full size outside: the binary bet with Martin's suggested wait for a real trend."""
    return _normalised(lambda z: (z > dead_zone).astype(float) - (z < -dead_zone).astype(float), 2.0 * float(ndtr(-dead_zone)))


ACTIVATIONS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "compound_sigmoid": compound_sigmoid(),
    "sign": _normalised(np.sign, 1.0),
    "linear": _normalised(lambda z: np.clip(z, -2.0, 2.0)),
    "double_step": double_step(),
}


def trend_forecast(closes: Sequence[float] | np.ndarray, *, activation: str = "compound_sigmoid", speeds: Sequence[tuple[int, int]] = DEFAULT_SPEEDS,
                   returns: np.ndarray | None = None) -> np.ndarray:
    """The average activation over `speeds` at every bar.

    Args:
        closes: Prices, oldest first. Used only for returns when `returns` is not given.
        activation: A key of `ACTIVATIONS`.
        speeds: (fast, slow) EMA lengths in bars.
        returns: Per-bar returns to use instead of the closes' changes (research adds carry this way); element i is the
            return into bar i, NaN for the first.
    """
    if activation not in ACTIVATIONS:
        raise ValueError(f"unknown activation {activation!r}; use one of {sorted(ACTIVATIONS)}")
    if returns is None:
        prices = np.asarray(closes, dtype=float)
        returns = np.concatenate([[np.nan], prices[1:] / prices[:-1] - 1.0]) if len(prices) else np.array([])
    returns = np.asarray(returns, dtype=float)
    if not len(returns):
        return np.array([])
    sigma = ewma_volatility(returns)
    adjusted = returns / np.concatenate([[np.nan], sigma[:-1]])  # each return over the volatility known before it
    psi = ACTIVATIONS[activation]
    return np.mean([psi(ema_difference(adjusted, fast, slow)) for fast, slow in speeds], axis=0)
