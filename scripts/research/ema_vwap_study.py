"""EMA x VWAP study: enter on a close through the VWAP, exit on a close back through the EMA; long-only and long/short.

The rule came from Instagram (3-minute candles, long only). This sweeps the catalog grid for `ema_vwap` (EMA
span, session or rolling VWAP, the EMA-vs-VWAP trend filter, flat at the end of each UTC day) on Kraken perpetual
candles, long-only and long/short, at taker and at maker costs, with the last 30% of each series as a holdout.

Sources (`--source`):
- kraken (default): Kraken perpetual candles. 15m and 1h use the cached history (BTC and ETH, 2020 onwards); 3m is
  built from 1-minute trade candles (`--days`, default 90), about 17 MB per coin: pass `--allow-download` on wifi.
- binance: Binance's public archive of 3m/15m/1h candles for whole months (`--months`), perpetuals or spot
  (`--market`), for any coins (`--symbols` in Binance form, e.g. SOLUSDT). Costs stay Kraken's: perps 0.05% taker /
  0.02% maker, spot 0.40% taker / 0.25% maker. Needs `--allow-download` the first time (a few MB per coin-month).

Usage:
    python scripts/research/ema_vwap_study.py                                # 15m and 1h, BTC and ETH perps
    python scripts/research/ema_vwap_study.py --intervals 3m --days 90 --allow-download
    python scripts/research/ema_vwap_study.py --source binance --market spot --intervals 3m --months 2026-06 2026-07 2026-08 --allow-download
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
from src.research.governance import write_manifest

MINUTES = {"3m": 3, "15m": 15, "1h": 60}
BINANCE_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT", "DOTUSDT"]


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


def load(symbol: str, interval: str, days: int, *, source: str = "kraken", market: str = "futures", months: list[str] | None = None) -> list[OHLCVBar]:
    if source == "binance":
        from src.data.binance_archive import load_klines

        return load_klines(symbol, interval, months or [], market=market)
    if interval == "3m":
        start = datetime.now(timezone.utc) - timedelta(days=days)
        return resample(load_bars(symbol, 60, source="perp", start=start), 3)
    return load_bars(symbol, interval, source="perp")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--intervals", nargs="*", default=["15m", "1h"], choices=sorted(MINUTES))
    parser.add_argument("--symbols", nargs="*", default=None, help="Default: BTC/USD ETH/USD (kraken) or ten liquid coins (binance)")
    parser.add_argument("--source", choices=["kraken", "binance"], default="kraken")
    parser.add_argument("--market", choices=["futures", "spot"], default="futures", help="binance only")
    parser.add_argument("--months", nargs="*", default=["2026-06", "2026-07", "2026-08"], help="binance only: whole months")
    parser.add_argument("--days", type=int, default=90, help="History for 3m (from 1-minute candles)")
    parser.add_argument("--allow-download", action="store_true", help="Needed for 3m: fetches 1-minute candles (~17 MB per coin per 90 days)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if (args.source == "binance" or "3m" in args.intervals) and not args.allow_download:
        parser.error("this needs a download (Kraken 1-minute or Binance archive candles): add --allow-download")
    symbols = args.symbols or (["BTC/USD", "ETH/USD"] if args.source == "kraken" else BINANCE_COINS)
    spot = args.source == "binance" and args.market == "spot"
    cost_cases = (("taker", CostSettings.spot()), ("maker", CostSettings.spot(maker=True))) if spot else (("taker", CostSettings.perp()), ("maker", CostSettings.perp(maker=True)))

    out = args.out or Path("data/research") / f"ema_vwap_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    write_manifest(out, args=args)  # commit, arguments and frozen-holdout state, for reproducing the run
    frames = []
    for interval in args.intervals:
        data = {symbol: load(symbol, interval, args.days, source=args.source, market=args.market, months=args.months) for symbol in symbols}
        data = {symbol: bars for symbol, bars in data.items() if len(bars) > 1000}
        for symbol, bars in data.items():
            print(f"{symbol} {interval}: {len(bars):,} bars, {bars[0].timestamp:%Y-%m-%d} to {bars[-1].timestamp:%Y-%m-%d}")
        for cost_name, costs in cost_cases:
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
    trip = "taker ~1.0% (0.40% fee + 10 bps slippage a side), maker ~0.50%" if spot else "taker ~0.20% (0.05% fee + 5 bps slippage a side), maker ~0.04%"
    print(f"\nRound trip: {trip} (maker fills assumed at the close). Results in {out}")


if __name__ == "__main__":
    main()
