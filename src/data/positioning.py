"""Public derivatives-positioning data: funding, open interest, trader ratios, taker flow and implied volatility.

Everything here is public market data (no API keys) from the largest perp
venues, where most crypto positioning lives:

- Binance USD-M futures: funding every 8h since 2019-09, hourly candles with
  taker-buy volume (buyer-initiated flow) since 2019-09, and 5-minute open
  interest, top-trader long/short and taker long/short ratios from its public
  data archive (data.binance.vision) since 2021-11.
- Bybit linear perps: funding since 2020-03 and hourly open interest.
- Deribit: hourly perpetual funding since 2019 and the DVOL implied-volatility
  index since 2021-03 (the market's 30-day volatility forecast from options).

OKX's API only keeps a few months of funding and open interest, and no venue
publishes liquidation history, so those have to be recorded live.

Each series is cached as parquet under data/historical_cache/positioning/ and
topped up incrementally. `load_positioning` aligns them to a bar series so a
feature at a bar only uses values published by that bar's close.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

CACHE_DIR = Path("data/historical_cache/positioning")
USER_AGENT = {"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"}
COINS = {"BTC": {"binance": "BTCUSDT", "bybit": "BTCUSDT", "deribit": "BTC-PERPETUAL", "dvol": "BTC"}, "ETH": {"binance": "ETHUSDT", "bybit": "ETHUSDT", "deribit": "ETH-PERPETUAL", "dvol": "ETH"}}


def _get_json(url: str, params: dict[str, Any] | None = None, *, retries: int = 3) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=USER_AGENT), timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return None


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _to_utc(values: Any, unit: str = "ms") -> pd.DatetimeIndex:
    return pd.to_datetime(pd.to_numeric(values), unit=unit, utc=True)


# ---- Binance ------------------------------------------------------------------

def fetch_binance_funding(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Settled funding rates (fraction per 8h) with the time they were settled."""
    rows: list[dict[str, Any]] = []
    cursor = _ms(start)
    while cursor < _ms(end):
        batch = _get_json("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol": symbol, "startTime": cursor, "endTime": _ms(end), "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "binance_funding"])
    return pd.DataFrame({"timestamp": _to_utc(frame["fundingTime"]), "binance_funding": frame["fundingRate"].astype(float)})


def fetch_binance_klines(symbol: str, start: datetime, end: datetime, interval: str = "1h") -> pd.DataFrame:
    """Candles with total and taker-buy (buyer-initiated) volume; timestamp is the bar's open time."""
    rows: list[list[Any]] = []
    cursor = _ms(start)
    while cursor < _ms(end):
        batch = _get_json("https://fapi.binance.com/fapi/v1/klines", {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": _ms(end), "limit": 1500})
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + 1
        if len(batch) < 1500:
            break
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "binance_volume", "binance_taker_buy_volume", "binance_trades"])
    return pd.DataFrame(
        {
            "timestamp": _to_utc(frame[0]),
            "binance_volume": frame[5].astype(float),
            "binance_taker_buy_volume": frame[9].astype(float),
            "binance_trades": frame[8].astype(float),
        }
    )


def _binance_metrics_day(symbol: str, day: datetime) -> pd.DataFrame | None:
    url = f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day:%Y-%m-%d}.zip"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=USER_AGENT), timeout=30) as response:
                payload = response.read()
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    else:
        return None
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        frame = pd.read_csv(archive.open(archive.namelist()[0]))
    return frame


def fetch_binance_metrics(symbol: str, start: datetime, end: datetime, *, workers: int = 8) -> pd.DataFrame:
    """5-minute open interest and long/short ratios from Binance's public daily archive (from 2021-11)."""
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    days = [day for day in days if day.date() < datetime.now(timezone.utc).date()]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        frames = [frame for frame in pool.map(lambda day: _binance_metrics_day(symbol, day), days) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["timestamp"])
    frame = pd.concat(frames, ignore_index=True)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(frame["create_time"], utc=True),
            "binance_open_interest": pd.to_numeric(frame["sum_open_interest"], errors="coerce"),
            "binance_open_interest_usd": pd.to_numeric(frame["sum_open_interest_value"], errors="coerce"),
            "binance_top_trader_long_short": pd.to_numeric(frame["sum_toptrader_long_short_ratio"], errors="coerce"),
            "binance_account_long_short": pd.to_numeric(frame["count_long_short_ratio"], errors="coerce"),
            "binance_taker_long_short": pd.to_numeric(frame["sum_taker_long_short_vol_ratio"], errors="coerce"),
        }
    )


