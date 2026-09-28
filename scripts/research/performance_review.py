"""Review a running portfolio (paper or live) against buy-and-hold, from the snapshots it wrote to SQLite.

    python scripts/research/performance_review.py                          # every portfolio in the database
    python scripts/research/performance_review.py --portfolio btc-live --since 2026-10-01

Each runtime cycle logs a snapshot (equity, and every instrument's mark, position, fees, funding and realized P&L).
From those alone, with no market-data download, this prints:

- the book's return, volatility, max drawdown and Sharpe next to buy-and-hold of its benchmark instrument (BTC by
  default) over the same days;
- beta, alpha, correlation, information ratio and up/down capture against it (daily returns);
- costs and P&L per instrument (fees, funding, realized) and P&L attributed to each sleeve.

Daily samples need weeks to mean much: with 30 days, a beta or alpha is a rough indication, not a measurement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from src.portfolio.review import review  # noqa: E402
from src.storage.trade_logger import TradeLogger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--portfolio", default=None, help="Portfolio name (default: every portfolio in the database)")
    parser.add_argument("--since", default=None, help="Only snapshots from this UTC date/time")
    parser.add_argument("--benchmark", default=None, help="Instrument id for buy-and-hold (default: the book's BTC instrument)")
    parser.add_argument("--database", default=None, help="SQLite path (default: settings.database_path)")
    args = parser.parse_args()

    logger = TradeLogger(database_path=args.database or settings.database_path)
    snapshots = logger.list_portfolio_snapshots(portfolio=args.portfolio)
    if args.since:
        since = pd.Timestamp(args.since, tz="UTC")
        snapshots = [snap for snap in snapshots if pd.Timestamp(snap["timestamp"]) >= since]
    if not snapshots:
        print("No portfolio snapshots found. They are written by a running --portfolio runtime (paper, live_dry_run or live).")
        return
    names = sorted({str(snap.get("portfolio")) for snap in snapshots})
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.float_format", "{:.4f}".format):
        for name in names:
            tables = review([snap for snap in snapshots if str(snap.get("portfolio")) == name], benchmark=args.benchmark)
            print(f"\n=== {name}: {tables['window']}")
            for title in ("summary", "versus", "instruments", "sleeves"):
                print(f"\n{title}:\n{tables[title].to_string()}")


if __name__ == "__main__":
    main()
