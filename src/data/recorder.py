"""Record live public market data to disk: trades, order-book snapshots, perp tickers and liquidations.

Edges at minutes-to-hours horizons usually come from order flow (who is trading
aggressively) and the order book (where liquidity sits). Neither can be
downloaded afterwards at a sensible price, so this recorder captures them from
public WebSocket feeds as they happen. No API keys are needed.

- Kraken Futures perpetuals: every trade with its taker side and type (``fill``,
  ``liquidation``, ...), the full order book, and the ticker (mark and index
  price, funding, open interest).
- Kraken spot: every trade with its taker side, and the top of the book.
- Binance and Bybit USD-margined perpetuals: liquidations. Binance pushes at
  most one liquidation per symbol per second (the largest), so it undercounts
  in cascades. Bybit pushes all of them.

Each book is rebuilt in memory from the exchange's snapshot plus updates. It is
checked (the Kraken spot checksum, Kraken Futures sequence numbers) and sampled
every ``book_interval_seconds`` as the top ``book_depth`` levels. A failed check,
a silent feed or a dropped connection reconnects that feed only, with backoff.
Every start, stop, connect and disconnect is written to the ``recorder/events``
channel so research can find and exclude the gaps (``recording_gaps``).

Files are ``<root>/<venue>/<channel>/<YYYY-MM-DD>.csv`` for the current UTC day,
appended and flushed every few seconds. They are compacted to ``.parquet`` once
the day is over. ``load_market_data`` reads both. Timestamps are integer
microseconds since the epoch: ``received_us`` is when this machine got the
message (or sampled the book), ``exchange_us`` is the exchange's own time.
"""

from __future__ import annotations

import asyncio
import csv
import heapq
import io
import json
import time
import zlib
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import pandas as pd
from loguru import logger

from src.utils.telemetry import WebSocketReconnectHandler

DEFAULT_ROOT = Path("data/market_data")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

TRADE_COLUMNS = ("received_us", "exchange_us", "symbol", "price", "qty", "side", "kind", "trade_id")
TICKER_COLUMNS = (
    "received_us", "exchange_us", "symbol", "mark_price", "index_price", "last", "bid", "ask",
    "funding_rate", "funding_rate_prediction", "relative_funding_rate", "relative_funding_rate_prediction",
    "next_funding_us", "open_interest", "volume_24h", "volume_quote_24h",
)
LIQUIDATION_COLUMNS = ("received_us", "exchange_us", "symbol", "liquidated_side", "raw_side", "price", "avg_price", "qty", "status")
EVENT_COLUMNS = ("received_us", "feed", "event", "detail")
DEPTH_BANDS_BPS = (10, 25, 50, 100)

Emit = Callable[[str, str, tuple[str, ...], tuple[Any, ...]], None]


class ResyncNeeded(Exception):
    """The local book no longer matches the exchange's; the feed must reconnect for a fresh snapshot."""


def now_us() -> int:
    """Current UTC time in integer microseconds."""
    return time.time_ns() // 1_000


def iso_to_us(text: str) -> int:
    """Kraken's ISO-8601 timestamps to integer microseconds, without float rounding."""
    return (datetime.fromisoformat(text) - EPOCH) // timedelta(microseconds=1)


def utc_day(microseconds: int) -> str:
    """The UTC date (YYYY-MM-DD) that a microsecond timestamp falls on; each day gets its own file."""
    return (EPOCH + timedelta(microseconds=microseconds)).date().isoformat()


