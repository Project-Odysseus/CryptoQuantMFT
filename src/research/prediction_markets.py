"""Prediction markets on crypto prices (Polymarket, Kalshi): read-only data, a fair-value model, and hedges.

RESEARCH ONLY. Nothing here places an order or needs an account or a key; it reads the venues' public market data.
Whether trading these venues is allowed (employer rules, and each venue's country restrictions) is a separate
question to settle before any money moves.

A market such as "BTC above 84,600 at 6 PM" or "BTC up over the next 15 minutes" pays 1 if it happens and 0 if not,
so its price is a probability. That is a binary (digital) option, and three things follow:

- **Fair value.** With the price at S, the level at K, time T left and volatility sigma, the probability of ending
  above K is N(d2), d2 = (ln(S/K) - sigma^2 T / 2) / (sigma sqrt(T)): `prob_above`, `prob_between`, `fair_value`.
  Both venues settle short markets on a 60-second average, which trims the variance a little
  (`effective_seconds`). Everything hinges on sigma: over minutes it comes from recent realized volatility
  (`recent_volatility`), over days from option prices (`chain_implied_vol`).
- **Edge after fees.** Both venues charge takers `rate x p x (1 - p)` per contract (`taker_fee`; Polymarket's rate
  comes with each market, Kalshi's is 0.07 in its general schedule). `edges` is what buying Yes or No at the ask
  would earn against a fair value, after that fee.
- **Hedges.** A binary's value moves with the price: `binary_delta` is that sensitivity, so a perp position of the
  opposite sign neutralises it for small moves. A binary above K is also the limit of a call spread around K, so a
  Deribit chain prices it independently (`call_spread_bounds`), and the same question on two venues can be compared
  directly (`match_markets`, `locked_edge`).

`calibration_table` and `backtest_updown_model` test a fair-value model against outcomes: of the times it said 70%,
did 70% happen? `kalshi_model_vs_market` lines the model up against what Kalshi's market actually quoted, minute
by minute, on settled markets.

Limits to keep in mind: N(d2) assumes a lognormal price with no jumps and no drift; the venues settle on different
price sources (Kalshi: CF Benchmarks' BRTI; Polymarket: Chainlink streams or Binance candles), so "the same" market
on two venues is not exactly the same bet; and quotes on short markets are run by fast market makers, so a stale
quote that looks mispriced is usually already gone.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
BINANCE_API = "https://api.binance.com/api/v3"
SECONDS_PER_YEAR = 365.0 * 86400.0
KALSHI_FEE_RATE = 0.07  # Kalshi's general taker schedule: 0.07 x p x (1 - p) per contract (rounded up to a cent per order)
COIN_NAMES = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
# Kalshi series per coin: daily "above K", daily ranges, 15-minute up/down. Checked live for BTC (2026-10-03).
KALSHI_SERIES = {"BTC": {"above": "KXBTCD", "between": "KXBTC", "up_down": "KXBTC15M"}, "ETH": {"above": "KXETHD", "between": "KXETH", "up_down": "KXETH15M"}}

Fetch = Callable[[str], Any]


def http_json(url: str) -> Any:
    """GET `url` and parse the JSON (blocking; public endpoints, no key)."""
    request = urllib.request.Request(url, headers={"User-Agent": "QuantMultiStrat/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    stamp = pd.Timestamp(value)
    return (stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")).to_pydatetime()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class BinaryMarket:
    """One yes/no market on a coin's price, in the same shape whatever the venue.

    Attributes:
        kind: "above" (Yes if the price ends at or above `floor`), "between" (`floor` <= price <= `cap`), or
            "up_down" (Yes if the price ends at or above where it was at `start`; `floor` is that reference price
            when the venue publishes it, else None until `with_reference` sets it).
        yes_bid, yes_ask: Best quotes for Yes as probabilities (0-1). No is the mirror: its ask is 1 - yes_bid.
        fee_rate: Taker fee coefficient: the fee per contract is `fee_rate * p * (1 - p)`.
        averaging_seconds: The settlement price is an average over this many final seconds (0: one print).
        settles_on: The venue's own description of the settlement source.
        token_ids: Polymarket's order-book ids for (Yes, No); empty elsewhere.
    """

    venue: str
    market_id: str
    question: str
    underlying: str
    kind: str
    expiry: datetime
    floor: float | None = None
    cap: float | None = None
    start: datetime | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    volume: float = 0.0
    fee_rate: float = 0.0
    averaging_seconds: float = 0.0
    settles_on: str = ""
    token_ids: tuple[str, ...] = ()

    @property
    def mid(self) -> float | None:
        """The midpoint of the Yes quotes, when both exist."""
        return (self.yes_bid + self.yes_ask) / 2.0 if self.yes_bid is not None and self.yes_ask is not None else None

    @property
    def no_ask(self) -> float | None:
        """What buying No costs: selling Yes at its bid is the same trade."""
        return 1.0 - self.yes_bid if self.yes_bid is not None else None

    def with_reference(self, price: float) -> "BinaryMarket":
        """An up/down market with its reference price filled in (the price at `start`, from the settlement source)."""
        return replace(self, floor=float(price))


# --- venues ---------------------------------------------------------------------------------------------------------

def parse_polymarket(market: Mapping[str, Any], underlying: str) -> BinaryMarket | None:
    """A Polymarket (Gamma API) market as a `BinaryMarket`; None for shapes this module doesn't price (e.g. one-touch)."""
    try:
        outcomes = [str(item).lower() for item in json.loads(market.get("outcomes") or "[]")]
        tokens = tuple(str(item) for item in json.loads(market.get("clobTokenIds") or "[]"))
    except (TypeError, ValueError):
        return None
    expiry = _time(market.get("endDate"))
    question = str(market.get("question") or "")
    if expiry is None or len(outcomes) != 2:
        return None
    source = str(market.get("resolutionSource") or "") or str(market.get("description") or "").split("\n")[0][:300]
    schedule = market.get("feeSchedule") or {}
    common = {"venue": "polymarket", "market_id": str(market.get("slug") or market.get("id")), "question": question, "underlying": underlying, "expiry": expiry,
              "yes_bid": _number(market.get("bestBid")), "yes_ask": _number(market.get("bestAsk")), "volume": _number(market.get("volume")) or 0.0,
              "fee_rate": float(schedule.get("rate") or 0.0) if market.get("feesEnabled") else 0.0, "averaging_seconds": 60.0 if "twap" in source.lower() else 0.0,
              "settles_on": source, "token_ids": tokens}
    if outcomes == ["up", "down"]:
        return BinaryMarket(kind="up_down", start=_time(market.get("eventStartTime")), **common)
    strike = re.search(r"above \$?([\d,]+(?:\.\d+)?)", question, flags=re.IGNORECASE)
    if outcomes == ["yes", "no"] and strike:
        return BinaryMarket(kind="above", floor=float(strike.group(1).replace(",", "")), **common)
    return None


