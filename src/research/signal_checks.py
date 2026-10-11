"""The first screen for a signal: does it predict the return that follows, and could that be luck, a leak or one lucky stretch?

A strategy is a signal plus trading rules plus costs. This module looks at the signal alone, before any rule is
written, and asks the same questions of every idea (the checks are QuantTAA's IC audit, adapted to bars and costs;
docs/signal_checks_plan.md has the notes). One call gives a card:

    from src.research import signal_checks as sc
    card = sc.signal_card(signal, close, horizons=[5, 20], name="funding_z")
    card.table      # one row per check: the number, what it is compared with, pass or fail
    card.verdict    # "worth a strategy test", "weak", "no evidence" or "short history"
    card.plot()

`signal` and `close` are a Series for one market or a DataFrame of dates by markets (then the card is
cross-sectional: each date's ranking of markets against the returns that followed). The signal at bar t must use
data up to bar t's close only; the forward return runs from that close. Pass `forward=` instead of `close` when
the return that follows is not a fixed number of bars (an afternoon after a morning, a week after a Friday).

The pass marks come from `research/signal_standard.toml`, fixed in advance. Each card is written to the trial
ledger, and data inside the frozen final holdout is cut off first.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from src.research import governance

STANDARD_PATH = Path("research/signal_standard.toml")
CARD_DIR = Path("data/research/signal_cards")
BPS = 10_000.0
VERDICTS = ("worth a strategy test", "weak", "no evidence", "short history")
OVERRIDE_MARK = "SIGNAL CARD OVERRIDE"
Data = pd.Series | pd.DataFrame


# --- the standard --------------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Standard:
    """The pass marks (see `research/signal_standard.toml` for what each means)."""

    version: int = 1
    level: float = 0.95
    placebo_p: float = 0.05
    delay_bars: int = 1
    delay_keep: float = 0.5
    stable_share: float = 0.70
    min_blocks: int = 3
    min_independent: float = 30.0
    round_trip_bps: float = 20.0
    buckets: int = 5
    baseline_lookback: int = 20
    placebo_runs: int = 200
    redundant_above: float = 0.80


def load_standard(path: Path | str = STANDARD_PATH) -> Standard:
    """The standard from its file. An unknown key is an error, so a typo cannot loosen a check without notice."""
    raw = tomllib.loads(Path(path).read_text())
    unknown = sorted(set(raw) - {item.name for item in fields(Standard)})
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}")
    return Standard(**raw)


# --- statistics on one pair of series --------------------------------------------------------------------------------

def forward_returns(close: Data, horizon: int) -> Data:
    """Log return from each bar's close to the close `horizon` bars later (NaN where it runs off the end)."""
    logs = np.log(close.astype(float))
    return logs.shift(-horizon) - logs


def _pairs(signal: pd.Series, forward: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"signal": signal, "forward": forward}).replace([np.inf, -np.inf], np.nan).dropna()


def _centred_ranks(values: pd.Series | np.ndarray) -> np.ndarray:
    ranks = pd.Series(values).rank().to_numpy(dtype=float)
    return ranks - ranks.mean()


def ic(signal: pd.Series, forward: pd.Series) -> float:
    """Information coefficient: the Spearman rank correlation of the signal with the return that followed."""
    frame = _pairs(signal, forward)
    if len(frame) < 3 or frame["signal"].nunique() < 2 or frame["forward"].nunique() < 2:
        return float("nan")
    return float(frame["signal"].rank().corr(frame["forward"].rank()))


def _autocorrelations(values: np.ndarray, lags: int) -> np.ndarray:
    """Autocorrelation at lags 1..`lags` of a centred series, all at once."""
    count = len(values)
    size = 1 << int(np.ceil(np.log2(2 * count)))
    spectrum = np.fft.rfft(values, size)
    products = np.fft.irfft(spectrum * np.conj(spectrum), size)[: lags + 1]
    return products[1:] / products[0] if products[0] > 0 else np.zeros(lags)


