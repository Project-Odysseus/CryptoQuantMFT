"""Tests for the lightweight simple backtester."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import SimpleBacktester, moving_average_crossover_strategy
from src.backtest.costs import CostModel
from src.backtest.simple_backtest import volume_confirmed_momentum_strategy
from src.risk.controls import RiskControlConfig, RiskManager
from src.storage.bar_aggregator import OHLCVBar


def _bar(*, close: float, volume: float, index: int) -> OHLCVBar:
    return OHLCVBar(
        exchange="mock",
        symbol="BTC/EUR",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
    )


def test_volume_confirmed_momentum_strategy_signals_long_on_confirmed_breakout() -> None:
    """A price breakout accompanied by a volume spike should signal long."""
    history = [
        _bar(close=100.0, volume=1.0, index=0),
        _bar(close=100.0, volume=1.0, index=1),
        _bar(close=100.0, volume=1.0, index=2),
        _bar(close=100.0, volume=10.0, index=3),
        _bar(close=100.5, volume=10.0, index=4),
        _bar(close=101.0, volume=10.0, index=5),
        _bar(close=102.0, volume=20.0, index=6),
    ]
    strategy = volume_confirmed_momentum_strategy(lookback=3, threshold=0.01, volume_window=3, volume_multiplier=1.5)

    signal = strategy(history, len(history) - 1, history[-1])

    assert signal == 1


def test_volume_confirmed_momentum_strategy_blocks_breakout_without_volume() -> None:
    """The same price breakout without a volume spike should not signal."""
    history = [
        _bar(close=100.0, volume=1.0, index=0),
        _bar(close=100.0, volume=1.0, index=1),
        _bar(close=100.0, volume=1.0, index=2),
        _bar(close=100.0, volume=10.0, index=3),
        _bar(close=100.5, volume=10.0, index=4),
        _bar(close=101.0, volume=10.0, index=5),
        _bar(close=102.0, volume=10.0, index=6),
    ]
    strategy = volume_confirmed_momentum_strategy(lookback=3, threshold=0.01, volume_window=3, volume_multiplier=1.5)

    signal = strategy(history, len(history) - 1, history[-1])

    assert signal == 0


def test_volume_confirmed_momentum_strategy_signals_short_on_confirmed_drop() -> None:
    """A confirmed downward breakout should signal short."""
    history = [
        _bar(close=100.0, volume=1.0, index=0),
        _bar(close=100.0, volume=1.0, index=1),
        _bar(close=100.0, volume=1.0, index=2),
        _bar(close=100.0, volume=10.0, index=3),
        _bar(close=99.5, volume=10.0, index=4),
        _bar(close=99.0, volume=10.0, index=5),
        _bar(close=98.0, volume=20.0, index=6),
    ]
    strategy = volume_confirmed_momentum_strategy(lookback=3, threshold=0.01, volume_window=3, volume_multiplier=1.5)

    signal = strategy(history, len(history) - 1, history[-1])

    assert signal == -1


def test_volume_confirmed_momentum_strategy_requires_enough_history() -> None:
    """With too little history, the strategy should stay flat rather than error."""
    history = [_bar(close=100.0, volume=1.0, index=0), _bar(close=101.0, volume=1.0, index=1)]
    strategy = volume_confirmed_momentum_strategy(lookback=3, threshold=0.01, volume_window=3, volume_multiplier=1.5)

    signal = strategy(history, len(history) - 1, history[-1])

    assert signal == 0


def test_simple_backtester_force_closes_short_on_position_drawdown_stop() -> None:
    """A short that moves 5%+ against entry should be force-closed even though the strategy never reverses."""

    def always_short_strategy(history: list, index: int, current_bar) -> int:
        return -1 if index >= 1 else 0

    bars = [
        _bar(close=100.0, volume=1.0, index=0),
        _bar(close=100.0, volume=1.0, index=1),  # opens short here at 100.0
        _bar(close=102.0, volume=1.0, index=2),  # +2%, within the 5% budget
        _bar(close=106.0, volume=1.0, index=3),  # +6%, breaches the 5% stop
        _bar(close=110.0, volume=1.0, index=4),
    ]
    risk_manager = RiskManager(RiskControlConfig(paper_mode=True, position_drawdown_stop_pct=0.05))

    result = SimpleBacktester(strategy=always_short_strategy, initial_equity=1000.0, risk_manager=risk_manager).run(bars)

    assert result.trades == 1
    assert result.trade_records[0].side == "short"
    assert result.trade_records[0].reason == "position_drawdown_stop"
    assert result.trade_records[0].exit_price == 106.0


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
