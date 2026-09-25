"""Tests for the configurable backtest runner."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import BacktestConfig, StrategyRegistry, compare_backtests, resolve_strategy
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


def test_strategy_registry_can_resolve_runtime_style_strategy() -> None:
    """The strategy registry should provide the new breakout strategy entry."""
    registry = StrategyRegistry()
    strategy = resolve_strategy("momentum_breakout", registry=registry, lookback=3, threshold=0.02)

    assert callable(strategy)


def test_strategy_registry_can_resolve_volume_confirmed_momentum_strategy() -> None:
    """The strategy registry should provide the volume-confirmed momentum strategy entry."""
    registry = StrategyRegistry()
    strategy = resolve_strategy("volume_confirmed_momentum", registry=registry, lookback=5, volume_multiplier=2.0)

    assert callable(strategy)


def test_strategy_registry_can_resolve_volume_confirmed_momentum_biased_strategy() -> None:
    """The strategy registry should provide the long-biased momentum strategy entry."""
    registry = StrategyRegistry()
    strategy = resolve_strategy("volume_confirmed_momentum_biased", registry=registry, short_threshold_multiplier=3.0)

    assert callable(strategy)


def test_strategy_registry_reports_short_capability() -> None:
    """The registry should expose whether each registered strategy can emit a short signal."""
    registry = StrategyRegistry()

    assert registry.can_short("moving_average_crossover") is True
    assert registry.can_short("volume_confirmed_momentum") is True

    def long_only_stub(**_: object) -> object:
        return lambda history, index, bar: 0

    registry.register("long_only_stub", long_only_stub, can_short=False)
    assert registry.can_short("long_only_stub") is False
