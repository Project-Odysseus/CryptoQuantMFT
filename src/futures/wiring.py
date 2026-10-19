"""Put the futures pieces together for one portfolio config: the contract board, the feed and one ledger per venue."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time
from pathlib import Path
from typing import Any, Callable

from src.futures.adapter import ContractBoard, Prices, SessionFuturesAdapter
from src.futures.contracts import Market, load_markets
from src.futures.feed import FuturesFeed
from src.futures.liquidity import LiquidityRules
from src.futures.quotes import DailyStore
from src.futures.sessions import Sessions, parse_slots

MOCK_RATES = {"USD": 10.0, "EUR": 11.5, "JPY": 0.065, "CHF": 12.5}  # NOK per unit, for mock runs only


@dataclass(slots=True)
class FuturesParts:
    """What a futures book needs beyond the engine.

    Attributes:
        markets: The config's futures instruments, from the contract table.
        board: Which contract month each market is in; shared by the feed and the ledgers.
        feed: Daily bars and currency rates.
        ledger: Builds the ledger for one venue (`build_paper_adapters` calls it).
    """

    markets: dict[str, Market]
    sessions: Sessions
    board: ContractBoard
    feed: FuturesFeed
    ledger: Callable[[str, list[Any]], SessionFuturesAdapter]


def is_futures_book(config: Any) -> bool:
    """Whether the config holds listed futures."""
    return any(spec.kind == "future" for spec in config.instruments.values())


def build(config: Any, state_dir: Path | str, *, prices: Prices | None = None, store: DailyStore | None = None, fetch: Any = None,
          clock: Any = None) -> FuturesParts:
    """The parts for `config`, keeping their state under `state_dir`. `prices` and `store` are left out in mock runs,
    which then fill at the engine's own price and apply no volume rules."""
    from src.portfolio.book import VENUE_CURRENCY

    state_dir = Path(state_dir)
    table = load_markets()
    markets = {instrument: table[instrument] for instrument, spec in config.instruments.items() if spec.kind == "future"}
    for instrument in config.futures.around_the_clock:  # CME Globex: 18:00 the evening before to 17:00, New York time
        markets[instrument] = replace(markets[instrument], liquid_from=time(18, 0), liquid_to=time(17, 0), timezone="America/New_York", anytime=True)
    sessions = Sessions()
    board = ContractBoard(markets, sessions, state_dir / "futures_contracts.json")
    settings = config.futures
    currencies = {VENUE_CURRENCY.get(spec.venue, config.base_currency) for spec in config.instruments.values()}
    feed = FuturesFeed(config.instruments, markets, board, sessions, base_currency=config.base_currency, currencies=currencies, fetch=fetch, clock=clock)
    rules = LiquidityRules(max_participation=settings.max_participation, min_daily_volume=settings.min_daily_volume)

    def within(path: str) -> Path:
        return Path(path) if Path(path).is_absolute() else state_dir / path

    def ledger(venue: str, specs: list[Any]) -> SessionFuturesAdapter:
        return SessionFuturesAdapter(
            venue=venue, markets={spec.symbol: markets[spec.id] for spec in specs}, board=board, sessions=sessions, slots=parse_slots(settings.slots),
            rules=rules, mode=settings.mode, book_name=config.name, prices=prices, store=store, state_path=state_dir / f"futures_{venue}.json",
            order_dir=within(settings.order_dir), fills_dir=within(settings.fills_dir), max_contracts=settings.max_contracts, no_trade=settings.no_trade)

    return FuturesParts(markets=markets, sessions=sessions, board=board, feed=feed, ledger=ledger)
