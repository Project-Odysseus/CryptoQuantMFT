"""Futures markets and their contracts: the table, contract months, last trading days and when to roll.

A *market* ("cme_usd:MES") is what strategies, risk and limits see. A *contract* ("MESZ26") is what is held and
traded. The market id's first part is the ledger its profit lands in: one per exchange and currency, because the
book keeps cash per venue in a single currency.
"""

from __future__ import annotations

import calendar as month_calendar
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping

import pandas as pd

from src.futures.sessions import Sessions

MARKETS_PATH = Path("config/futures_markets.csv")
MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M", 7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
MONTH_OF_CODE = {code: month for month, code in MONTH_CODES.items()}
LAST_TRADE_RULES = ("third_friday", "two_sessions_before_third_wednesday", "one_session_before_third_wednesday", "thursday_before_second_friday")


@dataclass(frozen=True, slots=True)
class Market:
    """One row of the contract table.

    Attributes:
        id: "<ledger>:<root>", e.g. "cme_usd:MES". The ledger is the book's venue for this market.
        multiplier: Units of the underlying per contract (5 index points for MES, 12,500 euros for M6E). A price
            times the multiplier is one contract's value in `currency`.
        tick_size, tick_value: The smallest price step and what it is worth per contract.
        settlement: "cash", "physical" (must always be rolled before the last trading day) or "check".
        months: Contract months as month codes, e.g. "HMUZ".
        last_trade_rule: One of `LAST_TRADE_RULES`.
        roll_sessions_before: Trading days before the last trading day on which the position moves to the next contract.
        calendar: The `exchange_calendars` code whose holidays close this market's liquid window.
        liquid_from, liquid_to, timezone: The liquid part of the day, local time.
        history_proxy: The index or currency whose history stands in for expired contracts.
        quote_exchange: The suffix of the free source's contract symbols ("MESZ26.CME"); empty when it has none.
        cost_ticks: Assumed cost per side in ticks: half the spread plus commission.
    """

    id: str
    name: str
    sector: str
    region: str
    exchange: str
    root: str
    multiplier: Decimal
    tick_size: Decimal
    tick_value: Decimal
    currency: str
    settlement: str
    months: str
    last_trade_rule: str
    roll_sessions_before: int
    calendar: str
    liquid_from: time
    liquid_to: time
    timezone: str
    history_proxy: str
    free_quote_symbol: str
    quote_exchange: str
    cost_ticks: Decimal
    spec_status: str = ""

    @property
    def venue(self) -> str:
        """The ledger, e.g. "cme_usd"."""
        return self.id.split(":", 1)[0]

    def contracts_for(self, units: Decimal | float) -> Decimal:
        """Engine units (index points, euros) as a number of contracts."""
        return Decimal(str(units)) / self.multiplier

    def cost(self, contracts: Decimal | float | int) -> Decimal:
        """The assumed cost of trading `contracts` on one side, in the contract's currency."""
        return abs(Decimal(str(contracts))) * self.cost_ticks * self.tick_value

    def slipped(self, price: Decimal | float, side: str) -> Decimal:
        """`price` moved against the trade by the assumed cost in ticks, kept on the tick grid."""
        ticks = (Decimal(str(price)) / self.tick_size).quantize(Decimal(1))
        return (ticks + (self.cost_ticks if side == "buy" else -self.cost_ticks)) * self.tick_size


def _clock(text: str) -> time:
    hour, minute = (int(part) for part in str(text).split(":"))
    return time(hour, minute)


