"""Kraken's measured trading costs against a portfolio config's assumed slippage.

    python scripts/research/kraken_costs.py config/portfolio.multi_paper.toml [--size 1000]

Reads what scripts/collectors/kraken_spreads.py has recorded and prints, per coin: the median and 90th-percentile
half-spread, the order-book cost of a market order of `--size` USD, and the config's `slippage_bps`. A ratio above 1
means the backtest and the paper exchange have been charging less than the market does. It reads only local files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data import kraken_spreads  # noqa: E402
from src.data.kraken_futures import venue_symbol_for  # noqa: E402
from src.portfolio.config import load_portfolio_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config")
    parser.add_argument("--size", type=float, default=1_000.0, help="Order size in USD for the depth cost (recorded: 100, 1000, 5000)")
    args = parser.parse_args()
    config = load_portfolio_config(args.config)
    assumed = {venue_symbol_for(spec.symbol): spec.slippage_bps for spec in config.instruments.values() if spec.venue == "kraken_futures" and spec.kind == "perp"}
    table = kraken_spreads.cost_table(assumed, size_usd=args.size)
    if table.empty:
        print("Nothing recorded yet: run scripts/collectors/kraken_spreads.py first.")
        return
    tickers = kraken_spreads.load("tickers")
    span = (tickers["time"].max() - tickers["time"].min()).total_seconds() / 3600.0
    print(f"{len(table)} coins, {tickers['time'].nunique()} spread samples over {span:.1f} hours ({tickers['time'].min():%Y-%m-%d %H:%M} to {tickers['time'].max():%Y-%m-%d %H:%M} UTC)\n")
    with pd.option_context("display.width", 220, "display.max_rows", 100, "display.float_format", lambda value: f"{value:,.1f}"):
        print(table.drop(columns=["volume_quote_24h"]).assign(volume_musd=table["volume_quote_24h"] / 1e6).to_string())
    worse = table[table["ratio"] > 1.0]
    print(f"\nMeasured cost above the assumption on {len(worse)} of {len(table)} coins; median ratio {table['ratio'].median():.2f} "
          f"(measured {table['measured_bps'].median():.1f} bps per side against an assumed {table['assumed_bps'].median():.1f}).")
    if span < 72:
        print("Fewer than three days of samples: spreads widen in fast markets, so wait for more before changing the config.")


if __name__ == "__main__":
    main()
