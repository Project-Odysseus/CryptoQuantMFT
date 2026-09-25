"""The strategies the research tools know about, with their hypotheses and sweep grids.

Every entry points at a factory registered in `StrategyRegistry` (the
runtime runs the same factories with the same signal semantics) and adds
what research needs on top:

- `hypothesis`: one line on *why* this should make money. If you can't
  write one, the backtest result is just curve-fitting.
- `defaults`: the parameters `compare` and `backtest` use when you don't
  give any.
- `grid`: the values a sensitivity sweep tries. Keep it to the two
  parameters that matter most so it can be drawn as a heatmap; hold the
  rest at their defaults.
- `constraint`: optional filter for combos that make no sense (e.g. a fast
  average longer than the slow one).

Two extra "wrapper" parameters work with any strategy and can go in a grid
too: ``long_only`` (True/False, suppresses shorts, which is what spot live
trading does anyway) and ``regime`` (None, "trending" or "ranging", gates
the signal with `classify_regime`).
"""

from __future__ import annotations

import inspect
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.backtest.runner import StrategyRegistry
from src.backtest.simple_backtest import StrategyFn
from src.backtest.strategies import make_long_only, make_regime_gated

WRAPPER_PARAMS = ("long_only", "regime")


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Research metadata for one registered strategy."""

    name: str
    family: str
    hypothesis: str
    defaults: dict[str, Any]
    grid: dict[str, list[Any]] = field(default_factory=dict)
    constraint: Callable[[dict[str, Any]], bool] | None = None
    warmup: Callable[[dict[str, Any]], int] | None = None

    def combos(self, grid: dict[str, list[Any]] | None = None) -> list[dict[str, Any]]:
        """Expand a grid (this spec's by default) into full parameter dicts, defaults filled in."""
        resolved_grid = self.grid if grid is None else grid
        if not resolved_grid:
            return [dict(self.defaults)]
        keys = list(resolved_grid)
        combos = []
        for values in itertools.product(*(resolved_grid[key] for key in keys)):
            params = {**self.defaults, **dict(zip(keys, values))}
            if self.constraint is None or self.constraint(params):
                combos.append(params)
        return combos

    def warmup_bars(self, params: dict[str, Any] | None = None) -> int:
        """Bars of history the strategy needs before it can signal."""
        merged = {**self.defaults, **(params or {})}
        if self.warmup is not None:
            return self.warmup(merged)
        windows = [value for value in merged.values() if isinstance(value, int) and not isinstance(value, bool)]
        return (max(windows) if windows else 1) + 2


CATALOG: dict[str, StrategySpec] = {
    spec.name: spec
    for spec in [
        StrategySpec(
            name="moving_average_crossover",
            family="trend",
            hypothesis="A fast average above a slow one marks an uptrend that tends to persist.",
            defaults={"short_window": 4, "long_window": 24},
            grid={"short_window": [2, 4, 8, 12], "long_window": [16, 24, 48, 96]},
            constraint=lambda p: p["short_window"] < p["long_window"],
        ),
        StrategySpec(
            name="momentum_breakout",
            family="momentum",
            hypothesis="A large N-bar return tends to continue (time-series momentum).",
            defaults={"lookback": 6, "threshold": 0.01},
            grid={"lookback": [3, 6, 12, 24, 48], "threshold": [0.0, 0.01, 0.02, 0.05]},
        ),
        StrategySpec(
            name="signal_trend",
            family="momentum",
            hypothesis="Follow the last bar's direction (momentum_breakout with lookback=1); a fast-reacting baseline.",
            defaults={"lookback": 1, "threshold": 0.001},
        ),
        StrategySpec(
            name="volume_confirmed_momentum",
            family="momentum",
            hypothesis="Momentum backed by above-average volume is more likely to be real participation than noise.",
            defaults={"lookback": 6, "threshold": 0.01, "volume_window": 12, "volume_multiplier": 1.5},
            grid={"lookback": [3, 6, 12], "volume_multiplier": [1.0, 1.5, 2.0]},
        ),
        StrategySpec(
            name="volume_confirmed_momentum_biased",
            family="momentum",
            hypothesis="Same as volume_confirmed_momentum, but shorts need a 2x bigger move and 1.5x more volume.",
            defaults={"lookback": 6, "threshold": 0.01, "volume_window": 12, "volume_multiplier": 1.5},
        ),
        StrategySpec(
            name="band_reversion",
            family="mean_reversion",
            hypothesis="Closes far outside a rolling band are overreactions that snap back toward the mean.",
            defaults={"window": 20, "num_std": 2.0},
            grid={"window": [10, 20, 40], "num_std": [1.0, 1.5, 2.0, 2.5]},
        ),
        StrategySpec(
            name="donchian_breakout",
            family="breakout",
            hypothesis="New N-bar highs/lows start trends; ride them until an opposite, shorter-channel break.",
            defaults={"entry_window": 20, "exit_window": 10},
            grid={"entry_window": [10, 20, 40, 60], "exit_window": [5, 10, 20]},
            constraint=lambda p: p["exit_window"] <= p["entry_window"],
        ),
        StrategySpec(
            name="keltner_breakout",
            family="volatility_breakout",
            hypothesis="A move of several ATRs beyond the average is a regime change, not noise, scaled to each market's volatility.",
            defaults={"window": 20, "atr_multiplier": 2.0},
            grid={"window": [10, 20, 40], "atr_multiplier": [1.0, 1.5, 2.0, 3.0]},
        ),
        StrategySpec(
            name="trend_tstat",
            family="trend",
            hypothesis="Only trends that are steep relative to their own noise persist; choppy drifts don't.",
            defaults={"window": 24, "strength_threshold": 1.5},
            grid={"window": [12, 24, 48, 96], "strength_threshold": [0.5, 1.0, 1.5, 2.0]},
        ),
        StrategySpec(
            name="volatility_squeeze",
            family="volatility_breakout",
            hypothesis="Volatility clusters: unusually quiet stretches end in sharp moves, so trade the first break.",
            defaults={"window": 20, "num_std": 2.0, "squeeze_lookback": 60, "squeeze_quantile": 0.2, "squeeze_memory": 5},
            grid={"window": [10, 20, 30], "squeeze_quantile": [0.1, 0.2, 0.35]},
            warmup=lambda p: p["window"] + p["squeeze_lookback"] + 2,
        ),
        StrategySpec(
            name="rsi_reversion",
            family="mean_reversion",
            hypothesis="Short-term oversold/overbought extremes revert once selling/buying pressure exhausts.",
            defaults={"rsi_window": 14, "oversold": 30.0, "exit_level": 50.0},
            grid={"rsi_window": [2, 3, 5, 14], "oversold": [10.0, 20.0, 30.0]},
        ),
        StrategySpec(
            name="trend_pullback",
            family="hybrid",
            hypothesis="In an uptrend, short-term dips are buying opportunities rather than reversals.",
            defaults={"trend_window": 50, "rsi_window": 3, "entry_rsi": 20.0, "exit_rsi": 70.0},
            grid={"trend_window": [20, 50, 100], "entry_rsi": [10.0, 20.0, 30.0]},
        ),
    ]
}


def get_spec(name: str) -> StrategySpec:
    """Return the catalog entry for `name`, with a helpful error listing what exists."""
    if name not in CATALOG:
        raise KeyError(f"'{name}' is not in the research catalog. Known: {', '.join(sorted(CATALOG))}")
    return CATALOG[name]


def build_strategy(name: str, **params: Any) -> StrategyFn:
    """Build a strategy by name from catalog defaults plus overrides, applying wrapper params.

    Unlike `resolve_strategy` (which silently falls back to defaults if a
    parameter is wrong), this raises on unknown parameter names, so a typo
    in a sweep can't quietly test the default settings instead.
    """
    long_only = bool(params.pop("long_only", False))
    regime = params.pop("regime", None)
    defaults = CATALOG[name].defaults if name in CATALOG else {}
    merged = {**defaults, **params}

    factory = StrategyRegistry().get(name)
    accepted = set(inspect.signature(factory).parameters)
    unknown = sorted(set(merged) - accepted)
    if unknown:
        raise TypeError(f"{name} does not take {unknown}; it accepts {sorted(accepted)} plus {list(WRAPPER_PARAMS)}")

    strategy = factory(**merged)
    if regime is not None:
        strategy = make_regime_gated(strategy, required_regime=regime)
    if long_only:
        strategy = make_long_only(strategy)
    return strategy