def parse_kalshi(market: Mapping[str, Any], underlying: str) -> BinaryMarket | None:
    """A Kalshi market as a `BinaryMarket`; None for strike types this module doesn't price."""
    expiry, floor, cap = _time(market.get("close_time")), _number(market.get("floor_strike")), _number(market.get("cap_strike"))
    strike_type = str(market.get("strike_type") or "")
    series = str(market.get("ticker") or "").split("-")[0]
    if expiry is None or floor is None:
        return None
    common = {"venue": "kalshi", "market_id": str(market["ticker"]), "question": f"{market.get('title', '')} {market.get('yes_sub_title') or market.get('subtitle') or ''}".strip(),
              "underlying": underlying, "expiry": expiry, "yes_bid": _number(market.get("yes_bid_dollars")) or None, "yes_ask": _number(market.get("yes_ask_dollars")) or None,
              "volume": _number(market.get("volume_fp")) or 0.0, "fee_rate": KALSHI_FEE_RATE, "averaging_seconds": 60.0, "settles_on": str(market.get("rules_primary") or "")}
    if series.endswith("15M") and strike_type in ("greater", "greater_or_equal"):
        return BinaryMarket(kind="up_down", floor=floor, start=_time(market.get("open_time")), **common)
    if strike_type in ("greater", "greater_or_equal"):
        return BinaryMarket(kind="above", floor=floor, **common)
    if strike_type == "between" and cap is not None:
        return BinaryMarket(kind="between", floor=floor, cap=cap, **common)
    return None


