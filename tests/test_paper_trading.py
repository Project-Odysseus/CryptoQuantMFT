"""Tests for the lightweight paper-trading engine."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.execution import PaperTradingEngine
from src.execution.adapters import ExecutionReport
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
    signals = [1.0, 1.0, 1.0]

    result = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5).run(bars, signals)

    assert len(result.orders) == 1
    assert result.orders[0].status == "FILLED"
    assert result.orders[0].filled_size == 1.0
    assert len(result.trades) == 1
    assert result.portfolio_history[-1].equity > 100.0


def test_paper_trading_engine_closes_positions_when_signal_flips() -> None:
    """A position should be closed when the signal turns against it."""
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
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 3, tzinfo=timezone.utc),
            open=103.0,
            high=103.0,
            low=103.0,
            close=103.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 4, tzinfo=timezone.utc),
            open=104.0,
            high=104.0,
            low=104.0,
            close=104.0,
            volume=10.0,
        ),
    ]
    signals = [1.0, 1.0, -1.0, 0.0, 0.0]

    result = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5).run(bars, signals)

    assert len(result.orders) == 2
    assert [order.side for order in result.orders] == ["buy", "sell"]
    assert len(result.trades) == 2
    assert result.portfolio_history[-1].position_size == 0.0


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


def test_paper_trading_engine_sizes_buy_orders_to_available_cash() -> None:
    """Buy orders should be reduced to a fraction that fits the current cash balance."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=50000.0,
            high=50000.0,
            low=50000.0,
            close=50000.0,
            volume=10.0,
        )
    ]

    result = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5).run(bars, [1.0])

    assert len(result.orders) == 1
    assert result.orders[0].size == 0.02


def test_exchange_cycle_reconciles_async_fill_and_avoids_duplicate_entry() -> None:
    """A real exchange order that fills asynchronously should be detected, logged, and not re-entered."""

    class AsyncFillExecutionAdapter:
        exchange_name = "kraken"
        name = "kraken"
        _base_currency = "EUR"

        def __init__(self) -> None:
            self._balances = {"EUR": 1000.0}
            self._positions: dict[str, float] = {}
            self._orders: list[SimpleNamespace] = []

        def list_orders(self) -> list[SimpleNamespace]:
            return list(self._orders)

        def get_account_snapshot(self) -> dict[str, object]:
            return {"balances": dict(self._balances), "positions": dict(self._positions), "account_reconciliation": {}}

        def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
            order = SimpleNamespace(
                order_id=order_id,
                side=side,
                size=size,
                symbol=symbol,
                exchange=self.exchange_name,
                status="SUBMITTED",
                filled_size=0.0,
                fill_price=None,
                fee=0.0,
                timestamp=timestamp,
            )
            self._orders.append(order)
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="submitted to kraken")

        def recover_execution_state(self, *, remote_snapshot: dict[str, object] | None = None, remote_orders: object | None = None) -> dict[str, object]:
            # Simulate the exchange reporting a real fill on the next poll,
            # exactly like a real Kraken limit order filling asynchronously
            # after submit_order() already returned SUBMITTED.
            for order in self._orders:
                if order.status == "SUBMITTED":
                    order.status = "FILLED"
                    order.filled_size = order.size
                    order.fill_price = 68000.0
                    order.fee = 0.01
                    self._balances["EUR"] -= order.size * 68000.0 + 0.01
                    self._positions["BTC"] = self._positions.get("BTC", 0.0) + order.size
            return {}

    class FakeTradeLogger:
        def __init__(self) -> None:
            self.trades: list[dict[str, object]] = []

        def log_event(self, **_: object) -> int:
            return 1

        def log_trade(self, **kwargs: object) -> tuple[int, None]:
            self.trades.append(kwargs)
            return 1, None

        def log_equity_snapshot(self, **_: object) -> int:
            return 1

    logger = FakeTradeLogger()
    adapter = AsyncFillExecutionAdapter()
    engine = PaperTradingEngine(execution_adapter=adapter, exchange_name="kraken", default_order_size=1.0, trade_logger=logger)

    bar1 = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=68000.0,
            high=68000.0,
            low=68000.0,
            close=68000.0,
            volume=10.0,
        )
    ]
    first = engine.run_exchange_cycle(bar1, [1.0])
    assert first.trades == []  # submitted, not yet filled
    assert len(adapter.list_orders()) == 1

    bar2 = bar1 + [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=68000.0,
            high=68000.0,
            low=68000.0,
            close=68000.0,
            volume=10.0,
        )
    ]
    order_size = adapter.list_orders()[0].size
    second = engine.run_exchange_cycle(bar2, [1.0, 1.0])

    assert len(second.trades) == 1
    assert second.trades[0].size == order_size
    assert second.trades[0].price == 68000.0
    assert second.portfolio_history[-1].position_size == order_size
    # Still just the one real order - the fill was recognized, so the
    # still-bullish signal did not trigger a second buy.
    assert len(adapter.list_orders()) == 1
    assert len(logger.trades) == 1


