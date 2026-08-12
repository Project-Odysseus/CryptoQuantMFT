"""Tests for volatility and trend filter helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from src.signals import TrendFilterSignal, VolatilityTrendFilter
from src.storage.bar_aggregator import OHLCVBar


def test_volatility_trend_filter_classifies_recent_momentum() -> None:
    """The filter should classify momentum relative to ATR-style volatility."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=100.5,
            high=103.0,
            low=100.0,
            close=102.5,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=102.5,
            high=105.0,
            low=101.0,
            close=104.0,
            volume=10.0,
        ),
    ]

    signal = VolatilityTrendFilter(lookback=3, atr_multiplier=1.0).compute(bars)

    assert isinstance(signal, TrendFilterSignal)
    assert signal.direction == "neutral"
    assert signal.momentum == 1.5
    assert signal.volatility == 3.5
    assert signal.value == 0.4285714286
