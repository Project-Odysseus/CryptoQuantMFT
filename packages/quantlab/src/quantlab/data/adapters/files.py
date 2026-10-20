"""Data you bring: CSV or Parquet files on disk (plan 1.2's "bring your own").

This is how a firm's own data enters the lab without the lab knowing the vendor: export bars to files (one per
instrument and interval) and point the adapter at the folder. The symbol is the file's path relative to the root,
without extension ("bbg/NQ1_daily"). Column names are matched case-insensitively and common aliases are accepted
(date/timestamp for the index; px_last/settle/last for the close). Daily files are indexed by session date; intraday
files must say whether their times are bar closes (default) or opens.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from quantlab.data.bars import from_open_stamped

ALIASES = {"close": ("close", "px_last", "settle", "settlement", "last", "price", "value"), "open": ("open", "px_open"), "high": ("high", "px_high"),
           "low": ("low", "px_low"), "volume": ("volume", "px_volume", "vol")}
TIME_COLUMNS = ("time", "date", "timestamp", "datetime", "day")


class FileAdapter:
    """CSV/Parquet bars under a root folder (`QUANTLAB_FILES`, or the root given)."""

    name, version = "file", "1"

    def __init__(self, root: str | Path | None = None, *, stamped_at: str = "close") -> None:
        self.root = Path(root or os.environ.get("QUANTLAB_FILES", "."))
        self.stamped_at = stamped_at

    def _path(self, symbol: str) -> Path:
        for suffix in (".parquet", ".csv"):
            path = self.root / f"{symbol}{suffix}"
            if path.exists():
                return path
        raise FileNotFoundError(f"no {symbol}.csv or {symbol}.parquet under {self.root}")

    def fetch(self, symbol: str, *, interval: str = "1d", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        path = self._path(symbol)
        raw = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
        columns = {name.lower(): name for name in raw.columns}
        time_column = next((columns[name] for name in TIME_COLUMNS if name in columns), None)
        if time_column is None:
            raise ValueError(f"{path}: no time column (one of {TIME_COLUMNS})")
        frame = pd.DataFrame(index=pd.DatetimeIndex(pd.to_datetime(raw[time_column], utc=interval != "1d")))
        for field, aliases in ALIASES.items():
            source = next((columns[alias] for alias in aliases if alias in columns), None)
            frame[field] = pd.to_numeric(raw[source], errors="coerce").to_numpy() if source else float("nan")
        if frame["close"].isna().all():
            raise ValueError(f"{path}: no close column (one of {ALIASES['close']})")
        for field in ("open", "high", "low"):  # a close-only file: the bar has no range
            frame[field] = frame[field].fillna(frame["close"])
        if interval == "1d":
            frame.index = frame.index.normalize()
        elif self.stamped_at == "open":
            frame = from_open_stamped(frame, interval)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        frame.index.name = "time"
        return frame.loc[start:end] if (start or end) else frame
