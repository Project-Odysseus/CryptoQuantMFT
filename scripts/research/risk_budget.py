"""How much capital, at what size? The historical risk of a portfolio config in money, and the scale for a drawdown limit.

Runs the config's research backtest (the same code the runtime uses) at
several scales and prints, for your capital: the worst day, week and month,
95%/99% one-day value-at-risk and expected shortfall, the deepest drawdown
and the longest time under water, the exposure and the margin needed. With
--max-drawdown it also finds the largest `[portfolio] scale` whose
historical drawdown times --safety stays inside your limit.

Usage:
    python scripts/research/risk_budget.py config/portfolio.example.toml --capital 5000
    python scripts/research/risk_budget.py config/portfolio.example.toml --capital 5000 --max-drawdown 0.2

Put the scale you choose in the config (`[portfolio] scale = ...`) and the
capital in `initial_equity`; both research and the runtime use them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from src.portfolio.backtest import prepare_inputs
from src.portfolio.config import load_portfolio_config
from src.portfolio.risk_budget import risk_report, scale_for_max_drawdown

ROWS = [
    ("scale", "{:.2f}"), ("cagr", "{:.1%}"), ("annual_vol", "{:.1%}"), ("sharpe", "{:.2f}"),
    ("max_drawdown", "{:.1%}"), ("max_drawdown_money", "{:,.0f}"), ("longest_underwater_days", "{:.0f}"),
    ("worst_day", "{:.1%}"), ("worst_day_money", "{:,.0f}"), ("worst_week", "{:.1%}"), ("worst_month", "{:.1%}"), ("worst_month_money", "{:,.0f}"),
    ("var_95_day_money", "{:,.0f}"), ("es_99_day_money", "{:,.0f}"), ("share_of_months_losing", "{:.0%}"),
    ("avg_gross_exposure", "{:.2f}x"), ("max_gross_exposure", "{:.2f}x"), ("max_gross_notional_money", "{:,.0f}"), ("max_margin_share", "{:.0%}"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="Portfolio TOML file")
    parser.add_argument("--capital", type=float, default=None, help="Money you would put in, in the base currency (default: initial_equity)")
    parser.add_argument("--max-drawdown", type=float, default=None, help="The drawdown you can sit through, e.g. 0.2 for 20%%")
    parser.add_argument("--safety", type=float, default=1.5, help="Assume the future's worst drawdown is this multiple of history's")
    parser.add_argument("--scales", type=float, nargs="*", default=[0.25, 0.5, 0.75, 1.0], help="Scales to compare")
    parser.add_argument("--funding-pct-per-day", type=float, default=0.01)
    args = parser.parse_args()

    config = load_portfolio_config(args.config)
    capital = args.capital or config.initial_equity
    inputs = prepare_inputs(config)
    scales = list(args.scales)
    chosen = None
    if args.max_drawdown is not None:
        chosen = scale_for_max_drawdown(config, inputs, args.max_drawdown, safety=args.safety, funding_pct_per_day=args.funding_pct_per_day)
        scales.append(round(chosen, 3))
    reports = {f"x{scale:g}": risk_report(config, inputs, capital=capital, scale=scale, funding_pct_per_day=args.funding_pct_per_day) for scale in sorted(set(scales))}
    table = pd.DataFrame({name: {row: fmt.format(report[row]) for row, fmt in ROWS} for name, report in reports.items()})
    print(f"Portfolio '{config.name}': {inputs.measure_start:%Y-%m-%d} to {inputs.prices.index[-1]:%Y-%m-%d}, capital {capital:,.0f} {config.base_currency}, "
          f"config scale {config.scale:g}, risk limits as configured (kill at {config.risk.max_drawdown:.0%} drawdown)")
    print(table.to_string())
    if chosen is not None:
        report = reports[f"x{round(chosen, 3):g}"]
        print(f"\nFor a {args.max_drawdown:.0%} drawdown limit with a {args.safety:g}x safety margin: scale {chosen:.2f}. "
              f"History at that scale: max drawdown {report['max_drawdown']:.1%} ({report['max_drawdown_money']:,.0f}), "
              f"worst month {report['worst_month']:.1%}, CAGR {report['cagr']:.1%}. Put `scale = {chosen:.2f}` under [portfolio].")
    print("\nHistory understates future risk: the next worst drawdown is usually deeper than the last. Money figures are at the stated capital.")


if __name__ == "__main__":
    main()
