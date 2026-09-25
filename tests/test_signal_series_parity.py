"""The vectorized signal_series must equal the per-bar strategy output at every bar (runtime uses per-bar)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.simple_backtest import SimpleBacktester
from src.backtest.strategies import make_long_only, make_regime_gated
from src.research.catalog import CATALOG, build_strategy
from src.storage.bar_aggregator import OHLCVBar


def _bars(seed: int, count: int = 260) -> list[OHLCVBar]:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, count)))
    spread = np.abs(rng.normal(0.0, 0.01, count)) * close
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        OHLCVBar(exchange="mock", symbol="X/USD", interval_seconds=3600, timestamp=start + timedelta(hours=i), open=float(close[i]), high=float(close[i] + spread[i]), low=float(close[i] - spread[i]), close=float(close[i]), volume=float(rng.uniform(0.5, 3.0)))
        for i in range(count)
    ]


def _per_bar(strategy, bars) -> list[int]:
    return [int(np.sign(float(strategy(bars[: i + 1], i, bars[i]) or 0))) for i in range(len(bars))]


def _cases():
    for spec in CATALOG.values():
        combos = spec.combos()
        for params in combos[:: max(1, len(combos) // 4)]:
            yield spec.name, params


@pytest.mark.parametrize("name,params", list(_cases()))
def test_every_catalog_strategy_matches_per_bar_signals(name: str, params: dict) -> None:
    """Checked on two random series, covering long/short behaviour and warmup."""
    for seed in (1, 2):
        bars = _bars(seed)
        strategy = build_strategy(name, **params)
        assert callable(getattr(strategy, "signal_series", None)), f"{name} has no vectorized path"
        assert list(np.asarray(strategy.signal_series(bars)).astype(int)) == _per_bar(strategy, bars)


def test_wrappers_keep_parity() -> None:
    """Long-only and regime gating preserve the equality."""
    bars = _bars(3)
    for strategy in (
        make_long_only(build_strategy("donchian_breakout")),
        make_regime_gated(build_strategy("moving_average_crossover"), required_regime="trending"),
        make_regime_gated(build_strategy("rsi_reversion"), required_regime="ranging"),
    ):
        assert list(np.asarray(strategy.signal_series(bars)).astype(int)) == _per_bar(strategy, bars)


def test_backtester_results_are_identical_with_and_without_the_fast_path() -> None:
    """Same trades and equity whether the backtester precomputes signals or calls the strategy per bar."""
    bars = _bars(4)
    fast = SimpleBacktester(strategy=build_strategy("keltner_breakout")).run(bars)
    plain = build_strategy("keltner_breakout")
    slow = SimpleBacktester(strategy=lambda history, index, bar: plain(history, index, bar)).run(bars)
    assert fast.trades == slow.trades > 0
    assert fast.mtm_equity_series == pytest.approx(slow.mtm_equity_series)
