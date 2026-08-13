"""Helpers for running configurable backtests from a simple strategy registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Sequence

from src.backtest.costs import CostModel, build_default_cost_model
from src.backtest.simple_backtest import BacktestResult, SimpleBacktester, moving_average_crossover_strategy


@dataclass(slots=True)
class BacktestConfig:
    """Simple configuration for a backtest run."""

    strategy_name: str = "moving_average_crossover"
    initial_equity: float = 100.0
    threshold: float = 0.02
    exchange: str = "mock"
    include_costs: bool = False
    taker_fee: float = 0.0
    maker_fee: float = 0.0
    fx_spread_bps: float = 0.0


@dataclass(slots=True)
class BacktestComparison:
    """Compare a baseline backtest against a cost-adjusted backtest."""

    baseline: BacktestResult
    with_costs: BacktestResult
    equity_delta: float
    return_delta: float


class StrategyRegistry:
    """Resolve a strategy name to a strategy callable."""

    def __init__(self) -> None:
        self._strategies: dict[str, Any] = {
            "moving_average_crossover": moving_average_crossover_strategy,
        }

    def register(self, name: str, strategy: Any) -> None:
        """Register a new named strategy."""
        self._strategies[name] = strategy

    def get(self, name: str) -> Any:
        """Return a strategy callable for a given name."""
        if name not in self._strategies:
            raise KeyError(f"Unknown strategy: {name}")
        return self._strategies[name]


def build_cost_model(config: BacktestConfig) -> CostModel:
    """Create a cost model for the configured backtest."""
    if not config.include_costs:
        return build_default_cost_model(config.exchange)

    return CostModel(
        exchange=config.exchange,
        maker_fee=config.maker_fee,
        taker_fee=config.taker_fee,
        fx_spread_bps=config.fx_spread_bps,
    )


def run_backtest(bars: Sequence[Any], config: BacktestConfig | None = None, registry: StrategyRegistry | None = None) -> BacktestResult:
    """Run a strategy over bars using the supplied configuration."""
    resolved_config = config or BacktestConfig()
    resolved_registry = registry or StrategyRegistry()

    strategy = _resolve_strategy(resolved_config, resolved_registry)

    cost_model = None
    if resolved_config.include_costs:
        cost_model = build_cost_model(resolved_config)

    backtester = SimpleBacktester(strategy=strategy, initial_equity=resolved_config.initial_equity, threshold=resolved_config.threshold, cost_model=cost_model)
    return backtester.run(list(bars))


def compare_backtests(bars: Sequence[Any], config: BacktestConfig | None = None, registry: StrategyRegistry | None = None) -> BacktestComparison:
    """Compare a baseline run against the same strategy with costs enabled."""
    resolved_config = config or BacktestConfig()
    baseline_config = replace(resolved_config, include_costs=False)
    cost_config = replace(resolved_config, include_costs=True)

    baseline = run_backtest(bars, baseline_config, registry)
    with_costs = run_backtest(bars, cost_config, registry)

    return BacktestComparison(
        baseline=baseline,
        with_costs=with_costs,
        equity_delta=with_costs.final_equity - baseline.final_equity,
        return_delta=with_costs.total_return - baseline.total_return,
    )


def _resolve_strategy(config: BacktestConfig, registry: StrategyRegistry) -> Any:
    """Resolve a configured strategy name into a callable."""
    strategy_factory = registry.get(config.strategy_name)
    strategy = strategy_factory() if callable(strategy_factory) and not isinstance(strategy_factory, str) else None

    if strategy is None:
        raise TypeError("Strategy registry entries must resolve to a callable")

    return strategy