def book_columns(depth: int, *, bands: bool = False) -> tuple[str, ...]:
    """Column names of a book row: the top `depth` levels, plus depth within bands of the mid when `bands`."""
    columns = ["received_us", "exchange_us", "symbol"]
    for side in ("bid", "ask"):
        columns += [f"{side}_px_{level}" for level in range(1, depth + 1)]
        columns += [f"{side}_qty_{level}" for level in range(1, depth + 1)]
    if bands:
        for side in ("bid", "ask"):
            columns += [f"{side}_depth_{bps}bps" for bps in DEPTH_BANDS_BPS]
    return tuple(columns)


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    """What to record and how often. Load from JSON with `from_json`; unknown keys are rejected.

    Attributes:
        root: Where the files go.
        kraken_futures_symbols: Kraken Futures product ids, e.g. PF_XBTUSD.
        kraken_spot_symbols: Kraken spot pairs in WebSocket v2 form, e.g. BTC/USD.
        binance_liquidations: Record Binance's all-market USD-M liquidation stream.
        bybit_liquidation_symbols: Bybit linear symbols whose liquidations to record.
        book_depth: Levels per side in each book sample.
        book_interval_seconds: How often books are sampled.
        ticker_interval_seconds: How often each Kraken Futures ticker is written (it updates ~2x a second).
        flush_interval_seconds: How often files are flushed to disk (the most a crash can lose).
        heartbeat_seconds: How often row counts are logged and written as a `heartbeat` event.
        reconnect_initial_seconds: First reconnect delay; it doubles up to `reconnect_max_seconds`.
        reconnect_max_seconds: Longest reconnect delay.
    """

    root: Path = DEFAULT_ROOT
    kraken_futures_symbols: tuple[str, ...] = ("PF_XBTUSD", "PF_ETHUSD")
    kraken_spot_symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD")
    binance_liquidations: bool = True
    bybit_liquidation_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
    book_depth: int = 10
    book_interval_seconds: float = 1.0
    ticker_interval_seconds: float = 10.0
    flush_interval_seconds: float = 5.0
    heartbeat_seconds: float = 300.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0

    @classmethod
    def from_json(cls, path: Path | str) -> "RecorderConfig":
        """Read a config file; lists become tuples and `root` a Path."""
        raw = json.loads(Path(path).read_text())
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"Unknown recorder config keys: {unknown}")
        values = {key: tuple(value) if isinstance(value, list) else value for key, value in raw.items()}
        if "root" in values:
            values["root"] = Path(values["root"])
        return cls(**values)

    def describe(self) -> str:
        """The config as compact JSON, written into the `started` event."""
        return json.dumps({key: str(value) if isinstance(value, Path) else value for key, value in asdict(self).items()})


class L2Book:
    """Price -> size on each side, rebuilt from a snapshot and kept current with updates.

    Args:
        max_levels: Keep only this many best levels per side after each update
            (Kraken spot sends a book of fixed depth and expects the client to
            drop levels that fall out of it). None keeps everything.
    """

    def __init__(self, max_levels: int | None = None) -> None:
        """Start empty and unsynced."""
        self.max_levels = max_levels
        self.bids: dict[Any, Any] = {}
        self.asks: dict[Any, Any] = {}
        self.exchange_us: int | None = None
        self.synced = False

    def clear(self) -> None:
        """Forget everything, e.g. before a snapshot or after a reconnect."""
        self.bids.clear()
        self.asks.clear()
        self.exchange_us = None
        self.synced = False

    def apply(self, side: str, price: Any, qty: Any) -> None:
        """Set one level; a size of zero removes it."""
        levels = self.bids if side == "bid" else self.asks
        if qty == 0:
            levels.pop(price, None)
        else:
            levels[price] = qty

    def truncate(self) -> None:
        """Drop levels beyond `max_levels` on each side."""
        if self.max_levels is None:
            return
        if len(self.bids) > self.max_levels:
            keep = set(heapq.nlargest(self.max_levels, self.bids))
            self.bids = {price: qty for price, qty in self.bids.items() if price in keep}
        if len(self.asks) > self.max_levels:
            keep = set(heapq.nsmallest(self.max_levels, self.asks))
            self.asks = {price: qty for price, qty in self.asks.items() if price in keep}

    def top(self, depth: int) -> tuple[list[tuple[Any, Any]], list[tuple[Any, Any]]]:
        """The best `depth` bids (high to low) and asks (low to high)."""
        bids = [(price, self.bids[price]) for price in heapq.nlargest(depth, self.bids)]
        asks = [(price, self.asks[price]) for price in heapq.nsmallest(depth, self.asks)]
        return bids, asks

    def depth_within(self, bands_bps: Iterable[int]) -> tuple[list[float], list[float]]:
        """Total size on each side within each band of the mid price; deeper liquidity than the top levels show."""
        bids, asks = self.top(1)
        if not bids or not asks:
            return [], []
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        bid_depth, ask_depth = [], []
        for bps in bands_bps:
            floor, ceiling = mid * (1.0 - bps / 10_000.0), mid * (1.0 + bps / 10_000.0)
            bid_depth.append(sum(float(qty) for price, qty in self.bids.items() if float(price) >= floor))
            ask_depth.append(sum(float(qty) for price, qty in self.asks.items() if float(price) <= ceiling))
        return bid_depth, ask_depth


def kraken_spot_checksum(bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]]) -> int:
    """CRC32 of the top 10 levels as Kraken's WebSocket v2 defines it.

    Asks from low to high, then bids from high to low; each level is its price
    then its size, written as sent (e.g. "0.00010000") with the decimal point
    and leading zeros removed. The values must be parsed as Decimal so trailing
    zeros survive.
    """

    def digits(value: Decimal) -> str:
        return format(value, "f").replace(".", "").lstrip("0")

    text = "".join(digits(price) + digits(qty) for price, qty in asks[:10])
    text += "".join(digits(price) + digits(qty) for price, qty in bids[:10])
    return zlib.crc32(text.encode())


