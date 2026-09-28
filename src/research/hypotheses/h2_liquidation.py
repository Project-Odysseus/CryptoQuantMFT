"""H2, liquidation overshoot (PROXY): forced-deleveraging-shaped hours, their exhaustion entry, and matched baselines.

Definitions follow research/prereg/H2.txt. There is no liquidation history, so an event is an hour with an extreme
return, an open-interest drop and a volume spike (Binance perp, BTC/ETH/SOL). Everything at bar t is computed from
data available at t's close: trailing windows exclude t itself where the prereg says "before t", and open interest is
as-of the bar close with the data layer's 5-minute publication lag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.research import pit

WINDOW = 720  # 30 days of hourly bars
MAX_WAIT = 6
MERGE_HOURS = 48


@dataclass(frozen=True, slots=True)
class EventRule:
    """Thresholds of an event definition (PRIMARY and LOOSE are pre-registered)."""

    name: str
    k_sigma: float
    oi_drop: float
    volume_multiple: float


PRIMARY = EventRule("primary", 3.0, 0.03, 5.0)
LOOSE = EventRule("loose", 2.5, 0.02, 3.0)


def hourly_frame(coin: str, *, end: Any = None) -> pd.DataFrame:
    """Hourly bars with the event inputs, indexed by open time; row t's inputs are known at t's close."""
    bars = pit.load_bars(coin, "1h", end=end).copy()
    closes = pd.DatetimeIndex(bars["available_at"])
    consecutive = bars.index.to_series().diff().eq(pd.Timedelta(hours=1)).to_numpy()
    bars["ret"] = np.where(consecutive, np.log(bars["close"] / bars["close"].shift(1)), np.nan)
    bars["sigma"] = bars["ret"].rolling(WINDOW, min_periods=WINDOW // 2).std().shift(1)
    bars["volume_median"] = bars["volume"].rolling(WINDOW, min_periods=WINDOW // 2).median().shift(1)
    oi = pit.load_open_interest(coin, "binance", end=end)
    at_close = pit.asof(oi, closes, ["oi"])["oi"].to_numpy()
    seen = pit.asof(oi.assign(seen=oi["available_at"]), closes, ["seen"])["seen"]
    fresh = (closes - pd.DatetimeIndex(seen)) <= pd.Timedelta(minutes=30)
    bars["oi"] = np.where(fresh, at_close, np.nan)
    bars["doi"] = np.where(consecutive, bars["oi"] / bars["oi"].shift(1) - 1.0, np.nan)
    bars["rv24"] = np.sqrt((bars["ret"] ** 2).rolling(24, min_periods=20).mean() * 24 * 365)
    return bars


def detect_events(frame: pd.DataFrame, rule: EventRule) -> pd.DataFrame:
    """Events (position t, direction) with cascades merged: one within 48h after the previous one of its direction joins it."""
    extreme_volume = frame["volume"] > rule.volume_multiple * frame["volume_median"]
    oi_drop = frame["doi"] < -rule.oi_drop
    down = frame["ret"] < -rule.k_sigma * frame["sigma"]
    up = frame["ret"] > rule.k_sigma * frame["sigma"]
    rows = []
    for direction, mask in ((1, down & oi_drop & extreme_volume), (-1, up & oi_drop & extreme_volume)):
        last = None
        for position in np.flatnonzero(mask.to_numpy()):
            if last is not None and position - last <= MERGE_HOURS:
                last = position
                continue
            rows.append({"t": int(position), "direction": direction})
            last = position
    return pd.DataFrame(rows, columns=["t", "direction"]).sort_values("t").reset_index(drop=True)


def entries(frame: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Exhaustion entry (open of the bar after the first quiet bar within 6h, else t+7) and the cascade stop."""
    volume, median = frame["volume"].to_numpy(), frame["volume_median"].to_numpy()
    low, high = frame["low"].to_numpy(), frame["high"].to_numpy()
    rows = []
    for event in events.itertuples():
        quiet = next((k for k in range(event.t + 1, min(event.t + MAX_WAIT, len(frame) - 1) + 1) if volume[k] < 2.0 * median[k]), None)
        last = quiet if quiet is not None else min(event.t + MAX_WAIT, len(frame) - 1)
        stop = float(np.min(low[event.t: last + 1])) if event.direction > 0 else float(np.max(high[event.t: last + 1]))
        rows.append({"t": event.t, "direction": event.direction, "entry": last + 1, "stop": stop, "waited": last - event.t, "exhausted": quiet is not None})
    return pd.DataFrame(rows)


def forward_log_returns(frame: pd.DataFrame, hold: int) -> np.ndarray:
    """ln(open[p + hold] / open[p]) at every position (NaN where it runs past the data)."""
    opens = frame["open"].to_numpy(dtype=float)
    out = np.full(len(opens), np.nan)
    out[:-hold] = np.log(opens[hold:] / opens[:-hold])
    return out


def exclusion_mask(length: int, events: pd.DataFrame, *, hours: int = MERGE_HOURS) -> np.ndarray:
    """Positions within `hours` of any event bar or entry: not usable as controls."""
    mask = np.zeros(length, dtype=bool)
    for event in events.itertuples():
        for anchor in (event.t, getattr(event, "entry", event.t)):
            mask[max(0, anchor - hours): min(length, anchor + hours + 1)] = True
    return mask


def news_driven(frame: pd.DataFrame, events: pd.DataFrame, releases: pd.Series) -> np.ndarray:
    """True where the event bar's close is within [-2h, +4h] of a scheduled release time."""
    closes = pit.ns(frame["available_at"])
    times = np.sort(pit.ns(releases))
    out = []
    for t in events["t"]:
        close = closes[t]
        i = np.searchsorted(times, close - pd.Timedelta(hours=4).value, side="left")
        out.append(bool(i < len(times) and times[i] <= close + pd.Timedelta(hours=2).value))
    return np.array(out, dtype=bool)
