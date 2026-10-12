"""H12, a slow version of the taker basket: run the pre-registered test (research/prereg/H12.txt).

    python scripts/research/h12_study.py            # writes data/research/h12_<time>/, logs the signal card and 9 trials
    python scripts/research/h12_study.py --no-ledger
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
from src.research import signal_checks as sc  # noqa: E402
from src.research import signal_lab as lab  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402
from src.research.portfolio import PortfolioCosts, simulate_portfolio, slippage_by_liquidity  # noqa: E402

HYPOTHESIS = "H12"
CARD = "taker-buy share, 90 days"
QUANTILE, BAND, TOP_N, MIN_NAMES = 0.20, 0.01, 30, 10
BASE_DAYS, SLOW_DAYS, CARD_HORIZON = 10, 30, 30
DAYS = 365.0
KEYS = ["turnover", "return", "sharpe", "max_drawdown", "vol", "costs", "funding_received", "return_before_costs", "names_per_side", "replaced_per_rebalance"]


def scores(wide: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """The rankings of the pre-registration, each from data up to that day's close."""
    taker, volume = wide["taker_buy_quote_volume"], wide["quote_volume"]

    def share(days: int, least: int) -> pd.DataFrame:
        return taker.rolling(days, min_periods=least).sum() / volume.rolling(days, min_periods=least).sum() - 0.5

    fast, slow = lab.features(wide)["taker_buy_share_7d"], share(90, 60)
    return {"fast": fast, "slow": slow, "slow_180": share(180, 120), "fast_part": fast - slow}


def replaced_share(weights: pd.DataFrame) -> float:
    """Of the names held after a rebalance, the average share that was not held on that side before it."""
    side = np.sign(weights)
    changed = (side != side.shift(1)).any(axis=1) & (side.shift(1).abs().sum(axis=1) > 0) & (side.abs().sum(axis=1) > 0)
    new = ((side != side.shift(1)) & (side != 0)).sum(axis=1)[changed]
    held = side.abs().sum(axis=1)[changed]
    return float((new / held).mean()) if len(held) else float("nan")


def run(wide: dict[str, pd.DataFrame], start: pd.Timestamp, score: pd.DataFrame, *, rebalance_days: int, cost_multiplier: float = 1.0, offset: int = 0) -> dict[str, object]:
    """One basket: ranked on `score`, rebalanced every `rebalance_days` from `start` plus `offset` days, measured from its first rebalance day."""
    first = start + pd.Timedelta(days=offset)
    weights = lab.buffered_rank_weights(score, wide["universe"], entry=QUANTILE, exit=QUANTILE, rebalance_days=rebalance_days, min_names=MIN_NAMES, start=first)
    costs = PortfolioCosts(fee_pct=0.05 * cost_multiplier, slippage_bps=slippage_by_liquidity(wide["quote_volume"]) * cost_multiplier)
    result = simulate_portfolio(wide["close"], weights, funding=wide["funding"], costs=costs, rebalance_every=1, rebalance_band=BAND)
    keep = result.returns.index >= first
    returns, held = result.returns[keep], weights[keep]
    numbers = lab.metrics(returns)
    return {"returns": returns, "weights": held, "sharpe": numbers["sharpe"], "return": numbers["return"], "max_drawdown": numbers["max_drawdown"], "vol": numbers["vol"],
            "turnover": float(result.turnover[keep].mean() * DAYS), "costs": float(result.costs[keep].mean() * DAYS), "funding_received": float(-result.funding[keep].mean() * DAYS),
            "return_before_costs": float((returns + result.costs[keep]).mean() * DAYS),
            "names_per_side": float(((held > 0).sum(axis=1) + (held < 0).sum(axis=1)).replace(0, np.nan).mean() / 2.0), "replaced_per_rebalance": replaced_share(held)}


