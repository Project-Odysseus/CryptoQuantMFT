"""FX rate collection helpers for EUR/NOK with local caching and fallback."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings


class FXRateCollector:
    """Fetch and cache FX rates from an upstream provider with fallback support."""

    def __init__(self, cache_path: str | Path | None = None) -> None:
        self.cache_path = Path(cache_path or "data/fx_rates.db")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fx_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    rate REAL NOT NULL,
                    fetched_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def get_rate(self, pair: str = "EUR/NOK") -> float:
        """Return the latest FX rate, using a cached value when the upstream source is unavailable."""
        rate = self._load_latest_rate(pair)
        if rate is not None:
            return rate

        try:
            rate = self._fetch_live_rate(pair)
        except Exception:
            return float(settings.eur_nok_fallback)

        self._store_rate(pair, rate)
        return rate

    def _fetch_live_rate(self, pair: str) -> float:
        if pair.upper() != "EUR/NOK":
            raise ValueError(f"Unsupported FX pair: {pair}")

        payload = self._request_json("https://api.kraken.com/0/public/Ticker", params={"pair": "EURNOK"})
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Kraken FX payload")
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))

        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected Kraken FX result payload")

        market = result.get("EURNOK")
        if market is None:
            raise ValueError("Kraken does not expose EURNOK")

        ask = market.get("a", [0, 0, 0])[0]
        bid = market.get("b", [0, 0, 0])[0]
        return float((float(bid) + float(ask)) / 2.0)

    def _load_latest_rate(self, pair: str) -> float | None:
        with closing(sqlite3.connect(self.cache_path)) as connection:
            row = connection.execute(
                "SELECT rate FROM fx_rates WHERE pair = ? ORDER BY id DESC LIMIT 1",
                (pair,),
            ).fetchone()
        return None if row is None else float(row[0])

    def _store_rate(self, pair: str, rate: float) -> None:
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute(
                "INSERT INTO fx_rates (pair, rate, fetched_at) VALUES (?, ?, ?)",
                (pair, rate, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()

    def _request_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        request = urllib.request.Request(url, headers={"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
