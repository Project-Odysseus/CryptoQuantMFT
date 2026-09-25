"""Backtests, parameter sweeps and robustness summaries for strategy research.

How every number here is produced (the "why" is in docs/research_guide.md):

1. Each (strategy, parameters, symbol) is ONE continuous backtest over all
   bars: trading costs on every fill, full position size (100% of equity,
   no leverage) and no risk overlay, so results measure the signal itself.
2. Bars are split in time. The first part is in-sample (IS); the last
   ``holdout_fraction`` is holdout (HO). Both segments are measured on the
   same mark-to-market equity curve, so a position that spans the boundary
   is split correctly between them.
3. IS measurement starts at a common ``measure_start`` bar (the longest
   warmup of anything being compared), so every combo is judged over the
   same dates as every other combo and as buy-and-hold.
4. Choose parameters by IS numbers only. HO answers "does the IS winner
   still work on data the choice never saw?"
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest.costs import CostModel
from src.backtest.indicators import series
from src.backtest.simple_backtest import BacktestResult, SimpleBacktester, StrategyFn
from src.backtest.strategies import make_long_only, make_regime_gated
from src.data.historical import load_ohlcv_csv, load_or_fetch_kraken_history
from src.research.catalog import CATALOG, StrategySpec, build_strategy

INTERVALS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
KRAKEN_MAX_CANDLES = 720
SECONDS_PER_YEAR = 365 * 86400
METRICS = ("return", "sharpe", "max_drawdown", "exposure", "trades", "win_rate", "avg_trade", "consistency", "buy_hold_return", "buy_hold_sharpe", "buy_hold_max_drawdown", "bars")


def parse_interval(interval: str | int) -> int:
    """Turn '4h' / '1d' / 14400 into seconds."""
    if isinstance(interval, int):
        return interval
    if interval not in INTERVALS:
        raise ValueError(f"Unknown interval '{interval}'. Use one of {list(INTERVALS)} or seconds.")
    return INTERVALS[interval]


def interval_label(seconds: int) -> str:
    """Turn 14400 into '4h' (or '<n>s' for anything non-standard)."""
    return next((label for label, value in INTERVALS.items() if value == seconds), f"{seconds}s")


@dataclass(frozen=True, slots=True)
class CostSettings:
    """Per-fill trading costs, plus optional funding on open positions.

    Defaults are Kraken spot: the taker fee for small accounts (0.40%) plus
    10 bps of slippage, about 1.0% for a full round trip. Use `spot(maker=True)`
    or fee_pct=0.25 to see what resting limit orders would change, and
    `perp()` for perpetual futures.

    `funding_pct_per_day` is charged on the position held into each bar, as
    a percentage of the position's notional per day. Positive means longs
    pay and shorts receive (the usual state of a perpetual in a bull market),
    negative means the reverse. It only touches the mark-to-market equity
    that return, Sharpe, drawdown and consistency are measured on;
    `avg_trade` and `win_rate` are per-trade price results and exclude
    funding.
    """

    fee_pct: float = 0.40
    slippage_bps: float = 10.0
    funding_pct_per_day: float = 0.0

    @classmethod
    def spot(cls, *, maker: bool = False) -> "CostSettings":
        """Kraken spot: 0.40% taker + 10 bps slippage, or 0.25% maker with no slippage assumed."""
        return cls(fee_pct=0.25, slippage_bps=0.0) if maker else cls(fee_pct=0.40, slippage_bps=10.0)

    @classmethod
    def perp(cls, *, maker: bool = False, funding_pct_per_day: float = 0.01) -> "CostSettings":
        """Kraken Futures perpetual costs: 0.05% taker or 0.02% maker (the base fee tier), plus funding.

        Fees are the venue's published entry tier (checked 2026-09-25 against
        the public fee schedule). Funding defaults to 0.01%/day, close to the
        measured one-year mean for BTC and ETH (about 0.009%/day); it swings
        from negative to about +0.15%/day, so 0.03 is a reasonable stress
        case and 0.10 is extreme. The 5 bps taker slippage is still an
        assumption.
        """
        return cls(fee_pct=0.02, slippage_bps=0.0, funding_pct_per_day=funding_pct_per_day) if maker else cls(fee_pct=0.05, slippage_bps=5.0, funding_pct_per_day=funding_pct_per_day)

    def cost_model(self) -> CostModel | None:
        """Build the backtester's cost model, or None when costs are switched off."""
        if self.fee_pct == 0.0 and self.slippage_bps == 0.0:
            return None
        return CostModel(exchange="research", taker_fee=self.fee_pct, maker_fee=self.fee_pct, fx_spread_bps=self.slippage_bps)

    @property
    def round_trip_pct(self) -> float:
        """Approximate total cost of entering and exiting once, in percent (excluding funding)."""
        return 2.0 * (self.fee_pct + self.slippage_bps / 100.0)


