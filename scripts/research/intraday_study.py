"""Intraday predictability study on BTC/ETH perpetuals: which features predict the next hours, and does it pay?

Two parts, both on Kraken perpetual candles from 2020 (see load_bars(source="perp")):

1. Feature study: for each feature, the rank correlation (IC) with the forward
   return, how many years keep the same sign, and time-of-day seasonality.
2. Combined forecast: a walk-forward ridge over all features (refit monthly on
   past data only), traded only when the forecast exceeds a threshold, at
   taker fills, the old optimistic maker shortcut, and realistic resting limit orders.

Usage:
    python scripts/research/intraday_study.py --interval 15m
    python scripts/research/intraday_study.py --interval 1h --horizon-hours 8 --symbols ETH/USD BTC/USD

Results go to data/research/intraday_<interval>_<timestamp>/ (features.csv,
seasonality_<coin>.csv, forecast.csv) and are printed. Read the findings in
docs/research_log.md before trusting any single number: maker results assume
every limit order fills at the bar close, which is optimistic.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from src.backtest.indicators import rolling_max, rolling_min, rolling_std
from src.research import CostSettings, FillModel, load_bars, simulate_fills
from src.research.features import bars_frame, bucket_table, forward_return, ic_by_year, information_coefficient, past_return, seasonality, volatility_scaled, zscore
from src.research.forecast import threshold_positions, walk_forward_ridge

BARS_PER_HOUR = {"15m": 4, "1h": 1}
# How each position change is executed. "maker fee at close" is the old optimistic shortcut (maker fee, always
# filled at the signal close); the realistic maker variants only fill when the market trades through the limit.
EXECUTION_VARIANTS = {
    "zero cost": (CostSettings(0.0, 0.0, 0.0, maker_fee_pct=0.0), FillModel()),
    "taker at close": (CostSettings.perp(), FillModel()),
    "maker fee at close": (CostSettings.perp(maker=True), FillModel()),
    "maker, cancel after 1 bar": (CostSettings.perp(), FillModel.maker(max_wait_bars=1, on_timeout="cancel")),
    "maker, requote": (CostSettings.perp(), FillModel.maker(max_wait_bars=1, on_timeout="requote")),
    "maker, taker after 2 bars": (CostSettings.perp(), FillModel.maker(max_wait_bars=2, on_timeout="taker")),
}


def build_features(frame: pd.DataFrame, other: pd.DataFrame, bars_per_day: int) -> pd.DataFrame:
    """Causal features at each bar's close: past returns (vol-scaled), the other coin's moves, position in the daily
    range, distance from the daily mean, volume and volatility shocks, and time of day."""
    close = frame["close"].to_numpy()
    other_close = other["close"].reindex(frame.index).ffill().to_numpy()
    week = 7 * bars_per_day
    one_bar = np.nan_to_num(past_return(close, 1))
    hours = frame.index.hour + frame.index.minute / 60.0
    features = pd.DataFrame(index=frame.index)
    for lookback, name in ((1, "ret_1bar"), (bars_per_day // 6, "ret_4h"), (bars_per_day, "ret_1d"), (3 * bars_per_day, "ret_3d")):
        features[name] = volatility_scaled(past_return(close, lookback), close, week)
    features["other_ret_1bar"] = volatility_scaled(past_return(other_close, 1), other_close, week)
    features["other_ret_4h"] = volatility_scaled(past_return(other_close, bars_per_day // 6), other_close, week)
    features["dist_mean_1d"] = zscore(close, bars_per_day)
    low, high = rolling_min(frame["low"].to_numpy(), bars_per_day), rolling_max(frame["high"].to_numpy(), bars_per_day)
    with np.errstate(divide="ignore", invalid="ignore"):
        features["range_position_1d"] = (close - low) / (high - low) - 0.5
        features["volatility_ratio"] = np.log(rolling_std(one_bar, bars_per_day) / rolling_std(one_bar, week))
    features["volume_z"] = zscore(np.log1p(frame["volume"].to_numpy()), week)
    features["hour_sin"], features["hour_cos"] = np.sin(2 * np.pi * hours / 24), np.cos(2 * np.pi * hours / 24)
    features["hour_sin2"], features["hour_cos2"] = np.sin(4 * np.pi * hours / 24), np.cos(4 * np.pi * hours / 24)
    return features.replace([np.inf, -np.inf], np.nan).clip(-6, 6)


def main() -> None:
    """Run both parts of the study and save the tables."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", choices=sorted(BARS_PER_HOUR), default="15m")
    parser.add_argument("--symbols", nargs=2, default=["BTC/USD", "ETH/USD"], help="Two coins; each is also used as a feature for the other")
    parser.add_argument("--horizon-hours", type=int, default=4, help="How far ahead the forecast looks")
    parser.add_argument("--thresholds", nargs="+", type=float, default=[0.0, 5.0, 10.0, 20.0, 30.0], help="Forecast thresholds in bps")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    per_hour = BARS_PER_HOUR[args.interval]
    bars_per_day = 24 * per_hour
    horizon = args.horizon_hours * per_hour
    out = Path(args.out) if args.out else Path("data/research") / f"intraday_{args.interval}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220)

    raw = {symbol: load_bars(symbol, args.interval, source="perp") for symbol in args.symbols}
    frames = {symbol: bars_frame(bars) for symbol, bars in raw.items()}
    feature_rows, forecast_rows = [], []
    for symbol in args.symbols:
        other = [s for s in args.symbols if s != symbol][0]
        frame = frames[symbol]
        features = build_features(frame, frames[other], bars_per_day)
        forward = forward_return(frame["close"].to_numpy(), horizon)

        for name in features.columns:
            if name.startswith("hour_"):
                continue
            yearly = ic_by_year(features[name].to_numpy(), forward, frame.index)
            ic = information_coefficient(features[name].to_numpy(), forward)
            feature_rows.append({"symbol": symbol, "feature": name, "ic": ic, "years_same_sign": int((np.sign(yearly) == np.sign(ic)).sum()), "years": len(yearly)})
        seasonality(frame, by="hour", horizon=per_hour).to_csv(out / f"seasonality_{symbol.replace('/', '-')}.csv")

        forecast = walk_forward_ridge(features, forward * 1e4, horizon=horizon, train_min=365 * bars_per_day, refit_every=30 * bars_per_day)
        live = np.isfinite(forecast)
        first = int(np.argmax(live))
        yearly_ic = pd.DataFrame({"f": forecast, "y": forward}, index=frame.index)[live].dropna()
        print(f"\n{symbol} {args.interval}, {args.horizon_hours}h horizon: out-of-sample IC {information_coefficient(forecast[live], forward[live]):+.4f}, by year",
              {year: round(information_coefficient(g.f.to_numpy(), g.y.to_numpy()), 3) for year, g in yearly_ic.groupby(yearly_ic.index.year)})
        print(bucket_table(forecast[live], forward[live], horizon, buckets=10)[["bucket", "feature_from", "feature_to", "mean_bps", "t_stat"]].to_string(index=False, float_format=lambda v: f"{v:+.2f}"))

        span_years = (frame.index[-1] - frame.index[first]).days / 365.25
        for threshold in args.thresholds:
            for long_only in (False, True):
                positions = threshold_positions(forecast, threshold, allow_short=not long_only).astype(float)
                positions[:first] = 0.0
                for execution, (costs, fills) in EXECUTION_VARIANTS.items():
                    result, stats = simulate_fills(
                        raw[symbol], positions, taker_fee_pct=costs.fee_pct, maker_fee_pct=costs.maker_fee_pct or costs.fee_pct,
                        slippage_bps=costs.slippage_bps, funding_pct_per_day=costs.funding_pct_per_day, fills=fills,
                    )
                    returns = pd.Series(result.mtm_equity_series[first:]).pct_change().fillna(0.0).to_numpy()
                    equity = np.cumprod(1.0 + returns)
                    forecast_rows.append({
                        "symbol": symbol, "threshold_bps": threshold, "side": "long" if long_only else "long/short", "execution": execution,
                        "cagr": equity[-1] ** (1 / span_years) - 1,
                        "sharpe": float(returns.mean() / returns.std() * np.sqrt(365 * bars_per_day)) if returns.std() > 0 else 0.0,
                        "max_drawdown": float(np.max(1 - equity / np.maximum.accumulate(equity))),
                        "trades_per_day": sum(1 for t in result.trade_records if t.timestamp >= frame.index[first]) / (span_years * 365),
                        "fill_rate": stats.fill_rate,
                    })

    features_table = pd.DataFrame(feature_rows)
    forecast_table = pd.DataFrame(forecast_rows)
    features_table.to_csv(out / "features.csv", index=False)
    forecast_table.to_csv(out / "forecast.csv", index=False)
    print("\nFeature information coefficients (forward", args.horizon_hours, "h):")
    print(features_table.pivot_table(index="feature", columns="symbol", values=["ic", "years_same_sign"]).round(4).to_string())
    print("\nForecast strategy Sharpe by threshold and execution:")
    print(forecast_table.pivot_table(index=["symbol", "threshold_bps", "side"], columns="execution", values="sharpe").round(2)[list(EXECUTION_VARIANTS)].to_string())
    print("\nFill rate of resting limit orders:")
    print(forecast_table[forecast_table.execution.str.startswith("maker")].pivot_table(index=["symbol", "threshold_bps", "side"], columns="execution", values="fill_rate").round(2).to_string())
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
