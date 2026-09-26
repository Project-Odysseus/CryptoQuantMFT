"""Can the trend book be diversified? Same rules on more coins, and the taker-buy cross-sectional candidate.

Part 1, breadth: the example book's daily trend rules (MA 4/48 long-only and
Keltner 40/2 long/short, vol-targeted at entry), unchanged, on the most-traded
Kraken-listed perps besides BTC and ETH. With no new parameter search there is
nothing to overfit: the only question is whether more coins make a better
book. Prices are Binance's daily perp candles (the archive in
data/historical_cache/binance_um), a proxy for the Kraken contracts with the
longest history. Costs are Kraken perp taker fees, 5 bps slippage on BTC/ETH
and 12 on alts, and a flat 0.01%/day funding.

Part 2, robustness of the taker-buy share book (research log, 2026-09-26): a
grid over rebalance days, universe size and leg size on the Kraken-listed
universe. It reports the share of cells that are positive, the plateau, and the
deflated Sharpe of the best in-sample cell, counting every configuration tried
in both studies.

Part 3, the mix: the core BTC/ETH book with 0-40% moved into the alt trend book
and into the taker-buy book, as blended daily return streams.

Usage:
    python scripts/research/diversification_study.py
    python scripts/research/diversification_study.py --coins 16

Writes data/research/diversification_<timestamp>/.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from src.data.binance_archive import CACHE_DIR, base_asset, kraken_perp_bases, load_panel
from src.portfolio.backtest import daily_returns, period_metrics, prepare_inputs, run_book
from src.portfolio.config import InstrumentSpec, load_portfolio_config
from src.portfolio.sleeves import SleeveSpec
from src.research.portfolio import PortfolioCosts, liquid_universe, rank_weights, simulate_portfolio, slippage_by_liquidity
from src.research.stats import deflated_sharpe_ratio, sharpe_per_period
from src.storage.bar_aggregator import OHLCVBar

HOLDOUT = pd.Timestamp("2024-10-01", tz="UTC")  # the split the perp and portfolio studies use
XS_HOLDOUT = pd.Timestamp("2024-01-01", tz="UTC")  # the split the cross-sectional study used
XS_START = pd.Timestamp("2020-06-01", tz="UTC")
EARLIER_TRIALS = 32  # the cross-sectional study: 8 features x 2 rebalance intervals x 2 universes
EXCLUDED_BASES = {"BTC", "ETH", "USDC", "USDT", "DAI", "FDUSD", "TUSD", "BUSD", "PAXG", "XAUT"}


def _load_cross_sectional_helpers():
    path = Path(__file__).with_name("cross_sectional_study.py")
    spec = importlib.util.spec_from_file_location("cross_sectional_study", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def binance_daily_bars(klines: pd.DataFrame) -> dict[str, list[OHLCVBar]]:
    """Daily OHLCV bars per base asset (BTC, SOL, ...) from the Binance archive panel."""
    out: dict[str, list[OHLCVBar]] = {}
    for symbol, frame in klines.groupby("symbol"):
        frame = frame[frame["quote_volume"] > 0].sort_values("date")
        out[base_asset(symbol)] = [
            OHLCVBar(exchange="binance", symbol=symbol, interval_seconds=86400, timestamp=row.date.to_pydatetime(), open=row.open, high=row.high,
                     low=row.low, close=row.close, volume=row.volume)
            for row in frame.itertuples()
        ]
    return out


def pick_alts(klines: pd.DataFrame, bases: set[str], count: int) -> list[str]:
    """The `count` Kraken-listed coins (not BTC/ETH/stable) most traded over the last year, with history from 2021 or earlier."""
    recent = klines[klines["date"] >= klines["date"].max() - pd.Timedelta(days=365)]
    volume = recent.groupby("symbol")["quote_volume"].mean()
    first = klines.groupby("symbol")["date"].min()
    rows = []
    for symbol, average in volume.items():
        base = base_asset(symbol)
        if base in bases and base not in EXCLUDED_BASES and first[symbol] <= pd.Timestamp("2021-12-31", tz="UTC"):
            rows.append((average, base))
    return [base for _average, base in sorted(rows, reverse=True)[:count]]


def trend_config(example_path: str, alts: list[str]):
    """The example book's daily sleeves on BTC/ETH (the core) plus the same two rules on each alt."""
    example = load_portfolio_config(example_path)
    core = tuple(sleeve for sleeve in example.sleeves if sleeve.interval == "1d")  # the archive is daily
    instruments = {key: replace(spec, slippage_bps=5.0) for key, spec in example.instruments.items()}
    sleeves = list(core)
    for base in alts:
        instrument = f"kraken_futures:{base}/USD"
        instruments[instrument] = InstrumentSpec(id=instrument, kind="perp", max_leverage=2.0, slippage_bps=12.0)
        for sleeve in (sleeve for sleeve in core if sleeve.instrument.endswith("BTC/USD")):  # the BTC sleeves' two rules
            sleeves.append(replace(sleeve, id=f"{base.lower()}_{sleeve.id.split('_', 1)[1]}", instrument=instrument))
    return replace(example, instruments=instruments, sleeves=tuple(sleeves)), [s.id for s in core], [s.id for s in sleeves if s not in core]


