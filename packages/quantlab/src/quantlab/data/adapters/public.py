"""Free public sources: Yahoo bars, OECD short rates, Norges Bank FX fixings.

Plain blocking HTTP (urllib): research loads are batch jobs, and the catalog caches every result, so each series is
fetched once. Parsing is separated from fetching (`parse_*`) so it is tested on recorded payloads without a network.
"""

from __future__ import annotations

import io
import json
import urllib.request
from typing import Any, Callable, Mapping
from urllib.parse import quote

import numpy as np
import pandas as pd

USER_AGENT = {"User-Agent": "Mozilla/5.0 (quantlab research)"}
Fetch = Callable[[str], bytes]


def http_get(url: str, *, timeout: float = 60.0) -> bytes:
    """GET a URL and return its body."""
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoints
        return response.read()


# --- Yahoo -------------------------------------------------------------------------------------------------------------

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start}&period2={end}&interval={interval}"
YAHOO_INTERVALS = {"1d": "1d", "1h": "60m"}


def parse_yahoo_chart(payload: Mapping[str, Any], *, interval: str, total_return: bool = False) -> pd.DataFrame:
    """OHLCV bars from a Yahoo chart response.

    Daily bars are dated by the exchange-local session date (the timestamp plus the exchange's GMT offset). Yahoo
    repeats the latest trade as an extra row for a session still in progress, so rows without a close are dropped;
    the catalog's frozen-holdout and completeness rules handle the last bar. With `total_return`, prices are scaled by
    Yahoo's adjusted close, so distributions (an ETF's coupons and dividends) are in the returns.
    """
    result = payload["chart"]["result"][0]
    stamps = np.asarray(result.get("timestamp") or [], dtype="int64")
    quotes = result["indicators"]["quote"][0] if len(stamps) else {}
    if interval == "1d":
        index = pd.to_datetime(stamps + int(result["meta"].get("gmtoffset", 0)), unit="s").normalize()
    else:
        index = pd.to_datetime(stamps, unit="s", utc=True) + pd.Timedelta(hours=1)  # Yahoo stamps hourly bars at their open
    frame = pd.DataFrame({name: pd.to_numeric(pd.Series(quotes.get(name), dtype="float64"), errors="coerce").to_numpy() if quotes else []
                          for name in ("open", "high", "low", "close", "volume")}, index=index)
    if total_return and len(stamps):
        adjusted = pd.to_numeric(pd.Series(result["indicators"]["adjclose"][0]["adjclose"], dtype="float64"), errors="coerce").to_numpy()
        factor = adjusted / frame["close"].to_numpy()
        for name in ("open", "high", "low", "close"):
            frame[name] = frame[name] * factor
    frame = frame.dropna(subset=["close"])
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.index.name = "time"
    return frame


class YahooAdapter:
    """Free daily (and recent hourly) bars for indices, ETFs, front-month futures and FX."""

    name, version = "yahoo", "1"

    def __init__(self, fetch: Fetch = http_get) -> None:
        self._fetch = fetch

    def fetch(self, symbol: str, *, interval: str = "1d", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        if interval not in YAHOO_INTERVALS:
            raise ValueError(f"Yahoo serves {sorted(YAHOO_INTERVALS)}, not {interval!r}")
        first = int(pd.Timestamp(start or "1970-01-02", tz="UTC").timestamp())
        last = int(pd.Timestamp(end, tz="UTC").timestamp()) if end else int(pd.Timestamp.now(tz="UTC").timestamp())
        url = YAHOO_CHART.format(symbol=quote(symbol), start=first, end=last, interval=YAHOO_INTERVALS[interval])
        return parse_yahoo_chart(json.loads(self._fetch(url)), interval=interval, total_return=self.total_return)

    total_return = False


class YahooTotalReturnAdapter(YahooAdapter):
    """Yahoo daily bars scaled to total return (adjusted close): for ETFs that stand in for futures."""

    name, version = "yahoo_tr", "1"
    total_return = True


# --- OECD 3-month interbank rates --------------------------------------------------------------------------------------

OECD_RATES = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/{area}.M.IR3TIB.PA._Z._Z._Z._Z.N"
              "?startPeriod={start}&format=csvfile")


