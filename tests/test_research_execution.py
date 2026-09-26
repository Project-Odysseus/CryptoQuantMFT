"""Tests for the research fill simulator (taker at the close vs resting limit orders)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.simple_backtest import SimpleBacktester
from src.research import CostSettings, FillModel, run_strategy, simulate_fills
from src.storage.bar_aggregator import OHLCVBar

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(rows: list[tuple[float, float, float]]) -> list[OHLCVBar]:
    """rows of (low, high, close)."""
    return [OHLCVBar(exchange="m", symbol="X/USD", interval_seconds=3600, timestamp=T0 + timedelta(hours=i), open=c, high=h, low=l, close=c, volume=1.0) for i, (l, h, c) in enumerate(rows)]


def _run(rows, targets, fills, **fees):
    kwargs = {"taker_fee_pct": 0.0, "maker_fee_pct": 0.0} | fees
    return simulate_fills(_bars(rows), targets, fills=fills, **kwargs)


def test_close_style_matches_the_classic_backtester_for_long_only_targets() -> None:
    """With no costs, filling at the signal close reproduces SimpleBacktester's equity curve."""
    rng = np.random.default_rng(0)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    rows = [(c * 0.995, c * 1.005, c) for c in closes]
    targets = (np.sin(np.arange(200) / 7.0) > 0).astype(float)
    ours, _ = _run(rows, targets, FillModel())
    classic = SimpleBacktester(strategy=lambda h, i, b: 0, initial_equity=1000.0)
    classic.strategy.signal_series = lambda bars: targets  # type: ignore[attr-defined]
    theirs = classic.run(_bars(rows))
    assert ours.mtm_equity_series == pytest.approx(theirs.mtm_equity_series)
    assert ours.trades == theirs.trades


def test_buy_limit_needs_the_market_to_trade_through_it() -> None:
    """Touching the limit is not enough; the low must go past it by through_bps."""
    fills = FillModel.maker(max_wait_bars=3, through_bps=10.0)
    touch = [(100, 101, 100), (100.0, 102, 101), (100.5, 103, 102), (101, 104, 103)]  # lows never below 100 - 10bps
    result, stats = _run(touch, [1, 1, 1, 1], fills)
    assert stats.maker_fills == 0 and stats.cancelled == 1 and result.trades == 0

    through = [(100, 101, 100), (99.8, 102, 101), (100.5, 103, 102), (101, 104, 103)]  # 99.8 <= 100 * (1 - 0.001)
    result, stats = _run(through, [1, 1, 1, 1], fills)
    assert stats.maker_fills == 1 and result.trade_records[0].entry_price == 100.0


def test_missed_trade_when_price_runs_away_is_the_cost_of_a_limit_order() -> None:
    """A rally that never dips back leaves a maker order unfilled, while a taker would have caught it."""
    rally = [(100, 100, 100), (100.5, 102, 102), (102, 104, 104), (104, 106, 106)]
    taker, _ = _run(rally, [1, 1, 1, 1], FillModel())
    maker, stats = _run(rally, [1, 1, 1, 1], FillModel.maker(max_wait_bars=1))
    assert taker.mtm_equity_series[-1] > 1050.0
    assert maker.mtm_equity_series[-1] == 1000.0 and stats.cancelled == 1 and stats.orders == 1  # gave up, didn't chase


def test_timeout_actions() -> None:
    """requote moves the limit to the newest close; taker crosses at the timeout bar's close with slippage."""
    rows = [(100, 100, 100), (100.5, 101, 101), (100.2, 102, 101.5), (100.9, 103, 102)]
    requote, rstats = _run(rows, [1, 1, 1, 1], FillModel.maker(max_wait_bars=1, on_timeout="requote", through_bps=0.0))
    assert rstats.maker_fills == 1 and requote.trade_records[0].entry_price == 101.0  # requoted at bar 1's close, filled in bar 2
    chase, cstats = _run(rows, [1, 1, 1, 1], FillModel.maker(max_wait_bars=1, on_timeout="taker"), slippage_bps=10.0)
    assert cstats.taker_fills >= 1 and chase.trade_records[0].entry_price == pytest.approx(101.0 * 1.001)


def test_fees_follow_the_fill_type_and_trades_record_it() -> None:
    """Maker fills pay the maker fee, the forced final exit pays taker, and the record says which."""
    rows = [(100, 100, 100), (99.0, 100, 99.5), (99.5, 101, 100.5), (100, 101, 100.5)]
    result, _ = _run(rows, [1, 1, 1, 1], FillModel.maker(max_wait_bars=2), taker_fee_pct=0.05, maker_fee_pct=0.02)
    trade = result.trade_records[0]
    assert trade.reason == "maker+taker"
    assert trade.cost == pytest.approx(1000.0 * 0.0002 + 1000.0 / 100.0 * 100.5 * 0.0005, rel=1e-6)


def test_funding_is_charged_on_held_notional() -> None:
    """Holding a long through bars pays funding; flat pays nothing."""
    rows = [(100, 100, 100)] * 25
    paid, _ = _run(rows, [1] * 25, FillModel(), funding_pct_per_day=0.24)  # 0.01% per hourly bar
    flat, _ = _run(rows, [0] * 25, FillModel(), funding_pct_per_day=0.24)
    assert flat.mtm_equity_series[-1] == 1000.0
    # Charged at each close the position is held into: bars 0..23 before the forced exit at bar 24.
    assert paid.mtm_equity_series[-2] == pytest.approx(1000.0 * (1 - 0.0001 * 24), rel=1e-6)


def test_run_strategy_uses_the_fill_model_and_reports_fill_stats() -> None:
    """The research engine accepts fills= and exposes what happened to the orders."""
    rng = np.random.default_rng(1)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    bars = _bars([(c * 0.99, c * 1.01, c) for c in closes])
    run = run_strategy(bars, "keltner_breakout", costs=CostSettings.perp(), fills=FillModel.maker(max_wait_bars=2))
    assert run.fill_stats is not None and run.fill_stats["orders"] > 0
    assert 0.0 <= run.fill_stats["fill_rate"] <= 1.0
    with pytest.raises(ValueError):
        FillModel(style="iceberg")
