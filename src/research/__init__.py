"""Strategy research toolkit: load data, backtest, sweep parameters, judge robustness.

Typical use (see notebooks/strategy_research.ipynb and docs/research_guide.md):

    from src.research import load_bars, run_strategy, sweep, summarize, plot_heatmap
    data = {s: load_bars(s, "4h") for s in ["BTC/EUR", "ETH/EUR", "SOL/EUR"]}
    results = sweep(data, "donchian_breakout")
    summarize(results)
"""

from __future__ import annotations

import pandas as pd

from src.research.catalog import CATALOG, StrategySpec, build_strategy, get_spec
from src.research.execution import FillModel, simulate_fills
from src.research.engine import CostSettings, ResearchRun, compare, load_bars, run_strategy, segment_metrics, summarize, sweep
from src.research.plots import plot_heatmap, plot_run, sweep_axes

__all__ = [
    "CATALOG",
    "CostSettings",
    "FillModel",
    "ResearchRun",
    "StrategySpec",
    "build_strategy",
    "catalog_table",
    "compare",
    "get_spec",
    "load_bars",
    "plot_heatmap",
    "plot_run",
    "run_strategy",
    "segment_metrics",
    "simulate_fills",
    "summarize",
    "sweep",
    "sweep_axes",
]


def catalog_table() -> pd.DataFrame:
    """The catalog as a table: name, family, hypothesis, defaults and grid for each strategy."""
    return pd.DataFrame(
        [
            {
                "strategy": spec.name,
                "family": spec.family,
                "hypothesis": spec.hypothesis,
                "defaults": spec.defaults,
                "grid": spec.grid or "(defaults only)",
                "combos": len(spec.combos()),
            }
            for spec in CATALOG.values()
        ]
    )