def _metrics_rows(label: str, returns: pd.Series, split: pd.Timestamp) -> list[dict[str, object]]:
    rows = []
    for period, part in (("is", returns[returns.index < split]), ("ho", returns[returns.index >= split])):
        if len(part) < 30:
            continue
        equity = (1 + part).cumprod()
        years = len(part) / 365.0
        rows.append({"book": label, "period": period, "sharpe": float(part.mean() / part.std() * np.sqrt(365)), "cagr": float(equity.iloc[-1] ** (1 / years) - 1),
                     "max_drawdown": float((1 - equity / equity.cummax()).max()), "vol": float(part.std() * np.sqrt(365))})
    return rows


def _pivot(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    return frame.pivot_table(index="book", columns="period", values=["sharpe", "cagr", "max_drawdown"], sort=False)[
        [("sharpe", "is"), ("sharpe", "ho"), ("cagr", "is"), ("cagr", "ho"), ("max_drawdown", "is"), ("max_drawdown", "ho")]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config/portfolio.example.toml", help="The core book (its daily sleeves are used)")
    parser.add_argument("--coins", type=int, default=12, help="How many alts to add")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or Path("data/research") / f"diversification_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220)

    klines = load_panel("klines_1d", cache_dir=CACHE_DIR)
    if klines.empty:
        raise SystemExit("No Binance archive cached; run scripts/research/cross_sectional_study.py --download first.")
    on_kraken = kraken_perp_bases()
    bars = binance_daily_bars(klines)
    alts = pick_alts(klines, on_kraken, args.coins)
    config, core_ids, alt_ids = trend_config(args.config, alts)
    loader = lambda spec, interval: bars[spec.symbol.split("/")[0]]  # noqa: E731 - every instrument maps to its Binance base
    inputs = prepare_inputs(config, bar_loader=loader)
    print(f"Part 1: core = {core_ids}; alts ({len(alts)}): {', '.join(alts)}; {inputs.measure_start:%Y-%m-%d} to {inputs.prices.index[-1]:%Y-%m-%d}, holdout from {HOLDOUT:%Y-%m-%d}")

    books = {
        "core (BTC/ETH)": run_book(config, inputs, sleeves=core_ids),
        f"alts ({len(alts)} coins)": run_book(config, inputs, sleeves=alt_ids),
        "all sleeves, equal": run_book(config, inputs),
    }
    budgets = {sleeve.id: (0.5 / len(core_ids) if sleeve.id in core_ids else 0.5 / len(alt_ids)) for sleeve in config.sleeves}
    fifty = replace(config, sleeves=tuple(replace(sleeve, budget=budgets[sleeve.id]) for sleeve in config.sleeves))
    books["core 50% / alts 50%"] = run_book(fifty, inputs, allocation="fixed")
    trend_rows = [row for label, book in books.items() for row in period_metrics(book, HOLDOUT, label=label)]
    print(pd.DataFrame(trend_rows).pivot_table(index="book", columns="period", values=["sharpe", "cagr", "max_drawdown", "avg_gross_exposure"], sort=False).round(2).to_string())
    core_daily, alt_daily = daily_returns(books["core (BTC/ETH)"]), daily_returns(books[f"alts ({len(alts)} coins)"])
    alone = {sleeve_id: daily_returns(run_book(config, inputs, allocation="equal", sleeves=[sleeve_id], risk_overlay=False)) for sleeve_id in alt_ids + core_ids}
    correlation = pd.DataFrame(alone).corr()
    alt_pairs = correlation.loc[alt_ids, alt_ids].to_numpy()[np.triu_indices(len(alt_ids), 1)]
    print(f"Daily-return correlation: core book vs alt book {core_daily.corr(alt_daily):.2f}; alt sleeves with each other {np.nanmean(alt_pairs):.2f} on average; "
          f"alt sleeves with the core sleeves {np.nanmean(correlation.loc[alt_ids, core_ids].to_numpy()):.2f}")
    per_coin = pd.DataFrame([{**row, "sleeve": sleeve_id} for sleeve_id in alt_ids for row in period_metrics(run_book(config, inputs, allocation="equal", sleeves=[sleeve_id], risk_overlay=False), HOLDOUT, label=sleeve_id)])
    positive = per_coin.pivot_table(index="sleeve", columns="period", values="sharpe")
    print(f"Alt sleeves alone: {int((positive['is'] > 0).sum())}/{len(positive)} positive in-sample, {int((positive['ho'] > 0).sum())}/{len(positive)} in the holdout; "
          f"median Sharpe {positive['is'].median():.2f} IS / {positive['ho'].median():.2f} HO")

    # Part 2: taker-buy share robustness on the Kraken-listed universe
    helpers = _load_cross_sectional_helpers()
    wide = helpers.load_wide(CACHE_DIR)
    close, volume = wide["close"], wide["quote_volume"]
    feature = helpers.build_features(wide)["taker_buy_share_7d"]
    kraken_columns = [symbol for symbol in close.columns if base_asset(symbol) in on_kraken]
    costs = PortfolioCosts(fee_pct=0.05, slippage_bps=slippage_by_liquidity(volume))
    grid_rows, grid_returns = [], {}
    for top_n in (20, 30, 50):
        universe = liquid_universe(volume, top_n=top_n)
        universe.loc[:, [symbol for symbol in close.columns if symbol not in kraken_columns]] = False
        universe.loc[universe.index < XS_START] = False
        for quantile in (0.1, 0.2, 0.3):
            weights = rank_weights(feature, universe, quantile=quantile, gross=1.0, min_names=max(6, int(top_n * 0.4)))
            for rebalance in (3, 5, 7, 10, 14):
                result = simulate_portfolio(close, weights, funding=wide["funding"], costs=costs, rebalance_every=rebalance)
                key = f"top{top_n}_q{quantile:g}_r{rebalance}"
                grid_returns[key] = result.returns
                row = {"config": key, "top_n": top_n, "quantile": quantile, "rebalance_days": rebalance}
                for label, start, end in (("is", XS_START, XS_HOLDOUT), ("ho", XS_HOLDOUT, None)):
                    metrics = result.metrics(start, end)
                    row[f"{label}_sharpe"], row[f"{label}_max_drawdown"] = metrics.get("sharpe", np.nan), metrics.get("max_drawdown", np.nan)
                grid_rows.append(row)
    grid = pd.DataFrame(grid_rows)
    best = grid.loc[grid["is_sharpe"].idxmax()]
    is_returns = {key: series[(series.index >= XS_START) & (series.index < XS_HOLDOUT)] for key, series in grid_returns.items()}
    trials = len(grid) + EARLIER_TRIALS
    variance = float(np.var([sharpe_per_period(series) for series in is_returns.values()]))
    deflated = deflated_sharpe_ratio(is_returns[best["config"]].to_numpy(), trials=trials, sharpe_variance=variance)
    print(f"\nPart 2: taker-buy share, {len(grid)} configurations on the Kraken-listed universe (in-sample {XS_START:%Y-%m} to 2023, holdout 2024 on):")
    print(f"  positive in-sample {int((grid['is_sharpe'] > 0).sum())}/{len(grid)}, in the holdout {int((grid['ho_sharpe'] > 0).sum())}/{len(grid)}; "
          f"median Sharpe {grid['is_sharpe'].median():.2f} IS / {grid['ho_sharpe'].median():.2f} HO")
    print(f"  best in-sample: {best['config']} (IS {best['is_sharpe']:.2f}, HO {best['ho_sharpe']:.2f}); deflated Sharpe probability {deflated:.2f} "
          f"after {trials} trials (above 0.95 = unlikely to be luck)")
    print(grid.pivot_table(index=["top_n", "quantile"], columns="rebalance_days", values="ho_sharpe").round(2).to_string())

    # Part 3: mixes of daily return streams, on the portfolio split
    xs_choice = "top50_q0.2_r7"  # the configuration the cross-sectional study reported, not the grid's best cell
    xs_daily = grid_returns[xs_choice].reindex(core_daily.index).fillna(0.0)
    mix_rows = []
    for share in (0.0, 0.1, 0.2, 0.3, 0.4):
        mix_rows += _metrics_rows(f"core + {share:.0%} alt trend", (1 - share) * core_daily + share * alt_daily.reindex(core_daily.index).fillna(0.0), HOLDOUT)
        if share:
            mix_rows += _metrics_rows(f"core + {share:.0%} taker-buy", (1 - share) * core_daily + share * xs_daily, HOLDOUT)
    print(f"\nPart 3: blends of daily returns (taker-buy = {xs_choice}; correlation with the core book {core_daily.corr(xs_daily):.2f}):")
    print(_pivot(mix_rows).round(2).to_string())

    pd.DataFrame(trend_rows).to_csv(out / "trend_books.csv", index=False)
    per_coin.to_csv(out / "alt_sleeves.csv", index=False)
    correlation.to_csv(out / "sleeve_correlation.csv")
    grid.to_csv(out / "taker_grid.csv", index=False)
    pd.DataFrame(mix_rows).to_csv(out / "mixes.csv", index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
