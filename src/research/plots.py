"""Charts for research results: sweep heatmaps and single-run equity curves."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.figure import Figure

from src.research.catalog import CATALOG
from src.research.engine import ResearchRun, interval_label

_DIVERGING = "RdBu"
_STRATEGY_COLOR = "#2a6fdb"
_BENCHMARK_COLOR = "#8a8f98"


def sweep_axes(results: pd.DataFrame, strategy: str) -> list[str]:
    """The parameters that actually vary in a strategy's sweep, in grid order."""
    subset = results[results["strategy"] == strategy]
    return [column[2:] for column in subset.columns if column.startswith("p_") and subset[column].nunique(dropna=False) > 1]


def plot_heatmap(
    results: pd.DataFrame,
    strategy: str,
    *,
    x: str | None = None,
    y: str | None = None,
    metric: str = "sharpe",
    interval: str | None = None,
    long_only: bool = False,
    path: str | Path | None = None,
) -> Figure:
    """Draw in-sample and holdout heatmaps of `metric` over two swept parameters, averaged over symbols.

    What to look for: a broad region of the same colour in the in-sample
    panel (a plateau) that is still the same colour in the holdout panel.
    A single bright cell surrounded by the opposite colour is a spike -
    luck, not a parameter setting.
    """
    subset = results[(results["strategy"] == strategy) & (results["long_only"] == long_only)]
    if interval is not None:
        subset = subset[subset["interval"] == interval]
    if subset.empty:
        raise ValueError(f"No rows for {strategy} (long_only={long_only}, interval={interval})")
    axes_names = sweep_axes(subset, strategy)
    x = x or (axes_names[0] if axes_names else None)
    y = y or (axes_names[1] if len(axes_names) > 1 else None)
    if x is None:
        raise ValueError(f"{strategy} has no swept parameter to plot")

    panels = [("In-sample", f"is_{metric}")]
    if f"ho_{metric}" in subset and subset[f"ho_{metric}"].notna().any():
        panels.append(("Holdout", f"ho_{metric}"))
    tables = [_pivot(subset, x, y, column) for _, column in panels]
    limit = max(float(np.nanmax(np.abs(table.to_numpy(dtype=float)))) for table in tables) or 1.0

    fig, axes = plt.subplots(1, len(panels), figsize=(5.4 * len(panels), 4.2), constrained_layout=True, squeeze=False)
    image = None
    for ax, (title, _), table in zip(axes[0], panels, tables):
        values = table.to_numpy(dtype=float)
        image = ax.imshow(values, cmap=_DIVERGING, vmin=-limit, vmax=limit, aspect="auto", origin="lower")
        for row_index in range(values.shape[0]):
            for column_index in range(values.shape[1]):
                value = values[row_index, column_index]
                if np.isfinite(value):
                    ax.text(column_index, row_index, f"{value:.2f}", ha="center", va="center", fontsize=9, color="black" if abs(value) < 0.6 * limit else "white")
        ax.set_xticks(range(len(table.columns)), [_format_level(level) for level in table.columns])
        ax.set_yticks(range(len(table.index)), [_format_level(level) for level in table.index])
        ax.set_xlabel(x)
        ax.set_ylabel(y or "")
        ax.set_title(title)
    fig.colorbar(image, ax=axes[0].tolist(), shrink=0.85, label=metric)
    intervals = ", ".join(sorted(subset["interval"].unique()))
    side = "long-only" if long_only else "long/short"
    fig.suptitle(f"{strategy} - {metric}, mean of {subset['symbol'].nunique()} symbols ({intervals}, {side})")
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=110)
        plt.close(fig)
    return fig


def plot_run(run: ResearchRun, *, path: str | Path | None = None) -> Figure:
    """Plot a run's mark-to-market equity against buy-and-hold, with the holdout period shaded."""
    start = run.measure_start - 1
    timestamps = run.result.timestamps[start:]
    equity = np.asarray(run.result.mtm_equity_series[start:], dtype=float)
    closes = np.asarray([bar.close for bar in run.bars[start:]], dtype=float)
    positions = np.asarray(run.result.position_series[start:], dtype=float)

    fig, (equity_ax, position_ax) = plt.subplots(2, 1, figsize=(10, 5.2), sharex=True, gridspec_kw={"height_ratios": [4, 1]}, constrained_layout=True)
    equity_ax.plot(timestamps, equity / equity[0], color=_STRATEGY_COLOR, linewidth=1.8, label=run.label)
    equity_ax.plot(timestamps, closes / closes[0], color=_BENCHMARK_COLOR, linewidth=1.4, label="buy & hold")
    equity_ax.axhline(1.0, color=_BENCHMARK_COLOR, linewidth=0.6)
    if run.split_index < len(run.result.timestamps):
        split_time = run.result.timestamps[run.split_index]
        for ax in (equity_ax, position_ax):
            ax.axvspan(split_time, timestamps[-1], color="#f2e6c9", alpha=0.55, linewidth=0)
        equity_ax.text(split_time, equity_ax.get_ylim()[1], " holdout", va="top", fontsize=9)
    equity_ax.set_ylabel("growth of 1")
    equity_ax.legend(loc="upper left", frameon=False)
    in_sample = run.metrics["in_sample"]
    summary = f"IS: return {in_sample['return']:+.1%}, sharpe {in_sample['sharpe']:.2f}, trades {int(in_sample['trades'])}"
    if "holdout" in run.metrics:
        holdout = run.metrics["holdout"]
        summary += f"   |   HO: return {holdout['return']:+.1%}, sharpe {holdout['sharpe']:.2f}, trades {int(holdout['trades'])}"
    shown_params = {**(CATALOG[run.label].defaults if run.label in CATALOG else {}), **run.params}
    equity_ax.set_title(f"{run.label}{_format_params(shown_params)} on {run.symbol} ({interval_label(run.interval_seconds)})\n{summary}", fontsize=10)
    position_ax.step(timestamps, positions, where="post", color=_STRATEGY_COLOR, linewidth=1.0)
    position_ax.set_ylabel("position")
    position_ax.set_yticks([-1, 0, 1])
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=110)
        plt.close(fig)
    return fig


def _pivot(subset: pd.DataFrame, x: str, y: str | None, column: str) -> pd.DataFrame:
    frame = subset.copy()
    frame["_x"] = frame[f"p_{x}"].astype(object).where(frame[f"p_{x}"].notna(), "None")
    if y is None:
        return frame.groupby("_x", sort=False)[column].mean().to_frame().T.rename(index={column: ""}).pipe(_sorted_columns)
    frame["_y"] = frame[f"p_{y}"].astype(object).where(frame[f"p_{y}"].notna(), "None")
    table = frame.pivot_table(index="_y", columns="_x", values=column, aggfunc="mean")
    return _sorted_columns(table.reindex(sorted(table.index, key=_level_key)))


def _sorted_columns(table: pd.DataFrame) -> pd.DataFrame:
    return table[sorted(table.columns, key=_level_key)]


def _level_key(value: Any) -> tuple[int, Any]:
    return (0, value) if isinstance(value, (int, float, np.number)) else (1, str(value))


def _format_level(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_params(params: dict[str, Any]) -> str:
    shown = {key: value for key, value in params.items() if key != "long_only" or value}
    return "(" + ", ".join(f"{key}={value}" for key, value in shown.items()) + ")" if shown else ""
