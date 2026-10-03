"""Event studies: what happens after a market condition occurs, measured the same way for every hypothesis.

A hypothesis usually predicts that *after* some condition (a drawdown of 10%, funding above its 95th percentile, a
liquidation burst) returns differ from normal. This module turns any list of such moments into the standard
answers, so each new idea needs only its own event definition:

- `event_study`: forward returns at several horizons after the events, with the mean, median, hit rate, quantiles,
  bootstrap confidence intervals, the worst and best excursion on the way, and the same horizon's return after
  matched control moments (random bars with a similar matching variable, e.g. volatility, away from any event). An
  effect that is no larger than its matched control is the market's normal behaviour in that state, not an edge.
- `drawdown_episodes`: one row per drawdown, with the first time it crossed each depth. Counting "5% dips" and
  "10% dips" as separate event lists double-counts the same selloff and compares different entry points; an
  episode keeps them together and says how often a 5% dip went on to 10%, and how long recovery took, with
  unfinished episodes flagged instead of dropped.
- `regime_labels` / `by_regime`: the same outcomes split by the trend and volatility state at the event.
- `data_quality`: gaps, duplicates and broken candles in the bars, before any of it is trusted.
- `robustness_grid`: one headline number across nearby definitions, to look for a plateau instead of a best cell.

Timing convention (no look-ahead): bars are stamped at their open, and an event at position p is known at the
close of bar p. With `entry="next_open"` the position is taken at the open of bar p+1 and a horizon of h bars ends
at the close of bar p+h. Everything is in log returns. Each combination tried is a trial: log them
(`governance.record_trials`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any

import numpy as np
import pandas as pd

from src.research.inference import block_bootstrap_ci, matched_baseline

HORIZONS = (1, 5, 10, 20)
ENTRIES = ("next_open", "close")


def first_crossings(active: Sequence[bool] | np.ndarray | pd.Series, *, min_gap: int = 1) -> np.ndarray:
    """Positions where `active` switches on, keeping only those at least `min_gap` bars after the previous kept one.

    A condition that stays true for days is one event, not one per bar; `min_gap` also merges a condition that
    flickers (use the longest horizon studied, so the outcome windows don't overlap).
    """
    flags = np.asarray(active, dtype=bool)
    starts = np.flatnonzero(flags & ~np.r_[False, flags[:-1]])
    kept: list[int] = []
    for position in starts:
        if not kept or position - kept[-1] >= min_gap:
            kept.append(int(position))
    return np.asarray(kept, dtype=int)


def _columns(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    missing = [name for name in ("open", "high", "low", "close") if name not in frame.columns]
    if missing:
        raise ValueError(f"the frame needs open, high, low and close columns (missing {missing})")
    return tuple(frame[name].to_numpy(dtype=float) for name in ("open", "high", "low", "close"))  # type: ignore[return-value]


def _entry_prices(open_: np.ndarray, close: np.ndarray, positions: np.ndarray, entry: str) -> np.ndarray:
    if entry not in ENTRIES:
        raise ValueError(f"entry must be one of {ENTRIES}")
    prices = np.full(len(positions), np.nan)
    if entry == "close":
        prices[:] = close[positions]
    else:
        inside = positions + 1 < len(open_)
        prices[inside] = open_[positions[inside] + 1]
    return prices


def forward_returns(frame: pd.DataFrame, positions: Sequence[int] | np.ndarray, horizon: int, *, entry: str = "next_open") -> np.ndarray:
    """Log return from the entry after each event to the close `horizon` bars after the event bar (NaN past the data)."""
    open_, _high, _low, close = _columns(frame)
    positions = np.asarray(positions, dtype=int)
    out = np.full(len(positions), np.nan)
    start = _entry_prices(open_, close, positions, entry)
    inside = positions + horizon < len(close)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[inside] = np.log(close[positions[inside] + horizon] / start[inside])
    return out


def excursions(frame: pd.DataFrame, positions: Sequence[int] | np.ndarray, horizon: int, *, entry: str = "next_open", side: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """(worst, best) log move against and for a `side` position on the way to the horizon, from the bars' highs and lows.

    The worst excursion is what a stop would have had to survive; a good mean with a terrible excursion is a
    trade few would hold. Both are 0 or beyond: the worst is <= 0, the best >= 0.
    """
    open_, high, low, close = _columns(frame)
    positions = np.asarray(positions, dtype=int)
    start = _entry_prices(open_, close, positions, entry)
    worst, best = np.full(len(positions), np.nan), np.full(len(positions), np.nan)
    for index, position in enumerate(positions):
        if position + horizon >= len(close) or not np.isfinite(start[index]):
            continue
        # the entry is after bar p in both modes, so bars p+1 .. p+horizon are the ones held
        highs, lows = high[position + 1 : position + horizon + 1], low[position + 1 : position + horizon + 1]
        up, down = float(np.log(highs.max() / start[index])), float(np.log(lows.min() / start[index]))
        favourable, adverse = (up, down) if side >= 0 else (-down, -up)
        worst[index], best[index] = min(adverse, 0.0), max(favourable, 0.0)
    return worst, best


def event_frequency(index: pd.DatetimeIndex, positions: Sequence[int] | np.ndarray) -> dict[str, Any]:
    """How often the signal fires: the count, per year, the usual gap, and how much of the sample has any event at all."""
    positions = np.asarray(positions, dtype=int)
    if not len(positions):
        return {"events": 0, "first": None, "last": None, "per_year": {}, "median_gap_bars": float("nan"), "years_with_events": 0, "years": int(len(set(index.year)))}
    years = pd.Series(index[positions].year).value_counts().sort_index()
    return {"events": int(len(positions)), "first": index[positions[0]], "last": index[positions[-1]],
            "per_year": {int(year): int(count) for year, count in years.items()},
            "median_gap_bars": float(np.median(np.diff(positions))) if len(positions) > 1 else float("nan"),
            "years_with_events": int(len(years)), "years": int(len(set(index.year)))}


@dataclass(slots=True)
class EventStudy:
    """The result of `event_study`.

    Attributes:
        summary: One row per horizon (bars): events, mean, median, hit_rate, std, quantiles, the mean's bootstrap
            interval, mean worst and best excursion, the matched controls' mean, the excess over them with its
            interval, and the unconditional mean of the same horizon. Returns are log returns for a `side`
            position, after `cost_bps`.
        events: One row per event: its time, position, and `ret_<h>`, `mae_<h>`, `mfe_<h>`, `control_<h>` per horizon.
        paths: Bars relative to the event (-pre .. +longest horizon) -> the mean and median log move of the close
            from the event bar's close, and how many events have that bar.
        frequency: `event_frequency` of the events.
    """

    summary: pd.DataFrame
    events: pd.DataFrame
    paths: pd.DataFrame
    frequency: dict[str, Any]
    side: float
    entry: str


def event_study(frame: pd.DataFrame, events: Sequence[int] | np.ndarray | pd.DatetimeIndex, *, horizons: Sequence[int] = HORIZONS, entry: str = "next_open",
                side: float = 1.0, matching: Sequence[float] | np.ndarray | pd.Series | None = None, controls: int = 20, tolerance: float = 0.2,
                cost_bps: float = 0.0, bootstrap_runs: int = 2000, pre_bars: int = 10, seed: int = 0) -> EventStudy:
    """Forward returns after `events`, against matched controls. See the module docstring for the timing.

    Args:
        frame: Bars indexed by open time with open, high, low, close.
        events: Bar positions, or the bars' timestamps.
        horizons: Holding periods in bars.
        side: +1 studies a long after the event, -1 a short (returns are multiplied by it).
        matching: A value per bar, known at that bar's close (e.g. trailing realized volatility). Each event is
            compared with `controls` random bars whose value is within `tolerance` of its own and that are not
            within the longest horizon of any event. None skips the controls.
        cost_bps: Round-trip cost taken off every event's return (controls are left gross: they are a yardstick
            for the market's behaviour, not a trade).
        pre_bars: Bars before the event shown in `paths`.
    """
    open_, _high, _low, close = _columns(frame)
    if isinstance(events, pd.DatetimeIndex):
        positions = frame.index.get_indexer(events)
        if (positions < 0).any():
            raise ValueError("some event times are not bar timestamps of the frame")
    else:
        positions = np.asarray(events, dtype=int)
    positions = np.sort(positions)
    horizons = tuple(sorted(int(horizon) for horizon in horizons))
    longest = horizons[-1]
    count = len(close)
    everywhere = np.arange(count)
    cost = cost_bps / 10_000.0
    near_event = np.zeros(count, dtype=bool)
    for position in positions:
        near_event[max(0, position - longest) : position + longest + 1] = True
    match = np.asarray(matching, dtype=float) if matching is not None else None

    per_event: dict[str, np.ndarray] = {}
    rows = []
    for horizon in horizons:
        returns = side * forward_returns(frame, positions, horizon, entry=entry) - cost
        worst, best = excursions(frame, positions, horizon, entry=entry, side=side)
        per_event[f"ret_{horizon}"], per_event[f"mae_{horizon}"], per_event[f"mfe_{horizon}"] = returns, worst, best
        usable = returns[np.isfinite(returns)]
        _estimate, low, high = block_bootstrap_ci(usable, block=1, runs=bootstrap_runs, seed=seed)
        everywhere_returns = side * forward_returns(frame, everywhere, horizon, entry=entry)
        row: dict[str, Any] = {
            "horizon": horizon, "events": int(len(usable)), "mean": float(np.mean(usable)) if len(usable) else np.nan,
            "median": float(np.median(usable)) if len(usable) else np.nan, "hit_rate": float(np.mean(usable > 0)) if len(usable) else np.nan,
            "std": float(np.std(usable, ddof=1)) if len(usable) > 1 else np.nan,
            **{f"q{int(q * 100):02d}": float(np.quantile(usable, q)) if len(usable) else np.nan for q in (0.05, 0.25, 0.75, 0.95)},
            "ci_low": low, "ci_high": high,
            "mean_mae": float(np.nanmean(worst)) if np.isfinite(worst).any() else np.nan, "mean_mfe": float(np.nanmean(best)) if np.isfinite(best).any() else np.nan,
            "unconditional_mean": float(np.nanmean(everywhere_returns)),
            "control_mean": np.nan, "excess": np.nan, "excess_ci_low": np.nan, "excess_ci_high": np.nan,
        }
        if match is not None and len(positions):
            control, _drawn = matched_baseline(positions, everywhere_returns, match, exclude=near_event, samples=controls, tolerance=tolerance, seed=seed)
            per_event[f"control_{horizon}"] = control
            excess = (returns - control)[np.isfinite(returns) & np.isfinite(control)]
            estimate, excess_low, excess_high = block_bootstrap_ci(excess, block=1, runs=bootstrap_runs, seed=seed)
            row.update({"control_mean": float(np.nanmean(control)) if np.isfinite(control).any() else np.nan, "excess": estimate,
                        "excess_ci_low": excess_low, "excess_ci_high": excess_high})
        rows.append(row)

    offsets = np.arange(-pre_bars, longest + 1)
    moves = np.full((len(positions), len(offsets)), np.nan)
    for row_index, position in enumerate(positions):
        bars = position + offsets
        inside = (bars >= 0) & (bars < count)
        moves[row_index, inside] = side * np.log(close[bars[inside]] / close[position])
    with np.errstate(all="ignore"):
        paths = pd.DataFrame({"mean": np.nanmean(moves, axis=0) if len(positions) else np.nan, "median": np.nanmedian(moves, axis=0) if len(positions) else np.nan,
                              "events": np.isfinite(moves).sum(axis=0)}, index=pd.Index(offsets, name="bars_from_event"))
    table = pd.DataFrame({"time": frame.index[positions], "position": positions, **per_event})
    return EventStudy(summary=pd.DataFrame(rows).set_index("horizon"), events=table, paths=paths, frequency=event_frequency(frame.index, positions), side=side, entry=entry)


# --- drawdown episodes ------------------------------------------------------------------------------------------------

def _label(threshold: float) -> str:
    return f"{threshold * 100:g}"


def drawdown_episodes(close: pd.Series, *, thresholds: Sequence[float] = (0.05, 0.07, 0.10, 0.15), peak_lookback: int | None = 20,
                      max_bars: int | None = None) -> pd.DataFrame:
    """One row per drawdown episode of `close`, with the first bar it crossed each depth.

    An episode starts at the first close at least `thresholds[0]` below the reference peak: the highest of the
    previous `peak_lookback` closes (the current bar excluded), or the highest close so far when it is None. That
    peak stays the episode's anchor. The episode ends at the first close back at the anchor (recovered), after
    `max_bars` bars (expired: a reset, so a long bear market doesn't hide every later dip), or with the data
    (censored: its recovery is unknown, not long).

    Returns a frame with, per episode: `peak_time`, `peak_price`, `start_time`, `cross_<pct>_time` and
    `cross_<pct>_pos` for every threshold (NaT / -1 if never reached), `trough_time`, `trough_price`,
    `max_drawdown`, `end_time`, `recovered`, `censored`, `bars_to_trough`, `bars_to_recovery` (NaN unless
    recovered) and `bars_observed`. Crossing times use only closes up to that bar; the trough and the recovery are
    outcomes.
    """
    thresholds = tuple(sorted(float(value) for value in thresholds))
    if not thresholds or thresholds[0] <= 0 or thresholds[-1] >= 1:
        raise ValueError("thresholds must be fractions between 0 and 1 (0.05 = 5% below the peak)")
    values = close.to_numpy(dtype=float)
    index = close.index
    prior = close.shift(1)
    peaks = (prior.rolling(peak_lookback, min_periods=peak_lookback).max() if peak_lookback else prior.cummax()).to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []
    position, count = 0, len(values)
    while position < count:
        anchor = peaks[position]
        if not np.isfinite(anchor) or values[position] > anchor * (1.0 - thresholds[0]):
            position += 1
            continue
        window_start = max(0, position - peak_lookback) if peak_lookback else 0
        peak_position = window_start + int(np.argmax(values[window_start:position]))
        crossed = {threshold: -1 for threshold in thresholds}
        trough_position, end, recovered, expired = position, count - 1, False, False
        for current in range(position, count):
            drawdown = 1.0 - values[current] / anchor
            if current > position and values[current] >= anchor:
                end, recovered = current, True
                break
            if max_bars is not None and current - position >= max_bars:
                end, expired = current, True
                break
            if values[current] < values[trough_position]:
                trough_position = current
            for threshold in thresholds:
                if crossed[threshold] < 0 and drawdown >= threshold:
                    crossed[threshold] = current
        row: dict[str, Any] = {"episode": len(rows), "peak_time": index[peak_position], "peak_price": anchor, "start_time": index[position]}
        for threshold in thresholds:
            hit = crossed[threshold]
            row[f"cross_{_label(threshold)}_time"] = index[hit] if hit >= 0 else pd.NaT
            row[f"cross_{_label(threshold)}_pos"] = hit
        row.update({"trough_time": index[trough_position], "trough_price": values[trough_position], "max_drawdown": 1.0 - values[trough_position] / anchor,
                    "end_time": index[end], "recovered": recovered, "censored": not recovered and not expired,
                    "bars_to_trough": trough_position - position, "bars_to_recovery": float(end - position) if recovered else np.nan,
                    "bars_observed": end - position})
        rows.append(row)
        position = end + 1 if recovered else end if expired else count
    return pd.DataFrame(rows)


def episode_events(episodes: pd.DataFrame, threshold: float) -> np.ndarray:
    """Bar positions where episodes first crossed `threshold`, for `event_study` (one per episode that got there)."""
    column = f"cross_{_label(threshold)}_pos"
    if column not in episodes:
        raise ValueError(f"the episodes were not built with a {threshold:g} threshold")
    return episodes.loc[episodes[column] >= 0, column].to_numpy(dtype=int)


def episode_progression(episodes: pd.DataFrame, *, recovery_bars: Sequence[int] = (20, 60, 120)) -> pd.DataFrame:
    """Per depth: how many episodes reached it, how many went on to each deeper one, and how often the peak came back in time.

    `recovered_within_<n>` counts bars from the crossing of that depth, among the episodes whose outcome at n bars
    is known: an episode still open with fewer than n bars after the crossing is left out of that share (its
    answer isn't in yet), not counted as a failure and not dropped from the other columns.
    """
    labels = [column[len("cross_"):-len("_pos")] for column in episodes.columns if column.startswith("cross_") and column.endswith("_pos")]
    rows = []
    for position, label in enumerate(labels):
        crossing = episodes[f"cross_{label}_pos"]
        reached = episodes[crossing >= 0]
        row: dict[str, Any] = {"threshold_pct": float(label), "episodes": int(len(reached))}
        if len(reached):
            start = episodes.loc[reached.index, "cross_" + labels[0] + "_pos"]
            since_crossing = start + reached["bars_observed"] - crossing[reached.index]
            row["median_further_drawdown"] = float((reached["max_drawdown"] - float(label) / 100.0).median())
            for deeper in labels[position + 1:]:
                row[f"went_on_to_{deeper}"] = float((reached[f"cross_{deeper}_pos"] >= 0).mean())
            for bars in recovery_bars:
                back = reached["recovered"] & (since_crossing <= bars)
                known = back | (since_crossing >= bars)
                row[f"recovered_within_{bars}"] = float(back[known].mean()) if known.any() else np.nan
            row["still_open"] = int(reached["censored"].sum())
        rows.append(row)
    return pd.DataFrame(rows).set_index("threshold_pct")


# --- conditioning, quality, robustness ---------------------------------------------------------------------------------

def regime_labels(frame: pd.DataFrame, *, trend_window: int = 200, vol_window: int = 30, vol_history: int = 365) -> pd.DataFrame:
    """The trend and volatility state at each bar's close, from bars up to it only.

    `trend`: "up" when the close is above the mean of the previous `trend_window` closes, else "down".
    `vol`: "low", "mid" or "high": where the realized volatility of the last `vol_window` returns ranks among its
    own previous `vol_history` values (thirds). Bars without enough history are "n/a".
    """
    close = frame["close"].astype(float)
    average = close.shift(1).rolling(trend_window, min_periods=trend_window).mean()
    trend = pd.Series(np.where(average.isna(), "n/a", np.where(close > average, "up", "down")), index=frame.index)
    realized = np.log(close).diff().rolling(vol_window, min_periods=vol_window).std()
    rank = realized.rolling(vol_history, min_periods=max(vol_window, vol_history // 3)).rank(pct=True)
    vol = pd.Series(np.where(rank.isna(), "n/a", np.where(rank <= 1 / 3, "low", np.where(rank <= 2 / 3, "mid", "high"))), index=frame.index)
    return pd.DataFrame({"trend": trend, "vol": vol})


def by_regime(study: EventStudy, labels: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """The study's `horizon` returns grouped by each regime column's label at the event: events, mean, median, hit rate."""
    column = f"ret_{horizon}"
    if column not in study.events:
        raise ValueError(f"the study has no {horizon}-bar horizon")
    rows = []
    for regime in labels.columns:
        at_event = labels[regime].to_numpy()[study.events["position"].to_numpy(dtype=int)]
        for label in sorted(set(at_event)):
            values = study.events.loc[at_event == label, column].dropna()
            rows.append({"regime": regime, "label": label, "events": int(len(values)), "mean": float(values.mean()) if len(values) else np.nan,
                         "median": float(values.median()) if len(values) else np.nan, "hit_rate": float((values > 0).mean()) if len(values) else np.nan})
    return pd.DataFrame(rows).set_index(["regime", "label"])


def data_quality(frame: pd.DataFrame, interval_seconds: float) -> dict[str, Any]:
    """Problems in a bar series that would quietly bend a study: gaps, duplicates, impossible candles, dead volume."""
    index = pd.DatetimeIndex(frame.index)
    gaps = pd.Series(index).diff().dt.total_seconds().to_numpy()[1:] / interval_seconds if len(index) > 1 else np.array([])
    close = frame["close"].to_numpy(dtype=float)
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        moves = np.abs(np.diff(np.log(close))) if len(close) > 1 else np.array([])
    broken = (frame["high"] < frame[["open", "close"]].max(axis=1) - 1e-12) | (frame["low"] > frame[["open", "close"]].min(axis=1) + 1e-12)
    volume = frame["volume"].to_numpy(dtype=float) if "volume" in frame else None
    return {
        "bars": int(len(frame)), "first": index[0] if len(index) else None, "last": index[-1] if len(index) else None,
        "sorted": bool(index.is_monotonic_increasing), "duplicate_timestamps": int(index.duplicated().sum()),
        "missing_bars": int(np.sum(np.round(gaps[gaps > 1.5]) - 1)) if len(gaps) else 0, "longest_gap_bars": int(round(float(gaps.max()))) if len(gaps) else 0,
        "nonpositive_or_nan_prices": int((~np.isfinite(prices) | (prices <= 0)).any(axis=1).sum()), "inconsistent_candles": int(broken.sum()),
        "zero_volume_share": float(np.mean(volume == 0)) if volume is not None and len(volume) else float("nan"),
        "largest_abs_log_return": float(np.nanmax(moves)) if len(moves) else float("nan"),
        "largest_move_at": index[int(np.nanargmax(moves)) + 1] if len(moves) and np.isfinite(moves).any() else None,
    }


def robustness_grid(study: Callable[..., EventStudy], grid: Mapping[str, Sequence[Any]], *, horizon: int, value: str = "mean") -> pd.DataFrame:
    """`study(**params)` for every combination in `grid`: one row each with the events and `value` at `horizon`.

    Use a small grid of nearby, sensible definitions fixed before looking. What to look for is a plateau: the same
    sign and a similar size across neighbours. One cell far above the rest is noise that happened to fit. Every
    row is a trial for the ledger (`len(result)`).
    """
    names = list(grid)
    rows = []
    for combination in product(*(grid[name] for name in names)):
        params = dict(zip(names, combination))
        result = study(**params)
        rows.append({**params, "events": int(result.summary.loc[horizon, "events"]), value: float(result.summary.loc[horizon, value]),
                     "ci_low": float(result.summary.loc[horizon, "ci_low"]), "ci_high": float(result.summary.loc[horizon, "ci_high"])})
    return pd.DataFrame(rows)


def plateau(table: pd.DataFrame, value: str = "mean") -> dict[str, float]:
    """How flat a robustness grid is: the share of cells with the median's sign, and the spread of the values."""
    values = table[value].dropna().to_numpy(dtype=float)
    if not len(values):
        return {"cells": 0, "same_sign_share": float("nan"), "min": float("nan"), "median": float("nan"), "max": float("nan")}
    median = float(np.median(values))
    return {"cells": int(len(values)), "same_sign_share": float(np.mean(np.sign(values) == np.sign(median))), "min": float(values.min()),
            "median": median, "max": float(values.max())}