# ---- Bybit --------------------------------------------------------------------

def fetch_bybit_funding(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Settled funding rates on Bybit linear perps, paging backwards from `end`."""
    rows: list[dict[str, Any]] = []
    cursor_end = _ms(end)
    while cursor_end > _ms(start):
        payload = _get_json("https://api.bybit.com/v5/market/funding/history", {"category": "linear", "symbol": symbol, "startTime": _ms(start), "endTime": cursor_end, "limit": 200})
        batch = (payload or {}).get("result", {}).get("list", [])
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(row["fundingRateTimestamp"]) for row in batch)
        if oldest >= cursor_end or len(batch) < 200:
            break
        cursor_end = oldest - 1
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "bybit_funding"])
    return pd.DataFrame({"timestamp": _to_utc(frame["fundingRateTimestamp"]), "bybit_funding": frame["fundingRate"].astype(float)})


def fetch_bybit_open_interest(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Hourly open interest on Bybit linear perps (in coins)."""
    rows: list[dict[str, Any]] = []
    window = timedelta(hours=200)
    cursor = start
    while cursor < end:
        upper = min(end, cursor + window)
        payload = _get_json("https://api.bybit.com/v5/market/open-interest", {"category": "linear", "symbol": symbol, "intervalTime": "1h", "startTime": _ms(cursor), "endTime": _ms(upper), "limit": 200})
        rows.extend((payload or {}).get("result", {}).get("list", []))
        cursor = upper
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "bybit_open_interest"])
    return pd.DataFrame({"timestamp": _to_utc(frame["timestamp"]), "bybit_open_interest": frame["openInterest"].astype(float)})


# ---- Deribit ------------------------------------------------------------------

def fetch_deribit_funding(instrument: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Hourly funding on Deribit's perpetual, as the 8h-equivalent rate (comparable with Binance/Bybit)."""
    rows: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        upper = min(end, cursor + timedelta(days=30))
        payload = _get_json("https://www.deribit.com/api/v2/public/get_funding_rate_history", {"instrument_name": instrument, "start_timestamp": _ms(cursor), "end_timestamp": _ms(upper)})
        rows.extend((payload or {}).get("result", []))
        cursor = upper
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "deribit_funding"])
    return pd.DataFrame({"timestamp": _to_utc(frame["timestamp"]), "deribit_funding": frame["interest_8h"].astype(float)})


