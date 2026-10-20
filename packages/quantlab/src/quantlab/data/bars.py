"""The bar schema: one frame per instrument and interval, indexed by when each bar's data is final (plan 1.7).

- Columns: open, high, low, close, volume (volume may be NaN where a source has none).
- Index: a DatetimeIndex named "time". Daily bars use the exchange-local session date (naive, midnight); intraday bars
  use their close time in UTC (tz-aware). Sorted, unique.

Sources that stamp bars at their open (Kraken, Binance) are converted with `from_open_stamped`. `to_daily` builds
session-dated daily bars from intraday bars with a session cut-off in the market's timezone, so a study can move
between 4h and daily by changing one setting.
"""

from __future__ import annotations

import pandas as pd

from quantlab.conventions import INTERVAL_SECONDS

FIELDS = ("open", "high", "low", "close", "volume")


class BarError(ValueError):
    """A bar frame that breaks the schema."""


def validate_bars(frame: pd.DataFrame, *, interval: str) -> pd.DataFrame:
    """The frame checked against the schema (raises BarError); returns it with columns in schema order."""
    missing = [name for name in FIELDS if name not in frame.columns]
    if missing:
        raise BarError(f"missing columns {missing}")
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        raise BarError("the index must be a DatetimeIndex")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise BarError("the index must be sorted and unique")
    if interval == "1d":
        if index.tz is not None or (index != index.normalize()).any():
            raise BarError("daily bars are indexed by naive session dates (midnight)")
    elif index.tz is None:
        raise BarError("intraday bars are indexed by their close time in UTC (tz-aware)")
    out = frame.loc[:, list(FIELDS)].astype(float)
    out.index.name = "time"
    return out


def from_open_stamped(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Bars stamped at their open re-indexed by their close (open + interval). Daily bars: the open's UTC date."""
    out = frame.copy()
    if interval == "1d":
        index = pd.DatetimeIndex(out.index)
        out.index = (index.tz_convert("UTC").tz_localize(None) if index.tz is not None else index).normalize()
    else:
        index = pd.DatetimeIndex(out.index)
        out.index = (index if index.tz is not None else index.tz_localize("UTC")) + pd.Timedelta(seconds=INTERVAL_SECONDS[interval])
    out.index.name = "time"
    return out


def to_daily(intraday: pd.DataFrame, *, timezone: str = "UTC", session_close: str = "00:00") -> pd.DataFrame:
    """Session-dated daily bars from intraday bars (close-stamped, UTC).

    A bar belongs to the session whose close it ends at or before: with `session_close` 16:00 New York, a bar closing
    at 16:00 local is the last bar of that day, one closing at 17:00 starts the next. "00:00" means calendar days.
    """
    if intraday.empty:
        return intraday
    local = intraday.index.tz_convert(timezone)
    hour, minute = (int(part) for part in session_close.split(":"))
    cutoff = pd.Timedelta(hours=hour, minutes=minute)
    if cutoff == pd.Timedelta(0):
        session = (local - pd.Timedelta(microseconds=1)).normalize()  # a bar closing at midnight belongs to the day before
    else:
        shifted = local - cutoff - pd.Timedelta(microseconds=1)
        session = shifted.normalize() + pd.Timedelta(days=1)
    session = pd.DatetimeIndex(session).tz_localize(None)
    grouped = intraday.groupby(session)
    daily = pd.DataFrame({"open": grouped["open"].first(), "high": grouped["high"].max(), "low": grouped["low"].min(),
                          "close": grouped["close"].last(), "volume": grouped["volume"].sum(min_count=1)})
    daily.index.name = "time"
    return daily


def available_at(index: pd.DatetimeIndex, *, interval: str, timezone: str = "UTC", session_close: str = "16:00") -> pd.DatetimeIndex:
    """When each bar's data could first be known, in UTC: the session close for daily bars, the bar close otherwise."""
    if interval != "1d":
        return index
    hour, minute = (int(part) for part in session_close.split(":"))
    local = (index + pd.Timedelta(hours=hour, minutes=minute)).tz_localize(timezone, nonexistent="shift_forward", ambiguous="NaT")
    return local.tz_convert("UTC")
