"""Helpers for fetching historical OHLCV bars from public exchange APIs."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
