"""Cross-sectional strategies on perpetuals: rank coins against each other, hold the best and short the worst.

Data: daily bars and funding for every Binance USDT perpetual that ever traded,
delisted coins included (src/data/binance_archive.py; add --download to fetch
or top up). Universe: each day, the 50 coins with the most volume over the
last 30 days that have been listed at least 60 days. `--kraken-only` limits it
to coins Kraken Futures lists today, i.e. what we could actually trade.

Features at each close (known at that close):
    mom_30d, mom_7d         past return
    ret_1d                  last day's return (reversal)
    funding_7d              average daily funding over the last week (crowding and carry)
    vol_30d                 volatility of daily log returns
    max_ret_30d             largest single-day return in the last 30 days ("lottery" coins)
    volume_trend            log of 7-day over 90-day average volume (attention)
    taker_buy_share_7d      share of volume from aggressive buyers

1. IC: the daily cross-sectional rank correlation with the next 1-day and
   7-day returns, in-sample (to the end of 2023) and holdout (2024 on). The
   7-day t-stat uses every 7th day, so windows don't overlap.
2. Portfolios: long the top 20% and short the bottom 20% by each feature,
   dollar-neutral and 1x gross, rebalanced daily or weekly. The sign of each
   feature is fixed from the in-sample IC only. Costs: Kraken's 0.05% taker
   fee, slippage by liquidity (3-25 bps), and each coin's actual funding.
3. Composite: the average cross-sectional z-score of the features whose
   in-sample 7-day IC had |t| >= 2, signed in-sample.

Usage:
    python scripts/research/cross_sectional_study.py --download
    python scripts/research/cross_sectional_study.py --kraken-only --top-n 30

Writes data/research/cross_sectional_<timestamp>/ (ic.csv, portfolios.csv, composite_returns.csv).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from src.data.binance_archive import CACHE_DIR, base_asset, kraken_perp_bases, load_panel, update_cache
from src.research.portfolio import PortfolioCosts, cross_sectional_ic, liquid_universe, rank_weights, simulate_portfolio, slippage_by_liquidity

START = pd.Timestamp("2020-06-01", tz="UTC")
HOLDOUT = pd.Timestamp("2024-01-01", tz="UTC")


def load_wide(cache_dir: Path | str = CACHE_DIR) -> dict[str, pd.DataFrame]:
    """Close, quote volume, taker-buy quote volume and daily funding as date x symbol frames."""
    klines = load_panel("klines_1d", cache_dir=cache_dir)
    if klines.empty:
        raise SystemExit("No Binance archive data cached; run with --download first.")
    wide = {column: klines.pivot_table(index="date", columns="symbol", values=column) for column in ("close", "quote_volume", "taker_buy_quote_volume")}
    traded = wide["quote_volume"] > 0
    wide["close"] = wide["close"].where(traded)  # a day without trades (e.g. after delisting) has no usable price
    wide["quote_volume"] = wide["quote_volume"].where(traded)
    funding = load_panel("funding", cache_dir=cache_dir)
    wide["funding"] = funding.pivot_table(index="date", columns="symbol", values="funding").reindex_like(wide["close"]) if not funding.empty else wide["close"] * 0.0
    return wide


def build_features(wide: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Each feature as a date x symbol frame, from data up to each close."""
    close, volume = wide["close"], wide["quote_volume"]
    ret_1d = close.pct_change(fill_method=None)
    log_ret = np.log(close).diff()
    return {
        "mom_30d": close / close.shift(30) - 1.0,
        "mom_7d": close / close.shift(7) - 1.0,
        "ret_1d": ret_1d,
        "funding_7d": wide["funding"].rolling(7, min_periods=5).mean(),
        "vol_30d": log_ret.rolling(30, min_periods=20).std(),
        "max_ret_30d": ret_1d.rolling(30, min_periods=20).max(),
        "volume_trend": np.log(volume.rolling(7, min_periods=5).mean() / volume.rolling(90, min_periods=60).mean()),
        "taker_buy_share_7d": wide["taker_buy_quote_volume"].rolling(7, min_periods=5).sum() / volume.rolling(7, min_periods=5).sum() - 0.5,
    }


def _ic_summary(daily_ic: pd.Series, step: int) -> tuple[float, float]:
    sample = daily_ic.dropna().iloc[::step]
    if len(sample) < 20:
        return float("nan"), float("nan")
    return float(sample.mean()), float(sample.mean() / sample.std() * np.sqrt(len(sample)))


def _zscore_rows(frame: pd.DataFrame, eligible: pd.DataFrame) -> pd.DataFrame:
    masked = frame.where(eligible)
    return masked.sub(masked.mean(axis=1), axis=0).div(masked.std(axis=1).replace(0.0, np.nan), axis=0)