def test_exchange_cycle_closes_existing_long_on_sell_signal() -> None:
    """Exchange-backed runtime cycles should close an existing long when the strategy flips sell."""

    class StubExecutionAdapter:
        exchange_name = "kraken"
        name = "kraken"
        _base_currency = "EUR"

        def __init__(self) -> None:
            self.orders: list[SimpleNamespace] = []
            self._balances = {"EUR": 10.68}
            self._positions = {"BTC": 0.00005145}

        def list_orders(self) -> list[SimpleNamespace]:
            return self.orders

        def get_account_snapshot(self) -> dict[str, object]:
            return {
                "balances": dict(self._balances),
                "positions": dict(self._positions),
                "account_reconciliation": {},
            }

        def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
            self._balances["EUR"] += price * size
            self._positions["BTC"] = 0.0
            order = SimpleNamespace(
                order_id=order_id,
                timestamp=timestamp,
                side=side,
                size=size,
                symbol=symbol,
                exchange=self.exchange_name,
                status="FILLED",
                filled_size=size,
                fill_price=price,
                fee=0.0,
                remote_status="FILLED",
                message="filled",
            )
            self.orders.append(order)
            return ExecutionReport(order_id=order_id, status="FILLED", fill_price=price, filled_size=size, fee=0.0, message="filled")

    bars = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=68000.0,
            high=68000.0,
            low=68000.0,
            close=68000.0,
            volume=10.0,
        )
    ]
    adapter = StubExecutionAdapter()
    engine = PaperTradingEngine(execution_adapter=adapter, exchange_name="kraken")

    result = engine.run_exchange_cycle(bars, [-1.0])

    assert len(result.orders) == 1
    assert result.orders[0].side == "sell"
    assert len(result.trades) == 1
    assert result.portfolio_history[-1].position_size == 0.0


def test_exchange_cycle_blocks_short_entry_by_default() -> None:
    """Exchange-backed cycles should still refuse to open a short unless allow_short is set."""
    from src.execution.adapters import SandboxExecutionAdapter

    adapter = SandboxExecutionAdapter(exchange_name="kraken")
    adapter._balances = {"EUR": 1000.0}
    adapter._base_currency = "EUR"
    bars = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=68000.0,
            high=68000.0,
            low=68000.0,
            close=68000.0,
            volume=10.0,
        )
    ]
    engine = PaperTradingEngine(execution_adapter=adapter, exchange_name="kraken", allow_short=False)

    result = engine.run_exchange_cycle(bars, [-1.0])

    assert result.trades == []
    assert result.entry_decisions
    assert result.entry_decisions[-1]["reason"] == "spot_shorting_disabled"
    assert result.portfolio_history[-1].position_size == 0.0


def test_exchange_cycle_opens_and_closes_a_short_when_allowed() -> None:
    """With allow_short=True, exchange-backed cycles should open a short on a sell signal and cover it on a buy signal."""
    from src.execution.adapters import SandboxExecutionAdapter

    adapter = SandboxExecutionAdapter(exchange_name="kraken")
    adapter._balances = {"EUR": 1000.0}
    adapter._base_currency = "EUR"
    engine = PaperTradingEngine(
        execution_adapter=adapter,
        exchange_name="kraken",
        default_order_size=1.0,
        allow_short=True,
    )

    open_bar = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=68000.0,
            high=68000.0,
            low=68000.0,
            close=68000.0,
            volume=10.0,
        )
    ]
    open_result = engine.run_exchange_cycle(open_bar, [-1.0])

    assert len(open_result.trades) == 1
    assert open_result.trades[0].side == "sell"
    assert open_result.portfolio_history[-1].position_size < 0.0

    close_bar = open_bar + [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=67000.0,
            high=67000.0,
            low=67000.0,
            close=67000.0,
            volume=10.0,
        )
    ]
    close_result = engine.run_exchange_cycle(close_bar, [-1.0, 1.0])

    assert len(close_result.trades) == 1
    assert close_result.trades[0].side == "buy"
    assert close_result.portfolio_history[-1].position_size == 0.0


