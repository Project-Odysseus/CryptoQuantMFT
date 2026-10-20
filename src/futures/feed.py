"""Daily bars and currency rates for a futures book, from the free source.

Each market's series is the contract the book holds, with older history from the source's front-month series scaled
to meet it. So the last bar is always the price of the held contract (what the book is marked at), and the step
from the old history to the held contract is not a price jump. Rolls further back in the front-month series are
left as they are: the free source has no expired contracts to adjust them with, and they only touch signal history.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import aiohttp
import pandas as pd

from src.futures.adapter import ContractBoard
from src.futures.contracts import Market
from src.futures.quotes import COLUMNS, fetch_chart
from src.futures.sessions import Sessions
from src.portfolio.feed import FeedResult, Key
from src.storage.bar_aggregator import OHLCVBar

DAY_SECONDS = 86_400
SPLICE_DAYS = 20  # the held contract's own bars are used for this many days; before that, the scaled front-month series
# When a trade date is over, local time, by exchange; a bar is used half an hour later
TRADE_DATE_END = {"CME": (time(17, 0), "America/New_York"), "CBOT": (time(17, 0), "America/New_York"), "NYMEX": (time(17, 0), "America/New_York"),
                  "Eurex": (time(22, 0), "Europe/Berlin")}
SETTLE_DELAY = timedelta(minutes=30)
Fetch = Callable[[aiohttp.ClientSession, str], Awaitable[tuple[pd.DataFrame, dict[str, Any]]]]


def splice(front: pd.DataFrame, held: pd.DataFrame, *, days: int = SPLICE_DAYS) -> pd.DataFrame:
    """The held contract's last `days` bars, preceded by the front-month series scaled to meet it on the joining day."""
    if held.empty:
        return front
    if front.empty:
        return held
    common = held.index.intersection(front.index)
    if common.empty:
        return held
    join = common[-days] if len(common) >= days else common[0]
    ratio = float(held.at[join, "close"]) / float(front.at[join, "close"])
    older = front[front.index < join].copy()
    older[["open", "high", "low", "close"]] *= ratio
    return pd.concat([older, held[held.index >= join]])


def completed(frame: pd.DataFrame, market: Market, now: datetime) -> pd.DataFrame:
    """The bars whose trade date is over at `now` (the source shows the day in progress as a bar too)."""
    ends, zone = TRADE_DATE_END.get(market.exchange, (time(23, 59), "UTC"))
    keep = [datetime.combine(day.date(), ends, tzinfo=ZoneInfo(zone)) + SETTLE_DELAY <= now for day in frame.index]
    return frame[keep]


def fill_holidays(frames: Mapping[str, pd.DataFrame], markets: Mapping[str, Market], sessions: Sessions) -> dict[str, pd.DataFrame]:
    """Give each market a flat bar on a day it was closed while another market traded.

    The engine expects every instrument to have the same latest bar; a market on holiday did not move, which is what
    a flat bar says. A day the market was open but has no bar is left missing, so it shows as stale data.
    """
    days = sorted(set().union(*(frame.index for frame in frames.values()))) if frames else []
    out = {}
    for name, frame in frames.items():
        if frame.empty:
            out[name] = frame
            continue
        rows, last = [], None
        present = set(frame.index)
        for day in days:
            if day in present:
                last = frame.loc[day]
                rows.append((day, *[float(last[column]) for column in COLUMNS]))
            elif last is not None and not sessions.is_session(markets[name].calendar, day.date()):
                close = float(last["close"])
                rows.append((day, close, close, close, close, 0.0))
        out[name] = pd.DataFrame(rows, columns=["day", *COLUMNS]).set_index("day")
    return out


def currency_symbol(currency: str, base: str) -> str:
    """The free source's symbol for `base` per one unit of `currency`."""
    return f"{currency}{base}=X"


