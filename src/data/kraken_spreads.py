"""Kraken Futures' real quoted costs: top-of-book spreads for every perp and book depth for chosen ones, sampled over time.

Backtests and the paper exchange charge an *assumed* slippage per coin (`slippage_bps` in a portfolio config). For the
40-coin basket that assumption decides whether the strategy has an edge at all, and paper trading can't check it,
because the paper exchange charges the same assumption. This module measures the real thing from public data (no keys):

- `fetch_tickers`: one request for every perp's best bid and ask -> the half-spread in basis points.
- `fetch_depth`: one request per coin for its order book -> what a market order of a given size would pay beyond the
  mid price (the volume-weighted fill against the resting orders), per side.
- `record_tickers` / `record_depth` append samples to daily CSV files (append-only, so a killed process loses nothing).
- `cost_table` summarises what was recorded per coin, next to a config's assumed slippage.

A taker pays about the half-spread plus the depth cost of its size, per side, on top of the exchange fee. Spreads widen
in fast markets, so the samples need to cover days, including a volatile one, before the tiers are replaced.
"""

from __future__ import annotations

import csv
import gzip
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

API = "https://futures.kraken.com/derivatives/api/v3"
ROOT = Path("data/market_data/kraken_spreads")
SIZES_USD = (100.0, 1_000.0, 5_000.0)
TICKER_COLUMNS = ("time", "symbol", "bid", "ask", "bid_size", "ask_size", "mark", "volume_quote_24h", "half_spread_bps")
DEPTH_COLUMNS = ("time", "symbol", "size_usd", "buy_bps", "sell_bps", "mid")

Fetch = Callable[[str], Any]


