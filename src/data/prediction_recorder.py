"""Record prediction-market quotes and order books (Kalshi, Polymarket) for crypto price markets. Public data, no keys.

Why: a venue keeps its trade history but not its order book. What a bet could have been bought or sold for, and how
much of it, exists only while it is on the screen. The paper exchange for bets and every backtest of a betting
strategy need exactly that, so it is recorded from now on.

Each cycle, per venue and coin:

- `quotes/<day>.csv`: every market with both a buyer and a seller quoting: best Yes bid and ask, volume, and the
  coin's spot price at that moment.
- `depth/<day>.csv`: the top `LEVELS` price levels on each side for the markets that settle soonest (at most
  `max_books` per venue and coin, within `horizon_hours`). Prices are Yes prices; a No bid at q is a Yes ask at 1 - q.
- `markets.csv`: one row per market the first time it has a quote recorded: the question, what it pays on, its settlement rule.
- `results/kalshi.csv`, `results/polymarket.csv`: how each market settled (checked hourly). Kalshi's come from its
  list of settled markets; Polymarket's are looked up one by one for the markets this recorder has described.

Files are append-only CSV, so a killed process loses nothing. One venue failing leaves the other's rows intact.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.research import prediction_markets as pm

ROOT = Path("data/market_data/prediction_markets")
LEVELS = 5
QUOTE_COLUMNS = ("time", "venue", "market_id", "underlying", "kind", "floor", "cap", "expiry", "yes_bid", "yes_ask", "volume", "spot")
DEPTH_COLUMNS = ("time", "venue", "market_id", "side", "level", "price", "size")
MARKET_COLUMNS = ("first_seen", "venue", "market_id", "underlying", "kind", "floor", "cap", "start", "expiry", "fee_rate", "averaging_seconds", "settles_on", "yes_token", "question")
RESULT_COLUMNS = ("venue", "market_id", "close_time", "floor", "settled_at", "yes")
KALSHI_KINDS = ("above", "up_down")


def _append(path: Path, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(columns)
        writer.writerows(rows)


def _column(path: Path, name: str) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {row[name] for row in csv.DictReader(handle)}


class PredictionRecorder:
    """Appends one sample of quotes and depth per call to `cycle`."""

    def __init__(self, *, root: Path | str = ROOT, coins: Sequence[str] = ("BTC", "ETH"), kalshi: Any = None, polymarket: Any = None,
                 spot: Callable[[str], float | None] | None = None, max_books: int = 20, horizon_hours: float = 48.0, list_every: float = 1800.0) -> None:
        """
        Args:
            kalshi, polymarket: Clients with `price_markets` and `order_book` (default: the public ones, with gzip).
            spot: coin -> its price now (default: Binance's last 1-minute close); None when unknown.
            max_books: Order books fetched per venue and coin each cycle, soonest expiry first.
            horizon_hours: Only markets settling within this many hours get their depth recorded.
            list_every: Seconds between refreshes of Polymarket's market list (its search answer is large; the
                quotes of the listed markets are then read from their order books each cycle).
        """
        from src.data.kraken_spreads import http_json_gzip

        self.root = Path(root)
        self.coins = tuple(coins)
        self.kalshi = kalshi if kalshi is not None else pm.KalshiClient(fetch=http_json_gzip)
        self.polymarket = polymarket if polymarket is not None else pm.PolymarketClient(fetch=http_json_gzip)
        self.spot = spot if spot is not None else self._binance_spot
        self.max_books, self.horizon, self.list_every = max_books, timedelta(hours=horizon_hours), list_every
        self._known: set[str] = set()  # "venue|market id" already in markets.csv (read from it on first use)
        self._known_loaded = False
        self._results_seen: set[str] | None = None
        self._results_at: datetime | None = None
        self._poly_list: dict[str, tuple[datetime, list[pm.BinaryMarket]]] = {}
        self._poly_results_at: datetime | None = None

    @staticmethod
    def _binance_spot(coin: str) -> float | None:
        try:
            return float(pm.binance_closes(f"{coin}USDT", limit=1).iloc[-1])
        except Exception:  # noqa: BLE001 - the quotes are still worth storing without it
            return None

    # --- one venue and coin ------------------------------------------------------------------------------------------

    def _note_markets(self, markets: Sequence[pm.BinaryMarket], now: datetime) -> None:
        path = self.root / "markets.csv"
        if not self._known_loaded:
            if path.exists():
                with path.open(newline="", encoding="utf-8") as handle:
                    self._known = {f"{row['venue']}|{row['market_id']}" for row in csv.DictReader(handle)}
            self._known_loaded = True
        rows = []
        for market in markets:
            key = f"{market.venue}|{market.market_id}"
            if key not in self._known:
                self._known.add(key)
                rows.append([now.isoformat(timespec="seconds"), market.venue, market.market_id, market.underlying, market.kind, market.floor, market.cap,
                             market.start.isoformat() if market.start else "", market.expiry.isoformat(), market.fee_rate, market.averaging_seconds, market.settles_on,
                             market.token_ids[0] if market.token_ids else "", market.question])
        _append(path, MARKET_COLUMNS, rows)

    def _books(self, client: Any, markets: Sequence[pm.BinaryMarket], now: datetime) -> tuple[list[list[Any]], dict[str, tuple[float | None, float | None]]]:
        """Depth rows for the soonest-settling markets, and each one's best (bid, ask) as read from its book."""
        stamp, rows, touch = now.isoformat(timespec="seconds"), [], {}
        soon = [market for market in markets if now < market.expiry <= now + self.horizon][: self.max_books]
        for market in soon:
            try:
                book = client.order_book(market.token_ids[0] if market.token_ids else market.market_id)
            except Exception:  # noqa: BLE001 - one market's book failing must not stop the rest
                continue
            best: dict[str, float | None] = {"bid": None, "ask": None}
            for side in ("bid", "ask"):
                levels = book[book["side"] == side].head(LEVELS)
                for level, row in enumerate(levels.itertuples(), start=1):
                    rows.append([stamp, market.venue, market.market_id, side, level, row.price, row.size])
                    if level == 1:
                        best[side] = float(row.price)
            touch[market.market_id] = (best["bid"], best["ask"])
        return rows, touch

    def _quote_rows(self, markets: Sequence[pm.BinaryMarket], spot: float | None, now: datetime, touch: dict[str, tuple[float | None, float | None]] | None = None) -> list[list[Any]]:
        rows = []
        for market in markets:
            bid, ask = (touch or {}).get(market.market_id, (market.yes_bid, market.yes_ask)) if touch is not None else (market.yes_bid, market.yes_ask)
            # Two-sided means a real buyer and a real seller: a venue shows "no bid" as 0 and "no offer" as 1
            if bid is None or ask is None or bid <= 0.0 or ask >= 1.0 or ask <= bid:
                continue
            rows.append([now.isoformat(timespec="seconds"), market.venue, market.market_id, market.underlying, market.kind, market.floor, market.cap, market.expiry.isoformat(),
                         bid, ask, market.volume, spot])
        return rows

    def _kalshi(self, coin: str, spot: float | None, now: datetime) -> tuple[int, int]:
        markets = [market for market in self.kalshi.price_markets(coin, kinds=KALSHI_KINDS) if market.expiry > now]
        quotes = self._quote_rows(markets, spot, now)  # Kalshi's list carries each market's best bid and ask
        quoted = {row[2] for row in quotes}
        self._note_markets([market for market in markets if market.market_id in quoted], now)  # hundreds of unquoted strikes open every hour: not worth a row each
        depth, _touch = self._books(self.kalshi, [market for market in markets if market.market_id in quoted], now)
        _append(self.root / "quotes" / f"{now:%Y-%m-%d}.csv", QUOTE_COLUMNS, quotes)
        _append(self.root / "depth" / f"{now:%Y-%m-%d}.csv", DEPTH_COLUMNS, depth)
        return len(quotes), len(depth)

    def _polymarket(self, coin: str, spot: float | None, now: datetime) -> tuple[int, int]:
        listed = self._poly_list.get(coin)
        if listed is None or (now - listed[0]).total_seconds() >= self.list_every:
            listed = (now, [market for market in self.polymarket.price_markets(coin, limit=40) if market.token_ids])
            self._poly_list[coin] = listed
        open_markets = [market for market in listed[1] if market.expiry > now]
        # Between list refreshes the listed quotes are stale, so a market's quote is the top of the book just read
        depth, touch = self._books(self.polymarket, [market for market in open_markets if market.yes_bid and market.yes_ask and market.yes_ask < 1.0], now)
        quotes = self._quote_rows([market for market in open_markets if market.market_id in touch], spot, now, touch)
        quoted = {row[2] for row in quotes}
        self._note_markets([market for market in open_markets if market.market_id in quoted], now)
        _append(self.root / "quotes" / f"{now:%Y-%m-%d}.csv", QUOTE_COLUMNS, quotes)
        _append(self.root / "depth" / f"{now:%Y-%m-%d}.csv", DEPTH_COLUMNS, depth)
        return len(quotes), len(depth)

    def _kalshi_results(self, now: datetime) -> int:
        """Append how recently settled Kalshi markets ended; at most once an hour."""
        if self._results_at is not None and now - self._results_at < timedelta(hours=1):
            return 0
        path = self.root / "results" / "kalshi.csv"
        if self._results_seen is None:
            self._results_seen = _column(path, "market_id")
        rows = []
        for coin in self.coins:
            for kind in KALSHI_KINDS:
                settled = self.kalshi.settled(pm.KALSHI_SERIES[coin][kind], limit=200)
                for row in settled.itertuples():
                    if row.ticker not in self._results_seen:
                        self._results_seen.add(row.ticker)
                        rows.append(["kalshi", row.ticker, row.close_time.isoformat() if row.close_time is not None else "", row.floor, row.settled_at, bool(row.yes)])
        _append(path, RESULT_COLUMNS, rows)
        self._results_at = now
        return len(rows)

    def _polymarket_results(self, now: datetime, *, per_pass: int = 60, give_up_days: float = 7.0) -> int:
        """Look up the outcome of described Polymarket markets that have ended; at most once an hour, `per_pass` lookups."""
        if self._poly_results_at is not None and now - self._poly_results_at < timedelta(hours=1):
            return 0
        self._poly_results_at = now
        markets, path = self.root / "markets.csv", self.root / "results" / "polymarket.csv"
        if not markets.exists():
            return 0
        done = _column(path, "market_id")
        with markets.open(newline="", encoding="utf-8") as handle:
            ended = [(row["market_id"], datetime.fromisoformat(row["expiry"])) for row in csv.DictReader(handle) if row["venue"] == "polymarket" and row["market_id"] not in done]
        due = sorted((expiry, market_id) for market_id, expiry in ended if now - timedelta(days=give_up_days) < expiry < now - timedelta(minutes=10))
        rows = []
        for expiry, market_id in due[:per_pass]:
            try:
                outcome = self.polymarket.result(market_id)
            except Exception:  # noqa: BLE001 - asked again next hour
                continue
            if outcome is not None:
                rows.append(["polymarket", market_id, expiry.isoformat(), "", "", outcome])
        _append(path, RESULT_COLUMNS, rows)
        return len(rows)

    # --- the cycle -----------------------------------------------------------------------------------------------

    def cycle(self, now: datetime | None = None) -> dict[str, Any]:
        """Record one sample from every venue and coin; returns counts and the errors met (never raises)."""
        now = now or datetime.now(timezone.utc)
        report: dict[str, Any] = {"quotes": 0, "depth": 0, "results": 0, "errors": []}
        for coin in self.coins:
            spot = self.spot(coin)
            for venue, record in (("kalshi", self._kalshi), ("polymarket", self._polymarket)):
                try:
                    quotes, depth = record(coin, spot, now)
                    report["quotes"] += quotes
                    report["depth"] += depth
                except Exception as exc:  # noqa: BLE001 - one venue down is a gap for that venue only
                    report["errors"].append(f"{venue} {coin}: {type(exc).__name__}: {exc}")
        for venue, results in (("kalshi", self._kalshi_results), ("polymarket", self._polymarket_results)):
            try:
                report["results"] += results(now)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"{venue} results: {type(exc).__name__}: {exc}")
        return report
