"""Epoch folding, its clustering-aware test and the seasonal forecast (src/research/seasonal.py) on made-up series."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from src.research import seasonal

HOURS = pd.date_range("2024-01-01", periods=24 * 7 * 60, freq="h", tz="UTC")  # 60 weeks, starting on a Monday


def _clustered(seed: int, *, pattern: bool) -> pd.Series:
    """Hourly variance with slow volatility clusters, with or without a spike at 14:00 UTC every day."""
    rng = np.random.default_rng(seed)
    persistence = 0.99  # calm and wild stretches lasting days: the log of the level is a slow AR(1) with a standard deviation of about 0.8
    level = np.exp(lfilter([0.8 * np.sqrt(1.0 - persistence**2)], [1.0, -persistence], rng.normal(0, 1, len(HOURS))))
    noise = rng.chisquare(1, len(HOURS))
    clock = np.where(HOURS.hour == 14, 3.0, 1.0) if pattern else 1.0
    return pd.Series(level * noise * clock, index=HOURS)


def test_folding_finds_the_hour_and_the_phase_is_anchored_to_the_clock() -> None:
    assert seasonal.clock_phase(HOURS[:3], 168).tolist() == [0, 1, 2] and seasonal.clock_phase(pd.DatetimeIndex([HOURS[24 * 7 + 5]]), 168).tolist() == [5]  # Monday 00:00 is 0
    series = _clustered(1, pattern=True)
    profile = seasonal.fold(series.to_numpy(), seasonal.clock_phase(HOURS, 24), 24)
    assert int(np.argmax(profile)) == 14 and profile[14] > 2 * np.delete(profile, 14).mean()
    assert seasonal.fold_statistic(series.to_numpy(), seasonal.clock_phase(HOURS, 24), 24) > 10
    flat = np.random.default_rng(0).chisquare(1, len(HOURS))
    assert 0.3 < seasonal.fold_statistic(flat, seasonal.clock_phase(HOURS, 24), 24) < 2.5  # independent noise: about 1


def test_clustered_noise_fools_the_independent_test_and_not_the_block_test() -> None:
    series = _clustered(3, pattern=False).to_numpy()  # no clock pattern at all
    false_naive = false_block = 0
    for period in range(300, 2300, 100):  # twenty long trial periods: few blocks to average, so a few wild days stand out
        phase = seasonal.clock_phase(HOURS, period)
        statistic = seasonal.fold_statistic(series, phase, period)
        false_naive += statistic > np.quantile(seasonal.null_statistics(series, phase, period, block=1, runs=100, seed=1), 0.99)
        false_block += statistic > np.quantile(seasonal.null_statistics(series, phase, period, block=300, runs=100, seed=1), 0.99)
    assert false_naive >= 3 and false_block <= 1  # at a 1% level 20 tries should give about none: shuffling single hours finds several "cycles", shuffling blocks doesn't
    real = _clustered(3, pattern=True).to_numpy()
    phase = seasonal.clock_phase(HOURS, 24)
    assert seasonal.fold_statistic(real, phase, 24) > seasonal.null_statistics(real, phase, 24, block=200, runs=200, seed=1).max()  # a real pattern still stands out


def test_the_seasonal_forecast_uses_only_the_past_and_beats_the_plain_one_when_there_is_a_pattern() -> None:
    series = _clustered(5, pattern=True)
    factors = seasonal.seasonal_factors(series, 24)
    assert (factors.iloc[: 24 * 7] == 1.0).all() and factors[HOURS.hour == 14].iloc[-50:].mean() > 2.0  # 1 until it has seen enough days; then about 3x at 14:00
    changed = series.copy()
    changed.iloc[5000:] *= 100.0
    assert np.allclose(seasonal.seasonal_factors(changed, 24).iloc[:5000], factors.iloc[:5000])  # the future doesn't reach back
    plain, with_clock = seasonal.ewma_forecast(series, 24.0), seasonal.seasonal_forecast(series, 24)
    measured = series.iloc[24 * 60:]
    assert seasonal.qlike(with_clock.reindex(measured.index), measured) < seasonal.qlike(plain.reindex(measured.index), measured)
    no_pattern = _clustered(6, pattern=False)
    gap = seasonal.qlike(seasonal.seasonal_forecast(no_pattern, 24).iloc[24 * 60:], no_pattern.iloc[24 * 60:]) - seasonal.qlike(seasonal.ewma_forecast(no_pattern, 24.0).iloc[24 * 60:], no_pattern.iloc[24 * 60:])
    assert abs(gap) < 0.1  # with no pattern the factor is noise around 1: little gained, little lost
