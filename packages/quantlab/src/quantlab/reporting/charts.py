"""The lab's charts: one look for notebooks and reports (plan 5.4).

Every function draws on an `ax` it is given (or a new figure) and returns the axes, so charts compose into grids
and work the same inline in a notebook and in an exported report (`to_png` / `to_base64`).

The module never selects a matplotlib backend. Picking "Agg" at import time is what once stopped notebook plots
from showing; scripts that need a headless backend set it themselves (or set MPLBACKEND=Agg). A test enforces this
for the whole package.

    from quantlab.reporting import charts
    charts.equity({"overlay": result.pnl, "static": static.pnl}, benchmark=returns["NDX"])
    charts.drawdown(result.pnl); charts.rolling_sharpe(result.pnl); charts.attribution(result)
    charts.regime_shading(ax, labels); charts.heatmap(table); charts.parameter_surface(plateau["table"], "fast", "slow")
"""

from __future__ import annotations

import base64
import io
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

PALETTE = ["#1f4e79", "#c55a11", "#548235", "#7030a0", "#bf9000", "#2e75b6", "#a5a5a5", "#843c0c"]
STATE_COLOURS = {"TREND": "#548235", "RANGE": "#c55a11", "TRANSITION": "#a5a5a5", "CALM": "#2e75b6", "NORMAL": "#a5a5a5", "STRESSED": "#c00000",
                 "VOLATILE": "#c00000", "bull": "#548235", "bear": "#c00000", "calm": "#2e75b6", "volatile": "#c00000"}

STYLE = {"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25, "axes.titlesize": 11, "axes.titleweight": "bold",
         "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8, "legend.frameon": False, "figure.dpi": 100,
         "axes.prop_cycle": None}


def style() -> dict[str, Any]:
    """rcParams for the lab's look (use `with plt.rc_context(charts.style()):`, or `apply_style()` once in a notebook)."""
    from matplotlib import cycler

    return {**{key: value for key, value in STYLE.items() if value is not None}, "axes.prop_cycle": cycler(color=PALETTE)}


def apply_style() -> None:
    import matplotlib.pyplot as plt

    plt.rcParams.update(style())


def _ax(ax: Any = None, size: tuple[float, float] = (10, 4)) -> Any:
    if ax is not None:
        return ax
    import matplotlib.pyplot as plt

    with plt.rc_context(style()):
        _figure, ax = plt.subplots(figsize=size)
    return ax


def _series(item: Any) -> pd.Series:
    return item.pnl if hasattr(item, "pnl") else item


def equity(curves: Mapping[str, Any] | pd.Series, *, benchmark: pd.Series | None = None, log: bool = True, ax: Any = None, title: str = "Growth of 1") -> Any:
    """Growth of 1 for one or several return series (or engine results), optionally against a benchmark."""
    ax = _ax(ax)
    named = {"strategy": curves} if isinstance(curves, pd.Series) else dict(curves)
    for index, (name, item) in enumerate(named.items()):
        series = _series(item).fillna(0.0)
        ax.plot(series.index, (1.0 + series).cumprod().to_numpy(), label=name, color=PALETTE[index % len(PALETTE)], linewidth=1.6 if index == 0 else 1.0)
    if benchmark is not None:
        first = _series(next(iter(named.values()))).index
        bench = benchmark.reindex(first).fillna(0.0)
        ax.plot(bench.index, (1.0 + bench).cumprod().to_numpy(), label="benchmark", color="black", linewidth=0.8, linestyle=":")
    if log:
        ax.set_yscale("log")
    ax.set_title(title)
    ax.legend()
    return ax


def drawdown(returns: Any, *, ax: Any = None, title: str = "Drawdown") -> Any:
    """Underwater curve in percent."""
    ax = _ax(ax, (10, 2.5))
    series = _series(returns).fillna(0.0)
    growth = (1.0 + series).cumprod()
    under = (growth / growth.cummax() - 1.0) * 100.0
    ax.fill_between(under.index, under.to_numpy(), 0.0, color=PALETTE[1], alpha=0.5, linewidth=0)
    ax.set_ylabel("%")
    ax.set_title(f"{title} (worst {under.min():.1f}%)")
    return ax


def rolling_sharpe(returns: Any, *, window: int = 252, periods: float = 252.0, ax: Any = None) -> Any:
    """Rolling Sharpe with a band of about two standard errors around zero."""
    ax = _ax(ax, (10, 2.5))
    series = _series(returns)
    rolling = series.rolling(window).mean() / series.rolling(window).std() * math.sqrt(periods)
    band = 2.0 * math.sqrt(periods / window)
    ax.plot(rolling.index, rolling.to_numpy(), color=PALETTE[0], linewidth=1.0)
    ax.axhspan(-band, band, color="grey", alpha=0.12, linewidth=0)
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_title(f"Rolling {window}-bar Sharpe (grey: within 2 standard errors of 0)")
    return ax


