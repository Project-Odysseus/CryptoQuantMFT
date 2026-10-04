"""H7, halve the trend rules after a flat stretch: run the pre-registered test (research/prereg/H7.txt).

    python scripts/research/h7_study.py            # writes data/research/h7_<time>/ and logs 1 trial
    python scripts/research/h7_study.py --no-ledger

The label is `regime_study.labels`. The test sample is everything before 2022-10-10, which the look that produced the
hypothesis did not cover; the later window is printed next to it for reference only.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from regime_study import labels  # noqa: E402
from src.portfolio.backtest import daily_returns, prepare_inputs, run_book  # noqa: E402
from src.portfolio.config import parse_portfolio_config  # noqa: E402
from src.research import governance, pit  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402

HYPOTHESIS = "H7"
SLEEVES = ("btc_ma_1d", "btc_keltner_ls", "eth_ma_1d", "eth_keltner_ls")
SEEN_FROM = pd.Timestamp("2022-10-10", tz="UTC")
SWITCH_COST = 0.0005
DAYS = 365.0


def trend_config():
    """The four BTC and ETH trend strategies of the multi-strategy book, nothing else."""
    raw = tomllib.loads(Path("config/portfolio.multi_paper.toml").read_text())
    raw.pop("baskets", None)
    raw.pop("execution", None)
    raw["sleeves"] = [sleeve for sleeve in raw["sleeves"] if sleeve["id"] in SLEEVES]
    used = {sleeve["instrument"] for sleeve in raw["sleeves"]}
    raw["instruments"] = {name: spec for name, spec in raw["instruments"].items() if name in used}
    raw["risk"] = {key: value for key, value in raw.get("risk", {}).items() if key not in ("groups", "benchmark")}
    return parse_portfolio_config(raw)


def sharpe(values: pd.Series) -> float:
    return float(values.mean() / values.std() * np.sqrt(DAYS)) if len(values) > 30 and values.std() > 0 else float("nan")


def drawdown(values: pd.Series) -> float:
    equity = (1.0 + values).cumprod()
    return float((1.0 - equity / equity.cummax()).max())


def describe(returns: pd.DataFrame, flat: pd.Series) -> dict[str, object]:
    book = returns.mean(axis=1)
    size = np.where(flat, 0.5, 1.0)
    switches = np.abs(np.diff(size, prepend=size[0])) > 0
    gated = book * size - switches * SWITCH_COST
    other = ~flat
    pairs = np.column_stack([book.to_numpy(), flat.to_numpy(dtype=float)])

    def difference(indices_block: np.ndarray) -> float:
        values, is_flat = indices_block[:, 0], indices_block[:, 1] > 0.5
        return float((values[is_flat].mean() - values[~is_flat].mean()) * DAYS) if is_flat.any() and (~is_flat).any() else float("nan")

    rng = np.random.default_rng(0)
    count, block = len(pairs), 20
    starts = rng.integers(0, count, size=(2000, int(np.ceil(count / block))))
    index = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(2000, -1)[:, :count] % count
    draws = np.array([difference(pairs[row]) for row in index])
    return {"days": len(book), "flat_days": int(flat.sum()), "flat_return": float(book[flat].mean() * DAYS), "other_return": float(book[other].mean() * DAYS),
            "difference": difference(pairs), "ci_low": float(np.nanquantile(draws, 0.025)), "ci_high": float(np.nanquantile(draws, 0.975)),
            "flat_sharpe": sharpe(book[flat]), "ungated_sharpe": sharpe(book), "gated_sharpe": sharpe(gated), "ungated_return": float(book.mean() * DAYS),
            "gated_return": float(gated.mean() * DAYS), "ungated_drawdown": drawdown(book), "gated_drawdown": drawdown(gated), "switches": int(switches.sum()),
            "per_strategy_flat": (returns[flat].mean() * DAYS).to_dict(),
            "per_year_flat": {int(year): float(value) for year, value in (book[flat].groupby(book[flat].index.year).mean() * DAYS).items()},
            "flat_days_per_year": {int(year): int(value) for year, value in flat.groupby(flat.index.year).sum().items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h7_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    config = trend_config()
    inputs = prepare_inputs(config)
    returns = pd.DataFrame({sleeve: daily_returns(run_book(config, inputs, allocation="equal", sleeves=[sleeve], risk_overlay=False)) for sleeve in SLEEVES}).fillna(0.0)
    returns.index = pd.DatetimeIndex(returns.index).tz_convert("UTC") if returns.index.tz is not None else pd.DatetimeIndex(returns.index).tz_localize("UTC")
    bitcoin = pit.load_bars("BTC", "1h")["close"].resample("1D").last().dropna()
    bitcoin.index = bitcoin.index + pd.Timedelta(days=1)
    direction = labels(bitcoin)["direction"].reindex(returns.index)
    returns = returns[direction.notna() & (direction != "n/a")]
    flat = (direction.reindex(returns.index) == "flat")

    test = describe(returns[returns.index < SEEN_FROM], flat[flat.index < SEEN_FROM])
    seen = describe(returns[returns.index >= SEEN_FROM], flat[flat.index >= SEEN_FROM])
    checks = {"1. return on flat days negative": test["flat_return"] < 0, "2. flat minus other negative, interval excludes 0": test["difference"] < 0 and test["ci_high"] < 0,
              "3. gated Sharpe >= ungated": test["gated_sharpe"] >= test["ungated_sharpe"]}
    verdict = "KILL" if test["flat_return"] > test["other_return"] else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    keys = ["days", "flat_days", "flat_return", "other_return", "difference", "ci_low", "ci_high", "flat_sharpe", "ungated_sharpe", "gated_sharpe", "ungated_return", "gated_return",
            "ungated_drawdown", "gated_drawdown", "switches"]
    first = returns.index[0]
    table = pd.DataFrame({f"test sample ({first:%Y-%m-%d} to 2022-10-09)": {key: test[key] for key in keys}, "seen before (2022-10-10 on)": {key: seen[key] for key in keys}})
    report = [f"# H7: halve the trend rules after a flat stretch, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "", md_table(table, digits=3), "",
              "Returns are per year as a fraction (0.10 = 10%).", "",
              f"Test sample, flat days per strategy (a year): " + ", ".join(f"{name} {value:+.0%}" for name, value in test["per_strategy_flat"].items()),
              f"Test sample, trend book on flat days per year: " + ", ".join(f"{year} {value:+.0%} ({test['flat_days_per_year'].get(year, 0)} days)" for year, value in test["per_year_flat"].items()), "",
              "## Verdict (test sample)", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()], "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    table.to_csv(out / "summary.csv")
    if not args.no_ledger:
        governance.record_trials("h7_study", 1, family=HYPOTHESIS, data=f"kraken BTC/ETH perps (daily) {first:%Y-%m-%d} to 2025-12-31", details={"verdict": verdict, "difference": test["difference"]})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
