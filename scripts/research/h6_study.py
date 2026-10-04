"""H6, cross-sectional funding carry on Kraken-tradable perps: run the pre-registered test (research/prereg/H6.txt).

    python scripts/research/h6_study.py                       # writes data/research/h6_<time>/ and logs 9 trials
    python scripts/research/h6_study.py --no-ledger           # a rerun after a code fix, not a new try
    python scripts/research/h6_study.py --no-kraken-funding   # skip the Kraken funding comparison (about 8 MB of requests)

Every number the pre-registration names is computed here and nothing else: the primary configuration, the 8-cell
grid, the 2x-cost case, the secondary outcomes, and the verdict against PASS and KILL. Development data only; the
frozen holdout stays locked.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.binance_archive import base_asset  # noqa: E402
from src.research import governance  # noqa: E402
from src.research import signal_lab as lab  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402
from src.research.portfolio import rank_weights  # noqa: E402

HYPOTHESIS = "H6"
PRIMARY = {"top_n": 50, "quantile": 0.2, "rebalance_days": 7}
GRID = [{"top_n": top_n, "quantile": quantile, "rebalance_days": days} for top_n in (30, 50) for quantile in (0.2, 0.3) for days in (7, 14)]
MIN_NAMES = 10
END = pd.Timestamp("2025-12-31", tz="UTC")
OVERLAP = (pd.Timestamp("2025-10-01", tz="UTC"), END)


def sharpe(values: np.ndarray) -> float:
    return float(np.mean(values) / np.std(values, ddof=1) * np.sqrt(lab.DAYS)) if len(values) > 2 and np.std(values, ddof=1) > 0 else float("nan")


def window_start(wide: dict[str, pd.DataFrame]) -> pd.Timestamp:
    """The first day with at least `MIN_NAMES` coins in the universe."""
    names = wide["universe"].sum(axis=1)
    return names[names >= MIN_NAMES].index[0]


def kraken_funding_check(wide: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Kraken against Binance funding over the overlap: per-coin daily correlation, and whether Kraken orders the book's sides the same way."""
    from src.data.kraken_futures import fetch_funding_history

    first, last = OVERLAP
    universe = wide["universe"].loc[first:last]
    symbols = [symbol for symbol in universe.columns if universe[symbol].any()]
    venue = {row.base: row.symbol for row in pd.read_csv(lab.LISTINGS).itertuples()}
    kraken = {}
    for symbol in symbols:
        base = base_asset(symbol)
        name = venue.get(base) or venue.get(symbol.removesuffix("USDT"))
        if name is None:
            continue
        try:
            rates = fetch_funding_history(name)
        except Exception:  # noqa: BLE001 - one coin's failure leaves it out of the comparison
            continue
        series = pd.Series({rate.timestamp: rate.hourly_rate for rate in rates if rate.timestamp <= last + pd.Timedelta(days=1)})  # nothing from the frozen window
        if len(series):
            kraken[symbol] = series.groupby(series.index.floor("D")).sum()
    daily = pd.DataFrame(kraken).reindex(pd.date_range(first, last, freq="D", tz="UTC"))
    binance = wide["funding"].reindex(index=daily.index, columns=daily.columns)
    correlations = [daily[symbol].corr(binance[symbol]) for symbol in daily.columns if daily[symbol].notna().sum() > 30]
    weights = rank_weights(-lab.features(wide)["funding_7d"], wide["universe"], quantile=PRIMARY["quantile"], gross=1.0).reindex(index=daily.index, columns=daily.columns)
    week = daily.rolling(7, min_periods=5).mean()
    longs, shorts = week.where(weights > 0).mean(axis=1), week.where(weights < 0).mean(axis=1)
    both = (longs.notna() & shorts.notna())
    return {"coins": float(len(daily.columns)), "median_daily_correlation": float(np.nanmedian(correlations)) if correlations else float("nan"),
            "days_compared": float(both.sum()), "share_days_shorts_pay_more_on_kraken": float((shorts[both] > longs[both]).mean()) if both.any() else float("nan"),
            "kraken_spread_pct_per_year": float((shorts[both] - longs[both]).mean() * lab.DAYS * 100.0) if both.any() else float("nan"),
            "binance_spread_pct_per_year": float((binance.rolling(7, min_periods=5).mean().where(weights < 0).mean(axis=1) - binance.rolling(7, min_periods=5).mean().where(weights > 0).mean(axis=1))[both].mean() * lab.DAYS * 100.0) if both.any() else float("nan")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true", help="Don't log the trials (a rerun after a code fix, not a new try)")
    parser.add_argument("--no-kraken-funding", action="store_true", help="Skip the Kraken funding comparison")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h6_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    wides = {top_n: lab.load_universe(top_n=top_n, listing="point_in_time") for top_n in (30, 50)}
    wide = wides[PRIMARY["top_n"]]
    start = window_start(wide)
    settings = {"quantile": PRIMARY["quantile"], "rebalance_days": PRIMARY["rebalance_days"], "start": start}
    primary = lab.carry_sleeve(wide, **settings)
    returns = primary.returns
    numbers = lab.metrics(returns)
    estimate, low, high = block_bootstrap_ci(returns.to_numpy(), block=20, runs=2000, seed=0, statistic=sharpe)
    doubled = lab.carry_sleeve(wide, cost_multiplier=2.0, **settings)

    parts = pd.DataFrame(primary.parts).assign(net=returns)
    by_year = parts.groupby(parts.index.year).mean() * lab.DAYS * 100.0
    by_year["sharpe"] = returns.groupby(returns.index.year).apply(lambda part: sharpe(part.to_numpy()))
    by_year["days"] = returns.groupby(returns.index.year).size()
    by_year.loc["all"] = [*(parts.mean() * lab.DAYS * 100.0), numbers["sharpe"], len(returns)]

    grid_rows = []
    for cell in GRID:
        result = lab.carry_sleeve(wides[cell["top_n"]], quantile=cell["quantile"], rebalance_days=cell["rebalance_days"], start=window_start(wides[cell["top_n"]]))
        cell_numbers = lab.metrics(result.returns)
        grid_rows.append({**cell, "sharpe": cell_numbers["sharpe"], "return": cell_numbers["return"], "max_drawdown": cell_numbers["max_drawdown"]})
    grid = pd.DataFrame(grid_rows)

    taker = lab.taker_sleeve(wide, **settings)
    bitcoin = wide["close"]["BTCUSDT"].pct_change(fill_method=None).reindex(returns.index)
    today = lab.carry_sleeve(lab.load_universe(top_n=PRIMARY["top_n"], listing="today"), **settings)
    names = wide["universe"].sum(axis=1).loc[start:]
    kraken = {} if args.no_kraken_funding else kraken_funding_check(wide)

    funding_received = float(primary.parts["funding received"].mean() * lab.DAYS)
    years = by_year.drop(index="all")
    positive_years = int((years.loc[[year for year in years.index if 2022 <= int(year) <= 2025], "net"] > 0).sum())
    checks = {
        "1. primary Sharpe >= 0.7": numbers["sharpe"] >= 0.7,
        "2. bootstrap lower bound > 0": low > 0,
        "3. funding received > 0": funding_received > 0,
        "4. net return at 2x costs > 0": lab.metrics(doubled.returns)["return"] > 0,
        "5. >= 6 of 8 grid cells positive and >= 3 of 4 years positive": int((grid["sharpe"] > 0).sum()) >= 6 and positive_years >= 3,
    }
    kills = {"primary Sharpe < 0.3": numbers["sharpe"] < 0.3, "net return at 2x costs negative": lab.metrics(doubled.returns)["return"] < 0, "funding received not positive": funding_received <= 0}
    verdict = "KILL" if any(kills.values()) else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    lines = [f"# H6: cross-sectional funding carry, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
             f"Window {start:%Y-%m-%d} to {returns.index[-1]:%Y-%m-%d} ({len(returns)} days); {int(names.median())} coins on a median day ({int(names.min())} to {int(names.max())}).", "",
             f"**Primary** (top 50, 20% a side, weekly): Sharpe {numbers['sharpe']:.2f} (95% interval {low:.2f} to {high:.2f}), return {numbers['return']:+.1%} a year, "
             f"volatility {numbers['vol']:.1%}, worst drawdown {numbers['max_drawdown']:.1%}.",
             f"At 2x costs: Sharpe {lab.metrics(doubled.returns)['sharpe']:.2f}, return {lab.metrics(doubled.returns)['return']:+.1%} a year.",
             f"Same rule on today's Kraken list, same window: Sharpe {lab.metrics(today.returns)['sharpe']:.2f}.",
             f"Correlation with the taker basket {returns.corr(taker.returns):+.2f} (taker Sharpe {lab.metrics(taker.returns)['sharpe']:.2f} on this universe), with BTC {returns.corr(bitcoin):+.2f}.", "",
             "## Per year (% of equity a year; Sharpe)", "", md_table(by_year, digits=1), "",
             "## Grid", "", md_table(grid.set_index(["top_n", "quantile", "rebalance_days"]), digits=2), ""]
    if kraken:
        lines += ["## Kraken against Binance funding, 2025-10-01 to 2025-12-31", "",
                  f"{int(kraken['coins'])} coins; median daily correlation {kraken['median_daily_correlation']:.2f}. On {kraken['share_days_shorts_pay_more_on_kraken']:.0%} of {int(kraken['days_compared'])} days "
                  f"the short side's 7-day funding was above the long side's on Kraken. Funding spread, short minus long: {kraken['kraken_spread_pct_per_year']:+.1f}% a year on Kraken, "
                  f"{kraken['binance_spread_pct_per_year']:+.1f}% on Binance.", ""]
    lines += ["## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()], *[f"- kill rule met: {label}" for label, hit in kills.items() if hit], "", f"**{verdict}**"]
    report = "\n".join(lines)
    (out / "report.md").write_text(report)
    by_year.to_csv(out / "by_year.csv")
    grid.to_csv(out / "grid.csv", index=False)
    pd.DataFrame({"carry": returns, "carry_2x_costs": doubled.returns, "taker": taker.returns, "carry_todays_list": today.returns}).to_csv(out / "daily_returns.csv")
    if not args.no_ledger:
        governance.record_trials("h6_study", len(GRID) + 1, family=HYPOTHESIS, data=f"binance perps (daily), Kraken-listed point-in-time, {start:%Y-%m-%d} to 2025-12-31",
                                 details={"primary": PRIMARY, "verdict": verdict, "primary_sharpe": numbers["sharpe"]})
    print(report)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
