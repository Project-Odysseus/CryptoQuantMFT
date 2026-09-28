"""Review a running portfolio against buy-and-hold from its SQLite snapshots (`TradeLogger.list_portfolio_snapshots`).

Used by `scripts/research/performance_review.py`. Each runtime cycle stores equity and every instrument's mark, so the
benchmark needs no separate market data: its prices are the book's own marks on the same timestamps.

Kill criteria (`ReviewConfig`, the config's `[review]` table) are written before a book goes live, like a
pre-registration: each sleeve's and the book's backtest Sharpe, and the book's backtest max drawdown. The review
flags, it never disables:

- **Decay:** a live Sharpe more than `decay_z` standard errors below the backtest's, once `min_days` of live history
  exist. The standard error of a Sharpe measured over T days is about sqrt((1 + SR_d^2 / 2) / T) per day (Lo, 2002),
  so six months can't tell a Sharpe of 0.7 from 0: a raw "negative Sharpe" rule would mostly flag noise.
- **Floor:** a live Sharpe below 0 after `floor_days`.
- **Drawdown:** the book's live drawdown beyond `max_drawdown_multiple` times the backtest's worst (well before the
  `[risk] max_drawdown` kill).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.research.bar_engine import performance
from src.research.benchmark import benchmark_metrics


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    """Pre-registered expectations for a live book and the rules that flag it (see the module docstring)."""

    backtest_sharpe: dict[str, float] = field(default_factory=dict)  # sleeve id -> the Sharpe live results are held to
    book_backtest_sharpe: float | None = None
    book_backtest_max_drawdown: float | None = None
    source: str = ""  # where the numbers came from (a research run), for the record
    min_days: int = 90
    floor_days: int = 365
    decay_z: float = 2.0
    max_drawdown_multiple: float = 1.5

    def __post_init__(self) -> None:
        """Keep the rules meaningful."""
        if self.min_days < 20 or self.floor_days < self.min_days:
            raise ValueError("review.min_days must be at least 20 and review.floor_days at least min_days")
        if self.decay_z <= 0 or self.max_drawdown_multiple <= 0:
            raise ValueError("review.decay_z and review.max_drawdown_multiple must be above 0")
        if self.book_backtest_max_drawdown is not None and not 0 < self.book_backtest_max_drawdown < 1:
            raise ValueError("review.book_backtest_max_drawdown must be a fraction between 0 and 1")


def sharpe_standard_error(annual_sharpe: float, days: int) -> float:
    """Standard error of an annualised Sharpe estimated from `days` daily returns (iid approximation, Lo 2002)."""
    daily = annual_sharpe / np.sqrt(365.0)
    return float(np.sqrt((1.0 + daily**2 / 2.0) / max(days, 1)) * np.sqrt(365.0))


def kill_criteria(snapshots: list[dict], review: ReviewConfig) -> pd.DataFrame:
    """One row per sleeve with a reference Sharpe, plus the book: live Sharpe, the gap in standard errors, and a status."""
    ordered = sorted(snapshots, key=lambda snap: pd.Timestamp(snap["timestamp"]))
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for snap in ordered:
        stamp = pd.Timestamp(snap["timestamp"])
        stamp = stamp.tz_convert("UTC") if stamp.tzinfo else stamp.tz_localize("UTC")
        rows[stamp] = {"equity": float(snap["equity"]), **{f"pnl:{sid}": float(info.get("pnl") or 0.0) for sid, info in (snap.get("sleeves") or {}).items()}}
    daily = pd.DataFrame.from_dict(rows, orient="index").sort_index().resample("1D").last().dropna(subset=["equity"])
    previous_equity = daily["equity"].shift(1)
    series = {"book": daily["equity"].pct_change()}
    for sleeve_id in review.backtest_sharpe:
        column = f"pnl:{sleeve_id}"
        series[sleeve_id] = daily[column].diff() / previous_equity if column in daily else pd.Series(dtype=float)
    out = []
    for name, values in series.items():
        values = values.dropna()
        days = len(values)
        reference = review.book_backtest_sharpe if name == "book" else review.backtest_sharpe.get(name)
        sharpe = float(values.mean() / values.std(ddof=1) * np.sqrt(365.0)) if days > 2 and values.std(ddof=1) > 0 else float("nan")
        row: dict[str, Any] = {"series": name, "days": days, "live_sharpe": sharpe, "backtest_sharpe": reference, "z": float("nan"), "status": "ok"}
        if reference is not None and np.isfinite(sharpe):
            row["z"] = (sharpe - reference) / sharpe_standard_error(reference, days)
        flags = []
        if days < review.min_days:
            row["status"] = f"too early ({days} of {review.min_days} days)"
        else:
            if np.isfinite(row["z"]) and row["z"] < -review.decay_z:
                flags.append(f"decay: {row['z']:.1f} standard errors below the backtest")
            if days >= review.floor_days and np.isfinite(sharpe) and sharpe < 0:
                flags.append(f"negative Sharpe after {days} days")
        if name == "book" and review.book_backtest_max_drawdown is not None and len(daily):
            drawdown = float((1.0 - daily["equity"] / daily["equity"].cummax()).max())
            row["live_max_drawdown"] = drawdown
            if drawdown > review.max_drawdown_multiple * review.book_backtest_max_drawdown:
                flags.append(f"drawdown {drawdown:.0%} beyond {review.max_drawdown_multiple:g}x the backtest's {review.book_backtest_max_drawdown:.0%}")
        if flags:
            row["status"] = "FLAG: " + "; ".join(flags)
        out.append(row)
    return pd.DataFrame(out).set_index("series")


def snapshot_frame(snapshots: list[dict]) -> pd.DataFrame:
    """One row per snapshot: equity and each instrument's mark, oldest first."""
    rows = []
    for snap in snapshots:
        row = {"timestamp": pd.Timestamp(snap["timestamp"]), "equity": float(snap["equity"])}
        for instrument, info in (snap.get("instruments") or {}).items():
            price = info.get("price")
            row[f"price:{instrument}"] = float(price) if price else np.nan  # 0 means "no mark yet"
        rows.append(row)
    frame = pd.DataFrame(rows).set_index("timestamp").sort_index()
    frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC") if frame.index.tz is not None else pd.DatetimeIndex(frame.index).tz_localize("UTC")
    return frame


