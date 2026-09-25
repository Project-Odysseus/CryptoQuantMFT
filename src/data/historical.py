"""Helpers for fetching historical OHLCV bars from public exchange APIs."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.storage.bar_aggregator import OHLCVBar


def fetch_kraken_ohlcv(
    symbol: str = "BTC/EUR",
    *,
    interval_seconds: int = 60,
    count: int = 200,
    since: int | None = None,
) -> list[OHLCVBar]:
    """Fetch recent OHLCV bars from Kraken and return them as OHLCVBar objects."""
    interval_minutes = _kraken_interval_minutes(interval_seconds)
    pair_code = _normalize_pair_code(symbol)
    params: dict[str, Any] = {
        "pair": pair_code,
        "interval": interval_minutes,
        "count": count,
    }
    if since is not None:
        params["since"] = since

    payload = _request_json(
        "GET",
        "https://api.kraken.com/0/public/OHLC",
        params=params,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Kraken OHLC payload")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))

    result = payload.get("result", {})
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected Kraken OHLC result payload")

    rows = result.get(pair_code)
    if not isinstance(rows, list):
        raise ValueError(f"Unsupported Kraken symbol: {symbol}")

    bars: list[OHLCVBar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        timestamp = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
        open_price = float(row[1])
        high_price = float(row[2])
        low_price = float(row[3])
        close_price = float(row[4])
        volume = float(row[6])
        bars.append(
            OHLCVBar(
                exchange="kraken",
                symbol=symbol,
                interval_seconds=interval_seconds,
                timestamp=timestamp,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
            )
        )

    bars.sort(key=lambda bar: bar.timestamp)
    return bars


def fetch_kraken_ohlcv_history(
    symbol: str = "BTC/EUR",
    *,
    interval_seconds: int = 3600,
    lookback_days: int = 90,
    max_requests: int = 30,
    request_pause_seconds: float = 1.5,
) -> list[OHLCVBar]:
    """Fetch a longer window of OHLCV history than a single Kraken OHLC call allows.

    Kraken's public OHLC endpoint only retains roughly the most recent 720
    candles *per pair/interval*, full stop - `since` does not unlock deeper
    history, it just marks where to resume. Confirmed empirically: passing
    `since` far in the past against BTC/EUR at 1-hour bars still only
    returned the most recent ~720 hours (~30 days), not 90. Pagination here
    still exists (harmless if Kraken ever changes this), but the practical
    way to reach a longer real horizon is a coarser `interval_seconds` (e.g.
    4-hour bars cover ~120 days in one page) rather than more pages of a
    fine interval. Callers should check the returned span, not assume
    `lookback_days` was actually achieved.
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")

    since = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    collected: dict[datetime, OHLCVBar] = {}
    for _ in range(max_requests):
        batch = fetch_kraken_ohlcv(symbol=symbol, interval_seconds=interval_seconds, count=720, since=since)
        if not batch:
            break
        for bar in batch:
            collected[bar.timestamp] = bar
        newest_timestamp = batch[-1].timestamp
        next_since = int(newest_timestamp.timestamp())
        if next_since <= since:
            break
        since = next_since
        if newest_timestamp >= datetime.now(timezone.utc) - timedelta(seconds=interval_seconds):
            break
        time.sleep(request_pause_seconds)

    bars = sorted(collected.values(), key=lambda bar: bar.timestamp)
    return bars


def load_or_fetch_kraken_history(
    symbol: str,
    *,
    interval_seconds: int = 3600,
    lookback_days: int = 90,
    cache_dir: str | Path = "data/historical_cache",
    refresh: bool = False,
) -> list[OHLCVBar]:
    """Load cached historical bars if present and fresh enough, otherwise fetch and cache them.

    Caches to local parquet so repeated research runs (walk-forward sweeps
    across strategies/params) don't re-fetch the same history from Kraken
    every time. Set `refresh=True` to force a fresh pull.
    """
    import pandas as pd

    cache_path = Path(cache_dir) / f"{symbol.replace('/', '-')}_{interval_seconds}s.parquet"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not refresh and cache_path.exists():
        frame = pd.read_parquet(cache_path)
        cached_bars = [
            OHLCVBar(
                exchange="kraken",
                symbol=symbol,
                interval_seconds=interval_seconds,
                timestamp=row.timestamp.to_pydatetime(),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
            )
            for row in frame.itertuples()
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        newest_cached = cached_bars[-1].timestamp if cached_bars else None
        oldest_cached = cached_bars[0].timestamp if cached_bars else None
        is_fresh = newest_cached is not None and newest_cached >= datetime.now(timezone.utc) - timedelta(seconds=interval_seconds * 3)
        # Require the cache to actually cover close to the requested depth,
        # not just be fresh - a short cache (e.g. from an earlier 5-day
        # request) must not be silently reused to satisfy a 90-day request.
        # A generous tolerance (not just a few bars) absorbs the normal gap
        # between when the cache was written and when it's read back.
        depth_tolerance = max(timedelta(seconds=interval_seconds * 3), timedelta(hours=6))
        is_deep_enough = oldest_cached is not None and oldest_cached <= cutoff + depth_tolerance
        if is_fresh and is_deep_enough:
            return [bar for bar in cached_bars if bar.timestamp >= cutoff]

    bars = fetch_kraken_ohlcv_history(symbol=symbol, interval_seconds=interval_seconds, lookback_days=lookback_days)
    frame = pd.DataFrame(
        {
            "timestamp": [bar.timestamp for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )
    frame.to_parquet(cache_path, index=False)
    return bars


def _request_json(method: str, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if params:
        query = urllib.parse.urlencode(params)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"

    request = urllib.request.Request(url, method=method, headers={"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8")
        return json.loads(payload)


def _normalize_pair_code(symbol: str) -> str:
    symbol_map = {
        "BTC/EUR": "XXBTZEUR",
        "BTC/USD": "XXBTZUSD",
        "ETH/EUR": "XETHZEUR",
        "ETH/USD": "XETHZUSD",
    }
    return symbol_map.get(symbol, symbol.upper().replace("/", ""))


def _kraken_interval_minutes(interval_seconds: int) -> int:
    interval_map = {
        60: 1,
        300: 5,
        900: 15,
        3600: 60,
        14400: 240,
        86400: 1440,
        604800: 10080,
        5184000: 21600,
    }
    if interval_seconds not in interval_map:
        raise ValueError(f"Unsupported Kraken interval: {interval_seconds}")
    return interval_map[interval_seconds]
