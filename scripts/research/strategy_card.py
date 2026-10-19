"""Write a strategy card (src/research/strategy_checks.py) for return series a study saved as CSV.

    # the study's daily net returns, one column per variant; P is the strategy, B1 what it has to beat
    python scripts/research/strategy_card.py data/research/h13_<time>/daily_net.csv --strategy P --baseline B1 \\
        --variants all --sweep --family H13 --periods-per-year 252 \\
        --positions data/research/h13_<time>/positions.csv --market data/research/h13_<time>/market_returns.csv

`--variants all` counts every other column as a configuration the study tried (it feeds the deflated Sharpe and the
Romano-Wolf p-values); `--sweep` also runs the overfitting test across them. `--positions` is a CSV with a two-row
header (variant, market) as the studies save it, or one column per market; `--market` the market returns with the
same market columns. The card is printed and saved under data/research/strategy_cards/ (or `--out`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import strategy_checks as stc  # noqa: E402


def _positions(path: str, strategy: str) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col=0, header=[0, 1], parse_dates=True)
    if strategy in frame.columns.get_level_values(0):
        return frame[strategy]
    return pd.read_csv(path, index_col=0, parse_dates=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("returns", help="CSV of net returns: a time index and one column per variant")
    parser.add_argument("--strategy", required=True, help="the column judged")
    parser.add_argument("--baseline", help="the column it has to beat")
    parser.add_argument("--variants", nargs="*", default=[], help="other columns the study tried, or 'all'")
    parser.add_argument("--sweep", action="store_true", help="run the overfitting test (CSCV) across the variants")
    parser.add_argument("--family", help="the hypothesis in the trial ledger, e.g. H13")
    parser.add_argument("--periods-per-year", type=float, default=252.0, help="252 for exchange-traded markets, 365 for crypto daily")
    parser.add_argument("--positions", help="positions earning each period's market return, for the timing placebo")
    parser.add_argument("--market", help="the markets' returns, same columns as the positions")
    parser.add_argument("--name", help="the card's name (default: the strategy column)")
    parser.add_argument("--out", help="folder to write the card to")
    parser.add_argument("--no-ledger", action="store_true", help="don't note the card in the trial ledger")
    args = parser.parse_args()

    frame = pd.read_csv(args.returns, index_col=0, parse_dates=True)
    names = [column for column in frame.columns if column != args.strategy] if args.variants == ["all"] else args.variants
    variants = frame[[args.strategy, *[column for column in names if column != args.strategy]]]
    positions = _positions(args.positions, args.strategy) if args.positions else None
    market = pd.read_csv(args.market, index_col=0, parse_dates=True) if args.market else None
    card = stc.strategy_card(frame[args.strategy], name=args.name or args.strategy, periods_per_year=args.periods_per_year,
                             baseline=frame[args.baseline] if args.baseline else None, variants=variants if len(variants.columns) > 1 else None,
                             positions=positions, asset_returns=market, sweep=variants if args.sweep and len(variants.columns) > 1 else None,
                             family=args.family, ledger=not args.no_ledger)
    print(card.to_markdown())
    print(f"Wrote {card.save(args.out)}")


if __name__ == "__main__":
    main()
