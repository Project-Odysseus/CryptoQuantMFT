"""Panels: several instruments' bars aligned on one calendar, plus point-in-time universes and macro inputs.

`load_panel(master, ids, catalog=...)` is how a study gets data:

1. for each instrument, its source for the interval goes through the catalog (a versioned snapshot);
2. a data card is built, and the series is cut at its `usable_from`;
3. all series are aligned on the union of their dates. A market without a bar on a date (a holiday) has its last
   close carried forward with a zero-range bar and `stale` set, so a carried price is never mistaken for a trade;
4. the panel records every snapshot id and data card, for the study's report.

`universe` gives a point-in-time tradable set (enough history, and liquid enough where volume exists).
`asof_daily` turns a lower-frequency series with `available_at` (OECD rates) into a daily series known at each date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

from quantlab.calendars import get_calendar
from quantlab.data.bars import FIELDS, validate_bars
from quantlab.data.catalog import DataCatalog
from quantlab.data.instruments import InstrumentMaster
from quantlab.data.quality import DataCard, data_card


@dataclass
class Panel:
    """Aligned bars of several instruments: one frame (dates x instruments) per field."""

    interval: str
    fields: dict[str, pd.DataFrame]
    stale: pd.DataFrame
    snapshots: dict[str, str] = field(default_factory=dict)
    cards: dict[str, DataCard] = field(default_factory=dict)

    @property
    def instruments(self) -> list[str]:
        return list(self.fields["close"].columns)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.fields["close"].index

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.fields[name]

    def bars(self, instrument: str) -> pd.DataFrame:
        """One instrument's aligned bars."""
        return pd.DataFrame({name: frame[instrument] for name, frame in self.fields.items()})

    def window(self, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None) -> "Panel":
        """The same panel on [start, end]."""
        cut = lambda frame: frame.loc[start:end]  # noqa: E731
        return Panel(self.interval, {name: cut(frame) for name, frame in self.fields.items()}, cut(self.stale), dict(self.snapshots), dict(self.cards))


def align(bars: dict[str, pd.DataFrame], *, interval: str = "1d") -> Panel:
    """Align bar frames on the union of their dates; carry closes over missing dates as stale zero-range bars."""
    index = pd.DatetimeIndex(sorted(set().union(*(frame.index for frame in bars.values())))) if bars else pd.DatetimeIndex([])
    fields_out: dict[str, dict[str, pd.Series]] = {name: {} for name in FIELDS}
    stale: dict[str, pd.Series] = {}
    for instrument, frame in bars.items():
        reindexed = frame.reindex(index)
        alive = (index >= frame.index[0]) & (index <= frame.index[-1]) if len(frame) else np.zeros(len(index), dtype=bool)
        missing = reindexed["close"].isna().to_numpy() & alive
        close = reindexed["close"].ffill().where(alive)
        for name in ("open", "high", "low"):
            fields_out[name][instrument] = reindexed[name].where(~missing, close)
        fields_out["close"][instrument] = close
        fields_out["volume"][instrument] = reindexed["volume"].where(~missing, 0.0)
        stale[instrument] = pd.Series(missing, index=index)
    return Panel(interval, {name: pd.DataFrame(columns, index=index) for name, columns in fields_out.items()}, pd.DataFrame(stale, index=index))


def load_panel(master: InstrumentMaster, ids: Iterable[str], *, catalog: DataCatalog | None = None, interval: str = "1d",
               start: str | None = None, end: str | None = None, refresh: bool = False, cut_close_only: bool = True) -> Panel:
    """Load, check and align instruments (see the module docstring)."""
    catalog = catalog or DataCatalog()
    frames, snapshots, cards = {}, {}, {}
    for instrument_id in ids:
        instrument = master[instrument_id]
        adapter, symbol = instrument.source(interval)
        raw, snapshot = catalog.get(adapter, symbol, interval, refresh=refresh)
        bars = validate_bars(raw, interval=interval)
        card = data_card(bars, name=instrument_id, calendar=get_calendar(instrument.calendar), interval=interval)
        if cut_close_only:
            bars = bars[bars.index >= card.usable_from]
        frames[instrument_id] = bars.loc[start:end]
        if frames[instrument_id].empty:
            raise ValueError(f"{instrument_id}: no usable bars between {start} and {end} (data card: usable from {card.usable_from:%Y-%m-%d}, "
                             f"{card.rows} bars {card.first} to {card.last})")
        snapshots[instrument_id], cards[instrument_id] = snapshot.id, card
    panel = align(frames, interval=interval)
    panel.snapshots, panel.cards = snapshots, cards
    return panel


def universe(panel: Panel, *, min_history: int = 252, min_volume: float | None = None, volume_window: int = 60) -> pd.DataFrame:
    """Point-in-time membership (dates x instruments): listed for `min_history` bars, not stale, and (if given) with a
    trailing median volume of at least `min_volume`, known at each date."""
    close = panel["close"]
    listed = close.notna().cumsum() >= min_history
    member = listed & ~panel.stale
    if min_volume is not None:
        volume = panel["volume"].where(~panel.stale)
        liquid = volume.rolling(volume_window, min_periods=volume_window // 2).median() >= min_volume
        member &= liquid
    return member.fillna(False)


def asof_daily(series: pd.DataFrame, index: pd.DatetimeIndex, *, value: str = "value", available: str = "available_at") -> pd.Series:
    """A daily series of `value` as known on each date of `index` (by `available`; the row's own time if absent)."""
    known_from = pd.DatetimeIndex(series[available]) if available in series else series.index
    ordered = pd.Series(series[value].to_numpy(), index=known_from).sort_index()
    ordered = ordered[~ordered.index.duplicated(keep="last")]
    return ordered.reindex(ordered.index.union(index)).ffill().reindex(index)
