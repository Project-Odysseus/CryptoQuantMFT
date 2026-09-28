"""Deribit option chains from the public API: fetch a snapshot, store it, and load the history back (no API keys).

Every pricing model and option strategy gets tested on these snapshots, so
record them from the start (`python main.py --record-option-chains`): one
snapshot of BTC and ETH is about 1,700 contracts and 150 KB, so a year of
hourly snapshots is a few GB of Parquet.

Normalised columns (one row per contract per snapshot):

    timestamp, chain, currency, underlying, settlement, instrument, expiry (08:00 UTC), t (years), strike,
    right, forward, index_price, discount, bid, ask, mark (USD), bid_btc, ask_btc, mark_btc, mark_iv,
    open_interest, volume

Two kinds of Deribit options, told apart by the instrument name:

- **inverse** (`BTC-27DEC26-100000-C`, chain "BTC"): quoted and settled in the coin. Each expiry has its own
  forward (`underlying_price`), and USD price = coin price x forward. The `*_btc` columns are coin prices.
- **linear** (`BTC_USDC-27DEC26-100000-C`, chain "USDC", which lists every coin): quoted and settled in USDC,
  so prices are already in dollars; the `*_btc` columns hold price / forward for comparison.

Each expiry has its own forward (the future or synthetic future for that date) with a zero interest rate.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.utils.logger import logger

PUBLIC_API = "https://www.deribit.com/api/v2/public"
CHAIN_ROOT = Path("data/options/deribit")
SECONDS_PER_YEAR = 365.0 * 86400.0


def parse_instrument(name: str) -> tuple[str, datetime, float, str]:
    """`BTC-25DEC26-100000-C` -> ("BTC", 2026-12-25 08:00 UTC, 100000.0, "call")."""
    currency, expiry, strike, flag = name.split("-")
    moment = datetime.strptime(expiry, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
    return currency, moment, float(strike.replace("d", ".")), "call" if flag == "C" else "put"


def underlying_and_settlement(currency: str) -> tuple[str, str]:
    """`BTC` -> ("BTC", "inverse"); `BTC_USDC` -> ("BTC", "linear")."""
    base, _, settle = currency.partition("_")
    return base, "linear" if settle else "inverse"


def fetch_book_summary(currency: str) -> list[dict[str, Any]]:
    """Deribit's book summary for every option on `currency` (one public request; blocking)."""
    url = f"{PUBLIC_API}/get_book_summary_by_currency?" + urllib.parse.urlencode({"currency": currency, "kind": "option"})
    request = urllib.request.Request(url, headers={"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "result" not in payload:
        raise RuntimeError(f"unexpected Deribit response: {str(payload)[:200]}")
    return payload["result"]


def normalize_chain(rows: Iterable[dict[str, Any]], fetched_at: datetime, *, chain: str | None = None) -> pd.DataFrame:
    """Book-summary rows as one tidy frame in USD (see the module docstring for the columns); `chain` is the requested currency."""
    records = []
    for row in rows:
        try:
            currency, expiry, strike, right = parse_instrument(str(row["instrument_name"]))
        except (ValueError, KeyError):
            continue
        forward = float(row.get("underlying_price") or np.nan)
        t = (expiry - fetched_at).total_seconds() / SECONDS_PER_YEAR
        if not t > 0 or not forward > 0:
            continue
        quoted = {side: (float(row[f"{side}_price"]) if row.get(f"{side}_price") not in (None, 0, 0.0) else np.nan) for side in ("bid", "ask", "mark")}
        underlying, settlement = underlying_and_settlement(currency)
        usd = quoted if settlement == "linear" else {side: price * forward for side, price in quoted.items()}
        coin = {side: price / forward for side, price in usd.items()}
        records.append({
            "timestamp": fetched_at, "chain": chain or currency, "currency": currency, "underlying": underlying, "settlement": settlement,
            "instrument": row["instrument_name"], "expiry": expiry, "t": t, "strike": strike, "right": right,
            "forward": forward, "index_price": float(row.get("estimated_delivery_price") or np.nan),
            "discount": float(np.exp(-float(row.get("interest_rate") or 0.0) * t)),
            "bid": usd["bid"], "ask": usd["ask"], "mark": usd["mark"],
            "bid_btc": coin["bid"], "ask_btc": coin["ask"], "mark_btc": coin["mark"],
            "mark_iv": float(row.get("mark_iv") or np.nan) / 100.0, "open_interest": float(row.get("open_interest") or 0.0), "volume": float(row.get("volume") or 0.0),
        })
    return pd.DataFrame(records).sort_values(["expiry", "strike", "right"]).reset_index(drop=True) if records else pd.DataFrame()


def fetch_chain(currency: str = "BTC", *, now: datetime | None = None) -> pd.DataFrame:
    """One normalised snapshot of every listed option on `currency`."""
    fetched_at = now or datetime.now(timezone.utc)
    return normalize_chain(fetch_book_summary(currency), fetched_at, chain=currency)


def save_chain(chain: pd.DataFrame, root: Path | str = CHAIN_ROOT) -> Path:
    """Store a snapshot as `<root>/<currency>/<YYYY-MM-DD>/<HHMMSS>.parquet` (one file per snapshot, so a crash loses at most one)."""
    stamp = pd.Timestamp(chain["timestamp"].iloc[0])
    folder = chain["chain"].iloc[0] if "chain" in chain.columns else chain["currency"].iloc[0]  # "USDC" holds every linear coin
    path = Path(root) / str(folder) / f"{stamp:%Y-%m-%d}" / f"{stamp:%H%M%S}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    chain.to_parquet(path, index=False)
    return path


def load_chains(currency: str = "BTC", *, start: str | datetime | None = None, end: str | datetime | None = None, root: Path | str = CHAIN_ROOT) -> pd.DataFrame:
    """Every stored snapshot of `currency` between `start` and `end` (dates, inclusive), oldest first."""
    folder = Path(root) / currency
    if not folder.exists():
        return pd.DataFrame()
    first = pd.Timestamp(start).date() if start is not None else None
    last = pd.Timestamp(end).date() if end is not None else None
    frames = []
    for day in sorted(folder.iterdir()):
        date = pd.Timestamp(day.name).date()
        if (first and date < first) or (last and date > last):
            continue
        frames.extend(pd.read_parquet(path) for path in sorted(day.glob("*.parquet")))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def latest_chain(currency: str = "BTC", *, root: Path | str = CHAIN_ROOT) -> pd.DataFrame:
    """The most recent stored snapshot of `currency` (empty if none)."""
    folder = Path(root) / currency
    files = sorted(folder.glob("*/*.parquet")) if folder.exists() else []
    return pd.read_parquet(files[-1]) if files else pd.DataFrame()


async def record_chains(currencies: Iterable[str] = ("BTC", "ETH", "USDC"), *, every_seconds: float = 3600.0, root: Path | str = CHAIN_ROOT,
                        iterations: int = 0, stop: asyncio.Event | None = None, retry_seconds: float = 60.0) -> int:
    """Snapshot each currency every `every_seconds` until stopped (or `iterations` rounds); returns snapshots saved.

    The request runs in a worker thread. A currency that fails (e.g. the network is down) is logged and retried every
    `retry_seconds` until it succeeds or the next round is due, so a short outage costs minutes, not a whole round.
    Nothing stops the recorder; rounds stay on their original schedule.
    """
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    saved = rounds = 0

    async def wait(seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    while not stop.is_set() and (iterations == 0 or rounds < iterations):
        next_round = loop.time() + every_seconds
        pending = list(currencies)
        while pending and not stop.is_set():
            failed = []
            for currency in pending:
                try:
                    chain = await asyncio.to_thread(fetch_chain, currency)
                    if not chain.empty:
                        path = save_chain(chain, root)
                        saved += 1
                        logger.info("option_chain_saved currency={} contracts={} path={}", currency, len(chain), path)
                except Exception as exc:  # noqa: BLE001 - the recorder must outlive a bad request
                    logger.warning("option_chain_failed currency={} error={!r}", currency, exc)
                    failed.append(currency)
            pending = failed
            if pending and loop.time() + retry_seconds < next_round:
                await wait(retry_seconds)
            else:
                break
        rounds += 1
        if iterations and rounds >= iterations:
            break
        await wait(next_round - loop.time())
    return saved
