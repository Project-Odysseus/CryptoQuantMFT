"""OHLCV bar aggregation over persisted market snapshots."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.storage.market_store import MarketStore


@dataclass(slots=True)
class OHLCVBar:
    """Single time-bucket OHLCV bar."""

    exchange: str
    symbol: str
    interval_seconds: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarAggregator:
    """Aggregate persisted market ticks into OHLCV bars for common intervals."""

    SUPPORTED_INTERVALS = {1, 60, 300, 1800, 3600, 86400}

    def __init__(self, store: MarketStore, interval_seconds: int = 60) -> None:
        if interval_seconds not in self.SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported interval: {interval_seconds}")
        self.store = store
        self.interval_seconds = interval_seconds

    def build_bars(self, limit: int | None = None) -> list[OHLCVBar]:
        """Build OHLCV bars from recent persisted ticks."""
        rows = self.store.list_ticks(limit=limit or 10000)
        if not rows:
            return []

        buckets: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            timestamp = datetime.fromisoformat(row["timestamp"])
            bucket_key = self._bucket_key(timestamp)
            buckets[(row["exchange"], row["symbol"], bucket_key)].append(row)

        bars: list[OHLCVBar] = []
        for (exchange, symbol, bucket_key), bucket_rows in buckets.items():
            bucket_rows.sort(key=lambda item: item["timestamp"])
            first = bucket_rows[0]
            last = bucket_rows[-1]
            bar = OHLCVBar(
                exchange=exchange,
                symbol=symbol,
                interval_seconds=self.interval_seconds,
                timestamp=datetime.fromtimestamp(bucket_key, tz=timezone.utc),
                open=float(first["last"] or first["bid"] or 0.0),
                high=max(float(row["last"] or row["ask"] or 0.0) for row in bucket_rows),
                low=min(float(row["bid"] or row["last"] or 0.0) for row in bucket_rows),
                close=float(last["last"] or last["bid"] or 0.0),
                volume=sum(float(row["volume"] or 0.0) for row in bucket_rows),
            )
            bars.append(bar)

        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    def _bucket_key(self, timestamp: datetime) -> int:
        epoch = int(timestamp.replace(tzinfo=timezone.utc).timestamp())
        return (epoch // self.interval_seconds) * self.interval_seconds