def test_exchange_cycle_force_closes_position_on_time_stop_without_exit_signal() -> None:
    """Exchange-backed cycles should force-close a stale position using the adapter's real entry state, even with no exit signal."""
    from src.execution.adapters import SandboxExecutionAdapter

    class FakeTradeLogger:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def log_event(self, **kwargs: object) -> int:
            self.events.append(kwargs)
            return 1

        def log_trade(self, **_: object) -> tuple[int, None]:
            return 1, None

        def log_equity_snapshot(self, **_: object) -> int:
            return 1

    logger = FakeTradeLogger()
    adapter = SandboxExecutionAdapter(exchange_name="kraken")
    adapter._balances = {"EUR": 1000.0}
    adapter._base_currency = "EUR"
    adapter.submit_order(
        order_id="opening-buy",
        side="buy",
        size=0.01,
        price=68000.0,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        symbol="BTC/EUR",
    )

    bars = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
            open=68000.0,
            high=68000.0,
            low=68000.0,
            close=68000.0,
            volume=10.0,
        )
        for index in range(3)
    ]
    signals = [0.0, 0.0, 0.0]

    risk_manager = RiskManager(RiskControlConfig(time_stop_bars=2))
    engine = PaperTradingEngine(execution_adapter=adapter, exchange_name="kraken", risk_manager=risk_manager, trade_logger=logger)

    result = engine.run_exchange_cycle(bars, signals)

    sell_orders = [order for order in result.orders if order.side == "sell"]
    assert sell_orders
    assert len(result.trades) == 1
    assert result.portfolio_history[-1].position_size == 0.0
    force_close_events = [event for event in logger.events if event.get("message") == "position force-closed by risk control"]
    assert force_close_events
    assert force_close_events[0]["metadata"]["reason"] == "time_stop"


def test_paper_trading_engine_sizes_buy_orders_to_risk_budget() -> None:
    """Buy orders should respect a portfolio risk budget based on current equity."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=50000.0,
            high=50000.0,
            low=50000.0,
            close=50000.0,
            volume=10.0,
        )
    ]

    risk_manager = RiskManager(RiskControlConfig(risk_per_trade_pct=0.10))
    result = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5, risk_manager=risk_manager).run(bars, [1.0])

    assert len(result.orders) == 1
    assert result.orders[0].size == 0.002


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


def test_paper_trading_engine_logs_execution_adapter_response() -> None:
    """Adapter fills should be logged with the adapter status and message."""
    class FakeExecutionAdapter:
        name = "sandbox"
        exchange_name = "sandbox"

        def submit_order(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(status="FILLED", message="filled by sandbox", fill_price=100.0, filled_size=1.0, fee=0.1)

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

    PaperTradingEngine(trade_logger=logger, execution_adapter=FakeExecutionAdapter()).run(bars, [1.0])

    assert any(
        event["event_type"] == "order_lifecycle"
        and event["metadata"]["execution_status"] == "FILLED"
        and event["metadata"]["execution_message"] == "filled by sandbox"
        for event in logger.events
    )


def test_paper_trading_engine_logs_runtime_symbol_and_exchange_for_trades() -> None:
    """Persisted paper trades should reflect the active bar symbol and venue."""
    class FakeTradeLogger:
        def __init__(self) -> None:
            self.trades: list[dict[str, object]] = []

        def log_event(self, **_: object) -> int:
            return 1

        def log_trade(self, **kwargs: object) -> int:
            self.trades.append(kwargs)
            return 1

        def log_equity_snapshot(self, **_: object) -> int:
            return 1

    logger = FakeTradeLogger()
    bars = [
        OHLCVBar(
            exchange="kraken",
            symbol="ETH/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="kraken",
            symbol="ETH/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=101.0,
            high=101.0,
            low=101.0,
            close=101.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="kraken",
            symbol="ETH/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
    ]

    PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, trade_logger=logger).run(bars, [1.0, 0.0, 0.0])

    assert logger.trades
    assert logger.trades[0]["exchange"] == "kraken"
    assert logger.trades[0]["pair"] == "ETH/EUR"


def test_paper_trading_engine_enables_tax_logging_only_when_requested() -> None:
    """Live-mode tax logging should be opt-in at the execution engine layer."""
    class FakeTradeLogger:
        def __init__(self) -> None:
            self.trades: list[dict[str, object]] = []

        def log_event(self, **_: object) -> int:
            return 1

        def log_trade(self, **kwargs: object) -> int:
            self.trades.append(kwargs)
            return 1

        def log_equity_snapshot(self, **_: object) -> int:
            return 1

    logger = FakeTradeLogger()
    bars = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=101.0,
            high=101.0,
            low=101.0,
            close=101.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
    ]

    PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, trade_logger=logger, enable_tax_logging=True).run(bars, [1.0, 0.0, 0.0])

    assert logger.trades
    assert logger.trades[0]["record_tax_event"] is True


def test_paper_trading_engine_tags_trades_with_strategy_id() -> None:
    """Persisted trades should carry the engine's strategy_id for future portfolio attribution."""
    class FakeTradeLogger:
        def __init__(self) -> None:
            self.trades: list[dict[str, object]] = []

        def log_event(self, **_: object) -> int:
            return 1

        def log_trade(self, **kwargs: object) -> int:
            self.trades.append(kwargs)
            return 1

        def log_equity_snapshot(self, **_: object) -> int:
            return 1

    logger = FakeTradeLogger()
    bars = [
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
            open=101.0,
            high=101.0,
            low=101.0,
            close=101.0,
            volume=10.0,
        ),
        OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),
            open=102.0,
            high=102.0,
            low=102.0,
            close=102.0,
            volume=10.0,
        ),
    ]

    PaperTradingEngine(
        initial_cash=1000.0,
        default_order_size=1.0,
        trade_logger=logger,
        strategy_id="moving_average_crossover",
    ).run(bars, [1.0, 0.0, 0.0])

    assert logger.trades
    assert logger.trades[0]["strategy_id"] == "moving_average_crossover"


