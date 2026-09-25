"""Vectorized, causal technical indicators for writing strategies.

Every function takes numpy arrays ordered oldest-first and returns an array
of the same length. Values are NaN wherever there isn't enough history yet,
and the value at index ``t`` only ever uses data at or before ``t``, so
these are safe to call on a strategy's ``history`` without look-ahead.

NaN compares False against anything, so an entry condition like
``close > rolling_max(high, 20)`` is automatically False during warmup.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

_PRICE_FIELDS_FALLING_BACK_TO_CLOSE = {"open", "high", "low"}


def series(history: Sequence[Any], field: str = "close") -> np.ndarray:
    """Extract one OHLCV field from a list of bars (objects or dicts) as a float array.

    Bars that lack open/high/low fall back to their close, so close-only
    data still works with range-based indicators (the range is just zero).
    """
    values = np.empty(len(history), dtype=float)
    for position, bar in enumerate(history):
        if isinstance(bar, dict):
            raw = bar.get(field)
            if raw is None and field in _PRICE_FIELDS_FALLING_BACK_TO_CLOSE:
                raw = bar["close"]
        else:
            raw = getattr(bar, field, None)
            if raw is None and field in _PRICE_FIELDS_FALLING_BACK_TO_CLOSE:
                raw = bar.close
        values[position] = float(raw if raw is not None else 0.0)
    return values


def shift(values: np.ndarray, periods: int = 1) -> np.ndarray:
    """Shift values later in time by `periods` bars (NaN-filled), e.g. to compare against the *prior* N bars."""
    out = np.full(len(values), np.nan)
    if periods < len(values):
        out[periods:] = values[: len(values) - periods]
    return out


def _rolling(values: np.ndarray, window: int, reducer: Any) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if window <= 0:
        raise ValueError("window must be positive")
    if len(values) >= window:
        out[window - 1 :] = reducer(sliding_window_view(values, window), axis=1)
    return out


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Simple moving average over the last `window` values (including the current one)."""
    return _rolling(values, window, np.mean)


def rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    """Population standard deviation over the last `window` values."""
    return _rolling(values, window, np.std)


def rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    """Highest value over the last `window` values."""
    return _rolling(values, window, np.max)


def rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    """Lowest value over the last `window` values."""
    return _rolling(values, window, np.min)


def rolling_quantile(values: np.ndarray, window: int, quantile: float) -> np.ndarray:
    """The `quantile` (0-1) of the last `window` values; NaN if the window contains NaN."""
    return _rolling(values, window, lambda windows, axis: np.quantile(windows, quantile, axis=axis))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Per-bar true range: the bar's range extended to include any gap from the prior close."""
    previous_close = shift(close)
    ranges = np.vstack([high - low, np.abs(high - previous_close), np.abs(low - previous_close)])
    return np.nanmax(ranges, axis=0)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int) -> np.ndarray:
    """Average true range as a simple moving average of true range (not Wilder-smoothed)."""
    return rolling_mean(true_range(high, low, close), window)


def rsi(close: np.ndarray, window: int) -> np.ndarray:
    """Relative Strength Index (0-100) using simple averages of gains/losses (Cutler's RSI).

    Wilder's original RSI smooths recursively from the first bar, so its
    value depends on where the history starts. The simple-average version
    only depends on the last `window` changes, which keeps backtest folds
    and live restarts consistent with each other.
    """
    out = np.full(len(close), np.nan)
    if len(close) < window + 1:
        return out
    changes = np.diff(close)
    avg_gain = rolling_mean(np.clip(changes, 0.0, None), window)
    avg_loss = rolling_mean(np.clip(-changes, 0.0, None), window)
    with np.errstate(divide="ignore", invalid="ignore"):
        values = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    values = np.where((avg_loss == 0.0) & (avg_gain > 0.0), 100.0, values)
    values = np.where((avg_loss == 0.0) & (avg_gain == 0.0), 50.0, values)
    out[1:] = values
    return out


def linreg_tstat(values: np.ndarray, window: int) -> np.ndarray:
    """t-statistic of the OLS slope of `values` against time over the last `window` bars.

    High positive = a steep *and* clean up-trend relative to the noise
    around it; near zero = no trend or too choppy to tell. Because prices
    are autocorrelated this is not a valid significance test (the values
    run much larger than textbook t-values), so treat it as a
    noise-normalised trend-strength score and calibrate thresholds on data.
    A perfectly straight line has zero residuals and returns +/-inf.
    """
    if window < 3:
        raise ValueError("window must be at least 3 for a slope t-statistic")
    out = np.full(len(values), np.nan)
    if len(values) < window:
        return out
    windows = sliding_window_view(values, window)
    time_centered = np.arange(window) - (window - 1) / 2.0
    sxx = float(np.sum(time_centered**2))
    slope = windows @ time_centered / sxx
    residuals = windows - windows.mean(axis=1, keepdims=True) - slope[:, None] * time_centered
    residual_var = np.sum(residuals**2, axis=1) / (window - 2)
    standard_error = np.sqrt(residual_var / sxx)
    with np.errstate(divide="ignore", invalid="ignore"):
        tstat = np.where(standard_error > 0.0, slope / standard_error, np.sign(slope) * np.inf)
    out[window - 1 :] = tstat
    return out
