"""Causal market-structure features for a trend/range regime overlay.

Ported from the host's src/signals/regime_features.py (docs/quantlab_migration.md); behaviour unchanged.

Every value at bar t uses data up to and including bar t's close only; "previous N-day" highs and lows exclude bar t
itself, so a breakout can never widen the boundary it is measured against. Percentile scores compare a value with a
reference set fixed before the calendar year starts (the trailing `calibration_days` before 1 January) and frozen
within the year: walk-forward calibration, so appending later data never changes an earlier score.

- `efficiency_ratio`: Kaufman's |net move| / path length over a window (0 = pure chop, 1 = straight line).
- `slope_tstat`: OLS slope t-statistic of log price over a window (reuses `indicators.linreg_tstat`).
- `adx`: Wilder's average directional index.
- `variance_ratio`: Lo-MacKinlay overlapping variance ratio of q-day to 1-day returns, bias-corrected.
- `volatility_ratio`: short over long realised volatility.
- `breakout_distance`: how far the close is beyond the previous N-day high / low, in ATRs.
- `calendar_percentile`: the walk-forward percentile transform.
- `adf_tstat`: rolling augmented Dickey-Fuller t-statistic (diagnostic only; never a live rule).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from quantlab.features.indicators import atr, linreg_tstat, rolling_max, rolling_min, shift

TRADING_DAYS = 252.0


def previous_high(high: np.ndarray, window: int) -> np.ndarray:
    """The highest high of the `window` bars before bar t (bar t excluded)."""
    return shift(rolling_max(np.asarray(high, dtype=float), window))


def previous_low(low: np.ndarray, window: int) -> np.ndarray:
    """The lowest low of the `window` bars before bar t (bar t excluded)."""
    return shift(rolling_min(np.asarray(low, dtype=float), window))


def efficiency_ratio(close: np.ndarray, window: int) -> np.ndarray:
    """|close_t - close_{t-window}| / sum of |daily changes| over the same `window` steps; NaN until full."""
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), np.nan)
    if len(close) <= window:
        return out
    steps = np.abs(np.diff(close))
    path = np.convolve(steps, np.ones(window), mode="valid")  # sums of `window` consecutive steps
    net = np.abs(close[window:] - close[:-window])
    with np.errstate(divide="ignore", invalid="ignore"):
        out[window:] = np.where(path > 0.0, net / path, 0.0)
    return out


def slope_tstat(close: np.ndarray, window: int) -> np.ndarray:
    """Signed OLS slope t-statistic of log(close) over the last `window` observations (bar t included)."""
    return linreg_tstat(np.log(np.asarray(close, dtype=float)), window)


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 14) -> np.ndarray:
    """Wilder's ADX: smoothed directional movement and true range, then a smoothed directional index."""
    high, low, close = (np.asarray(values, dtype=float) for values in (high, low, close))
    count = len(close)
    out = np.full(count, np.nan)
    if count < 2 * window + 1:
        return out
    up, down = np.diff(high), -np.diff(low)
    plus_dm = np.where((up > down) & (up > 0.0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0.0), down, 0.0)
    true_range = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])])

    def wilder(values: np.ndarray) -> np.ndarray:
        smoothed = np.full(len(values), np.nan)
        smoothed[window - 1] = values[:window].sum()
        for index in range(window, len(values)):
            smoothed[index] = smoothed[index - 1] - smoothed[index - 1] / window + values[index]
        return smoothed

    tr, plus, minus = wilder(true_range), wilder(plus_dm), wilder(minus_dm)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di, minus_di = 100.0 * plus / tr, 100.0 * minus / tr
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    dx = np.nan_to_num(dx, nan=0.0)
    values = np.full(len(dx), np.nan)
    first = 2 * window - 2
    values[first] = dx[window - 1:first + 1].mean()
    for index in range(first + 1, len(dx)):
        values[index] = (values[index - 1] * (window - 1) + dx[index]) / window
    out[1:] = values  # diffs start at bar 1
    return out


def variance_ratio(log_returns: np.ndarray, window: int, lag: int = 5) -> np.ndarray:
    """Lo-MacKinlay overlapping variance ratio VR(q) over the last `window` one-day log returns (bar t included).

    With n = window returns, mean m: sigma_a^2 = sum (r - m)^2 / (n - 1); the q-day sums over the n - q + 1
    overlapping windows give sigma_c^2 = sum (R_q - q m)^2 / M with M = q (n - q + 1)(1 - q / n), the unbiased
    overlapping estimator. VR = sigma_c^2 / sigma_a^2: above 1 persistence, below 1 mean reversion.
    """
    returns = np.asarray(log_returns, dtype=float)
    out = np.full(len(returns), np.nan)
    if len(returns) < window or window <= lag:
        return out
    windows = sliding_window_view(returns, window)  # rows end at bar t
    mean = windows.mean(axis=1, keepdims=True)
    sigma_a = ((windows - mean) ** 2).sum(axis=1) / (window - 1)
    sums = sliding_window_view(windows, lag, axis=1).sum(axis=2)  # rows x (n - q + 1)
    scale = lag * (window - lag + 1) * (1.0 - lag / window)
    sigma_c = ((sums - lag * mean) ** 2).sum(axis=1) / scale
    valid = np.isfinite(windows).all(axis=1) & (sigma_a > 0)
    out[window - 1:] = np.where(valid, sigma_c / np.where(sigma_a > 0, sigma_a, 1.0), np.nan)
    return out


