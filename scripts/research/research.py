"""Strategy research CLI: list strategies, backtest one, compare all, or sweep parameters.

Examples (full walkthrough in docs/research_guide.md):

    python scripts/research/research.py list
    python scripts/research/research.py backtest donchian_breakout --symbol BTC/EUR --interval 1d --param entry_window=40 --plot
    python scripts/research/research.py compare --interval 4h
    python scripts/research/research.py sweep --strategies donchian_breakout trend_tstat --intervals 4h 1d
    python scripts/research/research.py sweep --strategies keltner_breakout --grid window=10,20,30 --grid atr_multiplier=1,2,3

Everything is written to data/research/<command>_<timestamp>/ (override with --out).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import matplotlib

matplotlib.use("Agg")

import pandas as pd

from src.research import CATALOG, CostSettings, catalog_table, compare, load_bars, plot_heatmap, plot_run, run_strategy, summarize, sweep, sweep_axes
from src.research.engine import split_index, warmup_bars

DEFAULT_SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]
SUMMARY_COLUMNS = ["strategy", "interval", "long_only", "combos", "share_positive_is", "median_is", "median_ho", "best_params", "best_is", "best_neighbors_is", "best_ho", "best_ho_return", "rank_corr", "buy_hold_is", "buy_hold_ho"]
COMPARE_COLUMNS = ["strategy", "interval", "long_only", "symbol", "is_return", "is_sharpe", "is_max_drawdown", "is_trades", "is_exposure", "is_buy_hold_sharpe", "ho_return", "ho_sharpe", "ho_trades", "ho_buy_hold_sharpe"]


def main() -> None:
    """Parse the subcommand and run it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="Show every strategy in the research catalog with its hypothesis and grid")

    backtest_parser = commands.add_parser("backtest", help="Backtest one strategy on one symbol and show in-sample/holdout metrics")
    backtest_parser.add_argument("strategy", choices=sorted(CATALOG))
    backtest_parser.add_argument("--symbol", default="BTC/EUR")
    backtest_parser.add_argument("--interval", default="4h")
    backtest_parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="Override a parameter (repeatable)")
    backtest_parser.add_argument("--long-only", action="store_true", help="Suppress short signals (what spot live trading does)")
    backtest_parser.add_argument("--plot", action="store_true", help="Save an equity-vs-buy-and-hold chart")
    _add_data_arguments(backtest_parser, multiple_symbols=False)
    _add_evaluation_arguments(backtest_parser)

    compare_parser = commands.add_parser("compare", help="Run every strategy at its default parameters on each symbol")
    compare_parser.add_argument("--strategies", nargs="+", choices=sorted(CATALOG), default=None)
    compare_parser.add_argument("--intervals", nargs="+", default=["4h"])
    _add_data_arguments(compare_parser, multiple_symbols=True)
    _add_evaluation_arguments(compare_parser)
    _add_side_argument(compare_parser)

    sweep_parser = commands.add_parser("sweep", help="Sensitivity sweep: every grid combination on each symbol, plus heatmaps")
    sweep_parser.add_argument("--strategies", nargs="+", choices=sorted(CATALOG), default=None, help="Default: every catalog strategy that has a grid")
    sweep_parser.add_argument("--intervals", nargs="+", default=["4h"])
    sweep_parser.add_argument("--grid", action="append", default=[], metavar="NAME=V1,V2", help="Replace the catalog grid (single strategy only, repeatable)")
    _add_data_arguments(sweep_parser, multiple_symbols=True)
    _add_evaluation_arguments(sweep_parser)
    _add_side_argument(sweep_parser)

    args = parser.parse_args()
    {"list": _cmd_list, "backtest": _cmd_backtest, "compare": _cmd_compare, "sweep": _cmd_sweep}[args.command](args)


def _cmd_list(args: argparse.Namespace) -> None:
    table = catalog_table()
    for row in table.itertuples():
        print(f"\n{row.strategy}  [{row.family}]  ({row.combos} combos)")
        print(f"  why:      {row.hypothesis}")
        print(f"  defaults: {row.defaults}")
        print(f"  grid:     {row.grid}")


def _cmd_backtest(args: argparse.Namespace) -> None:
    params = _parse_assignments(args.param)
    if args.long_only:
        params["long_only"] = True
    bars = load_bars(args.symbol, args.interval, lookback_days=args.lookback_days, refresh=args.refresh, csv_path=_csv_paths(args.csv).get(args.symbol))
    run = run_strategy(bars, args.strategy, params=params, costs=_costs(args), holdout_fraction=args.holdout_fraction)

    print(f"\n{args.strategy} {params or '(defaults)'} on {args.symbol} {args.interval}, costs {_costs(args).round_trip_pct:.2f}% per round trip")
    print(f"in-sample {run.result.timestamps[run.measure_start]:%Y-%m-%d} -> {run.result.timestamps[run.split_index - 1]:%Y-%m-%d}", end="")
    if run.split_index < len(bars):
        print(f", holdout {run.result.timestamps[run.split_index]:%Y-%m-%d} -> {run.result.timestamps[-1]:%Y-%m-%d}")
    print("\n" + run.metrics_table().to_string(float_format=lambda value: f"{value:.4f}"))

    if args.plot:
        output_dir = _output_dir(args, "backtest")
        path = output_dir / f"{args.strategy}_{args.symbol.replace('/', '-')}_{args.interval}.png"
        plot_run(run, path=path)
        print(f"\nChart: {path}")


