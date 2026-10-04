"""Basket sleeves: one strategy that sets weights on many instruments at once (the cross-sectional taker-buy book).

A trend sleeve holds one instrument. A basket ranks a universe of coins every day and holds the top and bottom of
the ranking, long and short (research log 2026-09-26, "Diversifying the trend book"). In the portfolio it appears
as one *member sleeve* per coin (`<basket>__<coin>`, e.g. `taker__sol`) whose weight the basket sets; everything
downstream (netting, the risk overlay, lot rounding, orders, the book's attribution, restarts) works per member as
for any sleeve, and allocation treats the basket as one sleeve.

The signal and universe come from Binance's daily perp candles (the deepest market, public, no keys): research uses
the monthly archive (`binance_panel`), the runtime Binance's REST API (`fetch_binance_panel`). The orders go to the
configured venue's perps (Kraken Futures). Everything at decision time T (a UTC midnight) is computed from days that
closed by T, and the weights change only on rebalance days: days since 1970-01-01 divisible by `rebalance_days`, a
calendar rule that needs no state, so research and a restarted runtime agree on the schedule.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from src.research.portfolio import liquid_universe, rank_weights

Panel = dict[str, pd.DataFrame]  # field -> (day x coin); days are UTC midnights of each candle's open
PANEL_FIELDS = ("close", "quote_volume", "taker_buy_quote_volume")
_SLUG = re.compile(r"^[a-z0-9_]+$")


def taker_buy_share_7d(panel: Panel) -> pd.DataFrame:
    """The share of the last 7 days' volume that came from aggressive buyers, minus one half (study: cross_sectional)."""
    volume = panel["quote_volume"]
    return panel["taker_buy_quote_volume"].rolling(7, min_periods=5).sum() / volume.rolling(7, min_periods=5).sum() - 0.5


SIGNALS: dict[str, Callable[[Panel], pd.DataFrame]] = {"taker_buy_share_7d": taker_buy_share_7d}


@dataclass(frozen=True, slots=True)
class BasketSpec:
    """One `[[baskets]]` block of a portfolio config.

    Attributes:
        id: Slug; members are `<id>__<coin>`.
        coins: Candidate coins (base assets, e.g. "SOL"); each needs a perp in [instruments] on `venue`.
        signal: A name from `SIGNALS`. High scores are held long, low scores short.
        top_n: Each day's universe: the `top_n` candidates by 30-day average Binance volume (listed 60+ days).
        quantile: Share of the universe in each leg (0.2: long the top fifth, short the bottom fifth).
        gross: Total exposure of the basket at full size, split evenly between the legs.
        min_names: Fewer eligible coins than this and the basket stays flat.
        rebalance_days: Weights change only on days where days-since-epoch % rebalance_days == 0.
        long_only: Hold only the long leg (at full `gross`).
        budget: The basket's share under `fixed` allocation (it counts as one sleeve).
        venue: Where members trade; their instruments are "<venue>:<COIN>/USD".
    """

    id: str
    coins: tuple[str, ...]
    signal: str = "taker_buy_share_7d"
    top_n: int = 30
    quantile: float = 0.2
    gross: float = 1.0
    min_names: int = 12
    rebalance_days: int = 10
    long_only: bool = False
    budget: float = 1.0
    venue: str = "kraken_futures"
    enabled: bool = True
    interval: str = "1d"
    history_days: int = 120  # days of candles the runtime fetches: 60 of listing age + 30 of volume average + margin

    def __post_init__(self) -> None:
        """Reject settings the ranking can't work with."""
        problems = []
        if not _SLUG.match(self.id):
            problems.append("id must be lowercase letters, digits and underscores")
        if self.signal not in SIGNALS:
            problems.append(f"signal must be one of {sorted(SIGNALS)}")
        if len(self.coins) < 2 or len(set(self.coins)) != len(self.coins):
            problems.append("coins must list at least two different coins")
        if not 0 < self.quantile <= 0.5:
            problems.append("quantile must be above 0 and at most 0.5")
        if self.top_n < 2 or self.min_names < 2 or self.rebalance_days < 1 or self.history_days < 95:
            problems.append("top_n and min_names must be at least 2, rebalance_days at least 1, history_days at least 95")
        if not 0 < self.gross <= 3:
            problems.append("gross must be above 0 and at most 3")
        if self.interval != "1d":
            problems.append("interval must be 1d (the signal is built from daily candles)")
        if problems:
            raise ValueError("; ".join(problems))

    def member_id(self, coin: str) -> str:
        """The member sleeve for `coin`."""
        return f"{self.id}__{coin.lower()}"

    def instrument(self, coin: str) -> str:
        """The instrument a member trades."""
        return f"{self.venue}:{coin}/USD"

    def members(self) -> dict[str, tuple[str, str]]:
        """member sleeve id -> (coin, instrument)."""
        return {self.member_id(coin): (coin, self.instrument(coin)) for coin in self.coins}


def is_rebalance_day(day: pd.Timestamp | datetime, every: int) -> bool:
    """Whether `day` (a UTC date) is on the basket's calendar schedule."""
    stamp = pd.Timestamp(day)
    stamp = stamp.tz_convert("UTC") if stamp.tzinfo else stamp.tz_localize("UTC")
    return (stamp.normalize() - pd.Timestamp("1970-01-01", tz="UTC")).days % every == 0


