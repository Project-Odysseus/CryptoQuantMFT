"""Tests for the efficiency-ratio-based regime classifier."""

from __future__ import annotations

from datetime import datetime, timezone

from src.signals.regime import classify_regime, compute_efficiency_ratio
from src.storage.bar_aggregator import OHLCVBar


def _bar(*, close: float, index: int) -> OHLCVBar:
    return OHLCVBar(
        exchange="mock",
        symbol="BTC/EUR",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
    )


def test_compute_efficiency_ratio_is_one_for_a_straight_line_move() -> None:
    """A monotonic price move should have an efficiency ratio of 1.0."""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    assert compute_efficiency_ratio(closes) == 1.0


def test_compute_efficiency_ratio_is_near_zero_for_a_round_trip() -> None:
    """A price that oscillates back to its start covers a lot of path for zero net move."""
    closes = [100.0, 102.0, 98.0, 102.0, 100.0]
    assert compute_efficiency_ratio(closes) == 0.0


def test_compute_efficiency_ratio_handles_flat_prices() -> None:
    """A flat price series has zero path length and should not divide by zero."""
    assert compute_efficiency_ratio([100.0, 100.0, 100.0]) == 0.0


def test_classify_regime_trending_for_a_steady_uptrend() -> None:
    """A steadily rising close series should classify as trending."""
    history = [_bar(close=100.0 + i, index=i) for i in range(25)]
    assert classify_regime(history, window=20) == "trending"


def test_classify_regime_ranging_for_a_choppy_series() -> None:
    """A series oscillating between two values should classify as ranging."""
    history = [_bar(close=100.0 + (i % 2), index=i) for i in range(25)]
    assert classify_regime(history, window=20) == "ranging"