class _RecentIds:
    """The last `size` ids seen, to drop trades that a reconnect's snapshot sends again."""

    def __init__(self, size: int = 5_000) -> None:
        self._order: deque[str] = deque(maxlen=size)
        self._ids: set[str] = set()

    def add(self, trade_id: str) -> bool:
        """Remember `trade_id`; False if it was already seen."""
        if trade_id in self._ids:
            return False
        if len(self._order) == self._order.maxlen:
            self._ids.discard(self._order[0])
        self._order.append(trade_id)
        self._ids.add(trade_id)
        return True


def _number(value: Any) -> float | None:
    return None if value is None or value == "" else float(value)


class Feed:
    """One WebSocket connection: what to subscribe to and how its messages become rows.

    Subclasses set `name` (also the venue folder), `url` and `stale_after_seconds`
    (reconnect when nothing arrives for this long; None never reconnects a
    quiet feed).
    """

    name = "feed"
    url = ""
    stale_after_seconds: float | None = 30.0
    decimal_floats = False

    def __init__(self, emit: Emit) -> None:
        """Rows go to `emit(venue, channel, columns, row)`."""
        self.emit = emit

    def subscriptions(self) -> list[dict[str, Any]]:
        """Messages sent right after connecting."""
        return []

    def on_connect(self) -> None:
        """Reset per-connection state (books must be rebuilt from a new snapshot)."""

    def handle(self, message: Any, received_us: int) -> None:
        """Turn one parsed message into rows. Raise ResyncNeeded when a book check fails."""

    def sample(self, received_us: int) -> None:
        """Write a snapshot of each synced book."""

    async def keepalive(self, send: Callable[[str], Awaitable[None]]) -> None:
        """Application-level pings for venues that need them; runs for as long as the connection does."""