def apply_funding(result: BacktestResult, funding_pct_per_day: float, *, interval_seconds: int) -> BacktestResult:
    """Return a copy of `result` whose mark-to-market equity has funding charged on the held position.

    Each bar's return is reduced by ``position * funding_per_bar`` where
    position is the signed fraction of equity held into that bar (long
    positive, short negative), so longs pay and shorts receive when the
    rate is positive.
    """
    if funding_pct_per_day == 0.0:
        return result
    per_bar = funding_pct_per_day / 100.0 * interval_seconds / 86400.0
    equity = np.asarray(result.mtm_equity_series, dtype=float)
    positions = np.asarray(result.position_series, dtype=float)
    returns = equity[1:] / equity[:-1] - 1.0 - positions[:-1] * per_bar
    adjusted = np.concatenate([[equity[0]], equity[0] * np.cumprod(1.0 + returns)])
    return replace(result, mtm_equity_series=[float(value) for value in adjusted])


def load_bars(
    symbol: str,
    interval: str | int = "4h",
    *,
    lookback_days: int | None = None,
    refresh: bool = False,
    csv_path: str | Path | None = None,
    source: str = "spot",
    start: datetime | None = None,
) -> list[Any]:
    """Load bars for one symbol, from a CSV if given, otherwise from Kraken (cached locally).

    Kraken's public API only returns the most recent 720 candles, so by
    default this asks for exactly that much: 30 days of 1h, 120 days of 4h
    or ~2 years of daily bars. For more history, download Kraken's OHLCVT
    CSV files and pass `csv_path`.
    """
    interval_seconds = parse_interval(interval)
    if source == "perp":
        # Kraken Futures perpetual trade candles, 2020-02-26 onwards for BTC/USD and ETH/USD (see HISTORY_VENUE_SYMBOLS).
        from src.data.kraken_futures import load_or_fetch_perp_history

        first = start or datetime(2020, 2, 26, tzinfo=timezone.utc)
        if lookback_days is not None:
            first = max(first, datetime.now(timezone.utc) - timedelta(days=lookback_days))
        return load_or_fetch_perp_history(symbol, interval_seconds=interval_seconds, start=first, refresh=refresh)
    if source != "spot":
        raise ValueError("source must be 'spot' or 'perp'")
    if csv_path is not None:
        bars = load_ohlcv_csv(csv_path, symbol=symbol, interval_seconds=interval_seconds)
        if lookback_days is not None:
            cutoff = bars[-1].timestamp.timestamp() - lookback_days * 86400
            bars = [bar for bar in bars if bar.timestamp.timestamp() >= cutoff]
        return bars
    if lookback_days is None:
        lookback_days = max(1, KRAKEN_MAX_CANDLES * interval_seconds // 86400 - 1)
    return load_or_fetch_kraken_history(symbol=symbol, interval_seconds=interval_seconds, lookback_days=lookback_days, refresh=refresh)


@dataclass(slots=True)
class ResearchRun:
    """One strategy on one symbol: the raw backtest plus metrics for each segment."""

    label: str
    params: dict[str, Any]
    symbol: str
    interval_seconds: int
    bars: list[Any]
    result: BacktestResult
    measure_start: int
    split_index: int
    metrics: dict[str, dict[str, float]]

    def metrics_table(self) -> pd.DataFrame:
        """Metrics as a table: one row per metric, one column per segment."""
        return pd.DataFrame(self.metrics)


def split_index(n_bars: int, holdout_fraction: float) -> int:
    """Index of the first holdout bar (== n_bars when there is no holdout)."""
    if not 0.0 <= holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be in [0, 1)")
    return n_bars - int(round(n_bars * holdout_fraction))


def warmup_bars(strategy: str | Callable[..., Any], params: dict[str, Any] | None = None) -> int:
    """Bars a strategy needs before it can signal (catalog value, or longest integer parameter + 2)."""
    if isinstance(strategy, str) and strategy in CATALOG:
        return CATALOG[strategy].warmup_bars(params)
    windows = [value for value in (params or {}).values() if isinstance(value, int) and not isinstance(value, bool)]
    return (max(windows) if windows else 1) + 2


def run_strategy(
    bars: Sequence[Any],
    strategy: str | StrategyFn,
    *,
    params: dict[str, Any] | None = None,
    costs: CostSettings | None = None,
    holdout_fraction: float = 0.3,
    measure_start: int | None = None,
    label: str | None = None,
) -> ResearchRun:
    """Backtest one strategy on one symbol and measure the in-sample and holdout segments.

    Args:
        strategy: A registered strategy name (built from catalog defaults
            plus `params`, including the `long_only` / `regime` wrappers),
            or a ready-made StrategyFn such as one you wrote in a notebook.
        measure_start: First bar counted in the in-sample metrics. Defaults
            to the strategy's warmup; pass a shared value to line several
            runs up on the same dates.
    """
    resolved_bars = list(bars)
    resolved_params = dict(params or {})
    resolved_costs = costs or CostSettings()
    if isinstance(strategy, str):
        strategy_fn = build_strategy(strategy, **resolved_params)
        name = strategy
        default_start = warmup_bars(strategy, {k: v for k, v in resolved_params.items() if k not in ("long_only", "regime")})
    else:
        strategy_fn = strategy
        name = label or getattr(strategy, "__name__", "custom")
        default_start = 1

    split = split_index(len(resolved_bars), holdout_fraction)
    start = max(1, default_start if measure_start is None else measure_start)
    if start >= split - 1:
        raise ValueError(
            f"Only {split} in-sample bars but measurement starts at bar {start} (strategy warmup). "
            "Load more history, use a finer interval, or shrink the windows."
        )

    result = SimpleBacktester(strategy=strategy_fn, initial_equity=1000.0, cost_model=resolved_costs.cost_model()).run(resolved_bars)
    interval_seconds = _infer_interval_seconds(resolved_bars)
    result = apply_funding(result, resolved_costs.funding_pct_per_day, interval_seconds=interval_seconds)
    metrics = {"in_sample": segment_metrics(result, resolved_bars, start, split, interval_seconds=interval_seconds)}
    if split < len(resolved_bars):
        metrics["holdout"] = segment_metrics(result, resolved_bars, split, len(resolved_bars), interval_seconds=interval_seconds)

    symbol = str(getattr(resolved_bars[0], "symbol", "")) if resolved_bars else ""
    return ResearchRun(
        label=name,
        params=resolved_params,
        symbol=symbol,
        interval_seconds=interval_seconds,
        bars=resolved_bars,
        result=result,
        measure_start=start,
        split_index=split,
        metrics=metrics,
    )


def segment_metrics(
    result: BacktestResult,
    bars: Sequence[Any],
    start: int,
    end: int,
    *,
    interval_seconds: int,
    chunks: int = 6,
) -> dict[str, float]:
    """Performance of bars[start:end] measured on the mark-to-market equity curve.

    Returns: return, annualised sharpe, max_drawdown, exposure (share of
    bars in a position), trades (closed in the segment), win_rate,
    avg_trade (net of costs), consistency (share of `chunks` equal
    sub-periods that made money), and buy-and-hold return/sharpe/drawdown
    over the same bars for reference.
    """
    start = max(start, 1)
    anchor = start - 1
    equity = np.asarray(result.mtm_equity_series[anchor:end], dtype=float)
    closes = series(bars[anchor:end], "close")
    periods_per_year = SECONDS_PER_YEAR / interval_seconds

    first_timestamp = result.timestamps[start]
    last_timestamp = result.timestamps[end - 1]
    trades = [trade for trade in result.trade_records if trade.timestamp is not None and first_timestamp <= trade.timestamp <= last_timestamp]
    boundaries = np.linspace(0, len(equity) - 1, min(chunks, len(equity) - 1) + 1).astype(int)
    chunk_returns = [equity[b] / equity[a] - 1.0 for a, b in zip(boundaries[:-1], boundaries[1:]) if b > a]

    return {
        "return": float(equity[-1] / equity[0] - 1.0),
        "sharpe": _annualised_sharpe(equity[1:] / equity[:-1] - 1.0, periods_per_year),
        "max_drawdown": float(np.max(1.0 - equity / np.maximum.accumulate(equity))),
        "exposure": float(np.mean(np.asarray(result.position_series[start:end]) != 0.0)),
        "trades": float(len(trades)),
        "win_rate": float(np.mean([trade.return_pct > 0.0 for trade in trades])) if trades else float("nan"),
        "avg_trade": float(np.mean([trade.return_pct for trade in trades])) if trades else float("nan"),
        "consistency": float(np.mean([value > 0.0 for value in chunk_returns])) if chunk_returns else float("nan"),
        "buy_hold_return": float(closes[-1] / closes[0] - 1.0),
        "buy_hold_sharpe": _annualised_sharpe(closes[1:] / closes[:-1] - 1.0, periods_per_year),
        "buy_hold_max_drawdown": float(np.max(1.0 - closes / np.maximum.accumulate(closes))),
        "bars": float(end - start),
    }


def compare(
    data: dict[str, Sequence[Any]],
    strategies: Iterable[str] | None = None,
    *,
    long_only: bool | Iterable[bool] = (False, True),
    costs: CostSettings | None = None,
    holdout_fraction: float = 0.3,
    measure_start: int | None = None,
    progress: bool = False,
) -> pd.DataFrame:
    """Run each strategy at its catalog defaults on every symbol in `data` (symbol -> bars)."""
    names = list(strategies) if strategies is not None else list(CATALOG)
    jobs = [(name, _build_from_name(name), dict(CATALOG[name].defaults) if name in CATALOG else {}) for name in names]
    return _run_jobs(data, jobs, long_only=long_only, costs=costs, holdout_fraction=holdout_fraction, measure_start=measure_start, progress=progress)


def sweep(
    data: dict[str, Sequence[Any]],
    strategy: str | Callable[..., StrategyFn],
    grid: dict[str, list[Any]] | None = None,
    *,
    long_only: bool | Iterable[bool] = (False, True),
    costs: CostSettings | None = None,
    holdout_fraction: float = 0.3,
    measure_start: int | None = None,
    progress: bool = False,
) -> pd.DataFrame:
    """Run every parameter combination of a grid on every symbol in `data` (symbol -> bars).

    Args:
        strategy: A registered strategy name (grid defaults to its catalog
            grid), or a factory function you wrote yourself, i.e. one that
            takes parameters and returns a StrategyFn. A factory needs an
            explicit `grid`.
        grid: {parameter: [values]}. Parameters not in the grid stay at
            their defaults. May include the `long_only` / `regime` wrappers.
        long_only: Which side modes to run: (False, True) runs both.
    """
    if isinstance(strategy, str):
        spec = CATALOG.get(strategy) or StrategySpec(name=strategy, family="uncatalogued", hypothesis="", defaults={})
        combos = spec.combos(grid)
        builder = _build_from_name(strategy)
        name = strategy
    else:
        if not grid:
            raise ValueError("Sweeping a custom factory needs an explicit grid")
        spec = StrategySpec(name=strategy.__name__, family="custom", hypothesis="", defaults={}, grid=grid)
        combos = spec.combos()
        builder = _build_from_factory(strategy)
        name = strategy.__name__
    jobs = [(name, builder, combo) for combo in combos]
    return _run_jobs(data, jobs, long_only=long_only, costs=costs, holdout_fraction=holdout_fraction, measure_start=measure_start, progress=progress)


def summarize(results: pd.DataFrame, metric: str = "sharpe") -> pd.DataFrame:
    """Condense sweep/compare rows into one robustness verdict per (strategy, interval, long_only).

    Every combo is first averaged across symbols (a parameter set has to
    work on more than one coin). Columns:

    - combos: parameter combinations tried.
    - share_positive_is: share of combos with IS metric > 0. High = the idea
      works across settings; low = only a few lucky settings.
    - median_is / median_ho: the typical combo, in-sample and holdout. The
      most honest single estimate of the strategy family.
    - best_params / best_is: the IS winner (swept parameters only; the full
      parameter set is in best_params_key) and its IS score.
    - best_neighbors_is: median IS score of the combos one grid step away
      from the winner. Close to best_is = plateau (good); far below =
      spike (overfit).
    - best_ho: the IS winner's holdout score, i.e. what picking it would
      actually have delivered.
    - rank_corr: rank correlation of IS vs HO across combos. Clearly
      positive = IS ranking carries over; ~0 or negative = picking by IS
      is picking noise. Needs a decent number of combos to mean anything.
    - buy_hold_is / buy_hold_ho: buy-and-hold on the same dates, averaged
      across symbols, the bar any strategy has to clear.
    """
    rows = []
    is_col, ho_col = f"is_{metric}", f"ho_{metric}"
    has_holdout = ho_col in results.columns and results[ho_col].notna().any()
    for (strategy, interval, long_only), group in results.groupby(["strategy", "interval", "long_only"], sort=False):
        param_cols = [column for column in group.columns if column.startswith("p_") and group[column].nunique(dropna=False) > 1]
        aggregations: dict[str, Any] = {"is_value": (is_col, "mean"), "is_trades": ("is_trades", "mean"), "is_return": ("is_return", "mean")}
        if has_holdout:
            aggregations.update({"ho_value": (ho_col, "mean"), "ho_return": ("ho_return", "mean")})
        per_combo = group.groupby("params", sort=False).agg(**aggregations)
        for column in param_cols:
            per_combo[column] = group.groupby("params", sort=False)[column].first()

        best_key = per_combo["is_value"].idxmax()
        best = per_combo.loc[best_key]
        neighbors = _neighbor_values(per_combo, best, param_cols, "is_value")
        symbols = group.drop_duplicates("symbol")
        row = {
            "strategy": strategy,
            "interval": interval,
            "long_only": long_only,
            "combos": len(per_combo),
            "symbols": group["symbol"].nunique(),
            "share_positive_is": float((per_combo["is_value"] > 0.0).mean()),
            "median_is": float(per_combo["is_value"].median()),
            "median_ho": float(per_combo["ho_value"].median()) if has_holdout else float("nan"),
            "best_params": json.dumps({column[2:]: _plain(best[column]) for column in param_cols}),
            "best_params_key": best_key,
            "best_is": float(best["is_value"]),
            "best_neighbors_is": float(np.median(neighbors)) if neighbors else float("nan"),
            "best_ho": float(best["ho_value"]) if has_holdout else float("nan"),
            "best_ho_return": float(best["ho_return"]) if has_holdout else float("nan"),
            "best_is_trades": float(best["is_trades"]),
            "rank_corr": _rank_correlation(per_combo["is_value"], per_combo["ho_value"]) if has_holdout and len(per_combo) >= 4 else float("nan"),
            "buy_hold_is": float(symbols[f"is_buy_hold_{metric}"].mean()) if f"is_buy_hold_{metric}" in symbols else float("nan"),
            "buy_hold_ho": float(symbols[f"ho_buy_hold_{metric}"].mean()) if has_holdout and f"ho_buy_hold_{metric}" in symbols else float("nan"),
        }
        rows.append(row)
    return pd.DataFrame(rows).sort_values("median_is", ascending=False, ignore_index=True)


def _run_jobs(
    data: dict[str, Sequence[Any]],
    jobs: list[tuple[str, Callable[[dict[str, Any]], StrategyFn], dict[str, Any]]],
    *,
    long_only: bool | Iterable[bool],
    costs: CostSettings | None,
    holdout_fraction: float,
    measure_start: int | None,
    progress: bool,
) -> pd.DataFrame:
    modes = [long_only] if isinstance(long_only, bool) else list(long_only)
    resolved_costs = costs or CostSettings()
    start = measure_start if measure_start is not None else max(warmup_bars(name, params) for name, _, params in jobs)
    rows: list[dict[str, Any]] = []
    for symbol, bars in data.items():
        resolved_bars = list(bars)
        started = time.perf_counter()
        for name, builder, params in jobs:
            for mode in modes:
                run = run_strategy(
                    resolved_bars,
                    builder({**params, "long_only": mode}),
                    params=params,
                    costs=resolved_costs,
                    holdout_fraction=holdout_fraction,
                    measure_start=start,
                    label=name,
                )
                rows.append(_result_row(run, symbol=symbol, long_only=mode))
        if progress:
            names = sorted({name for name, _, _ in jobs})
            print(f"  {symbol}: {len(jobs) * len(modes)} runs of {', '.join(names)} in {time.perf_counter() - started:.1f}s", flush=True)
    return pd.DataFrame(rows)


def _result_row(run: ResearchRun, *, symbol: str, long_only: bool) -> dict[str, Any]:
    params = {key: value for key, value in run.params.items() if key != "long_only"}
    timestamps = run.result.timestamps
    row: dict[str, Any] = {
        "strategy": run.label,
        "interval": interval_label(run.interval_seconds),
        "symbol": symbol,
        "long_only": long_only,
        "params": json.dumps(params, sort_keys=True, default=str),
        **{f"p_{key}": value for key, value in params.items()},
        "is_from": _iso(timestamps[run.measure_start]),
        "is_to": _iso(timestamps[run.split_index - 1]),
        "ho_from": _iso(timestamps[run.split_index]) if run.split_index < len(timestamps) else None,
        "ho_to": _iso(timestamps[-1]) if run.split_index < len(timestamps) else None,
    }
    for segment, prefix in (("in_sample", "is"), ("holdout", "ho")):
        for key in METRICS:
            row[f"{prefix}_{key}"] = run.metrics.get(segment, {}).get(key, float("nan"))
    return row


def _build_from_name(name: str) -> Callable[[dict[str, Any]], StrategyFn]:
    return lambda params: build_strategy(name, **params)


def _build_from_factory(factory: Callable[..., StrategyFn]) -> Callable[[dict[str, Any]], StrategyFn]:
    def build(params: dict[str, Any]) -> StrategyFn:
        resolved = dict(params)
        long_only = bool(resolved.pop("long_only", False))
        regime = resolved.pop("regime", None)
        strategy = factory(**resolved)
        if regime is not None:
            strategy = make_regime_gated(strategy, required_regime=regime)
        return make_long_only(strategy) if long_only else strategy

    return build


def _neighbor_values(per_combo: pd.DataFrame, best: pd.Series, param_cols: list[str], value_col: str) -> list[float]:
    """Values of the combos exactly one grid step from `best` along a single parameter."""
    values: list[float] = []
    for column in param_cols:
        levels = sorted(per_combo[column].dropna().unique().tolist(), key=_sort_key)
        if best[column] not in levels:
            continue
        position = levels.index(best[column])
        for neighbor in (position - 1, position + 1):
            if not 0 <= neighbor < len(levels):
                continue
            mask = per_combo[column] == levels[neighbor]
            for other in param_cols:
                if other != column:
                    mask &= per_combo[other].isna() if pd.isna(best[other]) else per_combo[other] == best[other]
            values.extend(per_combo.loc[mask, value_col].tolist())
    return values


def _rank_correlation(first: pd.Series, second: pd.Series) -> float:
    valid = first.notna() & second.notna()
    if valid.sum() < 4:
        return float("nan")
    return float(first[valid].rank().corr(second[valid].rank()))


def _annualised_sharpe(returns: np.ndarray, periods_per_year: float) -> float:
    if returns.size < 2:
        return 0.0
    deviation = float(np.std(returns, ddof=1))
    if deviation <= 0.0:
        return 0.0
    return float(np.mean(returns) / deviation * np.sqrt(periods_per_year))


def _infer_interval_seconds(bars: Sequence[Any]) -> int:
    declared = getattr(bars[0], "interval_seconds", None) if bars else None
    if declared:
        return int(declared)
    gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(bars[:-1], bars[1:])]
    return int(np.median(gaps)) if gaps else 86400


def _plain(value: Any) -> Any:
    value = value.item() if isinstance(value, np.generic) else value
    # Parameter columns shared across strategies get upcast to float; show 40, not 40.0.
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _sort_key(value: Any) -> tuple[int, Any]:
    return (0, value) if isinstance(value, (int, float)) else (1, str(value))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
