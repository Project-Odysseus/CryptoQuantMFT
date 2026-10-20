"""Futures done properly: contract months, last trading days, rolls, held-contract returns, adjusted series (plan 1.4).

Two series come out of individual contracts, for two different jobs:

- **P&L: `held_returns`.** Each day's return is the return of the contract actually held over that day, from its own
  closes. A roll is a trade at a close (out of the old contract, into the next), with its cost. The price gap between
  two contracts never appears as a return.
- **Signals: `adjusted_series`.** One continuous price for features, back-adjusted by the difference (additive) or
  the ratio (multiplicative) at each roll, so a roll is not a fake move. Signals only: its level is not a tradable
  price, and its returns are not P&L.

A position decided at the close of day t is in the contract the schedule names for t. The schedule rolls at the close
of the roll day: the old contract earns that day, the new one from the next.

Contract rules follow `ContractSpec` (quantlab.data.instruments). Calendars come from quantlab.calendars.
"""

from __future__ import annotations

import calendar as month_calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Mapping

import numpy as np
import pandas as pd

from quantlab.calendars import TradingCalendar, on_or_before, shift_sessions
from quantlab.data.instruments import ContractSpec

MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M", 7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
MONTH_OF_CODE = {code: month for month, code in MONTH_CODES.items()}


@dataclass(frozen=True)
class Contract:
    """One contract month."""

    spec: ContractSpec
    year: int
    month: int

    @property
    def code(self) -> str:
        """Exchange-style code with a two-digit year, e.g. "NQZ26"."""
        return f"{self.spec.root}{MONTH_CODES[self.month]}{self.year % 100:02d}"

    def following(self) -> "Contract":
        """The next listed contract month."""
        months = sorted(MONTH_OF_CODE[code] for code in self.spec.months)
        later = [month for month in months if month > self.month]
        return Contract(self.spec, self.year, later[0]) if later else Contract(self.spec, self.year + 1, months[0])


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def last_trade_day(contract: Contract, calendar: TradingCalendar) -> date:
    """The contract's last trading day, moved back to a session when the rule lands on a holiday."""
    rule, year, month = contract.spec.last_trade_rule, contract.year, contract.month
    if rule == "third_friday":
        return on_or_before(calendar, _nth_weekday(year, month, month_calendar.FRIDAY, 3))
    if rule == "thursday_before_second_friday":
        return on_or_before(calendar, _nth_weekday(year, month, month_calendar.FRIDAY, 2) - timedelta(days=1))
    if rule == "last_session_of_month":
        return on_or_before(calendar, date(year, month, month_calendar.monthrange(year, month)[1]))
    if rule in ("first_notice_prior_month", "crude_oil"):
        # delivered contracts are left by their first notice day (the last session of the month before); WTI stops
        # three sessions before the 25th of the month before, four when the 25th is not a session
        prior_year, prior_month = (year, month - 1) if month > 1 else (year - 1, 12)
        if rule == "first_notice_prior_month":
            return on_or_before(calendar, date(prior_year, prior_month, month_calendar.monthrange(prior_year, prior_month)[1]))
        anchor = date(prior_year, prior_month, 25)
        return shift_sessions(calendar, anchor if calendar.is_session(anchor) else on_or_before(calendar, anchor), -3)
    third_wednesday = _nth_weekday(year, month, month_calendar.WEDNESDAY, 3)
    if rule == "two_sessions_before_third_wednesday":
        return shift_sessions(calendar, third_wednesday, -2)
    if rule == "one_session_before_third_wednesday":
        return shift_sessions(calendar, third_wednesday, -1)
    raise ValueError(f"unknown last_trade_rule {rule!r}")


def roll_day(contract: Contract, calendar: TradingCalendar) -> date:
    """The session at whose close a position leaves this contract for the next."""
    return shift_sessions(calendar, last_trade_day(contract, calendar), -contract.spec.roll_sessions_before)


