"""H9, a rank buffer on the taker basket: run the pre-registered test (research/prereg/H9.txt).

    python scripts/research/h9_study.py            # writes data/research/h9_<time>/ and logs 7 trials
    python scripts/research/h9_study.py --no-ledger
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research import signal_lab as lab  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402
from src.research.portfolio import PortfolioCosts, simulate_portfolio, slippage_by_liquidity  # noqa: E402

HYPOTHESIS = "H9"
ENTRY, PRIMARY_EXIT, OTHER_EXITS = 0.20, 0.35, (0.30, 0.45)
REBALANCE_DAYS, BAND, TOP_N, MIN_NAMES = 10, 0.01, 30, 10
DAYS = 365.0


def run(wide: dict[str, pd.DataFrame], start: pd.Timestamp, *, exit: float, cost_multiplier: float = 1.0) -> dict[str, object]:
    score = lab.features(wide)["taker_buy_share_7d"]
    weights = lab.buffered_rank_weights(score, wide["universe"], entry=ENTRY, exit=exit, rebalance_days=REBALANCE_DAYS, min_names=MIN_NAMES, start=start)
    costs = PortfolioCosts(fee_pct=0.05 * cost_multiplier, slippage_bps=slippage_by_liquidity(wide["quote_volume"]) * cost_multiplier)
    result = simulate_portfolio(wide["close"], weights, funding=wide["funding"], costs=costs, rebalance_every=1, rebalance_band=BAND)
    keep = result.returns.index >= start
    returns = result.returns[keep]
    numbers = lab.metrics(returns)
    held = weights[keep]
    return {"returns": returns, "sharpe": numbers["sharpe"], "return": numbers["return"], "max_drawdown": numbers["max_drawdown"], "vol": numbers["vol"],
            "turnover": float(result.turnover[keep].mean() * DAYS), "costs": float(result.costs[keep].mean() * DAYS), "funding_received": float(-result.funding[keep].mean() * DAYS),
            "names_per_side": float(((held > 0).sum(axis=1) + (held < 0).sum(axis=1)).replace(0, np.nan).mean() / 2.0)}


def window_start(wide: dict[str, pd.DataFrame], earliest: pd.Timestamp) -> pd.Timestamp:
    names = wide["universe"].sum(axis=1)
    return names[(names >= MIN_NAMES) & (names.index >= earliest)].index[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h9_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    wide = lab.load_universe(top_n=TOP_N, listing="point_in_time")
    start = window_start(wide, lab.START)
    base, primary = run(wide, start, exit=ENTRY), run(wide, start, exit=PRIMARY_EXIT)
    base_2x, primary_2x = run(wide, start, exit=ENTRY, cost_multiplier=2.0), run(wide, start, exit=PRIMARY_EXIT, cost_multiplier=2.0)
    others = {exit: run(wide, start, exit=exit) for exit in OTHER_EXITS}
    today = lab.load_universe(top_n=TOP_N, listing="today")
    today_start = window_start(today, lab.START)
    today_base, today_buffered = run(today, today_start, exit=ENTRY), run(today, today_start, exit=PRIMARY_EXIT)

    difference = (primary["returns"] - base["returns"]).to_numpy()
    estimate, low, high = block_bootstrap_ci(difference, block=20, runs=2000, seed=0, statistic=lambda values: float(np.mean(values) * DAYS))
    keys = ["turnover", "return", "sharpe", "max_drawdown", "vol", "costs", "funding_received", "names_per_side"]
    table = pd.DataFrame({"baseline (exit 20%)": {key: base[key] for key in keys}, "exit 30%": {key: others[0.30][key] for key in keys},
                          "exit 35% (primary)": {key: primary[key] for key in keys}, "exit 45%": {key: others[0.45][key] for key in keys},
                          "baseline, 2x costs": {key: base_2x[key] for key in keys}, "exit 35%, 2x costs": {key: primary_2x[key] for key in keys}})
    yearly = pd.DataFrame({"baseline return %": base["returns"].groupby(base["returns"].index.year).mean() * DAYS * 100, "buffered return %": primary["returns"].groupby(primary["returns"].index.year).mean() * DAYS * 100})
    yearly["difference"] = yearly["buffered return %"] - yearly["baseline return %"]

    cut = 1.0 - primary["turnover"] / base["turnover"]
    checks = {"1. turnover at least 25% lower": cut >= 0.25, "2. net return not more than 1 point lower": primary["return"] >= base["return"] - 0.01,
              "3. at 2x costs the buffered return is higher": primary_2x["return"] > base_2x["return"]}
    kills = {"turnover not lower": primary["turnover"] >= base["turnover"], "net return more than 3 points lower": primary["return"] < base["return"] - 0.03}
    verdict = "KILL" if any(kills.values()) else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    report = [f"# H9: a rank buffer on the taker basket, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"Window {start:%Y-%m-%d} to {base['returns'].index[-1]:%Y-%m-%d} ({len(base['returns'])} days); point-in-time Kraken list, top {TOP_N}, rebalanced every {REBALANCE_DAYS} days.", "",
              md_table(table, digits=3), "",
              "Turnover is in times the book a year; return, costs and funding as fractions a year (0.10 = 10%).", "",
              f"Turnover cut by the 35% buffer: {cut:.0%}. Net return {primary['return'] - base['return']:+.1%} a year against the baseline "
              f"(difference of daily returns {estimate:+.1%}, 95% interval {low:+.1%} to {high:+.1%}).", "",
              "## Per year", "", md_table(yearly, digits=1), "",
              f"## Today's Kraken list from {today_start:%Y-%m-%d} (the original study's universe; reported only)", "",
              f"Baseline: turnover {today_base['turnover']:.1f}x, return {today_base['return']:+.1%}, Sharpe {today_base['sharpe']:.2f}. "
              f"Buffered: turnover {today_buffered['turnover']:.1f}x, return {today_buffered['return']:+.1%}, Sharpe {today_buffered['sharpe']:.2f}.", "",
              "## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()], *[f"- kill rule met: {label}" for label, hit in kills.items() if hit], "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    table.to_csv(out / "summary.csv")
    pd.DataFrame({"baseline": base["returns"], "buffered": primary["returns"]}).to_csv(out / "daily_returns.csv")
    if not args.no_ledger:
        governance.record_trials("h9_study", 7, family=HYPOTHESIS, data=f"binance perps (daily), Kraken-listed, {start:%Y-%m-%d} to 2025-12-31", details={"verdict": verdict, "turnover_cut": cut})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
