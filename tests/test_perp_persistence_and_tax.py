"""Tests for perp sandbox persistence and perpetual-futures entries in the tax ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.fx import FXRateCollector
from src.execution import PaperTradingEngine
from src.execution.perps import SandboxPerpExecutionAdapter, assumed_perp_contract
from src.storage.bar_aggregator import OHLCVBar
from src.storage.trade_logger import TradeLogger

T0 = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
RATES = {"EUR/NOK": 11.5, "USD/NOK": 10.0}


def _bar(minute: int, price: float) -> OHLCVBar:
    return OHLCVBar(exchange="kraken_futures", symbol="BTC/USD", interval_seconds=60, timestamp=T0 + timedelta(minutes=minute), open=price, high=price, low=price, close=price, volume=1.0)


def _adapter(state_path: Path | None = None, funding: float = 0.0) -> SandboxPerpExecutionAdapter:
    return SandboxPerpExecutionAdapter(contract=assumed_perp_contract("BTC/USD"), starting_collateral=1000.0, max_leverage=2.0, funding_pct_per_day=funding, state_path=state_path)


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TradeLogger:
    monkeypatch.setattr(FXRateCollector, "get_rate", lambda self, pair="EUR/NOK", at=None: RATES[pair.upper()])
    return TradeLogger(database_path=str(tmp_path / "ledger.db"))


def test_sandbox_account_survives_a_restart(tmp_path: Path) -> None:
    """Wallet, position, entry price, open time and totals come back from the state file."""
    path = tmp_path / "perp.json"
    first = _adapter(path, funding=0.1)
    first.on_market_update(symbol="BTC/USD", mark_price=50000.0, timestamp=T0)
    first.submit_order(order_id="o1", side="buy", size=0.01, price=50000.0, timestamp=T0, symbol="BTC/USD")
    first.on_market_update(symbol="BTC/USD", mark_price=51000.0, timestamp=T0 + timedelta(hours=12))

    second = _adapter(path, funding=0.1)
    assert second.restored_from_state
    assert second.wallet_balance() == pytest.approx(first.wallet_balance())
    assert second.position_size() == pytest.approx(0.01)
    assert second._position_entry_price["BTC"] == 50000.0
    assert second._position_opened_at["BTC"] == T0
    assert second.funding_paid_total == pytest.approx(first.funding_paid_total) and second.funding_paid_total > 0.0
    # Funding keeps accruing from the saved update time, not from zero.
    second.on_market_update(symbol="BTC/USD", mark_price=51000.0, timestamp=T0 + timedelta(hours=24))
    assert second.funding_paid_total == pytest.approx(2 * first.funding_paid_total, rel=0.05)


def test_state_for_another_contract_is_refused(tmp_path: Path) -> None:
    """A state file must not be silently applied to a different contract."""
    path = tmp_path / "perp.json"
    _adapter(path).submit_order(order_id="o1", side="buy", size=0.01, price=50000.0, timestamp=T0, symbol="BTC/USD")
    with pytest.raises(ValueError, match="move it aside"):
        SandboxPerpExecutionAdapter(contract=assumed_perp_contract("ETH/USD"), state_path=path)


def test_runtime_reset_moves_the_old_state_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--perp-sandbox-reset starts fresh but keeps the previous file as a backup."""
    import main

    monkeypatch.setattr(main, "PERP_SANDBOX_STATE_DIR", tmp_path / "data")
    monkeypatch.setattr("src.data.kraken_futures.fetch_instrument", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    adapter = main._build_perp_adapter(mode="live_dry_run", symbol="BTC/USD", max_leverage=2.0)
    adapter.submit_order(order_id="o1", side="buy", size=0.01, price=50000.0, timestamp=T0, symbol="BTC/USD")
    assert main._build_perp_adapter(mode="live_dry_run", symbol="BTC/USD", max_leverage=2.0).restored_from_state

    fresh = main._build_perp_adapter(mode="live_dry_run", symbol="BTC/USD", max_leverage=2.0, reset_sandbox=True)
    assert not fresh.restored_from_state and fresh.position_size() == 0.0
    assert list((tmp_path / "data").glob("perp_sandbox_*.bak.json"))


def test_engine_books_realized_pnl_fees_and_funding_in_nok(ledger: TradeLogger) -> None:
    """Each cycle's realized PnL, fee and funding become tax rows valued at USD/NOK, with a EUR equivalent."""
    adapter = _adapter(funding=0.1)
    engine = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, execution_adapter=adapter, exchange_name="kraken_futures", allow_short=True, trade_logger=ledger, record_derivative_ledger=True)
    engine.run_exchange_cycle([_bar(0, 50000.0)], [1.0])
    bars = [_bar(0, 50000.0), _bar(720, 55000.0)]
    engine.run_exchange_cycle(bars, [1.0, 1.0])
    engine.run_exchange_cycle(bars + [_bar(721, 55000.0)], [1.0, 1.0, 0.0])

    events = ledger.list_tax_events(tax_year=2026)
    by_type: dict[str, float] = {}
    for event in events:
        assert event["metadata"]["instrument"] == "perpetual" and event["symbol"] == adapter.contract.venue_symbol
        assert event["amount_eur"] == pytest.approx(event["amount_nok"] / 11.5)
        by_type[event["transaction_type"]] = by_type.get(event["transaction_type"], 0.0) + event["amount_nok"]

    assert by_type["REALIZED_PNL"] == pytest.approx(adapter.realized_pnl_total * 10.0)
    assert by_type["TRADING_FEE"] == pytest.approx(-adapter.fees_paid_total * 10.0)
    assert by_type["FUNDING_FEE"] == pytest.approx(-adapter.funding_paid_total * 10.0)
    assert adapter.realized_pnl_total > 0.0 and adapter.funding_paid_total > 0.0

    derivatives = ledger.get_tax_year_summary(2026)["derivatives"]
    assert derivatives["realized_gains_nok"] == pytest.approx(adapter.realized_pnl_total * 10.0)
    assert derivatives["fees_nok"] == pytest.approx(adapter.fees_paid_total * 10.0)