def parse_oecd_rates(text: str) -> pd.DataFrame:
    """Monthly rates (fraction a year) indexed by month start, with `available_at` the start of the following month.

    The OECD publishes a month's average after the month ends, so a month's rate is not known during it: using it
    only from the next month on keeps every study point-in-time.
    """
    raw = pd.read_csv(io.StringIO(text))
    months = pd.PeriodIndex(raw["TIME_PERIOD"], freq="M")
    frame = pd.DataFrame({"value": raw["OBS_VALUE"].astype(float).to_numpy() / 100.0}, index=months.to_timestamp()).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    frame["available_at"] = (frame.index.to_period("M") + 1).to_timestamp()
    frame.index.name = "time"
    return frame


class OecdRatesAdapter:
    """Monthly 3-month interbank rates by OECD area code (USA, EA20, GBR, JPN, NOR, CHE, ...)."""

    name, version = "oecd", "1"

    def __init__(self, fetch: Fetch = http_get) -> None:
        self._fetch = fetch

    def fetch(self, symbol: str, *, interval: str = "1M", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        text = self._fetch(OECD_RATES.format(area=symbol, start=(start or "1990-01")[:7])).decode("utf-8")
        frame = parse_oecd_rates(text)
        return frame.loc[:end] if end else frame


# --- Norges Bank FX ----------------------------------------------------------------------------------------------------

NORGES_BANK = "https://data.norges-bank.no/api/data/EXR/B.{currency}.NOK.SP?format=csv&startPeriod={start}&locale=en&bom=include"


def parse_norges_bank(text: str) -> pd.DataFrame:
    """NOK per one unit of the currency, by fixing date (some currencies are quoted per 100: scaled back to one)."""
    raw = pd.read_csv(io.StringIO(text.lstrip("﻿")), sep=";", dtype=str)
    value = pd.to_numeric(raw["OBS_VALUE"], errors="coerce") / 10.0 ** pd.to_numeric(raw["UNIT_MULT"], errors="coerce")
    frame = pd.DataFrame({"value": value.to_numpy()}, index=pd.to_datetime(raw["TIME_PERIOD"]).to_numpy()).dropna().sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    frame.index.name = "time"
    return frame


class NorgesBankAdapter:
    """Daily official FX fixings against NOK (symbol: the currency code, e.g. USD, EUR)."""

    name, version = "norges_bank", "1"

    def __init__(self, fetch: Fetch = http_get) -> None:
        self._fetch = fetch

    def fetch(self, symbol: str, *, interval: str = "1d", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        text = self._fetch(NORGES_BANK.format(currency=symbol, start=(start or "1990-01-01")[:10])).decode("utf-8")
        frame = parse_norges_bank(text)
        return frame.loc[:end] if end else frame


class NorgesBankUsdAdapter:
    """US dollars per one unit of a currency, from two Norges Bank fixings taken at the same time (X/NOK over USD/NOK).

    Daily bars with open = high = low = close (a fixing is one price a day). Cleaner than free spot quotes, which have
    bad ticks; the stand-in for a currency future's history (with the interest differential, quantlab.engine.returns).
    """

    name, version = "norges_bank_usd", "1"

    def __init__(self, fetch: Fetch = http_get) -> None:
        self._source = NorgesBankAdapter(fetch)

    def fetch(self, symbol: str, *, interval: str = "1d", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        if interval != "1d":
            raise ValueError("Norges Bank fixings are daily")
        nok = self._source.fetch(symbol, start=start, end=end)["value"]
        usd = self._source.fetch("USD", start=start, end=end)["value"]
        cross = (nok / usd).dropna()
        frame = pd.DataFrame({"open": cross, "high": cross, "low": cross, "close": cross, "volume": 0.0}, index=cross.index)
        frame.index.name = "time"
        return frame
