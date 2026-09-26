"""Daily bars and funding for every Binance USD-M perpetual, delisted ones included, from Binance's public archive.

Cross-sectional research (ranking many coins against each other) needs many
coins over many years. Using only coins listed today would leave out the ones
that collapsed and were delisted (LUNA, FTT, ...), which flatters any strategy
that buys recent winners or sells recent losers. data.binance.vision keeps
monthly files for every USDT-margined perpetual that ever traded, so this
module downloads those:

- daily klines: OHLC, base and quote volume, trade count, taker-buy volume;
- funding: every settlement, summed per holding day;
- premium index (`premium_1d`, fetched on request): the perp's daily premium
  over spot, for carry research.

No API keys. Files are cached per symbol as parquet under
data/historical_cache/binance_um/ and topped up with newly published months.
Monthly files cover complete months only, so the data ends at the last full
month.

`load_panel` returns one long frame (date, symbol, ...) for all cached symbols.
Binance prices stand in for the venue we would trade on. Its perps are the
deepest market, and Kraken's track them closely.
"""

from __future__ import annotations

import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from loguru import logger

ARCHIVE_URL = "https://data.binance.vision/"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CACHE_DIR = Path("data/historical_cache/binance_um")
USER_AGENT = {"User-Agent": "CryptoQuantMFT/0.1"}
DATASETS = {
    "klines_1d": "data/futures/um/monthly/klines/{symbol}/1d/",
    "funding": "data/futures/um/monthly/fundingRate/{symbol}/",
    # Daily candles of the perp's premium over its spot index, (perp - index) / index: the basis a
    # long-spot / short-perp carry position is exposed to. The `close` column holds the premium.
    "premium_1d": "data/futures/um/monthly/premiumIndexKlines/{symbol}/1d/",
}
KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
# Not single coins: stablecoins, and Binance's composite index contracts.
EXCLUDED_SYMBOLS = frozenset({
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "USDEUSDT", "DAIUSDT", "EURUSDT",
    "BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT",
})
_MONTH = re.compile(r"-(\d{4}-\d{2})\.zip$")


def _fetch(url: str, *, retries: int = 4) -> bytes | None:
    """GET with retries; None when the file doesn't exist."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=USER_AGENT), timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))
    return None


def _list(prefix: str, *, tag: str) -> list[str]:
    """All keys (tag="Key") or sub-folders (tag="Prefix") under `prefix` in the archive's S3 listing, across pages."""
    found: list[str] = []
    marker = ""
    while True:
        url = f"{LISTING_URL}?delimiter=/&prefix={urllib.parse.quote(prefix)}" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
        xml = (_fetch(url) or b"").decode()
        page = re.findall(rf"<{tag}>([^<]+)</{tag}>", xml)
        page = [item for item in page if item != prefix]
        found += page
        if "<IsTruncated>true</IsTruncated>" not in xml or not page:
            return found
        marker = re.findall(r"<(?:Key|Prefix)>([^<]+)</(?:Key|Prefix)>", xml)[-1]


def list_symbols() -> list[str]:
    """Every USDT-margined perpetual with daily klines in the archive, current and delisted, minus non-coins."""
    prefixes = _list("data/futures/um/monthly/klines/", tag="Prefix")
    symbols = [prefix.rstrip("/").rsplit("/", 1)[-1] for prefix in prefixes]
    return sorted(symbol for symbol in symbols if symbol.endswith("USDT") and symbol not in EXCLUDED_SYMBOLS)


def _read_zip_csv(payload: bytes, columns: list[str] | None) -> pd.DataFrame:
    """One archive file. Older files have no header row, newer ones do."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        raw = archive.open(archive.namelist()[0]).read()
    first = raw.split(b"\n", 1)[0]
    has_header = not first[:1].isdigit()
    frame = pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)
    if columns is not None:
        frame.columns = columns[: len(frame.columns)]
    return frame


def parse_klines(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily klines to (date, open, high, low, close, volume, quote_volume, trades, taker_buy_quote_volume)."""
    opened = pd.to_numeric(frame["open_time"], errors="coerce")
    unit = "us" if opened.max() > 1e14 else "ms"  # Binance switched some archives to microseconds in 2025
    out = pd.DataFrame({"date": pd.to_datetime(opened, unit=unit, utc=True).dt.floor("D")})
    for column in ("open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_quote_volume"):
        out[column] = pd.to_numeric(frame[column], errors="coerce")
    return out.dropna(subset=["date", "close"])