class FuturesFeed:
    """The portfolio runtime's feed for a futures book: `now()` and `async fetch(keys, now)` (see `src/portfolio/feed.py`)."""

    def __init__(self, instruments: Mapping[str, Any], markets: Mapping[str, Market], board: ContractBoard, sessions: Sessions, *, base_currency: str,
                 currencies: Iterable[str] = (), refresh_seconds: float = 900.0, fetch: Fetch | None = None, clock: Callable[[], datetime] | None = None) -> None:
        """`instruments` are the config's; only daily bars are served. Data is fetched at most every `refresh_seconds`
        (and at once when a market moves to another contract), however often the runtime cycles."""
        self.instruments, self.markets, self.board, self.sessions = dict(instruments), dict(markets), board, sessions
        self.base_currency, self.currencies = base_currency, sorted(set(currencies) - {base_currency})
        self.refresh, self._fetch = timedelta(seconds=refresh_seconds), fetch or (lambda session, symbol: fetch_chart(session, symbol, span="2y"))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fetched_at: datetime | None = None
        self._fetched_codes: dict[str, str] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._errors: dict[str, str] = {}
        self._fx: dict[str, float] = {}

    def now(self) -> datetime:
        """The current time."""
        return self._clock()

    async def rates(self) -> dict[str, float]:
        """Base currency per unit of each foreign currency, fetched now. Raises when one is missing: a book cannot start without them."""
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*(self._fetch(session, currency_symbol(currency, self.base_currency)) for currency in self.currencies))
        rates = {}
        for currency, (_bars, latest) in zip(self.currencies, results):
            if not latest.get("price"):
                raise RuntimeError(f"no {self.base_currency} rate for {currency}")
            rates[currency] = float(latest["price"])
        self._fx = rates
        return dict(rates)

    async def _refresh(self, wanted: list[str], today: date) -> None:
        held = {name: self.board.current(name, today) for name in wanted}
        symbols: dict[str, str] = {}
        for name in wanted:
            market = self.markets[name]
            if not market.free_quote_symbol or held[name].price_symbol is None:
                self._errors[name] = "the free source has no quotes for this market"
                continue
            symbols[f"front:{name}"] = market.free_quote_symbol
            symbols[f"held:{name}"] = held[name].price_symbol  # the index itself when the source has no contracts: the splice is then a no-op
        for currency in self.currencies:
            symbols[f"fx:{currency}"] = currency_symbol(currency, self.base_currency)
        async with aiohttp.ClientSession() as session:
            results = dict(zip(symbols, await asyncio.gather(*(self._fetch(session, symbol) for symbol in symbols.values()), return_exceptions=True)))
        for name in wanted:
            front, own = results.get(f"front:{name}"), results.get(f"held:{name}")
            if front is None:
                continue
            if isinstance(front, BaseException) or isinstance(own, BaseException):
                error = front if isinstance(front, BaseException) else own
                self._errors[name] = f"{type(error).__name__}: {error}"
                continue
            series = splice(front[0], own[0])
            if series.empty:
                self._errors[name] = "no bars"
                continue
            self._frames[name] = series
            self._errors.pop(name, None)
        for currency in self.currencies:
            result = results.get(f"fx:{currency}")
            if result is not None and not isinstance(result, BaseException) and result[1].get("price"):
                self._fx[currency] = float(result[1]["price"])
        self._fetched_at, self._fetched_codes = self.now(), {name: contract.code for name, contract in held.items()}

    async def fetch(self, keys: Iterable[Key], now: datetime | None = None) -> FeedResult:
        """Daily bars per futures instrument, each market's last bar being its held contract's latest finished day."""
        result = FeedResult(now=now or self.now())
        keys = sorted(set(keys))
        wanted = sorted({instrument for instrument, interval in keys if interval == "1d" and instrument in self.markets})
        today = result.now.date()
        codes = {name: self.board.current(name, today).code for name in wanted}
        if self._fetched_at is None or result.now - self._fetched_at >= self.refresh or codes != self._fetched_codes:
            try:
                await self._refresh(wanted, today)
            except Exception as exc:  # noqa: BLE001 - the source being down must not stop the loop; the last good bars are served
                for name in wanted:
                    self._errors[name] = f"{type(exc).__name__}: {exc}"
        ready = fill_holidays({name: completed(self._frames[name], self.markets[name], result.now) for name in wanted if name in self._frames}, self.markets, self.sessions)
        for instrument, interval in keys:
            if interval != "1d" or instrument not in self.markets:
                result.failed[(instrument, interval)] = "the futures feed serves daily bars of markets in the contract table"
                continue
            if instrument in self._errors:
                result.failed[(instrument, interval)] = self._errors[instrument]
            frame = ready.get(instrument)
            if frame is None or frame.empty:
                result.failed.setdefault((instrument, interval), "no completed bars")
                continue
            spec = self.instruments[instrument]
            result.bars[(instrument, interval)] = [
                OHLCVBar(exchange=spec.venue, symbol=spec.symbol, interval_seconds=DAY_SECONDS, timestamp=day.to_pydatetime().replace(tzinfo=timezone.utc),
                         open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close), volume=float(row.volume or 0.0))
                for day, row in zip(frame.index, frame.itertuples(index=False))]
        result.fx = dict(self._fx) or None
        return result
