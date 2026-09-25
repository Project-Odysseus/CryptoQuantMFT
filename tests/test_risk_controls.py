"""Tests for the lightweight risk-control layer."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.backtest import SimpleBacktester
from src.risk.controls import CircuitBreaker, RiskControlConfig, RiskManager
from src.risk.kill_switch import KillSwitchController
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


def test_risk_manager_reduces_position_size_for_inventory_skew() -> None:
    """Heavily skewed inventory should shrink buy sizing for new long entries."""
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
    ]

    manager = RiskManager(RiskControlConfig(inventory_skew_threshold=0.5, inventory_penalty_factor=1.0))
    decision = manager.evaluate(bars=bars, equity=100.0, peak_equity=100.0, signal_side="buy", inventory_skew=0.8)

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

    manager = RiskManager(RiskControlConfig(max_quote_age_seconds=60, enforce_quote_freshness=True))
    decision = manager.evaluate(bars=[previous_bar, current_bar], equity=100.0, peak_equity=100.0, current_bar=current_bar, bar_index=1)

    assert not decision.allow_entry
    assert decision.reason == "stale_quote"


def test_risk_manager_triggers_circuit_breaker_on_hard_stop_drawdown() -> None:
    """A drawdown breach should activate the hard-stop circuit breaker."""
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
    breaker = CircuitBreaker()
    manager = RiskManager(RiskControlConfig(hard_stop_drawdown_pct=0.01))
    decision = manager.evaluate(bars=bars, equity=98.0, peak_equity=100.0, circuit_breaker=breaker)

    assert not decision.allow_entry
    assert decision.reason == "hard_stop_drawdown"
    assert breaker.is_active()


def test_risk_manager_enforces_exchange_open_order_limit() -> None:
    """Exchange-specific order limits should block new entries once the queue is saturated."""
    bar = OHLCVBar(
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

    manager = RiskManager(RiskControlConfig())
    decision = manager.evaluate(
        bars=[bar],
        equity=1000.0,
        peak_equity=1000.0,
        current_bar=bar,
        exchange_name="kraken",
        open_orders_count=2,
    )

    assert not decision.allow_entry
    assert decision.reason == "open_order_limit"


def test_risk_manager_enforces_exchange_notional_limit() -> None:
    """Exchange-specific notional caps should reduce or reject new entries once the budget is exhausted."""
    bar = OHLCVBar(
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

    manager = RiskManager(RiskControlConfig())
    decision = manager.evaluate(
        bars=[bar],
        equity=1000.0,
        peak_equity=1000.0,
        current_bar=bar,
        exchange_name="firi",
        current_exchange_notional=2000.0,
    )

    assert not decision.allow_entry
    assert decision.reason == "exchange_notional_limit"


def test_risk_manager_triggers_circuit_breaker_on_daily_loss_limit() -> None:
    """A same-day loss breach should activate the circuit breaker even without an all-time peak-drawdown breach."""
    bar = OHLCVBar(
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
    breaker = CircuitBreaker()
    # hard_stop_drawdown_pct is wide open here so only the daily-loss check should trip.
    manager = RiskManager(RiskControlConfig(hard_stop_drawdown_pct=0.5, daily_loss_limit_pct=0.03))
    decision = manager.evaluate(
        bars=[bar],
        equity=95.0,
        peak_equity=95.0,
        daily_reference_equity=100.0,
        circuit_breaker=breaker,
    )

    assert not decision.allow_entry
    assert decision.reason == "daily_loss_limit"
    assert breaker.is_active()
    assert breaker.state.reason == "daily_loss_limit"


def test_risk_manager_evaluate_exit_triggers_time_stop() -> None:
    """A position held past the configured bar count should be force-closed."""
    bar = OHLCVBar(
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
    manager = RiskManager(RiskControlConfig(time_stop_bars=3))

    decision = manager.evaluate_exit(
        bars=[bar],
        current_bar=bar,
        position_side="long",
        avg_entry_price=100.0,
        bars_held=3,
    )

    assert decision.force_exit is True
    assert decision.reason == "time_stop"


def test_risk_manager_evaluate_exit_does_not_trigger_before_time_stop() -> None:
    """A position under the configured bar count should not be force-closed."""
    bar = OHLCVBar(
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
    manager = RiskManager(RiskControlConfig(time_stop_bars=3))

    decision = manager.evaluate_exit(
        bars=[bar],
        current_bar=bar,
        position_side="long",
        avg_entry_price=100.0,
        bars_held=2,
    )

    assert decision.force_exit is False


def test_risk_manager_evaluate_exit_triggers_atr_stop_loss() -> None:
    """A long position that has fallen past the ATR-based stop distance should be force-closed."""
    bars = [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/NOK",
            interval_seconds=60,
            timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
            open=100.0,
            high=102.0,
            low=98.0,
            close=100.0,
            volume=10.0,
        )
        for index in range(5)
    ]
    # ATR over this window is ~4 (high-low range each bar). With a 1x multiplier,
    # a stop_distance of ~4 below the 100.0 entry means 90.0 must trigger it.
    current_bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc),
        open=95.0,
        high=95.0,
        low=90.0,
        close=90.0,
        volume=10.0,
    )
    manager = RiskManager(RiskControlConfig(atr_stop_multiplier=1.0, atr_window=5))

    decision = manager.evaluate_exit(
        bars=bars + [current_bar],
        current_bar=current_bar,
        position_side="long",
        avg_entry_price=100.0,
        bars_held=5,
    )

    assert decision.force_exit is True
    assert decision.reason == "atr_stop_loss"


def test_risk_manager_evaluate_exit_triggers_position_drawdown_stop_on_long() -> None:
    """A long down 5% from entry should be force-closed by the plain percentage stop."""
    bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=95.0,
        high=95.0,
        low=95.0,
        close=95.0,
        volume=10.0,
    )
    manager = RiskManager(RiskControlConfig(position_drawdown_stop_pct=0.05))

    decision = manager.evaluate_exit(
        bars=[bar],
        current_bar=bar,
        position_side="long",
        avg_entry_price=100.0,
        bars_held=1,
    )

    assert decision.force_exit is True
    assert decision.reason == "position_drawdown_stop"


def test_risk_manager_evaluate_exit_triggers_position_drawdown_stop_on_short() -> None:
    """A short up 5% from entry (adverse for a short) should be force-closed."""
    bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=105.0,
        high=105.0,
        low=105.0,
        close=105.0,
        volume=10.0,
    )
    manager = RiskManager(RiskControlConfig(position_drawdown_stop_pct=0.05))

    decision = manager.evaluate_exit(
        bars=[bar],
        current_bar=bar,
        position_side="short",
        avg_entry_price=100.0,
        bars_held=1,
    )

    assert decision.force_exit is True
    assert decision.reason == "position_drawdown_stop"


def test_risk_manager_evaluate_exit_does_not_trigger_drawdown_stop_within_budget() -> None:
    """A short move that hasn't reached the drawdown threshold should not be force-closed."""
    bar = OHLCVBar(
        exchange="mock",
        symbol="BTC/NOK",
        interval_seconds=60,
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=103.0,
        high=103.0,
        low=103.0,
        close=103.0,
        volume=10.0,
    )
    manager = RiskManager(RiskControlConfig(position_drawdown_stop_pct=0.05))

    decision = manager.evaluate_exit(
        bars=[bar],
        current_bar=bar,
        position_side="short",
        avg_entry_price=100.0,
        bars_held=1,
    )

    assert decision.force_exit is False