class PolymarketClient:
    """Polymarket's public market data: market search (Gamma API), order books and price history (CLOB API)."""

    def __init__(self, fetch: Fetch = http_json) -> None:
        """`fetch(url)` returns parsed JSON (tests pass a fake)."""
        self.fetch = fetch

    def search_events(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Active events matching `query`, each with its markets."""
        params = urllib.parse.urlencode({"q": query, "limit_per_type": limit, "events_status": "active"})
        return list(self.fetch(f"{POLYMARKET_GAMMA}/public-search?{params}").get("events", []))

    def price_markets(self, underlying: str = "BTC", *, limit: int = 20) -> list[BinaryMarket]:
        """Open up/down and "above K" markets on a coin, soonest expiry first."""
        name = COIN_NAMES[underlying]
        found: dict[str, BinaryMarket] = {}
        for query in (f"{name} up or down", f"{name} above"):
            for event in self.search_events(query, limit=limit):
                for raw in event.get("markets", []):
                    market = parse_polymarket(raw, underlying) if raw.get("active") and not raw.get("closed") else None
                    if market is not None and name in market.question.lower():
                        found[market.market_id] = market
        return sorted(found.values(), key=lambda market: (market.expiry, market.floor or 0.0))

    def order_book(self, token_id: str) -> pd.DataFrame:
        """The resting orders for one outcome token: side, price, size, best prices first."""
        book = self.fetch(f"{POLYMARKET_CLOB}/book?token_id={token_id}")
        rows = [{"side": side[:-1], "price": float(level["price"]), "size": float(level["size"])} for side in ("bids", "asks") for level in book.get(side, [])]
        frame = pd.DataFrame(rows, columns=["side", "price", "size"])
        return pd.concat([frame[frame["side"] == "bid"].sort_values("price", ascending=False), frame[frame["side"] == "ask"].sort_values("price")], ignore_index=True)

    def result(self, slug: str) -> bool | None:
        """How a market ended: True if its first outcome (Yes, or Up) won, False if the other did, None while it isn't resolved."""
        # Gamma leaves closed markets out unless asked for them
        found = self.fetch(f"{POLYMARKET_GAMMA}/markets?{urllib.parse.urlencode({'slug': slug, 'closed': 'true'})}")
        if not found or not found[0].get("closed"):
            return None
        try:
            first, second = (float(price) for price in json.loads(found[0].get("outcomePrices") or "[]"))
        except (TypeError, ValueError):
            return None
        return True if first >= 0.99 and second <= 0.01 else False if second >= 0.99 and first <= 0.01 else None

    def price_history(self, token_id: str, *, interval: str = "1d", fidelity_minutes: int = 1) -> pd.Series:
        """The outcome token's traded price over `interval` ("1h", "6h", "1d", "1w", "max"), one point per `fidelity_minutes`."""
        payload = self.fetch(f"{POLYMARKET_CLOB}/prices-history?market={token_id}&interval={interval}&fidelity={fidelity_minutes}")
        points = payload.get("history", [])
        return pd.Series([float(point["p"]) for point in points], index=pd.to_datetime([int(point["t"]) for point in points], unit="s", utc=True), name="price")


class KalshiClient:
    """Kalshi's public market data: markets by series, order books, settled markets and their minute candles."""

    def __init__(self, fetch: Fetch = http_json) -> None:
        """`fetch(url)` returns parsed JSON (tests pass a fake)."""
        self.fetch = fetch

    def raw_markets(self, series: str, *, status: str = "open", limit: int = 200, pages: int = 3) -> list[dict[str, Any]]:
        """Markets of one series as Kalshi returns them, following the cursor for up to `pages` pages."""
        out: list[dict[str, Any]] = []
        cursor = ""
        for _page in range(pages):
            params = {"series_ticker": series, "status": status, "limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor
            payload = self.fetch(f"{KALSHI_API}/markets?{urllib.parse.urlencode(params)}")
            out += payload.get("markets", [])
            cursor = payload.get("cursor") or ""
            if not cursor or len(out) >= limit:
                break
        return out[:limit]

    def price_markets(self, underlying: str = "BTC", *, kinds: Sequence[str] = ("above", "up_down"), limit: int = 200) -> list[BinaryMarket]:
        """Open markets on a coin for the chosen kinds ("above", "between", "up_down"), soonest expiry first."""
        markets = []
        for kind in kinds:
            for raw in self.raw_markets(KALSHI_SERIES[underlying][kind], limit=limit):
                market = parse_kalshi(raw, underlying)
                if market is not None:
                    markets.append(market)
        return sorted(markets, key=lambda market: (market.expiry, market.floor or 0.0))

    def order_book(self, ticker: str) -> pd.DataFrame:
        """Resting orders as Yes prices: a bid for No at q is an offer of Yes at 1 - q."""
        book = self.fetch(f"{KALSHI_API}/markets/{ticker}/orderbook").get("orderbook_fp") or {}
        bids = [{"side": "bid", "price": float(price), "size": float(size)} for price, size in book.get("yes_dollars") or []]
        asks = [{"side": "ask", "price": 1.0 - float(price), "size": float(size)} for price, size in book.get("no_dollars") or []]
        frame = pd.DataFrame(bids + asks, columns=["side", "price", "size"])
        return pd.concat([frame[frame["side"] == "bid"].sort_values("price", ascending=False), frame[frame["side"] == "ask"].sort_values("price")], ignore_index=True)

    def settled(self, series: str, *, limit: int = 100) -> pd.DataFrame:
        """The most recently settled markets of a series: when they ran, the level, the settlement value and the result."""
        rows = [{"ticker": raw["ticker"], "open_time": _time(raw.get("open_time")), "close_time": _time(raw.get("close_time")), "floor": _number(raw.get("floor_strike")),
                 "settled_at": _number(raw.get("expiration_value")), "yes": raw.get("result") == "yes", "volume": _number(raw.get("volume_fp")) or 0.0}
                for raw in self.raw_markets(series, status="settled", limit=limit, pages=max(1, limit // 200 + 1)) if raw.get("result") in ("yes", "no")]
        return pd.DataFrame(rows)

    def event_markets(self, event_ticker: str) -> list[dict[str, Any]]:
        """Every market of one event (e.g. "KXBTCD-26SEP3017": all the levels for one settlement time), as Kalshi returns them."""
        return list(self.fetch(f"{KALSHI_API}/markets?{urllib.parse.urlencode({'event_ticker': event_ticker, 'limit': 200})}").get("markets", []))

    def quotes_at(self, tickers: Sequence[str], when: datetime, *, lookback_minutes: int = 10, chunk: int = 40) -> dict[str, tuple[float, float]]:
        """Each market's (Yes bid, Yes ask) as of `when`: the latest minute candle within `lookback_minutes` before it.

        Markets without a two-sided quote in that window are left out: a quote older than that may predate a
        price move, and comparing it with anything measured at `when` would manufacture a gap.
        """
        out: dict[str, tuple[float, float]] = {}
        end = int(when.timestamp())
        for first in range(0, len(tickers), chunk):
            params = urllib.parse.urlencode({"market_tickers": ",".join(tickers[first : first + chunk]), "start_ts": end - lookback_minutes * 60, "end_ts": end, "period_interval": 1})
            for market in self.fetch(f"{KALSHI_API}/markets/candlesticks?{params}").get("markets", []):
                for candle in reversed(market.get("candlesticks", [])):
                    bid, ask = _number((candle.get("yes_bid") or {}).get("close_dollars")), _number((candle.get("yes_ask") or {}).get("close_dollars"))
                    if bid and ask and ask < 1.0:
                        out[str(market["market_ticker"])] = (bid, ask)
                        break
        return out

    def candles(self, series: str, ticker: str, start: datetime, end: datetime, *, minutes: int = 1) -> pd.DataFrame:
        """A market's Yes bid, ask and traded price at the close of each `minutes`-minute period (indexed by period end)."""
        params = urllib.parse.urlencode({"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": minutes})
        rows = []
        for candle in self.fetch(f"{KALSHI_API}/series/{series}/markets/{ticker}/candlesticks?{params}").get("candlesticks", []):
            rows.append({"time": pd.Timestamp(int(candle["end_period_ts"]), unit="s", tz="UTC"), "yes_bid": _number((candle.get("yes_bid") or {}).get("close_dollars")),
                         "yes_ask": _number((candle.get("yes_ask") or {}).get("close_dollars")), "price": _number((candle.get("price") or {}).get("close_dollars")),
                         "volume": _number(candle.get("volume_fp")) or 0.0})
        return pd.DataFrame(rows).set_index("time") if rows else pd.DataFrame(columns=["yes_bid", "yes_ask", "price", "volume"])


def binance_closes(symbol: str = "BTCUSDT", *, interval: str = "1m", limit: int = 1000, end: datetime | None = None, fetch: Fetch = http_json) -> pd.Series:
    """Recent Binance spot closes indexed by each candle's close time (public REST, at most 1000 candles a call)."""
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if end is not None:
        params["endTime"] = int(end.timestamp() * 1000)
    rows = fetch(f"{BINANCE_API}/klines?{urllib.parse.urlencode(params)}")
    return pd.Series([float(row[4]) for row in rows], index=pd.to_datetime([int(row[6]) + 1 for row in rows], unit="ms", utc=True), name=symbol)


# --- fair value -----------------------------------------------------------------------------------------------------

def _phi(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def effective_seconds(seconds_left: float, averaging_seconds: float = 0.0) -> float:
    """The horizon whose variance equals that of a settlement averaged over the final `averaging_seconds`.

    The average of a random walk over its last tau seconds has variance (T - tau) + tau / 3, not T: the final
    stretch counts for a third. Inside the averaging window part of the average is already known, which this
    ignores; treat the last minute's numbers as rough.
    """
    if averaging_seconds <= 0.0 or seconds_left <= 0.0:
        return max(seconds_left, 0.0)
    window = min(averaging_seconds, seconds_left)
    return seconds_left - window + window / 3.0


def prob_above(spot: float, strike: float, sigma: float, seconds_left: float) -> float:
    """P(price >= strike at expiry) for a driftless lognormal price: N(d2). `sigma` is annualised."""
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")
    variance = sigma * sigma * max(seconds_left, 0.0) / SECONDS_PER_YEAR
    if variance <= 0.0:
        return 1.0 if spot >= strike else 0.0
    return _phi((math.log(spot / strike) - variance / 2.0) / math.sqrt(variance))


def prob_between(spot: float, low: float, high: float, sigma: float, seconds_left: float) -> float:
    """P(low <= price <= high at expiry)."""
    return max(prob_above(spot, low, sigma, seconds_left) - prob_above(spot, high, sigma, seconds_left), 0.0)


def binary_delta(spot: float, strike: float, sigma: float, seconds_left: float) -> float:
    """Change in P(above strike) per 1 unit of price: phi(d2) / (S sigma sqrt(T)). It explodes near the strike at expiry."""
    variance = sigma * sigma * max(seconds_left, 0.0) / SECONDS_PER_YEAR
    if variance <= 0.0:
        return 0.0
    d2 = (math.log(spot / strike) - variance / 2.0) / math.sqrt(variance)
    return math.exp(-d2 * d2 / 2.0) / math.sqrt(2.0 * math.pi) / (spot * math.sqrt(variance))


def fair_value(market: BinaryMarket, spot: float, sigma: float, now: datetime) -> float | None:
    """The model's probability of Yes; None for an up/down market whose reference price isn't known yet."""
    if market.floor is None:
        return None
    left = effective_seconds((market.expiry - now).total_seconds(), market.averaging_seconds)
    if market.kind == "between":
        return prob_between(spot, market.floor, float(market.cap), sigma, left)
    return prob_above(spot, market.floor, sigma, left)


def hedge_units(market: BinaryMarket, spot: float, sigma: float, now: datetime, *, contracts: float = 1.0) -> float | None:
    """Coins to hold (negative = short) so `contracts` Yes contracts (1 unit of currency each) don't move with the price.

    A Yes position gains when the price rises, so its hedge is short. A "between" market's delta changes sign
    across the range. The hedge is only good for small moves: the delta itself swings hard near the level as
    expiry approaches, which is what makes short binaries expensive to hedge.
    """
    if market.floor is None:
        return None
    left = effective_seconds((market.expiry - now).total_seconds(), market.averaging_seconds)
    delta = binary_delta(spot, market.floor, sigma, left)
    if market.kind == "between":
        delta -= binary_delta(spot, float(market.cap), sigma, left)
    return -contracts * delta


def recent_volatility(closes: pd.Series, *, halflife_bars: float = 60.0) -> float:
    """Annualised volatility from recent closes: an EWMA of squared log returns (zero mean), latest value."""
    returns = np.log(closes.astype(float)).diff().dropna()
    if len(returns) < 2:
        raise ValueError("need at least three closes")
    seconds = float(pd.Series(closes.index).diff().dt.total_seconds().median())
    variance = float((returns**2).ewm(halflife=halflife_bars).mean().iloc[-1])
    return math.sqrt(variance * SECONDS_PER_YEAR / seconds)


# --- fees and edges -------------------------------------------------------------------------------------------------

def taker_fee(price: float, rate: float) -> float:
    """The taker fee per contract bought at `price`: rate x p x (1 - p). Largest at 50 cents, nothing at the extremes."""
    return rate * price * (1.0 - price)


def edges(market: BinaryMarket, fair: float) -> dict[str, float | None]:
    """What taking each side at the quoted ask earns in expectation, per contract, after the taker fee.

    `buy_yes` = fair - ask - fee; `buy_no` = (1 - fair) - (1 - bid) - fee. Both are negative when the fair value
    sits inside the spread, which is the normal state: the spread and the fee are the market maker's income.
    """
    buy_yes = fair - market.yes_ask - taker_fee(market.yes_ask, market.fee_rate) if market.yes_ask is not None else None
    buy_no = (1.0 - fair) - market.no_ask - taker_fee(market.no_ask, market.fee_rate) if market.no_ask is not None else None
    return {"buy_yes": buy_yes, "buy_no": buy_no}


def quote_table(markets: Sequence[BinaryMarket], spot: float, sigma: float, now: datetime) -> pd.DataFrame:
    """One row per market: its quotes, the model's fair value, the edge of taking either side, and the perp hedge."""
    rows = []
    for market in markets:
        fair = fair_value(market, spot, sigma, now)
        edge = edges(market, fair) if fair is not None else {"buy_yes": None, "buy_no": None}
        rows.append({"venue": market.venue, "market": market.market_id, "kind": market.kind, "level": market.floor, "cap": market.cap,
                     "minutes_left": (market.expiry - now).total_seconds() / 60.0, "yes_bid": market.yes_bid, "yes_ask": market.yes_ask, "fair": fair,
                     "edge_buy_yes": edge["buy_yes"], "edge_buy_no": edge["buy_no"], "hedge_coins_per_100": hedge_units(market, spot, sigma, now, contracts=100.0),
                     "volume": market.volume})
    return pd.DataFrame(rows)


# --- other prices for the same bet ----------------------------------------------------------------------------------

def match_markets(first: Sequence[BinaryMarket], second: Sequence[BinaryMarket], *, strike_tolerance: float = 0.001, minutes_tolerance: float = 5.0) -> list[tuple[BinaryMarket, BinaryMarket]]:
    """Pairs of "above" markets from two venues asking nearly the same question (level within a share, expiry within minutes)."""
    pairs = []
    for a in first:
        for b in second:
            if a.kind == b.kind == "above" and a.underlying == b.underlying and a.floor and b.floor \
                    and abs(a.floor / b.floor - 1.0) <= strike_tolerance and abs((a.expiry - b.expiry).total_seconds()) <= minutes_tolerance * 60.0:
                pairs.append((a, b))
    return pairs


def locked_edge(a: BinaryMarket, b: BinaryMarket) -> float | None:
    """The best of "Yes on one venue, No on the other", per contract after both taker fees; positive = the pair costs less than its payout of 1.

    It is only locked if both venues settle on the same number. They don't (different price sources and
    averaging), so a small positive edge near the level is basis risk, not free money.
    """
    best = None
    for yes, no in ((a, b), (b, a)):
        if yes.yes_ask is None or no.no_ask is None:
            continue
        edge = 1.0 - yes.yes_ask - no.no_ask - taker_fee(yes.yes_ask, yes.fee_rate) - taker_fee(no.no_ask, no.fee_rate)
        best = edge if best is None else max(best, edge)
    return best


def chain_implied_vol(chain: pd.DataFrame, strike: float, expiry: datetime, *, now: datetime | None = None) -> float:
    """Implied volatility for `strike` at `expiry` from a Deribit chain (`src/options/deribit.fetch_chain`).

    Mark volatilities are interpolated across strikes within the listed expiries on either side, then in total
    variance across time. Prediction markets rarely expire when Deribit does, and this ignores the smile's slope
    (a digital is worth N(d2) minus vega times that slope), so it is a first approximation.
    """
    now = now or chain["timestamp"].iloc[0]
    seconds = (pd.Timestamp(expiry) - pd.Timestamp(now)).total_seconds()
    points = []
    for listed, group in chain[chain["mark_iv"] > 0].groupby("expiry"):
        ordered = group.groupby("strike")["mark_iv"].mean().sort_index()
        if len(ordered) >= 2:
            listed_seconds = (pd.Timestamp(listed) - pd.Timestamp(now)).total_seconds()
            points.append((listed_seconds, float(np.interp(strike, ordered.index.to_numpy(), ordered.to_numpy()))))
    if not points:
        raise ValueError("the chain has no usable implied volatilities")
    points.sort()
    times = np.array([point[0] for point in points])
    total_variance = np.array([point[1] ** 2 * point[0] for point in points])
    if seconds <= times[0]:
        return points[0][1]  # shorter than the first listed expiry: its volatility, flat
    return float(math.sqrt(np.interp(seconds, times, total_variance) / seconds))


def call_spread_bounds(chain: pd.DataFrame, strike: float, expiry: datetime) -> dict[str, float] | None:
    """What a Deribit call spread around `strike` says a binary above it is worth, at the nearest listed expiry.

    Long the call just below, short the call just above, per unit of strike distance, pays 1 above the upper
    strike and 0 below the lower one: a binary with a ramp in between. `buy` uses the lower call's ask and the
    upper's bid (what building it costs), `sell` the reverse, `mark` the marks; `gap_hours` is how far the option expiry is from the one asked for. Returns None without both strikes.
    """
    listed = min(chain["expiry"].unique(), key=lambda value: abs((pd.Timestamp(value) - pd.Timestamp(expiry)).total_seconds()))
    calls = chain[(chain["expiry"] == listed) & (chain["right"] == "call")].set_index("strike").sort_index()
    below, above = calls.index[calls.index <= strike], calls.index[calls.index > strike]
    if not len(below) or not len(above):
        return None
    low, high = calls.loc[below[-1]], calls.loc[above[0]]
    width = float(above[0] - below[-1])
    gap_hours = abs((pd.Timestamp(listed) - pd.Timestamp(expiry)).total_seconds()) / 3600.0  # the option expiry is rarely the market's
    return {"expiry": pd.Timestamp(listed), "gap_hours": gap_hours, "lower_strike": float(below[-1]), "upper_strike": float(above[0]), "mark": float(low["mark"] - high["mark"]) / width,
            "buy": float(low["ask"] - high["bid"]) / width, "sell": float(low["bid"] - high["ask"]) / width}


# --- testing a model against outcomes -------------------------------------------------------------------------------

def calibration_table(probabilities: Sequence[float], outcomes: Sequence[bool], *, bins: int = 10) -> pd.DataFrame:
    """Per probability bucket: how many forecasts, their average, and how often the event happened.

    A calibrated model's `happened` matches its `forecast` in every row. `brier` (mean squared error; 0.25 is what
    always saying 50% scores) and `log_loss` are in the frame's `attrs`.
    """
    forecast, happened = np.asarray(probabilities, dtype=float), np.asarray(outcomes, dtype=float)
    keep = np.isfinite(forecast)
    forecast, happened = forecast[keep], happened[keep]
    bucket = np.minimum((forecast * bins).astype(int), bins - 1)
    rows = [{"bucket": f"{index / bins:.0%}-{(index + 1) / bins:.0%}", "forecasts": int((bucket == index).sum()), "forecast": float(forecast[bucket == index].mean()),
             "happened": float(happened[bucket == index].mean())} for index in range(bins) if (bucket == index).any()]
    table = pd.DataFrame(rows).set_index("bucket") if rows else pd.DataFrame(columns=["forecasts", "forecast", "happened"])
    clipped = np.clip(forecast, 1e-6, 1.0 - 1e-6)
    table.attrs["brier"] = float(np.mean((forecast - happened) ** 2)) if len(forecast) else float("nan")
    table.attrs["log_loss"] = float(-np.mean(happened * np.log(clipped) + (1.0 - happened) * np.log(1.0 - clipped))) if len(forecast) else float("nan")
    return table


def backtest_updown_model(closes: pd.Series, *, window_bars: int, halflife_bars: float = 288.0, vol_scale: float = 1.0) -> pd.DataFrame:
    """The N(d2) model on every "up over the next window" question in a history of closes, checked partway through.

    Windows of `window_bars` bars are laid end to end. The reference is the close just before the window; the
    outcome is whether the window's last close is at or above it. At each bar inside the window the model gives a
    probability from the price so far and an EWMA volatility of returns up to that bar only (`vol_scale` multiplies
    it). Returns one row per (window, bars elapsed): `elapsed`, `prob`, `up`.
    """
    prices = closes.to_numpy(dtype=float)
    returns = np.diff(np.log(prices), prepend=np.nan)
    variance = pd.Series(returns**2).ewm(halflife=halflife_bars, min_periods=int(halflife_bars)).mean().to_numpy()  # per bar, through each bar
    rows = []
    for first in range(int(halflife_bars) + 1, len(prices) - window_bars, window_bars):
        reference, last = prices[first - 1], prices[first + window_bars - 1]
        for elapsed in range(1, window_bars):
            position = first + elapsed - 1  # the latest close known `elapsed` bars into the window
            bar_variance = variance[position] * vol_scale**2
            if not np.isfinite(bar_variance) or bar_variance <= 0.0:
                continue
            total = bar_variance * (window_bars - elapsed)
            prob = _phi((math.log(prices[position] / reference) - total / 2.0) / math.sqrt(total))
            rows.append({"time": closes.index[position], "elapsed": elapsed, "prob": prob, "up": last >= reference})
    return pd.DataFrame(rows)


def kalshi_model_vs_market(client: KalshiClient, closes: pd.Series, *, series: str = "KXBTC15M", markets: int = 40, halflife_bars: float = 60.0) -> pd.DataFrame:
    """Kalshi's own quotes on settled up/down markets next to the model's fair value, minute by minute.

    `closes` are exchange closes (e.g. `binance_closes`) covering the markets' lives, indexed by close time. Kalshi
    settles on a different index (BRTI), so the model prices the *move*: the reference level times the exchange's
    return since the market opened. One row per (market, minute): `minutes_left`, `market_mid`, `model`, `yes`.
    One request per market: keep `markets` small.
    """
    settled = client.settled(series, limit=markets)
    log_returns = np.log(closes.astype(float)).diff()
    bar_seconds = float(pd.Series(closes.index).diff().dt.total_seconds().median())
    variance = (log_returns**2).ewm(halflife=halflife_bars).mean()
    rows = []
    for market in settled.itertuples():
        opened, closed = pd.Timestamp(market.open_time), pd.Timestamp(market.close_time)
        if opened not in closes.index or closed not in closes.index or not market.floor:
            continue
        candles = client.candles(series, market.ticker, market.open_time, market.close_time)
        for stamp, candle in candles.iterrows():
            if stamp >= closed or stamp not in closes.index or candle["yes_bid"] is None or candle["yes_ask"] is None or not np.isfinite(variance.get(stamp, np.nan)):
                continue
            spot = market.floor * closes[stamp] / closes[opened]
            left = effective_seconds((closed - stamp).total_seconds(), 60.0)
            sigma = math.sqrt(float(variance[stamp]) * SECONDS_PER_YEAR / bar_seconds)
            rows.append({"ticker": market.ticker, "time": stamp, "minutes_left": (closed - stamp).total_seconds() / 60.0,
                         "market_mid": (candle["yes_bid"] + candle["yes_ask"]) / 2.0, "spread": candle["yes_ask"] - candle["yes_bid"],
                         "model": prob_above(spot, market.floor, sigma, left), "yes": bool(market.yes)})
    return pd.DataFrame(rows)


def utc_now() -> datetime:
    """The current time in UTC (one place, so notebooks and tests agree on the clock they pass around)."""
    return datetime.now(timezone.utc)
