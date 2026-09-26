"""Backtest a whole portfolio config: each sleeve alone, then the book under each allocation method.

Runs the config's sleeves over cached Kraken history (perps from 2020), with
each instrument's fees and slippage from the config and a flat funding rate
on perps. Then it prints:

1. each sleeve alone at full size (its own sizing, no portfolio risk
   limits), in-sample and holdout;
2. the book under `fixed`, `equal` and `inverse_vol` allocation with the
   config's risk limits, plus the config's own method without them;
3. the correlation of the sleeves' daily returns (two sleeves on BTC and
   ETH are not two independent bets).

Choose the allocation by in-sample numbers. The holdout (from
--holdout-start, the split the perp studies in docs/research_log.md use)
only checks that choice.

Usage:
    python scripts/research/portfolio_backtest.py config/portfolio.example.toml
    python scripts/research/portfolio_backtest.py config/portfolio.example.toml --funding-pct-per-day 0.03

Writes data/research/portfolio_<timestamp>/ (sleeves.csv, books.csv,
correlation.csv, weights.csv, equity.csv).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from src.portfolio.allocation import ALLOCATION_METHODS
from src.portfolio.backtest import PortfolioBacktest, daily_returns, period_metrics, prepare_inputs, run_book
from src.portfolio.config import load_portfolio_config


def _table(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    return frame.pivot_table(index="book", columns="period", values=["sharpe", "cagr", "max_drawdown", "avg_gross_exposure"], sort=False)[
        [("sharpe", "is"), ("sharpe", "ho"), ("cagr", "is"), ("cagr", "ho"), ("max_drawdown", "is"), ("max_drawdown", "ho"), ("avg_gross_exposure", "is")]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="Portfolio TOML file")
    parser.add_argument("--holdout-start", default="2024-10-01", help="First day of the holdout (UTC)")
    parser.add_argument("--funding-pct-per-day", type=float, default=0.01, help="Funding longs pay on perps, %% of notional per day")
    parser.add_argument("--out", type=Path, default=None, help="Output folder (default data/research/portfolio_<timestamp>)")
    args = parser.parse_args()

    config = load_portfolio_config(args.config)
    holdout = pd.Timestamp(args.holdout_start, tz="UTC")
    inputs = prepare_inputs(config)
    print(f"Portfolio '{config.name}': {len(inputs.sleeve_weights.columns)} sleeves on {len(inputs.prices.columns)} instruments, "
          f"{inputs.grid_interval} grid, {inputs.measure_start:%Y-%m-%d} to {inputs.prices.index[-1]:%Y-%m-%d}, holdout from {holdout:%Y-%m-%d}; "
          f"funding {args.funding_pct_per_day:g}%/day on perps")

    sleeve_rows: list[dict[str, object]] = []
    sleeve_returns: dict[str, pd.Series] = {}
    for sleeve_id in inputs.sleeve_weights.columns:
        alone = run_book(config, inputs, allocation="equal", sleeves=[sleeve_id], risk_overlay=False, funding_pct_per_day=args.funding_pct_per_day)
        sleeve_rows += period_metrics(alone, holdout, label=sleeve_id)
        sleeve_returns[sleeve_id] = daily_returns(alone)

    book_rows: list[dict[str, object]] = []
    books: dict[str, PortfolioBacktest] = {}
    for method in ALLOCATION_METHODS:
        books[method] = run_book(config, inputs, allocation=method, funding_pct_per_day=args.funding_pct_per_day)
        book_rows += period_metrics(books[method], holdout)
    unlimited = run_book(config, inputs, risk_overlay=False, funding_pct_per_day=args.funding_pct_per_day)
    book_rows += period_metrics(unlimited, holdout, label=f"{config.allocation} without risk limits")

    correlation = pd.DataFrame(sleeve_returns).corr()
    out = args.out or Path("data/research") / f"portfolio_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(sleeve_rows).to_csv(out / "sleeves.csv", index=False)
    pd.DataFrame(book_rows).to_csv(out / "books.csv", index=False)
    correlation.to_csv(out / "correlation.csv")
    main_book = books[config.allocation]
    main_book.targets.to_csv(out / "weights.csv")
    pd.DataFrame({name: book.result.equity for name, book in books.items()}).to_csv(out / "equity.csv")

    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print("\nEach sleeve alone at full size (no portfolio risk limits):")
        print(_table(sleeve_rows).round(2).to_string())
        print(f"\nBooks (with the config's risk limits unless noted; the config uses '{config.allocation}'):")
        print(_table(book_rows).round(2).to_string())
        print("\nCorrelation of the sleeves' daily returns:")
        print(correlation.round(2).to_string())
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
