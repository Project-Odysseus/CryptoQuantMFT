"""Does a feature predict future returns? Measure that before building a strategy on it.

A strategy is a feature plus trading rules plus costs. Testing only the
finished strategy mixes the three up. These helpers look at the first part
on its own: bucket bars by a feature measured at the bar's close, then ask
what price did *afterwards*, in basis points, so the answer can be compared
directly with the round-trip cost (about 20 bps taker, 4 bps maker on
Kraken perpetuals).

Every feature must use data up to and including bar t only; forward returns
start at bar t's close. That is the whole look-ahead contract. t-statistics
are computed on non-overlapping observations (every `horizon`-th bar), because
overlapping forward returns share most of their data and would overstate
significance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.indicators import rolling_mean, rolling_std, series

BPS = 10_000.0


def bars_frame(bars: Sequence[Any]) -> pd.DataFrame:
    """OHLCV bars as a DataFrame indexed by UTC timestamp."""
    return pd.DataFrame(
        {field: series(bars, field) for field in ("open", "high", "low", "close", "volume")},
        index=pd.DatetimeIndex([bar.timestamp for bar in bars], name="timestamp"),
    )


def forward_return(close: np.ndarray | pd.Series, horizon: int) -> np.ndarray:
    """Log return from each bar's close to the close `horizon` bars later (NaN where it runs off the end)."""
    values = np.log(np.asarray(close, dtype=float))
    out = np.full(len(values), np.nan)
    if horizon < len(values):
        out[:-horizon] = values[horizon:] - values[:-horizon]
    return out


def past_return(close: np.ndarray | pd.Series, lookback: int) -> np.ndarray:
    """Log return over the last `lookback` bars, ending at each bar's close."""
    values = np.log(np.asarray(close, dtype=float))
    out = np.full(len(values), np.nan)
    if lookback < len(values):
        out[lookback:] = values[lookback:] - values[:-lookback]
    return out


def zscore(values: np.ndarray, window: int) -> np.ndarray:
    """(value - rolling mean) / rolling std over the last `window` bars, using data up to each bar only."""
    values = np.asarray(values, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (values - rolling_mean(values, window)) / rolling_std(values, window)


def volatility_scaled(returns: np.ndarray, close: np.ndarray, window: int) -> np.ndarray:
    """Divide `returns` by the rolling std of one-bar log returns over `window` bars, so a move reads the same in
    calm and volatile periods (a 2% hour means more in a quiet week than in a crash)."""
    one_bar = past_return(close, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(returns, dtype=float) / rolling_std(np.nan_to_num(one_bar), window)


def _t_stat(values: np.ndarray) -> float:
    values = values[np.isfinite(values)]
    if values.size < 3 or values.std(ddof=1) == 0.0:
        return float("nan")
    return float(values.mean() / (values.std(ddof=1) / np.sqrt(values.size)))


def bucket_table(feature: np.ndarray, forward: np.ndarray, horizon: int, *, buckets: int = 5, labels: Sequence[Any] | None = None) -> pd.DataFrame:
    """Mean forward return (bps) per feature bucket, with counts, hit rate and a non-overlapping t-stat.

    Numeric features are cut into `buckets` equal-count quantiles. Pass
    `labels` (or a non-numeric feature such as hour of day) to group by the
    values directly. Quantile edges use the whole sample, which is fine for
    describing a relationship but is a mild look-ahead if the edges are
    later used as trading thresholds; use fixed thresholds in strategies.
    """
    frame = pd.DataFrame({"feature": feature, "forward": forward}).dropna()
    frame["sample"] = False
    frame.loc[frame.index[::horizon], "sample"] = True
    if labels is None and pd.api.types.is_numeric_dtype(frame["feature"]) and frame["feature"].nunique() > buckets:
        frame["bucket"] = pd.qcut(frame["feature"], buckets, labels=False, duplicates="drop") + 1
    else:
        frame["bucket"] = frame["feature"]
    rows = []
    for bucket, group in frame.groupby("bucket", sort=True):
        sampled = group.loc[group["sample"], "forward"].to_numpy()
        rows.append(
            {
                "bucket": bucket,
                "feature_from": float(group["feature"].min()) if pd.api.types.is_numeric_dtype(group["feature"]) else None,
                "feature_to": float(group["feature"].max()) if pd.api.types.is_numeric_dtype(group["feature"]) else None,
                "count": int(len(group)),
                "mean_bps": float(group["forward"].mean() * BPS),
                "hit_rate": float((group["forward"] > 0).mean()),
                "t_stat": _t_stat(sampled),
            }
        )
    return pd.DataFrame(rows)


def information_coefficient(feature: np.ndarray, forward: np.ndarray) -> float:
    """Spearman rank correlation between a feature and the forward return (0 = no information)."""
    frame = pd.DataFrame({"feature": feature, "forward": forward}).dropna()
    if len(frame) < 10:
        return float("nan")
    return float(frame["feature"].rank().corr(frame["forward"].rank()))


def ic_by_year(feature: np.ndarray, forward: np.ndarray, timestamps: pd.DatetimeIndex) -> pd.Series:
    """Information coefficient per calendar year: a real effect keeps its sign year after year."""
    frame = pd.DataFrame({"feature": feature, "forward": forward}, index=timestamps).dropna()
    return frame.groupby(frame.index.year).apply(lambda group: information_coefficient(group["feature"].to_numpy(), group["forward"].to_numpy()))


def spread_by_year(feature: np.ndarray, forward: np.ndarray, timestamps: pd.DatetimeIndex, *, buckets: int = 5) -> pd.Series:
    """Top-bucket minus bottom-bucket mean forward return (bps) per year, with bucket edges set on the whole sample."""
    frame = pd.DataFrame({"feature": feature, "forward": forward}, index=timestamps).dropna()
    frame["bucket"] = pd.qcut(frame["feature"], buckets, labels=False, duplicates="drop")
    top, bottom = frame["bucket"].max(), frame["bucket"].min()
    return frame.groupby(frame.index.year).apply(
        lambda group: (group.loc[group["bucket"] == top, "forward"].mean() - group.loc[group["bucket"] == bottom, "forward"].mean()) * BPS
    )


def seasonality(frame: pd.DataFrame, *, by: str = "hour", horizon: int = 1) -> pd.DataFrame:
    """Mean forward return (bps) by UTC hour of day or weekday, per year and overall.

    A time-of-day effect worth trading shows the same sign in most years,
    not just a large average driven by one year.
    """
    forward = forward_return(frame["close"], horizon)
    key = frame.index.hour if by == "hour" else frame.index.dayofweek
    data = pd.DataFrame({"key": key, "year": frame.index.year, "forward": forward}, index=frame.index).dropna()
    table = data.pivot_table(index="key", columns="year", values="forward", aggfunc="mean") * BPS
    table["all"] = data.groupby("key")["forward"].mean() * BPS
    table["t_stat"] = data.groupby("key")["forward"].apply(lambda values: _t_stat(values.to_numpy()[::horizon]))
    table["years_positive"] = (table.drop(columns=["all", "t_stat"]) > 0).sum(axis=1)
    return table
