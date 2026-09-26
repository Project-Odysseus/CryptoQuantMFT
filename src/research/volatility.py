"""Volatility forecasts and volatility-scaled position sizes, all causal.

Returns are hard to predict, but volatility isn't: calm and turbulent periods
cluster, so recent volatility says a lot about the next days'. Sizing each
position so its forecast volatility is constant (volatility targeting) takes
risk off while a turbulent period is still going and adds it back when markets
calm down. That usually improves Sharpe and cuts drawdowns without touching the
signal.

Forecasts here are annualised volatility (0.5 = 50% a year), made at a bar's
close from data up to that close:

- `rolling_vol`: the standard deviation of the last `window` bar returns. The
  runtime's `RiskManager` does this over 10 bars.
- `ewma_vol`: an exponentially weighted variance (RiskMetrics); it reacts
  quickly and needs no fitting.
- `har_forecast`: HAR-RV (Corsi, 2009). It regresses the coming days' realized
  variance on the last day's, week's and month's, with realized variance
  measured from intraday returns (`daily_realized_variance`), refitted
  walk-forward.

`vol_scaled_positions` turns long/flat/short targets and a forecast into
position sizes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365.0


def log_returns(close: np.ndarray | Sequence[float]) -> np.ndarray:
    """Bar-to-bar log returns; the first value is NaN."""
    values = np.asarray(close, dtype=float)
    out = np.full(len(values), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = np.log(values[1:] / values[:-1])
    return out


def rolling_vol(returns: np.ndarray, window: int, periods_per_year: float) -> np.ndarray:
    """Annualised standard deviation of the last `window` returns (population std, like the runtime)."""
    series = pd.Series(returns)
    return (series.rolling(window, min_periods=window).std(ddof=0) * np.sqrt(periods_per_year)).to_numpy()


def ewma_variance(values: np.ndarray, halflife: float) -> np.ndarray:
    """Exponentially weighted mean of `values` (e.g. squared returns), using data up to each point only."""
    return pd.Series(values).ewm(halflife=halflife, min_periods=max(2, int(halflife))).mean().to_numpy()


def ewma_vol(returns: np.ndarray, halflife: float, periods_per_year: float) -> np.ndarray:
    """Annualised EWMA volatility of `returns` (zero-mean, as RiskMetrics does)."""
    return np.sqrt(ewma_variance(np.square(returns), halflife) * periods_per_year)


def daily_realized_variance(timestamps: Sequence[Any], close: np.ndarray | Sequence[float]) -> pd.Series:
    """Sum of squared intraday log returns per UTC day, indexed by the day.

    `timestamps` are bar open times (as the Kraken candles are stamped), so a
    bar's return belongs to the day it opened in. Days missing more than a
    quarter of their bars are dropped rather than understated.
    """
    index = pd.DatetimeIndex(pd.to_datetime(list(timestamps), utc=True))
    frame = pd.DataFrame({"r2": np.square(log_returns(close))}, index=index).dropna()
    per_day = frame.groupby(frame.index.floor("D"))["r2"].agg(["sum", "count"])
    spacing = pd.Series(index).diff().median()
    bars_per_day = pd.Timedelta(days=1) / spacing if pd.notna(spacing) and spacing > pd.Timedelta(0) else 1.0
    complete = per_day["count"] >= 0.75 * bars_per_day
    return per_day.loc[complete, "sum"].rename("realized_variance")


def har_forecast(daily_rv: pd.Series, *, horizon_days: int = 1, train_min_days: int = 365, refit_every_days: int = 30) -> pd.Series:
    """Walk-forward log-HAR forecast of the average daily realized variance over the next `horizon_days`.

    Features at each day's close: log RV of that day, of the last 7 days and of
    the last 30 days. At each refit, OLS is fitted on days whose target
    window had closed before the refit day; forecasts until the next refit
    use those coefficients. Logs keep single crash days from dominating the fit;
    `exp(fit + residual variance / 2)` converts back to variance without bias.

    Returns daily variance (not annualised), indexed like `daily_rv`, NaN
    before the first fit.
    """
    rv = daily_rv.astype(float).clip(lower=1e-12)
    features = pd.DataFrame({
        "day": np.log(rv),
        "week": np.log(rv.rolling(7).mean()),
        "month": np.log(rv.rolling(30).mean()),
    })
    target = np.log(rv.rolling(horizon_days).mean().shift(-horizon_days))
    x = np.column_stack([np.ones(len(rv)), features.to_numpy()])
    y = target.to_numpy()
    forecast = np.full(len(rv), np.nan)
    for start in range(train_min_days, len(rv), refit_every_days):
        train_end = start - horizon_days
        usable = np.isfinite(x[:train_end]).all(axis=1) & np.isfinite(y[:train_end])
        if usable.sum() < 60:
            continue
        beta, *_ = np.linalg.lstsq(x[:train_end][usable], y[:train_end][usable], rcond=None)
        residual_var = float(np.var(y[:train_end][usable] - x[:train_end][usable] @ beta))
        block = slice(start, min(start + refit_every_days, len(rv)))
        forecast[block] = np.exp(x[block] @ beta + residual_var / 2.0)
    return pd.Series(forecast, index=rv.index, name="har_variance")


def daily_forecast_to_bars(daily_annual_vol: pd.Series, bar_close_times: Sequence[Any]) -> np.ndarray:
    """For each bar, the forecast made at the close of the last day that had completed by the bar's close."""
    closes = pd.DatetimeIndex(pd.to_datetime(list(bar_close_times), utc=True))
    completed_day = closes.floor("D") - pd.Timedelta(days=1)
    return daily_annual_vol.reindex(completed_day).to_numpy()


