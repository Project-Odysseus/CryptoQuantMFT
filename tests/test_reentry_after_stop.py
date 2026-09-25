"""After a risk stop closes a position, the same side is not re-entered until the signal has left it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backtest.simple_backtest import SimpleBacktester
from src.execution import PaperTradingEngine
from src.execution.adapters import SandboxExecutionAdapter
from src.risk.controls import RiskControlConfig, RiskManager, gate_reentry
from src.storage.bar_aggregator import OHLCVBar

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Price falls 6% (the stop fires at 5%), then recovers while the signal stays long, then the signal resets.
PRICES = [100.0, 100.0, 100.0, 94.0, 95.0, 96.0, 97.0, 98.0, 99.0, 99.0]
SIGNALS = [1, 1, 1, 1, 1, 1, 0, 1, 1, 1]


def _bars() -> list[OHLCVBar]:
    return [OHLCVBar(exchange="kraken", symbol="BTC/EUR", interval_seconds=60, timestamp=T0 + timedelta(minutes=i), open=p, high=p, low=p, close=p, volume=1.0) for i, p in enumerate(PRICES)]


def _stop_only() -> RiskManager:
    # Only the per-position 5% stop is in play; the account-level drawdown stops are set out of reach.
    return RiskManager(RiskControlConfig(position_drawdown_stop_pct=0.05, hard_stop_drawdown_pct=0.9, max_drawdown_pct=0.9, risk_per_trade_pct=0.5, max_volatility_pct=1.0, paper_mode=True))


def test_gate_rules() -> None:
    """Same-side signals are suppressed until the signal leaves that side; the opposite side is never blocked."""
    assert gate_reentry(1.0, "long") == (0.0, "long", True)
    assert gate_reentry(0.0, "long") == (0.0, None, False)
    assert gate_reentry(-1.0, "long") == (-1.0, None, False)
    assert gate_reentry(-1.0, "short") == (0.0, "short", True)
    assert gate_reentry(1.0, None) == (1.0, None, False)


def test_backtester_waits_for_the_signal_to_reset_after_a_stop() -> None:
    """Stopped out at 94 (bar 3); no re-entry at 95/96 while the signal stays long; re-entry at 98 after the reset."""
    signals = iter(SIGNALS[1:])
    result = SimpleBacktester(strategy=lambda h, i, b: next(signals), risk_manager=_stop_only()).run(_bars())

    assert result.trade_records[0].reason == "position_drawdown_stop"
    assert result.trade_records[0].exit_price == 94.0
    assert result.position_series[3:7] == [0.0, 0.0, 0.0, 0.0]  # flat through 95, 96 and the reset bar
    assert result.position_series[7] > 0.0
    assert result.trade_records[1].entry_price == 98.0


def test_paper_engine_waits_for_the_signal_to_reset_after_a_stop() -> None:
    """Same rule in PaperTradingEngine.run(): orders are stop exit, then nothing until the signal resets."""
    engine = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, partial_fill_fraction=1.0, max_order_lifetime_bars=5, risk_manager=_stop_only())
    result = engine.run(_bars(), [float(s) for s in SIGNALS])
    stop_index = next(i for i, order in enumerate(result.orders) if order.last_reason == "position_drawdown_stop")
    later_buys = [order for order in result.orders[stop_index + 1 :] if order.side == "buy"]
    assert later_buys and later_buys[0].timestamp >= T0 + timedelta(minutes=7)


def test_exchange_cycle_blocks_reentry_and_says_why() -> None:
    """In live-style cycles the blocked entry is recorded with reason reentry_after_forced_exit."""
    adapter = SandboxExecutionAdapter(exchange_name="kraken")
    adapter._balances = {"EUR": 1000.0}
    engine = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, execution_adapter=adapter, exchange_name="kraken", risk_manager=_stop_only())
    bars = _bars()
    reasons = []
    for i in range(1, len(bars) + 1):
        result = engine.run_exchange_cycle(bars[:i], [float(s) for s in SIGNALS[:i]])
        reasons.append([d["reason"] for d in result.entry_decisions])
        if i == 4:
            assert adapter.get_account_snapshot()["positions"].get("BTC", 0.0) == 0.0  # stopped out at 94
    assert reasons[4] == ["reentry_after_forced_exit"] and reasons[5] == ["reentry_after_forced_exit"]
    assert adapter.get_account_snapshot()["positions"].get("BTC", 0.0) > 0.0  # re-entered after the reset
