"""Tests for the entry/exit ("latched") strategies and the latch helper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from src.backtest.simple_backtest import SimpleBacktester
from src.backtest.strategies import (
    donchian_breakout_strategy,
    keltner_breakout_strategy,
    latch_position,
    rsi_reversion_strategy,
    trend_pullback_strategy,
    trend_tstat_strategy,
    volatility_squeeze_strategy,
)
from src.storage.bar_aggregator import OHLCVBar

_START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _bars(closes: list[float], *, half_range: float = 0.5) -> list[OHLCVBar]:
    return [
        OHLCVBar(
            exchange="mock",
            symbol="BTC/EUR",
            interval_seconds=3600,
            timestamp=_START + timedelta(hours=index),
            open=close,
            high=close + half_range,
            low=close - half_range,
            close=close,
            volume=1.0,
        )
        for index, close in enumerate(closes)
    ]


def _signal(strategy, closes: list[float], **bar_kwargs) -> int:
    history = _bars(closes, **bar_kwargs)
    return strategy(history, len(history) - 1, history[-1])


def _flags(*indices: int, length: int = 10) -> np.ndarray:
    mask = np.zeros(length, dtype=bool)
    mask[list(indices)] = True
    return mask


def test_latch_holds_after_entry_until_exit_and_reenters() -> None:
    """Long after an entry, flat after a later exit, long again after a newer entry."""
    assert latch_position(_flags(2), _flags()) == 1
    assert latch_position(_flags(2), _flags(5)) == 0
    assert latch_position(_flags(2, 7), _flags(5)) == 1
    assert latch_position(_flags(), _flags()) == 0


def test_latch_opposite_entry_counts_as_exit() -> None:
    """A short entry ends a long, and once that short is exited the result is flat, not the old long."""
    no_events = _flags()
    assert latch_position(_flags(2), no_events, _flags(5), no_events) == -1
    assert latch_position(_flags(2), no_events, _flags(5), _flags(8)) == 0


def test_latch_without_short_side_never_returns_short() -> None:
    """Passing no short arrays makes the latch long-only."""
    assert latch_position(_flags(), _flags(), None, None) == 0


def test_donchian_enters_on_breakout_and_holds_inside_the_channel() -> None:
    """A breakout above the prior high goes long and stays long while price drifts inside the channel."""
    strategy = donchian_breakout_strategy(entry_window=5, exit_window=3)
    base = [100.0, 100.5, 99.5, 100.0, 100.2, 99.8]
    assert _signal(strategy, base) == 0
    assert _signal(strategy, base + [103.0]) == 1
    assert _signal(strategy, base + [103.0, 102.8, 102.9]) == 1
    # Close below the lowest low of the prior 3 bars exits.
    assert _signal(strategy, base + [103.0, 102.8, 102.9, 101.0]) == 0


def test_donchian_shorts_on_breakdown_unless_disabled() -> None:
    """A break below the prior low goes short, or stays flat when shorting is disabled."""
    base = [100.0, 100.5, 99.5, 100.0, 100.2, 99.8]
    assert _signal(donchian_breakout_strategy(entry_window=5, exit_window=3), base + [96.0]) == -1
    assert _signal(donchian_breakout_strategy(entry_window=5, exit_window=3, allow_short=False), base + [96.0]) == 0


def test_donchian_trade_is_held_across_bars_in_the_backtester() -> None:
    """Target-position semantics: one entry, held for several bars, then one exit."""
    closes = [100.0, 100.5, 99.5, 100.0, 100.2, 99.8, 103.0, 104.0, 105.0, 104.5, 101.0, 100.5]
    result = SimpleBacktester(strategy=donchian_breakout_strategy(entry_window=5, exit_window=3)).run(_bars(closes))
    assert result.trades == 1
    assert result.trade_records[0].entry_price == 103.0
    assert result.trade_records[0].exit_price == 101.0


def test_keltner_enters_on_an_atr_sized_move_and_exits_at_the_average() -> None:
    """A move beyond 2 ATRs from the mean goes long; closing back below the mean exits."""
    strategy = keltner_breakout_strategy(window=5, atr_multiplier=2.0)
    base = [100.0, 100.2, 99.8, 100.1, 99.9, 100.0]
    assert _signal(strategy, base) == 0
    assert _signal(strategy, base + [103.0]) == 1
    assert _signal(strategy, base + [103.0, 102.5]) == 1
    assert _signal(strategy, base + [103.0, 102.5, 99.5]) == 0


def test_trend_tstat_follows_clean_trends_in_both_directions() -> None:
    """A clean noisy uptrend is long, a downtrend is short (or flat when long-only)."""
    noise = [0.3, -0.2, 0.1, -0.3, 0.2, -0.1, 0.25, -0.15, 0.05, -0.05, 0.1, -0.1]
    up = [100.0 + index + wiggle for index, wiggle in enumerate(noise)]
    down = [120.0 - index + wiggle for index, wiggle in enumerate(noise)]
    assert _signal(trend_tstat_strategy(window=10, t_threshold=4.0), up) == 1
    assert _signal(trend_tstat_strategy(window=10, t_threshold=4.0), down) == -1
    assert _signal(trend_tstat_strategy(window=10, t_threshold=4.0, allow_short=False), down) == 0


def test_rsi_reversion_holds_until_rsi_recovers() -> None:
    """Buy the oversold drop, keep holding on a small bounce, exit once RSI is back above 50."""
    strategy = rsi_reversion_strategy(rsi_window=3, oversold=20.0, exit_level=50.0, allow_short=False)
    drop = [100.0, 100.0, 100.0, 99.0, 98.0, 97.0]
    assert _signal(strategy, drop) == 1
    assert _signal(strategy, drop + [97.2]) == 1
    assert _signal(strategy, drop + [97.2, 98.5, 99.5]) == 0


def test_rsi_reversion_symmetric_version_reverses_into_a_short_when_overbought() -> None:
    """With shorts allowed, a rebound strong enough to be overbought exits the long and goes short."""
    strategy = rsi_reversion_strategy(rsi_window=3, oversold=20.0, exit_level=50.0)
    assert _signal(strategy, [100.0, 100.0, 100.0, 99.0, 98.0, 97.0, 97.2, 98.5, 99.5]) == -1


def test_trend_pullback_buys_a_dip_only_above_the_trend_average() -> None:
    """A short-term dip above the long average is bought; the same dip below it is not."""
    strategy = trend_pullback_strategy(trend_window=10, rsi_window=3, entry_rsi=20.0, exit_rsi=70.0, allow_short=False)
    uptrend = [100.0 + 2.0 * index for index in range(15)]
    assert _signal(strategy, uptrend + [127.0, 126.0, 125.0]) == 1
    downtrend = [130.0 - 2.0 * index for index in range(15)]
    assert _signal(strategy, downtrend + [100.0, 99.0, 98.0]) == 0


def test_volatility_squeeze_trades_a_breakout_after_compression() -> None:
    """After a quiet stretch, a close outside the band enters in the breakout direction."""
    strategy = volatility_squeeze_strategy(window=5, num_std=2.0, squeeze_lookback=20, squeeze_quantile=0.2, squeeze_memory=5)
    noisy = [100.0 + (2.0 if index % 2 else -2.0) for index in range(20)]
    quiet = [100.0 + (0.05 if index % 2 else -0.05) for index in range(8)]
    assert _signal(strategy, noisy + quiet + [101.0]) == 1
    assert _signal(strategy, noisy + quiet + [99.0]) == -1
    # The same size move without a preceding squeeze is ignored.
    assert _signal(strategy, noisy + [100.0, 105.0]) == 0
