"""Data cards: the same quality checks on every series before anything is built on it (plan 1.5).

`data_card(bars, calendar=...)` checks a bar frame and says from which date it is usable:

- duplicates and ordering;
- OHLC consistency (high below open/close, low above them, non-positive prices);
- close-only history (bars with high = low = close, a missing field in early index data);
- stale prints (the same close many sessions in a row);
- calendar gaps (sessions the calendar has but the data lacks), when the calendar is exact; long gaps otherwise;
- outliers (a daily log move far beyond its own recent scale), flagged, not removed;
- roll jumps, for continuous futures series (`roll_days` given).

A card does not repair data. It reports, and gives `usable_from`: the first day after the last close-only year.
Studies cut their data there and include the card in their report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from quantlab.calendars import TradingCalendar

CLOSE_ONLY_YEAR = 20  # a year with more bars than this without a range is close-only history
STALE_RUN = 5  # this many identical closes in a row is a stale print
OUTLIER_SCALE = 8.0  # a move beyond this many robust standard deviations is flagged
LONG_GAP_DAYS = 6  # without an exact calendar, a gap longer than this many calendar days is flagged


@dataclass
class DataCard:
    """The checks of one series."""

    name: str
    rows: int
    first: str
    last: str
    usable_from: pd.Timestamp
    checks: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    flagged: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """No blocking problem (flags are for reading, problems block)."""
        return not self.problems

    def to_markdown(self) -> str:
        rows = ["| check | value |", "| --- | --- |"] + [f"| {key} | {value} |" for key, value in self.checks.items()]
        problems = [f"- PROBLEM: {item}" for item in self.problems]
        flags = [f"- {kind}: {', '.join(values[:10])}{' ...' if len(values) > 10 else ''}" for kind, values in self.flagged.items() if values]
        return "\n".join([f"### Data card: {self.name}", "", f"{self.rows} bars, {self.first} to {self.last}; usable from {self.usable_from:%Y-%m-%d}.", "",
                          *rows, "", *problems, *flags, ""])


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, length) of each run of True."""
    runs, start = [], None
    for index, value in enumerate(list(mask) + [False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index - start))
            start = None
    return runs


def data_card(bars: pd.DataFrame, *, name: str = "", calendar: TradingCalendar | None = None, interval: str = "1d",
              roll_days: pd.DatetimeIndex | None = None) -> DataCard:
    """Every check on one bar frame (schema: quantlab.data.bars)."""
    frame = bars.sort_index()
    problems: list[str] = []
    flagged: dict[str, list[str]] = {}
    o, h, l, c = (frame[name] for name in ("open", "high", "low", "close"))
    duplicates = int(frame.index.duplicated().sum())
    if duplicates:
        problems.append(f"{duplicates} duplicate timestamps")
    if not bars.index.is_monotonic_increasing:
        problems.append("timestamps are not sorted")
    bad_high = int((h < np.maximum(o, c) - 1e-9).sum())
    bad_low = int((l > np.minimum(o, c) + 1e-9).sum())
    non_positive = int((c <= 0).sum())
    if bad_high or bad_low:
        problems.append(f"OHLC inconsistent on {bad_high + bad_low} bars")
    if non_positive:
        flagged["non-positive closes"] = [f"{day:%Y-%m-%d}" for day in frame.index[(c <= 0).to_numpy()]]

    flat = ((h == l) & (l == c)).to_numpy()
    per_year = pd.Series(flat, index=frame.index).groupby(frame.index.year).sum()
    close_only = per_year[per_year > CLOSE_ONLY_YEAR]
    if len(close_only) and flat.mean() > 0.99:
        close_only = close_only.iloc[:0]  # one price a day throughout (a fixing, a rate): the series itself, not missing early ranges
    usable_from = pd.Timestamp(year=int(close_only.index.max()) + 1, month=1, day=1) if len(close_only) else frame.index[0]
    if getattr(frame.index, "tz", None) is not None:
        usable_from = usable_from.tz_localize(frame.index.tz) if usable_from.tz is None else usable_from

    closes = c.to_numpy()
    same = np.concatenate([[False], closes[1:] == closes[:-1]])
    stale = [(start, length) for start, length in _runs(same) if length + 1 >= STALE_RUN]
    flagged["stale runs"] = [f"{frame.index[start - 1]:%Y-%m-%d} ({length + 1} bars)" for start, length in stale]

    log_moves = np.log(c).diff()
    scale = (log_moves.abs().rolling(60, min_periods=20).median().shift(1) * 1.4826).replace(0, np.nan)
    outliers = log_moves.abs() > OUTLIER_SCALE * scale
    flagged["outliers"] = [f"{day:%Y-%m-%d} ({move:+.1%})" for day, move in log_moves[outliers].items()]

    missing_sessions = 0
    if interval == "1d" and calendar is not None and calendar.exact and len(frame):
        expected = calendar.sessions(frame.index[0], frame.index[-1])
        missing = expected.difference(frame.index)
        missing_sessions = len(missing)
        flagged["missing sessions"] = [f"{day:%Y-%m-%d}" for day in missing]
        extra = frame.index.difference(expected)
        flagged["bars on non-sessions"] = [f"{day:%Y-%m-%d}" for day in extra]
    elif interval == "1d" and len(frame) > 1:
        gaps = frame.index.to_series().diff().dt.days
        flagged["long gaps"] = [f"{day:%Y-%m-%d} ({int(gap)} days)" for day, gap in gaps[gaps > LONG_GAP_DAYS].items()]

    if roll_days is not None and len(roll_days):
        on_roll = log_moves.reindex(roll_days).abs()
        typical = log_moves.abs().median()
        flagged["roll jumps"] = [f"{day:%Y-%m-%d} ({move:.1%})" for day, move in on_roll.items() if np.isfinite(move) and move > 5 * typical]

    checks = {"duplicates": duplicates, "OHLC inconsistent bars": bad_high + bad_low, "close-only bars": int(flat.sum()),
              "close-only years": ", ".join(str(year) for year in close_only.index) or "-", "stale runs": len(stale),
              "outliers flagged": int(outliers.sum()), "missing sessions" if (calendar is not None and calendar.exact) else "long gaps": missing_sessions or len(flagged.get("long gaps", [])),
              "calendar": f"{calendar.name}{'' if calendar.exact else ' (no holidays)'}" if calendar is not None else "-"}
    return DataCard(name=name, rows=len(frame), first=f"{frame.index[0]:%Y-%m-%d}" if len(frame) else "", last=f"{frame.index[-1]:%Y-%m-%d}" if len(frame) else "",
                    usable_from=usable_from, checks=checks, problems=problems, flagged=flagged)
