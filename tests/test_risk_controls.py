"""Tests for the lightweight risk-control layer."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import SimpleBacktester
from src.risk.controls import RiskControlConfig, RiskManager
from src.storage.bar_aggregator import OHLCVBar


def test_risk_manager_blocks_entries_after_drawdown() -> None:
    """Drawdown controls should block new entries once the account has fallen enough."""
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
            open=90.0,
            high=90.0,
            low=90.0,
            close=90.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=80.0,
            high=80.0,
            low=80.0,
            close=80.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 3, tzinfo=timezone.utc),
            open=85.0,
            high=85.0,
            low=85.0,
            close=85.0,
            volume=10.0,
        ),
    ]

    manager = RiskManager(RiskControlConfig(max_drawdown_pct=0.15))
    decision = manager.evaluate(bars=bars[:3], equity=80.0, peak_equity=100.0)
    assert not decision.allow_entry
    assert decision.reason == "drawdown_limit"


def test_risk_manager_reduces_position_size_for_high_volatility() -> None:
    """Volatility should shrink the size of new positions."""
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
            open=120.0,
            high=120.0,
            low=120.0,
            close=120.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=80.0,
            high=80.0,
            low=80.0,
            close=80.0,
            volume=10.0,
        ),
    ]

    manager = RiskManager(RiskControlConfig(max_volatility_pct=0.5, risk_per_trade_pct=0.02))
    decision = manager.evaluate(bars=bars, equity=100.0, peak_equity=100.0)
    assert decision.allow_entry
    assert decision.position_size < 1.0


def test_risk_manager_blocks_entries_on_high_spread() -> None:
    """A wide spread should block entry before sizing is considered."""
    bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=100.0,
        high=105.0,
        low=90.0,
        close=100.0,
        volume=10.0,
    )

    manager = RiskManager(RiskControlConfig(max_spread_pct=0.01))
    decision = manager.evaluate(bars=[bar], equity=100.0, peak_equity=100.0, current_bar=bar)

    assert not decision.allow_entry
    assert decision.reason == "spread_limit"


def test_risk_manager_blocks_entries_on_excessive_slippage() -> None:
    """A large open-to-close move should block entry before sizing is considered."""
    bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=100.0,
        high=150.0,
        low=100.0,
        close=150.0,
        volume=10.0,
    )

    manager = RiskManager(RiskControlConfig(max_slippage_pct=0.2))
    decision = manager.evaluate(bars=[bar], equity=100.0, peak_equity=100.0, current_bar=bar)

    assert not decision.allow_entry
    assert decision.reason == "slippage_limit"


def test_risk_manager_blocks_entries_on_stale_quotes() -> None:
    """A quote gap larger than the configured threshold should block entry."""
    previous_bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=10.0,
    )
    current_bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 3, tzinfo=timezone.utc),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=10.0,
    )

    manager = RiskManager(RiskControlConfig(max_quote_age_seconds=60))
    decision = manager.evaluate(bars=[previous_bar, current_bar], equity=100.0, peak_equity=100.0, current_bar=current_bar, bar_index=1)

    assert not decision.allow_entry
    assert decision.reason == "stale_quote"


def test_simple_backtester_respects_risk_manager() -> None:
    """The backtester should skip entries when the risk manager blocks them."""

    def strategy(history: list[OHLCVBar], index: int, current_bar: OHLCVBar) -> int:
        if index == 1:
            return 1
        if index == 2:
            return -1
        if index == 3:
            return 1
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
            open=90.0,
            high=90.0,
            low=90.0,
            close=90.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=80.0,
            high=80.0,
            low=80.0,
            close=80.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 3, tzinfo=timezone.utc),
            open=85.0,
            high=85.0,
            low=85.0,
            close=85.0,
            volume=10.0,
        ),
    ]

    risk_manager = RiskManager(RiskControlConfig(max_drawdown_pct=0.15))
    result = SimpleBacktester(strategy=strategy, risk_manager=risk_manager).run(bars)

    assert result.trades == 1
    assert result.final_equity < 100.0