def review(snapshots: list[dict], *, benchmark: str | None = None, criteria: ReviewConfig | None = None) -> dict[str, pd.DataFrame | str]:
    """The review tables for one portfolio's snapshots (newest-first or oldest-first, any order)."""
    frame = snapshot_frame(snapshots)
    prices = [column for column in frame.columns if column.startswith("price:")]
    if not prices:
        raise ValueError("snapshots carry no instrument prices")
    chosen = benchmark or next((c.removeprefix("price:") for c in prices if "BTC" in c.upper() or "XBT" in c.upper()), prices[0].removeprefix("price:"))
    daily = frame.resample("1D").last().dropna(subset=["equity"])
    book = daily["equity"].pct_change().dropna()
    bench = daily[f"price:{chosen}"].replace(0.0, np.nan).pct_change().dropna()
    book_perf, bench_perf = performance(book, periods_per_year=365), performance(bench, periods_per_year=365)
    summary = pd.DataFrame({
        "book": {"days": len(book), "total_return": float(daily["equity"].iloc[-1] / daily["equity"].iloc[0] - 1), **book_perf},
        f"buy-and-hold {chosen}": {"days": len(bench), "total_return": float(daily[f"price:{chosen}"].iloc[-1] / daily[f"price:{chosen}"].iloc[0] - 1), **bench_perf},
    })
    versus = pd.Series(benchmark_metrics(book, bench), name=f"book vs {chosen}")

    latest = max(snapshots, key=lambda snap: pd.Timestamp(snap["timestamp"]))
    instruments = pd.DataFrame({name: {key: info.get(key) for key in ("units", "weight", "price", "realized_pnl", "fees", "funding")}
                                for name, info in (latest.get("instruments") or {}).items()}).T
    sleeves = pd.DataFrame({name: {key: info.get(key) for key in ("instrument", "strategy", "own_weight", "allocated_weight", "pnl", "last_action", "disabled")}
                            for name, info in (latest.get("sleeves") or {}).items()}).T
    window = f"{frame.index.min():%Y-%m-%d %H:%M} to {frame.index.max():%Y-%m-%d %H:%M} UTC, {len(frame)} snapshots"
    tables: dict[str, pd.DataFrame | str] = {"window": window, "summary": summary, "versus": versus.to_frame(), "instruments": instruments, "sleeves": sleeves}
    if criteria is not None:
        tables["kill criteria"] = kill_criteria(snapshots, criteria)
    return tables
