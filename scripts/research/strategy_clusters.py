"""How many independent bets are the registered strategies? Correlate their returns and cluster them.

Each catalog strategy runs at its default parameters, long/short, on Kraken perpetual candles (BTC and ETH,
`--interval`, frozen final holdout excluded). Strategy returns are the position held into each bar times that
bar's return, summed per day and averaged over the coins. Strategies whose daily returns correlate above
`--threshold` (average linkage) land in one cluster: they are the same bet with different smoothing, and one
representative per cluster is enough. The effective number of bets, (sum of eigenvalues)^2 / sum of squared
eigenvalues of the correlation matrix, says how many independent strategies the set is worth.

Usage:
    python scripts/research/strategy_clusters.py
    python scripts/research/strategy_clusters.py --interval 1d --threshold 0.6
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from src.portfolio.risk_model import effective_bets
from src.research import load_bars
from src.research.catalog import CATALOG, build_strategy
from src.research.governance import write_manifest


def strategy_returns(name: str, bars: list) -> pd.Series:
    """Daily returns of `name` at its defaults: the signal held into each bar times the bar's return (no costs)."""
    signals = np.asarray(build_strategy(name).signal_series(bars), dtype=float)
    close = np.array([bar.close for bar in bars])
    bar_returns = np.r_[0.0, close[1:] / close[:-1] - 1.0]
    held = np.r_[0.0, signals[:-1]]  # decided at the previous close
    index = pd.DatetimeIndex([bar.timestamp for bar in bars])
    return pd.Series(held * bar_returns, index=index).resample("1D").sum()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--symbols", nargs="*", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--threshold", type=float, default=0.7, help="Correlation above which strategies count as one bet")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or Path("data/research") / f"strategy_clusters_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    write_manifest(out, args=args)

    data = {symbol: load_bars(symbol, args.interval, source="perp") for symbol in args.symbols}
    names = sorted(CATALOG)
    per_coin = {symbol: pd.DataFrame({name: strategy_returns(name, bars) for name in names}) for symbol, bars in data.items()}
    returns = sum(per_coin.values()) / len(per_coin)
    returns = returns.loc[returns.abs().sum(axis=1) > 0]
    correlation = returns.corr().fillna(0.0)

    distance = squareform((1.0 - correlation).clip(lower=0.0).to_numpy(), checks=False)
    labels = fcluster(linkage(distance, method="average"), t=1.0 - args.threshold, criterion="distance")
    clusters = pd.Series(labels, index=correlation.index)
    sharpe = returns.mean() / returns.std() * np.sqrt(365)

    pd.set_option("display.width", 220)
    first, last = min(bars[0].timestamp for bars in data.values()), max(bars[-1].timestamp for bars in data.values())
    print(f"{len(names)} strategies at their defaults, long/short, {args.interval} bars on {', '.join(args.symbols)}, {first:%Y-%m-%d} to {last:%Y-%m-%d} (before costs)\n")
    print(correlation.round(2).to_string())
    print(f"\nClusters (daily-return correlation above {args.threshold:g}, average linkage):")
    for label in sorted(set(labels)):
        members = clusters[clusters == label].index.tolist()
        best = max(members, key=lambda name: sharpe[name])
        inner = correlation.loc[members, members].to_numpy()[np.triu_indices(len(members), 1)]
        spread = f", mean correlation {inner.mean():.2f}" if len(members) > 1 else ""
        print(f"  {label}: {', '.join(members)}{spread}; representative {best} (Sharpe {sharpe[best]:.2f} before costs)")
    print(f"\nEffective number of independent bets: {effective_bets(correlation):.1f} of {len(names)}")
    correlation.to_csv(out / "correlation.csv")
    clusters.rename("cluster").to_csv(out / "clusters.csv")
    print(f"Results in {out}")


if __name__ == "__main__":
    main()