def main() -> None:
    """Run the IC study, the per-feature portfolios and the composite."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download", action="store_true", help="Fetch or top up the Binance archive cache first (~50k files the first time)")
    parser.add_argument("--kraken-only", action="store_true", help="Limit the universe to coins with a Kraken Futures perpetual today")
    parser.add_argument("--top-n", type=int, default=50, help="Universe size: most-traded coins over the last 30 days")
    parser.add_argument("--quantile", type=float, default=0.2, help="Share of the universe in each leg")
    parser.add_argument("--cost-multiplier", type=float, default=1.0, help="Scale fees and slippage (2 = twice as expensive)")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR), help="Where the Binance archive cache lives")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path("data/research") / f"cross_sectional_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 240)
    if args.download:
        update_cache(cache_dir=args.cache_dir)

    wide = load_wide(args.cache_dir)
    close = wide["close"]
    universe = liquid_universe(wide["quote_volume"], top_n=args.top_n)
    if args.kraken_only:
        on_kraken = kraken_perp_bases()
        universe.loc[:, [symbol for symbol in close.columns if base_asset(symbol) not in on_kraken]] = False
    universe.loc[universe.index < START] = False
    features = build_features(wide)
    forward = {1: close.shift(-1) / close - 1.0, 7: close.shift(-7) / close - 1.0}
    costs = PortfolioCosts(fee_pct=0.05 * args.cost_multiplier, slippage_bps=slippage_by_liquidity(wide["quote_volume"]) * args.cost_multiplier)
    btc = close["BTCUSDT"].pct_change(fill_method=None)
    print(f"Universe: top {args.top_n}{' Kraken-listed' if args.kraken_only else ''} coins; {int(universe.sum(axis=1)[universe.index >= START].median())} names on a median day; "
          f"{int(universe.any(axis=0).sum())} different coins over the period; data {close.index[0]:%Y-%m-%d} to {close.index[-1]:%Y-%m-%d}")

    ic_rows, signs = [], {}
    for name, feature in features.items():
        row: dict[str, object] = {"feature": name}
        for horizon, step in ((1, 1), (7, 7)):
            daily = cross_sectional_ic(feature, forward[horizon], universe)
            for label, period in (("is", daily[daily.index < HOLDOUT]), ("ho", daily[daily.index >= HOLDOUT])):
                row[f"{label}_ic_{horizon}d"], row[f"{label}_t_{horizon}d"] = _ic_summary(period, step)
            if horizon == 7:
                yearly = daily.groupby(daily.index.year).mean()
                row["years_same_sign_7d"] = f"{int((np.sign(yearly) == np.sign(row['is_ic_7d'])).sum())}/{len(yearly)}"
        signs[name] = float(np.sign(row["is_ic_7d"])) if np.isfinite(row["is_ic_7d"]) else 0.0
        ic_rows.append(row)
    ic_table = pd.DataFrame(ic_rows)

    signed = {name: features[name] * signs[name] for name in features}
    chosen = [row["feature"] for row in ic_rows if abs(row["is_t_7d"]) >= 2.0]
    if chosen:
        zscores = [_zscore_rows(signed[name], universe) for name in chosen]
        available = sum(z.notna().astype(float) for z in zscores)
        signed["composite"] = sum(z.fillna(0.0) for z in zscores) / available.replace(0.0, np.nan)  # average of the features a coin has
    portfolio_rows, composite_returns = [], {}
    for name, scores in signed.items():
        weights = rank_weights(scores, universe, quantile=args.quantile, gross=1.0)
        for rebalance in (1, 7):
            result = simulate_portfolio(close, weights, funding=wide["funding"], costs=costs, rebalance_every=rebalance)
            if name == "composite":
                composite_returns[f"rebalance_{rebalance}d"] = result.returns
            for label, start, end in (("is", START, HOLDOUT), ("ho", HOLDOUT, None)):
                metrics = result.metrics(start, end)
                returns = result.returns[(result.returns.index >= start) & ((result.returns.index < end) if end is not None else True)]
                portfolio_rows.append({
                    "feature": name, "sign": "+" if signs.get(name, 1.0) >= 0 else "-", "rebalance_days": rebalance, "period": label, **metrics,
                    "corr_btc": float(returns.corr(btc.reindex(returns.index))),
                })
    portfolio_table = pd.DataFrame(portfolio_rows)

    ic_table.to_csv(out / "ic.csv", index=False)
    portfolio_table.to_csv(out / "portfolios.csv", index=False)
    if composite_returns:
        pd.DataFrame(composite_returns).to_csv(out / "composite_returns.csv")
    print("\nCross-sectional IC (in-sample to 2023, holdout 2024 on; t-stats on non-overlapping days):")
    print(ic_table.round(3).to_string(index=False))
    print(f"\nComposite uses: {', '.join(f'{signs[n]:+.0f} {n}' for n in chosen) or 'nothing (no feature had |t| >= 2 in-sample)'}")
    view = portfolio_table.pivot_table(index=["feature", "sign", "rebalance_days"], columns="period", values=["sharpe", "cagr", "max_drawdown"], sort=False)
    print("\nLong/short portfolios (sign fixed in-sample), Sharpe / CAGR / max drawdown:")
    print(view.round(2).to_string())
    holdout = portfolio_table[portfolio_table.period == "ho"].set_index(["feature", "rebalance_days"])
    print("\nHoldout details (turnover x equity per year; costs, funding, legs in % of equity per year; + funding = paid):")
    print(holdout[["turnover_per_year", "cost_pct_per_year", "funding_pct_per_year", "long_leg_pct_per_year", "short_leg_pct_per_year", "avg_positions", "corr_btc"]].round(2).to_string())
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
