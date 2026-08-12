"""Tests for the lightweight simple backtester."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import SimpleBacktester
from src.storage.bar_aggregator import OHLCVBar


def test_simple_backtester_runs_on_ohlcv_bars() -> None:
    """The backtester should produce a simple summary from a sequence of bars."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=101.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=101.0,
            high=102.0,
            low=100.0,
            close=102.0,
            volume=10.0,
        ),
    ]

    result = SimpleBacktester().run(bars)

    assert result.trades == 0
    assert result.total_return == 0.0
    assert result.win_rate == 0.0
    assert result.max_drawdown == 0.0
    assert result.final_equity == 100.0
