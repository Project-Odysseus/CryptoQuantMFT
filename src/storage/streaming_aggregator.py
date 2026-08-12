"""A lightweight streaming OHLCV aggregator for live market snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.storage.bar_aggregator import OHLCVBar
from src.storage.market_store import MarketStore


@dataclass(slots=True)
class StreamingBar:
    """The current in-progress bar for one symbol and interval."""

    exchange: str
    symbol: str
    interval_seconds: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    start_time: datetime
    end_time: datetime


@dataclass(slots=True)
class StreamingAggregator:
    """Maintain rolling OHLCV bars from incoming market snapshots."""

    store: MarketStore
    interval_seconds: int = 60
    bars: dict[tuple[str, str], StreamingBar] = field(default_factory=dict)

    def update(self, exchange: str, symbol: str, timestamp: datetime, bid: float, ask: float, last: float, volume: float) -> OHLCVBar | None:
        """Update the in-progress bar for the given symbol and interval."""
        if self.interval_seconds not in {1, 60, 300, 1800, 3600, 86400}:
            raise ValueError(f"Unsupported interval: {self.interval_seconds}")

        bucket_start = self._bucket_start(timestamp)
        key = (exchange, symbol)
        current = self.bars.get(key)

        if current is None or current.start_time != bucket_start:
            if current is not None:
                self._finalize_bar(current)
            current = StreamingBar(
                exchange=exchange,
                symbol=symbol,
                interval_seconds=self.interval_seconds,
                open=last,
                high=last,
                low=last,
                close=last,
                volume=0.0,
                start_time=bucket_start,
                end_time=bucket_start + self._interval_delta(),
            )
            self.bars[key] = current

        current.high = max(current.high, max(last, ask))
        current.low = min(current.low, min(last, bid))
        current.close = last
        current.volume += volume
        return None

    def flush(self) -> list[OHLCVBar]:
        """Finalize all currently open bars and return the completed bars."""
        completed: list[OHLCVBar] = []
        for key in list(self.bars):
            completed.append(self._finalize_bar(self.bars.pop(key)))
        return completed

    def _finalize_bar(self, bar: StreamingBar) -> OHLCVBar:
        completed = OHLCVBar(
            exchange=bar.exchange,
            symbol=bar.symbol,
            interval_seconds=bar.interval_seconds,
            timestamp=bar.start_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        self.store.save_tick(
            type("StoredTick", (), {
                "exchange": bar.exchange,
                "symbol": bar.symbol,
                "timestamp": completed.timestamp,
                "bid": bar.low,
                "ask": bar.high,
                "last": bar.close,
                "volume": bar.volume,
                "raw": {"source": "streaming_aggregator"},
            })()
        )
        return completed

    def _bucket_start(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        epoch = int(timestamp.timestamp())
        bucket = (epoch // self.interval_seconds) * self.interval_seconds
        return datetime.fromtimestamp(bucket, tz=timezone.utc)

    def _interval_delta(self) -> timedelta:
        return timedelta(seconds=self.interval_seconds)
