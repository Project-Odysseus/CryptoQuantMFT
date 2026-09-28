"""Shared pieces of the hypothesis studies: daily bars, cost series, and markdown tables for the reports."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.research.costs import VenueCosts, vol_ratio


def daily_bars(hourly: pd.DataFrame) -> pd.DataFrame:
    """UTC daily candles from hourly ones, indexed by the day's 00:00 (the decision time T that opens it)."""
    daily = hourly[["open", "high", "low", "close", "volume"]].resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return daily.dropna(subset=["open"])


def resample_bars(hourly: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Candles of `rule` (e.g. "4h", "1D") from hourly ones, indexed by open time; incomplete last bar dropped."""
    grouped = hourly[["open", "high", "low", "close", "volume"]].resample(rule)
    bars = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = grouped["open"].count()
    full = counts.max()
    return bars[counts == full].dropna(subset=["open"])


def cost_at(hourly: pd.DataFrame, times: pd.DatetimeIndex, venue: VenueCosts, coin: str, *, multiplier: float = 1.0,
            spread_stress: float = 1.0) -> np.ndarray:
    """Per-side cost at each time from the volatility ratio known then (24h realized vol vs its 1-year median)."""
    ratio = vol_ratio(hourly["close"], window=24, normal_window=24 * 365)
    ratio.index = pd.DatetimeIndex(hourly["available_at"]) if "available_at" in hourly else hourly.index + pd.Timedelta(hours=1)
    known = ratio.reindex(ratio.index.union(times)).ffill().reindex(times).fillna(1.0)
    return np.asarray(venue.per_side(known.to_numpy(), coin=coin, multiplier=multiplier, spread_stress=spread_stress), dtype=float)


def md_table(frame: pd.DataFrame, *, digits: int = 3, index: bool = True) -> str:
    """A GitHub markdown table (no tabulate dependency)."""
    data = frame.reset_index() if index else frame

    def fmt(value: Any) -> str:
        if isinstance(value, (float, np.floating)):
            return "" if not np.isfinite(value) else f"{value:.{digits}f}"
        return str(value)

    header = "| " + " | ".join(str(c) for c in data.columns) + " |"
    rule = "| " + " | ".join("---" for _ in data.columns) + " |"
    rows = ["| " + " | ".join(fmt(v) for v in row) + " |" for row in data.itertuples(index=False)]
    return "\n".join([header, rule, *rows])
