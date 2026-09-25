"""Tests for the perpetual-futures sandbox account, its engine integration and its safety gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.execution import ExecutionRouter, PaperTradingEngine
from src.execution.perps import (
    PerpContract,
    SandboxPerpExecutionAdapter,
    assumed_perp_contract,
    initial_margin,
    liquidation_price,
    maintenance_margin,
    unrealized_pnl,
)
from src.risk.controls import RiskControlConfig, RiskManager
from src.storage.bar_aggregator import OHLCVBar

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _adapter(*, collateral: float = 1000.0, leverage: float = 2.0, funding: float = 0.0) -> SandboxPerpExecutionAdapter:
    return SandboxPerpExecutionAdapter(contract=assumed_perp_contract("BTC/EUR"), starting_collateral=collateral, max_leverage=leverage, funding_pct_per_day=funding)


def _order(adapter: SandboxPerpExecutionAdapter, order_id: str, side: str, size: float, price: float, minutes: int = 0):
    return adapter.submit_order(order_id=order_id, side=side, size=size, price=price, timestamp=T0 + timedelta(minutes=minutes), symbol="BTC/EUR")


def _bar(minute: int, price: float, *, low: float | None = None, high: float | None = None) -> OHLCVBar:
    return OHLCVBar(
        exchange="kraken",
        symbol="BTC/EUR",
        interval_seconds=60,
        timestamp=T0 + timedelta(minutes=minute),
        open=price,
        high=high if high is not None else price,
        low=low if low is not None else price,
        close=price,
        volume=10.0,
    )


def test_contract_rejects_specs_that_cannot_exist() -> None:
    """Maintenance margin must sit below the initial margin rate or every position starts liquidatable."""
    good = assumed_perp_contract("ETH/EUR")
    assert good.base_asset == "ETH" and good.collateral_currency == "EUR" and not good.verified
    with pytest.raises(ValueError, match="maintenance_margin_rate"):
        PerpContract(symbol="X/USD", venue_symbol="X", base_asset="X", collateral_currency="USD", size_step=1, min_size=1, max_leverage=10.0, maintenance_margin_rate=0.2, taker_fee_rate=0.0, maker_fee_rate=0.0)
    with pytest.raises(ValueError, match="size_step"):
        PerpContract(symbol="X/USD", venue_symbol="X", base_asset="X", collateral_currency="USD", size_step=0.0, min_size=0.0, max_leverage=2.0, maintenance_margin_rate=0.01, taker_fee_rate=0.0, maker_fee_rate=0.0)


def test_margin_formulas() -> None:
    """Unrealized PnL, margins and liquidation prices for a long and a short, checked against the equity definition."""
    assert unrealized_pnl(2.0, 100.0, 110.0) == 20.0
    assert unrealized_pnl(-2.0, 100.0, 110.0) == -20.0
    assert initial_margin(-2.0, 100.0, 5.0) == 40.0
    assert maintenance_margin(-2.0, 100.0, 0.01) == 2.0

    long_liq = liquidation_price(1.0, 100.0, 20.0, 0.01)
    assert long_liq == pytest.approx(80.8081, abs=1e-3)
    assert 20.0 + unrealized_pnl(1.0, 100.0, long_liq) == pytest.approx(maintenance_margin(1.0, long_liq, 0.01))

    short_liq = liquidation_price(-1.0, 100.0, 20.0, 0.01)
    assert short_liq == pytest.approx(118.8119, abs=1e-3)
    assert 20.0 + unrealized_pnl(-1.0, 100.0, short_liq) == pytest.approx(maintenance_margin(-1.0, short_liq, 0.01))

    assert liquidation_price(0.0, 100.0, 20.0, 0.01) is None
    assert liquidation_price(1.0, 100.0, 100.0, 0.01) is None  # fully collateralised long cannot be liquidated by a falling price


def test_opening_a_position_moves_no_cash_except_the_fee() -> None:
    """A perp fill locks margin instead of paying notional: only the fee leaves the wallet."""
    adapter = _adapter()
    report = _order(adapter, "o1", "buy", 0.01, 50000.0)

    assert report.status == "FILLED"
    assert report.fee == pytest.approx(0.01 * 50000.0 * 0.0005)
    assert adapter.wallet_balance() == pytest.approx(1000.0 - report.fee)
    assert adapter.position_size() == pytest.approx(0.01)
    assert adapter.used_initial_margin() == pytest.approx(0.01 * 50000.0 / 2.0)
    assert adapter.equity() == pytest.approx(1000.0 - report.fee)


def test_pnl_is_realized_on_close_and_unrealized_while_open() -> None:
    """Equity tracks the mark while open, and the wallet only changes when the position is reduced."""
    adapter = _adapter()
    _order(adapter, "o1", "buy", 0.01, 50000.0)
    open_fee = adapter.fees_paid_total

    adapter.on_market_update(symbol="BTC/EUR", mark_price=52000.0, timestamp=T0 + timedelta(minutes=1))
    assert adapter.equity() == pytest.approx(1000.0 - open_fee + 0.01 * 2000.0)
    assert adapter.wallet_balance() == pytest.approx(1000.0 - open_fee)

    _order(adapter, "o2", "sell", 0.01, 52000.0, minutes=2)
    assert adapter.position_size() == 0.0
    assert adapter.realized_pnl_total == pytest.approx(20.0)
    assert adapter.wallet_balance() == pytest.approx(1000.0 + 20.0 - adapter.fees_paid_total)
    assert adapter.get_account_snapshot()["liquidation_price"] == {}


def test_short_profits_when_price_falls() -> None:
    """A short realizes a gain on a lower price."""
    adapter = _adapter()
    _order(adapter, "o1", "sell", 0.01, 50000.0)
    _order(adapter, "o2", "buy", 0.01, 48000.0, minutes=1)
    assert adapter.realized_pnl_total == pytest.approx(20.0)


def test_flipping_through_zero_realizes_only_the_closed_part() -> None:
    """Selling more than the long holds closes it and opens a short at the fill price."""
    adapter = _adapter()
    _order(adapter, "o1", "buy", 0.01, 50000.0)
    _order(adapter, "o2", "sell", 0.03, 51000.0, minutes=1)

    assert adapter.position_size() == pytest.approx(-0.02)
    assert adapter.realized_pnl_total == pytest.approx(0.01 * 1000.0)
    assert adapter._position_entry_price["BTC"] == 51000.0


def test_orders_beyond_the_leverage_cap_are_rejected_but_reductions_are_not() -> None:
    """Opening past max leverage is refused; closing is always allowed, even with almost no equity."""
    adapter = _adapter(collateral=100.0, leverage=2.0)
    too_big = _order(adapter, "big", "buy", 0.005, 50000.0)  # 250 notional needs 125 margin, equity is 100
    assert too_big.status == "REJECTED" and "insufficient margin" in (too_big.message or "")
    assert adapter.position_size() == 0.0

    assert _order(adapter, "ok", "buy", 0.003, 50000.0).status == "FILLED"  # 150 notional, 75 margin
    adapter.on_market_update(symbol="BTC/EUR", mark_price=40000.0, timestamp=T0 + timedelta(minutes=1))  # equity now ~ 70
    assert _order(adapter, "more", "buy", 0.001, 40000.0, minutes=2).status == "REJECTED"
    assert _order(adapter, "close", "sell", 0.003, 40000.0, minutes=3).status == "FILLED"


def test_contract_rules_are_enforced() -> None:
    """Sizes round down to the step, sub-minimum orders and other symbols are rejected."""
    adapter = _adapter()
    filled = _order(adapter, "o1", "buy", 0.01239, 50000.0)
    assert filled.filled_size == pytest.approx(0.0123)
    assert _order(adapter, "o2", "buy", 0.00004, 50000.0).status == "REJECTED"
    wrong = adapter.submit_order(order_id="o3", side="buy", size=0.01, price=3000.0, timestamp=T0, symbol="ETH/EUR")
    assert wrong.status == "REJECTED"
    with pytest.raises(ValueError, match="max_leverage"):
        SandboxPerpExecutionAdapter(max_leverage=50.0)


def test_funding_is_paid_by_longs_and_received_by_shorts_in_proportion_to_time() -> None:
    """0.10%/day on 500 notional is 0.50/day: a long pays it, a short receives it."""
    for side, sign in (("buy", -1.0), ("sell", 1.0)):
        adapter = _adapter(funding=0.10)
        adapter.on_market_update(symbol="BTC/EUR", mark_price=50000.0, timestamp=T0)
        _order(adapter, "o1", side, 0.01, 50000.0)
        wallet_after_fee = adapter.wallet_balance()
        events = adapter.on_market_update(symbol="BTC/EUR", mark_price=50000.0, timestamp=T0 + timedelta(hours=12))

        assert [event["type"] for event in events] == ["funding"]
        assert adapter.wallet_balance() - wallet_after_fee == pytest.approx(sign * 0.25)
        assert adapter.funding_paid_total == pytest.approx(-sign * 0.25)


def test_no_funding_accrues_while_flat() -> None:
    """Funding only applies to an open position."""
    adapter = _adapter(funding=0.10)
    adapter.on_market_update(symbol="BTC/EUR", mark_price=50000.0, timestamp=T0)
    assert adapter.on_market_update(symbol="BTC/EUR", mark_price=50000.0, timestamp=T0 + timedelta(days=1)) == []
    assert adapter.wallet_balance() == 1000.0


def test_position_is_liquidated_when_equity_reaches_maintenance_margin() -> None:
    """A 2x long is liquidated on a large enough fall, leaving a flat, non-negative account."""
    adapter = _adapter(collateral=100.0, leverage=2.0)
    _order(adapter, "o1", "buy", 0.0039, 50000.0)  # 195 notional on 100 collateral, just inside 2x after the fee
    liq = adapter.liquidation_price()
    assert liq is not None and 24000.0 < liq < 25000.0  # ~50% below entry for 2x leverage

    assert adapter.on_market_update(symbol="BTC/EUR", mark_price=liq * 1.02, timestamp=T0 + timedelta(minutes=1)) == []
    events = adapter.on_market_update(symbol="BTC/EUR", mark_price=liq * 0.99, timestamp=T0 + timedelta(minutes=2))

    assert [event["type"] for event in events] == ["liquidation"]
    assert adapter.position_size() == 0.0
    assert adapter.wallet_balance() >= 0.0
    assert any(order.order_id.startswith("liquidation-") for order in adapter.list_orders())


def test_a_gap_through_zero_floors_the_wallet_and_reports_bad_debt() -> None:
    """The sandbox has no insurance fund, so a gap that erases all collateral is floored and reported."""
    adapter = _adapter(collateral=100.0, leverage=2.0)
    _order(adapter, "o1", "buy", 0.0039, 50000.0)
    event = adapter.on_market_update(symbol="BTC/EUR", mark_price=20000.0, timestamp=T0 + timedelta(minutes=1))[0]
    assert event["bad_debt"] > 0.0
    assert adapter.wallet_balance() == 0.0


def _engine(adapter: SandboxPerpExecutionAdapter, **risk_kwargs) -> PaperTradingEngine:
    risk = RiskManager(RiskControlConfig(risk_per_trade_pct=0.10, max_notional_per_trade=10000.0, max_total_notional=25000.0, paper_mode=False, **risk_kwargs))
    return PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, execution_adapter=adapter, exchange_name="kraken", allow_short=True, risk_manager=risk)


def test_engine_uses_margin_equity_and_sizes_from_buying_power() -> None:
    """Equity is wallet plus unrealized PnL (not cash plus position value), and entry size fits the account."""
    adapter = _adapter(collateral=1000.0, leverage=2.0)
    engine = _engine(adapter)

    opened = engine.run_exchange_cycle([_bar(0, 50000.0)], [1.0])
    assert [trade.side for trade in opened.trades] == ["buy"]
    size = adapter.position_size()
    assert 0.0 < size <= 1000.0 * 2.0 / 50000.0
    assert opened.portfolio_history[-1].equity == pytest.approx(adapter.equity(), rel=1e-9)
    assert opened.portfolio_history[-1].equity == pytest.approx(1000.0, abs=1.0)  # not ~1000 + notional

    marked = engine.run_exchange_cycle([_bar(0, 50000.0), _bar(1, 55000.0)], [1.0, 1.0])
    assert marked.portfolio_history[-1].unrealized_pnl == pytest.approx(size * 5000.0)
    assert marked.portfolio_history[-1].equity == pytest.approx(1000.0 - adapter.fees_paid_total + size * 5000.0)


def test_engine_closes_a_perp_short_on_signal_zero_and_reports_short_pnl() -> None:
    """Shorts open, show unrealized PnL with the right sign, and close on signal 0."""
    adapter = _adapter()
    engine = _engine(adapter)
    engine.run_exchange_cycle([_bar(0, 50000.0)], [-1.0])
    assert adapter.position_size() < 0.0

    held = engine.run_exchange_cycle([_bar(0, 50000.0), _bar(1, 49000.0)], [-1.0, -1.0])
    assert held.portfolio_history[-1].position_side == "short"
    assert held.portfolio_history[-1].unrealized_pnl > 0.0

    closed = engine.run_exchange_cycle([_bar(0, 50000.0), _bar(1, 49000.0), _bar(2, 49000.0)], [-1.0, -1.0, 0.0])
    assert [trade.side for trade in closed.trades] == ["buy"]
    assert adapter.position_size() == 0.0


def test_position_stop_loss_now_applies_to_shorts_in_exchange_cycles() -> None:
    """Regression: exchange cycles returned no entry price for shorts, so drawdown/time/ATR stops never fired on them."""
    adapter = _adapter()
    engine = _engine(adapter, position_drawdown_stop_pct=0.05)
    engine.run_exchange_cycle([_bar(0, 50000.0)], [-1.0])
    assert adapter.position_size() < 0.0

    # The signal still says short, so only the stop can close it: +6% against the short.
    result = engine.run_exchange_cycle([_bar(0, 50000.0), _bar(1, 53000.0)], [-1.0, -1.0])
    assert adapter.position_size() == 0.0
    assert [trade.side for trade in result.trades] == ["buy"]


def test_liquidation_buffer_forces_an_exit_before_the_venue_liquidates() -> None:
    """With a buffer configured, the position is cut while still some distance from its liquidation price."""
    adapter = _adapter(collateral=1000.0, leverage=5.0)
    engine = _engine(adapter, liquidation_buffer_pct=0.05)
    engine.risk_manager.config.risk_per_trade_pct = 4.0  # oversize on purpose: a ~4x position sits close to liquidation
    engine.run_exchange_cycle([_bar(0, 50000.0)], [1.0])
    liq = adapter.liquidation_price()
    assert liq is not None and adapter.position_size() > 0.0

    near = liq * 1.03  # within 5% of the liquidation price but above it
    result = engine.run_exchange_cycle([_bar(0, 50000.0), _bar(1, near)], [1.0, 1.0])
    assert adapter.position_size() == 0.0
    assert [trade.side for trade in result.trades] == ["sell"]
    assert not any(order.order_id.startswith("liquidation-") for order in adapter.list_orders())  # we exited first


def test_engine_logs_funding_and_liquidation_events(tmp_path) -> None:
    """Funding and liquidations the account reports end up in the operational event log."""
    from src.storage.trade_logger import TradeLogger

    logger = TradeLogger(database_path=str(tmp_path / "perp.db"))
    adapter = _adapter(collateral=100.0, leverage=2.0, funding=0.10)
    engine = _engine(adapter)
    engine.trade_logger = logger
    _order(adapter, "seed", "buy", 0.0039, 50000.0)
    engine.run_exchange_cycle([_bar(0, 50000.0)], [1.0])
    engine.run_exchange_cycle([_bar(0, 50000.0), _bar(600, 50000.0)], [1.0, 1.0])
    engine.run_exchange_cycle([_bar(0, 50000.0), _bar(600, 50000.0), _bar(601, 20000.0)], [1.0, 1.0, 1.0])

    types = {event["event_type"] for event in logger.list_events(50)}
    assert {"funding_accrued", "position_liquidated"} <= types


def test_router_builds_a_perp_sandbox_for_dry_runs_and_refuses_live() -> None:
    """There is no real futures adapter, so `live` must fail loudly instead of falling back to something else."""
    dry = ExecutionRouter(mode="live_dry_run", exchange="kraken_futures")
    assert isinstance(dry.adapter, SandboxPerpExecutionAdapter)
    with pytest.raises(ValueError, match="explicit KrakenFuturesExecutionAdapter"):
        ExecutionRouter(mode="live", exchange="kraken_futures")


def test_runtime_refuses_futures_in_paper_mode() -> None:
    """Paper mode has no margin account, so kraken_futures is rejected before anything is built."""
    from main import build_runtime_orchestrator

    with pytest.raises(SystemExit, match="live_dry_run or live"):
        build_runtime_orchestrator(mode="paper", exchange="kraken_futures", use_mock_connector=True)


def test_runtime_builds_a_perp_dry_run_with_a_margin_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """--execution-exchange kraken_futures in live_dry_run wires the margin sandbox and liquidation buffer."""
    from main import build_runtime_orchestrator

    def offline(*args, **kwargs):
        raise RuntimeError("offline in tests")

    monkeypatch.setattr("src.data.kraken_futures.fetch_instrument", offline)

    orchestrator, _pipeline = build_runtime_orchestrator(mode="live_dry_run", exchange="kraken_futures", use_mock_connector=True, perp_max_leverage=3.0)
    adapter = orchestrator.execution_engine.execution_adapter
    assert isinstance(adapter, SandboxPerpExecutionAdapter)
    assert adapter.max_leverage == 3.0
    assert orchestrator.execution_engine.risk_manager.config.liquidation_buffer_pct == 0.10
    assert orchestrator.execution_engine.enable_tax_logging is False


def test_orchestrator_dashboard_reports_margin_equity_not_cash_plus_notional() -> None:
    """Regression: the runtime snapshot refresh used the spot formula (cash + size * price) for perps too."""
    from types import SimpleNamespace

    from src.execution.paper_trading import PortfolioSnapshot
    from src.runtime.orchestrator import RuntimeOrchestrator

    adapter = _adapter()
    engine = _engine(adapter)
    engine.run_exchange_cycle([_bar(0, 50000.0)], [1.0])
    size = adapter.position_size()
    snapshot = PortfolioSnapshot(timestamp=T0, cash=adapter.wallet_balance(), position_size=size, avg_entry_price=50000.0, equity=0.0, unrealized_pnl=0.0, realized_pnl=0.0, position_side="long", mark_price=50000.0, fees_paid=0.0)
    cycle = SimpleNamespace(execution_result=SimpleNamespace(portfolio_history=[snapshot]), snapshots=[SimpleNamespace(last=51000.0)], bars=[])
    fake_orchestrator = SimpleNamespace(execution_engine=engine, _resolve_latest_market_price=lambda cycle: 51000.0)

    RuntimeOrchestrator._refresh_latest_snapshot_prices(fake_orchestrator, cycle)
    assert snapshot.equity == pytest.approx(adapter.wallet_balance() + size * 1000.0)
