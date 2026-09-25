"""FX rate collection helpers for EUR/NOK with Norges Bank daily caching."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import settings

NORGES_BANK_EUR_NOK_URL = "https://data.norges-bank.no/api/data/EXR/B.EUR.NOK.SP"
NORGES_BANK_SERIES_URLS = {
    "EUR/NOK": NORGES_BANK_EUR_NOK_URL,
    "USD/NOK": "https://data.norges-bank.no/api/data/EXR/B.USD.NOK.SP",
}


class FXRateCollector:
    """Fetch and cache daily FX rates with business-day fallback."""

    def __init__(self, cache_path: str | Path | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.cache_path = Path(cache_path or "data/fx_rates.db")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fx_rates_daily (
                    pair TEXT NOT NULL,
                    rate_date TEXT NOT NULL,
                    rate REAL NOT NULL,
                    fetched_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (pair, rate_date)
                )
                """
            )
            connection.commit()

    def get_rate(self, pair: str = "EUR/NOK", at: date | datetime | str | None = None) -> float:
        """Return the official daily FX rate for the requested date or prior business day.

        EUR/NOK falls back to the configured `eur_nok_fallback` when Norges
        Bank is unreachable. USD/NOK has no fallback: a tax figure built on a
        guessed rate is worse than a visible failure, so it raises instead.
        """
        normalized_pair = pair.upper()
        if normalized_pair not in NORGES_BANK_SERIES_URLS:
            raise ValueError(f"Unsupported FX pair: {pair}")

        target_date = self._normalize_target_date(at)
        for day_offset in range(0, 8):
            candidate_date = target_date - timedelta(days=day_offset)
            cached_rate = self._load_rate_for_date(normalized_pair, candidate_date)
            if cached_rate is not None:
                return cached_rate

            try:
                fetched_rate = self._fetch_rate_for_date(normalized_pair, candidate_date)
            except Exception:
                fetched_rate = None
            if fetched_rate is None:
                continue

            self._store_rate(normalized_pair, candidate_date, fetched_rate, source="norges_bank")
            return fetched_rate

        if normalized_pair != "EUR/NOK":
            raise LookupError(f"no Norges Bank {normalized_pair} rate found for {target_date} or the 7 days before")
        return float(settings.eur_nok_fallback)

    def _fetch_rate_for_date(self, pair: str, target_date: date) -> float | None:
        if pair not in NORGES_BANK_SERIES_URLS:
            raise ValueError(f"Unsupported FX pair: {pair}")
        payload = self._request_json(
            NORGES_BANK_SERIES_URLS[pair],
            params={
                "format": "sdmx-json",
                "startPeriod": target_date.isoformat(),
                "endPeriod": target_date.isoformat(),
            },
        )
        return self._extract_daily_rate(payload)

    def _extract_daily_rate(self, payload: dict[str, Any] | list[Any]) -> float | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        datasets = data.get("dataSets")
        if not isinstance(datasets, list) or not datasets:
            return None
        dataset = datasets[0]
        if not isinstance(dataset, dict):
            return None
        series = dataset.get("series")
        if not isinstance(series, dict) or not series:
            return None
        first_series = next(iter(series.values()))
        if not isinstance(first_series, dict):
            return None
        observations = first_series.get("observations")
        if not isinstance(observations, dict) or not observations:
            return None
        first_key = sorted(observations.keys(), key=lambda value: int(value))[0]
        observation = observations.get(first_key)
        if not isinstance(observation, list) or not observation:
            return None
        return float(observation[0])

    def _load_rate_for_date(self, pair: str, target_date: date) -> float | None:
        with closing(sqlite3.connect(self.cache_path)) as connection:
            row = connection.execute(
                "SELECT rate FROM fx_rates_daily WHERE pair = ? AND rate_date = ?",
                (pair, target_date.isoformat()),
            ).fetchone()
        return None if row is None else float(row[0])

    def _store_rate(self, pair: str, target_date: date, rate: float, *, source: str) -> None:
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute(
                """
                INSERT INTO fx_rates_daily (pair, rate_date, rate, fetched_at, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pair, rate_date) DO UPDATE SET
                    rate = excluded.rate,
                    fetched_at = excluded.fetched_at,
                    source = excluded.source
                """,
                (
                    pair,
                    target_date.isoformat(),
                    float(rate),
                    datetime.now(timezone.utc).isoformat(),
                    source,
                ),
            )
            connection.commit()

    def _normalize_target_date(self, value: date | datetime | str | None) -> date:
        if value is None:
            return datetime.now(timezone.utc).date()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).date() if value.tzinfo is not None else value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                return date.fromisoformat(value)
        raise TypeError(f"Unsupported FX date value: {value!r}")

    def _request_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        request = urllib.request.Request(url, headers={"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