def basket_weights(spec: BasketSpec, panel: Panel, *, tradable: pd.DataFrame | None = None) -> pd.DataFrame:
    """Target weights per decision time (the close of each day, i.e. the next UTC midnight) and coin, held between rebalances.

    Args:
        panel: Daily Binance fields per coin, indexed by each candle's open day.
        tradable: Optional day x coin mask of coins that can be traded that day (e.g. listed on Kraken); others are
            left out of the universe, so the legs stay balanced.
    """
    coins = [coin for coin in spec.coins if coin in panel["close"].columns]
    frames = {name: panel[name].reindex(columns=coins) for name in PANEL_FIELDS}
    score = SIGNALS[spec.signal](frames)
    universe = liquid_universe(frames["quote_volume"], top_n=spec.top_n)
    if tradable is not None:
        universe &= tradable.reindex(index=universe.index, columns=universe.columns).fillna(False).astype(bool)
    raw = rank_weights(score, universe, quantile=spec.quantile, gross=spec.gross, long_only=spec.long_only, min_names=spec.min_names)
    rebalance = pd.Series([is_rebalance_day(day + pd.Timedelta(days=1), spec.rebalance_days) for day in raw.index], index=raw.index)
    held = raw.where(rebalance, np.nan)
    # before the first rebalance day with a ranking there is nothing to hold
    held = held.ffill().fillna(0.0)
    held.index = held.index + pd.Timedelta(days=1)  # decided when the day closes
    return held.reindex(columns=list(spec.coins), fill_value=0.0)


def weights_at(spec: BasketSpec, panel: Panel, decision: pd.Timestamp | datetime) -> dict[str, float] | None:
    """The basket's weights decided at `decision` (a UTC midnight), from days closed by then; None off-schedule."""
    stamp = pd.Timestamp(decision)
    stamp = stamp.tz_convert("UTC") if stamp.tzinfo else stamp.tz_localize("UTC")
    if not is_rebalance_day(stamp, spec.rebalance_days):
        return None
    closed = {name: frame[frame.index + pd.Timedelta(days=1) <= stamp] for name, frame in panel.items()}
    weights = basket_weights(spec, closed)
    if stamp not in weights.index:
        return None
    return {coin: float(value) for coin, value in weights.loc[stamp].items()}


# --- data: the Binance archive (research) and Binance's REST API (runtime) ---------------------------------------------

def binance_symbol_map(coins: Sequence[str], symbols: Sequence[str]) -> dict[str, str]:
    """coin -> the Binance USDT perp symbol trading it (e.g. PEPE -> 1000PEPEUSDT), from a list of symbols."""
    from src.data.binance_archive import base_asset

    by_base: dict[str, str] = {}
    for symbol in sorted(symbols, key=len):
        if symbol.endswith("USDT"):
            by_base.setdefault(base_asset(symbol), symbol)
    return {coin: by_base[coin] for coin in coins if coin in by_base}


def binance_panel(coins: Sequence[str]) -> Panel:
    """The research panel from Binance's monthly archive (cached; the frozen holdout is cut off by the loader)."""
    from src.data.binance_archive import load_panel

    klines = load_panel("klines_1d")
    mapping = binance_symbol_map(coins, klines["symbol"].unique())
    reverse = {symbol: coin for coin, symbol in mapping.items()}
    klines = klines[klines["symbol"].isin(reverse)].assign(coin=lambda frame: frame["symbol"].map(reverse))
    return {name: klines.pivot_table(index="date", columns="coin", values=name).sort_index() for name in PANEL_FIELDS}


def _binance_klines(symbol: str, days: int) -> list[list[Any]]:
    query = urllib.parse.urlencode({"symbol": symbol, "interval": "1d", "limit": min(days + 1, 1500)})
    request = urllib.request.Request(f"https://fapi.binance.com/fapi/v1/klines?{query}", headers={"User-Agent": "QuantMultiStrat/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_binance_panel(coins: Sequence[str], *, days: int = 120, now: datetime | None = None,
                        fetch: Callable[[str, int], list[list[Any]]] = _binance_klines,
                        symbols: Mapping[str, str] | None = None) -> Panel:
    """The last `days` completed daily candles per coin from Binance's public futures API (runtime; blocking).

    A coin whose request fails is left out (its column is missing), so one bad symbol can't stop the basket.
    """
    now = now or datetime.now(timezone.utc)
    mapping = dict(symbols) if symbols is not None else {coin: f"{coin}USDT" for coin in coins}
    rows = []
    for coin in coins:
        symbol = mapping.get(coin)
        if symbol is None:
            continue
        try:
            candles = fetch(symbol, days)
        except Exception:  # noqa: BLE001 - reported by the caller as a missing coin
            continue
        for candle in candles:
            opened = datetime.fromtimestamp(int(candle[0]) / 1000, tz=timezone.utc)
            if opened + timedelta(days=1) <= now:  # completed candles only
                rows.append({"date": pd.Timestamp(opened).floor("D"), "coin": coin, "close": float(candle[4]), "quote_volume": float(candle[7]),
                             "taker_buy_quote_volume": float(candle[10])})
    if not rows:
        return {name: pd.DataFrame() for name in PANEL_FIELDS}
    frame = pd.DataFrame(rows)
    return {name: frame.pivot_table(index="date", columns="coin", values=name).sort_index() for name in PANEL_FIELDS}


@dataclass(slots=True)
class CachedPanelSource:
    """The runtime's basket data: one Binance fetch per basket and UTC day, reused by every cycle that day."""

    fetch: Callable[..., Panel] = fetch_binance_panel
    _cache: dict[tuple[str, str], Panel] = field(default_factory=dict)

    def __call__(self, spec: BasketSpec, now: datetime) -> Panel:
        """The panel for `spec` with every day closed by `now`."""
        key = (spec.id, f"{now:%Y-%m-%d}")
        if key not in self._cache:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != spec.id}
            self._cache[key] = self.fetch(list(spec.coins), days=spec.history_days, now=now)
        return self._cache[key]