def effective_n(signal: pd.Series, forward: pd.Series, horizon: int = 1) -> float:
    """Independent observations behind an IC when both series are persistent.

    Bartlett's formula: n / (1 + 2 x the sum over lags of the two autocorrelations multiplied). A slow signal against
    overlapping forward returns has far fewer independent observations than bars, and fewer than bars / horizon.
    """
    frame = _pairs(signal, forward)
    count = len(frame)
    if count < 8:
        return float(count)
    lags = int(min(count // 4, max(100, 10 * horizon)))
    weights = 1.0 - np.arange(1, lags + 1) / count
    total = float(np.sum(weights * _autocorrelations(_centred_ranks(frame["signal"]), lags) * _autocorrelations(_centred_ranks(frame["forward"]), lags)))
    return float(np.clip(count / max(1.0 + 2.0 * total, 1e-9), 3.0, count))


def ic_interval(value: float, n_eff: float, level: float = 0.95) -> tuple[float, float]:
    """The interval of a correlation from the Fisher transform with `n_eff` observations."""
    if not np.isfinite(value) or n_eff <= 3:
        return float("nan"), float("nan")
    half = NormalDist().inv_cdf(0.5 + level / 2.0) / np.sqrt(n_eff - 3.0)
    centre = np.arctanh(np.clip(value, -0.999999, 0.999999))
    return float(np.tanh(centre - half)), float(np.tanh(centre + half))


def non_overlapping_ic(signal: pd.Series, forward: pd.Series, horizon: int) -> float:
    """The IC on every `horizon`-th bar, averaged over the `horizon` possible starting bars."""
    frame = _pairs(signal, forward)
    values = [ic(part["signal"], part["forward"]) for part in (frame.iloc[start::horizon] for start in range(max(horizon, 1)))]
    values = [value for value in values if np.isfinite(value)]
    return float(np.mean(values)) if values else float("nan")


def newey_west_mean(values: pd.Series | np.ndarray, lags: int) -> tuple[float, float]:
    """(mean, its standard error) with a Bartlett-kernel variance over `lags` lags, for overlapping observations."""
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    count = len(data)
    if count < 3:
        return float("nan"), float("nan")
    centred = data - data.mean()
    variance = centred @ centred / count
    for lag in range(1, min(lags, count - 1) + 1):
        variance += 2.0 * (1.0 - lag / (lags + 1.0)) * (centred[lag:] @ centred[:-lag]) / count
    return float(data.mean()), float(np.sqrt(variance / count)) if variance > 0 else float("nan")


def expanding_buckets(signal: pd.Series, buckets: int = 5, min_history: int = 100) -> pd.Series:
    """Each value's bucket (1 = lowest) against the signal's own history up to that bar: no later value is used."""
    share = signal.expanding(min_periods=min_history).rank(pct=True)
    return np.ceil(share * buckets).clip(1, buckets).where(signal.notna())


def bucket_edges(signal: pd.Series, forward: pd.Series, buckets: int = 5, min_history: int = 100) -> pd.DataFrame:
    """Mean forward return per point-in-time bucket, in basis points above the average of all bucketed bars."""
    frame = _pairs(signal, forward)
    frame["bucket"] = expanding_buckets(frame["signal"], buckets, min(min_history, max(len(frame) // 5, 10)))
    frame = frame.dropna(subset=["bucket"])
    grouped = frame.groupby("bucket")["forward"]
    return pd.DataFrame({"bars": grouped.count(), "edge_bps": (grouped.mean() - frame["forward"].mean()) * BPS})


def hit_and_spread(signal: pd.Series, forward: pd.Series, horizon: int, *, min_history: int = 100) -> dict[str, float]:
    """After a high signal against after a low one, split at the signal's own median so far.

    `hit_rate`: share of bars where the side of the median matched the side of the return against its average.
    `spread_bps`: mean forward return above the median minus below it. `spread_t`: its t-statistic with Newey-West
    errors over `horizon` - 1 lags, because forward returns overlap.
    """
    frame = _pairs(signal, forward)
    median = frame["signal"].expanding(min_periods=min(min_history, max(len(frame) // 5, 10))).median()
    side = np.sign(frame["signal"] - median).replace(0.0, np.nan)
    frame = frame.assign(side=side).dropna()
    if len(frame) < 10 or frame["side"].nunique() < 2:
        return {"hit_rate": float("nan"), "spread_bps": float("nan"), "spread_t": float("nan")}
    excess = frame["forward"] - frame["forward"].mean()
    mean, error = newey_west_mean(frame["side"] * excess, max(horizon - 1, 0))
    above, below = frame.loc[frame["side"] > 0, "forward"].mean(), frame.loc[frame["side"] < 0, "forward"].mean()
    return {"hit_rate": float((np.sign(excess) == frame["side"]).mean()), "spread_bps": float((above - below) * BPS),
            "spread_t": float(mean / error) if error and np.isfinite(error) else float("nan")}


def shift_placebo(signal: pd.Series, forward: pd.Series, horizon: int) -> dict[str, float]:
    """The IC of the signal against returns from other dates, for every circular shift at once.

    A shift keeps the persistence of both series and breaks only their link in time, so the spread of these ICs is
    what luck looks like for this signal. `p`: the share of shifts with an IC at least as large in size.
    """
    frame = _pairs(signal, forward)
    count = len(frame)
    away = int(max(2 * horizon, 20))
    if count < 4 * away:
        return {"p": float("nan"), "placebo_95": float("nan"), "shifts": 0.0}
    a, b = _centred_ranks(frame["signal"]), _centred_ranks(frame["forward"])
    correlations = np.fft.irfft(np.fft.rfft(b) * np.conj(np.fft.rfft(a)), count) / np.sqrt((a @ a) * (b @ b))
    actual, others = correlations[0], np.abs(correlations[away: count - away])
    return {"p": float(np.mean(others >= abs(actual))), "placebo_95": float(np.quantile(others, 0.95)), "shifts": float(len(others))}


def permutation_95(signal: pd.Series, forward: pd.Series, runs: int = 200, seed: int = 0) -> float:
    """The 95th percentile of |IC| when the signal's order is scrambled. Too easy to beat for a persistent signal: reported, never the test."""
    frame = _pairs(signal, forward)
    if len(frame) < 10:
        return float("nan")
    a, b = _centred_ranks(frame["signal"]), _centred_ranks(frame["forward"])
    rng, scale = np.random.default_rng(seed), np.sqrt((a @ a) * (b @ b))
    return float(np.quantile([abs(rng.permutation(a) @ b) / scale for _ in range(runs)], 0.95))


def delay_profile(signal: pd.Series, forward: pd.Series, delays: Sequence[int] = (0, 1, 2, 3, 5, 10)) -> pd.Series:
    """The IC with the signal known `delay` bars late. A real effect fades; one that needs the signal's own bar falls at once."""
    return pd.Series({delay: ic(signal.shift(delay), forward) for delay in delays}, name="ic")


def rolling_ic(signal: pd.Series, forward: pd.Series, window: int) -> pd.Series:
    """The IC over the last `window` bars (correlation of whole-sample ranks, which is fast and very close)."""
    frame = _pairs(signal, forward)
    return frame["signal"].rank().rolling(window, min_periods=max(window // 2, 10)).corr(frame["forward"].rank()).rename("rolling_ic")


def block_of(index: pd.DatetimeIndex, block: str | None = None) -> pd.Index:
    """Calendar block per date: years, or quarters when the sample is shorter than three years."""
    if block is None:
        block = "Y" if len(index) and (index.max() - index.min()).days >= 3 * 365 else "Q"
    return index.year if block == "Y" else pd.Index([f"{stamp.year}Q{stamp.quarter}" for stamp in index])


def ic_by_block(signal: pd.Series, forward: pd.Series, horizon: int = 1, block: str | None = None) -> pd.Series:
    """The IC per calendar block, for blocks with enough bars to say anything."""
    frame = _pairs(signal, forward)
    if frame.empty:
        return pd.Series(dtype=float, name="ic")
    labels = block_of(pd.DatetimeIndex(frame.index), block)
    need = max(20, 3 * horizon)
    out = {label: ic(part["signal"], part["forward"]) for label, part in frame.groupby(np.asarray(labels)) if len(part) >= need}
    return pd.Series(out, name="ic", dtype=float)


# --- cross-section: a ranking of markets on each date ----------------------------------------------------------------

def ic_by_date(signal: pd.DataFrame, forward: pd.DataFrame, min_markets: int = 5) -> pd.Series:
    """Per date: the rank correlation across markets between the signal and the forward return."""
    both = signal.notna() & forward.notna()
    a, b = signal.where(both).rank(axis=1), forward.where(both).rank(axis=1)
    a, b = a.sub(a.mean(axis=1), axis=0), b.sub(b.mean(axis=1), axis=0)
    scale = np.sqrt((a**2).sum(axis=1) * (b**2).sum(axis=1))
    return ((a * b).sum(axis=1) / scale.where(scale > 0)).where(both.sum(axis=1) >= min_markets).rename("ic")


def top_minus_rest(signal: pd.DataFrame, forward: pd.DataFrame, buckets: int = 5, min_markets: int = 5) -> pd.DataFrame:
    """Per date: forward return of the top and of the bottom group of markets, each minus that date's average."""
    both = signal.notna() & forward.notna()
    count = both.sum(axis=1)
    share = signal.where(both).rank(axis=1, pct=True)
    relative = forward.where(both).sub(forward.where(both).mean(axis=1), axis=0)
    top = relative.where(share > 1.0 - 1.0 / buckets).mean(axis=1)
    bottom = relative.where(share <= 1.0 / buckets).mean(axis=1)
    return pd.DataFrame({"top": top, "bottom": bottom}).where(count >= min_markets)


# --- the card ----------------------------------------------------------------------------------------------------------

@dataclass(slots=True)
class SignalCard:
    """The checks of one signal.

    Attributes:
        table: One row per check at the primary horizon: the value, what it is compared with, and whether it passes
            (blank for a number that is only reported).
        by_horizon: The same values for every horizon asked for, one column each.
        verdict, reason: The reading at the primary horizon, and why in one line.
        details: The series behind the plots: the IC by delay, by block and rolling, bucket edges, per-market ICs.
    """

    name: str
    kind: str
    horizons: tuple[int, ...]
    primary: int
    table: pd.DataFrame
    by_horizon: pd.DataFrame
    verdict: str
    reason: str
    standard: Standard
    details: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    def __repr__(self) -> str:
        return f"SignalCard({self.name!r}, {self.kind}, {self.primary} bars: {self.verdict}; {self.reason})"

    def to_markdown(self) -> str:
        """The card as a markdown page."""
        shown = self.table.copy()
        shown["passes"] = shown["passes"].map({True: "pass", False: "FAIL"}).fillna("")
        rows = ["| check | value | compared with | |", "| --- | --- | --- | --- |"]
        rows += [f"| {check} | {row['value']} | {row['compared with']} | {row['passes']} |" for check, row in shown.iterrows()]
        return "\n".join([f"# Signal card: {self.name}", "", f"{self.kind}, primary horizon {self.primary} bars; standard version {self.standard.version}.", "",
                          f"**{self.verdict}**: {self.reason}", "", *rows, ""])

    def plot(self) -> Any:
        """Six small charts: IC by horizon, by delay, by block and rolling, the bucket edges, and the placebo's spread."""
        import matplotlib.pyplot as plt

        figure, axes = plt.subplots(2, 3, figsize=(13, 6.5))
        values = self.by_horizon
        ax = axes[0, 0]
        centre = values.loc["IC"].astype(float)
        ax.errorbar(range(len(centre)), centre, yerr=[centre - values.loc["interval low"].astype(float), values.loc["interval high"].astype(float) - centre], fmt="o", capsize=3)
        ax.set_xticks(range(len(centre)), [str(column) for column in centre.index])
        ax.set_title("IC by horizon (bars), with its interval")
        delays = self.details["delay"]
        axes[0, 1].bar([str(item) for item in delays.index], delays.to_numpy())
        axes[0, 1].set_title("IC with the signal late (bars)")
        blocks = self.details["blocks"]
        axes[0, 2].bar([str(item) for item in blocks.index], blocks.to_numpy())
        axes[0, 2].set_title("IC by calendar block")
        axes[0, 2].tick_params(axis="x", rotation=60)
        self.details["rolling"].plot(ax=axes[1, 0], title="Rolling IC")
        edges = self.details["buckets"]
        axes[1, 1].bar([str(item) for item in edges.index], edges["edge_bps"].to_numpy())
        axes[1, 1].axhline(self.details["round_trip_bps"], linestyle="--", linewidth=1)
        axes[1, 1].axhline(-self.details["round_trip_bps"], linestyle="--", linewidth=1)
        axes[1, 1].set_title("Forward return by bucket, bps over average (dashed: one round trip)")
        ax = axes[1, 2]
        ax.bar(["IC", "placebo 95%", "scrambled 95%"], [abs(float(centre[self.primary])), self.details["placebo_95"], self.details["permutation_95"]])
        ax.set_title("IC against what luck gives")
        for ax in axes.flat:
            ax.axhline(0.0, linewidth=0.8, color="black")
        figure.suptitle(f"{self.name}: {self.verdict}")
        figure.tight_layout()
        return figure

    def save(self, directory: Path | str | None = None) -> Path:
        """Write the page, the tables and the charts; returns the folder."""
        out = Path(directory) if directory else CARD_DIR / f"{_slug(self.name)}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "card.md").write_text(self.to_markdown())
        self.table.to_csv(out / "table.csv")
        self.by_horizon.to_csv(out / "by_horizon.csv")
        figure = self.plot()
        figure.savefig(out / "card.png", dpi=120)
        import matplotlib.pyplot as plt

        plt.close(figure)
        return out


def _slug(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name.lower()).strip("_") or "signal"


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "no"
    return f"{value:.{digits}f}" if isinstance(value, (float, np.floating)) else str(value)


def _same_sign(value: float, reference: float) -> bool:
    return bool(np.isfinite(value) and np.isfinite(reference) and value * reference > 0)


def _checks_one(signal: pd.Series, forward: pd.Series, close: pd.Series | None, horizon: int, standard: Standard, direction: int | None,
                round_trip_bps: float, block: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(values, passes, details) of every check for one market at one horizon."""
    frame = _pairs(signal, forward)
    value = ic(signal, forward)
    n_eff = effective_n(signal, forward, horizon)
    low, high = ic_interval(value, n_eff, standard.level)
    placebo = shift_placebo(signal, forward, horizon)
    delays = delay_profile(signal, forward, sorted({0, standard.delay_bars, 2, 3, 5, 10}))
    blocks = ic_by_block(signal, forward, horizon, block)
    edges = bucket_edges(signal, forward, standard.buckets)
    sides = hit_and_spread(signal, forward, horizon)
    sign = float(np.sign(value)) if np.isfinite(value) else 0.0
    kept = float(delays[standard.delay_bars] / value) if np.isfinite(value) and value != 0 else float("nan")
    same = float((np.sign(blocks) == sign).mean()) if len(blocks) else float("nan")
    without = float("nan")
    if len(blocks) >= 2:
        best = (blocks * sign).idxmax()
        labels = np.asarray(block_of(pd.DatetimeIndex(frame.index), block))
        rest = frame[labels != best]
        without = ic(rest["signal"], rest["forward"])
    baseline = float("nan")
    if close is not None:
        trailing = np.log(close.astype(float)).diff(standard.baseline_lookback)
        baseline = ic(trailing, forward)
    edge = float("nan")
    if len(edges):
        top, bottom = float(edges["edge_bps"].iloc[-1]), float(edges["edge_bps"].iloc[0])
        edge = max(abs(top), abs(bottom)) if direction is None else (top if direction > 0 else -bottom)
    values = {"bars": len(frame), "independent observations": n_eff, "IC": value, "interval low": low, "interval high": high,
              "IC on non-overlapping bars": non_overlapping_ic(signal, forward, horizon), "placebo p": placebo["p"], "placebo 95%": placebo["placebo_95"],
              "hit rate": sides["hit_rate"], "spread bps": sides["spread_bps"], "spread t": sides["spread_t"],
              f"IC kept {standard.delay_bars} bar late": kept, "blocks": len(blocks), "blocks with the same sign": same,
              "IC without the best block": without, "baseline IC": baseline, "best bucket bps": edge}
    right_way = direction is None or sign == float(np.sign(direction))
    passes = {"independent observations": bool(n_eff >= standard.min_independent),
              "IC": bool(np.isfinite(low) and (low > 0 or high < 0) and right_way),
              "placebo p": bool(np.isfinite(placebo["p"]) and placebo["p"] < standard.placebo_p),
              f"IC kept {standard.delay_bars} bar late": bool(np.isfinite(kept) and kept >= standard.delay_keep),
              "blocks with the same sign": bool(len(blocks) >= standard.min_blocks and same >= standard.stable_share),
              "IC without the best block": _same_sign(without, value),
              "baseline IC": bool(abs(value) > abs(baseline)) if np.isfinite(baseline) and np.isfinite(value) else None,
              "best bucket bps": bool(np.isfinite(edge) and edge > round_trip_bps)}
    details = {"delay": delays, "blocks": blocks, "buckets": edges, "placebo_95": placebo["placebo_95"],
               "rolling": rolling_ic(signal, forward, max(min(len(frame) // 8, 2000), 30)), "permutation_95": permutation_95(signal, forward)}
    return values, passes, details


def _checks_panel(signal: pd.DataFrame, forward: pd.DataFrame, close: pd.DataFrame | None, horizon: int, standard: Standard, direction: int | None,
                  round_trip_bps: float, block: str | None, min_markets: int, seed: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(values, passes, details) of every check for a ranking of markets at one horizon."""
    lags = max(horizon - 1, 0)
    series = ic_by_date(signal, forward, min_markets).dropna()
    mean, error = newey_west_mean(series, lags)
    z = NormalDist().inv_cdf(0.5 + standard.level / 2.0)
    low, high = (mean - z * error, mean + z * error) if np.isfinite(error) else (float("nan"), float("nan"))
    spread_of_dates = float(series.std(ddof=1)) if len(series) > 2 else float("nan")
    n_eff = float(np.clip((spread_of_dates / error) ** 2, 1.0, len(series))) if error and np.isfinite(error) and error > 0 else float(len(series))
    sign = float(np.sign(mean)) if np.isfinite(mean) else 0.0

    count, away = len(signal), int(max(2 * horizon, 20))
    rng = np.random.default_rng(seed)
    shifts = rng.integers(away, max(away + 1, count - away), size=standard.placebo_runs) if count > 4 * away else np.array([], dtype=int)
    placebos = np.array([abs(float(ic_by_date(pd.DataFrame(np.roll(signal.to_numpy(), int(k), axis=0), index=signal.index, columns=signal.columns), forward, min_markets).mean()))
                         for k in shifts])
    p = float(np.mean(placebos >= abs(mean))) if len(placebos) else float("nan")
    placebo_95 = float(np.quantile(placebos, 0.95)) if len(placebos) else float("nan")

    delays = pd.Series({delay: float(ic_by_date(signal.shift(delay), forward, min_markets).mean()) for delay in sorted({0, standard.delay_bars, 2, 3, 5, 10})}, name="ic")
    kept = float(delays[standard.delay_bars] / mean) if np.isfinite(mean) and mean != 0 else float("nan")
    labels = np.asarray(block_of(pd.DatetimeIndex(series.index), block))
    grouped = series.groupby(labels)
    blocks = grouped.mean()[grouped.count() >= max(20, 3 * horizon)].rename("ic")
    same = float((np.sign(blocks) == sign).mean()) if len(blocks) else float("nan")
    without_block = float(series[labels != (blocks * sign).idxmax()].mean()) if len(blocks) >= 2 else float("nan")
    per_market = pd.Series({market: float(ic_by_date(signal.drop(columns=market), forward.drop(columns=market), min_markets).mean()) for market in signal.columns}) if signal.shape[1] > min_markets else pd.Series(dtype=float)
    without_market = float((per_market * sign).min() * sign) if len(per_market) else float("nan")
    groups = top_minus_rest(signal, forward, standard.buckets, min_markets).dropna()
    top, bottom = (float(groups["top"].mean() * BPS), float(groups["bottom"].mean() * BPS)) if len(groups) else (float("nan"), float("nan"))
    edge = max(abs(top), abs(bottom)) if direction is None else (top if direction > 0 else -bottom)
    spread = groups["top"] - groups["bottom"] if len(groups) else pd.Series(dtype=float)
    spread_mean, spread_error = newey_west_mean(spread, lags)
    baseline = float("nan")
    if close is not None:
        baseline = float(ic_by_date(np.log(close.astype(float)).diff(standard.baseline_lookback), forward, min_markets).mean())
    values = {"bars": len(series), "independent observations": n_eff, "IC": mean, "interval low": low, "interval high": high,
              "IC on non-overlapping bars": float(np.mean([series.iloc[start::horizon].mean() for start in range(max(horizon, 1))])) if len(series) else float("nan"),
              "placebo p": p, "placebo 95%": placebo_95, "hit rate": float((spread * sign > 0).mean()) if len(spread) else float("nan"),
              "spread bps": float(spread_mean * BPS) if np.isfinite(spread_mean) else float("nan"),
              "spread t": float(spread_mean / spread_error) if spread_error and np.isfinite(spread_error) else float("nan"),
              f"IC kept {standard.delay_bars} bar late": kept, "blocks": len(blocks), "blocks with the same sign": same,
              "IC without the best block": without_block, "IC without the best market": without_market, "baseline IC": baseline, "best bucket bps": edge}
    right_way = direction is None or sign == float(np.sign(direction))
    passes = {"independent observations": bool(n_eff >= standard.min_independent),
              "IC": bool(np.isfinite(low) and (low > 0 or high < 0) and right_way),
              "placebo p": bool(np.isfinite(p) and p < standard.placebo_p),
              f"IC kept {standard.delay_bars} bar late": bool(np.isfinite(kept) and kept >= standard.delay_keep),
              "blocks with the same sign": bool(len(blocks) >= standard.min_blocks and same >= standard.stable_share),
              "IC without the best block": _same_sign(without_block, mean),
              "IC without the best market": _same_sign(without_market, mean) if len(per_market) else None,
              "baseline IC": bool(abs(mean) > abs(baseline)) if np.isfinite(baseline) and np.isfinite(mean) else None,
              "best bucket bps": bool(np.isfinite(edge) and edge > round_trip_bps)}
    edges = pd.DataFrame({"bars": [len(groups), len(groups)], "edge_bps": [bottom, top]}, index=pd.Index(["bottom group", "top group"], name="bucket"))
    details = {"delay": delays, "blocks": blocks, "buckets": edges, "placebo_95": placebo_95, "permutation_95": float("nan"),
               "rolling": series.rolling(max(min(len(series) // 8, 500), 20), min_periods=10).mean().rename("rolling_ic"), "ic_by_date": series,
               "without_each_market": per_market}
    return values, passes, details


COMPARED = {"independent observations": "at least {min_independent:g}", "IC": "{level:.0%} interval excludes zero", "placebo p": "below {placebo_p:g}",
            "blocks with the same sign": "at least {stable_share:.0%} of {min_blocks}+ blocks", "IC without the best block": "same sign as the IC",
            "IC without the best market": "same sign as the IC", "baseline IC": "smaller in size than the IC", "best bucket bps": "above one round trip"}


def _verdict(values: Mapping[str, Any], passes: Mapping[str, Any], standard: Standard, direction: int | None) -> tuple[str, str]:
    if not passes["independent observations"]:
        return "short history", f"{values['independent observations']:.0f} independent observations; {standard.min_independent:g} are needed before anything is claimed"
    failed = [check for check, ok in passes.items() if ok is False]
    not_run = [check for check, ok in passes.items() if ok is None]
    if not passes["IC"] or not passes["placebo p"]:
        wrong = direction is not None and np.isfinite(values["IC"]) and np.sign(values["IC"]) != np.sign(direction)
        why = "the IC has the opposite sign to the one stated" if wrong else "the IC is not distinguishable from zero or from time-shifted placebos"
        return "no evidence", f"{why} (IC {values['IC']:+.3f}, interval {values['interval low']:+.3f} to {values['interval high']:+.3f}, placebo p {values['placebo p']:.2f})"
    if failed or not_run:
        parts = ([f"fails: {', '.join(failed)}"] if failed else []) + ([f"not checked: {', '.join(not_run)}"] if not_run else [])
        return "weak", f"distinguishable from luck (IC {values['IC']:+.3f}, placebo p {values['placebo p']:.2f}), but " + "; ".join(parts)
    return "worth a strategy test", f"every check passes (IC {values['IC']:+.3f}, interval {values['interval low']:+.3f} to {values['interval high']:+.3f})"


def _fingerprint(name: str, signal: Data, target: Data, horizons: Sequence[int], standard: Standard) -> str:
    digest = hashlib.sha256()
    for item in (signal, target):
        digest.update(pd.util.hash_pandas_object(item, index=True).to_numpy().tobytes())
    digest.update(repr((name, tuple(horizons), standard)).encode())
    return digest.hexdigest()[:16]


def _trim(item: Data | None) -> Data | None:
    if item is None:
        return None
    if not isinstance(item.index, pd.DatetimeIndex):
        raise TypeError("the signal and the prices need a DatetimeIndex")
    if isinstance(item, pd.Series):
        return governance.trim_frame(item.to_frame("value"), label="signal card")["value"].rename(item.name)
    return governance.trim_frame(item, label="signal card")


def signal_card(signal: Data, close: Data | None = None, *, horizons: Sequence[int] = (1,), name: str = "signal", forward: Data | None = None,
                primary: int | None = None, direction: int | None = None, round_trip_bps: float | None = None, family: str = "", block: str | None = None,
                min_markets: int = 5, standard: Standard | None = None, ledger: bool = True, seed: int = 0) -> SignalCard:
    """Run every check on one signal and give its card.

    Args:
        signal: The value known at each bar's close; a Series, or a DataFrame of dates by markets for a ranking.
        close: Prices on the same index. Forward returns over each horizon are taken from them.
        horizons: Holding periods in bars. Each one is a trial. The verdict is for `primary` (the first by default),
            chosen before looking, so the best of several horizons is never the one reported.
        name: What the ledger and the saved page call this signal.
        forward: The return that followed each signal, instead of `close`, when it is not a fixed number of bars.
            `horizons` then says how many observations each return spans (1 when they do not overlap).
        direction: +1 if a high signal should be followed by a high return, -1 for the opposite, None if not
            stated. With a stated direction the IC must have that sign and the cost check uses that side only.
        round_trip_bps: One trade in and out for this market, when it is not the standard's.
        family: The hypothesis this belongs to in the trial ledger (e.g. "H12").
        ledger: Write the card to the trial ledger (once per identical input). Tests switch it off.
    """
    standard = standard or load_standard()
    if (close is None) == (forward is None):
        raise ValueError("pass prices (close) or the realised forward returns (forward), one of them")
    horizons = tuple(int(h) for h in horizons)
    primary = int(primary) if primary is not None else horizons[0]
    if primary not in horizons or min(horizons) < 1:
        raise ValueError("horizons are whole bars of 1 or more, and the primary horizon must be one of them")
    signal, close, forward = _trim(signal), _trim(close), _trim(forward)
    panel = isinstance(signal, pd.DataFrame)
    cost = float(round_trip_bps) if round_trip_bps is not None else standard.round_trip_bps
    columns: dict[int, dict[str, Any]] = {}
    kept: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    for horizon in horizons:
        target = forward if forward is not None else forward_returns(close, horizon)
        target = target.reindex(signal.index)
        result = (_checks_panel(signal, target, close, horizon, standard, direction, cost, block, min_markets, seed) if panel
                  else _checks_one(signal, target, close, horizon, standard, direction, cost, block))
        columns[horizon] = result[0]
        if horizon == primary:
            kept = result
    assert kept is not None
    values, passes, details = kept
    verdict, reason = _verdict(values, passes, standard, direction)
    compared = {check: text.format(**{item.name: getattr(standard, item.name) for item in fields(standard)}) for check, text in COMPARED.items()}
    compared[f"IC kept {standard.delay_bars} bar late"] = f"at least {standard.delay_keep:.0%}"
    compared["best bucket bps"] = f"above one round trip ({cost:g} bps)"
    table = pd.DataFrame({"value": {check: _fmt(value) for check, value in values.items()},
                          "compared with": {check: compared.get(check, "") for check in values},
                          "passes": {check: passes.get(check) for check in values}})
    table.index.name = "check"
    details["round_trip_bps"] = cost
    if panel:
        target = forward if forward is not None else forward_returns(close, primary)
        details["per_market"] = pd.DataFrame({market: {"IC": ic(signal[market], target[market]), "independent observations": effective_n(signal[market], target[market], primary)}
                                              for market in signal.columns}).T
    card = SignalCard(name=name, kind="ranking of markets" if panel else "one market", horizons=horizons, primary=primary, table=table,
                      by_horizon=pd.DataFrame(columns), verdict=verdict, reason=reason, standard=standard, details=details,
                      fingerprint=_fingerprint(name, signal, forward if forward is not None else close, horizons, standard))
    if ledger:
        _log(card, family)
    return card


def _log(card: SignalCard, family: str) -> None:
    """One ledger entry per card; the same signal on the same data is not counted twice."""
    study = f"signal_card:{card.name}"
    if any(entry.get("study") == study and (entry.get("details") or {}).get("fingerprint") == card.fingerprint for entry in governance.ledger_entries()):
        return
    governance.record_trials(study, len(card.horizons), family=family or card.name, data=card.kind,
                             details={"fingerprint": card.fingerprint, "verdict": card.verdict, "primary": card.primary, "horizons": list(card.horizons),
                                      "ic": float(card.by_horizon.at["IC", card.primary]), "standard_version": card.standard.version})


# --- the gate before a strategy test -----------------------------------------------------------------------------------

class CardGate(RuntimeError):
    """A strategy test was started for a signal whose card does not allow it."""


def require_card(name: str, hypothesis: str | None = None) -> dict[str, Any]:
    """The latest ledger entry of this signal's card; raises unless it lets a strategy test go ahead.

    "No evidence" blocks the test. The owner can override it by writing a line that starts with
    "SIGNAL CARD OVERRIDE:" and gives the reason into the hypothesis's pre-registration before the test is run; the
    use of an override is then written to the ledger. A missing card or one with "short history" blocks as well.
    """
    study = f"signal_card:{name}"
    entries = [entry for entry in governance.ledger_entries() if entry.get("study") == study]
    if not entries:
        raise CardGate(f"no signal card for {name!r}: run signal_checks.signal_card(..., name={name!r}) first")
    details = dict(entries[-1].get("details") or {})
    if details.get("verdict") in ("worth a strategy test", "weak"):
        return details
    text = governance.prereg_path(hypothesis).read_text() if hypothesis and governance.prereg_path(hypothesis).exists() else ""
    override = next((line.strip() for line in text.splitlines() if line.strip().upper().startswith(OVERRIDE_MARK) and line.split(":", 1)[-1].strip()), None)
    if override is None:
        raise CardGate(f"the signal card of {name!r} says '{details.get('verdict')}': no strategy test, unless the pre-registration "
                       f"{'of ' + hypothesis + ' ' if hypothesis else ''}has a line '{OVERRIDE_MARK}: <reason>'")
    governance._append({"type": "signal_card_override", "study": f"signal_card_override:{name}", "family": hypothesis or name,  # not a trial: a record that the gate was passed by hand
                        "details": {"override": override, "verdict": details.get("verdict")}})
    return {**details, "override": override}