def http_json_gzip(url: str) -> Any:
    """GET `url` with gzip (these payloads compress about 4x) and parse the JSON (blocking; public, no key)."""
    request = urllib.request.Request(url, headers={"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def fetch_tickers(*, now: datetime | None = None, fetch: Fetch = http_json_gzip) -> pd.DataFrame:
    """Best bid and ask of every linear perp (PF_ symbols) right now, with the half-spread in basis points of the mid."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for ticker in fetch(f"{API}/tickers").get("tickers", []):
        symbol, bid, ask = str(ticker.get("symbol", "")), ticker.get("bid"), ticker.get("ask")
        if not symbol.startswith("PF_") or not bid or not ask or float(ask) <= float(bid):
            continue
        mid = (float(bid) + float(ask)) / 2.0
        rows.append({"time": now.isoformat(timespec="seconds"), "symbol": symbol, "bid": float(bid), "ask": float(ask), "bid_size": float(ticker.get("bidSize") or 0.0),
                     "ask_size": float(ticker.get("askSize") or 0.0), "mark": float(ticker.get("markPrice") or mid), "volume_quote_24h": float(ticker.get("volumeQuote") or 0.0),
                     "half_spread_bps": (float(ask) - float(bid)) / 2.0 / mid * 10_000.0})
    return pd.DataFrame(rows, columns=list(TICKER_COLUMNS))


class TouchQuotes:
    """The real best bid and ask per runtime symbol ("BTC/USD"), from one cached `tickers` request.

    The paper exchange asks this when it holds a resting (post-only) order: at placement, for the price to rest at,
    and each cycle after, to see whether the market has moved through it. One request answers for every coin, so
    the answer is kept for `max_age` seconds. Any failure returns None ("unknown"), never an exception.
    """

    def __init__(self, *, max_age: float = 15.0, fetch: Fetch = http_json_gzip, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_age, self._fetch, self._clock = max_age, fetch, clock
        self._at: float | None = None
        self._quotes: dict[str, tuple[float, float]] = {}

    def __call__(self, symbol: str) -> tuple[float, float] | None:
        """(best bid, best ask) of the perp for `symbol`, or None when Kraken doesn't quote it or can't be reached."""
        from src.data.kraken_futures import venue_symbol_for

        if self._at is None or self._clock() - self._at > self.max_age:
            try:
                tickers = self._fetch(f"{API}/tickers").get("tickers", [])
            except Exception:  # noqa: BLE001 - the caller treats None as "no quote"
                return None
            self._quotes = {str(item.get("symbol")): (float(item["bid"]), float(item["ask"])) for item in tickers if item.get("bid") and item.get("ask")}
            self._at = self._clock()
        return self._quotes.get(venue_symbol_for(symbol))


def market_order_cost_bps(levels: Sequence[Sequence[float]], mid: float, size_usd: float) -> float:
    """How far beyond `mid` a market order of `size_usd` fills on average, in basis points, walking `levels` (best first).

    NaN when the book shown is too thin to fill the size: that is an answer too (the size can't be traded at once).
    """
    remaining, paid = size_usd, 0.0
    for price, quantity in levels:
        value = float(price) * float(quantity)
        take = min(value, remaining)
        paid += take * abs(float(price) / mid - 1.0)
        remaining -= take
        if remaining <= 1e-9:
            return paid / size_usd * 10_000.0
    return float("nan")


def fetch_depth(symbol: str, *, sizes_usd: Sequence[float] = SIZES_USD, now: datetime | None = None, fetch: Fetch = http_json_gzip) -> pd.DataFrame:
    """One coin's cost of a market buy and a market sell at each size, from its order book."""
    now = now or datetime.now(timezone.utc)
    book = fetch(f"{API}/orderbook?{urllib.parse.urlencode({'symbol': symbol})}").get("orderBook") or {}
    bids = sorted(([float(price), float(size)] for price, size in book.get("bids", [])), key=lambda level: -level[0])
    asks = sorted(([float(price), float(size)] for price, size in book.get("asks", [])), key=lambda level: level[0])
    if not bids or not asks:
        return pd.DataFrame(columns=list(DEPTH_COLUMNS))
    mid = (bids[0][0] + asks[0][0]) / 2.0
    rows = [{"time": now.isoformat(timespec="seconds"), "symbol": symbol, "size_usd": float(size), "buy_bps": market_order_cost_bps(asks, mid, size),
             "sell_bps": market_order_cost_bps(bids, mid, size), "mid": mid} for size in sizes_usd]
    return pd.DataFrame(rows, columns=list(DEPTH_COLUMNS))


def _append(frame: pd.DataFrame, kind: str, root: Path, now: datetime) -> Path:
    path = root / kind / f"{now:%Y-%m-%d}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(frame.columns)
        writer.writerows(frame.itertuples(index=False, name=None))
    return path


def record_tickers(*, root: Path | str = ROOT, now: datetime | None = None, fetch: Fetch = http_json_gzip) -> int:
    """Append one sample of every perp's spread to today's file; returns the rows written."""
    now = now or datetime.now(timezone.utc)
    frame = fetch_tickers(now=now, fetch=fetch)
    if len(frame):
        _append(frame, "tickers", Path(root), now)
    return len(frame)


def record_depth(symbols: Sequence[str], *, root: Path | str = ROOT, now: datetime | None = None, fetch: Fetch = http_json_gzip) -> int:
    """Append one depth sample per symbol to today's file. A symbol whose request fails is skipped, not fatal."""
    now = now or datetime.now(timezone.utc)
    written = 0
    for symbol in symbols:
        try:
            frame = fetch_depth(symbol, now=now, fetch=fetch)
        except Exception:  # noqa: BLE001 - one coin's failure must not stop the others
            continue
        if len(frame):
            _append(frame, "depth", Path(root), now)
            written += len(frame)
    return written


def load(kind: str, *, root: Path | str = ROOT) -> pd.DataFrame:
    """Everything recorded of one kind ("tickers" or "depth")."""
    files = sorted((Path(root) / kind).glob("*.csv"))
    if not files:
        return pd.DataFrame(columns=list(TICKER_COLUMNS if kind == "tickers" else DEPTH_COLUMNS))
    frame = pd.concat([pd.read_csv(path) for path in files], ignore_index=True)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


def cost_table(assumed_bps: Mapping[str, float], *, root: Path | str = ROOT, size_usd: float = 1_000.0) -> pd.DataFrame:
    """Per venue symbol: the recorded half-spread (median and 90th percentile), the depth cost at `size_usd`, and the config's assumption.

    `measured_bps` = median half-spread + median depth cost beyond the touch at that size (the larger of buy and
    sell): what a taker pays per side before the fee. `ratio` = measured / assumed; above 1 the backtest was too
    kind. `assumed_bps` maps venue symbols (PF_XBTUSD) to the config's `slippage_bps`.
    """
    tickers, depth = load("tickers", root=root), load("depth", root=root)
    rows = []
    for symbol, assumed in assumed_bps.items():
        spreads = tickers.loc[tickers["symbol"] == symbol, "half_spread_bps"]
        book = depth[(depth["symbol"] == symbol) & (depth["size_usd"] == size_usd)]
        if not len(spreads):
            continue
        walk = float(np.nanmedian(np.maximum(book["buy_bps"], book["sell_bps"]))) if len(book) and book[["buy_bps", "sell_bps"]].notna().any().any() else float("nan")
        half = float(spreads.median())
        measured = max(half, walk) if np.isfinite(walk) else half  # the walk already starts at the touch, so it includes the half-spread
        rows.append({"symbol": symbol, "samples": int(len(spreads)), "half_spread_median": half, "half_spread_p90": float(spreads.quantile(0.9)),
                     f"cost_at_{size_usd:g}_usd": walk, "unfillable_share": float(book[["buy_bps", "sell_bps"]].isna().any(axis=1).mean()) if len(book) else float("nan"),
                     "measured_bps": measured, "assumed_bps": float(assumed), "ratio": measured / float(assumed) if assumed else float("nan"),
                     "volume_quote_24h": float(tickers.loc[tickers["symbol"] == symbol, "volume_quote_24h"].median())})
    return pd.DataFrame(rows).set_index("symbol").sort_values("volume_quote_24h", ascending=False) if rows else pd.DataFrame()
