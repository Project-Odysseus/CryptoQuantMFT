"""Trading calendars: which days a market has a session.

Three kinds, behind one small interface (`is_session`, `sessions`):

- `AlwaysCalendar`: every day (crypto).
- `WeekdayCalendar`: Monday to Friday, no holidays (the fallback when exact holidays are unknown).
- `ExchangeCalendar`: an `exchange_calendars` code (XNYS, CMES, XEUR, XTKS, XLON, ...), with holidays. The library is an
  optional extra (`pip install quantlab[calendars]`); without it, or outside its date range, weekdays are used and the
  calendar says so (`exact` is False), so a data card can flag it rather than fail.

`get_calendar(code)` picks the kind: "always", "weekdays", or an exchange code. Calendars are cached.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta
from functools import lru_cache
from typing import Protocol

import pandas as pd

HISTORY_START = "1980-01-01"  # exchange_calendars defaults to 20 years back: too short for research


class TradingCalendar(Protocol):
    """What the lab needs from a calendar."""

    name: str
    exact: bool

    def is_session(self, day: date) -> bool: ...

    def sessions(self, start: date | str, end: date | str) -> pd.DatetimeIndex: ...


class AlwaysCalendar:
    """Every calendar day is a session."""

    name, exact = "always", True

    def is_session(self, day: date) -> bool:
        return True

    def sessions(self, start: date | str, end: date | str) -> pd.DatetimeIndex:
        return pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D")


class WeekdayCalendar:
    """Monday to Friday; holidays unknown."""

    exact = False

    def __init__(self, name: str = "weekdays") -> None:
        self.name = name

    def is_session(self, day: date) -> bool:
        return pd.Timestamp(day).weekday() < 5

    def sessions(self, start: date | str, end: date | str) -> pd.DatetimeIndex:
        return pd.bdate_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize())


class ExchangeCalendar:
    """An exchange_calendars calendar; weekdays outside its range."""

    exact = True

    def __init__(self, code: str) -> None:
        import exchange_calendars

        self.name = code
        self._calendar = exchange_calendars.get_calendar(code, start=HISTORY_START)
        self._first, self._last = self._calendar.first_session, self._calendar.last_session

    def is_session(self, day: date) -> bool:
        stamp = pd.Timestamp(day).normalize()
        if stamp < self._first or stamp > self._last:
            return stamp.weekday() < 5
        return bool(self._calendar.is_session(stamp))

    def sessions(self, start: date | str, end: date | str) -> pd.DatetimeIndex:
        first, last = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        inside = self._calendar.sessions_in_range(max(first, self._first), min(last, self._last)) if first <= self._last and last >= self._first else pd.DatetimeIndex([])
        before = pd.bdate_range(first, min(last, self._first - pd.Timedelta(days=1))) if first < self._first else pd.DatetimeIndex([])
        after = pd.bdate_range(max(first, self._last + pd.Timedelta(days=1)), last) if last > self._last else pd.DatetimeIndex([])
        return pd.DatetimeIndex(before.append(pd.DatetimeIndex(inside).tz_localize(None)).append(after))


@lru_cache(maxsize=None)
def get_calendar(code: str) -> TradingCalendar:
    """The calendar for a code: "always", "weekdays", or an exchange_calendars code (weekdays if unavailable)."""
    if code == "always":
        return AlwaysCalendar()
    if code == "weekdays":
        return WeekdayCalendar()
    try:
        return ExchangeCalendar(code)
    except ImportError:
        warnings.warn(f"exchange_calendars is not installed: {code} uses weekdays without holidays", stacklevel=2)
    except Exception as exc:  # noqa: BLE001 - an unknown code must not stop a study; the calendar reports itself inexact
        warnings.warn(f"calendar {code!r} unavailable ({exc}); using weekdays without holidays", stacklevel=2)
    return WeekdayCalendar(code)


def on_or_before(calendar: TradingCalendar, day: date) -> date:
    """`day` if it is a session, else the last session before it."""
    while not calendar.is_session(day):
        day -= timedelta(days=1)
    return day


def shift_sessions(calendar: TradingCalendar, day: date, sessions: int) -> date:
    """The day `sessions` sessions after `day` (before it when negative); `day` itself need not be a session."""
    step = timedelta(days=1 if sessions > 0 else -1)
    for _ in range(abs(sessions)):
        day += step
        while not calendar.is_session(day):
            day += step
    return day