def by_coin(wide: dict[str, pd.DataFrame], weights: pd.DataFrame) -> pd.Series:
    """Price P&L per coin over the run, as a fraction of equity a year: the weight held times the coin's next daily return."""
    ahead = wide["close"].pct_change(fill_method=None).shift(-1).reindex(weights.index)
    return ((weights * ahead).sum() * DAYS / len(weights)).sort_values(ascending=False)


def window_start(wide: dict[str, pd.DataFrame], earliest: pd.Timestamp) -> pd.Timestamp:
    names = wide["universe"].sum(axis=1)
    return names[(names >= MIN_NAMES) & (names.index >= earliest)].index[0]


def yearly(returns: pd.Series) -> pd.Series:
    return returns.groupby(returns.index.year).mean() * DAYS * 100


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true", help="log neither the card nor the trials (to reproduce a logged run)")
    args = parser.parse_args()
    ledger = not args.no_ledger
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h12_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    wide = lab.load_universe(top_n=TOP_N, listing="point_in_time")
    start = window_start(wide, lab.START)
    ranking = scores(wide)
    inside = wide["universe"].astype(bool)

    # Step 1: the signal card of the slow score, before any basket is run
    card = sc.signal_card(ranking["slow"].where(inside), wide["close"].where(inside), horizons=[CARD_HORIZON], name=CARD, direction=1, family=HYPOTHESIS, ledger=ledger)
    card.save(out / "signal_card")
    card_ic = float(card.by_horizon.at["IC", CARD_HORIZON])
    gate = sc.require_card(CARD, HYPOTHESIS) if ledger else {}
    header = [f"# H12: a slow version of the taker basket, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"## Signal card: {CARD}, {CARD_HORIZON}-day horizon (standard version {card.standard.version})", "",
              f"**{card.verdict}.** {card.reason}", "", md_table(card.table), "",
              *([f"The gate was passed by the override in the pre-registration: {gate['override']}", ""] if gate.get("override") else [])]
    if not card_ic > 0:
        report = [*header, "## Verdict", "", "- kill rule met: the signal card's IC of the slow score is zero or negative; no basket was run", "", "**KILL**"]
        (out / "report.md").write_text("\n".join(report))
        print("\n".join(report))
        return

    # Step 2: the baskets
    base = run(wide, start, ranking["fast"], rebalance_days=BASE_DAYS)
    slow = run(wide, start, ranking["slow"], rebalance_days=SLOW_DAYS)
    base_2x = run(wide, start, ranking["fast"], rebalance_days=BASE_DAYS, cost_multiplier=2.0)
    slow_2x = run(wide, start, ranking["slow"], rebalance_days=SLOW_DAYS, cost_multiplier=2.0)
    others = {"slow score, every 10 days": run(wide, start, ranking["slow"], rebalance_days=BASE_DAYS),
              "fast score, every 30 days": run(wide, start, ranking["fast"], rebalance_days=SLOW_DAYS),
              "fast part only, every 10 days": run(wide, start, ranking["fast_part"], rebalance_days=BASE_DAYS),
              "180-day share, every 30 days": run(wide, start, ranking["slow_180"], rebalance_days=SLOW_DAYS)}
    moved = {offset: run(wide, start, ranking["slow"], rebalance_days=SLOW_DAYS, offset=offset) for offset in (10, 20)}
    today = lab.load_universe(top_n=TOP_N, listing="today")
    today_start, today_ranking = window_start(today, lab.START), scores(today)
    today_base, today_slow = run(today, today_start, today_ranking["fast"], rebalance_days=BASE_DAYS), run(today, today_start, today_ranking["slow"], rebalance_days=SLOW_DAYS)

    difference = (slow["returns"] - base["returns"]).to_numpy()
    estimate, low, high = block_bootstrap_ci(difference, block=20, runs=2000, seed=0, statistic=lambda values: float(np.mean(values) * DAYS))
    columns = {"baseline (7-day, every 10)": base, "slow basket (90-day, every 30)": slow, "baseline, 2x costs": base_2x, "slow basket, 2x costs": slow_2x, **others,
               **{f"slow basket, moved {offset} days": result for offset, result in moved.items()}}
    table = pd.DataFrame({label: {key: result[key] for key in KEYS} for label, result in columns.items()})
    years = pd.DataFrame({"baseline return %": yearly(base["returns"]), "slow basket return %": yearly(slow["returns"])})
    years["difference"] = years["slow basket return %"] - years["baseline return %"]
    coins = by_coin(wide, slow["weights"])
    earned = float(coins.sum())
    fast_part = others["fast part only, every 10 days"]

    cut = 1.0 - slow["turnover"] / base["turnover"]
    checks = {"1. turnover at least 50% lower": cut >= 0.50, "2. net return not more than 1 point lower": slow["return"] >= base["return"] - 0.01,
              "3. at 2x costs the slow basket's return is higher": slow_2x["return"] > base_2x["return"]}
    kills = {"the slow basket's net return is zero or negative": not slow["return"] > 0, "net return more than 3 points lower": slow["return"] < base["return"] - 0.03}
    verdict = "KILL" if any(kills.values()) else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    report = [*header,
              "## Baskets", "",
              f"Window {start:%Y-%m-%d} to {base['returns'].index[-1]:%Y-%m-%d} ({len(base['returns'])} days); point-in-time Kraken list, top {TOP_N}, {QUANTILE:.0%} legs.", "",
              md_table(table, digits=3), "",
              "Turnover is in times the book a year; return, costs and funding as fractions a year (0.10 = 10%); `replaced_per_rebalance` is the share of the names held "
              "after a rebalance that were not held on that side before it. The baseline's figures are H9's (turnover 51.4, return +18.4%, Sharpe 0.72).", "",
              f"Turnover cut by the slow basket: {cut:.0%}. Net return {slow['return'] - base['return']:+.1%} a year against the baseline "
              f"(difference of daily returns {estimate:+.1%}, 95% interval {low:+.1%} to {high:+.1%}).",
              f"The fast part alone (7-day share minus 90-day share): {fast_part['return_before_costs']:+.1%} a year before costs, {fast_part['return']:+.1%} after.", "",
              "## Per year", "", md_table(years, digits=1), "",
              "## The slow basket's price P&L by coin", "",
              f"{int((coins != 0).sum())} coins held at some point; price P&L {earned:+.1%} a year in total. Best coin {coins.index[0]} {coins.iloc[0]:+.1%} "
              f"({coins.iloc[0] / earned:.0%} of the total), best three {', '.join(coins.index[:3])} {coins.iloc[:3].sum():+.1%} ({coins.iloc[:3].sum() / earned:.0%}); "
              f"worst coin {coins.index[-1]} {coins.iloc[-1]:+.1%}." if earned else "No price P&L.", "",
              f"## Today's Kraken list from {today_start:%Y-%m-%d} (the original study's universe; reported only)", "",
              f"Baseline: turnover {today_base['turnover']:.1f}x, return {today_base['return']:+.1%}, Sharpe {today_base['sharpe']:.2f}. "
              f"Slow basket: turnover {today_slow['turnover']:.1f}x, return {today_slow['return']:+.1%}, Sharpe {today_slow['sharpe']:.2f}.", "",
              "## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()], *[f"- kill rule met: {label}" for label, hit in kills.items() if hit], "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    table.to_csv(out / "summary.csv")
    coins.rename("price_pnl_per_year").to_csv(out / "slow_basket_by_coin.csv")
    pd.DataFrame({"baseline": base["returns"], "slow": slow["returns"], **{label: result["returns"] for label, result in others.items()}}).to_csv(out / "daily_returns.csv")
    if ledger:
        governance.record_trials("h12_study", 9, family=HYPOTHESIS, data=f"binance perps (daily), Kraken-listed, {start:%Y-%m-%d} to 2025-12-31",
                                 details={"verdict": verdict, "turnover_cut": cut, "return_difference": slow["return"] - base["return"], "card": card.verdict})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
