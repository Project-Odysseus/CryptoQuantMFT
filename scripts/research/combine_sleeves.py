"""Phase 6 (development data only): does the one H1 survivor, timed carry, diversify the live trend book?

    python scripts/research/combine_sleeves.py

Compares daily returns of the live book's sleeves (BTC and ETH, EWMA entry sizing, Kraken perp costs, H3's replica)
with timed carry (pooled BTC/ETH/SOL) at Binance fees (14-day hold) and at Kraken fees (30-day hold). The carry is
added at 30% of AUM notional per coin (the per-trade cap), which ties up 45% of AUM per coin in spot plus 2x perp
margin, so only two coins fit next to anything else. Not a new trial: no parameters are chosen here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research.bar_engine import by_year, performance  # noqa: E402
from src.research.costs import VENUES  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.hypotheses.h1_crowding import build_signals, prepare_market, run_carry  # noqa: E402
from src.research.hypotheses.h3_vrp import daily, prepare_sleeves, sleeve_run, vrp_frame  # noqa: E402

YEAR = 365.0


def main() -> None:
    trend = {}
    for coin in ("BTC", "ETH"):
        for data in prepare_sleeves(coin, vrp_frame(coin), VENUES["kraken_perp"]):
            trend[f"{coin}:{data.sleeve}"] = daily(sleeve_run(data, "ewma").net)
    trend_frame = pd.DataFrame(trend)
    start = trend_frame.apply(lambda s: s.ne(0).idxmax()).max()
    book = trend_frame.loc[start:].fillna(0.0).mean(axis=1).rename("trend_book")

    carry = {}
    for fees, hold in (("binance", 14), ("kraken", 30)):
        parts = {}
        for coin in ("BTC", "ETH", "SOL"):
            signals = build_signals(coin)
            frame, _ = run_carry(signals, prepare_market(coin, signals, VENUES), VENUES, fees=fees, expected_hold_days=hold)
            parts[coin] = frame["net"][signals["F_ann"].notna().reindex(frame.index).fillna(False)]
        carry[f"carry_{fees}_{hold}d"] = pd.DataFrame(parts).mean(axis=1)
    frame = pd.concat([book, pd.DataFrame(carry)], axis=1).dropna()
    frame.index = pd.DatetimeIndex(frame.index)

    rows = []
    for name in frame:
        rows.append({"series": name, **performance(frame[name], periods_per_year=YEAR)})
    for name in carry:
        # carry at 30% of AUM per coin on 3 coins = 0.9 notional; the pooled series is per unit notional per coin
        combined = frame["trend_book"] + 0.9 * frame[name]
        rows.append({"series": f"trend_book + {name} (0.3 notional x 3 coins)", **performance(combined, periods_per_year=YEAR)})
    stats = pd.DataFrame(rows)
    stats[["ann_return", "ann_vol", "max_drawdown", "worst_month"]] *= 100
    corr = frame.corr()
    down = frame[frame["trend_book"] < frame["trend_book"].quantile(0.1)].corr()
    years = pd.concat({name: by_year(frame[name], periods_per_year=YEAR)["return"] * 100 for name in frame}, axis=1)
    out = ["# Phase 6: carry vs the live trend book (development data, holdout locked)", "", f"Common window {frame.index.min():%Y-%m-%d} to {frame.index.max():%Y-%m-%d}.", "",
           md_table(stats, index=False), "", "Daily return correlation:", "", md_table(corr), "", "Correlation on the trend book's worst 10% of days:", "", md_table(down), "",
           "Return by year (%):", "", md_table(years), ""]
    print("\n".join(out))
    Path("data/research").mkdir(parents=True, exist_ok=True)
    Path("data/research/combine_latest.md").write_text("\n".join(out))


if __name__ == "__main__":
    main()
