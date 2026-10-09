"""H11, trend, carry and relative momentum on equity index futures and currencies: the pre-registered test (research/prereg/H11.txt).

    python scripts/research/h11_study.py            # writes data/research/h11_<time>/ and logs 11 trials
    python scripts/research/h11_study.py --no-ledger
    python scripts/research/h11_study.py --refresh  # download the data again
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research import multi_asset as ma  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402

HYPOTHESIS = "H11"
START, END, SPLIT = pd.Timestamp("2000-02-01"), pd.Timestamp("2025-12-31"), pd.Timestamp("2013-01-01")
CAPITALS_NOK = (1_000_000.0, 100_000.0)
NAMES = {"book": "The book", "fx": "Currency book", "equity": "Equity book", "fx_trend": "Currency trend", "fx_carry": "Currency carry",
         "fx_momentum": "Currency momentum", "equity_trend": "Equity trend", "equity_momentum": "Equity momentum"}


def window(series: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """The pre-registered window."""
    return series[(series.index >= START) & (series.index <= END)]


def data_checks(panel: ma.Panel) -> pd.DataFrame:
    """Per market: first day with a return, days on which the price did not change, and the largest one-day move."""
    returns = window(panel.returns)
    prices = window(panel.prices)
    unchanged = (prices.diff() == 0).sum()
    return pd.DataFrame({"first return": panel.returns.apply(lambda column: f"{column.first_valid_index():%Y-%m-%d}"), "days": returns.notna().sum(),
                         "days unchanged": unchanged, "largest move": returns.abs().max()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true", help="do not log the trials")
    parser.add_argument("--refresh", action="store_true", help="download the data again")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h11_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    panel = asyncio.run(ma.load_panel(refresh=args.refresh))
    checks_frame = data_checks(panel)
    result = ma.run_book(panel)
    net = {name: window(performance.net) for name, performance in result.results.items()}
    gross = {name: window(performance.gross) for name, performance in result.results.items()}
    book = net["book"]

    estimate, low, high = block_bootstrap_ci(book.to_numpy(), block=60, runs=2000, seed=0, statistic=ma.sharpe)
    table = pd.DataFrame({NAMES[name]: {**ma.summary(net[name]), "Sharpe before costs": ma.sharpe(gross[name]),
                                         "Sharpe 2000-2012": ma.sharpe(net[name][net[name].index < SPLIT]), "Sharpe 2013-2025": ma.sharpe(net[name][net[name].index >= SPLIT])}
                          for name in NAMES}).T
    halves = {label: {"Sharpe": ma.sharpe(part), "Sortino": ma.sortino(part)} for label, part in
              (("2000-02 to 2012-12", book[book.index < SPLIT]), ("2013-01 to 2025-12", book[book.index >= SPLIT]))}
    by_year = pd.DataFrame({NAMES[name]: (1.0 + net[name]).groupby(net[name].index.year).prod() - 1.0 for name in ("book", "fx", "equity")}) * 100.0

    doubled = window(ma.run_book(panel, cost_multiplier=2.0).results["book"].net)
    held = window(result.results["book"].held)
    turnover = float(window(result.results["book"].turnover).mean() * ma.DAYS_PER_YEAR)
    gross_leverage = float(held.abs().sum(axis=1).mean())
    cost_drag = float((gross["book"].mean() - book.mean()) * ma.DAYS_PER_YEAR)

    equity_markets = panel.sector("equity")
    basket = window(panel.returns[equity_markets].mean(axis=1))
    spx = window(panel.returns["SPX"])
    months = pd.DataFrame({"book": ma.monthly(book), "basket": ma.monthly(basket), "spx": ma.monthly(spx)})
    bad = months[months["basket"] < -0.05]
    sleeve_names = [name for name in NAMES if name not in ("book", "fx", "equity")]
    sleeve_corr = pd.DataFrame({NAMES[name]: ma.monthly(net[name]) for name in sleeve_names}).corr()

    lots = {}
    for capital in CAPITALS_NOK:
        rounded, contracts = ma.whole_contracts(panel, result.positions["book"], capital_nok=capital)
        lot_net = window(ma.performance(panel, rounded).net)
        wanted = window(result.positions["book"]).abs() > 0
        lots[capital] = {"Sharpe": ma.sharpe(lot_net), "Sortino": ma.sortino(lot_net), "volatility": float(lot_net.std() * np.sqrt(ma.DAYS_PER_YEAR)),
                         "positions rounded to zero": float(((window(contracts) == 0) & wanted).to_numpy().sum() / max(wanted.to_numpy().sum(), 1)),
                         "correlation with the exact book": float(lot_net.corr(book))}
    latest = result.positions["book"].index[-1]
    notional = ma.contract_notional_nok(panel).loc[latest]
    sizes = pd.DataFrame({"contract": panel.markets["contract"], "one contract in NOK": notional.round(-3),
                          "position at 1,000,000 NOK": (result.positions["book"].loc[latest] * 1_000_000.0).round(-3)})

    checks = {"1. Sharpe after costs positive, 95% interval above 0": estimate > 0 and low > 0,
              "2. Sharpe and Sortino positive in both halves": all(value > 0 for half in halves.values() for value in half.values()),
              "3. each sector book positive over the whole window": ma.sharpe(net["fx"]) > 0 and ma.sharpe(net["equity"]) > 0,
              "4. still positive at doubled costs": ma.sharpe(doubled) > 0}
    killed = not halves["2013-01 to 2025-12"]["Sharpe"] > 0
    verdict = "KILL" if killed else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    stats = ma.summary(book)
    report = [f"# H11: trend, carry and relative momentum on equity index futures and currencies, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"{len(book)} trading days, {book.index[0]:%Y-%m-%d} to {book.index[-1]:%Y-%m-%d}; {len(equity_markets)} equity indices and {len(panel.sector('fx'))} currencies; "
              "weekly decisions, traded the next day; returns above cash, after costs.", "",
              f"**Primary:** Sharpe {estimate:.2f} (95% interval {low:.2f} to {high:.2f}); Sortino {stats['Sortino']:.2f}; {stats['yearly return']:.1%} a year at "
              f"{stats['volatility']:.1%} volatility; worst drawdown {stats['worst drawdown']:.1%}.",
              "Halves: " + "; ".join(f"{label}: Sharpe {half['Sharpe']:.2f}, Sortino {half['Sortino']:.2f}" for label, half in halves.items()) + ".",
              f"At doubled costs: Sharpe {ma.sharpe(doubled):.2f}. Costs take {cost_drag:.2%} a year; turnover {turnover:.1f} times capital a year; "
              f"average total position {gross_leverage:.1f} times capital.",
              f"Correlation of monthly returns: {months['book'].corr(months['spx']):+.2f} with the S&P 500, {months['book'].corr(months['basket']):+.2f} with the basket of {len(equity_markets)} indices. "
              f"In the {len(bad)} months the basket fell more than 5% (average {bad['basket'].mean():.1%}), the book averaged {bad['book'].mean():+.1%}.", "",
              "## Sleeves, sector books and the book (after costs)", "", md_table(table, digits=2), "",
              "## Per year, % (after costs)", "", md_table(by_year, digits=1), "",
              "## Correlation between sleeves (monthly)", "", md_table(sleeve_corr, digits=2), "",
              "## Whole contracts", "", md_table(pd.DataFrame({f"{capital:,.0f} NOK": values for capital, values in lots.items()}).T, digits=2), "",
              f"Sizes on {latest:%Y-%m-%d} (contract sizes not yet verified against the exchanges):", "", md_table(sizes, digits=0), "",
              "## Data checks", "", md_table(checks_frame, digits=3), "",
              "## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()],
              *(["- kill rule met: Sharpe after costs in 2013-2025 is zero or negative"] if killed else []), "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    pd.DataFrame({name: net[name] for name in NAMES}).to_csv(out / "daily_net.csv")
    result.positions["book"].to_csv(out / "book_positions.csv")
    if not args.no_ledger:
        governance.record_trials("h11_study", 11, family=HYPOTHESIS, data="12 equity indices (Yahoo), 9 currencies (Norges Bank), OECD rates, 2000-02 to 2025-12",
                                 details={"verdict": verdict, "sharpe_net": estimate, "interval": [low, high]})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
