"""Tests for the multi-exchange bar normalizer."""

from __future__ import annotations

from datetime import datetime, timezone

from src.storage.bar_aggregator import OHLCVBar
from src.storage.normalizer import DataNormalizer


def test_normalizer_aligns_timestamps_and_symbols() -> None:
    """The normalizer should align bars to common buckets and symbols."""
    normalizer = DataNormalizer(interval_seconds=60)
    bar = OHLCVBar(
        exchange="firi",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 12, 0, 34, tzinfo=timezone.utc),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=2.0,
    )

    normalized = normalizer.normalize([bar])

    assert len(normalized) == 1
    assert normalized[0].timestamp == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert normalized[0].symbol == "BTCNOK"
    assert normalized[0].exchange == "firi"
