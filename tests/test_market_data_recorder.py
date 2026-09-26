import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.data.recorder import (
    BinanceLiquidationFeed,
    BybitLiquidationFeed,
    DailyCsvSink,
    KrakenFuturesFeed,
    KrakenSpotFeed,
    MarketDataRecorder,
    RecorderConfig,
    ResyncNeeded,
    TRADE_COLUMNS,
    compact_finished_days,
    load_market_data,
    market_data_status,
    recording_gaps,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kraken_spot_book_ethusd.jsonl"
DAY_US = 86_400 * 1_000_000


class Rows:
    """Collects emitted rows by (venue, channel)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict]] = {}

    def __call__(self, venue, channel, columns, row) -> None:
        self.rows.setdefault((venue, channel), []).append(dict(zip(columns, row)))


def _spot_messages() -> list[dict]:
    return [json.loads(line, parse_float=Decimal) for line in FIXTURE.read_text().splitlines()]


def test_kraken_spot_book_matches_every_real_checksum() -> None:
    rows = Rows()
    feed = KrakenSpotFeed(rows, ["ETH/USD"], depth=10)
    for message in _spot_messages():
        feed.handle(message, received_us=1)  # raises ResyncNeeded on any checksum mismatch

    feed.sample(received_us=2)
    (sample,) = rows.rows[("kraken_spot", "book10")]
    bids = [sample[f"bid_px_{level}"] for level in range(1, 11)]
    asks = [sample[f"ask_px_{level}"] for level in range(1, 11)]
    assert bids == sorted(bids, reverse=True) and asks == sorted(asks)
    assert bids[0] < asks[0]
    assert sample["exchange_us"] > 0


def test_kraken_spot_book_detects_a_corrupted_update() -> None:
    feed = KrakenSpotFeed(Rows(), ["ETH/USD"], depth=10)
    messages = _spot_messages()
    feed.handle(messages[0], received_us=1)
    update = next(message for message in messages[1:] if message["data"][0]["bids"] or message["data"][0]["asks"])
    levels = update["data"][0]["bids"] or update["data"][0]["asks"]
    levels[0]["qty"] = levels[0]["qty"] + Decimal("1")
    with pytest.raises(ResyncNeeded):
        feed.handle(update, received_us=2)


def test_kraken_spot_trades_skip_ids_already_recorded() -> None:
    rows = Rows()
    feed = KrakenSpotFeed(rows, ["BTC/USD"], depth=10)

    def trade(trade_id: int) -> dict:
        return {"symbol": "BTC/USD", "side": "buy", "price": Decimal("100.5"), "qty": Decimal("0.1"), "ord_type": "market", "trade_id": trade_id, "timestamp": "2026-09-26T08:28:27.195924Z"}

    feed.handle({"channel": "trade", "type": "snapshot", "data": [trade(1), trade(2), trade(3)]}, received_us=1)
    feed.handle({"channel": "trade", "type": "snapshot", "data": [trade(2), trade(3), trade(4)]}, received_us=2)  # after a reconnect

    recorded = rows.rows[("kraken_spot", "trades")]
    assert [row["trade_id"] for row in recorded] == [1, 2, 3, 4]
    assert recorded[0]["exchange_us"] == 1_790_411_307_195_924
    assert recorded[0]["side"] == "buy" and recorded[0]["kind"] == "market" and recorded[0]["price"] == 100.5


def _futures_snapshot(seq: int = 10) -> dict:
    return {
        "feed": "book_snapshot", "product_id": "PF_XBTUSD", "timestamp": 1_000, "seq": seq,
        "bids": [{"price": 99.0, "qty": 1.0}, {"price": 98.0, "qty": 2.0}, {"price": 90.0, "qty": 5.0}],
        "asks": [{"price": 101.0, "qty": 1.5}, {"price": 102.0, "qty": 3.0}, {"price": 120.0, "qty": 9.0}],
    }


def test_kraken_futures_book_applies_updates_and_samples_depth_bands() -> None:
    rows = Rows()
    feed = KrakenFuturesFeed(rows, ["PF_XBTUSD"], depth=2, ticker_interval_seconds=10)
    feed.handle(_futures_snapshot(), received_us=1)
    feed.handle({"feed": "book", "product_id": "PF_XBTUSD", "side": "buy", "seq": 11, "price": 100.5, "qty": 0.5, "timestamp": 2_000}, received_us=2)
    feed.handle({"feed": "book", "product_id": "PF_XBTUSD", "side": "sell", "seq": 12, "price": 101.0, "qty": 0.0, "timestamp": 3_000}, received_us=3)

    feed.sample(received_us=4)
    (sample,) = rows.rows[("kraken_futures", "book2")]
    assert (sample["bid_px_1"], sample["bid_qty_1"], sample["bid_px_2"]) == (100.5, 0.5, 99.0)
    assert (sample["ask_px_1"], sample["ask_qty_1"], sample["ask_px_2"]) == (102.0, 3.0, 120.0)
    assert sample["exchange_us"] == 3_000_000
    # mid = 101.25: 100 bps reaches bids >= 100.24 and asks <= 102.26; 50 bps reaches neither side's levels
    assert (sample["bid_depth_100bps"], sample["ask_depth_100bps"]) == (0.5, 3.0)
    assert (sample["bid_depth_50bps"], sample["ask_depth_50bps"]) == (0.0, 0.0)


def test_kraken_futures_book_sequence_gap_forces_a_resync() -> None:
    feed = KrakenFuturesFeed(Rows(), ["PF_XBTUSD"], depth=2, ticker_interval_seconds=10)
    feed.handle(_futures_snapshot(seq=10), received_us=1)
    with pytest.raises(ResyncNeeded):
        feed.handle({"feed": "book", "product_id": "PF_XBTUSD", "side": "buy", "seq": 12, "price": 99.5, "qty": 0.5, "timestamp": 2_000}, received_us=2)


def test_kraken_futures_trades_dedupe_across_snapshots_and_ticker_is_throttled() -> None:
    rows = Rows()
    feed = KrakenFuturesFeed(rows, ["PF_XBTUSD"], depth=2, ticker_interval_seconds=10)

    def trade(uid: str, at: int, kind: str = "fill") -> dict:
        return {"feed": "trade", "product_id": "PF_XBTUSD", "uid": uid, "side": "sell", "type": kind, "time": at, "qty": 0.1, "price": 100.0, "seq": at}

    for ack in ({"event": "info", "version": 1}, {"event": "subscribed", "feed": "trade", "product_ids": ["PF_XBTUSD"]}, {"event": "subscribed", "feed": "ticker", "product_ids": ["PF_XBTUSD"]}):
        feed.handle(ack, received_us=0)  # subscription acks carry a "feed" key but are not data
    feed.handle({"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": [trade("b", 2), trade("a", 1)]}, received_us=1)
    feed.handle(trade("c", 3, kind="liquidation"), received_us=2)
    feed.on_connect()
    feed.handle({"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": [trade("c", 3, kind="liquidation"), trade("b", 2)]}, received_us=3)

    trades = rows.rows[("kraken_futures", "trades")]
    assert [row["trade_id"] for row in trades] == ["a", "b", "c"]
    assert trades[2]["kind"] == "liquidation" and trades[0]["exchange_us"] == 1_000

    ticker = {"feed": "ticker", "product_id": "PF_XBTUSD", "time": 5, "markPrice": 100.1, "index": 100.0, "funding_rate": -0.005, "relative_funding_rate": -6e-8, "next_funding_rate_time": 3_600_000, "openInterest": 2177.2}
    for received in (0, 5_000_000, 10_000_000):
        feed.handle(ticker, received_us=received)
    tickers = rows.rows[("kraken_futures", "ticker")]
    assert [row["received_us"] for row in tickers] == [0, 10_000_000]
    assert tickers[0]["open_interest"] == 2177.2 and tickers[0]["next_funding_us"] == 3_600_000_000


def test_liquidation_sides_follow_each_exchanges_convention() -> None:
    rows = Rows()
    binance = BinanceLiquidationFeed(rows)
    binance.handle({"e": "forceOrder", "E": 1, "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "q": "0.014", "p": "9910", "ap": "9905", "X": "FILLED", "z": "0.014", "T": 7}}, received_us=1)
    bybit = BybitLiquidationFeed(rows, ["ETHUSDT"])
    bybit.handle({"topic": "allLiquidation.ETHUSDT", "type": "snapshot", "ts": 1, "data": [{"T": 8, "s": "ETHUSDT", "S": "Buy", "v": "2.5", "p": "2690.1"}]}, received_us=2)
    bybit.handle({"op": "pong", "success": True}, received_us=3)

    (binance_row,) = rows.rows[("binance", "liquidations")]
    (bybit_row,) = rows.rows[("bybit", "liquidations")]
    assert (binance_row["liquidated_side"], binance_row["qty"], binance_row["avg_price"], binance_row["exchange_us"]) == ("long", 0.014, 9905.0, 7_000)
    assert (bybit_row["liquidated_side"], bybit_row["raw_side"], bybit_row["qty"]) == ("long", "Buy", 2.5)
    assert bybit.subscriptions() == [{"op": "subscribe", "args": ["allLiquidation.ETHUSDT"]}]


def test_sink_compacts_finished_days_and_loader_reads_both_formats(tmp_path) -> None:
    day_one = 1_790_000_000_000_000 - (1_790_000_000_000_000 % DAY_US)
    sink = DailyCsvSink(tmp_path)
    sink.write("kraken_spot", "trades", TRADE_COLUMNS, (day_one + 5, day_one + 1, "BTC/USD", 100.0, 0.1, "buy", "limit", 1))
    sink.write("kraken_spot", "trades", TRADE_COLUMNS, (day_one + DAY_US + 5, day_one + DAY_US, "BTC/USD", 101.0, 0.2, "sell", "market", 2))
    sink.flush()
    today = pd.Timestamp(day_one + DAY_US, unit="us").date().isoformat()
    closed = sink.roll(today)
    assert [path.name for path in closed] == [f"{pd.Timestamp(day_one, unit='us').date()}.csv"]
    compact_finished_days(tmp_path, today=today)
    sink.close()

    # A crash mid-line leaves a partial row; the next run starts a new line and the loader skips the fragment.
    today_file = tmp_path / "kraken_spot" / "trades" / f"{today}.csv"
    with today_file.open("a") as handle:
        handle.write(f"{day_one + DAY_US + 6},{day_one + DAY_US},BTC/USD,10")
    sink = DailyCsvSink(tmp_path)
    sink.write("kraken_spot", "trades", TRADE_COLUMNS, (day_one + DAY_US + 7, day_one + DAY_US, "BTC/USD", 102.0, 0.3, "buy", "limit", 3))
    sink.close()

    assert sorted(path.suffix for path in (tmp_path / "kraken_spot" / "trades").iterdir()) == [".csv", ".parquet"]
    frame = load_market_data("kraken_spot", "trades", root=tmp_path)
    assert frame["trade_id"].tolist() == [1, 2, 3]
    assert str(frame["received_at"].dt.tz) == "UTC" and "exchange_time" in frame
    later = load_market_data("kraken_spot", "trades", root=tmp_path, start=pd.Timestamp(day_one + DAY_US, unit="us", tz="UTC"))
    assert later["trade_id"].tolist() == [2, 3]
    status = market_data_status(tmp_path)
    assert status.loc[0, "rows"] >= 3 and status.loc[0, "days"] == 2


def test_recording_gaps_from_events(tmp_path) -> None:
    sink = DailyCsvSink(tmp_path)
    columns = ("received_us", "feed", "event", "detail")
    base = 1_790_000_000_000_000
    second = 1_000_000
    for offset, feed, event, detail in [
        (0, "recorder", "started", "{}"),
        (1, "bybit", "connected", ""),
        (100, "bybit", "disconnected", "ConnectionClosed"),
        (130, "bybit", "connected", ""),
        (400, "recorder", "heartbeat", "{}"),
        (1000, "recorder", "started", "{}"),  # no "stopped": the process died after the heartbeat
        (1002, "bybit", "connected", ""),
        (1100, "recorder", "stopped", "interrupted"),
        (2000, "recorder", "started", "{}"),
        (2001, "bybit", "connected", ""),
    ]:
        sink.write("recorder", "events", columns, (base + offset * second, feed, event, detail))
    sink.close()

    gaps = recording_gaps(tmp_path)
    assert gaps["seconds"].tolist() == [30.0, 602.0, 901.0]
    assert gaps["reason"].tolist() == ["ConnectionClosed", "recorder died without stopping", "recorder stopped"]


class FakeConnection:
    def __init__(self, messages: list[str], then: BaseException | None) -> None:
        self.messages = list(messages)
        self.then = then
        self.sent: list[str] = []

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if self.then is not None:
            raise self.then
        await asyncio.sleep(3600)
        return ""


@pytest.mark.asyncio
async def test_recorder_reconnects_a_failed_feed_and_records_events(tmp_path) -> None:
    def liquidation(uid: int) -> str:
        return json.dumps({"topic": "allLiquidation.BTCUSDT", "data": [{"T": uid, "s": "BTCUSDT", "S": "Sell", "v": "1", "p": "100"}]})

    connections = [FakeConnection([liquidation(1)], ConnectionError("dropped")), FakeConnection([liquidation(2)], None)]
    opened: list[str] = []

    def connector(url, **kwargs):
        opened.append(url)
        return connections.pop(0)

    config = RecorderConfig(
        root=tmp_path, kraken_futures_symbols=(), kraken_spot_symbols=(), binance_liquidations=False, bybit_liquidation_symbols=("BTCUSDT",),
        book_interval_seconds=0.01, flush_interval_seconds=0.01, reconnect_initial_seconds=0.01,
    )
    recorder = MarketDataRecorder(config, connector=connector)
    await recorder.run(duration_seconds=0.3)

    assert len(opened) == 2
    liquidations = load_market_data("bybit", "liquidations", root=tmp_path)
    assert liquidations["liquidated_side"].tolist() == ["short", "short"]
    events = load_market_data("recorder", "events", root=tmp_path)
    assert events["event"].tolist() == ["started", "connected", "disconnected", "connected", "stopped"]
    assert "dropped" in events.loc[2, "detail"]
