"""Review a running portfolio against buy-and-hold from its SQLite snapshots (`TradeLogger.list_portfolio_snapshots`).

Used by `scripts/research/performance_review.py`. Each runtime cycle stores equity and every instrument's mark, so the
benchmark needs no separate market data: its prices are the book's own marks on the same timestamps.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bar_engine import performance
from src.research.benchmark import benchmark_metrics


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


def review(snapshots: list[dict], *, benchmark: str | None = None) -> dict[str, pd.DataFrame | str]:
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
    return {"window": window, "summary": summary, "versus": versus.to_frame(), "instruments": instruments, "sleeves": sleeves}