def attribution(result: Any, *, by: str = "part", ax: Any = None, top: int = 8) -> Any:
    """Cumulative P&L (% of capital) by target part, or by instrument, with costs."""
    ax = _ax(ax)
    if by == "part" and result.parts_pnl:
        pieces = {name: frame.sum(axis=1) for name, frame in result.parts_pnl.items()}
    elif by == "part":
        pieces = {"gross": result.gross.sum(axis=1)}
    else:
        frame = result.gross - result.costs
        order = frame.sum().abs().sort_values(ascending=False).index[:top]
        pieces = {str(name): frame[name] for name in order}
    for index, (name, series) in enumerate(pieces.items()):
        ax.plot(series.index, series.cumsum().to_numpy() * 100.0, label=name, color=PALETTE[index % len(PALETTE)], linewidth=1.0)
    if by == "part":
        costs = result.costs.sum(axis=1)
        ax.plot(costs.index, -costs.cumsum().to_numpy() * 100.0, label="costs", color="black", linestyle=":", linewidth=1.0)
    ax.axhline(0.0, color="black", linewidth=0.6)
    ax.set_ylabel("% of capital")
    ax.set_title(f"Cumulative P&L by {'part' if by == 'part' else 'instrument'}")
    ax.legend()
    return ax


def regime_shading(ax: Any, labels: pd.Series, *, colours: Mapping[str, str] | None = None, alpha: float = 0.12) -> Any:
    """Shade the background of a time axis by state (one span per spell); adds a legend entry per state."""
    from matplotlib.patches import Patch

    colours = {**STATE_COLOURS, **dict(colours or {})}
    valid = labels.dropna()
    if valid.empty:
        return ax
    change = valid.ne(valid.shift())
    starts = list(valid.index[change.to_numpy()])
    ends = starts[1:] + [valid.index[-1]]
    seen = {}
    for start, end in zip(starts, ends):
        state = valid[start]
        colour = colours.get(state, PALETTE[len(seen) % len(PALETTE)])
        seen.setdefault(state, colour)
        ax.axvspan(start, end, color=colour, alpha=alpha, linewidth=0)
    handles, names = ax.get_legend_handles_labels()
    ax.legend(handles + [Patch(color=colour, alpha=0.4) for colour in seen.values()], names + list(seen))
    return ax


def heatmap(table: pd.DataFrame, *, ax: Any = None, title: str = "", fmt: str = "{:.2f}", centre: float | None = 0.0, cmap: str = "RdYlGn") -> Any:
    """A labelled heat map of a table (e.g. Sharpe by year and month, or correlations)."""
    ax = _ax(ax, (max(4.0, 0.7 * table.shape[1] + 2), max(3.0, 0.4 * table.shape[0] + 1)))
    values = table.to_numpy(dtype=float)
    if centre is not None and np.isfinite(values).any():
        reach = np.nanmax(np.abs(values - centre))
        image = ax.imshow(values, cmap=cmap, vmin=centre - reach, vmax=centre + reach, aspect="auto")
    else:
        image = ax.imshow(values, cmap=cmap, aspect="auto")
    ax.set_xticks(range(table.shape[1]), [str(c) for c in table.columns], rotation=45, ha="right")
    ax.set_yticks(range(table.shape[0]), [str(i) for i in table.index])
    ax.grid(False)
    if table.size <= 400:
        for row in range(table.shape[0]):
            for column in range(table.shape[1]):
                if np.isfinite(values[row, column]):
                    ax.text(column, row, fmt.format(values[row, column]), ha="center", va="center", fontsize=7)
    ax.figure.colorbar(image, ax=ax, fraction=0.04)
    ax.set_title(title)
    return ax


def parameter_surface(table: pd.DataFrame, x: str, y: str, value: str = "Sharpe", *, ax: Any = None, chosen: Mapping[str, Any] | None = None) -> Any:
    """A parameter grid's metric as a heat map, with the chosen point outlined (from robustness.parameter_plateau)."""
    pivot = table.pivot(index=y, columns=x, values=value)
    ax = heatmap(pivot, ax=ax, title=f"{value} over {x} and {y}")
    if chosen is not None:
        from matplotlib.patches import Rectangle

        column, row = list(pivot.columns).index(chosen[x]), list(pivot.index).index(chosen[y])
        ax.add_patch(Rectangle((column - 0.5, row - 0.5), 1, 1, fill=False, edgecolor="black", linewidth=2))
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    return ax


def monthly_returns(returns: Any, *, ax: Any = None) -> Any:
    """Return by year and month, in percent."""
    series = _series(returns).dropna()
    table = series.groupby([series.index.year, series.index.month]).apply(lambda r: (1.0 + r).prod() - 1.0).unstack() * 100.0
    table.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][: table.shape[1]]
    return heatmap(table, ax=ax, title="Monthly return, %", fmt="{:.1f}")


def tearsheet(returns: Any, *, benchmark: pd.Series | None = None, labels: pd.Series | None = None, title: str = "") -> Any:
    """Equity (with regime shading), drawdown and rolling Sharpe stacked in one figure."""
    import matplotlib.pyplot as plt

    with plt.rc_context(style()):
        figure, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
        equity({title or "strategy": returns}, benchmark=benchmark, ax=axes[0])
        if labels is not None:
            regime_shading(axes[0], labels)
        drawdown(returns, ax=axes[1])
        rolling_sharpe(returns, ax=axes[2])
        if title:
            figure.suptitle(title, fontweight="bold")
        figure.tight_layout()
    return figure


def to_png(figure: Any, path: Any, *, dpi: int = 130) -> None:
    figure.savefig(path, dpi=dpi, bbox_inches="tight")


def to_base64(figure: Any, *, dpi: int = 110, close: bool = True) -> str:
    """A PNG of the figure as base64, for embedding in a self-contained HTML report."""
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
    if close:
        import matplotlib.pyplot as plt

        plt.close(figure)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
