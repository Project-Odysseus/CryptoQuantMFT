"""The book's trend rules on 4-hour bars for BTC, ETH and SOL, next to their daily versions. A look, not a test.

    python scripts/research/trend_4h_study.py

Each rule runs alone through `run_book` on Kraken perp candles with the config's costs and funding, sized to 50%
yearly volatility like the sleeves in the paper books. Two ways to put a daily rule on 4-hour bars are shown:

- the same numbers on shorter bars (a faster rule: 8/96 bars for the average, as the BTC book's 4-hour sleeve uses;
  40 bars for the channel);
- the same horizon in days on finer bars (24/288 bars = 4/48 days; 240 bars = 40 days).

In-sample to 2024-09-30, holdout 2024-10-01 to 2025-12-31 (the frozen 2026 holdout stays locked). Nothing is chosen
from this: it is logged as 15 looks (family "trend") and no pass or fail is attached.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.portfolio.backtest import prepare_inputs, run_book  # noqa: E402
from src.portfolio.config import parse_portfolio_config  # noqa: E402
from src.research import governance  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

HOLDOUT = pd.Timestamp("2024-10-01", tz="UTC")
COINS = {"BTC": (0.0001, 5), "ETH": (0.001, 5), "SOL": (0.01, 5)}
RULES = {
    "trend 1d (4/48 days)": ("1d", "moving_average_crossover", {"short_window": 4, "long_window": 48}, True),
    "trend 4h, faster (8/96 bars)": ("4h", "moving_average_crossover", {"short_window": 8, "long_window": 96}, True),
    "trend 4h, same horizon (24/288 bars)": ("4h", "moving_average_crossover", {"short_window": 24, "long_window": 288}, True),
    "breakout 1d (40 days)": ("1d", "keltner_breakout", {"window": 40, "atr_multiplier": 2.0}, False),
    "breakout 4h, faster (40 bars)": ("4h", "keltner_breakout", {"window": 40, "atr_multiplier": 2.0}, False),
    "breakout 4h, same horizon (240 bars)": ("4h", "keltner_breakout", {"window": 240, "atr_multiplier": 2.0}, False),
}


def config_for(coin: str, rule: str):
    interval, strategy, params, long_only = RULES[rule]
    instrument = f"kraken_futures:{coin}/USD"
    lot, slippage = COINS[coin]
    return parse_portfolio_config({
        "portfolio": {"name": "trend-4h-look", "initial_equity": 10_000, "allocation": "equal", "rebalance_band": 0.02},
        "risk": {"max_drawdown": 0.99, "max_gross_exposure": 5.0, "max_net_exposure": 5.0, "max_instrument_weight": 5.0, "daily_loss_limit": 0.99},
        "instruments": {instrument: {"kind": "perp", "max_leverage": 5.0, "lot_step": lot, "min_order_size": lot, "slippage_bps": slippage}},
        "sleeves": [{"id": "rule", "instrument": instrument, "interval": interval, "strategy": strategy, "params": params, "long_only": long_only,
                     "sizing": "vol_target", "sizing_params": {"target_annual_vol": 0.5}}],
    })


def main() -> None:
    rows = []
    for coin in COINS:
        for rule in RULES:
            config = config_for(coin, rule)
            book = run_book(config, prepare_inputs(config), risk_overlay=False)
            row = {"coin": coin, "rule": rule, "from": f"{book.result.returns.index[0]:%Y-%m}"}
            for label, start, end in (("in-sample", None, HOLDOUT), ("holdout", HOLDOUT, None)):
                metrics = book.result.metrics(start, end)
                row.update({f"Sharpe {label}": metrics.get("sharpe"), f"return {label}": metrics.get("cagr"), f"drawdown {label}": metrics.get("max_drawdown"),
                            f"turnover {label}": metrics.get("turnover_per_year"), f"costs % {label}": metrics.get("cost_pct_per_year")})
            rows.append(row)
            print(f"{coin} {rule}: Sharpe {row['Sharpe in-sample']:.2f} / {row['Sharpe holdout']:.2f}", flush=True)
    table = pd.DataFrame(rows).set_index(["coin", "rule"])
    out = Path("data/research") / f"trend_4h_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out)
    table.to_csv(out / "summary.csv")
    governance.record_trials("trend_4h_study", 15, family="trend", data="kraken perps (1d, 4h) to 2025-12-31",
                             details={"note": "a look at the book's rules on 4h bars; 3 of the 18 cells are the existing sleeves", "coins": list(COINS)})
    columns = ["from", "Sharpe in-sample", "Sharpe holdout", "return holdout", "drawdown holdout", "turnover holdout", "costs % holdout"]
    print("\n" + md_table(table[columns], digits=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
