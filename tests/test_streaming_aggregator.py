"""Tests for the streaming OHLCV aggregator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.storage.market_store import MarketStore
from src.storage.streaming_aggregator import StreamingAggregator


def test_streaming_aggregator_updates_and_flushes_bars(tmp_path: Path) -> None:
    """The aggregator should build a bar from incoming snapshots and flush it later."""
    store = MarketStore(database_path=tmp_path / "stream.db")
    aggregator = StreamingAggregator(store=store, interval_seconds=60)

    aggregator.update("firi", "BTC/NOK", datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc), 100.0, 101.0, 100.5, 1.0)
    aggregator.update("firi", "BTC/NOK", datetime(2024, 1, 1, 12, 0, 30, tzinfo=timezone.utc), 99.0, 102.0, 101.0, 2.0)

    completed = aggregator.flush()

    assert len(completed) == 1
    assert completed[0].open == 100.5
    assert completed[0].high == 102.0
    assert completed[0].low == 99.0
    assert completed[0].close == 101.0
    assert completed[0].volume == 3.0
