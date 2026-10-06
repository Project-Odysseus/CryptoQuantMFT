"""H10: does the US morning's move continue into the US afternoon? One trade per coin and weekday, on 5-minute bars.

Definitions follow research/prereg/H10.txt. Times are New York local time, so the US stock open stays at 09:30 across
daylight saving. Every price is a 5-minute bar's open or close, and the position is entered one bar after the signal
is known.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

NEW_YORK = "America/New_York"


def session_trades(bars: pd.DataFrame, *, signal_start: str = "09:30", signal_end: str = "11:30", exit: str = "16:00", delay_minutes: int = 5) -> pd.DataFrame:
    """One row per weekday: the morning's log return (the signal) and the log return of holding from just after it to `exit`.

    Args:
        bars: 5-minute bars indexed by their UTC open time, with `open` and `close`.
        signal_start, signal_end: New York times. The signal is from the open of the bar starting at `signal_start`
            to the close of the bar ending at `signal_end`.
        exit: New York time at which the position is closed (the close of the bar ending then).
        delay_minutes: The position is entered at the close of the bar ending this long after `signal_end`.

    A day missing any of the four prices is left out.
    """
    local = bars.index.tz_convert(NEW_YORK)
    opens = pd.Series(bars["open"].to_numpy(), index=local)
    closes_at = pd.Series(bars["close"].to_numpy(), index=local + pd.Timedelta(minutes=5))  # a bar's close, stamped with the time it ends
    days = pd.DatetimeIndex(sorted(set(local.normalize())))
    days = days[days.dayofweek < 5]

    def at(series: pd.Series, clock: str, extra_minutes: int = 0) -> np.ndarray:
        hour, minute = (int(part) for part in clock.split(":"))
        stamps = pd.DatetimeIndex([pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, tz=NEW_YORK) for day in days]) + pd.Timedelta(minutes=extra_minutes)
        return series.reindex(stamps).to_numpy()

    start, known, entry, out = at(opens, signal_start), at(closes_at, signal_end), at(closes_at, signal_end, delay_minutes), at(closes_at, exit)
    frame = pd.DataFrame({"signal": np.log(known / start), "hold": np.log(out / entry)}, index=days.tz_localize(None))
    return frame.dropna()


def rule_returns(trades: pd.DataFrame, *, cost_bps: float = 0.0, direction: float = 1.0) -> pd.Series:
    """The rule's return per trade: sign of the signal times the hold return, minus the round-trip cost; days with a zero signal are flat (no cost)."""
    side = np.sign(trades["signal"]) * direction
    return side * trades["hold"] - (side != 0) * cost_bps / 10_000.0
