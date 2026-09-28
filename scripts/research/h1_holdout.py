"""H1b final holdout: the one approved look at the frozen data for "carry, Binance fees, 14-day hold".

    python scripts/research/h1_holdout.py

Follows research/prereg/H1.txt, addendum 2: the carry state machine runs continuously from the start of the data,
and only days from 2026-01-01 are scored. The look is logged to the trial ledger by the governance module. Don't
run this again to "check" a changed rule: the holdout is spent for this candidate.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research.bar_engine import performance  # noqa: E402
from src.research.costs import VENUES  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.hypotheses.h1_crowding import build_signals, prepare_market, run_carry  # noqa: E402

COINS = ("BTC", "ETH", "SOL")
FEES, HOLD = "binance", 14
REASON = "H1b carry, Binance fees, 14-day hold: approved by the owner 2026-09-28"


def main() -> None:
    start = pd.Timestamp(governance.FINAL_HOLDOUT_START)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("data/research") / f"h1_holdout_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    nets, nets2, always, rows, monthly = {}, {}, {}, [], {}
    with governance.final_holdout(REASON):
        for coin in COINS:
            signals = build_signals(coin)
            market = prepare_market(coin, signals, VENUES)
            frame, info = run_carry(signals, market, VENUES, fees=FEES, expected_hold_days=HOLD)
            frame2, _ = run_carry(signals, market, VENUES, fees=FEES, expected_hold_days=HOLD, multiplier=2.0)
            on, _ = run_carry(signals, market, VENUES, fees=FEES, expected_hold_days=HOLD, always_on=True)
            held = frame.loc[start:]
            nets[coin], nets2[coin], always[coin] = held["net"], frame2.loc[start:, "net"], on.loc[start:, "net"]
            f_ann = signals["F_ann"].loc[start:]
            previous = frame["position"].shift(1, fill_value=0.0).loc[start:]
            entries = int(((held["position"] > 0) & (previous == 0)).sum())
            rows.append({"coin": coin, "days": len(held), "last_day": f"{held.index.max():%Y-%m-%d}", "in_position_on_2026-01-01": bool(held["position"].iloc[0] > 0),
                         "time_in": float(held["position"].mean()), "entries": entries, "hurdle_ann_pct": info["hurdle_ann"] * 100,
                         "mean_F_ann_pct": float(f_ann.mean() * 100), "days_F_above_hurdle": int((f_ann > info["hurdle_ann"]).sum()),
                         "funding_pct": float(held["funding"].sum() * 100), "basis_pnl_pct": float((held["spot"] + held["perp"]).sum() * 100),
                         "cost_pct": float(held["cost"].sum() * 100), "net_pct": float(held["net"].sum() * 100),
                         "net_2x_pct": float(nets2[coin].sum() * 100), "always_on_net_pct": float(always[coin].sum() * 100)})
            monthly[coin] = held["net"].groupby(held.index.tz_localize(None).to_period("M")).sum() * 100
    pooled = pd.DataFrame(nets).mean(axis=1).dropna()
    pooled2 = pd.DataFrame(nets2).mean(axis=1).dropna()
    pooled_always = pd.DataFrame(always).mean(axis=1).dropna()
    perf = performance(pooled, periods_per_year=365)
    summary = pd.DataFrame([{"window": f"{pooled.index.min():%Y-%m-%d}..{pooled.index.max():%Y-%m-%d}", "pooled_net_pct": pooled.sum() * 100,
                             "pooled_net_2x_pct": pooled2.sum() * 100, "pooled_always_on_pct": pooled_always.sum() * 100,
                             "ann_net_pct": perf["ann_return"] * 100, "sharpe": perf["sharpe"] if pooled.std() > 0 else float("nan"),
                             "max_dd_pct": perf["max_drawdown"] * 100, "PASS (net > 0)": bool(pooled.sum() > 0)}])
    per_coin, months = pd.DataFrame(rows), pd.DataFrame(monthly)
    for name, frame in {"summary": summary, "per_coin": per_coin, "monthly": months}.items():
        frame.to_csv(out / f"{name}.csv")
    governance.write_manifest(out, args={"fees": FEES, "hold": HOLD}, data="holdout", extra={"reason": REASON})
    report = ["## Holdout (one look, 2026-09-28)", "", f"`{REASON}`. Outputs in `{out}`.", "", md_table(summary, index=False), "",
              "Per coin (% of notional over the window):", "", md_table(per_coin, index=False), "", "Net by month (%):", "", md_table(months), ""]
    print("\n".join(report))
    (out / "report.md").write_text("\n".join(report))


if __name__ == "__main__":
    main()