class KrakenSpotFeed(Feed):
    """Kraken spot WebSocket v2: trades (taker side) and a checksum-verified book."""

    name = "kraken_spot"
    url = "wss://ws.kraken.com/v2"
    decimal_floats = True
    SUBSCRIBE_DEPTHS = (10, 25, 100, 500, 1000)

    def __init__(self, emit: Emit, symbols: Iterable[str], depth: int) -> None:
        """Subscribe to the smallest book Kraken offers that covers `depth` levels."""
        super().__init__(emit)
        self.symbols = list(symbols)
        self.depth = depth
        self.subscribed_depth = min(d for d in self.SUBSCRIBE_DEPTHS if d >= depth)
        self.books = {symbol: L2Book(self.subscribed_depth) for symbol in self.symbols}
        self.last_trade_id: dict[str, int] = {}
        self.columns = book_columns(depth)

    def subscriptions(self) -> list[dict[str, Any]]:
        """Trades with a snapshot of the last 50 (to fill small reconnect gaps) and the book."""
        return [
            {"method": "subscribe", "params": {"channel": "trade", "symbol": self.symbols, "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "book", "symbol": self.symbols, "depth": self.subscribed_depth}},
        ]

    def on_connect(self) -> None:
        """Books start over from the snapshot the new subscription sends."""
        for book in self.books.values():
            book.clear()

    def handle(self, message: Any, received_us: int) -> None:
        """Trades are deduplicated by Kraken's increasing trade id; book updates are checksummed."""
        channel = message.get("channel")
        if channel == "trade":
            for trade in message.get("data", []):
                symbol, trade_id = trade["symbol"], int(trade["trade_id"])
                if trade_id <= self.last_trade_id.get(symbol, -1):
                    continue
                self.last_trade_id[symbol] = trade_id
                row = (received_us, iso_to_us(trade["timestamp"]), symbol, float(trade["price"]), float(trade["qty"]), trade["side"], trade.get("ord_type", ""), trade_id)
                self.emit(self.name, "trades", TRADE_COLUMNS, row)
        elif channel == "book":
            for entry in message.get("data", []):
                book = self.books.get(entry.get("symbol"))
                if book is None:
                    continue
                if message.get("type") == "snapshot":
                    book.clear()
                elif not book.synced:
                    continue
                for level in entry.get("bids", []):
                    book.apply("bid", level["price"], level["qty"])
                for level in entry.get("asks", []):
                    book.apply("ask", level["price"], level["qty"])
                book.truncate()
                if entry.get("timestamp"):
                    book.exchange_us = iso_to_us(entry["timestamp"])
                expected = entry.get("checksum")
                if expected is not None and kraken_spot_checksum(*book.top(10)) != int(expected):
                    raise ResyncNeeded(f"{entry['symbol']} book checksum mismatch")
                book.synced = True
        elif message.get("method") == "subscribe" and message.get("success") is False:
            logger.error("Kraken spot subscription failed: {}", message.get("error"))

    def sample(self, received_us: int) -> None:
        """One row per synced book: the top `depth` levels."""
        for symbol, book in self.books.items():
            if book.synced:
                self.emit(self.name, f"book{self.depth}", self.columns, _book_row(received_us, symbol, book, self.depth))


class KrakenFuturesFeed(Feed):
    """Kraken Futures WebSocket v1: trades (taker side and type), the full book (sequence-checked) and the ticker."""

    name = "kraken_futures"
    url = "wss://futures.kraken.com/ws/v1"

    def __init__(self, emit: Emit, symbols: Iterable[str], depth: int, ticker_interval_seconds: float) -> None:
        """Keep the full book per product so depth bands beyond the top levels can be measured."""
        super().__init__(emit)
        self.symbols = list(symbols)
        self.depth = depth
        self.ticker_interval_us = int(ticker_interval_seconds * 1_000_000)
        self.books = {symbol: L2Book() for symbol in self.symbols}
        self.sequence: dict[str, int] = {}
        self.recent_trades = _RecentIds()
        self.last_ticker_us: dict[str, int] = {}
        self.columns = book_columns(depth, bands=True)

    def subscriptions(self) -> list[dict[str, Any]]:
        """One subscription per feed for all products."""
        return [{"event": "subscribe", "feed": feed, "product_ids": self.symbols} for feed in ("trade", "book", "ticker")]

    def on_connect(self) -> None:
        """Books and sequence numbers start over; recent trade ids are kept to drop the snapshot's repeats."""
        for book in self.books.values():
            book.clear()
        self.sequence.clear()

    def handle(self, message: Any, received_us: int) -> None:
        """Route by feed. Book updates must arrive with consecutive sequence numbers per product."""
        event = message.get("event")
        if event is not None:  # info, subscribed, and errors; subscription acks also carry a "feed" key
            if event in ("error", "alert", "subscribed_failed"):
                logger.error("Kraken Futures feed {}: {}", event, message.get("message"))
            return
        feed = message.get("feed")
        if feed == "book":
            symbol = message["product_id"]
            book = self.books.get(symbol)
            if book is None or not book.synced:
                return
            if message["seq"] != self.sequence[symbol] + 1:
                raise ResyncNeeded(f"{symbol} book sequence jumped from {self.sequence[symbol]} to {message['seq']}")
            self.sequence[symbol] = message["seq"]
            book.apply("bid" if message["side"] == "buy" else "ask", message["price"], message["qty"])
            book.exchange_us = int(message["timestamp"]) * 1_000
        elif feed == "trade":
            self._trade(message, received_us)
        elif feed == "ticker":
            self._ticker(message, received_us)
        elif feed == "book_snapshot":
            symbol = message["product_id"]
            book = self.books.get(symbol)
            if book is None:
                return
            book.clear()
            for level in message.get("bids", []):
                book.apply("bid", level["price"], level["qty"])
            for level in message.get("asks", []):
                book.apply("ask", level["price"], level["qty"])
            book.exchange_us = int(message["timestamp"]) * 1_000
            self.sequence[symbol] = message["seq"]
            book.synced = True
        elif feed == "trade_snapshot":
            for trade in sorted(message.get("trades", []), key=lambda item: (item["time"], item.get("seq", 0))):
                self._trade(trade, received_us)

    def _trade(self, trade: dict[str, Any], received_us: int) -> None:
        if not self.recent_trades.add(str(trade["uid"])):
            return
        row = (received_us, int(trade["time"]) * 1_000, trade["product_id"], float(trade["price"]), float(trade["qty"]), trade["side"], trade.get("type", ""), trade["uid"])
        self.emit(self.name, "trades", TRADE_COLUMNS, row)

    def _ticker(self, ticker: dict[str, Any], received_us: int) -> None:
        symbol = ticker["product_id"]
        last = self.last_ticker_us.get(symbol)
        if last is not None and received_us - last < self.ticker_interval_us:
            return
        self.last_ticker_us[symbol] = received_us
        next_funding = ticker.get("next_funding_rate_time")
        row = (
            received_us, int(ticker["time"]) * 1_000, symbol, _number(ticker.get("markPrice")), _number(ticker.get("index")),
            _number(ticker.get("last")), _number(ticker.get("bid")), _number(ticker.get("ask")),
            _number(ticker.get("funding_rate")), _number(ticker.get("funding_rate_prediction")),
            _number(ticker.get("relative_funding_rate")), _number(ticker.get("relative_funding_rate_prediction")),
            int(next_funding) * 1_000 if next_funding else None, _number(ticker.get("openInterest")),
            _number(ticker.get("volume")), _number(ticker.get("volumeQuote")),
        )
        self.emit(self.name, "ticker", TICKER_COLUMNS, row)

    def sample(self, received_us: int) -> None:
        """One row per synced book: the top `depth` levels and the size within 10/25/50/100 bps of the mid."""
        for symbol, book in self.books.items():
            if book.synced:
                bid_depth, ask_depth = book.depth_within(DEPTH_BANDS_BPS)
                row = _book_row(received_us, symbol, book, self.depth) + tuple(bid_depth or [None] * len(DEPTH_BANDS_BPS)) + tuple(ask_depth or [None] * len(DEPTH_BANDS_BPS))
                self.emit(self.name, f"book{self.depth}", self.columns, row)


class BinanceLiquidationFeed(Feed):
    """Binance USD-M futures liquidations for every symbol (at most one per symbol per second).

    The URL is the `/market` path. The legacy `/ws/!forceOrder@arr` path still
    accepts connections but no longer sends anything (checked 2026-09-26), so
    the feed also reconnects after 15 silent minutes: across all symbols a
    liquidation arrives every few seconds in normal markets.
    """

    name = "binance"
    url = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
    stale_after_seconds = 900.0

    def handle(self, message: Any, received_us: int) -> None:
        """A SELL liquidation order closes a long, a BUY closes a short."""
        if message.get("e") != "forceOrder":
            return
        order = message["o"]
        row = (
            received_us, int(order["T"]) * 1_000, order["s"], "long" if order["S"] == "SELL" else "short", order["S"],
            float(order["p"]), _number(order.get("ap")), float(order.get("z") or order["q"]), order.get("X", ""),
        )
        self.emit(self.name, "liquidations", LIQUIDATION_COLUMNS, row)


class BybitLiquidationFeed(Feed):
    """Bybit linear-perp liquidations, all of them, per symbol."""

    name = "bybit"
    url = "wss://stream.bybit.com/v5/public/linear"
    stale_after_seconds = 90.0  # quiet symbols can go an hour without a liquidation, but the pings are answered
    PING_SECONDS = 20.0

    def __init__(self, emit: Emit, symbols: Iterable[str]) -> None:
        """Subscribe to `allLiquidation.<symbol>` for each symbol."""
        super().__init__(emit)
        self.symbols = list(symbols)

    def subscriptions(self) -> list[dict[str, Any]]:
        """Bybit accepts at most 10 topics per subscribe request."""
        topics = [f"allLiquidation.{symbol}" for symbol in self.symbols]
        return [{"op": "subscribe", "args": topics[start : start + 10]} for start in range(0, len(topics), 10)]

    def handle(self, message: Any, received_us: int) -> None:
        """Bybit's side is the liquidated position's side: Buy means a long was liquidated (the opposite of Binance)."""
        if not str(message.get("topic", "")).startswith("allLiquidation."):
            if message.get("op") == "subscribe" and message.get("success") is False:
                logger.error("Bybit subscription failed: {}", message.get("ret_msg"))
            return
        for item in message.get("data", []):
            row = (received_us, int(item["T"]) * 1_000, item["s"], "long" if item["S"] == "Buy" else "short", item["S"], float(item["p"]), None, float(item["v"]), "")
            self.emit(self.name, "liquidations", LIQUIDATION_COLUMNS, row)

    async def keepalive(self, send: Callable[[str], Awaitable[None]]) -> None:
        """Bybit drops connections without an application-level ping about every 20 seconds."""
        while True:
            await asyncio.sleep(self.PING_SECONDS)
            await send(json.dumps({"op": "ping"}))


def _book_row(received_us: int, symbol: str, book: L2Book, depth: int) -> tuple[Any, ...]:
    bids, asks = book.top(depth)
    values: list[Any] = [received_us, book.exchange_us, symbol]
    for levels in (bids, asks):
        padded = levels + [(None, None)] * (depth - len(levels))
        values += [None if price is None else float(price) for price, _ in padded]
        values += [None if qty is None else float(qty) for _, qty in padded]
    return tuple(values)


def build_feeds(config: RecorderConfig, emit: Emit) -> list[Feed]:
    """The feeds a config asks for; empty symbol lists switch a venue off."""
    feeds: list[Feed] = []
    if config.kraken_futures_symbols:
        feeds.append(KrakenFuturesFeed(emit, config.kraken_futures_symbols, config.book_depth, config.ticker_interval_seconds))
    if config.kraken_spot_symbols:
        feeds.append(KrakenSpotFeed(emit, config.kraken_spot_symbols, config.book_depth))
    if config.binance_liquidations:
        feeds.append(BinanceLiquidationFeed(emit))
    if config.bybit_liquidation_symbols:
        feeds.append(BybitLiquidationFeed(emit, config.bybit_liquidation_symbols))
    return feeds


class DailyCsvSink:
    """Append rows to one CSV file per venue, channel and UTC day.

    CSV is used while a day is being written because appending is cheap and a
    crash loses at most the rows since the last flush. `roll` closes finished
    days so they can be compacted to Parquet.
    """

    def __init__(self, root: Path | str) -> None:
        """Files go under `root`."""
        self.root = Path(root)
        self._files: dict[tuple[str, str, str], tuple[TextIO, Any]] = {}

    def write(self, venue: str, channel: str, columns: tuple[str, ...], row: tuple[Any, ...]) -> None:
        """Append `row` (its first value is `received_us`) to the file for its day."""
        day = utc_day(row[0])
        key = (venue, channel, day)
        entry = self._files.get(key)
        if entry is None:
            entry = self._open(self.root / venue / channel / f"{day}.csv", columns)
            self._files[key] = entry
        entry[1].writerow(row)

    @staticmethod
    def _open(path: Path, columns: tuple[str, ...]) -> tuple[TextIO, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.exists() and path.stat().st_size > 0
        if existing:
            _drop_partial_last_line(path)
        handle = path.open("a", newline="")
        writer = csv.writer(handle)
        if not existing:
            writer.writerow(columns)
        return handle, writer

    def flush(self) -> None:
        """Push buffered rows to disk."""
        for handle, _ in self._files.values():
            handle.flush()

    def roll(self, today: str) -> list[Path]:
        """Close files for days before `today` and return their paths."""
        closed = []
        for key in [key for key in self._files if key[2] < today]:
            handle, _ = self._files.pop(key)
            handle.close()
            closed.append(self.root / key[0] / key[1] / f"{key[2]}.csv")
        return closed

    def close(self) -> None:
        """Flush and close every file."""
        for handle, _ in self._files.values():
            handle.close()
        self._files.clear()


def _drop_partial_last_line(path: Path) -> None:
    """Cut a row that a crash left half-written, so the next row doesn't merge into it."""
    with path.open("r+b") as handle:
        size = handle.seek(0, 2)
        tail_start = max(0, size - 1_048_576)
        handle.seek(tail_start)
        tail = handle.read()
        if tail.endswith(b"\n"):
            return
        cut = tail.rfind(b"\n")
        handle.truncate(tail_start + cut + 1 if cut >= 0 else 0)


def _read_day_csv(path: Path) -> pd.DataFrame:
    """Read a day file up to its last complete line; the recorder may be halfway through writing the next one."""
    data = path.read_bytes()
    data = data[: data.rfind(b"\n") + 1]
    if not data:
        return pd.DataFrame()
    return pd.read_csv(io.BytesIO(data), on_bad_lines="skip")


def compact_csv(path: Path) -> Path:
    """Convert a finished day's CSV to Parquet (about 5-10x smaller) and delete the CSV.

    If the Parquet file already exists (a crash between writing it and deleting
    the CSV, or a restart that appended more rows), both are merged and exact
    duplicate rows dropped.
    """
    target = path.with_suffix(".parquet")
    frame = _read_day_csv(path)
    if target.exists():
        frame = pd.concat([pd.read_parquet(target), frame], ignore_index=True).drop_duplicates()
    temporary = target.with_suffix(".parquet.tmp")
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(target)
    path.unlink()
    return target


def compact_finished_days(root: Path | str = DEFAULT_ROOT, *, today: str | None = None) -> list[Path]:
    """Compact every CSV from before `today` (UTC), e.g. ones left behind when the recorder was stopped overnight."""
    today = today or utc_day(now_us())
    return [compact_csv(path) for path in sorted(Path(root).glob("*/*/*.csv")) if path.stem < today]


Connector = Callable[..., Any]


def _default_connector(url: str, **kwargs: Any) -> Any:
    from websockets.asyncio.client import connect

    return connect(url, **kwargs)


class MarketDataRecorder:
    """Run every feed concurrently until stopped, writing rows through a `DailyCsvSink`.

    Each feed reconnects on its own with exponential backoff, so one venue
    failing never stops the others. Books are sampled on a fixed clock, files
    are flushed every `flush_interval_seconds`, and finished days are
    compacted in a worker thread so the event loop never blocks on disk.
    """

    def __init__(self, config: RecorderConfig, *, connector: Connector | None = None) -> None:
        """`connector(url, **kwargs)` returns an async context manager with `send`/`recv` (websockets by default)."""
        self.config = config
        self.sink = DailyCsvSink(config.root)
        self.feeds = build_feeds(config, self._emit)
        self.counts: Counter[str] = Counter()
        self.parse_errors = 0
        self._connector = connector or _default_connector
        self._stop: asyncio.Event | None = None
        self._stop_reason = "finished"

    def _emit(self, venue: str, channel: str, columns: tuple[str, ...], row: tuple[Any, ...]) -> None:
        self.sink.write(venue, channel, columns, row)
        self.counts[f"{venue}/{channel}"] += 1

    def _event(self, feed: str, event: str, detail: str = "") -> None:
        self._emit("recorder", "events", EVENT_COLUMNS, (now_us(), feed, event, detail))
        log = logger.warning if event == "disconnected" else logger.info
        log("market data recorder: {} {} {}", feed, event, detail)

    def stop(self, reason: str = "stopped") -> None:
        """Ask `run` to finish (safe to call from a signal handler)."""
        self._stop_reason = reason
        if self._stop is not None:
            self._stop.set()

    async def run(self, duration_seconds: float | None = None) -> None:
        """Record until `stop()`, cancellation (Ctrl-C) or `duration_seconds` passes."""
        self._stop = asyncio.Event()
        compacted = await asyncio.to_thread(compact_finished_days, self.config.root)
        if compacted:
            logger.info("market data recorder: compacted {} finished day files", len(compacted))
        self._event("recorder", "started", self.config.describe())
        tasks = [asyncio.create_task(self._run_feed(feed), name=f"record-{feed.name}") for feed in self.feeds]
        tasks += [asyncio.create_task(self._sample_books()), asyncio.create_task(self._housekeeping())]
        try:
            if duration_seconds is None:
                await self._stop.wait()
            else:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=duration_seconds)
                except TimeoutError:
                    self._stop_reason = f"duration of {duration_seconds:g}s reached"
        except asyncio.CancelledError:
            self._stop_reason = "interrupted"
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._event("recorder", "stopped", self._stop_reason)
            self.sink.close()

    async def _run_feed(self, feed: Feed) -> None:
        backoff = WebSocketReconnectHandler(initial_delay_seconds=self.config.reconnect_initial_seconds, max_delay_seconds=self.config.reconnect_max_seconds)
        loop = asyncio.get_running_loop()
        while True:
            connected_at: float | None = None
            try:
                async with self._connector(feed.url, open_timeout=15, close_timeout=2, ping_interval=20, ping_timeout=20, max_size=2**24) as connection:
                    feed.on_connect()
                    for subscription in feed.subscriptions():
                        await connection.send(json.dumps(subscription))
                    connected_at = loop.time()
                    self._event(feed.name, "connected", feed.url)
                    keepalive = asyncio.create_task(feed.keepalive(connection.send))
                    try:
                        await self._consume(feed, connection)
                    finally:
                        keepalive.cancel()
                reason = "connection closed"
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                reason = f"no data for {feed.stale_after_seconds:g}s" if connected_at is not None else "connect timed out"
            except ResyncNeeded as exc:
                reason = f"resync: {exc}"
            except Exception as exc:  # noqa: BLE001 - any network or protocol failure only restarts this feed
                reason = f"{type(exc).__name__}: {exc}"
            self._event(feed.name, "disconnected", reason)
            if connected_at is not None and loop.time() - connected_at > 60.0:
                backoff.reset()
            await asyncio.sleep(backoff.handle_disconnect())

    async def _consume(self, feed: Feed, connection: Any) -> None:
        while True:
            if feed.stale_after_seconds is None:
                raw = await connection.recv()
            else:
                raw = await asyncio.wait_for(connection.recv(), timeout=feed.stale_after_seconds)
            received = now_us()
            try:
                message = json.loads(raw, parse_float=Decimal) if feed.decimal_floats else json.loads(raw)
                feed.handle(message, received)
            except ResyncNeeded:
                raise
            except Exception as exc:  # noqa: BLE001 - one malformed message must not drop the connection
                self.parse_errors += 1
                if self.parse_errors <= 5 or self.parse_errors % 1000 == 0:
                    logger.warning("market data recorder: {} could not parse a message ({} so far): {!r}", feed.name, self.parse_errors, exc)

    async def _sample_books(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self.config.book_interval_seconds
        next_tick = loop.time()
        while True:
            next_tick += interval
            await asyncio.sleep(max(0.0, next_tick - loop.time()))
            sampled_at = now_us()
            for feed in self.feeds:
                feed.sample(sampled_at)

    async def _housekeeping(self) -> None:
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        last_counts: Counter[str] = Counter()
        while True:
            await asyncio.sleep(self.config.flush_interval_seconds)
            self.sink.flush()
            for path in self.sink.roll(utc_day(now_us())):
                await asyncio.to_thread(compact_csv, path)
            if loop.time() - last_heartbeat >= self.config.heartbeat_seconds:
                last_heartbeat = loop.time()
                since = {key: value - last_counts.get(key, 0) for key, value in self.counts.items() if key != "recorder/events"}
                last_counts = Counter(self.counts)
                self._event("recorder", "heartbeat", json.dumps({"rows": since, "parse_errors": self.parse_errors}))


def _as_timestamp(value: str | datetime | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def load_market_data(
    venue: str,
    channel: str,
    *,
    start: str | datetime | pd.Timestamp | None = None,
    end: str | datetime | pd.Timestamp | None = None,
    root: Path | str = DEFAULT_ROOT,
) -> pd.DataFrame:
    """Recorded rows for one venue and channel (e.g. "kraken_futures", "trades"), oldest first.

    Adds `received_at` and `exchange_time` as UTC datetimes. `start`/`end`
    filter on `received_at` (end exclusive). Returns an empty frame when
    nothing was recorded.
    """
    folder = Path(root) / venue / channel
    begin, finish = _as_timestamp(start), _as_timestamp(end)
    frames = []
    for path in sorted(folder.glob("*.parquet")) + sorted(folder.glob("*.csv")):
        day = date.fromisoformat(path.stem)
        if (begin is not None and day < begin.date()) or (finish is not None and day > finish.date()):
            continue
        frames.append(pd.read_parquet(path) if path.suffix == ".parquet" else _read_day_csv(path))
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True).sort_values("received_us", kind="stable")
    frame.insert(0, "received_at", pd.to_datetime(frame["received_us"], unit="us", utc=True))
    if "exchange_us" in frame:
        frame.insert(1, "exchange_time", pd.to_datetime(frame["exchange_us"], unit="us", utc=True))
    if begin is not None:
        frame = frame[frame["received_at"] >= begin]
    if finish is not None:
        frame = frame[frame["received_at"] < finish]
    return frame.reset_index(drop=True)


def recording_gaps(root: Path | str = DEFAULT_ROOT, *, min_seconds: float = 5.0) -> pd.DataFrame:
    """Periods when a feed was not connected, from the `recorder/events` channel.

    A gap runs from a disconnect (or the recorder stopping) to that feed's next
    connect. If the recorder died without a `stopped` event (a crash, or the
    machine sleeping), the gap starts at the last event before the restart,
    which is at most one heartbeat late. Gaps still open now aren't listed.
    """
    events = load_market_data("recorder", "events", root=root)
    columns = ["feed", "gap_from", "gap_to", "seconds", "reason"]
    if events.empty:
        return pd.DataFrame(columns=columns)
    connected: dict[str, bool] = {}
    down_since: dict[str, tuple[pd.Timestamp, str]] = {}
    gaps = []
    last_seen: pd.Timestamp | None = None
    for row in events.itertuples(index=False):
        at, feed, event = row.received_at, row.feed, row.event
        detail = "" if pd.isna(row.detail) else str(row.detail)
        if event == "started" and last_seen is not None:
            for name, is_up in connected.items():
                if is_up:
                    down_since[name] = (last_seen, "recorder died without stopping")
                connected[name] = False
        elif event == "stopped":
            for name, is_up in connected.items():
                if is_up:
                    down_since[name] = (at, "recorder stopped")
                connected[name] = False
        elif event == "connected":
            if feed in down_since:
                began, reason = down_since.pop(feed)
                gaps.append({"feed": feed, "gap_from": began, "gap_to": at, "seconds": (at - began).total_seconds(), "reason": reason})
            connected[feed] = True
        elif event == "disconnected":
            if connected.get(feed):
                down_since[feed] = (at, detail)
            connected[feed] = False
        last_seen = at
    table = pd.DataFrame(gaps, columns=columns)
    return table[table["seconds"] >= min_seconds].reset_index(drop=True)


def market_data_status(root: Path | str = DEFAULT_ROOT) -> pd.DataFrame:
    """Rows, days and disk use per venue and channel, to check the recorder is doing its job."""
    import pyarrow.parquet as pq

    rows = []
    for folder in sorted(path for path in Path(root).glob("*/*") if path.is_dir()):
        files = sorted(folder.glob("*.parquet")) + sorted(folder.glob("*.csv"))
        if not files:
            continue
        count = 0
        for path in files:
            if path.suffix == ".parquet":
                count += pq.ParquetFile(path).metadata.num_rows
            else:
                with path.open("rb") as handle:
                    count += max(sum(1 for _ in handle) - 1, 0)
        days = sorted({path.stem for path in files})
        rows.append({
            "venue": folder.parent.name, "channel": folder.name, "rows": count, "days": len(days),
            "first_day": days[0], "last_day": days[-1], "size_mb": round(sum(path.stat().st_size for path in files) / 1e6, 1),
        })
    return pd.DataFrame(rows)

