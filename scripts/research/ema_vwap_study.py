"""EMA x VWAP study: enter on a close through the VWAP, exit on a close back through the EMA; long-only and long/short.

The rule came from Instagram (3-minute candles, long only). This sweeps the catalog grid for `ema_vwap` (EMA
span, session or rolling VWAP, the EMA-vs-VWAP trend filter, flat at the end of each UTC day) on Kraken perpetual
candles, long-only and long/short, at taker and at maker costs, with the last 30% of each series as a holdout.

Intervals:
- 15m and 1h use the cached perpetual history (BTC and ETH, 2020 onwards).
- 3m is built from Kraken Futures 1-minute trade candles (`--days`, default 90), resampled to 3 minutes. That is a
  download of about 17 MB per coin for 90 days: pass `--allow-download` on wifi.

Usage:
    python scripts/research/ema_vwap_study.py                                # 15m and 1h, BTC and ETH perps
    python scripts/research/ema_vwap_study.py --intervals 3m --days 90 --allow-download
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from src.research import CostSettings, load_bars, summarize, sweep
from src.storage.bar_aggregator import OHLCVBar

MINUTES = {"3m": 3, "15m": 15, "1h": 60}


def resample(bars: list[OHLCVBar], minutes: int) -> list[OHLCVBar]:
    """Aggregate 1-minute bars into complete `minutes` bars aligned to the hour (an incomplete last bar is dropped)."""
    frame = pd.DataFrame({"timestamp": [bar.timestamp for bar in bars], "open": [bar.open for bar in bars], "high": [bar.high for bar in bars],
                          "low": [bar.low for bar in bars], "close": [bar.close for bar in bars], "volume": [bar.volume for bar in bars]}).set_index("timestamp")
    grouped = frame.resample(f"{minutes}min", label="left", closed="left")
    out = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    counts = grouped["close"].count()
    out = out[counts.reindex(out.index) == minutes]
    symbol = bars[0].symbol if bars else ""
    return [OHLCVBar(exchange="kraken_futures", symbol=symbol, interval_seconds=60 * minutes, timestamp=stamp.to_pydatetime(), open=row.open, high=row.high,
                     low=row.low, close=row.close, volume=row.volume) for stamp, row in out.iterrows()]


def load(symbol: str, interval: str, days: int) -> list[OHLCVBar]:
    if interval == "3m":
        start = datetime.now(timezone.utc) - timedelta(days=days)
        return resample(load_bars(symbol, 60, source="perp", start=start), 3)
    return load_bars(symbol, interval, source="perp")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--intervals", nargs="*", default=["15m", "1h"], choices=sorted(MINUTES))
    parser.add_argument("--symbols", nargs="*", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--days", type=int, default=90, help="History for 3m (from 1-minute candles)")
    parser.add_argument("--allow-download", action="store_true", help="Needed for 3m: fetches 1-minute candles (~17 MB per coin per 90 days)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if "3m" in args.intervals and not args.allow_download:
        parser.error("3m needs 1-minute candles from Kraken (a download): add --allow-download")

    out = args.out or Path("data/research") / f"ema_vwap_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for interval in args.intervals:
        data = {symbol: load(symbol, interval, args.days) for symbol in args.symbols}
        for symbol, bars in data.items():
            print(f"{symbol} {interval}: {len(bars):,} bars, {bars[0].timestamp:%Y-%m-%d} to {bars[-1].timestamp:%Y-%m-%d}")
        for cost_name, costs in (("taker", CostSettings.perp()), ("maker", CostSettings.perp(maker=True))):
            results = sweep(data, "ema_vwap", costs=costs)
            results["costs"] = cost_name
            frames.append(results)
    results = pd.concat(frames, ignore_index=True)
    results.to_csv(out / "results.csv", index=False)

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    rows = []
    for cost_name, group in results.groupby("costs", sort=False):
        summary = summarize(group)
        summary.insert(1, "costs", cost_name)
        rows.append(summary)
    summary = pd.concat(rows, ignore_index=True)
    columns = ["strategy", "costs", "interval", "long_only", "combos", "share_positive_is", "median_is", "median_ho", "best_params", "best_is", "best_ho",
               "best_is_trades", "rank_corr", "buy_hold_is", "buy_hold_ho"]
    print("\nSharpe by interval, side and costs (every grid combo averaged over the coins; holdout = last 30%)\n")
    print(summary[columns].round(2).to_string(index=False))

    trades = results.groupby(["costs", "interval", "long_only"]).agg(trades_per_combo=("is_trades", "median"), avg_trade_pct=("is_avg_trade", "median")).round(4)
    print("\nMedian in-sample trades per combo and average trade (fraction of price, after costs):\n")
    print(trades.to_string())
    print(f"\nRound trip: taker ~0.20% (0.05% fee + 5 bps slippage a side), maker ~0.04% (fills assumed at the close). Results in {out}")


if __name__ == "__main__":
    main()
