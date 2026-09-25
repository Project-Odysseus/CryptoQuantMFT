"""Tests for bar-close trading (bar interval independent of polling) and history warmup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data import kraken_futures
from src.data.kraken_futures import fetch_candles
from src.runtime.config import _parse_bar_interval
from src.storage.bar_aggregator import OHLCVBar
from src.storage.market_store import MarketStore
from src.storage.streaming_aggregator import StreamingAggregator

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _aggregator(tmp_path: Path, interval: int = 14400) -> StreamingAggregator:
    return StreamingAggregator(store=MarketStore(database_path=tmp_path / "m.db"), interval_seconds=interval)


def test_a_4h_bar_collects_ticks_for_its_whole_bucket(tmp_path: Path) -> None:
    """Polls inside the bucket build one bar; it is only handed over once the bucket has ended."""
    aggregator = _aggregator(tmp_path)
    for minutes, price in ((0, 100.0), (60, 110.0), (120, 95.0), (239, 105.0)):
        aggregator.update("kraken", "BTC/USD", T0 + timedelta(minutes=minutes), price, price, price, 1.0)
        assert aggregator.drain_completed(T0 + timedelta(minutes=minutes)) == []

    completed = aggregator.drain_completed(T0 + timedelta(hours=4))
    assert len(completed) == 1
    bar = completed[0]
    assert (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume) == (T0, 100.0, 110.0, 95.0, 105.0, 4.0)


def test_a_tick_in_the_next_bucket_completes_the_previous_bar(tmp_path: Path) -> None:
    """The bar is finalized when the next bucket's first tick arrives, and the new bar stays open."""
    aggregator = _aggregator(tmp_path)
    aggregator.update("kraken", "BTC/USD", T0, 100.0, 100.0, 100.0, 1.0)
    aggregator.update("kraken", "BTC/USD", T0 + timedelta(hours=4, minutes=1), 120.0, 120.0, 120.0, 1.0)
    completed = aggregator.drain_completed(T0 + timedelta(hours=4, minutes=1))
    assert [bar.close for bar in completed] == [100.0]
    assert aggregator.bars  # the new 16:00 bar is still collecting


def test_bar_interval_parsing() -> None:
    """Labels, seconds and 'one bar per poll' all parse; nonsense is rejected."""
    assert _parse_bar_interval("4h") == 14400
    assert _parse_bar_interval("1d") == 86400
    assert _parse_bar_interval(None) is None
    assert _parse_bar_interval(3600) == 3600
    with pytest.raises(ValueError):
        _parse_bar_interval("7m")


def test_futures_candles_drop_the_open_candle_and_keep_the_last_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warmup must only contain completed candles, oldest first."""
    now = datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc)
    start = int(datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc).timestamp()) * 1000
    candles = [{"time": start + i * 14400000, "open": "1", "high": "2", "low": "0.5", "close": str(10 + i), "volume": "0"} for i in range(6)]
    monkeypatch.setattr(kraken_futures, "_request_json", lambda method, url, params=None: {"candles": candles, "more_candles": False})

    bars = fetch_candles("PF_XBTUSD", interval_seconds=14400, count=3, symbol="BTC/USD", now=now)
    assert [bar.close for bar in bars] == [12.0, 13.0, 14.0]  # 20:00 candle (close 15) is still open at 23:00
    assert bars[-1].timestamp == datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
    assert {bar.symbol for bar in bars} == {"BTC/USD"}
    with pytest.raises(ValueError):
        fetch_candles("PF_XBTUSD", interval_seconds=7, count=3)


def _history(count: int, start: datetime) -> list[OHLCVBar]:
    return [OHLCVBar(exchange="kraken_futures", symbol="BTC/USD", interval_seconds=14400, timestamp=start + timedelta(hours=4 * i), open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.0 + i, volume=0.0) for i in range(count)]


@pytest.mark.asyncio
async def test_runtime_seeds_history_signals_at_once_but_only_trades_on_a_new_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a bar interval: seeded history gives an immediate signal, polls without a new bar never trade."""
    from main import build_runtime_orchestrator

    monkeypatch.setattr("src.data.kraken_futures.fetch_instrument", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    orchestrator, pipeline = build_runtime_orchestrator(mode="live_dry_run", exchange="kraken_futures", use_mock_connector=True)
    orchestrator.bar_interval_seconds = 14400
    orchestrator.strategy = lambda history, index, bar: 1
    pipeline.aggregator.interval_seconds = 14400
    assert orchestrator.seed_history(_history(30, datetime.now(timezone.utc) - timedelta(days=6))) == 30

    cycle = await orchestrator.run_once()
    assert cycle.signals and cycle.signals[-1] == 1.0
    assert cycle.execution_result.entry_decisions == []  # no bar completed, so the strategy did not act
    assert not orchestrator.execution_engine.execution_adapter.list_orders()

    pipeline.drain_completed_bars = lambda now: _history(1, datetime.now(timezone.utc) - timedelta(hours=4))  # a bar just closed
    closed = await orchestrator.run_once()
    assert [decision["side"] for decision in closed.execution_result.entry_decisions] == ["buy"]
    assert len(closed.bars) == 31
    assert orchestrator.execution_engine.execution_adapter.position_size() > 0.0  # and the entry was actually sized


def test_seed_history_drops_other_symbols_and_overlap() -> None:
    """Seeding keeps one clean series: right symbol, strictly before the first live bar."""
    from src.runtime.orchestrator import RuntimeOrchestrator

    orchestrator = RuntimeOrchestrator(pipeline=None, mode="paper", trading_symbol="BTC/USD")
    live = _history(1, T0)
    orchestrator._bar_history = list(live)
    other = OHLCVBar(exchange="x", symbol="ETH/USD", interval_seconds=14400, timestamp=T0 - timedelta(hours=8), open=1, high=1, low=1, close=1, volume=0)
    seeded = orchestrator.seed_history(_history(3, T0 - timedelta(hours=8)) + [other])
    assert seeded == 2  # the third seeded bar is at T0, overlapping the live bar
    assert [bar.timestamp for bar in orchestrator._bar_history] == [T0 - timedelta(hours=8), T0 - timedelta(hours=4), T0]