def load_markets(path: Path | str = MARKETS_PATH) -> dict[str, Market]:
    """The contract table by market id. Raises on a row that contradicts itself, so a typo can't size an order."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    markets: dict[str, Market] = {}
    problems: list[str] = []
    for row in frame.to_dict("records"):
        market = Market(id=row["market"], name=row["name"], sector=row["sector"], region=row["region"], exchange=row["exchange"], root=row["root"],
                        multiplier=Decimal(row["multiplier"]), tick_size=Decimal(row["tick_size"]), tick_value=Decimal(row["tick_value"]),
                        currency=row["currency"], settlement=row["settlement"], months=row["months"], last_trade_rule=row["last_trade_rule"],
                        roll_sessions_before=int(row["roll_sessions_before"]), calendar=row["calendar"], liquid_from=_clock(row["liquid_from"]),
                        liquid_to=_clock(row["liquid_to"]), timezone=row["timezone"], history_proxy=row["history_proxy"],
                        free_quote_symbol=row["free_quote_symbol"], quote_exchange=row["quote_exchange"], cost_ticks=Decimal(row["cost_ticks_per_side"]),
                        spec_status=row.get("spec_status", ""))
        if market.multiplier * market.tick_size != market.tick_value:
            problems.append(f"{market.id}: tick value {market.tick_value} is not multiplier {market.multiplier} x tick {market.tick_size}")
        if market.last_trade_rule not in LAST_TRADE_RULES:
            problems.append(f"{market.id}: unknown last_trade_rule {market.last_trade_rule!r}")
        if not market.months or any(code not in MONTH_OF_CODE for code in market.months):
            problems.append(f"{market.id}: months {market.months!r} are not month codes")
        if market.id.split(":", 1)[-1] != market.root or market.id in markets:
            problems.append(f"{market.id}: the id must be '<ledger>:{market.root}' and unique")
        if market.settlement == "physical" and market.roll_sessions_before < 2:
            problems.append(f"{market.id}: a delivered contract must roll at least 2 sessions before its last trading day")
        markets[market.id] = market
    if problems:
        raise ValueError("contract table: " + "; ".join(problems))
    return markets


@dataclass(frozen=True, slots=True)
class Contract:
    """One contract month of a market."""

    market: Market
    year: int
    month: int

    @property
    def code(self) -> str:
        """The exchange code with a two-digit year, e.g. "MESZ26"."""
        return f"{self.market.root}{MONTH_CODES[self.month]}{self.year % 100:02d}"

    @property
    def quote_symbol(self) -> str | None:
        """The free source's symbol ("MESZ26.CME"), or None when it does not carry this market."""
        return f"{self.code}.{self.market.quote_exchange}" if self.market.quote_exchange else None

    def following(self) -> "Contract":
        """The next contract month of the same market."""
        months = sorted(MONTH_OF_CODE[code] for code in self.market.months)
        later = [month for month in months if month > self.month]
        return Contract(self.market, self.year, later[0]) if later else Contract(self.market, self.year + 1, months[0])


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def last_trade_day(contract: Contract, sessions: Sessions) -> date:
    """The last day the contract trades, moved back to a trading day when the rule lands on a holiday."""
    market, rule = contract.market, contract.market.last_trade_rule
    if rule == "third_friday":
        return sessions.on_or_before(market.calendar, _nth_weekday(contract.year, contract.month, month_calendar.FRIDAY, 3))
    if rule == "thursday_before_second_friday":
        return sessions.on_or_before(market.calendar, _nth_weekday(contract.year, contract.month, month_calendar.FRIDAY, 2) - timedelta(days=1))
    third_wednesday = _nth_weekday(contract.year, contract.month, month_calendar.WEDNESDAY, 3)
    if rule == "two_sessions_before_third_wednesday":
        return sessions.shift(market.calendar, third_wednesday, -2)
    if rule == "one_session_before_third_wednesday":
        return sessions.shift(market.calendar, third_wednesday, -1)
    raise ValueError(f"unknown last_trade_rule {rule!r}")


def roll_day(contract: Contract, sessions: Sessions) -> date:
    """The day the position leaves this contract for the next one, at the latest."""
    return sessions.shift(contract.market.calendar, last_trade_day(contract, sessions), -contract.market.roll_sessions_before)


def scheduled_contract(market: Market, day: date, sessions: Sessions) -> Contract:
    """The contract to hold on `day` by the calendar: the nearest one whose roll day has not come yet."""
    months = sorted(MONTH_OF_CODE[code] for code in market.months)
    contract = Contract(market, day.year, next((month for month in months if month >= day.month), months[0]))
    if contract.month < day.month:
        contract = Contract(market, day.year + 1, contract.month)
    while roll_day(contract, sessions) <= day:
        contract = contract.following()
    return contract


def parse_code(market: Market, code: str, *, near: date) -> Contract:
    """The contract for a code like "MESZ26", reading the two-digit year as the one closest to `near`."""
    if not code.startswith(market.root) or len(code) != len(market.root) + 3 or code[len(market.root)] not in MONTH_OF_CODE:
        raise ValueError(f"{code!r} is not a contract of {market.id}")
    century = near.year - near.year % 100
    year = min((century - 100 + int(code[-2:]), century + int(code[-2:]), century + 100 + int(code[-2:])), key=lambda value: abs(value - near.year))
    return Contract(market, year, MONTH_OF_CODE[code[len(market.root)]])


def contract_to_hold(market: Market, day: date, sessions: Sessions, *, current: Contract | None = None, volumes: Mapping[str, float] | None = None) -> Contract:
    """The contract a position should be in on `day`.

    The calendar gives the latest possible roll. The position moves earlier when the next contract already trades
    more than the one held (`volumes`: latest daily volume by contract code), which is when the market itself has
    moved. It never moves back to a nearer contract.
    """
    scheduled = scheduled_contract(market, day, sessions)
    held = current if current is not None and (current.year, current.month) >= (scheduled.year, scheduled.month) else scheduled
    following = held.following()
    if volumes and volumes.get(following.code, 0.0) > volumes.get(held.code, 0.0) > 0.0:
        return following
    return held