def _cmd_compare(args: argparse.Namespace) -> None:
    strategies = args.strategies or list(CATALOG)
    output_dir = _output_dir(args, "compare")
    frames = []
    for interval in args.intervals:
        data = _load_symbols(args, interval)
        start = _common_start(data, [(name, CATALOG[name].defaults) for name in strategies], args.holdout_fraction)
        print(f"\n{interval}: comparing {len(strategies)} strategies on {', '.join(data)} (measuring from bar {start})")
        frames.append(compare(data, strategies, long_only=_sides(args), costs=_costs(args), holdout_fraction=args.holdout_fraction, measure_start=start))
    results = pd.concat(frames, ignore_index=True)
    _write_outputs(results, output_dir, args, heatmaps=False)
    table = results.groupby(["strategy", "interval", "long_only"], sort=False)[[c for c in COMPARE_COLUMNS if c.startswith(("is_", "ho_"))]].mean().reset_index()
    print("\nMean across symbols (per-symbol rows are in results.csv):")
    print(table.sort_values(["interval", "is_sharpe"], ascending=[True, False]).to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    print(f"\nWrote {output_dir}")


def _cmd_sweep(args: argparse.Namespace) -> None:
    strategies = args.strategies or [name for name, spec in CATALOG.items() if spec.grid]
    grid_override = _parse_grid(args.grid)
    if grid_override and len(strategies) != 1:
        raise SystemExit("--grid replaces one strategy's grid; pass exactly one --strategies name with it")
    output_dir = _output_dir(args, "sweep")
    frames = []
    for interval in args.intervals:
        data = _load_symbols(args, interval)
        jobs = [(name, combo) for name in strategies for combo in CATALOG[name].combos(grid_override or None)]
        start = _common_start(data, jobs, args.holdout_fraction)
        print(f"\n{interval}: sweeping {len(jobs)} combos x {len(_sides(args))} side mode(s) x {len(data)} symbols (measuring from bar {start})")
        for name in strategies:
            started = time.perf_counter()
            frames.append(
                sweep(data, name, grid_override or None, long_only=_sides(args), costs=_costs(args), holdout_fraction=args.holdout_fraction, measure_start=start)
            )
            print(f"  {name}: done in {time.perf_counter() - started:.1f}s", flush=True)
    results = pd.concat(frames, ignore_index=True)
    summary = _write_outputs(results, output_dir, args, heatmaps=True)
    print("\n" + _format_summary(summary).to_string(index=False))
    print(f"\nWrote {output_dir} (results.csv, summary.csv, summary.md, heatmaps/). How to read it: docs/research_guide.md")


def _write_outputs(results: pd.DataFrame, output_dir: Path, args: argparse.Namespace, *, heatmaps: bool) -> pd.DataFrame:
    output_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(output_dir / "results.csv", index=False)
    summary = summarize(results, metric=args.metric)
    summary.to_csv(output_dir / "summary.csv", index=False)
    config = {key: value for key, value in vars(args).items()}
    config["run_at"] = datetime.now(timezone.utc).isoformat()
    config["round_trip_cost_pct"] = _costs(args).round_trip_pct
    config["periods"] = results.groupby("interval")[["is_from", "is_to", "ho_from", "ho_to"]].first().to_dict(orient="index")
    (output_dir / "config.json").write_text(json.dumps(config, indent=2, default=str))
    (output_dir / "summary.md").write_text(_summary_markdown(summary, config))
    if heatmaps:
        for (strategy, interval, long_only), group in results.groupby(["strategy", "interval", "long_only"], sort=False):
            if sweep_axes(group, strategy):
                side = "long_only" if long_only else "long_short"
                plot_heatmap(group, strategy, metric=args.metric, interval=interval, long_only=long_only, path=output_dir / "heatmaps" / interval / f"{strategy}_{side}.png")
    return summary


def _summary_markdown(summary: pd.DataFrame, config: dict[str, Any]) -> str:
    lines = [
        f"# Research run {config['run_at'][:16]}",
        "",
        f"- Command: `{config['command']}`, symbols: {', '.join(config['symbols'])}, intervals: {', '.join(config['intervals'])}",
        f"- Costs: {config['fee_pct']}% fee + {config['slippage_bps']} bps slippage per fill (~{config['round_trip_cost_pct']:.2f}% round trip)",
        f"- Holdout: last {config['holdout_fraction']:.0%} of bars; metric: {config['metric']} (annualised, mark-to-market)",
    ]
    for interval, period in config["periods"].items():
        lines.append(f"- {interval}: in-sample {period['is_from']} -> {period['is_to']}, holdout {period['ho_from']} -> {period['ho_to']}")
    lines += ["", "Column meanings and how to judge them: docs/research_guide.md", "", _markdown_table(_format_summary(summary))]
    return "\n".join(lines) + "\n"


def _format_summary(summary: pd.DataFrame) -> pd.DataFrame:
    table = summary[[column for column in SUMMARY_COLUMNS if column in summary.columns]].copy()
    table["long_only"] = table["long_only"].map({True: "long", False: "long/short"})
    for column in table.columns:
        if column == "best_ho_return":
            table[column] = table[column].map(lambda value: f"{value:+.1%}")
        elif column in {"share_positive_is"}:
            table[column] = table[column].map(lambda value: f"{value:.0%}")
        elif table[column].dtype.kind == "f":
            table[column] = table[column].map(lambda value: f"{value:.2f}")
    table["best_params"] = table["best_params"].map(lambda text: ", ".join(f"{k}={v}" for k, v in json.loads(text).items()))
    return table


def _markdown_table(frame: pd.DataFrame) -> str:
    header = "| " + " | ".join(frame.columns) + " |"
    divider = "| " + " | ".join("---" for _ in frame.columns) + " |"
    rows = ["| " + " | ".join(str(value) for value in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join([header, divider, *rows])


def _load_symbols(args: argparse.Namespace, interval: str) -> dict[str, list[Any]]:
    csv_paths = _csv_paths(args.csv)
    data = {}
    for symbol in args.symbols:
        bars = load_bars(symbol, interval, lookback_days=args.lookback_days, refresh=args.refresh, csv_path=csv_paths.get(symbol))
        print(f"  {symbol} {interval}: {len(bars)} bars, {bars[0].timestamp:%Y-%m-%d} -> {bars[-1].timestamp:%Y-%m-%d}")
        data[symbol] = bars
    return data


def _common_start(data: dict[str, list[Any]], jobs: list[tuple[str, dict[str, Any]]], holdout_fraction: float) -> int:
    start = max(warmup_bars(name, params) for name, params in jobs)
    shortest_split = min(split_index(len(bars), holdout_fraction) for bars in data.values())
    if start >= shortest_split - 20:
        raise SystemExit(f"Warmup ({start} bars) leaves almost no in-sample period ({shortest_split} bars). Load more history or drop the long-window strategies.")
    return start


def _add_data_arguments(parser: argparse.ArgumentParser, *, multiple_symbols: bool) -> None:
    if multiple_symbols:
        parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--lookback-days", type=int, default=None, help="Default: all Kraken gives (720 candles)")
    parser.add_argument("--refresh", action="store_true", help="Re-download instead of using data/historical_cache")
    parser.add_argument("--csv", action="append", default=[], metavar="SYMBOL=PATH", help="Use a downloaded OHLCV CSV for a symbol (repeatable)")
    parser.add_argument("--out", default=None, help="Output directory (default data/research/<command>_<timestamp>)")


def _add_evaluation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fee-pct", type=float, default=0.40, help="Fee per fill in percent (Kraken taker 0.40, maker 0.25)")
    parser.add_argument("--slippage-bps", type=float, default=10.0, help="Slippage per fill in basis points")
    parser.add_argument("--holdout-fraction", type=float, default=0.3, help="Share of the most recent bars held out from parameter choice")
    parser.add_argument("--metric", default="sharpe", choices=["sharpe", "return", "consistency"], help="Metric the summary ranks by")


def _add_side_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sides", choices=["both", "long", "long-short"], default="both", help="Run long-only, long/short, or both")


def _sides(args: argparse.Namespace) -> tuple[bool, ...]:
    return {"both": (False, True), "long": (True,), "long-short": (False,)}[args.sides]


def _costs(args: argparse.Namespace) -> CostSettings:
    return CostSettings(fee_pct=args.fee_pct, slippage_bps=args.slippage_bps)


def _output_dir(args: argparse.Namespace, command: str) -> Path:
    if args.out:
        return Path(args.out)
    return Path("data/research") / f"{command}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _parse_assignments(assignments: list[str]) -> dict[str, Any]:
    parsed = {}
    for assignment in assignments:
        name, _, value = assignment.partition("=")
        if not value:
            raise SystemExit(f"Expected NAME=VALUE, got '{assignment}'")
        parsed[name.strip()] = _parse_value(value.strip())
    return parsed


def _parse_grid(assignments: list[str]) -> dict[str, list[Any]]:
    return {name: [_parse_value(item) for item in str(values).split(",")] for name, values in ((a.partition("=")[0], a.partition("=")[2]) for a in assignments)}


def _csv_paths(assignments: list[str]) -> dict[str, str]:
    return {name: path for name, _, path in (assignment.partition("=") for assignment in assignments)}


if __name__ == "__main__":
    main()
