"""H8, pairs of Kraken-tradable perps: run the pre-registered test (research/prereg/H8.txt).

    python scripts/research/h8_study.py            # writes data/research/h8_<time>/ and logs 11 trials
    python scripts/research/h8_study.py --no-ledger

Development data only; the frozen holdout stays locked.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from regime_study import labels  # noqa: E402
from src.data.binance_archive import base_asset  # noqa: E402
from src.research import governance, pit  # noqa: E402
from src.research import signal_lab as lab  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402
from src.research.pairs import PairsResult, pair_weights  # noqa: E402
from src.research.portfolio import PortfolioCosts, PortfolioResult, simulate_portfolio, slippage_by_liquidity  # noqa: E402

HYPOTHESIS = "H8"
PRIMARY = {"formation": 90, "entry": 2.0}
GRID = [{"formation": formation, "entry": entry} for formation in (60, 90, 120) for entry in (1.5, 2.0, 2.5)]
COINS = 10
DAYS = 365.0


def sharpe(values: np.ndarray) -> float:
    return float(np.mean(values) / np.std(values, ddof=1) * np.sqrt(DAYS)) if len(values) > 2 and np.std(values, ddof=1) > 0 else float("nan")


def load() -> dict[str, pd.DataFrame]:
    """Closes, funding, 30-day average volume and the point-in-time Kraken eligibility of every Binance perp."""
    wide = lab.load_universe(top_n=50, listing="point_in_time")  # for the closes, funding and the taker comparison; pairs use their own coin choice
    opened = lab.kraken_opening_dates()
    eligible = pd.DataFrame({symbol: (wide["close"].index > opened[base_asset(symbol)]) if base_asset(symbol) in opened else False for symbol in wide["close"].columns}, index=wide["close"].index)
    wide["eligible"] = eligible
    wide["average_volume"] = wide["quote_volume"].rolling(30, min_periods=30).mean()
    return wide


def first_start(wide: dict[str, pd.DataFrame]) -> pd.Timestamp:
    """The first date on which `COINS` Kraken-listed coins have a 30-day volume."""
    count = (wide["eligible"] & wide["average_volume"].notna()).sum(axis=1)
    return count[count >= COINS].index[0]


def run(wide: dict[str, pd.DataFrame], start: pd.Timestamp, *, formation: int, entry: float, delay: int = 1, cost_multiplier: float = 1.0) -> tuple[PairsResult, PortfolioResult]:
    picked = pair_weights(wide["close"], wide["eligible"], wide["average_volume"], formation=formation, entry=entry, coins=COINS, delay=delay, start=start)
    costs = PortfolioCosts(fee_pct=0.05 * cost_multiplier, slippage_bps=slippage_by_liquidity(wide["quote_volume"]) * cost_multiplier)
    return picked, simulate_portfolio(wide["close"], picked.weights, funding=wide["funding"], costs=costs, rebalance_every=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h8_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    wide = load()
    start = first_start(wide)
    picked, result = run(wide, start, **PRIMARY)
    began = picked.periods[0]["start"]
    cut = lambda series: series[series.index >= began]  # noqa: E731
    returns = cut(result.returns)
    numbers = lab.metrics(returns)
    _estimate, low, high = block_bootstrap_ci(returns.to_numpy(), block=20, runs=2000, seed=0, statistic=sharpe)
    doubled = lab.metrics(cut(run(wide, start, cost_multiplier=2.0, **PRIMARY)[1].returns))
    undelayed = lab.metrics(cut(run(wide, start, delay=0, **PRIMARY)[1].returns))

    parts = pd.DataFrame({"price": cut(result.long_pnl + result.short_pnl), "funding received": cut(-result.funding), "costs": cut(-result.costs), "net": returns})
    by_year = parts.groupby(parts.index.year).mean() * DAYS * 100.0
    by_year["sharpe"] = returns.groupby(returns.index.year).apply(lambda part: sharpe(part.to_numpy()))
    by_year["trades"] = picked.trades.groupby(pd.DatetimeIndex(picked.trades["opened"]).year).size().reindex(by_year.index).fillna(0)
    by_year.loc["all"] = [*(parts.mean() * DAYS * 100.0), numbers["sharpe"], len(picked.trades)]

    grid_rows = []
    for cell in GRID:
        cell_picked, cell_result = run(wide, start, **cell)
        cell_returns = cell_result.returns[cell_result.returns.index >= cell_picked.periods[0]["start"]]
        cell_numbers = lab.metrics(cell_returns)
        grid_rows.append({**cell, "sharpe": cell_numbers["sharpe"], "return": cell_numbers["return"], "max_drawdown": cell_numbers["max_drawdown"], "trades": len(cell_picked.trades)})
    grid = pd.DataFrame(grid_rows)

    bitcoin = pit.load_bars("BTC", "1h")["close"].resample("1D").last().dropna()
    bitcoin.index = bitcoin.index + pd.Timedelta(days=1)
    direction = labels(bitcoin)["direction"].reindex(returns.index)
    by_state = pd.DataFrame({state: {"days": int((direction == state).sum()), "return_pct_per_year": returns[direction == state].mean() * DAYS * 100.0,
                                     "sharpe": sharpe(returns[direction == state].to_numpy())} for state in ("flat", "middling", "trending")}).T
    taker = lab.taker_sleeve(wide, start=began).returns.reindex(returns.index)
    market = wide["close"]["BTCUSDT"].pct_change(fill_method=None).reindex(returns.index)
    trades = picked.trades
    pair_counts = pd.Series([f"{base_asset(a)}-{base_asset(b)}" for period in picked.periods for a, b in period["pairs"]]).value_counts()

    positive_years = int((by_year.drop(index="all")["net"] > 0).sum())
    checks = {"1. primary Sharpe >= 0.7": numbers["sharpe"] >= 0.7, "2. bootstrap lower bound > 0": low > 0, "3. net return at 2x costs > 0": doubled["return"] > 0,
              "4. >= 7 of 9 grid cells positive": int((grid["sharpe"] > 0).sum()) >= 7, "5. >= 3 of 4 years positive": positive_years >= 3}
    kills = {"primary Sharpe < 0.3": numbers["sharpe"] < 0.3, "net return at 2x costs negative": doubled["return"] < 0}
    verdict = "KILL" if any(kills.values()) else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    gross = cut(result.gross_exposure)
    report = [f"# H8: pairs of Kraken-tradable perps, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"Window {began:%Y-%m-%d} to {returns.index[-1]:%Y-%m-%d} ({len(returns)} days, {len(picked.periods)} trading periods).", "",
              f"**Primary** (90-day formation, open at 2 sigma, one day's delay): Sharpe {numbers['sharpe']:.2f} (95% interval {low:.2f} to {high:.2f}), return {numbers['return']:+.1%} a year, "
              f"volatility {numbers['vol']:.1%}, worst drawdown {numbers['max_drawdown']:.1%}.",
              f"At 2x costs: Sharpe {doubled['sharpe']:.2f}, return {doubled['return']:+.1%}. Without the one-day delay: Sharpe {undelayed['sharpe']:.2f}, return {undelayed['return']:+.1%}.",
              f"{len(trades)} trades, {trades['converged'].mean():.0%} closed by convergence, {trades['days'].mean():.1f} days held on average; average gross exposure {gross.mean():.2f}x.",
              f"Correlation with the taker basket {returns.corr(taker):+.2f}, with BTC {returns.corr(market):+.2f}.",
              "Most chosen pairs: " + ", ".join(f"{name} ({count})" for name, count in pair_counts.head(8).items()) + ".", "",
              "## Per year (% of equity a year; Sharpe)", "", md_table(by_year, digits=1), "",
              "## By market state", "", md_table(by_state, digits=2), "",
              "## Grid", "", md_table(grid.set_index(["formation", "entry"]), digits=2), "",
              "## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()], *[f"- kill rule met: {label}" for label, hit in kills.items() if hit], "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    by_year.to_csv(out / "by_year.csv")
    grid.to_csv(out / "grid.csv", index=False)
    trades.to_csv(out / "trades.csv", index=False)
    returns.rename("pairs").to_csv(out / "daily_returns.csv")
    if not args.no_ledger:
        governance.record_trials("h8_study", len(GRID) + 2, family=HYPOTHESIS, data=f"binance perps (daily), Kraken-listed point-in-time, {began:%Y-%m-%d} to 2025-12-31",
                                 details={"primary": PRIMARY, "verdict": verdict, "primary_sharpe": numbers["sharpe"]})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
