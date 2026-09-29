"""The taker-buy basket as the portfolio engine trades it: on Kraken perp prices, with the runtime's own code.

    python scripts/research/basket_backtest.py [config/portfolio.taker_paper.toml] [--trend config/portfolio.btc_live.toml]

The research study (research log 2026-09-26) used Binance daily closes as the price proxy. This replays the chosen
configuration (top 30, 20% legs, every 10 days: fixed before this run, nothing tuned here) through `run_book` on
Kraken's own perp candles, with each instrument's slippage from the config and Kraken's taker fee, and prints:

1. the basket alone, in-sample and holdout (split 2024-10-01), at 1x and 2x costs;
2. against BTC buy-and-hold (beta, alpha, capture);
3. its daily correlation with the trend book and blends of the two.

A coin joins the universe only once Kraken lists its perp, so early years hold fewer coins. The frozen final holdout
(2026 on) stays locked. Logs one trial (family "cross_sectional").
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.portfolio.backtest import daily_returns, period_metrics, prepare_inputs, run_book  # noqa: E402
from src.portfolio.config import load_portfolio_config  # noqa: E402
from src.research.bar_engine import performance  # noqa: E402
from src.research.benchmark import benchmark_metrics  # noqa: E402
from src.research.governance import record_trials, write_manifest  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

HOLDOUT = pd.Timestamp("2024-10-01", tz="UTC")


def doubled_costs(config):
    """The same config with every instrument's fee and slippage doubled."""
    return replace(config, instruments={key: replace(spec, fee_pct=spec.taker_fee_pct * 2, slippage_bps=spec.slippage_bps * 2)
                                        for key, spec in config.instruments.items()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?", default="config/portfolio.taker_paper.toml")
    parser.add_argument("--trend", default="config/portfolio.btc_live.toml", help="The trend book to compare and blend with")
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    config = load_portfolio_config(args.config)
    inputs = prepare_inputs(config)
    basket = run_book(config, inputs, risk_overlay=True)
    costly = run_book(doubled_costs(config), inputs, risk_overlay=True)
    rows = period_metrics(basket, HOLDOUT, label="basket") + period_metrics(costly, HOLDOUT, label="basket, 2x costs")

    returns = daily_returns(basket)
    coins = [column for column in basket.targets.columns if basket.targets[column].abs().sum() > 0]
    held = (basket.targets.abs() > 0).sum(axis=1)
    btc_price = inputs.prices.get("kraken_futures:BTC/USD")
    versus = {}
    if btc_price is not None:
        btc = btc_price.resample("1D").last().pct_change().dropna()
        for period, part in (("is", returns[returns.index < HOLDOUT]), ("ho", returns[returns.index >= HOLDOUT])):
            versus[period] = benchmark_metrics(part, btc)

    trend_config = load_portfolio_config(args.trend)
    trend = daily_returns(run_book(trend_config, prepare_inputs(trend_config), risk_overlay=True))
    both = pd.concat({"trend": trend, "basket": returns}, axis=1).dropna()
    blends = []
    for share in (0.0, 0.3, 0.5, 1.0):
        mix = (1 - share) * both["trend"] + share * both["basket"]
        for period, part in (("is", mix[mix.index < HOLDOUT]), ("ho", mix[mix.index >= HOLDOUT])):
            stats = performance(part, periods_per_year=365)
            blends.append({"basket share": share, "period": period, "sharpe": stats["sharpe"], "ann_return": stats["ann_return"], "max_drawdown": stats["max_drawdown"]})

    if not args.no_ledger:
        record_trials("basket_backtest", 1, family="cross_sectional", data="kraken perps to 2025-12-31",
                      details={"config": args.config, "basket": [b.id for b in config.baskets], "note": "pre-chosen top30/q0.2/10d replayed on Kraken prices"})
    out = Path("data/research") / f"basket_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    write_manifest(out, args=args)
    report = [
        f"# Basket on Kraken prices ({inputs.measure_start:%Y-%m-%d} to {inputs.prices.index[-1]:%Y-%m-%d}, holdout from {HOLDOUT:%Y-%m-%d})", "",
        f"{len(coins)} coins traded at some point; coins held per day: median {held[held > 0].median():.0f}, max {held.max()}.", "",
        md_table(pd.DataFrame(rows), index=False), "",
        "Against BTC buy-and-hold:", "", md_table(pd.DataFrame(versus).T[["beta", "alpha_ann", "correlation", "up_capture", "down_capture"]]) if versus else "(no BTC price)", "",
        f"Daily correlation with the trend book ({args.trend}): {both['trend'].corr(both['basket']):.2f} "
        f"(in-sample {both[both.index < HOLDOUT].corr().iloc[0, 1]:.2f}, holdout {both[both.index >= HOLDOUT].corr().iloc[0, 1]:.2f})", "",
        "Blends of daily returns:", "", md_table(pd.DataFrame(blends), index=False), "",
    ]
    (out / "report.md").write_text("\n".join(report))
    print("\n".join(report))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
