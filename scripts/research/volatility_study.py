"""Volatility forecasts for position sizing: which forecast is most accurate, and does sizing by it help?

1. Forecast accuracy, daily, BTC and ETH perpetuals from March 2021: the
   runtime's 10-bar rolling std, a 30-day rolling std, EWMA of daily returns,
   EWMA of daily realized variance (from 1h bars) and HAR-RV. Each is scored
   against the realized variance of the next day and the next 7 days.
2. Sizing: the strategies that held up on the full history (research_log.md,
   2026-09-26) plus buy-and-hold, at full size and scaled to a target
   volatility with each forecast. Two variants: entry-only (the runtime sizes
   positions at entry and never resizes) and rebalanced when the ideal size
   moves more than 25%. Taker fills and perp costs.

Vol scaling changes the average size, so a lower drawdown alone proves
nothing. The fair comparison is Sharpe, and the drawdown of the full-size
strategy levered to the same volatility ("same-vol DD"). Varying the size at
all changes Sharpe by chance too, so a placebo repeats the entry-only sizing
with the forecast shuffled in time (same sizes, no timing information) and
reports where the real result ranks.

Usage:
    python scripts/research/volatility_study.py
    python scripts/research/volatility_study.py --target-vol 0.3 --max-leverage 1

Writes data/research/volatility_<timestamp>/ (forecasts.csv, sizing.csv).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from src.research import CostSettings, load_bars, simulate_fills
from src.research.catalog import build_strategy
from src.research.volatility import (
    DAYS_PER_YEAR,
    daily_forecast_to_bars,
    daily_realized_variance,
    ewma_variance,
    ewma_vol,
    forecast_losses,
    har_forecast,
    log_returns,
    rolling_vol,
    vol_scaled_positions,
)

MEASURE_FROM = pd.Timestamp("2021-03-01", tz="UTC")  # HAR's first walk-forward fit needs a year of history
STRATEGIES = (
    ("buy and hold", "1d", None, {}),
    ("keltner(40,2) long-only", "1d", "keltner_breakout", {"window": 40, "atr_multiplier": 2.0, "long_only": True}),
    ("ma(4,48) long-only", "1d", "moving_average_crossover", {"short_window": 4, "long_window": 48, "long_only": True}),
    ("donchian(40,10) long-only", "1d", "donchian_breakout", {"entry_window": 40, "exit_window": 10, "long_only": True}),
    ("keltner(40,2) long/short", "1d", "keltner_breakout", {"window": 40, "atr_multiplier": 2.0}),
    ("ma(8,96) 4h long-only", "4h", "moving_average_crossover", {"short_window": 8, "long_window": 96, "long_only": True}),
)
INTERVAL_SECONDS = {"1d": 86_400, "4h": 14_400}


def daily_forecasts(coin: str) -> tuple[pd.DataFrame, pd.Series]:
    """Daily variance forecasts from every model (made at each day's close), and the realized variance per day."""
    rv = daily_realized_variance(*_timestamps_and_close(load_bars(f"{coin}/USD", "1h", source="perp")))
    days, close = _timestamps_and_close(load_bars(f"{coin}/USD", "1d", source="perp"))
    returns = pd.Series(log_returns(close), index=pd.DatetimeIndex(pd.to_datetime(days, utc=True)).floor("D"))
    forecasts = pd.DataFrame(index=rv.index)
    forecasts["rolling 10 days (runtime)"] = (pd.Series(rolling_vol(returns.to_numpy(), 10, 1.0), index=returns.index) ** 2).reindex(rv.index)
    forecasts["rolling 30 days"] = (pd.Series(rolling_vol(returns.to_numpy(), 30, 1.0), index=returns.index) ** 2).reindex(rv.index)
    forecasts["EWMA daily returns (10d)"] = pd.Series(ewma_variance(np.square(returns.to_numpy()), 10), index=returns.index).reindex(rv.index)
    forecasts["EWMA realized variance (5d)"] = ewma_variance(rv.to_numpy(), 5)
    forecasts["HAR (1d ahead)"] = har_forecast(rv, horizon_days=1)
    forecasts["HAR (7d ahead)"] = har_forecast(rv, horizon_days=7)
    return forecasts, rv


def _timestamps_and_close(bars: list) -> tuple[list, np.ndarray]:
    return [bar.timestamp for bar in bars], np.array([bar.close for bar in bars], dtype=float)


def _metrics(returns: np.ndarray, periods_per_year: float) -> dict[str, float]:
    equity = np.cumprod(1.0 + returns)
    years = len(returns) / periods_per_year
    std = returns.std()
    return {
        "cagr": float(equity[-1] ** (1 / years) - 1) if equity[-1] > 0 else -1.0,
        "vol": float(std * np.sqrt(periods_per_year)),
        "sharpe": float(returns.mean() / std * np.sqrt(periods_per_year)) if std > 0 else 0.0,
        "max_drawdown": float(np.max(1 - equity / np.maximum.accumulate(equity))),
    }


def main() -> None:
    """Score the forecasts, then size each strategy with each of them."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--target-vol", type=float, default=0.5, help="Annualised volatility each position is sized to (0.5 = 50%%)")
    parser.add_argument("--max-leverage", type=float, default=2.0, help="Largest position as a multiple of equity")
    parser.add_argument("--rebalance-band", type=float, default=0.25, help="Resize an open position when its ideal size moves more than this (relative)")
    parser.add_argument("--placebo-runs", type=int, default=200, help="Shuffled-forecast runs per strategy for the entry-only placebo (0 skips it)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path("data/research") / f"volatility_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 240)
    costs = CostSettings.perp()

    forecast_rows, sizing_rows, placebo_rows = [], [], []
    for coin in args.coins:
        forecasts, rv = daily_forecasts(coin)
        for horizon in (1, 7):
            realized = rv.rolling(horizon).mean().shift(-horizon)
            scored = forecasts[forecasts.index >= MEASURE_FROM].dropna()
            for model in forecasts.columns:
                if ("7d ahead" in model and horizon == 1) or ("1d ahead" in model and horizon == 7):
                    continue
                losses = forecast_losses(scored[model].to_numpy(), realized.reindex(scored.index).to_numpy())
                forecast_rows.append({"coin": coin, "horizon_days": horizon, "model": model, **losses})
        har_annual_vol = np.sqrt(forecasts["HAR (1d ahead)"] * DAYS_PER_YEAR)

        for label, interval, strategy, params in STRATEGIES:
            bars = load_bars(f"{coin}/USD", interval, source="perp")
            timestamps, close = _timestamps_and_close(bars)
            per_year = DAYS_PER_YEAR * 86_400 / INTERVAL_SECONDS[interval]
            bars_per_day = 86_400 // INTERVAL_SECONDS[interval]
            if strategy is None:
                targets = np.ones(len(bars))
            else:
                targets = np.sign(np.asarray(build_strategy(strategy, **dict(params)).signal_series(bars), dtype=float))
            start = int(np.searchsorted(pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True)), MEASURE_FROM))
            targets[:start] = 0.0  # every sizing opens its first position inside the measured period
            returns = log_returns(close)
            vol_forecasts = {
                "rolling 10 bars (runtime)": rolling_vol(returns, 10, per_year),
                "EWMA (10d)": ewma_vol(returns, 10 * bars_per_day, per_year),
                "HAR": daily_forecast_to_bars(har_annual_vol, [t + pd.Timedelta(seconds=INTERVAL_SECONDS[interval]) for t in timestamps]),
            }
            sizings = {"full size": targets}
            for name, forecast in vol_forecasts.items():
                for entry_only in (True, False):
                    positions = vol_scaled_positions(targets, forecast, target_vol=args.target_vol, max_leverage=args.max_leverage, rebalance_band=args.rebalance_band, entry_only=entry_only)
                    sizings[f"{name}, {'entry-only' if entry_only else 'rebalanced'}"] = positions
            full_returns = None
            for sizing, positions in sizings.items():
                result, stats = simulate_fills(bars, positions, taker_fee_pct=costs.fee_pct, maker_fee_pct=costs.maker_fee_pct or costs.fee_pct, slippage_bps=costs.slippage_bps, funding_pct_per_day=costs.funding_pct_per_day)
                equity = np.asarray(result.mtm_equity_series, dtype=float)
                period_returns = equity[start + 1 :] / equity[start:-1] - 1.0
                row = _metrics(period_returns, per_year)
                if sizing == "full size":
                    full_returns = period_returns
                    full_vol = row["vol"]
                matched = _metrics(full_returns * (row["vol"] / full_vol), per_year) if full_vol > 0 else row
                changes = np.count_nonzero(np.diff(positions[start:]) != 0)
                sizing_rows.append({
                    "coin": coin, "strategy": label, "sizing": sizing, **row,
                    "same_vol_full_size_drawdown": matched["max_drawdown"], "same_vol_full_size_cagr": matched["cagr"],
                    "avg_size": float(np.mean(np.abs(positions[start:]))), "orders_per_year": changes / (len(period_returns) / per_year),
                })
            if args.placebo_runs:
                rng = np.random.default_rng(7)
                for name in ("EWMA (10d)", "HAR"):
                    forecast = vol_forecasts[name]
                    live = np.flatnonzero(np.isfinite(forecast) & (np.arange(len(forecast)) >= start))
                    placebo = []
                    for _ in range(args.placebo_runs):
                        shuffled = forecast.copy()
                        shuffled[live] = rng.permutation(forecast[live])
                        positions = vol_scaled_positions(targets, shuffled, target_vol=args.target_vol, max_leverage=args.max_leverage, entry_only=True)
                        result, _ = simulate_fills(bars, positions, taker_fee_pct=costs.fee_pct, maker_fee_pct=costs.maker_fee_pct or costs.fee_pct, slippage_bps=costs.slippage_bps, funding_pct_per_day=costs.funding_pct_per_day)
                        equity = np.asarray(result.mtm_equity_series, dtype=float)
                        placebo.append(_metrics(equity[start + 1 :] / equity[start:-1] - 1.0, per_year)["sharpe"])
                    real = next(r["sharpe"] for r in sizing_rows if r["coin"] == coin and r["strategy"] == label and r["sizing"] == f"{name}, entry-only")
                    full = next(r["sharpe"] for r in sizing_rows if r["coin"] == coin and r["strategy"] == label and r["sizing"] == "full size")
                    placebo_rows.append({
                        "coin": coin, "strategy": label, "forecast": name, "full_size_sharpe": full, "vol_scaled_sharpe": real,
                        "placebo_median": float(np.median(placebo)), "placebo_p90": float(np.quantile(placebo, 0.9)),
                        "share_of_placebos_beaten": float(np.mean(np.asarray(placebo) < real)),
                    })

    forecast_table = pd.DataFrame(forecast_rows)
    sizing_table = pd.DataFrame(sizing_rows)
    forecast_table.to_csv(out / "forecasts.csv", index=False)
    sizing_table.to_csv(out / "sizing.csv", index=False)
    print(f"Forecast accuracy from {MEASURE_FROM:%Y-%m-%d} (QLIKE: lower is better; mean_ratio > 1: forecasts too low):")
    print(forecast_table.pivot_table(index=["horizon_days", "model"], columns="coin", values=["qlike", "r2_log", "mean_ratio"]).round(3).to_string())
    order = list(dict.fromkeys(sizing_table["sizing"]))
    for metric in ("sharpe", "max_drawdown", "same_vol_full_size_drawdown", "cagr", "avg_size", "orders_per_year"):
        print(f"\n{metric} (target vol {args.target_vol:.0%}, max leverage {args.max_leverage:g}x):")
        print(sizing_table.pivot_table(index=["coin", "strategy"], columns="sizing", values=metric, sort=False)[order].round(2).to_string())
    if placebo_rows:
        placebo_table = pd.DataFrame(placebo_rows)
        placebo_table.to_csv(out / "placebo.csv", index=False)
        print(f"\nEntry-only vol scaling vs {args.placebo_runs} shuffled-forecast placebos (share beaten > 0.9 suggests the timing is real):")
        print(placebo_table.round(2).to_string(index=False))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
