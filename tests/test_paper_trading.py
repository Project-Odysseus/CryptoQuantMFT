"""Tests for the lightweight paper-trading engine."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.execution import PaperTradingEngine
from src.risk.controls import RiskControlConfig, RiskManager
from src.storage.bar_aggregator import OHLCVBar


def test_paper_trading_engine_advances_orders_through_state_machine() -> None:
    """Orders should transition through pending, open, and filled states."""
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
            open=101.0,
            high=101.0,
            low=101.0,
            close=101.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
    ]
    signals = [1.0, 0.0, 0.0]

    result = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5).run(bars, signals)

    assert len(result.orders) == 1
    assert result.orders[0].status == "FILLED"
    assert result.orders[0].filled_size == 1.0
    assert len(result.trades) == 1
    assert result.portfolio_history[-1].equity > 100.0


def test_paper_trading_engine_can_cancel_unfilled_orders() -> None:
    """Orders should be cancelled if they remain unresolved too long."""
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
            open=101.0,
            high=101.0,
            low=101.0,
            close=101.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
    ]
    signals = [1.0, 1.0, 1.0]

    result = PaperTradingEngine(
        default_order_size=1.0,
        partial_fill_fraction=0.5,
        max_order_lifetime_bars=1,
        risk_manager=RiskManager(
            RiskControlConfig(max_drawdown_pct=0.0, max_volatility_pct=0.0, risk_per_trade_pct=0.02, max_position_size=1.0)
        ),
    ).run(bars, signals)

    assert result.orders[0].status == "CANCELED"
    assert len(result.trades) == 0


def test_paper_trading_engine_skips_zero_size_orders() -> None:
    """Zero-size risk decisions should not create paper-trading orders."""
    class ZeroSizeRiskManager:
        """Represent a ZeroSizeRiskManager."""
        def evaluate(self, **_: object) -> SimpleNamespace:
            """Evaluate whether the current state allows a new trade entry."""
            return SimpleNamespace(allow_entry=True, position_size=0.0)

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
        )
    ]

    result = PaperTradingEngine(risk_manager=ZeroSizeRiskManager()).run(bars, [1.0])

    assert result.orders == []
    assert result.trades == []


def test_paper_trading_engine_records_blocked_entry_decisions() -> None:
    """Blocked entry attempts should be recorded for dashboard summaries."""
    class BlockingRiskManager:
        """Represent a BlockingRiskManager."""
        def evaluate(self, **_: object) -> SimpleNamespace:
            """Reject entries with a specific reason for reporting."""
            return SimpleNamespace(allow_entry=False, position_size=0.0, reason="spread_limit")

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
        )
    ]

    result = PaperTradingEngine(risk_manager=BlockingRiskManager()).run(bars, [1.0])

    assert len(result.entry_decisions) == 1
    assert result.entry_decisions[0]["allowed"] is False
    assert result.entry_decisions[0]["reason"] == "spread_limit"


def test_paper_trading_engine_logs_blocked_entry_reasons() -> None:
    """Trade logging should capture entry decisions with the underlying blocking reason."""
    class BlockingRiskManager:
        def evaluate(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(allow_entry=False, position_size=0.0, reason="spread_limit")

    class FakeTradeLogger:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def log_event(self, **kwargs: object) -> int:
            self.events.append(kwargs)
            return 1

        def log_trade(self, **_: object) -> int:
            return 1

        def log_equity_snapshot(self, **_: object) -> int:
            return 1

    logger = FakeTradeLogger()
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
        )
    ]

    PaperTradingEngine(risk_manager=BlockingRiskManager(), trade_logger=logger).run(bars, [1.0])

    assert any(event["event_type"] == "entry_decision" and event["metadata"]["reason"] == "spread_limit" for event in logger.events)
