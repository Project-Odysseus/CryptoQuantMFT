"""A multi-strategy book under the correlation-aware limits: which allocation, and what does each limit cost or save?

    python scripts/research/multi_book_study.py [config/portfolio.multi_paper.toml]

Runs the config (trend sleeves plus the taker-buy basket) through `run_book` on Kraken perp candles and compares:

1. **allocation** between the units (each trend sleeve and the basket as a whole): `equal`, `risk_parity`, `hrp`,
   all with the config's limits;
2. **limits**, under the config's allocation: only the plain caps (gross, net, per instrument, drawdown), then each
   correlation-aware limit added alone (group caps, the beta cap, the stressed-volatility cap), then all of them
   (the config as written).

For every book it prints performance in-sample and in the holdout (split 2024-10-01) and what the book was exposed
to: beta to BTC, volatility at crash correlations, the altcoin group's net. The limits' values are the config's,
fixed before this run; nothing is tuned here. Choose by the in-sample columns. The frozen final holdout (2026 on)
stays locked. Logs one trial per book (family "portfolio_construction").
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.portfolio.backtest import PortfolioBacktest, daily_returns, prepare_inputs, run_book  # noqa: E402
from src.portfolio.book_risk import exposure_history  # noqa: E402
from src.portfolio.config import PortfolioConfig, load_portfolio_config  # noqa: E402
from src.portfolio.risk_model import average_correlation, effective_bets  # noqa: E402
from src.research.governance import record_trials, write_manifest  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

HOLDOUT = pd.Timestamp("2024-10-01", tz="UTC")
PERFORMANCE = ("sharpe", "cagr", "vol", "max_drawdown", "turnover_per_year", "cost_pct_per_year")


def limit_variants(config: PortfolioConfig) -> dict[str, PortfolioConfig]:
    """The config with only its plain caps, with each correlation-aware limit alone, and as written."""
    plain = replace(config.risk, groups={}, max_beta_exposure=None, max_portfolio_vol=None)
    return {
        "plain caps only": replace(config, risk=plain),
        "+ group caps": replace(config, risk=replace(plain, groups=config.risk.groups)),
        "+ beta cap": replace(config, risk=replace(plain, max_beta_exposure=config.risk.max_beta_exposure)),
        "+ volatility cap": replace(config, risk=replace(plain, max_portfolio_vol=config.risk.max_portfolio_vol)),
        "all limits (the config)": config,
    }


def describe_book(label: str, config: PortfolioConfig, book: PortfolioBacktest, inputs) -> list[dict[str, object]]:
    """One row per period: performance, and the exposure the book actually carried."""
    exposure = exposure_history(config, inputs.grid_interval, inputs.prices, book.result.weights, warmup_prices=inputs.warmup_prices)
    rows = []
    for period, start, end in (("is", None, HOLDOUT), ("ho", HOLDOUT, None)):
        metrics = book.result.metrics(start, end)
        if not metrics:
            continue
        window = exposure[(exposure.index < HOLDOUT) if period == "is" else (exposure.index >= HOLDOUT)]
        rows.append({
            "book": label, "period": period, **{key: metrics[key] for key in PERFORMANCE},
            "avg gross": window["gross"].mean(), "avg |beta|": window["beta_exposure"].abs().mean(), "max |beta|": window["beta_exposure"].abs().max(),
            "avg stressed vol": window["stressed_volatility"].mean(), "max stressed vol": window["stressed_volatility"].max(),
            "max |alts net|": window["alts net"].abs().max() if "alts net" in window else float("nan"),
        })
    return rows


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
    alone = {unit: daily_returns(run_book(config, inputs, allocation="equal", sleeves=members, risk_overlay=False)) for unit, members in units.items()}
    in_sample = pd.DataFrame(alone)
    in_sample = in_sample[in_sample.index < HOLDOUT]
    correlation = in_sample.corr()

    allocation_rows: list[dict[str, object]] = []
    for method in ("equal", "risk_parity", "hrp"):
        allocation_rows += describe_book(method, config, run_book(config, inputs, allocation=method), inputs)
    limit_rows: list[dict[str, object]] = []
    variants = limit_variants(config)
    for label, variant in variants.items():
        limit_rows += describe_book(label, variant, run_book(variant, inputs), inputs)

    books = 3 + len(variants) - 1  # "all limits" under the config's allocation is one of the three allocation books
    if not args.no_ledger:
        record_trials("multi_book_study", books, family="portfolio_construction", data="kraken perps (daily) to 2025-12-31",
                      details={"config": args.config, "allocations": ["equal", "risk_parity", "hrp"], "limits": list(variants)})
    out = Path("data/research") / f"multi_book_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    write_manifest(out, args=args)
    pd.DataFrame(allocation_rows).to_csv(out / "allocations.csv", index=False)
    pd.DataFrame(limit_rows).to_csv(out / "limits.csv", index=False)
    correlation.to_csv(out / "unit_correlation.csv")
    report = [
        f"# Multi-strategy book: {config.name} ({inputs.measure_start:%Y-%m-%d} to {inputs.prices.index[-1]:%Y-%m-%d}, holdout from {HOLDOUT:%Y-%m-%d})", "",
        f"{len(units)} allocation units on {len(inputs.prices.columns)} instruments, {inputs.grid_interval} grid.", "",
        "Daily-return correlation of the units, each alone at full size, in-sample:", "", md_table(correlation.round(2)), "",
        f"They are worth {effective_bets(correlation.fillna(0.0)):.1f} independent bets (average correlation {average_correlation(correlation.fillna(0.0)):+.2f}).", "",
        "## Allocation between the units (the config's limits on)", "", md_table(pd.DataFrame(allocation_rows).round(3), index=False), "",
        f"## Limits (allocation '{config.allocation}')", "", md_table(pd.DataFrame(limit_rows).round(3), index=False), "",
    ]
    (out / "report.md").write_text("\n".join(report))
    print("\n".join(report))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
