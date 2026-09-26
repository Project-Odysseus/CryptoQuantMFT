"""Do derivatives positioning signals (funding, open interest, trader ratios, taker flow, implied vol) predict returns?

Uses hourly Kraken perpetual bars (the price we would trade) and public
positioning data from Binance, Bybit and Deribit (src/data/positioning.py),
each aligned so a bar only sees values published by its close.

1. Feature study: rank correlation (IC) of each feature with the forward
   return at several horizons, per year, plus decile tables in bps.
2. Combined forecast: walk-forward ridge over price and positioning features
   (refit monthly on past data only), traded when the forecast beats a
   threshold, with taker fills and realistic resting limit orders.
3. Crowding composite: no fitting at all. The average of four crowding
   measures, each as a 30-day z-score, with the sign fixed in advance by the
   hypothesis that crowded longs underperform. It is a check on whether the
   ridge is fitting noise.

Usage:
    python scripts/research/positioning_study.py
    python scripts/research/positioning_study.py --coins ETH --horizon-hours 72

Writes data/research/positioning_<timestamp>/ (features.csv, forecast.csv, composite.csv, deciles_<coin>.csv).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from src.backtest.indicators import rolling_mean, rolling_std
from src.data.positioning import load_positioning
from src.research import CostSettings, FillModel, load_bars, simulate_fills
from src.research.features import bars_frame, bucket_table, forward_return, ic_by_year, information_coefficient, past_return, volatility_scaled, zscore
from src.research.forecast import threshold_positions, walk_forward_ridge

DAY = 24
EXECUTION_VARIANTS = {
    "zero cost": (CostSettings(0.0, 0.0, 0.0, maker_fee_pct=0.0), FillModel()),
    "taker at close": (CostSettings.perp(), FillModel()),
    "maker, requote": (CostSettings.perp(), FillModel.maker(max_wait_bars=1, on_timeout="requote")),
    "maker, taker after 2 bars": (CostSettings.perp(), FillModel.maker(max_wait_bars=2, on_timeout="taker")),
}
# Higher = more crowded long; the composite is minus their average z-score (bullish when the crowd is short).
CROWDING_FEATURES = ("funding_bps", "oi_change_1d", "retail_long_short", "top_trader_long_short")


def _change(values: np.ndarray, bars: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[bars:] = np.log(values[bars:] / values[:-bars])
    return out


def build_features(frame: pd.DataFrame, positioning: pd.DataFrame) -> pd.DataFrame:
    """Price features (for comparison and combination) and positioning features, all known at each bar's close."""
    close = frame["close"].to_numpy()
    month = 30 * DAY
    one_bar = np.nan_to_num(past_return(close, 1))
    realized_vol_30d = rolling_std(one_bar, month) * np.sqrt(365 * DAY) * 100.0
    p = positioning
    funding = p[["binance_funding", "bybit_funding"]].mean(axis=1).to_numpy() * 1e4  # bps per 8h
    taker_share = (p["binance_taker_buy_volume"] / p["binance_volume"]).to_numpy()
    features = pd.DataFrame(index=frame.index)
    # price
    features["ret_4h"] = volatility_scaled(past_return(close, 4), close, 7 * DAY)
    features["ret_1d"] = volatility_scaled(past_return(close, DAY), close, 7 * DAY)
    features["ret_3d"] = volatility_scaled(past_return(close, 3 * DAY), close, 7 * DAY)
    # funding (crowding and cost of carry)
    features["funding_bps"] = funding
    features["funding_z_30d"] = zscore(funding, month)
    features["deribit_funding_bps"] = p["deribit_funding"].to_numpy() * 1e4
    features["funding_change_1d"] = funding - np.concatenate([np.full(DAY, np.nan), funding[:-DAY]])
    # open interest (new positions entering or leaving)
    features["oi_change_4h"] = _change(p["binance_open_interest"].to_numpy(), 4)
    features["oi_change_1d"] = _change(p["binance_open_interest"].to_numpy(), DAY)
    features["bybit_oi_change_1d"] = _change(p["bybit_open_interest"].to_numpy(), DAY)
    features["oi_up_price_up_1d"] = features["oi_change_1d"] * np.sign(past_return(close, DAY))
    # who is positioned how
    features["top_trader_long_short"] = np.log(p["binance_top_trader_long_short"].to_numpy())
    features["retail_long_short"] = np.log(p["binance_account_long_short"].to_numpy())
    features["retail_long_short_change_1d"] = _change(p["binance_account_long_short"].to_numpy(), DAY)
    # aggressive flow (who is paying the spread)
    features["taker_buy_share_1h"] = taker_share - 0.5
    features["taker_buy_share_4h"] = rolling_mean(taker_share, 4) - 0.5
    features["taker_buy_share_1d"] = rolling_mean(taker_share, DAY) - 0.5
    features["volume_z_1d"] = zscore(np.log1p(p["binance_volume"].to_numpy()), month)
    # implied volatility
    features["dvol"] = p["dvol"].to_numpy()
    features["dvol_change_1d"] = _change(p["dvol"].to_numpy(), DAY)
    features["implied_minus_realized_vol"] = p["dvol"].to_numpy() - realized_vol_30d
    return features.replace([np.inf, -np.inf], np.nan)