def realised_volatility(log_returns: np.ndarray, window: int) -> np.ndarray:
    """Annualised standard deviation of the last `window` daily log returns."""
    return pd.Series(log_returns).rolling(window).std().to_numpy() * math.sqrt(TRADING_DAYS)


def volatility_ratio(log_returns: np.ndarray, short: int, long: int) -> np.ndarray:
    """Short-window over long-window realised volatility."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return realised_volatility(log_returns, short) / realised_volatility(log_returns, long)


def breakout_distance(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int, atr_values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """((close - previous high) / ATR, (previous low - close) / ATR): positive when the close is beyond that boundary."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return (close - previous_high(high, window)) / atr_values, (previous_low(low, window) - close) / atr_values


def calendar_percentile(values: pd.Series, *, calibration_days: int, min_calibration: int) -> pd.Series:
    """Each value's percentile in a reference set fixed before its calendar year and frozen within it.

    The reference for year Y is the last `calibration_days` finite values strictly before 1 January of Y. With fewer
    than `min_calibration`, the score is NaN (warm-up). Ties count half, so the score of the reference median is 0.5.
    """
    out = pd.Series(np.nan, index=values.index)
    for year in sorted(set(values.index.year)):
        start = pd.Timestamp(year=year, month=1, day=1, tz=values.index.tz)
        reference = values[values.index < start].dropna().to_numpy()[-calibration_days:]
        if len(reference) < min_calibration:
            continue
        reference = np.sort(reference)
        current = values[(values.index >= start) & (values.index.year == year)]
        finite = current.dropna()
        below = np.searchsorted(reference, finite.to_numpy(), side="left")
        equal = np.searchsorted(reference, finite.to_numpy(), side="right") - below
        out.loc[finite.index] = (below + 0.5 * equal) / len(reference)
    return out


def adf_tstat(series: np.ndarray, window: int, *, lags: int = 1, every: int = 5) -> np.ndarray:
    """Rolling ADF t-statistic (constant, `lags` lagged differences) over the last `window` values, every `every` bars.

    Diagnostic only: low power in short windows and unstable under changing volatility, so it never drives a rule.
    Around -2.9 is the 5% critical value with a constant; more negative = more evidence of mean reversion.
    """
    values = np.asarray(series, dtype=float)
    out = np.full(len(values), np.nan)
    for end in range(window, len(values) + 1, every):
        chunk = values[end - window:end]
        if not np.isfinite(chunk).all():
            continue
        diff = np.diff(chunk)
        y = diff[lags:]
        columns = [chunk[lags:-1], np.ones(len(y))] + [diff[lags - lag:-lag] for lag in range(1, lags + 1)]
        x = np.column_stack(columns)
        coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        residual = y - x @ coef
        dof = len(y) - x.shape[1]
        if dof <= 0:
            continue
        sigma2 = residual @ residual / dof
        try:
            cov = sigma2 * np.linalg.inv(x.T @ x)
        except np.linalg.LinAlgError:
            continue
        out[end - 1] = coef[0] / math.sqrt(cov[0, 0]) if cov[0, 0] > 0 else np.nan
    return pd.Series(out).ffill(limit=every - 1).to_numpy()


def build_features(frame: pd.DataFrame, *, horizon: int, adx_window: int, vr_lag: int, short_vol: int, long_vol: int, breakout: int,
                   atr_window: int = 20) -> pd.DataFrame:
    """All raw regime features for an OHLC frame (index: dates), each known at its bar's close."""
    high, low, close = (frame[name].to_numpy(dtype=float) for name in ("high", "low", "close"))
    log_returns = np.concatenate([[np.nan], np.diff(np.log(close))])
    atr_values = atr(high, low, close, atr_window)
    upper, lower = breakout_distance(high, low, close, breakout, atr_values)
    slope = slope_tstat(close, horizon)
    return pd.DataFrame({
        "er": efficiency_ratio(close, horizon), "slope_t": slope, "abs_slope_t": np.abs(slope), "adx": adx(high, low, close, adx_window),
        "vr": variance_ratio(log_returns, horizon, vr_lag), "short_vol": realised_volatility(log_returns, short_vol),
        "long_vol": realised_volatility(log_returns, long_vol), "vol_ratio": volatility_ratio(log_returns, short_vol, long_vol),
        "upper_distance": upper, "lower_distance": lower, "breakout_beyond": np.fmax(upper, lower), "atr": atr_values,
        "log_return": log_returns,
    }, index=frame.index)
