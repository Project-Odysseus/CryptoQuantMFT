"""Sleeves: one strategy on one instrument, turned into a target weight bar by bar (src/portfolio/sleeves.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest.simple_backtest import SimpleBacktester
from src.portfolio.sleeves import SleeveRunner, SleeveSpec, SleeveState, _Prefix, run_sleeve
from src.research.catalog import build_strategy
from src.research.engine import CostSettings
from src.research.portfolio import PortfolioCosts, simulate_portfolio
from src.storage.bar_aggregator import OHLCVBar

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
FREE = PortfolioCosts(fee_pct=0.0, slippage_bps=0.0)


def _bars(closes: list[float] | np.ndarray, *, hours: int = 24) -> list[OHLCVBar]:
    return [
        OHLCVBar(exchange="mock", symbol="BTC/USD", interval_seconds=hours * 3600, timestamp=START + timedelta(hours=hours * i),
                 open=float(c), high=float(c) * 1.01, low=float(c) * 0.99, close=float(c), volume=1.0)
        for i, c in enumerate(closes)
    ]


def _random_walk(count: int, *, seed: int = 7, vol: float = 0.03) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0005, vol, count)))


def _spec(**overrides: object) -> SleeveSpec:
    values: dict[str, object] = {"id": "btc_test", "instrument": "kraken_futures:BTC/USD", "interval": "1d", "strategy": "moving_average_crossover",
                                 "params": {"short_window": 5, "long_window": 20}}
    values.update(overrides)
    return SleeveSpec(**values)  # type: ignore[arg-type]


def _held_runs(weights: np.ndarray) -> list[np.ndarray]:
    """Consecutive stretches with the same nonzero side."""
    runs, current = [], []
    for weight in weights:
        if current and (weight == 0.0 or np.sign(weight) != np.sign(current[-1])):
            runs.append(np.array(current))
            current = []
        if weight != 0.0:
            current.append(weight)
    if current:
        runs.append(np.array(current))
    return runs


def test_a_long_only_sleeve_never_goes_short_and_keeps_its_entry_size_while_held() -> None:
    bars = _bars(_random_walk(400))
    run = run_sleeve(_spec(long_only=True, sizing="vol_target", sizing_params={"target_annual_vol": 0.4}), bars)

    assert (run.weights >= 0.0).all()
    runs = _held_runs(run.weights)
    assert len(runs) >= 3, "the test needs several round trips to mean anything"
    for held in runs:
        assert np.all(held == held[0])  # sized at entry, never resized while open
    assert len({round(held[0], 6) for held in runs}) > 1  # vol targeting does size entries differently


def test_sleeve_weights_do_not_look_ahead() -> None:
    closes = _random_walk(300, seed=3)
    spec = _spec(strategy="keltner_breakout", params={"window": 20, "atr_multiplier": 1.0}, sizing="vol_target", sizing_params={"target_annual_vol": 0.5})
    base = run_sleeve(spec, _bars(closes))
    assert np.count_nonzero(base.weights < 0) and np.count_nonzero(base.weights > 0)

    for cut in (60, 150, 240):
        changed = closes.copy()
        changed[cut + 1 :] *= np.random.default_rng(cut).uniform(0.5, 1.5, len(closes) - cut - 1)
        perturbed = run_sleeve(spec, _bars(changed))
        np.testing.assert_array_equal(perturbed.weights[: cut + 1], base.weights[: cut + 1])


def test_run_sleeve_matches_stepping_once_per_new_bar_with_a_json_restart_every_bar() -> None:
    """The runtime form: a new bar arrives, the signal is computed from history so far, state is saved and reloaded."""
    bars = _bars(_random_walk(160, seed=11))
    spec = _spec(strategy="keltner_breakout", params={"window": 20, "atr_multiplier": 1.0}, sizing="kelly",
                 sizing_params={"min_trades": 2, "fallback_fraction": 0.3}, stops={"atr_stop_multiplier": 2.0, "time_stop_bars": 15})
    replayed = run_sleeve(spec, bars)

    runner = SleeveRunner(spec)
    state = SleeveState()
    weights = []
    for index in range(len(bars)):
        history = bars[: index + 1]
        state, _decision = runner.step(state, history, runner.signals(history)[-1])
        state = SleeveState.from_dict(json.loads(json.dumps(state.to_dict())))
        weights.append(state.weight)

    np.testing.assert_allclose(weights, replayed.weights)
    assert state == replayed.state
    assert {decision.action for decision in replayed.decisions} >= {"enter", "stop", "hold"}


def test_a_long_only_sleeve_reproduces_the_single_strategy_backtest() -> None:
    bars = _bars(_random_walk(500, seed=5))
    spec = _spec(long_only=True, sizing="fixed_fraction", sizing_params={"fraction": 1.0})
    weights = run_sleeve(spec, bars).weights
    prices = pd.DataFrame({"BTC": [bar.close for bar in bars]}, index=pd.DatetimeIndex([bar.timestamp for bar in bars]))
    frame = pd.DataFrame({"BTC": weights}, index=prices.index)
    strategy = build_strategy("moving_average_crossover", short_window=5, long_window=20, long_only=True)

    free = SimpleBacktester(strategy=strategy, initial_equity=1.0).run(bars)
    assert free.trades >= 5
    assert simulate_portfolio(prices, frame, costs=FREE).equity.iloc[-1] == pytest.approx(free.final_equity, rel=1e-9)

    settings = CostSettings(fee_pct=0.05, slippage_bps=5.0)
    costly = SimpleBacktester(strategy=strategy, initial_equity=1.0, cost_model=settings.cost_model()).run(bars)
    portfolio = simulate_portfolio(prices, frame, costs=PortfolioCosts(fee_pct=0.05, slippage_bps=5.0)).equity.iloc[-1]
    # Both charge 0.1% per side; they differ only in where the cost lands (entry price vs traded notional).
    assert portfolio == pytest.approx(costly.final_equity, rel=2e-3)


def _step_through(runner: SleeveRunner, closes: list[float], signals: list[float]) -> list[tuple[str, float, str | None]]:
    bars = _bars(closes)
    state = SleeveState()
    out = []
    for index, signal in enumerate(signals):
        state, decision = runner.step(state, bars[: index + 1], signal)
        assert decision.weight == state.weight
        out.append((decision.action, round(state.weight, 6), decision.reason))
    return out


def test_a_stop_flattens_the_sleeve_and_blocks_reentry_until_the_signal_resets() -> None:
    runner = SleeveRunner(_spec(sizing_params={"fraction": 0.5}, stops={"position_drawdown_stop_pct": 0.10}))
    closes = [100.0, 95.0, 88.0, 90.0, 92.0, 93.0, 94.0]
    signals = [1, 1, 1, 1, 0, 1, 1]
    assert _step_through(runner, closes, signals) == [
        ("enter", 0.5, None),
        ("hold", 0.5, None),
        ("stop", 0.0, "position_drawdown_stop"),
        ("blocked", 0.0, "reentry_after_stop"),
        ("flat", 0.0, None),  # the signal left the long side: the block clears
        ("enter", 0.5, None),
        ("hold", 0.5, None),
    ]


def test_a_stopped_long_may_still_enter_short_and_a_time_stop_counts_bars_held() -> None:
    runner = SleeveRunner(_spec(sizing_params={"fraction": 0.5}, stops={"time_stop_bars": 2}))
    closes = [100.0] * 6
    assert _step_through(runner, closes, [1, 1, 1, 1, -1, -1]) == [
        ("enter", 0.5, None),
        ("hold", 0.5, None),
        ("stop", 0.0, "time_stop"),
        ("blocked", 0.0, "reentry_after_stop"),
        ("enter", -0.5, None),  # a block on the long side never stops a short
        ("hold", -0.5, None),
    ]


def test_exits_flips_and_declined_entries_say_why() -> None:
    runner = SleeveRunner(_spec(sizing_params={"fraction": 0.25}))
    assert _step_through(runner, [100.0, 110.0, 99.0, 99.0], [1, -1, 0, 0]) == [
        ("enter", 0.25, None),
        ("flip", -0.25, None),
        ("exit", 0.0, "signal_flat"),
        ("flat", 0.0, None),
    ]
    state, _ = runner.step(SleeveState(), _bars([100.0]), 1)
    state, _ = runner.step(state, _bars([100.0, 110.0]), -1)
    assert state.trade_returns == [pytest.approx(0.10)]  # the closed long is remembered for Kelly sizing

    vol_runner = SleeveRunner(_spec(sizing="vol_target", sizing_params={"target_annual_vol": 0.5}))
    state, decision = vol_runner.step(SleeveState(), _bars([100.0, 101.0, 102.0]), 1)
    assert (decision.action, decision.reason, state.weight) == ("declined", "volatility_forecast_unavailable", 0.0)


def test_a_disabled_sleeve_holds_nothing_and_unknown_stops_are_rejected() -> None:
    runner = SleeveRunner(_spec(enabled=False))
    state, decision = runner.step(SleeveState(weight=0.5, entry_price=100.0, bars_held=3, trade_returns=[0.1]), _bars([100.0]), 1)
    assert (decision.action, decision.reason) == ("flat", "sleeve_disabled")
    assert state.weight == 0.0 and state.trade_returns == [0.1]

    with pytest.raises(ValueError, match="unknown stops"):
        SleeveRunner(_spec(stops={"atr_stop": 2.0}))


def test_instrument_id_splits_into_venue_and_symbol() -> None:
    spec = _spec()
    assert (spec.venue, spec.symbol) == ("kraken_futures", "BTC/USD")


def test_prefix_view_behaves_like_a_list_slice() -> None:
    items = list(range(10))
    view = _Prefix(items, 6)
    assert len(view) == 6 and view[-1] == 5 and view[0] == 0
    assert view[2:4] == [2, 3] and view[-3:] == [3, 4, 5] and view[:] == items[:6]
    assert view[::-1] == [5, 4, 3, 2, 1, 0] and view[4::-2] == [4, 2, 0]
    with pytest.raises(IndexError):
        view[6]
