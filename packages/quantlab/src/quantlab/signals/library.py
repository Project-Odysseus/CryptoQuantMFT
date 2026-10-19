"""The signal library: the trend, mean-reversion, momentum and volatility signals the lab has used, on one scale (plan 3.1).

Every signal here is a Signal (quantlab.engine.components): one instrument's bars -> a forecast per bar, known at
that bar's close, in the forecast convention (long-run standard deviation about 1, capped at +/-2). Raw indicators
are put on that scale by `normalize`: divided by their own trailing root-mean-square, so the scale uses only the past.
Each docstring states the hypothesis, the expected sign and the horizon, so a test of the signal knows what it is
testing.

    from quantlab.signals import library as lib
    lib.ewmac(16, 64), lib.breakout(60), lib.zscore_reversion(40), lib.trend_forecast("compound_sigmoid")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from quantlab.conventions import FORECAST_CAP
from quantlab.features.indicators import atr, ema, linreg_tstat, rolling_max, rolling_mean, rolling_min, shift
from quantlab.signals.forecast import ACTIVATIONS, trend_forecast as _trend_forecast


def normalize(raw: pd.Series, *, window: int = 500, min_periods: int = 100, cap: float = FORECAST_CAP) -> pd.Series:
    """A raw indicator on the forecast scale: divided by its trailing root-mean-square (past bars only), capped."""
    rms = np.sqrt((raw.shift(1) ** 2).rolling(window, min_periods=min_periods).mean())
    return (raw / rms).clip(-cap, cap)


@dataclass(frozen=True)
class LibrarySignal:
    """A library signal: a name, a warm-up, a hypothesis, and the function of bars that computes it."""

    name: str
    function: Callable[[pd.DataFrame], pd.Series]
    warmup: int
    hypothesis: str
    sign: str  # what a positive value predicts
    horizon: str

    def __call__(self, bars: pd.DataFrame) -> pd.Series:
        out = self.function(bars).astype(float)
        out.iloc[: self.warmup] = np.nan
        return out.rename(self.name)


def _close(bars: pd.DataFrame) -> np.ndarray:
    return bars["close"].to_numpy(dtype=float)


# --- trend ---------------------------------------------------------------------------------------------------------

def ma_cross(fast: int = 4, slow: int = 48) -> LibrarySignal:
    """+1 / -1 as the fast simple average is above / below the slow one (the crypto books' 4/48 rule)."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        close = _close(bars)
        return pd.Series(np.sign(rolling_mean(close, fast) - rolling_mean(close, slow)), index=bars.index)
    return LibrarySignal(f"ma_cross_{fast}_{slow}", compute, slow, "Prices trend: the recent average leads the slow one.", "up-trend continues",
                         f"{slow // 2}-{slow * 2} bars")


def ewmac(fast: int = 16, slow: int = 64) -> LibrarySignal:
    """Exponential MA crossover scaled by price volatility, normalised: a continuous trend strength."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        close = _close(bars)
        vol = pd.Series(close).diff().ewm(span=36, min_periods=20).std().to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            raw = (ema(close, fast) - ema(close, slow)) / vol
        return normalize(pd.Series(raw, index=bars.index))
    return LibrarySignal(f"ewmac_{fast}_{slow}", compute, slow * 2, "Prices trend; a stronger move relative to noise trends more.", "up-trend continues",
                         f"{slow // 2}-{slow * 2} bars")


def breakout(window: int = 60) -> LibrarySignal:
    """Position of the close in its previous `window`-bar range: +1 at the high, -1 at the low (beyond them capped),
    scaled by 1.7 so a uniform position in the range has a standard deviation of about 1."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        close = _close(bars)
        high, low = shift(rolling_max(bars["high"].to_numpy(dtype=float), window)), shift(rolling_min(bars["low"].to_numpy(dtype=float), window))
        with np.errstate(divide="ignore", invalid="ignore"):
            raw = 2.0 * (close - low) / (high - low) - 1.0
        return pd.Series(np.clip(raw, -1.0, 1.0), index=bars.index) * 1.7
    return LibrarySignal(f"breakout_{window}", compute, window + 1, "New highs follow new highs (Donchian breakout).", "up-trend continues",
                         f"{window // 2}-{window} bars")


def trend_tstat(window: int = 60) -> LibrarySignal:
    """Slope t-statistic of log price over `window` bars, normalised: steep and clean trends score high."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        return normalize(pd.Series(linreg_tstat(np.log(_close(bars)), window), index=bars.index).replace([np.inf, -np.inf], np.nan))
    return LibrarySignal(f"trend_tstat_{window}", compute, window, "Trends that are steep relative to their noise persist.", "up-trend continues",
                         f"{window // 2}-{window} bars")


def trend_forecast(activation: str = "compound_sigmoid", speeds: tuple[tuple[int, int], ...] = ((10, 20), (20, 40), (40, 80))) -> LibrarySignal:
    """H13's volatility-normalised EMA difference through an activation (Martin 2023); already on the forecast scale."""
    if activation not in ACTIVATIONS:
        raise ValueError(f"activation must be one of {sorted(ACTIVATIONS)}")

    def compute(bars: pd.DataFrame) -> pd.Series:
        return pd.Series(_trend_forecast(_close(bars), activation=activation, speeds=speeds), index=bars.index)
    return LibrarySignal(f"trend_forecast_{activation}", compute, 2 * max(slow for _fast, slow in speeds), "Trends persist; size grows with strength (positive skew).",
                         "up-trend continues", "20-200 bars")


def h14_trend(fast: int = 20, slow: int = 100, entry: int = 60, exit_window: int = 20) -> LibrarySignal:
    """H14's trend model: 0.5 tanh(MA trend / ATR / 2) + 0.5 x a breakout state."""
    from quantlab.strategies.regime_overlay import OverlayConfig, TrendSettings, trend_model

    config = OverlayConfig(trend=TrendSettings(fast_mean=fast, slow_mean=slow, breakout_entry=entry, breakout_exit=exit_window))

    def compute(bars: pd.DataFrame) -> pd.Series:
        return trend_model(bars, config)["trend_signal"]
    return LibrarySignal("h14_trend", compute, slow, "Prices trend (MA plus breakout).", "up-trend continues", "20-120 bars")


# --- mean reversion --------------------------------------------------------------------------------------------------

def zscore_reversion(anchor: int = 40, window: int = 40) -> LibrarySignal:
    """Minus the z-score of the close around its EWMA, normalised: stretched prices come back toward the anchor."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        close = _close(bars)
        residual = pd.Series(close - ema(close, anchor), index=bars.index)
        z = residual / residual.rolling(window).std()
        return (-z / 1.25).clip(-FORECAST_CAP, FORECAST_CAP)
    return LibrarySignal(f"zscore_reversion_{anchor}", compute, max(anchor, window) + 1, "Deviations from a slow anchor revert.", "a fall reverts up",
                         "1-20 bars")


def short_reversal(lookback: int = 5) -> LibrarySignal:
    """Minus the last `lookback` bars' volatility-scaled return, normalised: short-term overreaction reverts."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        returns = bars["close"].pct_change()
        vol = returns.rolling(60, min_periods=20).std()
        return normalize(-returns.rolling(lookback).sum() / (vol * np.sqrt(lookback)))
    return LibrarySignal(f"short_reversal_{lookback}", compute, 60, "Short-horizon moves overshoot and partly reverse.", "a fall reverts up",
                         f"1-{lookback} bars")


# --- momentum and volatility -------------------------------------------------------------------------------------------

def time_series_momentum(lookback: int = 252, skip: int = 21) -> LibrarySignal:
    """Sign of the return from `lookback` to `skip` bars ago (Moskowitz, Ooi and Pedersen, 2012)."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        close = bars["close"]
        return np.sign(close.shift(skip) / close.shift(lookback) - 1.0)
    return LibrarySignal(f"tsmom_{lookback}_{skip}", compute, lookback, "Twelve-month winners keep winning for a month.", "up-trend continues", "21 bars")


def low_volatility(window: int = 20, long_window: int = 250) -> LibrarySignal:
    """Minus the short-over-long volatility ratio, normalised: calm markets earn more per unit of risk."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        returns = bars["close"].pct_change()
        ratio = np.log(returns.rolling(window).std() / returns.rolling(long_window).std())
        return normalize(-ratio)
    return LibrarySignal(f"low_vol_{window}_{long_window}", compute, long_window, "Volatility spikes come with poor near-term returns.",
                         "calm predicts better returns", "5-60 bars")


def atr_channel(window: int = 40, multiplier: float = 2.0) -> LibrarySignal:
    """Keltner-style: +1 above the average + multiplier x ATR until a close back below the average; -1 mirrored."""
    def compute(bars: pd.DataFrame) -> pd.Series:
        close = _close(bars)
        middle = shift(rolling_mean(close, window))
        band = multiplier * shift(atr(bars["high"].to_numpy(dtype=float), bars["low"].to_numpy(dtype=float), close, window))
        state, current = np.zeros(len(close)), 0.0
        for index in range(len(close)):
            if not np.isfinite(middle[index]) or not np.isfinite(band[index]):
                continue
            if current > 0 and close[index] < middle[index] or current < 0 and close[index] > middle[index]:
                current = 0.0
            if close[index] > middle[index] + band[index]:
                current = 1.0
            elif close[index] < middle[index] - band[index]:
                current = -1.0
            state[index] = current
        return pd.Series(state, index=bars.index)
    return LibrarySignal(f"atr_channel_{window}", compute, window + 2, "Breaks of a volatility-scaled channel trend.", "up-trend continues",
                         f"{window // 2}-{window * 2} bars")


CATALOG: dict[str, Callable[..., LibrarySignal]] = {
    "ma_cross": ma_cross, "ewmac": ewmac, "breakout": breakout, "trend_tstat": trend_tstat, "trend_forecast": trend_forecast, "h14_trend": h14_trend,
    "zscore_reversion": zscore_reversion, "short_reversal": short_reversal, "time_series_momentum": time_series_momentum, "low_volatility": low_volatility,
    "atr_channel": atr_channel,
}


def relative_momentum(panel_close: pd.DataFrame, lookback: int = 252, skip: int = 21) -> pd.DataFrame:
    """Cross-sectional momentum: each date's ranking of the lookback-to-skip return, centred and scaled to [-1, 1]."""
    past = panel_close.shift(skip) / panel_close.shift(lookback) - 1.0
    ranks = past.rank(axis=1, pct=True)
    return (2.0 * ranks - 1.0 - (2.0 * ranks - 1.0).mean(axis=1).to_numpy()[:, None]).clip(-1, 1) * 1.5
