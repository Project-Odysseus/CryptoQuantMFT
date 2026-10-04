"""Contract strategies: sleeves that hold specific contracts (prediction-market bets; options later) under a loss budget.

A weight sleeve says "40% of equity in BTC" and the engine works out the trade. That doesn't fit a contract that
expires, is sized in contracts and has a known worst loss. A contract strategy instead returns the positions it wants
in named contracts, each with a limit price and whether to take liquidity or rest:

    class MyStrategy:
        def targets(self, context: ContractContext) -> list[ContractTarget]: ...

The engine calls it every cycle, keeps its positions apart from every other sleeve's, cuts its targets down to its
budget (the money it may lose if every bet goes wrong), and sends the difference between target and held position as
orders through the venue's adapter, with the same write-ahead and restart handling as every other order.

A "maker" order that has rested for the config's `maker_timeout_seconds` is withdrawn and never sent to market; if
the strategy still names the target, a fresh order is placed at its limit price of that moment.

A contract held and missing from the targets is closed (target 0), so a strategy that wants to keep a position must
keep naming it. Bets are held to resolution by returning the same target until the market settles.

Strategies are registered by name (`register`) and chosen in a config's `[[contract_sleeves]]`. None ships here: a
strategy goes in the registry when it has passed its pre-registered test.
"""

from __future__ import annotations

import csv
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

STYLES = ("taker", "maker")


@dataclass(frozen=True, slots=True)
class ContractTarget:
    """A wanted position in one contract.

    Attributes:
        instrument: "<venue>:<market id>", e.g. "kalshi:KXBTCD-26OCT0416-T85399.99".
        units: Signed Yes contracts wanted in total (not the change): positive is Yes, negative is No, 0 closes.
        limit_price: The worst Yes price to trade at (a buy pays at most this; a sell, which is how No is bought,
            receives at least this).
        style: "taker" fills what the book offers inside the limit now and cancels the rest; "maker" rests at the
            limit until filled or until the engine's maker timeout cancels it.
        reason: For the log and the trade alert.
    """

    instrument: str
    units: float
    limit_price: float
    style: str = "taker"
    reason: str = ""

    def __post_init__(self) -> None:
        if ":" not in self.instrument:
            raise ValueError(f"an instrument is '<venue>:<market id>', not {self.instrument!r}")
        if self.style not in STYLES:
            raise ValueError(f"style must be one of {STYLES}, not {self.style!r}")
        if self.units and not 0.0 < self.limit_price < 1.0:
            raise ValueError(f"a binary's limit price is between 0 and 1 (got {self.limit_price})")


@dataclass(slots=True)
class ContractContext:
    """What a strategy may look at when it decides."""

    now: datetime
    equity: float  # the whole book's, in its base currency
    budget: float  # the money this sleeve may have at risk
    held: Mapping[str, float]  # this sleeve's positions: instrument -> signed Yes contracts
    books: Callable[[str], pd.DataFrame | None]  # instrument -> its order book now (side, price, size; best first), or None
    marks: Mapping[str, float] = field(default_factory=dict)  # the book's marks (coin prices from the perp instruments, and held contracts)


class ContractStrategy(Protocol):
    """What the engine needs from a contract strategy."""

    def targets(self, context: ContractContext) -> Sequence[ContractTarget]:
        """The positions wanted now."""


REGISTRY: dict[str, Callable[..., ContractStrategy]] = {}


def register(name: str, factory: Callable[..., ContractStrategy]) -> None:
    """Make a strategy selectable as `strategy = "<name>"` in `[[contract_sleeves]]`; its `params` table is passed to `factory`."""
    REGISTRY[name] = factory


def build(name: str, params: Mapping[str, Any]) -> ContractStrategy:
    """The strategy registered under `name`, built with `params`."""
    if name not in REGISTRY:
        raise KeyError(f"no contract strategy named '{name}'; registered: {sorted(REGISTRY) or 'none yet'}")
    return REGISTRY[name](**dict(params))


