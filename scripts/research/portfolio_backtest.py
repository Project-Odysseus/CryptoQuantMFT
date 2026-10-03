"""Backtest a whole portfolio config: each sleeve alone, then the book under each allocation method.

Runs the config's sleeves over cached Kraken history (perps from 2020), with
each instrument's fees and slippage from the config and a flat funding rate
on perps. Then it prints:

1. each sleeve alone at full size (its own sizing, no portfolio risk
   limits), in-sample and holdout;
2. the book under `fixed`, `equal` and `inverse_vol` allocation with the
   config's risk limits, plus the config's own method without them;
3. the correlation of the sleeves' daily returns (two sleeves on BTC and
   ETH are not two independent bets);
4. each sleeve and book against buy-and-hold of the benchmark instrument
   (BTC by default): beta, alpha, correlation, information ratio and
   up/down capture, in-sample and holdout.

Choose the allocation by in-sample numbers. The holdout (from
--holdout-start, the split the perp studies in docs/research_log.md use)
only checks that choice.

Usage:
    python scripts/research/portfolio_backtest.py config/portfolio.example.toml
    python scripts/research/portfolio_backtest.py config/portfolio.example.toml --funding-pct-per-day 0.03

Writes data/research/portfolio_<timestamp>/ (sleeves.csv, books.csv,
correlation.csv, benchmark.csv, weights.csv, equity.csv).
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
from src.portfolio.book_risk import estimate_stream, exposure_history, exposure_summary, format_exposure
from src.portfolio.config import load_portfolio_config
from src.portfolio.risk_model import average_correlation, effective_bets
from src.research.benchmark import benchmark_metrics
from src.research.governance import write_manifest


def _table(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    return frame.pivot_table(index="book", columns="period", values=["sharpe", "cagr", "max_drawdown", "avg_gross_exposure"], sort=False)[
        [("sharpe", "is"), ("sharpe", "ho"), ("cagr", "is"), ("cagr", "ho"), ("max_drawdown", "is"), ("max_drawdown", "ho"), ("avg_gross_exposure", "is")]
    ]


def _benchmark_rows(named: dict[str, pd.Series], benchmark: pd.Series, holdout: pd.Timestamp) -> pd.DataFrame:
    """Benchmark metrics per series and period ("is" before the holdout, "ho" from it)."""
    rows = []
    for name, series in named.items():
        for period, part in (("is", series[series.index < holdout]), ("ho", series[series.index >= holdout])):
            metrics = benchmark_metrics(part, benchmark)
            rows.append({"series": name, "period": period, **{key: metrics[key] for key in ("beta", "alpha_ann", "correlation", "information_ratio", "up_capture", "down_capture")}})
    return pd.DataFrame(rows)


def _default_benchmark(instruments: list[str]) -> str:
    """BTC's instrument if the book has one, else the first instrument."""
    return next((instrument for instrument in instruments if "BTC" in instrument.upper() or "XBT" in instrument.upper()), instruments[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="Portfolio TOML file")
    parser.add_argument("--holdout-start", default="2024-10-01", help="First day of the holdout (UTC)")
    parser.add_argument("--funding-pct-per-day", type=float, default=0.01, help="Funding longs pay on perps, %% of notional per day")
    parser.add_argument("--benchmark", default=None, help="Instrument id whose buy-and-hold is the benchmark (default: the book's BTC instrument)")
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
    units: dict[str, list[str]] = {}  # allocation unit -> its sleeves (a basket runs alone as one unit, with all its members)
    for sleeve_id in inputs.sleeve_weights.columns:
        units.setdefault(inputs.sleeve_groups.get(sleeve_id, sleeve_id), []).append(sleeve_id)
    for unit, members in units.items():
        alone = run_book(config, inputs, allocation="equal", sleeves=members, risk_overlay=False, funding_pct_per_day=args.funding_pct_per_day)
        sleeve_rows += period_metrics(alone, holdout, label=unit)
        sleeve_returns[unit] = daily_returns(alone)

    book_rows: list[dict[str, object]] = []
    books: dict[str, PortfolioBacktest] = {}
    methods = ALLOCATION_METHODS if not config.baskets else tuple(method for method in ALLOCATION_METHODS if method != "inverse_vol")
    for method in methods:
        books[method] = run_book(config, inputs, allocation=method, funding_pct_per_day=args.funding_pct_per_day)
        book_rows += period_metrics(books[method], holdout)
    unlimited = run_book(config, inputs, risk_overlay=False, funding_pct_per_day=args.funding_pct_per_day)
    book_rows += period_metrics(unlimited, holdout, label=f"{config.allocation} without risk limits")

    correlation = pd.DataFrame(sleeve_returns).corr()
    benchmark_id = args.benchmark or _default_benchmark(list(inputs.prices.columns))
    benchmark = inputs.prices[benchmark_id].resample("1D").last().pct_change().dropna()
    versus = _benchmark_rows(sleeve_returns | {f"book: {name}": daily_returns(book) for name, book in books.items()}, benchmark, holdout)
    out = args.out or Path("data/research") / f"portfolio_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    write_manifest(out, args=args)  # commit, arguments and frozen-holdout state, for reproducing the run
    pd.DataFrame(sleeve_rows).to_csv(out / "sleeves.csv", index=False)
    pd.DataFrame(book_rows).to_csv(out / "books.csv", index=False)
    correlation.to_csv(out / "correlation.csv")
    versus.to_csv(out / "benchmark.csv", index=False)
    main_book = books[config.allocation]
    main_book.targets.to_csv(out / "weights.csv")
    # What the book was exposed to, daily, from the positions it held after the risk limits
    per_day = max(1, round(inputs.bars_per_day))
    exposure = exposure_history(config, inputs.grid_interval, inputs.prices, main_book.result.weights, warmup_prices=inputs.warmup_prices, every=per_day)
    exposure.to_csv(out / "exposure.csv")
    held = main_book.result.weights
    last_estimate = estimate_stream(config, inputs.grid_interval, inputs.prices, warmup_prices=inputs.warmup_prices)(len(held) - 1)
    latest = exposure_summary(held.iloc[-1].to_dict(), config=config, estimate=last_estimate, units=inputs.sleeve_groups,
                              sleeve_weights={sleeve_id: (inputs.sleeve_instrument[sleeve_id], float(main_book.allocated[sleeve_id].iloc[-1])) for sleeve_id in main_book.allocated.columns})
    limits = {"groups": config.risk.groups, "max_beta_exposure": config.risk.max_beta_exposure, "max_portfolio_vol": config.risk.max_portfolio_vol}
    pd.DataFrame({name: book.result.equity for name, book in books.items()}).to_csv(out / "equity.csv")

    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print("\nEach sleeve alone at full size (no portfolio risk limits):")
        print(_table(sleeve_rows).round(2).to_string())
        print(f"\nBooks (with the config's risk limits unless noted; the config uses '{config.allocation}'):")
        print(_table(book_rows).round(2).to_string())
        print("\nCorrelation of the sleeves' daily returns:")
        print(correlation.round(2).to_string())
        if len(correlation) > 1:
            print(f"The {len(correlation)} sleeves are worth {effective_bets(correlation.fillna(0.0)):.1f} independent bets; average correlation {average_correlation(correlation.fillna(0.0)):+.2f}")
        print(f"\nExposure of the '{config.allocation}' book after its risk limits (daily; volatility is annualised, 'stressed' at crash correlations):")
        print(exposure.describe().loc[["mean", "50%", "max"]].rename(index={"50%": "median"}).T.round(2).to_string())
        print(f"\nAt the last bar ({held.index[-1]:%Y-%m-%d %H:%M}):")
        for line in format_exposure(latest, limits=limits):
            print(f"  {line}")
        print(f"\nAgainst buy-and-hold of {benchmark_id} (daily returns; alpha is annualised):")
        print(versus.pivot_table(index="series", columns="period", values=["beta", "alpha_ann", "down_capture"], sort=False).round(2).to_string())
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
