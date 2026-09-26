"""Portfolio runtime (src/portfolio/runtime.py), candle feeds (src/portfolio/feed.py) and the --portfolio CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.portfolio.book import PortfolioBook
from src.portfolio.config import InstrumentSpec, parse_portfolio_config
from src.portfolio.engine import PortfolioEngine, build_paper_adapters
from src.portfolio.feed import CandleFeed, FeedResult, MockCandleFeed
from src.portfolio.runtime import CYCLE_ERROR_LIMIT, PortfolioRuntime
from src.storage.bar_aggregator import OHLCVBar
from src.storage.trade_logger import TradeLogger

BTC, ETH = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD"
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _config():
    return parse_portfolio_config({
        "portfolio": {"name": "runtime-test", "initial_equity": 10_000},
        "risk": {"max_drawdown": 0.9, "max_gross_exposure": 3.0, "max_net_exposure": 3.0, "max_instrument_weight": 2.0, "daily_loss_limit": 0.5, "stale_after_bars": 2},
        "instruments": {BTC: {"kind": "perp", "max_leverage": 3.0, "lot_step": 0.0001, "min_order_size": 0.0001},
                        ETH: {"kind": "perp", "max_leverage": 3.0, "lot_step": 0.001, "min_order_size": 0.001}},
        "sleeves": [
            {"id": "btc_ma_1d", "instrument": BTC, "interval": "1d", "strategy": "moving_average_crossover", "params": {"short_window": 3, "long_window": 12}, "warmup_bars": 15},
            {"id": "eth_ma_4h", "instrument": ETH, "interval": "4h", "strategy": "moving_average_crossover", "params": {"short_window": 6, "long_window": 36}, "warmup_bars": 40},
        ],
    })


class RecordingNotifier:
    def __init__(self) -> None:
        self.trades: list[Any] = []
        self.alerts: list[dict[str, Any]] = []

    def send_trade_alert(self, alert: Any) -> bool:
        self.trades.append(alert)
        return True

    def send_alert(self, **kwargs: Any) -> bool:
        self.alerts.append(kwargs)
        return True


def _runtime(tmp_path, *, feed=None, kill_switch_file=None, strategies=None):
    config = _config()
    book = PortfolioBook.from_config(config)
    logger = TradeLogger(tmp_path / "trades.db")
    notifier = RecordingNotifier()
    engine = PortfolioEngine(config, adapters=build_paper_adapters(config, book), book=book, trade_logger=logger, notifier=notifier, strategies=strategies)
    feed = feed or MockCandleFeed(config.instruments, grid_interval=engine.grid_interval, history_days=60, total_days=120)
    runtime = PortfolioRuntime(engine, feed, interval_seconds=0, trade_logger=logger, notifier=notifier, kill_switch_file=kill_switch_file)
    return runtime, logger, notifier


def test_the_mock_feed_only_shows_completed_candles_and_moves_one_grid_bar_per_fetch() -> None:
    feed = MockCandleFeed([BTC], grid_interval="4h", history_days=10, total_days=20)
    first = asyncio.run(feed.fetch([(BTC, "4h"), (BTC, "1d")]))
    assert len(first.bars[(BTC, "4h")]) == 60 and len(first.bars[(BTC, "1d")]) == 10
    assert first.now == first.bars[(BTC, "4h")][-1].timestamp + timedelta(hours=4)
    for _ in range(5):  # five more 4h bars: day 11 is still open
        result = asyncio.run(feed.fetch([(BTC, "4h"), (BTC, "1d")]))
    assert len(result.bars[(BTC, "4h")]) == 65 and len(result.bars[(BTC, "1d")]) == 10
    result = asyncio.run(feed.fetch([(BTC, "1d"), (BTC, "4h")]))
    assert len(result.bars[(BTC, "1d")]) == 11
    daily, last_four_hours = result.bars[(BTC, "1d")][-1], result.bars[(BTC, "4h")][-6:]
    assert daily.timestamp == last_four_hours[0].timestamp == datetime(2023, 1, 11, tzinfo=timezone.utc)
    assert daily.close == last_four_hours[-1].close and daily.high >= max(bar.close for bar in last_four_hours)


def test_the_candle_feed_isolates_failures_and_drops_the_open_candle() -> None:
    calls = {"fail": False}

    def loader(spec: InstrumentSpec, interval: str) -> list[OHLCVBar]:
        if spec.id == ETH and calls["fail"]:
            raise ConnectionError("venue down")
        return [OHLCVBar(exchange="x", symbol=spec.symbol, interval_seconds=14400, timestamp=T0 + timedelta(hours=4 * i), open=1, high=1, low=1, close=1.0 + i, volume=1) for i in range(4)]

    feed = CandleFeed({BTC: InstrumentSpec(id=BTC), ETH: InstrumentSpec(id=ETH)}, loader=loader)
    now = T0 + timedelta(hours=15)  # the bar stamped 12:00 closes at 16:00: still open
    first = asyncio.run(feed.fetch([(BTC, "4h"), (ETH, "4h")], now))
    assert first.failed == {} and [bar.close for bar in first.bars[(ETH, "4h")]] == [1.0, 2.0, 3.0]
    calls["fail"] = True
    second = asyncio.run(feed.fetch([(BTC, "4h"), (ETH, "4h")], now))
    assert list(second.failed) == [(ETH, "4h")] and "venue down" in second.failed[(ETH, "4h")]
    assert second.bars[(ETH, "4h")] == first.bars[(ETH, "4h")] and (BTC, "4h") in second.bars  # last good bars; BTC unaffected


def test_the_runtime_trades_writes_snapshots_and_stops_after_its_iterations(tmp_path) -> None:
    runtime, logger, notifier = _runtime(tmp_path)
    reports = asyncio.run(runtime.run(iterations=40))
    assert len(reports) == 40 and all(report.decided for report in reports)
    assert notifier.trades and sum(len(report.fills) for report in reports) == len(notifier.trades)
    snapshots = logger.list_portfolio_snapshots(portfolio="runtime-test")
    assert len(snapshots) == 40 and set(snapshots[0]["sleeves"]) == {"btc_ma_1d", "eth_ma_4h"}
    assert snapshots[0]["sleeves"]["eth_ma_4h"]["last_action"] in {"enter", "exit", "flip", "hold", "flat"}
    events = {event["event_type"] for event in logger.list_events(limit=500)}
    assert {"portfolio_runtime_started", "portfolio_runtime_stopped", "portfolio_decision"} <= events


class FlakyFeed:
    """Wraps a feed: fails a key for chosen fetches, or serves frozen bars to make an instrument stale."""

    def __init__(self, inner: MockCandleFeed, *, fail_on: set[int] = frozenset(), freeze_eth_from: int | None = None) -> None:
        self.inner, self.fail_on, self.freeze_eth_from, self.calls, self.frozen = inner, fail_on, freeze_eth_from, 0, None

    def now(self) -> datetime:
        return self.inner.now()

    async def fetch(self, keys, now=None) -> FeedResult:
        result = await self.inner.fetch(keys, now)
        self.calls += 1
        if self.calls in self.fail_on:
            result.failed[(ETH, "4h")] = "ConnectionError: venue down"
        if self.freeze_eth_from is not None and self.calls >= self.freeze_eth_from:
            self.frozen = self.frozen or result.bars[(ETH, "4h")]
            result.bars[(ETH, "4h")] = self.frozen
        return result


def test_problems_alert_once_when_they_start_and_once_when_they_clear(tmp_path) -> None:
    config = _config()
    inner = MockCandleFeed(config.instruments, grid_interval="4h", history_days=60, total_days=120)
    runtime, logger, notifier = _runtime(tmp_path, feed=FlakyFeed(inner, fail_on={3, 4, 5}))
    reports = asyncio.run(runtime.run(iterations=8))
    kinds = [alert["event_type"] for alert in notifier.alerts]
    assert kinds.count("market_data") == 1 and kinds.count("market_data_resolved") == 1
    assert kinds.count("stale_data") == 1 and kinds.count("stale_data_resolved") == 1  # a failed fetch also marks the instrument stale
    for report in reports[2:5]:  # while ETH's data failed, its position could only shrink
        assert all(order.reduce_only for order in report.orders if order.instrument == ETH)


def test_an_instrument_without_fresh_bars_can_shrink_but_not_grow(tmp_path) -> None:
    config = _config()
    inner = MockCandleFeed(config.instruments, grid_interval="4h", history_days=60, total_days=120)
    runtime, _logger, notifier = _runtime(tmp_path, feed=FlakyFeed(inner, freeze_eth_from=2))
    reports = asyncio.run(runtime.run(iterations=10))
    assert any(alert["event_type"] == "stale_data" and ETH in alert["message"] for alert in notifier.alerts)
    for report in reports[4:]:  # ETH's last bar is now more than 2 grid bars old
        eth_orders = [order for order in report.orders if order.instrument == ETH]
        assert all(order.reduce_only for order in eth_orders)


def test_the_kill_switch_flattens_every_position_and_stops(tmp_path) -> None:
    switch = tmp_path / "kill_switch_state.json"
    runtime, logger, notifier = _runtime(tmp_path, kill_switch_file=switch)
    asyncio.run(runtime.run(iterations=5))
    assert runtime.engine.book.units(), "the test needs open positions"
    switch.write_text(json.dumps({"active": True, "reason": "test", "orders_cancelled": []}))
    asyncio.run(runtime.run(iterations=0))
    assert runtime.stop_reason == "kill switch" and runtime.engine.book.units() == {}
    assert runtime.engine.reconcile() == {} and any(alert["event_type"] == "kill_switch" for alert in notifier.alerts)
    assert all(order.reduce_only for order in runtime.reports[-1].orders)


def test_repeated_cycle_errors_stop_a_run_until_stopped_loop(tmp_path) -> None:
    class BrokenFeed:
        def now(self) -> datetime:
            return T0

        async def fetch(self, keys, now=None):
            raise ConnectionError("no network")

    runtime, _logger, notifier = _runtime(tmp_path, feed=BrokenFeed())
    reports = asyncio.run(runtime.run(iterations=0))
    assert reports == [] and runtime.stop_reason == f"{CYCLE_ERROR_LIMIT} failed cycles in a row"
    assert [alert["event_type"] for alert in notifier.alerts] == ["cycle_error"]  # alerted once, not five times


def test_a_stop_request_ends_a_run_until_stopped_loop(tmp_path) -> None:
    runtime, _logger, _notifier = _runtime(tmp_path)
    original = runtime.run_once

    async def run_once():
        report = await original()
        if len(runtime.reports) == 3:
            runtime.request_stop("received SIGTERM")
        return report

    runtime.run_once = run_once
    asyncio.run(runtime.run(iterations=0))
    assert len(runtime.reports) == 3 and runtime.stop_reason == "received SIGTERM"


def test_a_broken_sleeve_is_held_flat_then_disabled_while_the_rest_trades(tmp_path) -> None:
    def broken(history, index, bar):
        raise RuntimeError("bad data")

    broken.signal_series = lambda bars: (_ for _ in ()).throw(RuntimeError("bad data"))
    runtime, _logger, notifier = _runtime(tmp_path, strategies={"eth_ma_4h": broken})
    reports = asyncio.run(runtime.run(iterations=6))
    assert "eth_ma_4h" in runtime.engine.disabled_sleeves
    assert any(alert["event_type"] == "portfolio_sleeve_disabled" for alert in notifier.alerts)
    assert all("eth_ma_4h" in report.sleeve_errors for report in reports)
    assert ETH not in runtime.engine.book.units() and BTC in runtime.engine.book.units()


def test_the_cli_refuses_live_and_runs_a_mock_portfolio(tmp_path, monkeypatch, capsys) -> None:
    import main

    monkeypatch.setattr(main, "PORTFOLIO_KILL_SWITCH_FILE", tmp_path / "kill.json")
    monkeypatch.setattr(sys, "argv", ["main.py", "--runtime", "live", "--portfolio", "config/portfolio.example.toml"])
    with pytest.raises(SystemExit) as exited:
        main.main()
    assert exited.value.code == 2 and "not available yet" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["main.py", "--runtime", "paper", "--portfolio", "config/portfolio.example.toml", "--use-mock-connector",
                                      "--portfolio-state-dir", str(tmp_path / "state"), "--runtime-iterations", "12", "--runtime-interval", "0", "--dashboard"])
    with pytest.raises(SystemExit) as exited:
        main.main()
    out = capsys.readouterr().out
    assert exited.value.code == 0 and "Stopped after 12 cycles" in out and "Portfolio 'trend-core'" in out and "Sleeves (own target" in out
    assert (tmp_path / "state" / "engine.json").exists() and (tmp_path / "state" / "paper_kraken_futures.json").exists()

    monkeypatch.setattr(sys, "argv", ["main.py", "--portfolio", "config/portfolio.example.toml", "--dashboard"])
    with pytest.raises(SystemExit) as exited:
        main.main()
    assert exited.value.code == 0 and "Instruments (target and actual" in capsys.readouterr().out
