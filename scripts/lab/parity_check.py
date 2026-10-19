"""Compare a paper (or live) book with the research engine replayed on the same days (plan 7.3-7.4).

    python scripts/lab/parity_check.py --config config/portfolio.equity_trend_paper.toml --spec smooth_trend

Reads the book's snapshots from the trading database (TradeLogger), takes the last snapshot of each day, and maps
each sleeve's instrument to the lab instrument it is a proxy of (the master's `proxy_for`). The spec is replayed with
quantlab on the lab's data for those days (the frozen holdout is opened for this, logged as operational access).

Timing: a snapshot on day X holds the position traded on X, which earns from X's close; in the engine that is the
position decided at X-1's close, `held` one day later. Both are compared as weights of the book's equity.

Prints position parity, P&L parity and the expectation check, and records the result as a run in the lab's store.
Exits 1 when parity or the expectation check flags something. Read-only for the book.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the book's portfolio TOML")
    parser.add_argument("--spec", required=True, help="the strategy spec the book runs (id or path)")
    parser.add_argument("--min-days", type=int, default=5)
    args = parser.parse_args()

    from config import settings
    from quantlab.deploy.monitor import expectation_check, operational_lab, parity
    from quantlab.deploy.spec import backtest_spec, find_spec, load_spec
    from quantlab.lab import use_project_root
    from src.portfolio.config import load_portfolio_config
    from src.storage.trade_logger import TradeLogger

    use_project_root()
    config = load_portfolio_config(args.config)
    spec = load_spec(find_spec(args.spec))
    snapshots = TradeLogger(settings.database_path).list_portfolio_snapshots(portfolio=config.name)
    if not snapshots:
        print(f"no snapshots for {config.name}")
        return 1
    frame = pd.DataFrame(snapshots)
    frame["day"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(None).dt.normalize()
    daily = frame.sort_values("timestamp").groupby("day").last()
    if len(daily) < args.min_days:
        print(f"{config.name}: {len(daily)} days of snapshots; parity needs at least {args.min_days}. Nothing to compare yet.")
        return 0

    lab = operational_lab(spec, start=str((daily.index[0] - pd.DateOffset(years=2)).date()), reason=f"parity of {config.name}")
    proxies = {lab._master[name].proxy_for: name for name in lab.instruments if lab._master[name].proxy_for}
    live = pd.DataFrame([{proxies[item["instrument"]]: float(item.get("allocated_weight", 0.0)) for item in row.values() if item.get("instrument") in proxies}
                         for row in daily["sleeves"]], index=daily.index).fillna(0.0)
    result = backtest_spec(spec, lab=lab)
    research = result.held.shift(-1)
    equity = daily["equity"].astype(float)
    report = parity(live, research, live_pnl=equity.pct_change().dropna(), research_pnl=result.pnl)
    print(f"Parity of {config.name} against {spec.key} over {len(daily)} days:")
    print(report["positions"].round(3).to_string())
    if "pnl" in report:
        print(report["pnl"].to_string())
    expectations = expectation_check(equity.pct_change().dropna(), spec.expectations)
    print("Against expectations:")
    print(expectations.to_string() if len(expectations) else "  nothing to compare yet")
    from quantlab.tracking.store import ExperimentStore

    with ExperimentStore().run(spec.strategy.hypothesis or None, f"parity {config.name}", config={"spec": spec.key, "book": config.name}) as run:
        run.log_metrics({f"{instrument} sign agreement": row["sign agreement"] for instrument, row in report["positions"].iterrows()})
        if "pnl" in report:
            run.log_metrics({"tracking error": report["pnl"]["tracking error (annual)"], "cumulative gap": report["pnl"]["cumulative gap"]})
    flagged = not report["ok"] or expectations.attrs.get("overall") == "flag"
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
