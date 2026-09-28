"""Point-in-time research data for the H1-H3 hypothesis studies (BTC, ETH, SOL), with the holdout locked.

Every frame returned here has a UTC `available_at` column: the moment the row could first have been known. Signals
may only use rows with `available_at <= decision time`, and `asof` does that join. The conventions:

- Candles (Binance perp and spot, 1h and 5m; quarterly futures 1d; the perp premium index 1d) are stamped at their
  open by Binance, so they become available at open + interval.
- Funding is available at its settlement time (the actual timestamps, which differ by venue and change over time).
- Open interest: Binance's 5-minute snapshots and Bybit's hourly ones are lagged by one sampling interval, because
  it isn't documented whether the stamp is the start or the end of the window. Being late is safe; being early leaks.
- DVOL candles are stamped at their open and the close keeps updating until the hour ends, so the close is available
  at open + 1h.

The frozen final holdout (`governance.FINAL_HOLDOUT_START`) is enforced on every read: rows available at or after it
are dropped unless the holdout is unlocked, and asking for an `end` inside it raises `HoldoutLocked`. Downloads are
separate (`update_hypothesis_data`) and fetch everything, holdout included, so the one final look needs no download.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from src.data import binance_archive, positioning
from src.research import governance

COINS = ("BTC", "ETH", "SOL")
CACHE_DIR = Path("data/historical_cache/binance_um")
HOURLY_START = {"BTC": "2019-09", "ETH": "2019-11", "SOL": "2020-09"}
FIVE_MINUTE_START = "2021-03"  # DVOL starts 2021-03-24; 5-minute bars are only needed for realized vol against it
INTERVALS = {"1h": timedelta(hours=1), "5m": timedelta(minutes=5), "1d": timedelta(days=1)}
_QUARTERLY = re.compile(r"^(BTC|ETH|SOL)USDT_(\d{6})$")


class HoldoutLocked(RuntimeError):
    """Raised when research code asks for data inside the frozen final holdout without unlocking it."""


# --- the holdout lock -----------------------------------------------------------------------------------------------

def holdout_start() -> pd.Timestamp:
    """First moment of the frozen final holdout."""
    return pd.Timestamp(governance.FINAL_HOLDOUT_START)


def check_end(end: Any) -> None:
    """Refuse an explicit request for holdout data while the holdout is locked."""
    if end is not None and pd.Timestamp(end, tz="UTC") > holdout_start() and not governance.unlocked_reason():
        raise HoldoutLocked(f"end={end} is inside the frozen holdout (from {holdout_start():%Y-%m-%d}); "
                            "unlock with governance.final_holdout(reason) only for a finished candidate's final test")


def lock(frame: pd.DataFrame, *, label: str, end: Any = None) -> pd.DataFrame:
    """`frame` restricted to rows available before the holdout (or before `end`), unless the holdout is unlocked."""
    check_end(end)
    if frame.empty:
        return frame
    available = pd.to_datetime(frame["available_at"], utc=True)
    keep = np.ones(len(frame), dtype=bool)
    if governance.unlocked_reason():
        governance._note_look(label)  # every look at the frozen data is logged
    else:
        keep &= (available < holdout_start()).to_numpy()
    if end is not None:
        keep &= (available <= pd.Timestamp(end, tz="UTC")).to_numpy()
    return frame[keep].reset_index(drop=True)


def asof(series: pd.DataFrame, decision_times: pd.DatetimeIndex, columns: Iterable[str] | None = None) -> pd.DataFrame:
    """For each decision time, the latest row of `series` with `available_at <= decision time` (no look-ahead)."""
    columns = list(columns) if columns is not None else [c for c in series.columns if c not in ("available_at", "timestamp")]
    right = series.sort_values("available_at", kind="stable")[["available_at", *columns]].copy()
    right["available_at"] = pd.to_datetime(right["available_at"], utc=True).astype("datetime64[ns, UTC]")
    right = right.drop_duplicates("available_at", keep="last")  # rows published together: the last one written wins
    left = pd.DataFrame({"decision": pd.DatetimeIndex(decision_times).tz_convert("UTC").astype("datetime64[ns, UTC]")})
    merged = pd.merge_asof(left, right, left_on="decision", right_on="available_at", direction="backward")
    return merged.set_index("decision")[columns]


# --- downloads (everything, holdout included; reads are locked) -----------------------------------------------------

def _months(first: str, last: str) -> list[str]:
    return [f"{p.year:04d}-{p.month:02d}" for p in pd.period_range(first, last, freq="M")]


def _last_full_month() -> str:
    today = datetime.now(timezone.utc)
    return f"{(today.replace(day=1) - timedelta(days=1)):%Y-%m}"


def _kline_path(symbol: str, interval: str, market: str, month: str) -> Path:
    return CACHE_DIR / f"klines_{interval}" / market / f"{symbol}-{month}.parquet"


def _download_kline_month(symbol: str, interval: str, market: str, month: str) -> bool:
    """One monthly kline file into the same cache `binance_archive.load_klines` uses, with volume and taker flow."""
    path = _kline_path(symbol, interval, market, month)
    if path.exists():
        return True
    template = binance_archive.INTRADAY_PATHS[market] if interval != "1d" else "data/futures/um/monthly/klines/{symbol}/1d/{symbol}-1d-{month}.zip"
    payload = binance_archive._fetch(binance_archive.ARCHIVE_URL + template.format(symbol=symbol, interval=interval, month=month))
    if payload is None:
        return False
    raw = binance_archive._read_zip_csv(payload, binance_archive.KLINE_COLUMNS)
    opened = pd.to_numeric(raw["open_time"], errors="coerce")
    frame = pd.DataFrame({"timestamp": pd.to_datetime(opened, unit="us" if opened.max() > 1e14 else "ms", utc=True)})
    for column in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume"):
        frame[column] = pd.to_numeric(raw[column], errors="coerce")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.dropna(subset=["timestamp", "close"]).to_parquet(path, index=False)
    return True


def quarterly_symbols() -> list[str]:
    """Binance USDT-margined quarterly futures on BTC and ETH, expired ones included (SOL has none)."""
    prefixes = binance_archive._list("data/futures/um/monthly/klines/", tag="Prefix")
    names = [prefix.rstrip("/").rsplit("/", 1)[-1] for prefix in prefixes]
    return sorted(name for name in names if _QUARTERLY.match(name))


def update_hypothesis_data(*, five_minute: bool = True, workers: int = 12) -> dict[str, int]:
    """Download what the H1-H3 studies need and the repo lacks; resumable (only missing files are fetched).

    - Binance perp and spot 1h klines for BTC, ETH, SOL (price, volume, taker flow);
    - Binance perp 5m klines for BTC and ETH from 2021-03 (realized vol against DVOL);
    - daily klines of every BTC/ETH quarterly future (the quarterly basis);
    - SOL positioning (Binance funding and 5-minute OI archive, Bybit funding and hourly OI), and a top-up of BTC/ETH.
    """
    last = _last_full_month()
    tasks: list[tuple[str, str, str, str]] = []
    for coin in COINS:
        for market in ("futures", "spot"):
            tasks += [(f"{coin}USDT", "1h", market, month) for month in _months(HOURLY_START[coin], last)]
    if five_minute:
        for coin in ("BTC", "ETH"):
            tasks += [(f"{coin}USDT", "5m", "futures", month) for month in _months(FIVE_MINUTE_START, last)]
    for symbol in quarterly_symbols():
        expiry = pd.Timestamp(datetime.strptime(symbol.split("_")[1], "%y%m%d"))
        tasks += [(symbol, "1d", "futures", month) for month in _months(f"{expiry - pd.Timedelta(days=200):%Y-%m}", min(f"{expiry:%Y-%m}", last))]
    missing = [task for task in tasks if not _kline_path(*task).exists()]
    logger.info("hypothesis data: {} kline files, {} missing", len(tasks), len(missing))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = sum(pool.map(lambda task: _download_kline_month(*task), missing))
    for coin in COINS:
        for name in ("binance_funding", "binance_metrics", "bybit_funding", "bybit_open_interest"):
            positioning.load_series(name, coin)
        if coin != "SOL":
            positioning.load_series("deribit_dvol", coin)
    return {"kline_files": len(tasks), "fetched": int(fetched)}


# --- loaders (locked) -----------------------------------------------------------------------------------------------

def _read_klines(symbol: str, interval: str, market: str) -> pd.DataFrame:
    folder = CACHE_DIR / f"klines_{interval}" / market
    frames = [pd.read_parquet(path) for path in sorted(folder.glob(f"{symbol}-*.parquet"))]
    if not frames:
        raise FileNotFoundError(f"no {interval} {market} klines cached for {symbol}; run update_hypothesis_data()")
    frame = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    frame["available_at"] = frame["timestamp"] + INTERVALS[interval]
    return frame


def load_bars(coin: str, interval: str = "1h", *, market: str = "futures", end: Any = None) -> pd.DataFrame:
    """Binance candles indexed by open time, with `available_at` (the close). Holdout-locked."""
    frame = _read_klines(f"{coin}USDT", interval, market)
    return lock(frame, label=f"pit {coin} {market} {interval}", end=end).set_index("timestamp", drop=False)


def load_funding(coin: str, venue: str, *, end: Any = None) -> pd.DataFrame:
    """Settled funding per 8h-equivalent fraction at its actual settlement times, `available_at` = settlement."""
    frame = positioning.load_series(f"{venue}_funding", coin)
    frame = frame.rename(columns={f"{venue}_funding": "rate"})[["timestamp", "rate"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["available_at"] = frame["timestamp"]
    return lock(frame.dropna(), label=f"pit {coin} {venue} funding", end=end)


def load_open_interest(coin: str, venue: str, *, end: Any = None) -> pd.DataFrame:
    """Open interest in coins (`oi`) and, for Binance, USD (`oi_usd`), lagged one sampling interval (see module doc)."""
    if venue == "binance":
        raw = positioning.load_series("binance_metrics", coin)
        frame = pd.DataFrame({"timestamp": pd.to_datetime(raw["timestamp"], utc=True), "oi": raw["binance_open_interest"],
                              "oi_usd": raw["binance_open_interest_usd"], "account_long_short": raw["binance_account_long_short"],
                              "top_trader_long_short": raw["binance_top_trader_long_short"]})
        lag = timedelta(minutes=5)
    elif venue == "bybit":
        raw = positioning.load_series("bybit_open_interest", coin)
        frame = pd.DataFrame({"timestamp": pd.to_datetime(raw["timestamp"], utc=True), "oi": raw["bybit_open_interest"]})
        lag = timedelta(hours=1)
    else:
        raise ValueError(f"no open interest for venue {venue!r}")
    frame["available_at"] = frame["timestamp"] + lag
    return lock(frame.dropna(subset=["oi"]).sort_values("timestamp").reset_index(drop=True), label=f"pit {coin} {venue} oi", end=end)


def load_dvol(coin: str, *, end: Any = None) -> pd.DataFrame:
    """Deribit DVOL (30-day implied vol, annualised %) at hourly candle closes; BTC and ETH only."""
    if coin == "SOL":
        raise ValueError("Deribit publishes no DVOL for SOL")
    raw = positioning.load_series("deribit_dvol", coin)
    frame = pd.DataFrame({"timestamp": pd.to_datetime(raw["timestamp"], utc=True), "dvol": raw["dvol"].astype(float)})
    frame["available_at"] = frame["timestamp"] + timedelta(hours=1)
    return lock(frame, label=f"pit {coin} dvol", end=end)


def load_quarterly_basis(coin: str, *, end: Any = None, min_days: float = 14.0) -> pd.DataFrame:
    """Annualised basis of the nearest quarterly future with at least `min_days` to expiry, over Binance spot, daily.

    basis = (F / S - 1) * 365 / days_to_expiry, from daily closes (available at the next 00:00 UTC).
    """
    spot = _read_klines(f"{coin}USDT", "1h", "spot")
    spot_daily = spot.set_index("available_at")["close"]
    spot_daily = spot_daily[spot_daily.index.hour == 0]
    rows = []
    for symbol in quarterly_symbols():
        if not symbol.startswith(f"{coin}USDT_"):
            continue
        try:
            futures = _read_klines(symbol, "1d", "futures")
        except FileNotFoundError:
            continue
        expiry = pd.Timestamp(datetime.strptime(symbol.split("_")[1], "%y%m%d"), tz="UTC") + pd.Timedelta(hours=8)  # Binance delivers at 08:00 UTC
        futures = futures.assign(symbol=symbol, expiry=expiry)
        rows.append(futures[["available_at", "close", "symbol", "expiry"]])
    if not rows:
        raise FileNotFoundError(f"no quarterly futures cached for {coin}")
    quarterly = pd.concat(rows, ignore_index=True)
    quarterly["days"] = (quarterly["expiry"] - quarterly["available_at"]).dt.total_seconds() / 86400
    quarterly = quarterly[quarterly["days"] >= min_days].sort_values(["available_at", "days"]).groupby("available_at").head(1)
    quarterly["spot"] = quarterly["available_at"].map(spot_daily)
    quarterly["basis_ann"] = (quarterly["close"] / quarterly["spot"] - 1.0) * 365.0 / quarterly["days"]
    frame = quarterly.dropna(subset=["basis_ann"]).rename(columns={"available_at": "timestamp"})
    frame["available_at"] = frame["timestamp"]
    return lock(frame[["timestamp", "available_at", "symbol", "days", "basis_ann"]].reset_index(drop=True), label=f"pit {coin} quarterly basis", end=end)


def coverage() -> pd.DataFrame:
    """Start, end, rows and the largest gap per series and coin, over the research window (holdout excluded)."""
    records: list[dict[str, Any]] = []

    def add(name: str, coin: str, frame: pd.DataFrame, step: timedelta) -> None:
        times = pd.to_datetime(frame["available_at"], utc=True).sort_values()
        gaps = times.diff().dropna()
        records.append({"series": name, "coin": coin, "start": f"{times.min():%Y-%m-%d}", "end": f"{times.max():%Y-%m-%d}", "rows": len(frame),
                        "expected_step": str(step), "max_gap": str(gaps.max()) if len(gaps) else "", "gaps_over_3_steps": int((gaps > 3 * step).sum())})

    for coin in COINS:
        for market in ("futures", "spot"):
            add(f"binance {market} 1h", coin, load_bars(coin, "1h", market=market), timedelta(hours=1))
        if coin != "SOL":
            add("binance futures 5m", coin, load_bars(coin, "5m"), timedelta(minutes=5))
            add("deribit dvol", coin, load_dvol(coin), timedelta(hours=1))
            add("quarterly basis (daily)", coin, load_quarterly_basis(coin), timedelta(days=1))
        for venue in ("binance", "bybit"):
            add(f"{venue} funding", coin, load_funding(coin, venue), timedelta(hours=8))
            add(f"{venue} open interest", coin, load_open_interest(coin, venue), timedelta(minutes=5) if venue == "binance" else timedelta(hours=1))
    return pd.DataFrame(records)
