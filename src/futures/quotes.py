"""Daily prices of the contracts trading now, from the free source, and a store that keeps them.

The free source drops a contract's history when it expires, so what is not recorded is gone: the recorder
(`scripts/collectors/futures_daily.py`) saves the listed contracts of every market each day. Real roll gaps and
volume per contract come from this store.
"""

from __future__ import annotations

import asyncio
import json
import time as clock
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import aiohttp
import numpy as np
import pandas as pd

from src.futures.contracts import Contract, Market, scheduled_contract
from src.futures.sessions import Sessions

STORE_DIR = Path("data/market_data/futures/daily")
COLUMNS = ["open", "high", "low", "close", "volume"]
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={span}&interval=1d"


def parse_chart(payload: Mapping[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """(daily bars by the exchange's local date, the latest price and its time) from one chart response.

    The source repeats the latest trade as an extra row for a day still in progress; rows without a close are dropped.
    """
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    stamps = result.get("timestamp") or []
    quote_rows = result["indicators"]["quote"][0] if stamps else {}
    days = pd.to_datetime(np.asarray(stamps, dtype="int64") + int(meta.get("gmtoffset", 0)), unit="s").normalize()
    frame = pd.DataFrame({column: pd.Series(quote_rows.get(column), index=days, dtype=float) for column in COLUMNS}) if stamps else pd.DataFrame(columns=COLUMNS)
    frame = frame.dropna(subset=["close"])
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.index.name = "day"
    price, at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    latest = {"price": float(price) if price else None, "time": datetime.fromtimestamp(int(at), timezone.utc) if at else None}
    return frame, latest


async def fetch_chart(session: aiohttp.ClientSession, symbol: str, *, span: str = "3mo") -> tuple[pd.DataFrame, dict[str, Any]]:
    """Daily bars and the latest price for one symbol."""
    async with session.get(_CHART.format(symbol=quote(symbol), span=span), headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=60)) as response:
        response.raise_for_status()
        return parse_chart(await response.json(content_type=None))


class DailyStore:
    """One CSV of daily bars per contract code, merged on every recording so a bar is never lost or duplicated."""

    def __init__(self, directory: Path | str = STORE_DIR) -> None:
        self.directory = Path(directory)

    def path(self, code: str) -> Path:
        """Where a contract's bars are kept."""
        return self.directory / f"{code}.csv"

    def load(self, code: str) -> pd.DataFrame:
        """A contract's recorded bars by day; empty when nothing is recorded."""
        path = self.path(code)
        if not path.exists():
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="day"))
        return pd.read_csv(path, index_col="day", parse_dates=True)

    def merge(self, code: str, frame: pd.DataFrame) -> int:
        """Add `frame` to the contract's bars (a newer value for a day replaces the old one); returns the days added."""
        if frame.empty:
            return 0
        old = self.load(code)
        merged = pd.concat([old, frame[COLUMNS]])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged.index.name = "day"
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path(code).with_suffix(".tmp")
        merged.to_csv(temporary)
        temporary.replace(self.path(code))
        return len(merged) - len(old)

    def latest_volumes(self, codes: Sequence[str]) -> dict[str, float]:
        """The most recent recorded daily volume per contract code (codes without a record are left out)."""
        out = {}
        for code in codes:
            volume = self.load(code)["volume"].dropna()
            if len(volume):
                out[code] = float(volume.iloc[-1])
        return out

    def volumes(self, code: str) -> list[float]:
        """A contract's recorded daily volumes, oldest first."""
        return [float(value) for value in self.load(code)["volume"].dropna()]


def listed_contracts(market: Market, day: date, sessions: Sessions, *, count: int = 2) -> list[Contract]:
    """The contract held by the calendar on `day` and the ones after it."""
    contracts = [scheduled_contract(market, day, sessions)]
    while len(contracts) < count:
        contracts.append(contracts[-1].following())
    return contracts


async def record(markets: Mapping[str, Market], store: DailyStore, sessions: Sessions, *, today: date | None = None) -> dict[str, Any]:
    """Save the daily bars of each market's listed contracts. One contract failing is reported, not raised."""
    today = today or datetime.now(timezone.utc).date()
    wanted = [contract for market in markets.values() if market.quote_exchange for contract in listed_contracts(market, today, sessions)]
    added: dict[str, int] = {}
    failed: dict[str, str] = {}
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(fetch_chart(session, contract.quote_symbol or "") for contract in wanted), return_exceptions=True)
    for contract, result in zip(wanted, results):
        if isinstance(result, BaseException):
            failed[contract.code] = f"{type(result).__name__}: {result}"
            continue
        added[contract.code] = store.merge(contract.code, result[0])
    return {"added": added, "failed": failed}


def _http_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - a fixed https endpoint
        return json.loads(response.read())


class LastPrices:
    """The latest traded price per quote symbol ("MESZ26.CME"), cached for `max_age` seconds.

    The paper broker asks this when an order is released, so a paper fill is at the price the market was actually
    at (delayed by the source's 10 to 15 minutes), not at yesterday's close. Any failure returns None.
    """

    def __init__(self, *, max_age: float = 60.0, fetch: Callable[[str], Any] = _http_json, clock_: Callable[[], float] = clock.monotonic) -> None:
        self.max_age, self._fetch, self._clock = max_age, fetch, clock_
        self._cache: dict[str, tuple[float, tuple[float, datetime] | None]] = {}

    def __call__(self, symbol: str) -> tuple[float, datetime] | None:
        """(price, the time it traded) or None when the source has no price."""
        cached = self._cache.get(symbol)
        if cached is not None and self._clock() - cached[0] <= self.max_age:
            return cached[1]
        try:
            _bars, latest = parse_chart(self._fetch(_CHART.format(symbol=quote(symbol), span="5d")))
            value = (latest["price"], latest["time"]) if latest["price"] and latest["time"] else None
        except Exception:  # noqa: BLE001 - the caller treats None as "no price"
            value = None
        self._cache[symbol] = (self._clock(), value)
        return value