def test_risk_manager_evaluate_exit_ignores_flat_position() -> None:
    """A flat position (no side, no entry price) should never be force-closed."""
    bar = OHLCVBar(
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
    manager = RiskManager(RiskControlConfig(time_stop_bars=1, atr_stop_multiplier=0.01))

    decision = manager.evaluate_exit(
        bars=[bar],
        current_bar=bar,
        position_side=None,
        avg_entry_price=None,
        bars_held=5,
    )

    assert decision.force_exit is False


def test_kill_switch_controller_cancels_open_orders(tmp_path) -> None:
    """The kill switch should cancel outstanding orders and record its state."""

    class DummyAdapter:
        """Represent a DummyAdapter."""
        def __init__(self) -> None:
            """Initialize the object with its runtime state."""
            self._orders = [SimpleNamespace(order_id="order-1", status="OPEN")]

        def list_orders(self) -> list[SimpleNamespace]:
            """Return the tracked orders for this adapter."""
            return self._orders

        def cancel_order(self, *, order_id: str) -> SimpleNamespace:
            """Cancel an existing order and return the execution outcome."""
            self._orders[0].status = "CANCELED"
            return SimpleNamespace(status="CANCELED")

        def get_account_snapshot(self) -> dict[str, object]:
            """Return the current account balance and position snapshot."""
            return {"balances": {"USD": 1000.0}, "positions": {}}

    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")
    state = controller.activate("manual", execution_adapter=DummyAdapter())

    assert state["active"] is True
    assert state["orders_cancelled"][0]["status"] == "CANCELED"


def test_kill_switch_controller_ensure_ready_persists_default_state(tmp_path) -> None:
    """Ensuring readiness should create an inactive persisted kill-switch state."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")

    readiness = controller.ensure_ready()

    assert readiness["ready"] is True
    assert readiness["active"] is False
    assert controller.state_file.exists() is True


def test_kill_switch_controller_records_cancel_failures(tmp_path) -> None:
    """Cancel failures should still be captured in the persisted kill-switch state."""

    class DummyAdapter:
        def __init__(self) -> None:
            self._orders = [SimpleNamespace(order_id="order-1", status="OPEN")]

        def list_orders(self) -> list[SimpleNamespace]:
            return self._orders

        def cancel_order(self, *, order_id: str) -> SimpleNamespace:
            return SimpleNamespace(status="REJECTED")

        def get_account_snapshot(self) -> dict[str, object]:
            return {"balances": {"USD": 1000.0}, "positions": {}}

    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")

    state = controller.activate("manual", execution_adapter=DummyAdapter())

    assert state["active"] is True
    assert state["orders_cancelled"][0]["status"] == "REJECTED"


def test_kill_switch_controller_preview_activation_reports_open_orders_without_mutating_state(tmp_path) -> None:
    """Preview should show what the kill switch would cancel without persisting an active state."""

    class DummyAdapter:
        def __init__(self) -> None:
            self._orders = [
                SimpleNamespace(order_id="remote-1", remote_order_id="kraken-1", status="OPEN", exchange="kraken"),
                SimpleNamespace(order_id="filled-1", remote_order_id="kraken-2", status="FILLED", exchange="kraken"),
            ]

        def list_orders(self) -> list[SimpleNamespace]:
            return self._orders

        def get_account_snapshot(self) -> dict[str, object]:
            return {"balances": {"EUR": 1000.0}, "positions": {}}

    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")

    preview = controller.preview_activation(execution_adapter=DummyAdapter())

    assert preview["active"] is False
    assert preview["order_count"] == 1
    assert preview["orders_to_cancel"][0]["order_id"] == "remote-1"
    assert preview["orders_to_cancel"][0]["remote_order_id"] == "kraken-1"
    assert controller.get_state()["active"] is False


def test_simple_backtester_respects_risk_manager() -> None:
    """The backtester should skip entries when the risk manager blocks them."""

    def strategy(history: list[OHLCVBar], index: int, current_bar: OHLCVBar) -> int:
        """Generate the signal strategy output for the current market context."""
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


def test_allowed_entries_are_not_sized_to_zero_after_a_falling_stretch() -> None:
    """Regression: the market-return Kelly multiplier returned 0 after mostly-down bars, so 'allowed' entries got size 0.

    It was on by default and ignored trade direction; it is now opt-in via kelly_sizing.
    """
    from datetime import datetime, timedelta, timezone

    from src.risk.controls import RiskControlConfig, RiskManager
    from src.storage.bar_aggregator import OHLCVBar

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = [100.0]
    for i in range(1, 30):
        closes.append(closes[-1] + (0.2 if i % 4 == 0 else -0.3))  # a few small up-bars, mostly bigger down-bars
    falling = [
        OHLCVBar(exchange="kraken", symbol="BTC/EUR", interval_seconds=14400, timestamp=start + timedelta(hours=4 * i), open=close, high=close + 0.5, low=close - 0.5, close=close, volume=1.0)
        for i, close in enumerate(closes)
    ]
    kwargs = dict(bars=falling, equity=1000.0, peak_equity=1000.0, current_position=0.0, current_bar=falling[-1], bar_index=29, signal_side="buy")

    default = RiskManager(RiskControlConfig(risk_per_trade_pct=0.10, max_volatility_pct=0.5, paper_mode=True)).evaluate(**kwargs)
    assert default.allow_entry and default.position_size > 0.0

    with_kelly = RiskManager(RiskControlConfig(risk_per_trade_pct=0.10, max_volatility_pct=0.5, paper_mode=True, kelly_sizing=True)).evaluate(**kwargs)
    assert with_kelly.allow_entry and with_kelly.position_size == 0.0  # the old behaviour, still available on request