def held_contract(spec: ContractSpec, day: date, calendar: TradingCalendar) -> Contract:
    """The contract held at the close of `day`: the nearest one whose roll day is after `day`."""
    months = sorted(MONTH_OF_CODE[code] for code in spec.months)
    contract = Contract(spec, day.year, next((month for month in months if month >= day.month), months[0]))
    if contract.month < day.month:
        contract = Contract(spec, day.year + 1, contract.month)
    while roll_day(contract, calendar) <= day:
        contract = contract.following()
    return contract


def roll_schedule(spec: ContractSpec, calendar: TradingCalendar, start: str | date, end: str | date) -> pd.DataFrame:
    """Per session: the contract held after its close (`contract`) and whether the position rolled at that close
    (`roll`: the held contract differs from the previous session's)."""
    sessions = calendar.sessions(start, end)
    held = [held_contract(spec, day.date(), calendar).code for day in sessions]
    schedule = pd.DataFrame({"contract": held}, index=sessions)
    schedule["roll"] = schedule["contract"].ne(schedule["contract"].shift(1)) & schedule["contract"].shift(1).notna()
    schedule.index.name = "time"
    return schedule


def held_returns(closes: Mapping[str, pd.Series], schedule: pd.DataFrame, *, cost_bps: float = 0.0) -> pd.DataFrame:
    """Daily P&L of a one-unit long position in the held contract (simple returns), and the cost of each roll.

    `closes` maps contract codes to their daily closes. The return on day t is that of the contract held at the close
    of t-1, from its own close at t-1 to its close at t. Rolling at the close of t costs one round trip (`cost_bps`
    per side, both legs) as a fraction of notional. A missing close for the held contract leaves the day's return NaN.
    """
    previous = schedule["contract"].shift(1)
    returns = pd.Series(np.nan, index=schedule.index)
    for code in set(previous.dropna()):
        if code not in closes:
            continue
        own = closes[code].reindex(schedule.index)
        mask = (previous == code).to_numpy()
        returns[mask] = (own / own.shift(1) - 1.0)[mask]
    roll_cost = schedule["roll"].astype(float) * 2.0 * cost_bps / 10_000.0
    return pd.DataFrame({"held": previous, "return": returns, "roll_cost": roll_cost, "net_return": returns - roll_cost})


def adjusted_series(closes: Mapping[str, pd.Series], schedule: pd.DataFrame, *, method: Literal["difference", "ratio"] = "ratio") -> pd.Series:
    """One continuous price for signals: the held contract's close, back-adjusted at every roll so rolls are not moves.

    Ends at the actual price of the last held contract. Earlier history is shifted by the gap between the new and the
    old contract at each roll close (`difference`), or scaled by their ratio (`ratio`, which keeps percentage moves and
    stays positive).
    """
    raw = pd.Series(np.nan, index=schedule.index)
    for code in set(schedule["contract"]):
        if code in closes:
            mask = (schedule["contract"] == code).to_numpy()
            raw[mask] = closes[code].reindex(schedule.index)[mask]
    adjusted = raw.copy()
    rolls = schedule.index[schedule["roll"].to_numpy()]
    for day in rolls:  # at the roll close both contracts trade: the gap between them there is what is removed
        position = schedule.index.get_loc(day)
        old, new = schedule["contract"].iloc[position - 1], schedule["contract"].iloc[position]
        if old not in closes or new not in closes:
            continue
        old_price, new_price = closes[old].get(day, np.nan), closes[new].get(day, np.nan)
        if not (np.isfinite(old_price) and np.isfinite(new_price)):
            continue
        earlier = adjusted.index < day
        if method == "ratio":
            adjusted[earlier] = adjusted[earlier] * (new_price / old_price)
        else:
            adjusted[earlier] = adjusted[earlier] + (new_price - old_price)
    adjusted.name = "adjusted_close"
    return adjusted
