"""EMA x VWAP: the indicators, and the entry/exit rules on hand-built candles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.indicators import ema, vwap
from src.backtest.strategies import ema_vwap_strategy
from src.storage.bar_aggregator import OHLCVBar

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_ema_matches_the_recursive_definition() -> None:
    out = ema(np.array([10.0, 11.0, 12.0, 13.0]), 3)  # alpha = 0.5
    assert out == pytest.approx([10.0, 10.5, 11.25, 12.125])


def test_session_vwap_restarts_each_utc_day_and_rolling_vwap_uses_the_last_bars() -> None:
    price = np.array([10.0, 20.0, 30.0, 40.0])
    volume = np.array([1.0, 3.0, 1.0, 1.0])
    epoch = np.array([T0.timestamp() + hours * 3600 for hours in (22, 23, 24, 25)])  # two bars on each day
    session = vwap(price, price, price, volume, epoch_seconds=epoch)
    assert session == pytest.approx([10.0, (10 + 60) / 4, 30.0, 35.0])
    rolling = vwap(price, price, price, volume, window=2)
    assert rolling == pytest.approx([10.0, 17.5, 22.5, 35.0])
    assert vwap(price, price, price, np.zeros(4), window=2) == pytest.approx(price)  # no volume: the typical price


def _bars(closes: list[float], *, minutes: int = 3, start: datetime = T0) -> list[OHLCVBar]:
    return [OHLCVBar(exchange="mock", symbol="BTC/USD", interval_seconds=60 * minutes, timestamp=start + timedelta(minutes=minutes * index),
                     open=close, high=close, low=close, close=close, volume=1.0) for index, close in enumerate(closes)]


def test_a_close_through_the_vwap_enters_and_a_close_back_through_the_ema_exits() -> None:
    # A dip below the day's VWAP, a break back above it, a run up, then a close back under the EMA
    closes = [100.0] * 10 + [98.0, 97.0, 96.0] + [101.0, 103.0, 105.0, 107.0, 106.0, 99.0]  # 106 stays above the EMA (105.5)
    strategy = ema_vwap_strategy(ema_window=3, trend_filter=False, allow_short=False)
    signals = list(strategy.signal_series(_bars(closes)))
    entry = closes.index(101.0)
    assert signals[entry - 1] == 0 and signals[entry] == 1
    assert signals[closes.index(99.0)] == 0  # closed below the EMA: out
    assert set(signals[entry:closes.index(99.0)]) == {1}


def test_the_short_side_mirrors_the_long_side() -> None:
    closes = [100.0] * 10 + [102.0, 103.0, 104.0] + [99.0, 97.0, 95.0, 93.0, 94.0, 101.0]  # 94 stays below the EMA
    signals = list(ema_vwap_strategy(ema_window=3, trend_filter=False).signal_series(_bars(closes)))
    entry = closes.index(99.0)
    assert signals[entry - 1] == 1 and signals[entry] == -1  # long from the break up at 102, flipped short by the break down
    assert signals[closes.index(101.0)] == 1  # closed above the EMA (short out) and back through the VWAP (long in)
    assert list(ema_vwap_strategy(ema_window=3, trend_filter=False, allow_short=False).signal_series(_bars(closes))).count(-1) == 0


def test_the_trend_filter_needs_the_ema_on_the_right_side_of_the_vwap() -> None:
    # A sharp one-bar pop through the VWAP after a long slide: the slow EMA is still under the VWAP, so no entry
    closes = [140.0 - index for index in range(40)] + [150.0]  # longer than the warmup (3 x the EMA window)
    unfiltered = ema_vwap_strategy(ema_window=10, trend_filter=False, allow_short=False).signal_series(_bars(closes))
    filtered = ema_vwap_strategy(ema_window=10, trend_filter=True, allow_short=False).signal_series(_bars(closes))
    assert unfiltered[-1] == 1 and filtered[-1] == 0


def test_the_session_exit_is_flat_on_the_last_bar_of_each_utc_day() -> None:
    start = T0 + timedelta(hours=23, minutes=30)  # 3m bars crossing midnight
    closes = [100.0] * 5 + [95.0, 101.0, 103.0, 105.0, 107.0, 109.0, 111.0]
    bars = _bars(closes, start=start)
    held = list(ema_vwap_strategy(ema_window=3, trend_filter=False, allow_short=False).signal_series(bars))
    flat = list(ema_vwap_strategy(ema_window=3, trend_filter=False, allow_short=False, session_exit=True).signal_series(bars))
    last_of_day = next(index for index, bar in enumerate(bars) if (bar.timestamp + timedelta(minutes=3)).hour == 0 and (bar.timestamp + timedelta(minutes=3)).minute == 0)
    assert held[last_of_day] == 1 and flat[last_of_day] == 0
