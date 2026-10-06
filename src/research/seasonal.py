"""Clock patterns in volatility: epoch folding, a test that survives volatility clustering, and a seasonal forecast.

"Epoch folding" cuts a series into blocks of a trial period, lines the blocks up and averages them: whatever repeats
at the same position survives, noise averages away. On absolute or squared returns it shows *when* the market moves
most (the hour of the day, the day of the week). It says nothing about direction.

Three things here:

- `fold` / `fold_statistic`: the average profile over a period, and how far it is from flat (1 = what independent
  noise gives).
- `null_statistics`: what that number looks like with no clock pattern at all. Shuffling single hours (the textbook
  test) assumes each hour is independent, but calm and wild stretches come in clusters, and clustered noise folds
  into bumps by chance. Shuffling the series in blocks keeps the clusters and breaks only their link to the clock.
- `seasonal_factors` / `seasonal_forecast`: a causal hour-by-hour multiplier (from past data only) on top of a plain
  volatility forecast, so the two can be scored against what happened.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def clock_phase(index: pd.DatetimeIndex, period_hours: int) -> np.ndarray:
    """Each hour's position within a period that is anchored to the clock (a week starts Monday 00:00 UTC)."""
    hours = (index.asi8 // 3_600_000_000_000) if index.unit == "ns" else (index.as_unit("ns").asi8 // 3_600_000_000_000)
    return ((hours - 96) % period_hours).astype(int)  # 1970-01-05 00:00 UTC was a Monday: 96 hours after the epoch


def fold(values: np.ndarray, phase: np.ndarray, period: int) -> np.ndarray:
    """The mean of `values` at each phase 0..period-1 (NaN where a phase never occurs)."""
    values = np.asarray(values, dtype=float)
    usable = np.isfinite(values)
    sums = np.bincount(phase[usable], weights=values[usable], minlength=period)
    counts = np.bincount(phase[usable], minlength=period)
    return np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)


def fold_statistic(values: np.ndarray, phase: np.ndarray, period: int) -> float:
    """How far the folded profile is from flat: the variance between phase means over what independent noise would give (about 1 when flat)."""
    values = np.asarray(values, dtype=float)
    usable = np.isfinite(values)
    values, phase = values[usable], phase[usable]
    counts = np.bincount(phase, minlength=period)
    means = np.bincount(phase, weights=values, minlength=period) / np.maximum(counts, 1)
    spread = float(np.sum(counts * (means - values.mean()) ** 2) / max(period - 1, 1))
    return spread / float(values.var(ddof=1))


def null_statistics(values: np.ndarray, phase: np.ndarray, period: int, *, block: int, runs: int = 500, seed: int = 0) -> np.ndarray:
    """`fold_statistic` of `runs` shuffles of the series against the same clock.

    `block=1` shuffles single observations (independent noise). A larger `block` cuts the series into pieces of
    random length averaging `block`, and shuffles the pieces: clusters of volatility stay together, their timing
    against the clock is lost.
    """
    values = np.asarray(values, dtype=float)
    usable = np.isfinite(values)
    values, phase = values[usable], phase[usable]
    rng = np.random.default_rng(seed)
    out = np.empty(runs)
    for run in range(runs):
        if block <= 1:
            shuffled = rng.permutation(values)
        else:
            cuts = np.flatnonzero(rng.random(len(values)) < 1.0 / block)
            pieces = np.split(values, cuts)
            order = rng.permutation(len(pieces))
            shuffled = np.concatenate([pieces[i] for i in order])
        out[run] = fold_statistic(shuffled, phase, period)
    return out


def seasonal_factors(values: pd.Series, period_hours: int, *, window_hours: int = 24 * 180, min_cycles: int = 8) -> pd.Series:
    """A multiplier per hour: how large `values` usually is at this hour's phase, relative to all hours, from the past `window_hours` only.

    The factor at time t uses observations strictly before t, so it can scale a forecast made before t. It is 1 until
    a phase has been seen `min_cycles` times.
    """
    phase = clock_phase(values.index, period_hours)
    cycles = max(window_hours // period_hours, 1)
    by_phase = values.groupby(phase).transform(lambda part: part.shift(1).rolling(cycles, min_periods=min_cycles).mean())
    overall = values.shift(1).rolling(window_hours, min_periods=min_cycles * period_hours).mean()
    factor = (by_phase / overall).replace([np.inf, -np.inf], np.nan)
    return factor.fillna(1.0).clip(lower=0.1, upper=10.0)


def ewma_forecast(variance: pd.Series, halflife_hours: float) -> pd.Series:
    """The exponentially weighted mean of past hourly variance: a forecast for each hour made at the end of the one before."""
    return variance.ewm(halflife=halflife_hours, min_periods=int(halflife_hours)).mean().shift(1)


def seasonal_forecast(variance: pd.Series, period_hours: int, *, halflife_hours: float = 24.0, window_hours: int = 24 * 180) -> pd.Series:
    """`ewma_forecast` of the variance with the clock pattern taken out, times the pattern's factor for the hour being forecast."""
    factor = seasonal_factors(variance, period_hours, window_hours=window_hours)
    return ewma_forecast(variance / factor, halflife_hours) * factor


def qlike(forecast: pd.Series, realized: pd.Series) -> float:
    """The QLIKE loss (0 when perfect; penalises forecasts that are too low more than ones too high)."""
    both = pd.concat([forecast, realized], axis=1).dropna()
    both = both[(both.iloc[:, 0] > 0) & (both.iloc[:, 1] > 0)]
    ratio = both.iloc[:, 1] / both.iloc[:, 0]
    return float(np.mean(ratio - np.log(ratio) - 1.0))