def _rolling_z(values: pd.Series, window: int) -> pd.Series:
    """Past-only z-score that tolerates short gaps (at least two thirds of the window must be present)."""
    rolling = values.rolling(window, min_periods=2 * window // 3)
    return (values - rolling.mean()) / rolling.std()


def trade_signal(bars: list, frame: pd.DataFrame, signal: np.ndarray, threshold: float, first: int, **labels: object) -> list[dict[str, object]]:
    """Trade `signal` long/short and long-only from bar `first` under every execution variant; one row per run."""
    close = frame["close"].to_numpy()
    span_years = (frame.index[-1] - frame.index[first]).days / 365.25
    rows = []
    for long_only in (False, True):
        positions = threshold_positions(signal, threshold, allow_short=not long_only).astype(float)
        positions[:first] = 0.0
        for execution, (costs, fills) in EXECUTION_VARIANTS.items():
            result, _ = simulate_fills(bars, positions, taker_fee_pct=costs.fee_pct, maker_fee_pct=costs.maker_fee_pct or costs.fee_pct, slippage_bps=costs.slippage_bps, funding_pct_per_day=costs.funding_pct_per_day, fills=fills)
            returns = pd.Series(result.mtm_equity_series[first:]).pct_change().fillna(0.0).to_numpy()
            equity = np.cumprod(1.0 + returns)
            rows.append({
                **labels, "side": "long" if long_only else "long/short", "execution": execution,
                "cagr": equity[-1] ** (1 / span_years) - 1, "sharpe": float(returns.mean() / returns.std() * np.sqrt(365 * DAY)) if returns.std() > 0 else 0.0,
                "max_drawdown": float(np.max(1 - equity / np.maximum.accumulate(equity))),
                "trades_per_week": sum(1 for t in result.trade_records if t.timestamp >= frame.index[first]) / (span_years * 52),
                "exposure": float(np.mean(np.asarray(result.position_series[first:]) != 0)),
                "buy_hold_cagr": (close[-1] / close[first]) ** (1 / span_years) - 1,
            })
    return rows


def main() -> None:
    """Run the feature study and the combined forecast for each coin."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--horizons", nargs="+", type=int, default=[4, 24, 72], help="Forward horizons in hours for the feature study")
    parser.add_argument("--horizon-hours", type=int, default=24, help="Horizon of the combined forecast")
    parser.add_argument("--thresholds", nargs="+", type=float, default=[0.0, 25.0, 50.0, 100.0], help="Forecast thresholds in bps")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path("data/research") / f"positioning_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 230)

    feature_rows, forecast_rows, composite_rows = [], [], []
    for coin in args.coins:
        bars = load_bars(f"{coin}/USD", "1h", source="perp")
        frame = bars_frame(bars)
        closes = frame.index + pd.Timedelta(hours=1)
        # Carry a published value forward for at most a day; longer gaps stay missing (the forecast is then flat).
        positioning = load_positioning(coin, closes).set_index(frame.index).ffill(limit=DAY)
        features = build_features(frame, positioning)
        close = frame["close"].to_numpy()

        for horizon in args.horizons:
            forward = forward_return(close, horizon)
            for name in features.columns:
                values = features[name].to_numpy()
                available = np.isfinite(values)
                if available.sum() < 1000:
                    continue
                yearly = ic_by_year(values, forward, frame.index)
                ic = information_coefficient(values, forward)
                feature_rows.append({"coin": coin, "horizon_h": horizon, "feature": name, "from": f"{frame.index[available][0]:%Y-%m}", "ic": ic,
                                     "years_same_sign": int((np.sign(yearly) == np.sign(ic)).sum()), "years": int(yearly.notna().sum())})
        forward_main = forward_return(close, args.horizon_hours)
        deciles = []
        for name in ("funding_z_30d", "oi_change_1d", "retail_long_short", "taker_buy_share_1d", "implied_minus_realized_vol", "top_trader_long_short"):
            table = bucket_table(features[name].to_numpy(), forward_main, args.horizon_hours, buckets=10)
            table.insert(0, "feature", name)
            deciles.append(table)
        pd.concat(deciles).to_csv(out / f"deciles_{coin}.csv", index=False)

        # Combined forecast from when every feature exists (Binance metrics start late 2021).
        # Tame outliers with expanding (past-only) percentiles, so no bar is clipped using values from its future.
        raw_features = features.drop(columns=["dvol"])
        lower = raw_features.expanding(min_periods=30 * DAY).quantile(0.005)
        upper = raw_features.expanding(min_periods=30 * DAY).quantile(0.995)
        model_features = raw_features.clip(lower, upper)
        start = int(np.argmax(np.isfinite(model_features.to_numpy()).all(axis=1)))
        target = forward_main * 1e4
        forecast = np.full(len(frame), np.nan)
        forecast[start:] = walk_forward_ridge(model_features.iloc[start:], target[start:], horizon=args.horizon_hours, train_min=365 * DAY, refit_every=30 * DAY)
        live = np.isfinite(forecast)
        first = int(np.argmax(live))
        yearly_ic = pd.DataFrame({"f": forecast, "y": forward_main}, index=frame.index)[live].dropna()
        print(f"\n{coin}: combined {args.horizon_hours}h forecast, out-of-sample from {frame.index[first]:%Y-%m-%d}, IC {information_coefficient(forecast[live], forward_main[live]):+.4f}, by year",
              {year: round(information_coefficient(g.f.to_numpy(), g.y.to_numpy()), 3) for year, g in yearly_ic.groupby(yearly_ic.index.year)})
        print(bucket_table(forecast[live], forward_main[live], args.horizon_hours, buckets=10)[["bucket", "feature_from", "feature_to", "mean_bps", "t_stat"]].to_string(index=False, float_format=lambda v: f"{v:+.1f}"))
        for threshold in args.thresholds:
            forecast_rows += trade_signal(bars, frame, forecast, threshold, first, coin=coin, threshold_bps=threshold)

        # 3. Crowding composite, traded from the forecast's out-of-sample start so the two are comparable.
        crowding = pd.DataFrame({name: _rolling_z(features[name], 30 * DAY) for name in CROWDING_FEATURES})
        composite = -crowding.mean(axis=1, skipna=False).to_numpy()
        composite_ics = {f"ic_{h}h": information_coefficient(composite[first:], forward_return(close, h)[first:]) for h in (24, 72)}
        composite_years = ic_by_year(composite[first:], forward_return(close, 72)[first:], frame.index[first:])
        print(f"\n{coin}: crowding composite from {frame.index[first]:%Y-%m-%d}, IC 24h {composite_ics['ic_24h']:+.4f}, 72h {composite_ics['ic_72h']:+.4f}, 72h by year",
              composite_years.round(3).to_dict())
        print(bucket_table(composite[first:], forward_main[first:], args.horizon_hours, buckets=10)[["bucket", "feature_from", "feature_to", "mean_bps", "t_stat"]].to_string(index=False, float_format=lambda v: f"{v:+.2f}"))
        for threshold in (0.5, 1.0, 1.5):
            composite_rows += trade_signal(bars, frame, composite, threshold, first, coin=coin, threshold_z=threshold)

    features_table = pd.DataFrame(feature_rows)
    forecast_table = pd.DataFrame(forecast_rows)
    composite_table = pd.DataFrame(composite_rows)
    features_table.to_csv(out / "features.csv", index=False)
    forecast_table.to_csv(out / "forecast.csv", index=False)
    composite_table.to_csv(out / "composite.csv", index=False)
    print("\nFeature ICs by horizon (years with the same sign in brackets):")
    features_table["cell"] = features_table.apply(lambda r: f"{r.ic:+.3f} ({r.years_same_sign}/{r.years})", axis=1)
    print(features_table.pivot_table(index=["feature"], columns=["coin", "horizon_h"], values="cell", aggfunc="first").to_string())
    print("\nCombined forecast, Sharpe by threshold and execution:")
    print(forecast_table.pivot_table(index=["coin", "threshold_bps", "side"], columns="execution", values="sharpe").round(2)[list(EXECUTION_VARIANTS)].to_string())
    print(forecast_table[forecast_table.execution == "taker at close"][["coin", "threshold_bps", "side", "cagr", "max_drawdown", "trades_per_week", "exposure", "buy_hold_cagr"]].round(2).to_string(index=False))
    print("\nCrowding composite, Sharpe by threshold (z) and execution:")
    print(composite_table.pivot_table(index=["coin", "threshold_z", "side"], columns="execution", values="sharpe").round(2)[list(EXECUTION_VARIANTS)].to_string())
    print(composite_table[composite_table.execution == "taker at close"][["coin", "threshold_z", "side", "cagr", "max_drawdown", "trades_per_week", "exposure", "buy_hold_cagr"]].round(2).to_string(index=False))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
