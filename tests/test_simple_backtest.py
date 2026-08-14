"""Tests for the lightweight simple backtester."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import SimpleBacktester, moving_average_crossover_strategy
from src.backtest.costs import CostModel
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


def test_simple_backtester_supports_user_supplied_strategy() -> None:
    """The backtester should accept a strategy callback and record its trades."""
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
    assert len(result.trade_returns) == 1
    assert result.final_equity > 100.0
    assert result.win_rate == 1.0


def test_simple_backtester_uses_cost_model_for_trade_pnl() -> None:
    """The backtester should reduce equity when a cost model is supplied."""

    def strategy(history: list[OHLCVBar], index: int, current_bar: OHLCVBar) -> int:
        """Generate the signal strategy output for the current market context."""
        if index == 1:
            return 1
        if index == 2:
            return -1
        return 0

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
            close=100.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=101.0,
            volume=10.0,
        ),
    ]

    cost_model = CostModel(exchange="mock", taker_fee=0.5, fx_spread_bps=100)
    result = SimpleBacktester(strategy=strategy, initial_equity=100.0, cost_model=cost_model).run(bars)

    assert result.trades == 1
    assert result.trade_costs
    assert result.final_equity < 100.0
    assert result.trade_records[0].cost > 0.0