def forecast_losses(forecast_variance: np.ndarray, realized_variance: np.ndarray) -> dict[str, float]:
    """How well variance forecasts match what happened.

    - `qlike`: the standard loss for volatility forecasts (0 when perfect). It
      stays robust when the realized value is a noisy proxy and penalises
      under-forecasting more, which is the costly error for sizing.
    - `r2_log`: squared correlation of log forecast and log realized variance.
    - `mean_ratio`: mean of realized / forecast variance; 1 is unbiased, above
      1 means forecasts ran low.
    """
    forecast_variance = np.asarray(forecast_variance, dtype=float)
    realized_variance = np.asarray(realized_variance, dtype=float)
    usable = np.isfinite(forecast_variance) & np.isfinite(realized_variance) & (forecast_variance > 0) & (realized_variance > 0)
    ratio = realized_variance[usable] / forecast_variance[usable]
    log_f, log_r = np.log(forecast_variance[usable]), np.log(realized_variance[usable])
    return {
        "qlike": float(np.mean(ratio - np.log(ratio) - 1.0)),
        "r2_log": float(np.corrcoef(log_f, log_r)[0, 1] ** 2),
        "mean_ratio": float(np.mean(ratio)),
        "days": int(usable.sum()),
    }


def vol_scaled_positions(
    targets: Sequence[float],
    annual_vol_forecast: Sequence[float],
    *,
    target_vol: float,
    max_leverage: float = 2.0,
    rebalance_band: float = 0.25,
    entry_only: bool = False,
) -> np.ndarray:
    """Scale long/flat/short targets so each position's forecast volatility is `target_vol`.

    Size = min(max_leverage, target_vol / forecast) in multiples of equity.
    While a position is open, the size only changes when the ideal size has
    moved more than `rebalance_band` (relative), so small forecast wiggles
    don't pay fees. `entry_only` fixes the size when a position opens, which
    is what the runtime does today. Where the forecast is missing, the size
    is 1 (full equity, unscaled).
    """
    signs = np.sign(np.nan_to_num(np.asarray(targets, dtype=float)))
    forecast = np.asarray(annual_vol_forecast, dtype=float)
    out = np.zeros(len(signs))
    current = 0.0
    for index, sign in enumerate(signs):
        if sign == 0.0:
            current = 0.0
        else:
            vol = forecast[index]
            ideal = min(max_leverage, target_vol / vol) if np.isfinite(vol) and vol > 0.0 else 1.0
            if current == 0.0 or np.sign(current) != sign:
                current = sign * ideal
            elif not entry_only and abs(ideal - abs(current)) > rebalance_band * abs(current):
                current = sign * ideal
        out[index] = current
    return out
