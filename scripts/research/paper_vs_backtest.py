"""Compare a running (paper or live) book with the research backtest over the same days.

    python scripts/research/paper_vs_backtest.py config/portfolio.btc_live.toml             # the paper book
    python scripts/research/paper_vs_backtest.py config/portfolio.btc_live.toml --live      # its live book ("<name>-live")

Replays the backtest (whole lots, the config's costs) on the candles the runtime trades on, up to now, and compares
it bar by bar with the book's stored snapshots: the position held after each decision, and the return since the first
compared bar. See src/portfolio/paper_check.py for how to read the result.

It needs this year's candles, which research is otherwise locked out of (the frozen holdout). The backtest here selects
nothing: it is the reference the running book is checked against. The look is logged in the trial ledger as such.
A basket's signal data comes from Binance's public API (about one request per coin).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from src.portfolio import paper_check  # noqa: E402
from src.portfolio.backtest import prepare_inputs, run_book  # noqa: E402
from src.portfolio.config import load_portfolio_config  # noqa: E402
from src.portfolio.engine import live_book_name  # noqa: E402
from src.portfolio.feed import kraken_history_loader  # noqa: E402
from src.research import governance  # noqa: E402
from src.storage.trade_logger import TradeLogger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config")
    parser.add_argument("--live", action="store_true", help="Compare the config's live book instead of its paper book")
    parser.add_argument("--database", default=None)
    args = parser.parse_args()
    config = load_portfolio_config(args.config)
    name = live_book_name(config.name) if args.live else config.name
    snapshots = TradeLogger(database_path=args.database or settings.database_path).list_portfolio_snapshots(portfolio=name)
    if not snapshots:
        print(f"No snapshots for '{name}': the book hasn't run yet.")
        return
    panel = None
    if config.baskets:
        from src.portfolio.basket import fetch_binance_panel

        panel = lambda spec: fetch_binance_panel(list(spec.coins), days=400)  # noqa: E731 - the runtime's own source, with more history
    with governance.final_holdout(f"paper-versus-backtest reconciliation of {name}: a reference for the running book, no selection"):
        inputs = prepare_inputs(config, bar_loader=kraken_history_loader, basket_panel=panel)
        first = sorted(snapshots, key=lambda snap: snap["timestamp"])[0]
        decisions = paper_check.decision_frame(snapshots, inputs.grid_interval)
        if len(decisions):  # start the backtest where the book started, with the book's capital
            inputs = paper_check.start_at(inputs, decisions.index[0])
        book = run_book(replace(config, initial_equity=float(first.get("initial_equity") or config.initial_equity)), inputs, lots=True)
    result = paper_check.compare(snapshots, book, inputs.grid_interval)
    summary = result["summary"]
    print(f"'{name}': {len(snapshots)} snapshots; backtest on {inputs.grid_interval} bars to {inputs.prices.index[-1]:%Y-%m-%d %H:%M} UTC")
    if not summary["bars"]:
        print(paper_check.verdict(summary, tolerance=0.0))
        return
    prices = inputs.prices.iloc[-1].to_dict()
    equity = float(sorted(snapshots, key=lambda snap: snap["timestamp"])[-1]["equity"])
    tolerance = max(config.rebalance_band, paper_check.one_lot_weight(config, prices, equity))
    print(f"{summary['bars']} decision bars compared, {summary['first_bar']:%Y-%m-%d %H:%M} to {summary['last_bar']:%Y-%m-%d %H:%M} UTC")
    print(paper_check.verdict(summary, tolerance=tolerance))
    print(f"Return since the first compared bar: running book {summary['paper_return']:+.2%}, backtest {summary['backtest_return']:+.2%}")
    with pd.option_context("display.width", 200, "display.max_rows", 60, "display.float_format", lambda value: f"{value:+.4f}"):
        latest = result["positions"][result["positions"]["bar"] == summary["last_bar"]].drop(columns=["bar"]).set_index("instrument")
        print(f"\nPositions at the last compared bar (share of equity):\n{latest.sort_values('difference', key=abs, ascending=False).head(20).to_string()}")
        worst = result["positions"].reindex(result["positions"]["difference"].abs().sort_values(ascending=False).index).head(8)
        print(f"\nLargest gaps at any bar:\n{worst.to_string(index=False)}")
    out = Path("data/research") / f"paper_vs_backtest_{name}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    result["positions"].to_csv(out / "positions.csv", index=False)
    result["returns"].to_csv(out / "returns.csv")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