def parse_funding(frame: pd.DataFrame) -> pd.DataFrame:
    """Funding settlements to (date, funding): the sum of rates settled while a position was held over that day.

    A daily bar runs from 00:00 to 24:00 UTC, and a position held over it pays
    the settlements after 00:00 up to and including 24:00. So a settlement at
    exactly midnight belongs to the day that just ended.
    """
    settled = pd.to_numeric(frame["calc_time"], errors="coerce")
    unit = "us" if settled.max() > 1e14 else "ms"
    times = pd.to_datetime(settled, unit=unit, utc=True)
    days = (times - pd.Timedelta(milliseconds=1)).dt.floor("D")
    rates = pd.to_numeric(frame["last_funding_rate"], errors="coerce")
    grouped = pd.DataFrame({"date": days, "funding": rates}).dropna().groupby("date", as_index=False)["funding"].sum()
    return grouped


def _cache_path(dataset: str, symbol: str, cache_dir: Path) -> Path:
    return cache_dir / dataset / f"{symbol}.parquet"


def update_cache(
    symbols: list[str] | None = None,
    *,
    datasets: tuple[str, ...] = ("klines_1d", "funding"),
    cache_dir: Path | str = CACHE_DIR,
    workers: int = 16,
    batch_size: int = 40,
) -> dict[str, int]:
    """Download the monthly files each symbol's cache is missing; returns how many files were fetched per dataset.

    Symbols are processed in batches of `batch_size`, and each batch is saved
    before the next starts, so an interrupted download keeps what it has
    fetched. Downloads within a batch share one thread pool. Only months
    missing from a symbol's cache are fetched, so re-running resumes, tops
    the cache up and retries any file that failed.
    """
    cache_dir = Path(cache_dir)
    symbols = symbols if symbols is not None else list_symbols()
    fetched: dict[str, int] = {}
    for dataset in datasets:
        parser, columns = (parse_funding, None) if dataset == "funding" else (parse_klines, KLINE_COLUMNS)

        def download(task: tuple[str, str]) -> tuple[str, pd.DataFrame | None]:
            try:
                payload = _fetch(ARCHIVE_URL + urllib.parse.quote(task[1]))
                return task[0], None if payload is None else parser(_read_zip_csv(payload, columns))
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop a 50,000-file download
                logger.warning("binance archive: skipped {} ({!r}); re-run to retry", task[1], exc)
                return task[0], None

        fetched[dataset] = 0
        for first in range(0, len(symbols), batch_size):
            batch = symbols[first : first + batch_size]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                listings = dict(zip(batch, pool.map(lambda symbol: _list(DATASETS[dataset].format(symbol=symbol), tag="Key"), batch)))
            tasks: list[tuple[str, str]] = []
            cached: dict[str, pd.DataFrame] = {}
            for symbol, keys in listings.items():
                path = _cache_path(dataset, symbol, cache_dir)
                have: set[str] = set()
                if path.exists():
                    cached[symbol] = pd.read_parquet(path)
                    have = set(cached[symbol]["date"].dt.strftime("%Y-%m"))
                tasks += [(symbol, key) for key in keys if (match := _MONTH.search(key)) and match.group(1) not in have]
            new: dict[str, list[pd.DataFrame]] = defaultdict(list)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for symbol, frame in pool.map(download, tasks):
                    if frame is not None and not frame.empty:
                        new[symbol].append(frame)
            for symbol, frames in new.items():
                combined = pd.concat([cached.get(symbol, pd.DataFrame()), *frames], ignore_index=True)
                combined = combined.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
                path = _cache_path(dataset, symbol, cache_dir)
                path.parent.mkdir(parents=True, exist_ok=True)
                combined.to_parquet(path, index=False)
            fetched[dataset] += len(tasks)
            logger.info("binance archive {}: {}/{} symbols done, {} files fetched so far", dataset, min(first + batch_size, len(symbols)), len(symbols), fetched[dataset])
    return fetched


def load_panel(dataset: str = "klines_1d", *, cache_dir: Path | str = CACHE_DIR) -> pd.DataFrame:
    """Every cached symbol's rows in one long frame with a `symbol` column, sorted by date then symbol."""
    folder = Path(cache_dir) / dataset
    frames = [pd.read_parquet(path).assign(symbol=path.stem) for path in sorted(folder.glob("*.parquet"))]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)


def base_asset(symbol: str) -> str:
    """The coin behind a Binance symbol, without the size prefix some small-priced coins carry (1000PEPEUSDT -> PEPE)."""
    base = symbol.removesuffix("USDT")
    for prefix in ("1000000", "1000", "1M"):
        if base.startswith(prefix) and len(base) > len(prefix) and not base[len(prefix)].isdigit():
            return base[len(prefix):]
    return base


def kraken_perp_bases() -> set[str]:
    """Coins with a tradeable Kraken Futures linear perpetual today (PF_XBTUSD counts as BTC)."""
    from src.data.kraken_futures import fetch_instruments

    bases = set()
    for instrument in fetch_instruments():
        symbol = str(instrument.get("symbol", ""))
        if symbol.startswith("PF_") and symbol.endswith("USD") and instrument.get("tradeable", True):
            base = symbol[3:-3]
            bases.add("BTC" if base == "XBT" else base)
            bases.add(base_asset(base + "USDT"))
    return bases
