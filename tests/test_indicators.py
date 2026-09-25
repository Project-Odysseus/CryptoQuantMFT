"""Tests for the vectorized strategy indicators."""

from __future__ import annotations

import math

import numpy as np

from src.backtest.indicators import atr, linreg_tstat, rolling_max, rolling_mean, rolling_min, rolling_quantile, rsi, series, shift


def test_rolling_mean_is_nan_during_warmup_then_averages() -> None:
    """Values before a full window are NaN, then it's a plain moving average."""
    values = rolling_mean(np.array([1.0, 2.0, 3.0, 4.0]), 2)
    assert math.isnan(values[0])
    assert list(values[1:]) == [1.5, 2.5, 3.5]


def test_rolling_max_min_and_shift_give_the_prior_window_extremes() -> None:
    """shift(rolling_max(...)) is the highest value of the *prior* N bars, excluding the current one."""
    values = np.array([1.0, 5.0, 2.0, 3.0])
    assert list(rolling_max(values, 2)[1:]) == [5.0, 5.0, 3.0]
    assert list(rolling_min(values, 2)[1:]) == [1.0, 2.0, 2.0]
    prior_max = shift(rolling_max(values, 2))
    assert math.isnan(prior_max[1])
    assert list(prior_max[2:]) == [5.0, 5.0]


def test_rolling_quantile_matches_numpy_on_each_window() -> None:
    """Rolling quantile should equal np.quantile applied to each trailing window."""
    values = np.array([4.0, 1.0, 3.0, 2.0, 5.0])
    result = rolling_quantile(values, 3, 0.5)
    assert list(result[2:]) == [3.0, 2.0, 3.0]


def test_rsi_is_100_for_only_gains_0_for_only_losses_and_50_when_flat() -> None:
    """RSI extremes should behave sensibly, including the no-movement case."""
    rising = rsi(np.arange(10.0), 3)
    falling = rsi(np.arange(10.0)[::-1].copy(), 3)
    flat = rsi(np.full(10, 5.0), 3)
    assert math.isnan(rising[2])
    assert rising[-1] == 100.0
    assert falling[-1] == 0.0
    assert flat[-1] == 50.0


def test_atr_equals_the_bar_range_when_there_are_no_gaps() -> None:
    """With a constant 2-point range and no gaps, ATR is 2."""
    close = np.full(6, 100.0)
    high = close + 1.0
    low = close - 1.0
    assert atr(high, low, close, 3)[-1] == 2.0


def test_linreg_tstat_sign_follows_the_trend_and_is_infinite_for_a_perfect_line() -> None:
    """Noisy up/down trends give positive/negative t-stats; a perfect line has zero residuals."""
    noise = np.array([0.3, -0.2, 0.1, -0.3, 0.2, -0.1, 0.25, -0.15, 0.05, -0.05])
    up = np.arange(10.0) + noise
    down = -np.arange(10.0) + noise
    assert linreg_tstat(up, 10)[-1] > 5.0
    assert linreg_tstat(down, 10)[-1] < -5.0
    assert linreg_tstat(np.arange(10.0), 10)[-1] == math.inf


def test_series_falls_back_to_close_for_missing_high_low() -> None:
    """Close-only dict bars should still work with range-based indicators."""
    bars = [{"close": 1.0}, {"close": 2.0}]
    assert list(series(bars, "high")) == [1.0, 2.0]
    assert list(series(bars, "volume")) == [0.0, 0.0]
