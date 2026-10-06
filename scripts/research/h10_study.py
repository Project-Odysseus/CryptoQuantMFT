"""H10, the US morning's move continues into the afternoon: run the pre-registered test (research/prereg/H10.txt).

    python scripts/research/h10_study.py            # writes data/research/h10_<time>/ and logs 6 trials
    python scripts/research/h10_study.py --no-ledger
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance, pit  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.hypotheses.h10_session import rule_returns, session_trades  # noqa: E402
from src.research.inference import block_bootstrap_ci, newey_west_ols  # noqa: E402

HYPOTHESIS = "H10"
COINS = ("BTC", "ETH")
TAKER_BPS, MAKER_BPS = 11.0, 4.0
SPLIT = pd.Timestamp("2023-07-01")
GRID = [("10:30", "16:00"), ("10:30", "20:00"), ("11:30", "16:00"), ("11:30", "20:00")]
BPS = 10_000.0


def pooled(per_coin: dict[str, pd.Series]) -> pd.Series:
    """The book's return per day: each coin gets half of the capital."""
    return pd.DataFrame(per_coin).mean(axis=1, skipna=True)


HOLDOUT_FROM, HOLDOUT_TO = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-09-30")


def holdout(args: argparse.Namespace, lock: dict) -> None:
    """The final holdout test of version 2: rules A and B on 2026-01-01 to 2026-09-30, looked at once."""
    if int(lock["version"]) < 2:
        raise SystemExit("the holdout test is defined in version 2 of the pre-registration; lock that first")
    out = Path("data/research") / f"h10_holdout_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})
    with governance.final_holdout("H10 final holdout test (pre-registration v2, approved by the owner 2026-10-06): rules A and B, 2026-01-01 to 2026-09-30"):
        bars = {coin: pit.load_bars(coin, "5m") for coin in COINS}
    trades = {coin: session_trades(bars[coin]) for coin in COINS}
    window = lambda series: series[(series.index >= HOLDOUT_FROM) & (series.index <= HOLDOUT_TO)]  # noqa: E731
    gross = {coin: window(rule_returns(trades[coin])) for coin in COINS}
    net = {coin: window(rule_returns(trades[coin], cost_bps=TAKER_BPS)) for coin in COINS}
    large = {}
    for coin in COINS:
        size = trades[coin]["signal"].abs()
        usual = size.rolling(60, min_periods=30).median().shift(1)
        large[coin] = window(rule_returns(trades[coin][size > usual], cost_bps=TAKER_BPS))
    book_gross, book_net, book_large = pooled(gross), pooled(net), pooled(large)
    book_maker = pooled({coin: window(rule_returns(trades[coin], cost_bps=MAKER_BPS)) for coin in COINS})
    _estimate, low, high = block_bootstrap_ci(book_gross.to_numpy(), block=20, runs=2000, seed=0)
    _estimate_b, low_b, high_b = block_bootstrap_ci(book_large.to_numpy(), block=20, runs=2000, seed=0)

    def bp(series: pd.Series) -> float:
        return float(series.mean() * BPS)

    verdict = "REJECTED" if bp(book_gross) <= 0 else "SURVIVES" if bp(book_gross) >= TAKER_BPS and bp(book_large) > 0 else "UNDECIDED"
    monthly = pd.DataFrame({"gross bp": book_gross.groupby(book_gross.index.month).mean() * BPS, "days": book_gross.groupby(book_gross.index.month).size()})
    report = [f"# H10 final holdout test, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"{len(book_gross)} weekdays, {book_gross.index[0]:%Y-%m-%d} to {book_gross.index[-1]:%Y-%m-%d}.", "",
              f"**Rule A:** gross {bp(book_gross):+.1f} bp a trade (95% interval {low * BPS:+.1f} to {high * BPS:+.1f}); net of 11 bp {bp(book_net):+.1f}; net of 4 bp {bp(book_maker):+.1f}. "
              f"Right sign on {float(pooled({coin: (gross[coin] > 0).astype(float) for coin in COINS}).mean()):.0%} of days.",
              f"Per coin, gross: " + "; ".join(f"{coin} {bp(gross[coin]):+.1f}" for coin in COINS) + ".",
              f"**Rule B** (larger-than-usual mornings, {int(book_large.notna().sum())} days): net of 11 bp {bp(book_large):+.1f} bp (95% interval {low_b * BPS:+.1f} to {high_b * BPS:+.1f}).", "",
              "## Per month (rule A, gross)", "", md_table(monthly, digits=1), "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    pd.DataFrame({"gross": book_gross, "net_11bp": book_net, "large_net_11bp": book_large}).to_csv(out / "daily.csv")
    if not args.no_ledger:
        governance.record_trials("h10_holdout", 2, family=HYPOTHESIS, data="binance perp 5m, BTC and ETH, 2026-01-01 to 2026-09-30 (final holdout)", details={"verdict": verdict, "gross_bp": bp(book_gross)})
    print("\n".join(report))
    print(f"\nWrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true")
    parser.add_argument("--holdout", action="store_true", help="The one approved look at the frozen 2026 window (version 2 of the pre-registration)")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    if args.holdout:
        holdout(args, lock)
        return
    out = Path("data/research") / f"h10_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    bars = {coin: pit.load_bars(coin, "5m") for coin in COINS}
    trades = {coin: session_trades(bars[coin]) for coin in COINS}
    gross = {coin: rule_returns(trades[coin]) for coin in COINS}
    net = {coin: rule_returns(trades[coin], cost_bps=TAKER_BPS) for coin in COINS}
    book_gross, book_net, book_maker = pooled(gross), pooled(net), pooled({coin: rule_returns(trades[coin], cost_bps=MAKER_BPS) for coin in COINS})
    estimate, low, high = block_bootstrap_ci(book_net.to_numpy(), block=20, runs=2000, seed=0)

    grid_rows = []
    for signal_end, exit in GRID:
        cell = pooled({coin: rule_returns(session_trades(bars[coin], signal_end=signal_end, exit=exit), cost_bps=TAKER_BPS) for coin in COINS})
        cell_gross = pooled({coin: rule_returns(session_trades(bars[coin], signal_end=signal_end, exit=exit)) for coin in COINS})
        grid_rows.append({"signal": f"09:30-{signal_end}", "exit": exit, "gross bp": cell_gross.mean() * BPS, "net bp at 11": cell.mean() * BPS, "trades": int(cell.notna().sum())})
    grid = pd.DataFrame(grid_rows).set_index(["signal", "exit"])

    large = {}
    for coin in COINS:
        size = trades[coin]["signal"].abs()
        usual = size.rolling(60, min_periods=30).median().shift(1)  # the median of the 60 mornings before this one
        large[coin] = rule_returns(trades[coin][size > usual], cost_bps=TAKER_BPS)
    book_large = pooled(large)

    slopes = {}
    for coin in COINS:
        fit = newey_west_ols(trades[coin]["hold"].to_numpy(), trades[coin]["signal"].to_numpy(), lags=5)
        row = fit.iloc[-1]
        slopes[coin] = (float(row.iloc[0]), float(row["t"]) if "t" in fit.columns else float("nan"))

    def bp(series: pd.Series) -> float:
        return float(series.mean() * BPS)

    by_year = pd.DataFrame({"gross bp": book_gross.groupby(book_gross.index.year).mean() * BPS, "net bp at 11": book_net.groupby(book_net.index.year).mean() * BPS,
                            "share of days right": pooled({coin: (gross[coin] > 0).astype(float) for coin in COINS}).groupby(book_gross.index.year).mean()})
    by_weekday = pd.DataFrame({"gross bp": book_gross.groupby(book_gross.index.dayofweek).mean() * BPS})
    by_weekday.index = ["Mon", "Tue", "Wed", "Thu", "Fri"][: len(by_weekday)]

    halves = {"first half": bp(book_net[book_net.index < SPLIT]), "second half": bp(book_net[book_net.index >= SPLIT])}
    checks = {"1. net mean at 11 bp positive, interval above 0": estimate > 0 and low > 0,
              "2. positive net in both halves": all(value > 0 for value in halves.values()),
              "3. positive net for BTC and for ETH": all(bp(net[coin]) > 0 for coin in COINS),
              "4. at least 3 of 4 grid cells positive net": int((grid["net bp at 11"] > 0).sum()) >= 3}
    killed = bp(book_gross) < 5.5
    verdict = "KILL" if killed else "PASS" if all(checks.values()) else "INCONCLUSIVE"
    yearly_sharpe = float(book_net.mean() / book_net.std() * np.sqrt(252)) if book_net.std() > 0 else float("nan")

    report = [f"# H10: the US morning's move continues into the afternoon, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"{len(book_net)} weekdays, {book_net.index[0]:%Y-%m-%d} to {book_net.index[-1]:%Y-%m-%d}; BTC and ETH, half each.", "",
              f"**Primary** (morning 09:30-11:30, hold 11:35-16:00 New York): gross {bp(book_gross):+.1f} bp a trade; net of 11 bp {estimate * BPS:+.1f} bp "
              f"(95% interval {low * BPS:+.1f} to {high * BPS:+.1f}); net of 4 bp {bp(book_maker):+.1f} bp. As a book at 11 bp: Sharpe {yearly_sharpe:.2f}.",
              f"Per coin, gross / net at 11 bp: " + "; ".join(f"{coin} {bp(gross[coin]):+.1f} / {bp(net[coin]):+.1f}" for coin in COINS)
              + f". Halves, net: {halves['first half']:+.1f} then {halves['second half']:+.1f}.",
              f"Slope of the afternoon on the morning return: " + "; ".join(f"{coin} {slope:+.3f} (t {t:+.1f})" for coin, (slope, t) in slopes.items()) + ".",
              f"Only after a larger-than-usual morning ({int(book_large.notna().sum())} days): net {bp(book_large):+.1f} bp.",
              f"The mirror rule (fade the morning): gross {-bp(book_gross):+.1f} bp, net of 11 bp {-bp(book_gross) - TAKER_BPS:+.1f} bp.", "",
              "## Grid (pooled)", "", md_table(grid, digits=1), "", "## Per year", "", md_table(by_year, digits=2), "", "## Per weekday", "", md_table(by_weekday.T, digits=1), "",
              "## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()], *(["- kill rule met: gross mean below 5.5 bp"] if killed else []), "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    pd.DataFrame({"gross": book_gross, "net_11bp": book_net}).to_csv(out / "daily.csv")
    grid.to_csv(out / "grid.csv")
    if not args.no_ledger:
        governance.record_trials("h10_study", 6, family=HYPOTHESIS, data="binance perp 5m, BTC and ETH, 2021-03 to 2025-12-31", details={"verdict": verdict, "gross_bp": bp(book_gross)})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
