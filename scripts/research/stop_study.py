"""Do the risk exits help? Stop variants on every sleeve of a portfolio config, through the research backtester.

The single-strategy runtime (`build_runtime_orchestrator` in main.py) has position exits (a percentage stop, a
time stop, a fixed or trailing ATR stop) and two account breakers: a daily loss limit and a hard stop on the
drawdown below the starting equity. Either breaker shuts the runtime down. This study:

1. applies each exit variant to every sleeve of the config and reports each sleeve alone and the whole book,
   in-sample and in the holdout (the same `run_sleeve` / `run_book` code the runtime uses);
2. replays each sleeve the way the runtime sizes it (a position worth `--position-fraction` of equity, default
   0.10 as `--risk-per-trade-pct`) and reports, for a runtime started on any day in the history, the share of
   starts where the hard stop at each threshold would have shut it down within a year, and how many days lost
   more than the daily limit.

Usage:
    python scripts/research/stop_study.py config/portfolio.btc_live.toml
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from src.portfolio.backtest import period_metrics, prepare_inputs, run_book
from src.portfolio.config import PortfolioConfig, load_portfolio_config

RUNTIME_DEFAULTS = {"position_drawdown_stop_pct": 0.05, "time_stop_bars": 60, "atr_stop_multiplier": 3.0}
VARIANTS: dict[str, dict[str, object]] = {
    "no stops": {},
    "runtime defaults": RUNTIME_DEFAULTS,
    "5% stop": {"position_drawdown_stop_pct": 0.05},
    "10% stop": {"position_drawdown_stop_pct": 0.10},
    "60-bar time stop": {"time_stop_bars": 60},
    "3 ATR fixed": {"atr_stop_multiplier": 3.0},
    "3 ATR trailing": {"atr_stop_multiplier": 3.0, "atr_trailing": True},
    "5 ATR trailing": {"atr_stop_multiplier": 5.0, "atr_trailing": True},
    "3 ATR trailing + 5% stop": {"atr_stop_multiplier": 3.0, "atr_trailing": True, "position_drawdown_stop_pct": 0.05},
    "3 ATR trailing + 10% stop": {"atr_stop_multiplier": 3.0, "atr_trailing": True, "position_drawdown_stop_pct": 0.10},
}


def with_stops(config: PortfolioConfig, stops: dict[str, object]) -> PortfolioConfig:
    """The config with every sleeve's exits replaced by `stops`."""
    return replace(config, sleeves=tuple(replace(sleeve, stops=dict(stops)) for sleeve in config.sleeves))


def exit_table(config: PortfolioConfig, holdout: pd.Timestamp, funding: float) -> pd.DataFrame:
    """Sharpe, CAGR and drawdown for each variant: every sleeve alone, and the book with its risk overlay."""
    rows = []
    for name, stops in VARIANTS.items():
        variant = with_stops(config, stops)
        inputs = prepare_inputs(variant)
        books = {sleeve_id: run_book(variant, inputs, allocation="equal", sleeves=[sleeve_id], risk_overlay=False, funding_pct_per_day=funding)
                 for sleeve_id in inputs.sleeve_weights.columns}
        books["book"] = run_book(variant, inputs, funding_pct_per_day=funding)
        for label, book in books.items():
            for row in period_metrics(book, holdout, label=label):
                rows.append({"variant": name, **row})
    frame = pd.DataFrame(rows)
    return frame.pivot_table(index=["book", "variant"], columns="period", values=["sharpe", "cagr", "max_drawdown", "turnover_per_year"], sort=False)[
        [("sharpe", "is"), ("sharpe", "ho"), ("cagr", "is"), ("cagr", "ho"), ("max_drawdown", "is"), ("max_drawdown", "ho"), ("turnover_per_year", "is")]
    ]


def breaker_table(config: PortfolioConfig, fraction: float, funding: float, daily_limit: float, hard_stops: list[float]) -> pd.DataFrame:
    """Each sleeve sized like the runtime (fixed fraction, runtime default exits): how the account breakers would fire."""
    rows = []
    for sleeve in config.enabled_sleeves:
        runtime_like = replace(sleeve, sizing="fixed_fraction", sizing_params={"fraction": fraction}, stops=dict(RUNTIME_DEFAULTS))
        variant = replace(config, sleeves=(runtime_like,), scale=1.0)
        inputs = prepare_inputs(variant)
        equity = run_book(variant, inputs, allocation="equal", risk_overlay=False, funding_pct_per_day=funding).result.equity
        daily = equity.resample("1D").last().dropna()
        # The hard stop measures the fall below the equity the runtime started with, so it depends on the start day:
        # for every start with a full year after it, the deepest fall below that start within the year
        year = 365
        lowest_ahead = daily[::-1].rolling(year, min_periods=1).min()[::-1]
        worst_within_year = (1.0 - lowest_ahead / daily).iloc[: len(daily) - year]
        row: dict[str, object] = {
            "sleeve": sleeve.id,
            "years": round((daily.index[-1] - daily.index[0]).days / 365.25, 1),
            "max drawdown": f"{(1.0 - daily / daily.cummax()).max():.1%}",
            f"days losing > {daily_limit:.0%}": int((daily.pct_change() < -daily_limit).sum()),
        }
        for threshold in hard_stops:
            row[f"hard stop {threshold:.0%}: starts stopped within a year"] = f"{(worst_within_year > threshold).mean():.0%}"
        rows.append(row)
    return pd.DataFrame(rows).set_index("sleeve")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="Portfolio TOML file")
    parser.add_argument("--holdout-start", default="2024-10-01")
    parser.add_argument("--funding-pct-per-day", type=float, default=0.01)
    parser.add_argument("--position-fraction", type=float, default=0.10, help="The runtime's position size as a share of equity")
    parser.add_argument("--daily-loss-limit", type=float, default=0.05)
    parser.add_argument("--hard-stops", type=float, nargs="*", default=[0.02, 0.05, 0.10, 0.20])
    args = parser.parse_args()

    config = load_portfolio_config(args.config)
    holdout = pd.Timestamp(args.holdout_start, tz="UTC")
    pd.set_option("display.width", 200)
    print(f"Exit variants on '{config.name}' (holdout from {holdout:%Y-%m-%d}); the book includes its [risk] overlay\n")
    print(exit_table(config, holdout, args.funding_pct_per_day).round(3).to_string())
    print(f"\nAccount breakers with each sleeve sized like the runtime ({args.position_fraction:.0%} of equity, runtime default exits)\n")
    print(breaker_table(config, args.position_fraction, args.funding_pct_per_day, args.daily_loss_limit, args.hard_stops).to_string())


if __name__ == "__main__":
    main()
