"""Market data for the portfolio runtime: completed exchange candles per (instrument, interval), over REST.

Why candles, not ticker polls: the single-strategy runtime builds bars by
sampling the ticker every cycle. A 4h or daily bar built that way only sees
the prices that happened at poll times, so its high and low are too narrow
(ATR-based rules like Keltner then see a different market than research did),
and a restart mid-bar leaves a gap. The exchange's own candles are complete,
are the series the research used, and can be re-fetched after any outage.

Why REST is enough: the portfolio decides on 4h and daily bar closes. Polling
once a minute sees a new candle within a minute of it closing; that delay is
noise next to a 4-hour holding decision, and it costs a handful of requests
per minute, far inside Kraken's public limits. A WebSocket feed would matter
for intraday or order-book strategies, and for private fill updates in live
trading (see docs/portfolio_plan.md, 4.1).

The HTTP calls are blocking library calls, so each (instrument, interval) is
loaded in a worker thread and all of them run concurrently. One failing
instrument keeps its last good bars and is reported, and the others are
unaffected.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from src.portfolio.config import InstrumentSpec
from src.runtime.config import BAR_INTERVALS
from src.storage.bar_aggregator import OHLCVBar

Key = tuple[str, str]  # (instrument id, interval)
PERP_HISTORY_START = datetime(2020, 2, 26, tzinfo=timezone.utc)


@dataclass(slots=True)
class FeedResult:
    """Bars per key (the last good ones when a fetch failed), and which keys failed this time and why."""

    now: datetime
    bars: dict[Key, list[Any]] = field(default_factory=dict)
    failed: dict[Key, str] = field(default_factory=dict)


def kraken_history_loader(spec: InstrumentSpec, interval: str, *, start: datetime = PERP_HISTORY_START, cache_dir: str = "data/historical_cache") -> list[Any]:
    """Completed Kraken candles for one instrument, from the local cache topped up over REST (blocking).

    Perps use the stitched trade-candle history the research used
    (`load_or_fetch_perp_history`), or the linear contract alone for coins
    without a stitched history. Spot uses Kraken's OHLC endpoint, which only
    serves the last 720 candles.
    """
    from src.data import kraken_futures
    from src.research.engine import load_bars

    seconds = BAR_INTERVALS[interval]
    if spec.venue == "kraken_futures":
        if spec.symbol.upper() in kraken_futures.HISTORY_SEGMENTS:
            return kraken_futures.load_or_fetch_perp_history(spec.symbol, interval_seconds=seconds, start=start, cache_dir=cache_dir)
        venue_symbol = kraken_futures.venue_symbol_for(spec.symbol)
        return kraken_futures._load_or_fetch_venue_history(venue_symbol, spec.symbol, interval_seconds=seconds, start=start, end=None, cache_dir=cache_dir, refresh=False)
    if spec.venue == "kraken":
        return load_bars(spec.symbol, interval, source="spot")
    raise ValueError(f"no market data feed for venue {spec.venue!r} ({spec.id})")


class CandleFeed:
    """Completed candles for every key, loaded concurrently; each key fails on its own."""

    def __init__(self, instruments: Mapping[str, InstrumentSpec], *, loader: Callable[[InstrumentSpec, str], Sequence[Any]] = kraken_history_loader) -> None:
        """`loader(spec, interval)` returns completed bars, oldest first; it may block (it runs in a thread)."""
        self.instruments = dict(instruments)
        self.loader = loader
        self._last_good: dict[Key, list[Any]] = {}

    def now(self) -> datetime:
        """The feed's clock: real UTC time."""
        return datetime.now(timezone.utc)

    async def fetch(self, keys: Iterable[Key], now: datetime | None = None) -> FeedResult:
        """Load every key concurrently. A failed key keeps its last good bars and is listed in `failed`."""
        result = FeedResult(now=now or self.now())

        async def load(key: Key) -> tuple[Key, list[Any] | None, str | None]:
            try:
                bars = await asyncio.to_thread(self.loader, self.instruments[key[0]], key[1])
                completed = [bar for bar in bars if bar.timestamp + timedelta(seconds=BAR_INTERVALS[key[1]]) <= result.now]
                if not completed:
                    return key, None, "no completed candles"
                return key, completed, None
            except Exception as exc:  # noqa: BLE001 - one venue's failure must not stop the others
                return key, None, f"{type(exc).__name__}: {exc}"

        for key, bars, error in await asyncio.gather(*(load(key) for key in sorted(set(keys)))):
            if bars is not None:
                self._last_good[key] = bars
            else:
                result.failed[key] = error or "unknown error"
            if key in self._last_good:
                result.bars[key] = self._last_good[key]
        return result


class MockCandleFeed:
    """Deterministic synthetic candles for smoke runs and tests: each fetch moves a simulated clock one grid bar on.

    Every instrument gets its own seeded random walk at the grid interval;
    coarser intervals are aggregated from it on UTC boundaries, so a daily
    candle appears only once its last grid bar has closed, as on the real feed.
    """

    def __init__(self, instruments: Iterable[str], *, grid_interval: str, history_days: int = 400, total_days: int = 1000,
                 start: datetime = datetime(2023, 1, 1, tzinfo=timezone.utc), seed: int = 7) -> None:
        """Build `total_days` of candles and start the clock after `history_days` (the warmup the first cycle sees)."""
        self.grid_seconds = BAR_INTERVALS[grid_interval]
        self.start = start
        per_day = 86400 // self.grid_seconds
        count = total_days * per_day
        self._closes: dict[str, np.ndarray] = {}
        for instrument in instruments:
            rng = np.random.default_rng(seed + zlib.crc32(instrument.encode()) % 10_000)
            returns = np.zeros(count)
            for index in range(1, count):
                returns[index] = 0.1 * returns[index - 1] + rng.normal(0.0001, 0.03 / np.sqrt(per_day))
            self._closes[instrument] = 100.0 * np.exp(np.cumsum(returns)) * (500.0 if "BTC" in instrument else 30.0)
        self.clock = history_days * per_day - 1  # index of the latest completed grid bar

    def now(self) -> datetime:
        """The simulated time: just after the latest grid bar closed."""
        return self.start + timedelta(seconds=self.grid_seconds * (self.clock + 1))

    def _bars(self, instrument: str, interval: str) -> list[OHLCVBar]:
        seconds = BAR_INTERVALS[interval]
        factor = seconds // self.grid_seconds
        closes = self._closes[instrument][: self.clock + 1]
        usable = (len(closes) // factor) * factor
        bars = []
        for first in range(0, usable, factor):
            chunk = closes[first : first + factor]
            opened = closes[first - 1] if first > 0 else chunk[0]
            bars.append(OHLCVBar(exchange="mock", symbol=instrument.split(":", 1)[1], interval_seconds=seconds,
                                 timestamp=self.start + timedelta(seconds=self.grid_seconds * first), open=float(opened),
                                 high=float(max(opened, chunk.max())) * 1.002, low=float(min(opened, chunk.min())) * 0.998, close=float(chunk[-1]), volume=1.0))
        return bars

    async def fetch(self, keys: Iterable[Key], now: datetime | None = None) -> FeedResult:
        """Every key's completed bars up to the clock, then advance the clock one grid bar."""
        result = FeedResult(now=self.now(), bars={key: self._bars(*key) for key in set(keys)})
        self.clock = min(self.clock + 1, len(next(iter(self._closes.values()))) - 1)
        return result
