"""H4, what downside protection bought as Kalshi No contracts costs and returns: run the pre-registered study (research/prereg/H4.txt).

    python scripts/research/h4_study.py --window pilot      # snapshots recorded before 2026-10-04: a pipeline check
    python scripts/research/h4_study.py --window confirm    # 2026-10-05 to 2026-11-16: run once, after 2026-11-17
    python scripts/research/h4_study.py --collect           # any time before that: fetch and cache the confirmatory window's
                                                            # Kalshi quotes for the chains recorded so far; prints counts only

Reads the recorded Deribit chains and Kalshi's public data (quotes at each snapshot, settlements); the panel is shared
with H5 (src/research/hypotheses/binary_panel.py) and cached under data/research/prediction_markets/. Refuses to
run until the pre-registration is locked, and refuses the confirmatory window before it is over. Every run is logged
to the trial ledger (family "H4"). Research only: nothing is traded.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.options.deribit import load_chains  # noqa: E402
from src.research import governance  # noqa: E402
from src.research import prediction_markets as pm  # noqa: E402
from src.research.hypotheses import binary_panel as bp  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

HYPOTHESIS = "H4"
WINDOWS = {"pilot": (None, "2026-10-04"), "confirm": ("2026-10-05", "2026-11-16")}
CONFIRM_NOT_BEFORE = datetime(2026, 11, 17, tzinfo=timezone.utc)
MIN_EVENTS = 20
PANEL = Path("data/research/prediction_markets/panel")


def load_panel(start: str | None, end: str | None) -> pd.DataFrame:
    """The shared panel for a window: snapshots whose rows have all settled come from the cache, the rest are rebuilt."""
    chains = load_chains("BTC", start=start, end=end)
    if chains.empty:
        return pd.DataFrame()
    PANEL.mkdir(parents=True, exist_ok=True)
    client, parts = pm.KalshiClient(), []
    for stamp, chain in chains.groupby("timestamp"):
        path = PANEL / f"{pd.Timestamp(stamp):%Y%m%dT%H%M%S}.parquet"
        if path.exists():
            parts.append(pd.read_parquet(path))
            continue
        part = bp.build_panel(chain, client)
        if len(part) and part["yes"].notna().all():
            part.to_parquet(path, index=False)
        parts.append(part)
    return pd.concat([part for part in parts if len(part)], ignore_index=True) if any(len(part) for part in parts) else pd.DataFrame()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", choices=sorted(WINDOWS))
    parser.add_argument("--collect", action="store_true", help="Cache the confirmatory window's panel for the chains recorded so far and print row counts only (no results)")
    parser.add_argument("--no-ledger", action="store_true", help="Don't log the trials (for a rerun after a code fix, not for new tries)")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    if args.collect:
        # Kalshi may not keep minute quotes forever, and the test must not be read early: store the data, show nothing of it
        panel = load_panel(*WINDOWS["confirm"])
        settled = int(panel["yes"].notna().sum()) if len(panel) else 0
        print(f"Confirmatory window so far: {len(panel)} rows, {panel['time'].nunique() if len(panel) else 0} snapshots, "
              f"{panel['event'].nunique() if len(panel) else 0} daily events, {settled} rows settled and cached in {PANEL}. No results are shown before {CONFIRM_NOT_BEFORE:%Y-%m-%d}.")
        return
    if args.window is None:
        parser.error("pass --window pilot|confirm, or --collect")
    if args.window == "confirm" and datetime.now(timezone.utc) < CONFIRM_NOT_BEFORE:
        raise SystemExit(f"The confirmatory window ends 2026-11-16; run this once, on or after {CONFIRM_NOT_BEFORE:%Y-%m-%d}.")
    start, end = WINDOWS[args.window]
    panel = load_panel(start, end)
    if panel.empty:
        raise SystemExit("No rows: no recorded chain snapshot in this window had a quoted Kalshi market in range.")
    out = Path("data/research") / f"h4_{args.window}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})
    panel.to_csv(out / "panel.csv", index=False)
    label = "PILOT (pipeline check; no conclusion is drawn)" if args.window == "pilot" else "CONFIRMATORY"
    print(f"{HYPOTHESIS} {label}: {len(panel)} rows from {panel['time'].nunique()} snapshots, {panel['event'].nunique()} daily events, "
          f"{panel['time'].min():%Y-%m-%d %H:%M} to {panel['time'].max():%Y-%m-%d %H:%M} UTC; {int(panel['yes'].notna().sum())} rows settled")
    result = bp.h4_protection(panel)
    events = result["events"]
    print(f"Protection rows (level 0.5-2% below the index, 2+ hours left): {result['rows']}, of which settled {result['settled_rows']} in {events} events")
    print(f"Primary: mean profit per contract of buying No at the ask plus the fee: {bp.describe_interval(result['pnl'])}")
    print(f"  mean cost {result['mean_cost']:.3f}; breach rate {result['breach_rate']:.3f}; options-implied fair value {result['mean_fair']:.3f}; richness (cost - fair) {bp.describe_interval(result['richness'])}")
    print(f"  covering a long down to the level costs {result['mean_cover_bps']:.1f} bps of the position on average")
    print(f"Deribit put spread on {result['put_spread_rows']} comparable rows: {result['put_spread_buy']:.3f} per unit of payout, against Kalshi's {result['kalshi_cost_same_rows']:.3f} (a ramp at another expiry: context only)")
    result["detail"].to_csv(out / "protection_rows.csv", index=False)
    if args.window == "confirm" and events < MIN_EVENTS:
        print(f"Only {events} events: the pre-registration needs {MIN_EVENTS}. Extend the window's end as it says, and rerun then.")
    if not args.no_ledger:
        governance.record_trials(f"h4_study", 1, family=HYPOTHESIS, data=f"kalshi KXBTCD daily + deribit chains, {args.window} window",
                                 details={"window": args.window, "rows": int(len(panel)), "events": int(events), "prereg_version": lock["version"]})
    print(f"Wrote {out}. Judge the confirmatory run against research/prereg/{HYPOTHESIS}.txt, then write research/reports/{HYPOTHESIS}.md and the research log entry.")


if __name__ == "__main__":
    main()