def fetch_deribit_dvol(currency: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Hourly DVOL: the 30-day implied volatility index from Deribit options, in annualised percent (from 2021-03)."""
    rows: list[list[Any]] = []
    cursor = start
    while cursor < end:
        upper = min(end, cursor + timedelta(hours=900))
        payload = _get_json("https://www.deribit.com/api/v2/public/get_volatility_index_data", {"currency": currency, "start_timestamp": _ms(cursor), "end_timestamp": _ms(upper), "resolution": 3600})
        rows.extend((payload or {}).get("result", {}).get("data", []))
        cursor = upper
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "dvol"])
    return pd.DataFrame({"timestamp": _to_utc(frame[0]), "dvol": frame[4].astype(float)})


# ---- caching and alignment ----------------------------------------------------

SOURCES: dict[str, tuple[Callable[[str, datetime, datetime], pd.DataFrame], str, datetime]] = {
    "binance_funding": (fetch_binance_funding, "binance", datetime(2019, 9, 1, tzinfo=timezone.utc)),
    "binance_klines": (fetch_binance_klines, "binance", datetime(2019, 9, 1, tzinfo=timezone.utc)),
    "binance_metrics": (fetch_binance_metrics, "binance", datetime(2021, 11, 1, tzinfo=timezone.utc)),
    "bybit_funding": (fetch_bybit_funding, "bybit", datetime(2020, 3, 1, tzinfo=timezone.utc)),
    "bybit_open_interest": (fetch_bybit_open_interest, "bybit", datetime(2020, 3, 1, tzinfo=timezone.utc)),
    "deribit_funding": (fetch_deribit_funding, "deribit", datetime(2019, 6, 1, tzinfo=timezone.utc)),
    "deribit_dvol": (fetch_deribit_dvol, "dvol", datetime(2021, 3, 1, tzinfo=timezone.utc)),
}


def load_series(name: str, coin: str, *, refresh: bool = False, cache_dir: Path | str | None = None) -> pd.DataFrame:
    """One positioning series for BTC or ETH from its first available date to now, cached and topped up."""
    if name not in SOURCES:
        raise ValueError(f"unknown series {name!r}; known: {sorted(SOURCES)}")
    coin = coin.upper()
    if coin not in COINS:
        raise ValueError(f"unknown coin {coin!r}; known: {sorted(COINS)}")
    fetch, venue_key, first = SOURCES[name]
    path = Path(cache_dir or CACHE_DIR) / f"{name}_{coin}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    cached = pd.read_parquet(path) if path.exists() and not refresh else pd.DataFrame(columns=["timestamp"])
    since = cached["timestamp"].max() + pd.Timedelta(milliseconds=1) if len(cached) else pd.Timestamp(first)
    now = datetime.now(timezone.utc)
    if since.to_pydatetime() < now - timedelta(hours=1):
        fresh = fetch(COINS[coin][venue_key], since.to_pydatetime(), now)
        combined = pd.concat([cached, fresh], ignore_index=True) if len(cached) else fresh
        combined = combined.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        combined.to_parquet(path, index=False)
        return combined
    return cached.sort_values("timestamp").reset_index(drop=True)


def align_to_bars(series_frame: pd.DataFrame, bar_close_times: pd.DatetimeIndex, *, stamped_at_open: timedelta | None = None) -> pd.DataFrame:
    """For each bar, the latest value published at or before the bar's close (no look-ahead).

    Args:
        stamped_at_open: The series is itself a bar series stamped with its
            open time (Binance klines); pass its bar length so each row counts
            as published when that bar closed.
    """
    frame = series_frame.dropna(subset=["timestamp"]).sort_values("timestamp")
    if stamped_at_open is not None:
        frame = frame.assign(timestamp=frame["timestamp"] + stamped_at_open)
    frame = frame.assign(timestamp=pd.to_datetime(frame["timestamp"], utc=True).astype("datetime64[ns, UTC]"))
    target = pd.DataFrame({"bar_close": pd.DatetimeIndex(bar_close_times).astype("datetime64[ns, UTC]")})
    merged = pd.merge_asof(target, frame.rename(columns={"timestamp": "published"}), left_on="bar_close", right_on="published", direction="backward")
    return merged.drop(columns=["published"]).set_index("bar_close")


def load_positioning(coin: str, bar_close_times: pd.DatetimeIndex, *, refresh: bool = False) -> pd.DataFrame:
    """All positioning series for `coin`, aligned to the given bar close times (hourly bars recommended)."""
    parts = [
        align_to_bars(load_series("binance_funding", coin, refresh=refresh), bar_close_times),
        align_to_bars(load_series("bybit_funding", coin, refresh=refresh), bar_close_times),
        align_to_bars(load_series("deribit_funding", coin, refresh=refresh), bar_close_times),
        align_to_bars(load_series("binance_klines", coin, refresh=refresh), bar_close_times, stamped_at_open=timedelta(hours=1)),
        align_to_bars(load_series("binance_metrics", coin, refresh=refresh), bar_close_times),
        align_to_bars(load_series("bybit_open_interest", coin, refresh=refresh), bar_close_times),
        align_to_bars(load_series("deribit_dvol", coin, refresh=refresh), bar_close_times),
    ]
    return pd.concat(parts, axis=1)
