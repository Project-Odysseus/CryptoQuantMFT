"""Tests for Kelly sizing and performance analytics."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtest import SimpleBacktester
from src.risk.controls import RiskControlConfig, RiskManager
from src.storage.bar_aggregator import OHLCVBar


def test_risk_manager_uses_fractional_kelly_sizing() -> None:
    """Kelly sizing should scale entries down when edge is modest."""
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
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
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
            timestamp=datetime(2024, 1, 1, 0, 3, tzinfo=timezone.utc),
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
    ]

    manager = RiskManager(
        RiskControlConfig(max_volatility_pct=0.5, max_position_size=1.0, sizing="kelly", sizing_params={"kelly_fraction": 0.5, "min_trades": 20})
    )
    for index in range(40):  # the strategy's own closed trades: +10% and -8% alternating (mean 1%)
        manager.record_trade_return(0.10 if index % 2 == 0 else -0.08)
    decision = manager.evaluate(bars=bars, equity=100.0, peak_equity=100.0)

    full_kelly = 0.01 / ((0.10**2 + 0.08**2) / 2)  # about 1.22, so half Kelly is about 0.61
    assert decision.allow_entry and decision.sizing == "kelly"
    assert decision.position_size == pytest.approx(0.5 * full_kelly)
    assert 0.0 < decision.position_size < 1.0


def test_simple_backtester_populates_performance_metrics() -> None:
    """Backtests should expose a first-pass analytics bundle."""
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
            high=105.0,
            low=100.0,
            close=105.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=105.0,
            high=106.0,
            low=104.0,
            close=106.0,
            volume=10.0,
        ),
    ]

    def strategy(history: list[OHLCVBar], index: int, current_bar: OHLCVBar) -> int:
        """Generate the signal strategy output for the current market context."""
        return 1 if index >= 1 else 0

    result = SimpleBacktester(strategy=strategy, initial_equity=100.0).run(bars)

    assert result.trades == 1
    assert result.metrics.sharpe_ratio >= 0.0
    assert result.metrics.max_drawdown >= 0.0
    assert result.metrics.profit_factor >= 0.0
    assert result.metrics.expectancy >= 0.0