def test_totals_restored_from_state_are_not_booked_again(tmp_path: Path, ledger: TradeLogger) -> None:
    """After a restart the first cycle only sets a baseline, so earlier profit isn't double counted."""
    path = tmp_path / "perp.json"
    first = _adapter(path)
    first.submit_order(order_id="o1", side="buy", size=0.01, price=50000.0, timestamp=T0, symbol="BTC/USD")
    first.submit_order(order_id="o2", side="sell", size=0.01, price=52000.0, timestamp=T0, symbol="BTC/USD")
    assert first.realized_pnl_total > 0.0

    restarted = _adapter(path)
    engine = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, execution_adapter=restarted, exchange_name="kraken_futures", trade_logger=ledger, record_derivative_ledger=True)
    engine.run_exchange_cycle([_bar(0, 52000.0)], [0.0])
    assert ledger.list_tax_events(tax_year=2026) == []


def test_ledger_is_off_unless_requested(ledger: TradeLogger) -> None:
    """Dry runs must not write tax rows."""
    adapter = _adapter()
    engine = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, execution_adapter=adapter, exchange_name="kraken_futures", trade_logger=ledger)
    engine.run_exchange_cycle([_bar(0, 50000.0)], [1.0])
    engine.run_exchange_cycle([_bar(0, 50000.0), _bar(1, 51000.0)], [1.0, 0.0])
    assert ledger.list_tax_events(tax_year=2026) == []


def test_usd_nok_has_no_silent_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a Norges Bank USD/NOK rate the lookup fails loudly instead of inventing one."""
    collector = FXRateCollector(cache_path=tmp_path / "fx.db")
    monkeypatch.setattr(collector, "_fetch_rate_for_date", lambda pair, target_date: None)
    with pytest.raises(LookupError, match="USD/NOK"):
        collector.get_rate("USD/NOK", at=T0)
    assert collector.get_rate("EUR/NOK", at=T0) > 0.0  # EUR keeps its configured fallback
    with pytest.raises(ValueError):
        collector.get_rate("GBP/NOK", at=T0)


def test_derivative_event_rejects_unknown_types(ledger: TradeLogger) -> None:
    """Only the three perp cash-flow types are accepted."""
    with pytest.raises(ValueError, match="unsupported"):
        ledger.log_derivative_event(timestamp=T0, venue_symbol="PF_XBTUSD", transaction_type="BUY", amount=1.0)
