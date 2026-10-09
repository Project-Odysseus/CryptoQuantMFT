"""When a futures market can be traded: exchange holidays, the liquid part of the day, and the times orders go out.

Most index and currency futures trade nearly around the clock, but the order book is thin outside the home market's
day. So each market has a *liquid window* in its own local time, and orders are released only at a *slot* (a fixed
time of day at the desk) that falls inside that window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

SEARCH_DAYS = 14  # how far ahead to look for the next slot inside a liquid window (covers any holiday run)


class LiquidHours(Protocol):
    """What the session functions need to know about a market (`contracts.Market` provides it)."""

    calendar: str
    liquid_from: time
    liquid_to: time
    timezone: str


class Sessions:
    """Trading days per exchange calendar (`exchange_calendars` codes such as XNYS, CMES, XEUR), loaded once each.

    Outside the library's date range every weekday counts as a session, so a date far in the future never fails;
    it is only less exact about holidays.
    """

    def __init__(self) -> None:
        self._calendars: dict[str, Any] = {}

    def _calendar(self, name: str) -> Any:
        if name not in self._calendars:
            import exchange_calendars

            self._calendars[name] = exchange_calendars.get_calendar(name)
        return self._calendars[name]

    def is_session(self, name: str, day: date) -> bool:
        """Whether the exchange trades on `day`."""
        if day.weekday() >= 5:
            return False
        calendar, stamp = self._calendar(name), pd.Timestamp(day)
        if stamp < calendar.first_session or stamp > calendar.last_session:
            return True
        return bool(calendar.is_session(stamp))

    def close(self, name: str, day: date) -> datetime | None:
        """When the session of `day` ends (earlier on a half day), in UTC; None when unknown or not a session."""
        calendar, stamp = self._calendar(name), pd.Timestamp(day)
        if stamp < calendar.first_session or stamp > calendar.last_session or not self.is_session(name, day):
            return None
        return calendar.session_close(stamp).to_pydatetime().astimezone(timezone.utc)

    def on_or_before(self, name: str, day: date) -> date:
        """`day` if it is a session, else the last session before it."""
        while not self.is_session(name, day):
            day -= timedelta(days=1)
        return day

    def shift(self, name: str, day: date, sessions: int) -> date:
        """The day `sessions` trading days after `day` (before it when negative); `day` itself need not be a session."""
        step = timedelta(days=1 if sessions > 0 else -1)
        for _ in range(abs(sessions)):
            day += step
            while not self.is_session(name, day):
                day += step
        return day


def liquid_window(market: LiquidHours, day: date, sessions: Sessions) -> tuple[datetime, datetime] | None:
    """The liquid hours of `day` in UTC, cut short on a half day; None when the home market is closed."""
    if not sessions.is_session(market.calendar, day):
        return None
    zone = ZoneInfo(market.timezone)
    start = datetime.combine(day, market.liquid_from, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(day, market.liquid_to, tzinfo=zone).astimezone(timezone.utc)
    close = sessions.close(market.calendar, day)
    if close is not None and start < close < end:
        end = close
    return (start, end) if start < end else None


def is_liquid(market: LiquidHours, now: datetime, sessions: Sessions) -> bool:
    """Whether `now` is inside the market's liquid window."""
    window = liquid_window(market, now.astimezone(ZoneInfo(market.timezone)).date(), sessions)
    return window is not None and window[0] <= now < window[1]


@dataclass(frozen=True, slots=True)
class Slot:
    """A time of day at the desk when orders are released, e.g. 10:00 in Oslo for the European contracts."""

    name: str
    at: time
    timezone: str = "Europe/Oslo"

    def on(self, day: date) -> datetime:
        """This slot on `day`, in UTC."""
        return datetime.combine(day, self.at, tzinfo=ZoneInfo(self.timezone)).astimezone(timezone.utc)


DEFAULT_SLOTS = (Slot("europe", time(10, 0)), Slot("americas", time(16, 0)))


def parse_slots(items: Sequence[dict[str, str]] | None) -> tuple[Slot, ...]:
    """Slots from config rows like {"name": "europe", "at": "10:00", "timezone": "Europe/Oslo"}; the default when empty."""
    if not items:
        return DEFAULT_SLOTS
    slots = []
    for item in items:
        hour, minute = (int(part) for part in str(item["at"]).split(":"))
        slots.append(Slot(str(item["name"]), time(hour, minute), str(item.get("timezone", "Europe/Oslo"))))
    return tuple(slots)


def release_time(market: LiquidHours, after: datetime, slots: Sequence[Slot], sessions: Sessions) -> tuple[datetime, Slot] | None:
    """The first slot at or after `after` that falls inside the market's liquid window, with the slot.

    None means no slot ever reaches this market's liquid hours (a configuration error the caller reports), or the
    market is closed for longer than two weeks.
    """
    start = after.astimezone(timezone.utc)
    candidates = []
    for offset in range(-1, SEARCH_DAYS + 1):
        for slot in slots:
            moment = slot.on(start.date() + timedelta(days=offset))
            if moment >= start:
                candidates.append((moment, slot))
    for moment, slot in sorted(candidates, key=lambda pair: pair[0]):
        if is_liquid(market, moment, sessions):
            return moment, slot
    return None


def releasable(market: LiquidHours, now: datetime, slots: Sequence[Slot], sessions: Sessions) -> Slot | None:
    """The slot that lets an order for this market go out at `now`, or None.

    An order may go out when the market is liquid and a slot has already come up inside the same liquid window. So
    an order decided after a slot (a late start, a restart) still goes out that day, and one decided overnight waits
    for the slot.
    """
    moment = now.astimezone(timezone.utc)
    window = liquid_window(market, moment.astimezone(ZoneInfo(market.timezone)).date(), sessions)
    if window is None or not window[0] <= moment < window[1]:
        return None
    passed = [slot for slot in slots for day in (moment.date() - timedelta(days=1), moment.date(), moment.date() + timedelta(days=1))
              if window[0] <= slot.on(day) <= moment]
    return max(passed, key=lambda slot: slot.at) if passed else None