def at_risk(units: float, price: float) -> float:
    """The most a binary position can lose: what it costs. Yes costs its price; No costs 1 minus the Yes price."""
    return units * price if units >= 0 else -units * (1.0 - price)


def fit_to_budget(targets: Sequence[ContractTarget], budget: float) -> list[ContractTarget]:
    """`targets` scaled down, all by the same factor and to whole contracts, so their total cost is within `budget`."""
    total = sum(at_risk(target.units, target.limit_price) for target in targets)
    if total <= budget or total <= 0:
        return [ContractTarget(target.instrument, float(math.trunc(target.units)), target.limit_price, target.style, target.reason) for target in targets]
    scale = max(budget, 0.0) / total
    return [ContractTarget(target.instrument, float(math.trunc(target.units * scale)), target.limit_price, target.style, target.reason) for target in targets]


# --- where a paper book gets a bet's order book and result ----------------------------------------------------------------

class VenueBooks:
    """A market's live order book by instrument id, from the venues' public APIs, kept for a few seconds."""

    def __init__(self, *, kalshi: Any = None, polymarket: Any = None, max_age: float = 20.0, markets_file: str | Path = "data/market_data/prediction_markets/markets.csv") -> None:
        from src.data.kraken_spreads import http_json_gzip
        from src.research import prediction_markets as pm

        self.kalshi = kalshi if kalshi is not None else pm.KalshiClient(fetch=http_json_gzip)
        self.polymarket = polymarket if polymarket is not None else pm.PolymarketClient(fetch=http_json_gzip)
        self.max_age, self.markets_file = max_age, Path(markets_file)
        self._seen: dict[str, tuple[float, pd.DataFrame | None]] = {}
        self._tokens: dict[str, str] = {}

    def _token(self, slug: str) -> str | None:
        """Polymarket's order-book id for a market's Yes outcome, from the recorder's market list."""
        if slug not in self._tokens and self.markets_file.exists():
            with self.markets_file.open(newline="", encoding="utf-8") as handle:
                self._tokens.update({row["market_id"]: row["yes_token"] for row in csv.DictReader(handle) if row["venue"] == "polymarket" and row["yes_token"]})
        return self._tokens.get(slug)

    def __call__(self, instrument: str) -> pd.DataFrame | None:
        """The order book of `instrument` ("kalshi:<ticker>" or "polymarket:<slug>"), or None when it can't be read."""
        known = self._seen.get(instrument)
        if known is not None and time.monotonic() - known[0] <= self.max_age:
            return known[1]
        venue, _, market = instrument.partition(":")
        try:
            if venue == "kalshi":
                book = self.kalshi.order_book(market)
            elif venue == "polymarket":
                token = self._token(market)
                book = self.polymarket.order_book(token) if token else None
            else:
                book = None
        except Exception:  # noqa: BLE001 - "unknown" to the caller
            book = None
        self._seen[instrument] = (time.monotonic(), book)
        return book


class RecordedResults:
    """How a market resolved, from the recorder's result files (`data/market_data/prediction_markets/results/<venue>.csv`)."""

    def __init__(self, root: str | Path = "data/market_data/prediction_markets/results", *, max_age: float = 300.0) -> None:
        self.root, self.max_age = Path(root), max_age
        self._loaded: dict[str, tuple[float, dict[str, bool]]] = {}

    def __call__(self, instrument: str) -> bool | None:
        """True if Yes won, False if not, None while the recorder has no result for it."""
        venue, _, market = instrument.partition(":")
        known = self._loaded.get(venue)
        if known is None or time.monotonic() - known[0] > self.max_age:
            path, results = self.root / f"{venue}.csv", {}
            if path.exists():
                with path.open(newline="", encoding="utf-8") as handle:
                    results = {row["market_id"]: row["yes"].strip().lower() == "true" for row in csv.DictReader(handle)}
            known = (time.monotonic(), results)
            self._loaded[venue] = known
        return known[1].get(market)
