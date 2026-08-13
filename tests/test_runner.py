"""Tests for the configurable backtest runner."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import BacktestConfig, compare_backtests
from src.storage.bar_aggregator import OHLCVBar


def test_compare_backtests_reports_cost_impact() -> None:
    """The runner should compare baseline and cost-adjusted runs."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=100.0,
            low=100.0,
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

    config = BacktestConfig(strategy_name="moving_average_crossover", include_costs=False)
    comparison = compare_backtests(bars, config=config)

    assert comparison.baseline.final_equity >= 100.0
    assert comparison.with_costs.final_equity <= comparison.baseline.final_equity
