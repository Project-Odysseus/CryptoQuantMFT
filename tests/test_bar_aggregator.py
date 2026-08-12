"""Tests for the simple OHLCV bar aggregator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.data.exchanges import MarketTick
from src.storage.bar_aggregator import BarAggregator, build_multi_interval_bars
from src.storage.market_store import MarketStore


def test_bar_aggregator_supports_common_intervals() -> None:
    """The aggregator should accept the common interval sizes we plan to use."""
    assert BarAggregator.SUPPORTED_INTERVALS == {1, 60, 300, 1800, 3600, 86400}


def test_bar_aggregator_builds_one_minute_bars(tmp_path: Path) -> None:
    """The aggregator should merge persisted ticks into a single minute bar."""
    store = MarketStore(database_path=tmp_path / "bars.db")
    aggregator = BarAggregator(store=store, interval_seconds=60)

    for index, price in enumerate((100.0, 101.0, 99.5), start=1):
        tick = MarketTick(
            exchange="firi",
            symbol="BTC/NOK",
            timestamp=datetime(2024, 1, 1, 12, 0, index, tzinfo=timezone.utc),
            bid=price - 0.5,
            ask=price + 0.5,
            last=price,
            volume=1.0,
            raw={"source": "test"},
        )
        store.save_tick(tick)

    bars = aggregator.build_bars(limit=10)

    assert len(bars) == 1
    assert bars[0].open == 100.0
    assert bars[0].high == 101.0
    assert bars[0].low == 99.0
    assert bars[0].close == 99.5
    assert bars[0].volume == 3.0


def test_build_multi_interval_bars_returns_all_supported_intervals(tmp_path: Path) -> None:
    """The multi-interval helper should return bars for each supported interval size."""
    store = MarketStore(database_path=tmp_path / "multi.db")
    tick = MarketTick(
        exchange="firi",
        symbol="BTC/NOK",
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        bid=100.0,
        ask=101.0,
        last=100.5,
        volume=1.0,
        raw={"source": "test"},
    )
    store.save_tick(tick)

    multi_bars = build_multi_interval_bars(store=store, limit=10)

    assert set(multi_bars) == {1, 60, 300, 1800, 3600, 86400}
    assert len(multi_bars[60]) == 1
    assert len(multi_bars[86400]) == 1
