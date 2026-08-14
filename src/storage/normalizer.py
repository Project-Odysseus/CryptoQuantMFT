"""Helpers for normalizing multi-exchange market data into aligned time buckets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.storage.bar_aggregator import OHLCVBar


@dataclass(slots=True)
class NormalizedBar:
    """A bar normalized to a common symbol and aligned timestamp."""

    exchange: str
    symbol: str
    interval_seconds: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    raw: dict[str, Any] | None = None


class DataNormalizer:
    """Align OHLCV bars from multiple exchanges into a common representation."""

    def __init__(self, interval_seconds: int) -> None:
        """Initialize the object with its runtime state."""
        self.interval_seconds = interval_seconds

    def normalize(self, bars: list[OHLCVBar]) -> list[NormalizedBar]:
        """Normalize a list of bars by aligning timestamps and resolving common symbols."""
        normalized: list[NormalizedBar] = []
        for bar in bars:
            timestamp = self._align_timestamp(bar.timestamp)
            symbol = self._normalize_symbol(bar.symbol)
            normalized.append(
                NormalizedBar(
                    exchange=bar.exchange,
                    symbol=symbol,
                    interval_seconds=self.interval_seconds,
                    timestamp=timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    raw={"source": bar.exchange},
                )
            )
        return normalized

    def _align_timestamp(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        epoch = int(timestamp.astimezone(timezone.utc).timestamp())
        aligned_epoch = (epoch // self.interval_seconds) * self.interval_seconds
        return datetime.fromtimestamp(aligned_epoch, tz=timezone.utc)

    def _normalize_symbol(self, symbol: str) -> str:
        return symbol.upper().replace("/", "")
