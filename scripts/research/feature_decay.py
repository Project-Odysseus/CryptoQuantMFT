"""IC decay by horizon: for every feature, how its information fades with the holding period, next to the cost.

Features are the positioning study's (price, funding, open interest, trader ratios, taker flow, implied vol) on
Kraken perpetual 1h bars for BTC and ETH, frozen final holdout excluded. For each feature and horizon: the IC,
its t-stat on non-overlapping samples, and how far the top and bottom quintiles' forward returns sit from the
average, in bps, against a round trip on Kraken perps (taker ~20 bps). Measure this before writing any trading
rule: the horizon where the IC peaks is the natural holding period, and a feature whose best bucket can't clear
the round trip there won't pay however the rule is tuned.

Usage:
    python scripts/research/feature_decay.py
    python scripts/research/feature_decay.py --coins BTC --round-trip-bps 4   # maker costs
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from src.data.positioning import load_positioning
from src.research import load_bars
from src.research.features import bars_frame, ic_decay
from src.research.governance import write_manifest

HORIZONS = {"1h": 1, "4h": 4, "12h": 12, "1d": 24, "3d": 72, "7d": 168}


def _positioning_study():
    spec = importlib.util.spec_from_file_location("positioning_study", Path(__file__).with_name("positioning_study.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--round-trip-bps", type=float, default=20.0, help="Cost of one trade in and out (Kraken perp taker ~20)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or Path("data/research") / f"feature_decay_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    write_manifest(out, args=args)
    study = _positioning_study()

    rows = []
    for coin in args.coins:
        frame = bars_frame(load_bars(f"{coin}/USD", "1h", source="perp"))
        positioning = load_positioning(coin, frame.index + pd.Timedelta(hours=1)).set_index(frame.index).ffill(limit=study.DAY)
        features = study.build_features(frame, positioning)
        close = frame["close"].to_numpy()
        for name in features.columns:
            table = ic_decay(features[name].to_numpy(), close, HORIZONS, round_trip_bps=args.round_trip_bps)
            rows += [{"coin": coin, "feature": name, **row} for row in table.to_dict("records")]
        print(f"{coin}: {len(frame):,} hourly bars, {frame.index[0]:%Y-%m-%d} to {frame.index[-1]:%Y-%m-%d}")
    results = pd.DataFrame(rows)
    results.to_csv(out / "ic_decay.csv", index=False)

    pd.set_option("display.width", 220)
    ic = results.pivot_table(index="feature", columns="horizon", values="ic", aggfunc="mean")[list(HORIZONS)]
    edge = results.pivot_table(index="feature", columns="horizon", values="best_edge_bps", aggfunc="mean")[list(HORIZONS)]
    peak = ic.abs().idxmax(axis=1)
    summary = ic.round(3)
    summary["peak_horizon"] = peak
    summary["edge_at_peak_bps"] = [round(edge.loc[name, horizon], 1) for name, horizon in peak.items()]
    ic_t = results.pivot_table(index="feature", columns="horizon", values="ic_t", aggfunc="mean")
    summary["ic_t_at_peak"] = [round(ic_t.loc[name, horizon], 1) for name, horizon in peak.items()]
    summary["clears_cost_at_peak"] = summary["edge_at_peak_bps"] > args.round_trip_bps
    summary["verdict"] = ["candidate" if clears and abs(t) >= 2 else "noise" if abs(t) < 2 else "too small for costs"
                          for clears, t in zip(summary["clears_cost_at_peak"], summary["ic_t_at_peak"])]
    summary = summary.reindex(ic.abs().max(axis=1).sort_values(ascending=False).index)
    print(f"\nIC by horizon (averaged over {', '.join(args.coins)}), the peak, and the best quintile's edge there vs a {args.round_trip_bps:g} bps round trip:\n")
    print(summary.to_string())
    print("\nverdict: 'candidate' = |t| >= 2 on non-overlapping samples and the best quintile clears the round trip; long horizons have few")
    print("independent samples (about 300 weeks), so their big bps edges are mostly noise unless the t-stat backs them.")
    print(f"Per-coin detail in {out / 'ic_decay.csv'}")


if __name__ == "__main__":
    main()