def test_paper_trading_engine_force_closes_position_on_time_stop() -> None:
    """A position held past time_stop_bars should be closed even without an opposing signal."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
        )
        for index in range(7)
    ]
    signals = [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0]  # signal stays long until after the time stop fires, so only the time stop can exit

    risk_manager = RiskManager(RiskControlConfig(time_stop_bars=2, paper_mode=True))
    result = PaperTradingEngine(
        initial_cash=1000.0,
        default_order_size=1.0,
        partial_fill_fraction=1.0,
        max_order_lifetime_bars=5,
        risk_manager=risk_manager,
    ).run(bars, signals)

    time_stop_orders = [order for order in result.orders if order.last_reason == "time_stop"]
    assert time_stop_orders
    assert time_stop_orders[0].status == "FILLED"
    assert result.portfolio_history[-1].position_size == 0.0


def test_engine_closes_a_long_when_signal_returns_to_zero() -> None:
    """Signal 0 means 'be flat': a strategy that exits by returning 0 must actually exit (matches the backtester)."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
        )
        for index in range(6)
    ]

    result = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5).run(bars, [1.0, 1.0, 0.0, 0.0, 0.0, 0.0])

    assert [order.side for order in result.orders] == ["buy", "sell"]
    assert result.portfolio_history[-1].position_size == 0.0


def test_exchange_cycle_closes_long_and_short_positions_when_signal_returns_to_zero() -> None:
    """Exchange-backed cycles should treat signal 0 as 'be flat' for both sides, not 'hold'."""
    from src.execution.adapters import SandboxExecutionAdapter

    def bar(minute: int, price: float) -> OHLCVBar:
        return OHLCVBar(
            exchange="kraken",
            symbol="BTC/EUR",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, minute, tzinfo=timezone.utc),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=10.0,
        )

    for entry_signal, entry_side, exit_side in ((1.0, "buy", "sell"), (-1.0, "sell", "buy")):
        adapter = SandboxExecutionAdapter(exchange_name="kraken")
        adapter._balances = {"EUR": 1000.0}
        adapter._base_currency = "EUR"
        engine = PaperTradingEngine(execution_adapter=adapter, exchange_name="kraken", default_order_size=1.0, allow_short=True)

        opened = engine.run_exchange_cycle([bar(0, 68000.0)], [entry_signal])
        assert [trade.side for trade in opened.trades] == [entry_side]

        closed = engine.run_exchange_cycle([bar(0, 68000.0), bar(1, 68000.0)], [entry_signal, 0.0])
        assert [trade.side for trade in closed.trades] == [exit_side]
        assert closed.portfolio_history[-1].position_size == 0.0
