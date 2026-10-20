"""The data catalog: every load is a fingerprinted, versioned snapshot (plan 1.3).

`DataCatalog.get(adapter, symbol, interval)` returns the latest snapshot of that series, fetching it once. A refresh
fetches again and stores a new version only if the content changed. Each snapshot is a CSV (portable, diffable, no
extra dependency) plus a line in the series' `manifest.jsonl`:
id, content fingerprint, adapter and adapter version, fetch time, rows, first and last time.

A study records the snapshot ids it used (`Snapshot.id`), and `get(..., snapshot=<id>)` loads exactly that version
again, so a result can be reproduced on the same bytes even after the source revises history.

The frozen holdout: data on or after `frozen_from` is cut from every load unless the catalog is unlocked with a
reason (`unlock("H15 final look")`), and each unlocked load is logged in `unlocks.jsonl` beside the data.

Layout under the catalog root (default: the `QUANTLAB_DATA` environment variable, else `./quantlab_data`):

    <root>/<adapter>/<symbol>/<interval>/<fingerprint>.csv
    <root>/<adapter>/<symbol>/<interval>/manifest.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.data import adapters as registry

DEFAULT_FROZEN_FROM = "2026-01-01"


def fingerprint(frame: pd.DataFrame) -> str:
    """A content hash of a frame (index and values, at 10 significant digits so float noise does not matter)."""
    text = frame.to_csv(float_format="%.10g", date_format="%Y-%m-%dT%H:%M:%S%z")
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Snapshot:
    """One stored version of one series."""

    adapter: str
    symbol: str
    interval: str
    fingerprint: str
    adapter_version: str
    fetched_at: str
    rows: int
    first: str
    last: str

    @property
    def id(self) -> str:
        """"adapter:symbol:interval@fingerprint", what a study records."""
        return f"{self.adapter}:{self.symbol}:{self.interval}@{self.fingerprint}"


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._=-]", "_", symbol)


class DataCatalog:
    """Versioned local copies of every series the lab loads."""

    def __init__(self, root: str | Path | None = None, *, frozen_from: str | None = DEFAULT_FROZEN_FROM) -> None:
        self.root = Path(root or os.environ.get("QUANTLAB_DATA", "quantlab_data"))
        self.frozen_from = pd.Timestamp(frozen_from) if frozen_from else None
        self._unlock_reason: str | None = None

    # --- the frozen holdout --------------------------------------------------------------------------------------------

    def unlock(self, reason: str) -> None:
        """Load data inside the frozen holdout from now on, for `reason` (logged with every such load)."""
        if not reason.strip():
            raise ValueError("say what the holdout is opened for")
        self._unlock_reason = reason

    def lock(self) -> None:
        """Cut the holdout again."""
        self._unlock_reason = None

    def _cut(self, frame: pd.DataFrame, snapshot: Snapshot) -> pd.DataFrame:
        if self.frozen_from is None or frame.empty:
            return frame
        times = frame.index.tz_localize(None) if getattr(frame.index, "tz", None) is not None else frame.index
        if self._unlock_reason:
            if (times >= self.frozen_from).any():
                self._append(self.root / "unlocks.jsonl", {"at": _now(), "reason": self._unlock_reason, "snapshot": snapshot.id})
            return frame
        return frame[times < self.frozen_from]

    # --- storage -------------------------------------------------------------------------------------------------------

    def _folder(self, adapter: str, symbol: str, interval: str) -> Path:
        return self.root / adapter / _safe(symbol) / interval

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def snapshots(self, adapter: str, symbol: str, interval: str) -> list[Snapshot]:
        """Every stored version of a series, oldest first."""
        manifest = self._folder(adapter, symbol, interval) / "manifest.jsonl"
        if not manifest.exists():
            return []
        return [Snapshot(**json.loads(line)) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

    def store(self, adapter: str, symbol: str, interval: str, frame: pd.DataFrame, *, adapter_version: str = "") -> Snapshot:
        """Keep `frame` as a snapshot (a new version only if its content is new); returns its snapshot."""
        digest = fingerprint(frame)
        existing = [item for item in self.snapshots(adapter, symbol, interval) if item.fingerprint == digest]
        if existing:
            return existing[-1]
        folder = self._folder(adapter, symbol, interval)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f"{digest}.csv.tmp"
        frame.to_csv(temporary)
        temporary.replace(folder / f"{digest}.csv")
        snapshot = Snapshot(adapter=adapter, symbol=symbol, interval=interval, fingerprint=digest, adapter_version=adapter_version, fetched_at=_now(),
                            rows=len(frame), first=str(frame.index[0]) if len(frame) else "", last=str(frame.index[-1]) if len(frame) else "")
        self._append(folder / "manifest.jsonl", asdict(snapshot))
        return snapshot

    def read(self, snapshot: Snapshot) -> pd.DataFrame:
        """A stored snapshot's frame, exactly as stored (before the holdout cut)."""
        path = self._folder(snapshot.adapter, snapshot.symbol, snapshot.interval) / f"{snapshot.fingerprint}.csv"
        frame = pd.read_csv(path, index_col=0)
        index = pd.to_datetime(frame.index, utc=snapshot.interval != "1d" and not snapshot.interval.endswith("M"))
        frame.index = pd.DatetimeIndex(index)
        frame.index.name = "time"
        for column in frame.columns:
            if column == "available_at":
                frame[column] = pd.to_datetime(frame[column])
        return frame

    # --- loading -------------------------------------------------------------------------------------------------------

    def get(self, adapter: str, symbol: str, interval: str = "1d", *, refresh: bool = False, snapshot: str | None = None) -> tuple[pd.DataFrame, Snapshot]:
        """(frame without the frozen holdout, its snapshot): the latest version, fetched once; or the version named."""
        versions = self.snapshots(adapter, symbol, interval)
        if snapshot is not None:
            wanted = snapshot.split("@")[-1]
            chosen = next((item for item in versions if item.fingerprint == wanted), None)
            if chosen is None:
                raise KeyError(f"no snapshot {snapshot} of {adapter}:{symbol}:{interval}")
        elif versions and not refresh:
            chosen = versions[-1]
        else:
            source = registry.get(adapter)
            chosen = self.store(adapter, symbol, interval, source.fetch(symbol, interval=interval), adapter_version=getattr(source, "version", ""))
        return self._cut(self.read(chosen), chosen), chosen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
