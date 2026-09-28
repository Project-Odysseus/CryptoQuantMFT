"""H2, liquidation overshoot (PROXY): run the pre-registered event study (research/prereg/H2.txt) on development data.

    python scripts/research/h2_study.py      # writes data/research/h2_<time>/ (tables, raw report, manifest)

Every hold x event-definition configuration is logged to the trial ledger (family "H2"). The holdout stays locked.
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
from src.research.bar_engine import funding_by_bar, simulate_trade  # noqa: E402
from src.research.costs import VENUES  # noqa: E402
from src.research.hypotheses.common import cost_at, md_table  # noqa: E402
from src.research.hypotheses.h2_liquidation import (  # noqa: E402
    LOOSE,
    PRIMARY,
    detect_events,
    entries,
    exclusion_mask,
    forward_log_returns,
    hourly_frame,
    news_driven,
)
from src.research.inference import block_bootstrap_ci, deflated_sharpe, log_trial  # noqa: E402
from src.research.stats import sharpe_per_period  # noqa: E402

COINS = ("BTC", "ETH", "SOL")
HOLDS = (12, 24, 48)
CONTROLS, TOLERANCE = 20, 0.2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placebo-runs", type=int, default=1000)
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("data/research") / f"h2_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)

    fomc = pd.read_csv("research/data/fomc_statements.csv")["statement_utc"]
    frames, costs, costs_stress, funding = {}, {}, {}, {}
    for coin in COINS:
        frame = hourly_frame(coin)
        frame = frame[frame["oi"].notna().cummax()]  # from the first open-interest value on
        frames[coin] = frame.reset_index(drop=True)  # positional from here on; `timestamp` stays a column
        costs_stress[coin] = cost_at(frame, frame.index, VENUES["kraken_perp"], coin, spread_stress=3.0)
        funding[coin] = funding_by_bar(frame.index, pit.load_funding(coin, "binance"))
    data_range = f"{min(f['timestamp'].min() for f in frames.values()):%Y-%m-%d}..{max(f['timestamp'].max() for f in frames.values()):%Y-%m-%d}"

    counts = {coin: detect_events(frames[coin], PRIMARY) for coin in COINS}
    primary_total = sum(len(e) for e in counts.values())
    rules = [PRIMARY] + ([LOOSE] if primary_total < 30 else [])

    summary_rows, breakdown_rows, trade_tables, event_tables, placebo_rows, dsr_rows = [], [], [], [], [], []
    coverage_rows = [{"coin": coin, "first_bar": f"{frames[coin]['timestamp'].min():%Y-%m-%d}", "last_bar": f"{frames[coin]['timestamp'].max():%Y-%m-%d}",
                      "hours": len(frames[coin]), "primary_events": len(counts[coin]),
                      "loose_events": len(detect_events(frames[coin], LOOSE))} for coin in COINS]
    for rule in rules:
        per_coin = {}
        for coin in COINS:
            frame = frames[coin]
            events = entries(frame, detect_events(frame, rule))
            events = events[events["entry"] < len(frame)].reset_index(drop=True)
            events["coin"] = coin
            events["time"] = frame["timestamp"].iloc[events["t"]].to_numpy()
            events["news_fomc"] = news_driven(frame, events, fomc) if len(events) else []
            per_coin[coin] = events
        all_events = pd.concat(per_coin.values(), ignore_index=True)
        event_tables.append(all_events.assign(rule=rule.name))

        for hold in HOLDS:
            car_rows, trades = [], []
            placebo_pools: list[tuple[str, int, np.ndarray]] = []
            for coin in COINS:
                frame, events = frames[coin], per_coin[coin]
                if events.empty:
                    continue
                fwd = forward_log_returns(frame, hold)
                exclude = exclusion_mask(len(frame), events)
                matching = np.r_[np.nan, frame["rv24"].to_numpy()[:-1]]  # trailing vol known at each bar's open
                eligible = np.flatnonzero(~exclude & np.isfinite(fwd) & np.isfinite(matching) & (matching > 0))
                for event in events.itertuples():
                    if not np.isfinite(fwd[event.entry]):
                        continue
                    level = matching[event.entry]
                    pool = eligible[np.abs(matching[eligible] / level - 1.0) <= TOLERANCE] if np.isfinite(level) and level > 0 else np.array([], dtype=int)
                    controls = rng.choice(pool, size=min(CONTROLS, len(pool)), replace=False) if len(pool) else pool
                    car = event.direction * fwd[event.entry]
                    baseline = event.direction * float(np.mean(fwd[controls])) if len(controls) else np.nan
                    placebo_pools.append((coin, event.direction, pool))
                    trade = simulate_trade(frame, event.entry, event.direction, hold, stop=event.stop, cost_per_side=float(costs_stress[coin][event.entry]),
                                           funding_rates=funding[coin])
                    trade2 = simulate_trade(frame, event.entry, event.direction, hold, stop=event.stop, cost_per_side=2 * float(costs_stress[coin][event.entry]),
                                            funding_rates=funding[coin])
                    car_rows.append({"coin": coin, "time": event.time, "direction": event.direction, "news_fomc": event.news_fomc, "car": car, "baseline": baseline,
                                     "abnormal": car - baseline, "controls": len(controls), "gross": trade["gross"], "net": trade["net"], "net_2x": trade2["net"],
                                     "stopped": trade["stopped"], "cost_side": float(costs_stress[coin][event.entry])})
            cars = pd.DataFrame(car_rows)
            if cars.empty:
                continue
            cars["year"] = pd.DatetimeIndex(cars["time"]).year
            trade_tables.append(cars.assign(rule=rule.name, hold=hold))
            abnormal = cars["abnormal"].dropna()
            est, low, high = block_bootstrap_ci(abnormal, block=1, runs=5000)
            car_est, car_low, car_high = block_bootstrap_ci(cars["car"], block=1, runs=5000)
            forward = {coin: forward_log_returns(frames[coin], hold) for coin in COINS}
            usable = [(forward[coin], direction, pool) for coin, direction, pool in placebo_pools if len(pool)]
            placebo_means = [np.mean([direction * values[rng.choice(pool)] for values, direction, pool in usable]) for _ in range(args.placebo_runs)]
            placebo_means = np.asarray(placebo_means)
            p_placebo = float(np.mean(placebo_means >= cars["car"].mean()))
            net_est, net_low, net_high = block_bootstrap_ci(cars["net"], block=1, runs=5000)
            years = cars.groupby("year")["net"].mean()
            coins_positive = int((cars.groupby("coin")["net"].mean() > 0).sum())
            label = f"{rule.name} hold={hold}h"
            per_trade_sr = sharpe_per_period(cars["net"])
            trades_per_year = len(cars) / max((pd.Timestamp(data_range.split("..")[1]) - pd.Timestamp(data_range.split("..")[0])).days / 365.0, 1e-9)
            if not args.no_ledger:
                log_trial("H2", "h2_study", {"rule": rule.name, "hold": hold}, data_range=data_range, gross_sharpe=sharpe_per_period(cars["gross"]) * np.sqrt(trades_per_year),
                          net_sharpe=per_trade_sr * np.sqrt(trades_per_year), net_sharpe_2x=sharpe_per_period(cars["net_2x"]) * np.sqrt(trades_per_year),
                          trades=len(cars), sharpe_per_period_net=per_trade_sr, extra={"proxy": True})
            summary_rows.append({"config": label, "events": len(cars), "mean_car_bps": cars["car"].mean() * 1e4, "car_ci_bps": f"{car_low * 1e4:.0f}..{car_high * 1e4:.0f}",
                                 "mean_abnormal_bps": est * 1e4, "abnormal_ci_bps": f"{low * 1e4:.0f}..{high * 1e4:.0f}", "placebo_p": p_placebo,
                                 "mean_gross_bps": cars["gross"].mean() * 1e4, "mean_net_bps": cars["net"].mean() * 1e4, "net_ci_bps": f"{net_low * 1e4:.0f}..{net_high * 1e4:.0f}",
                                 "mean_net_2x_bps": cars["net_2x"].mean() * 1e4, "hit_rate": float((cars["net"] > 0).mean()), "stopped": float(cars["stopped"].mean()),
                                 "mean_cost_side_bps": cars["cost_side"].mean() * 1e4, "coins_positive": coins_positive,
                                 "years_positive": f"{int((years > 0).sum())}/{len(years)}", "trades_per_year": trades_per_year,
                                 "ann_sharpe_net": per_trade_sr * np.sqrt(trades_per_year)})
            placebo_rows.append({"config": label, "real_mean_car_bps": cars["car"].mean() * 1e4, "placebo_median_bps": float(np.median(placebo_means)) * 1e4,
                                 "placebo_95_bps": float(np.quantile(placebo_means, 0.95)) * 1e4, "p": p_placebo})
            dsr_rows.append({"config": label, **deflated_sharpe(cars["net"], "H2")})
            for key in ("direction", "coin", "news_fomc", "year"):
                for value, group in cars.groupby(key):
                    breakdown_rows.append({"config": label, "by": key, "value": value, "events": len(group), "mean_car_bps": group["car"].mean() * 1e4,
                                           "mean_abnormal_bps": group["abnormal"].mean() * 1e4, "mean_net_bps": group["net"].mean() * 1e4})

    summary, breakdown, placebo, dsr = pd.DataFrame(summary_rows), pd.DataFrame(breakdown_rows), pd.DataFrame(placebo_rows), pd.DataFrame(dsr_rows)
    coverage = pd.DataFrame(coverage_rows)
    events_all = pd.concat(event_tables, ignore_index=True) if event_tables else pd.DataFrame()
    trades_all = pd.concat(trade_tables, ignore_index=True) if trade_tables else pd.DataFrame()
    for name, frame in {"coverage": coverage, "summary": summary, "breakdown": breakdown, "placebo": placebo, "dsr": dsr, "events": events_all, "trades": trades_all}.items():
        frame.to_csv(out / f"{name}.csv", index=False)
    governance.write_manifest(out, args=args, data=data_range, extra={"hypothesis": "H2", "prereg": "research/prereg/H2.txt", "proxy": True})

    report = ["# H2 results — PROXY events (development data only; holdout locked)", "", f"Run {stamp}, commit `{governance.git_commit()}`, outputs in `{out}`.", "",
              f"Primary events pooled: {primary_total}; LOOSE definition {'run (primary < 30)' if LOOSE in rules else 'not run'}.", "",
              "## Coverage", "", md_table(coverage, index=False), "", "## Event study and trades", "", md_table(summary, index=False, digits=3), "",
              "## Placebo (random vol-matched pseudo-events)", "", md_table(placebo, index=False), "", "## Breakdowns", "", md_table(breakdown, index=False, digits=1), "",
              "## Deflated Sharpe (per-trade)", "", md_table(dsr, index=False), ""]
    (out / "report_raw.md").write_text("\n".join(report))
    print("\n".join(report))


if __name__ == "__main__":
    main()
