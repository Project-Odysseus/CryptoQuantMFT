"""How does each strategy in a book do in flat, trending, calm and stormy markets? Measured, with nothing fitted.

    python scripts/research/regime_study.py [config/portfolio.multi_paper.toml]

Every day gets two labels from Bitcoin's daily closes up to the day before (so a label is known before the day it
describes):

- **direction**: how straight Bitcoin's last 60 days were. The efficiency ratio is |60-day change| divided by the sum
  of the 60 daily moves: near 0 when the price went nowhere through a lot of back and forth, near 1 for a straight
  line. Days are "flat", "middling" or "trending" by where the ratio ranks among its own previous 365 values (thirds).
- **volatility**: "calm", "normal" or "stormy", the 30-day realized volatility ranked the same way.

Each strategy of the config is run alone (through `run_book`, with costs and funding) and its daily returns are split
by label. The windows (60, 30, 365) were fixed before looking and nothing is tuned to the result. Development data
only; logged as 2 looks (family "regime").
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.portfolio.backtest import daily_returns, prepare_inputs, run_book  # noqa: E402
from src.portfolio.config import load_portfolio_config  # noqa: E402
from src.research import governance, pit  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

DAYS = 365.0
ER_WINDOW, VOL_WINDOW, RANK_HISTORY = 60, 30, 365


def labels(close: pd.Series) -> pd.DataFrame:
    """Direction and volatility label per day, each from closes up to the day before."""
    change = close.diff()
    ratio = (close - close.shift(ER_WINDOW)).abs() / change.abs().rolling(ER_WINDOW, min_periods=ER_WINDOW).sum()
    realized = np.log(close).diff().rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std()

    def thirds(values: pd.Series, names: tuple[str, str, str]) -> pd.Series:
        rank = values.rolling(RANK_HISTORY, min_periods=RANK_HISTORY // 2).rank(pct=True).shift(1)
        return pd.Series(np.where(rank.isna(), "n/a", np.where(rank <= 1 / 3, names[0], np.where(rank <= 2 / 3, names[1], names[2]))), index=values.index)

    return pd.DataFrame({"direction": thirds(ratio, ("flat", "middling", "trending")), "volatility": thirds(realized, ("calm", "normal", "stormy")),
                         "efficiency_ratio": ratio.shift(1)})


def split(returns: pd.DataFrame, label: pd.Series, order: tuple[str, ...]) -> pd.DataFrame:
    """Per strategy and label: return a year, Sharpe, share of days."""
    label = label.reindex(returns.index).fillna("n/a")
    rows = {}
    for name in order:
        part = returns[label == name]
        for column in returns.columns:
            values = part[column]
            rows[(column, name)] = {"days": len(values), "return_pct_per_year": values.mean() * DAYS * 100.0,
                                    "sharpe": values.mean() / values.std() * np.sqrt(DAYS) if len(values) > 30 and values.std() > 0 else np.nan}
    return pd.DataFrame(rows).T


def by_year(returns: pd.DataFrame, label: pd.Series, name: str) -> pd.DataFrame:
    """Each strategy's return (% a year) on the days labelled `name`, year by year: is the pattern the same every year?"""
    label = label.reindex(returns.index)
    part = returns[label == name]
    table = part.groupby(part.index.year).mean() * DAYS * 100.0
    table.insert(0, "days", part.groupby(part.index.year).size())
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?", default="config/portfolio.multi_paper.toml")
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    config = load_portfolio_config(args.config)
    inputs = prepare_inputs(config)
    units: dict[str, list[str]] = {}
    for sleeve_id in inputs.sleeve_weights.columns:
        units.setdefault(inputs.sleeve_groups.get(sleeve_id, sleeve_id), []).append(sleeve_id)
    returns = pd.DataFrame({unit: daily_returns(run_book(config, inputs, allocation="equal", sleeves=members, risk_overlay=False)) for unit, members in units.items()})
    returns["whole book"] = daily_returns(run_book(config, inputs))
    returns = returns.fillna(0.0)

    bitcoin = pit.load_bars("BTC", "1h")["close"].resample("1D").last().dropna()  # Binance's BTC perp: a longer history than Kraken's for the rankings
    bitcoin.index = bitcoin.index + pd.Timedelta(days=1)  # a day's close is known from the next midnight, where the returns are stamped
    marks = labels(bitcoin)
    returns.index = pd.DatetimeIndex(returns.index).tz_convert("UTC") if returns.index.tz is not None else pd.DatetimeIndex(returns.index).tz_localize("UTC")

    direction = split(returns, marks["direction"], ("flat", "middling", "trending"))
    volatility = split(returns, marks["volatility"], ("calm", "normal", "stormy"))
    flat_years = by_year(returns, marks["direction"], "flat")
    trending_years = by_year(returns, marks["direction"], "trending")
    share = marks["direction"].reindex(returns.index).groupby(returns.index.year).apply(lambda part: (part == "flat").mean())

    out = Path("data/research") / f"regime_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args)
    for name, table in (("direction", direction), ("volatility", volatility), ("flat_by_year", flat_years), ("trending_by_year", trending_years)):
        table.to_csv(out / f"{name}.csv")
    pd.concat([returns, marks.reindex(returns.index)], axis=1).to_csv(out / "daily.csv")
    if not args.no_ledger:
        governance.record_trials("regime_study", 2, family="regime", data="kraken perps (daily) to 2025-12-31", details={"config": args.config, "labels": ["60d efficiency ratio thirds", "30d volatility thirds"]})

    def wide(table: pd.DataFrame, value: str) -> pd.DataFrame:
        return table[value].unstack().reindex(index=returns.columns)

    report = [f"# Strategies by market state ({returns.index[0]:%Y-%m-%d} to {returns.index[-1]:%Y-%m-%d}, {len(returns)} days)", "",
              "## By direction: return, % a year", "", md_table(wide(direction, "return_pct_per_year")[["flat", "middling", "trending"]], digits=1), "",
              "## By direction: Sharpe", "", md_table(wide(direction, "sharpe")[["flat", "middling", "trending"]], digits=2), "",
              "## By volatility: return, % a year", "", md_table(wide(volatility, "return_pct_per_year")[["calm", "normal", "stormy"]], digits=1), "",
              "## By volatility: Sharpe", "", md_table(wide(volatility, "sharpe")[["calm", "normal", "stormy"]], digits=2), "",
              "## Flat days only, year by year (% a year)", "", md_table(flat_years, digits=1), "",
              "## Trending days only, year by year (% a year)", "", md_table(trending_years, digits=1), "",
              "Share of each year's days labelled flat: " + ", ".join(f"{year} {value:.0%}" for year, value in share.items())]
    (out / "report.md").write_text("\n".join(report))
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
