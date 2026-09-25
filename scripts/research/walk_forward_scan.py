"""Reusable signal-research harness: walk-forward validation across symbols/strategies/params.

This is the "test a signal hypothesis" tool: point it at a list of symbols
and strategy configurations, and it fetches (and locally caches) real Kraken
history, runs proper walk-forward out-of-sample evaluation for each
combination, and prints/saves a comparison so a candidate strategy has to
clear a bar across multiple out-of-sample folds before it's worth taking
seriously - not just look good on one calm backtest window.

Usage:
    python scripts/research/walk_forward_scan.py
    python scripts/research/walk_forward_scan.py --symbols BTC/EUR ETH/EUR --lookback-days 60
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backtest.costs import build_default_cost_model
from src.backtest.runner import BacktestConfig, StrategyRegistry
from src.backtest.simple_backtest import (
    band_reversion_strategy,
    make_long_only,
    make_regime_gated,
    momentum_breakout_strategy,
    moving_average_crossover_strategy,
    volume_confirmed_momentum_biased_strategy,
    volume_confirmed_momentum_strategy,
)
from src.backtest.walk_forward import evaluate_walk_forward
from src.data.historical import load_or_fetch_kraken_history
from src.risk.controls import RiskControlConfig, RiskManager

# Each entry: (label, factory_fn, kwargs). `factory_fn` takes **kwargs and
# returns a StrategyFn (built-ins with wrappers applied via lambdas so the
# same list drives symmetric, long-only, regime-gated and mean-reversion
# candidates side by side). Windows are chosen for hourly/4h bars, not the
# (3, 6)-on-1-second-bars mistake found in live testing - a "moving average"
# needs enough bars to represent real momentum, not tick noise. Add
# candidates here as new hypotheses come up; this list is the point of the
# tool.
STRATEGY_CONFIGS: list[tuple[str, Callable[..., Any], dict[str, Any]]] = [
    ("moving_average_crossover(4,24)", moving_average_crossover_strategy, {"short_window": 4, "long_window": 24}),
    (
        "moving_average_crossover(4,24)_long_only",
        lambda **kwargs: make_long_only(moving_average_crossover_strategy(**kwargs)),
        {"short_window": 4, "long_window": 24},
    ),
    ("momentum_breakout(6,1%)", momentum_breakout_strategy, {"lookback": 6, "threshold": 0.01}),
    (
        "momentum_breakout(6,1%)_long_only",
        lambda **kwargs: make_long_only(momentum_breakout_strategy(**kwargs)),
        {"lookback": 6, "threshold": 0.01},
    ),
    (
        "volume_confirmed_momentum(6,1%,12,1.5x)",
        volume_confirmed_momentum_strategy,
        {"lookback": 6, "threshold": 0.01, "volume_window": 12, "volume_multiplier": 1.5},
    ),
    (
        "volume_confirmed_momentum_biased(6,1%,12,1.5x)",
        volume_confirmed_momentum_biased_strategy,
        {"lookback": 6, "threshold": 0.01, "volume_window": 12, "volume_multiplier": 1.5},
    ),
    (
        "volume_confirmed_momentum(6,1%,12,1.5x)_long_only",
        volume_confirmed_momentum_strategy,
        {"lookback": 6, "threshold": 0.01, "volume_window": 12, "volume_multiplier": 1.5, "allow_short": False},
    ),
    (
        "band_reversion(20,2std)",
        band_reversion_strategy,
        {"window": 20, "num_std": 2.0},
    ),
    (
        "band_reversion(20,2std)_long_only",
        band_reversion_strategy,
        {"window": 20, "num_std": 2.0, "allow_short": False},
    ),
    (
        "moving_average_crossover(4,24)_regime_gated_trending",
        lambda **kwargs: make_regime_gated(moving_average_crossover_strategy(**kwargs), required_regime="trending"),
        {"short_window": 4, "long_window": 24},
    ),
]

DEFAULT_SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]


def run_scan(
    *,
    symbols: list[str],
    interval_seconds: int,
    lookback_days: int,
    train_window: int,
    test_window: int,
    step_size: int,
    refresh: bool,
    apply_costs: bool = True,
) -> list[dict[str, Any]]:
    """Run the walk-forward scan across every symbol/strategy combination and return the results."""
    risk_manager = RiskManager(RiskControlConfig(paper_mode=True))
    results: list[dict[str, Any]] = []
    # Real Kraken taker fee (0.40%) applied on every simulated fill, plus a
    # 10bps FX-spread proxy for slippage - a strategy's apparent edge has to
    # clear this before it's worth taking seriously, since paper/live will
    # pay it on every single trade.
    kraken_costs = build_default_cost_model("kraken")

    for symbol in symbols:
        print(f"\nFetching {symbol} ({interval_seconds}s bars, {lookback_days}d lookback)...")
        try:
            bars = load_or_fetch_kraken_history(
                symbol=symbol,
                interval_seconds=interval_seconds,
                lookback_days=lookback_days,
                refresh=refresh,
            )
        except Exception as exc:
            print(f"  skipped {symbol}: {exc}")
            continue
        achieved_days = (bars[-1].timestamp - bars[0].timestamp).total_seconds() / 86400.0
        print(f"  {len(bars)} bars, {bars[0].timestamp} -> {bars[-1].timestamp} ({achieved_days:.1f}d achieved of {lookback_days}d requested)")
        if achieved_days < lookback_days * 0.8:
            print(f"  NOTE: only reached {achieved_days:.1f} of the requested {lookback_days} days - Kraken's OHLC endpoint likely capped history at this interval; use a coarser --interval-seconds for more real depth.")

        if len(bars) < train_window + test_window:
            print(f"  not enough bars for train_window={train_window} + test_window={test_window}, skipping")
            continue

        for label, factory, kwargs in STRATEGY_CONFIGS:
            registry = StrategyRegistry()
            synthetic_name = f"_scan_{label}"
            registry.register(synthetic_name, lambda **_: factory(**kwargs))
            config = BacktestConfig(
                strategy_name=synthetic_name,
                include_costs=apply_costs,
                taker_fee=kraken_costs.taker_fee,
                maker_fee=kraken_costs.maker_fee,
                fx_spread_bps=kraken_costs.fx_spread_bps,
            )

            try:
                wf_result = evaluate_walk_forward(
                    bars,
                    config=config,
                    registry=registry,
                    train_window=train_window,
                    test_window=test_window,
                    step_size=step_size,
                )
            except Exception as exc:
                print(f"  {symbol} / {label}: FAILED ({exc})")
                continue

            summary = wf_result.summary
            row = {
                "symbol": symbol,
                "strategy": label,
                "params": kwargs,
                "fold_count": summary.get("fold_count", 0),
                "avg_return": summary.get("avg_return", 0.0),
                "median_return": summary.get("median_return", 0.0),
                "positive_folds": summary.get("positive_folds", 0),
                "cumulative_return": summary.get("cumulative_return", 0.0),
                "total_trades": sum(fold.trades for fold in wf_result.folds),
            }
            results.append(row)
            fold_count = row["fold_count"] or 1
            print(
                f"  {symbol:8s} | {label:45s} | folds={row['fold_count']:2d} "
                f"pos={row['positive_folds']:2d}/{fold_count:2d} "
                f"avg_ret={row['avg_return']:+.4%} median_ret={row['median_return']:+.4%} "
                f"cum_ret={row['cumulative_return']:+.4%} trades={row['total_trades']}"
            )

    return results


def main() -> None:
    """Parse CLI arguments and run the walk-forward scan."""
    parser = argparse.ArgumentParser(description="Walk-forward signal-hypothesis scan across symbols and strategies")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Symbols to test, e.g. BTC/EUR ETH/EUR SOL/EUR")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=14400,
        help=(
            "Bar interval in seconds (must be a Kraken-supported OHLC interval). "
            "Kraken's public OHLC endpoint only retains ~720 candles per pair/interval "
            "regardless of lookback-days, so reaching a real 90-day window needs a "
            "coarse-enough interval (4h default covers ~120 days in one page; 1h only "
            "covers ~30 days)."
        ),
    )
    parser.add_argument("--lookback-days", type=int, default=90, help="How many days of history to pull/use (subject to the ~720-candle ceiling above)")
    parser.add_argument("--train-window", type=int, default=90, help="Bars in each walk-forward training window")
    parser.add_argument("--test-window", type=int, default=20, help="Bars in each walk-forward out-of-sample test window")
    parser.add_argument("--step-size", type=int, default=20, help="Bars to advance between folds")
    parser.add_argument("--refresh", action="store_true", help="Force a fresh data pull instead of using the local cache")
    parser.add_argument("--no-costs", action="store_true", help="Disable the real Kraken fee/FX-spread cost model (results become unrealistically optimistic)")
    parser.add_argument("--output-path", default=None, help="Optional path to save results as JSON (defaults to data/research/walk_forward_<timestamp>.json)")
    args = parser.parse_args()

    results = run_scan(
        symbols=args.symbols,
        interval_seconds=args.interval_seconds,
        lookback_days=args.lookback_days,
        train_window=args.train_window,
        test_window=args.test_window,
        step_size=args.step_size,
        refresh=args.refresh,
        apply_costs=not args.no_costs,
    )

    output_path = Path(args.output_path) if args.output_path else Path("data/research") / f"walk_forward_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as handle:
        json.dump(
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "interval_seconds": args.interval_seconds,
                "lookback_days": args.lookback_days,
                "train_window": args.train_window,
                "test_window": args.test_window,
                "step_size": args.step_size,
                "apply_costs": not args.no_costs,
                "results": results,
            },
            handle,
            indent=2,
        )
    print(f"\nSaved {len(results)} results to {output_path}")


if __name__ == "__main__":
    main()
