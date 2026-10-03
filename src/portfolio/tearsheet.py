"""A one-page tearsheet for a book: a static HTML file with the charts and tables a review needs.

The same page is built from a research backtest (`scripts/research/portfolio_backtest.py` writes it next to its
CSVs) and from a running book's SQLite snapshots (`main.py --portfolio PATH --tearsheet`), so the two can be laid
side by side: equity against the benchmark, the underwater curve, monthly returns, rolling volatility, Sharpe and
beta, the exposure the book carried, and how alike its strategies are.

Charts are matplotlib PNGs embedded in the page, so the file opens anywhere and can be sent as one attachment.
Every chart has one y-axis; where two measures have different scales they get a panel each. Each heatmap prints
its numbers in the cells, and the summary table carries the headline figures as text.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.figure import Figure

from src.portfolio.risk_model import average_correlation, effective_bets
from src.research.benchmark import benchmark_metrics, rolling_beta
from src.utils.report import AXIS, DIVERGING, INK, MUTED, SERIES, html_page, image
from src.utils.report import legend as _legend
from src.utils.report import png as _png
from src.utils.report import style_axis as _style

DAYS_PER_YEAR = 365.0
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def summary_table(returns: pd.Series, benchmark: pd.Series | None, benchmark_name: str) -> pd.DataFrame:
    """Headline figures for the book (and the benchmark's buy-and-hold) from daily returns."""

    def column(values: pd.Series) -> dict[str, float]:
        if len(values) < 2:
            return {}
        equity = (1.0 + values).cumprod()
        years = len(values) / DAYS_PER_YEAR
        std = float(values.std())
        tail = float(np.quantile(values, 0.05))
        return {
            "days": float(len(values)), "total return": float(equity.iloc[-1] - 1.0),
            "CAGR": float(equity.iloc[-1] ** (1.0 / years) - 1.0) if equity.iloc[-1] > 0 else -1.0,
            "volatility (annual)": std * np.sqrt(DAYS_PER_YEAR), "Sharpe": float(values.mean() / std * np.sqrt(DAYS_PER_YEAR)) if std > 0 else 0.0,
            "max drawdown": float((1.0 - equity / equity.cummax()).max()), "worst day": float(-values.min()),
            "1-in-20 day loss (VaR 95%)": -tail, "average loss beyond it (ES 95%)": float(-values[values <= tail].mean()),
            "share of losing months": float(((1.0 + values).resample("ME").prod() < 1.0).mean()),
        }

    table = {"book": column(returns)}
    if benchmark is not None and len(benchmark) > 1:
        table[f"buy-and-hold {benchmark_name}"] = column(benchmark)
        versus = benchmark_metrics(returns, benchmark)
        for key, label in (("beta", "beta to the benchmark"), ("alpha_ann", "alpha (annual)"), ("correlation", "correlation with the benchmark"), ("down_capture", "down capture")):
            if key in versus:
                table["book"][label] = float(versus[key])
    return pd.DataFrame(table)


def _format(label: str, value: float) -> str:
    if value is None or not np.isfinite(value):
        return ""
    if label == "days":
        return f"{value:.0f}"
    if label in ("Sharpe", "beta to the benchmark", "correlation with the benchmark", "down capture"):
        return f"{value:.2f}"
    return f"{value:.1%}"


def monthly_returns(returns: pd.Series) -> pd.DataFrame:
    """Year x month table of compounded returns."""
    monthly = (1.0 + returns).resample("ME").prod() - 1.0
    frame = pd.DataFrame({"year": monthly.index.year, "month": monthly.index.month, "value": monthly.to_numpy()})
    return frame.pivot(index="year", columns="month", values="value").reindex(columns=range(1, 13))


def _heatmap(values: np.ndarray, rows: Sequence[Any], columns: Sequence[Any], title: str, *, limit: float, percent: bool) -> Figure:
    figure, axis = plt.subplots(figsize=(max(5.0, 0.75 * len(columns) + 1.5), max(2.0, 0.45 * len(rows) + 1.2)))
    axis.imshow(np.ma.masked_invalid(values), cmap=DIVERGING, vmin=-limit, vmax=limit, aspect="auto")
    axis.set_xticks(range(len(columns)), [str(column) for column in columns], rotation=45 if not percent else 0, ha="right" if not percent else "center")
    axis.set_yticks(range(len(rows)), [str(row) for row in rows])
    axis.set_title(title, loc="left", fontsize=11, color=INK)
    axis.tick_params(colors=MUTED, labelsize=8, length=0)
    for spine in axis.spines.values():
        spine.set_visible(False)
    for (row, column), value in np.ndenumerate(values):
        if np.isfinite(value):
            axis.text(column, row, f"{value:+.0%}" if percent else f"{value:.2f}", ha="center", va="center", fontsize=8, color=INK)
    return figure


def tearsheet_html(title: str, equity: pd.Series, *, benchmark: pd.Series | None = None, benchmark_name: str = "benchmark",
                   unit_returns: pd.DataFrame | None = None, exposure: pd.DataFrame | None = None, notes: Sequence[str] = (), window: int = 90) -> str:
    """The tearsheet as one HTML string.

    Args:
        equity: The book's equity, any frequency (resampled to daily closes).
        benchmark: The benchmark's price on the same dates (e.g. BTC's mark).
        unit_returns: Daily returns per strategy (sleeve or basket) as shares of equity, for their correlations.
        exposure: Columns from `book_risk.exposure_history` (gross, net, beta_exposure, stressed_volatility ...).
        notes: Lines printed under the title (the config, the period, caveats).
        window: Days in the rolling charts.
    """
    daily = equity.resample("1D").last().dropna()
    returns = daily.pct_change().dropna()
    bench_returns = benchmark.resample("1D").last().pct_change().dropna().reindex(returns.index).dropna() if benchmark is not None else None
    if len(returns) < 2:
        raise ValueError("a tearsheet needs at least three days of equity")
    charts: list[tuple[str, str]] = []
    summary = summary_table(returns, bench_returns, benchmark_name)

    figure, axis = plt.subplots(figsize=(10, 3.6))
    axis.plot(daily.index, daily / daily.iloc[0], color=SERIES[0], linewidth=1.8, label="book")
    if benchmark is not None:
        price = benchmark.resample("1D").last().reindex(daily.index).ffill().dropna()
        if len(price):
            axis.plot(price.index, price / price.iloc[0], color=SERIES[1], linewidth=1.4, label=f"buy-and-hold {benchmark_name}")
    axis.set_yscale("log")
    axis.yaxis.set_major_formatter(lambda value, _: f"{value:g}x")
    axis.yaxis.set_minor_formatter(lambda value, _: f"{value:g}x" if round(value, 6) in (0.5, 0.7, 1.5, 2.0, 3.0, 5.0, 7.0) else "")
    _style(axis, "Growth of 1 (log scale)")
    _legend(axis)
    charts.append(("Equity", _png(figure)))

    figure, axis = plt.subplots(figsize=(10, 2.4))
    underwater = daily / daily.cummax() - 1.0
    axis.fill_between(underwater.index, underwater.to_numpy(), 0.0, color=SERIES[0], alpha=0.35, linewidth=0)
    axis.plot(underwater.index, underwater, color=SERIES[0], linewidth=1.2)
    axis.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
    _style(axis, f"Drawdown from the peak (deepest {underwater.min():.1%})")
    charts.append(("Drawdown", _png(figure)))

    months = monthly_returns(returns)
    limit = max(0.05, float(np.nanmax(np.abs(months.to_numpy()))))
    charts.append(("Monthly returns", _png(_heatmap(months.to_numpy(), list(months.index), MONTHS, "Monthly returns", limit=limit, percent=True))))

    span = min(window, max(10, len(returns) // 3))
    panels = [("volatility (annual)", returns.rolling(span).std() * np.sqrt(DAYS_PER_YEAR), "{:.0%}"),
              ("Sharpe", returns.rolling(span).mean() / returns.rolling(span).std() * np.sqrt(DAYS_PER_YEAR), "{:.1f}")]
    if bench_returns is not None and len(bench_returns) > span:
        panels.append((f"beta to {benchmark_name}", rolling_beta(returns, bench_returns, window=span), "{:.2f}"))
    figure, axes = plt.subplots(len(panels), 1, figsize=(10, 2.1 * len(panels)), sharex=True)
    for axis, (name, series, pattern) in zip(np.atleast_1d(axes), panels):
        axis.plot(series.index, series, color=SERIES[0], linewidth=1.4)
        axis.axhline(0.0, color=AXIS, linewidth=0.8)
        axis.yaxis.set_major_formatter(lambda value, _, pattern=pattern: pattern.format(value))
        _style(axis, f"Rolling {span}-day {name}")
    figure.tight_layout()
    charts.append(("Rolling risk", _png(figure)))

    if exposure is not None and len(exposure.dropna(how="all")) > 1:
        lines = [(name, label) for name, label in (("gross", "gross"), ("net", "net"), ("beta_exposure", f"in {benchmark_name} terms (weight x beta)")) if name in exposure]
        has_vol = "stressed_volatility" in exposure and exposure["stressed_volatility"].notna().any()
        figure, axes = plt.subplots(2 if has_vol else 1, 1, figsize=(10, 4.6 if has_vol else 2.6), sharex=True)
        axes = np.atleast_1d(axes)
        for (name, label), color in zip(lines, SERIES):
            axes[0].plot(exposure.index, exposure[name], color=color, linewidth=1.4, label=label)
        axes[0].axhline(0.0, color=AXIS, linewidth=0.8)
        _style(axes[0], "Exposure (x equity)")
        _legend(axes[0])
        if has_vol:
            axes[1].plot(exposure.index, exposure["volatility"], color=SERIES[0], linewidth=1.4, label="at estimated correlations")
            axes[1].plot(exposure.index, exposure["stressed_volatility"], color=SERIES[1], linewidth=1.4, label="at crash correlations")
            axes[1].yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
            _style(axes[1], "Forecast volatility of the book (annual)")
            _legend(axes[1])
        figure.tight_layout()
        charts.append(("Exposure", _png(figure)))

    alike = ""
    if unit_returns is not None and unit_returns.shape[1] > 1:
        moving = unit_returns.loc[:, unit_returns.std() > 0]
        if moving.shape[1] > 1:
            correlation = moving.corr()
            charts.append(("Strategy correlations", _png(_heatmap(correlation.to_numpy(), list(correlation.index), list(correlation.columns),
                                                                  "Correlation of the strategies' daily returns", limit=1.0, percent=False))))
            alike = (f"The {moving.shape[1]} strategies are worth {effective_bets(correlation):.1f} independent bets "
                     f"(average correlation {average_correlation(correlation):+.2f}).")

    rows = "".join(f"<tr><th>{html.escape(str(label))}</th>" + "".join(f"<td>{_format(str(label), row[column])}</td>" for column in summary.columns) + "</tr>"
                   for label, row in summary.iterrows())
    head = "".join(f"<th>{html.escape(str(column))}</th>" for column in summary.columns)
    body = [f"<h1>{html.escape(title)}</h1>",
            f"<p class='muted'>{daily.index[0]:%Y-%m-%d} to {daily.index[-1]:%Y-%m-%d}, daily closes (UTC).</p>",
            *[f"<p>{html.escape(note)}</p>" for note in notes],
            f"<table><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table>",
            f"<p>{html.escape(alike)}</p>" if alike else "",
            *[image(name, data) for name, data in charts]]
    return html_page(title, body)


def snapshot_inputs(snapshots: Sequence[Mapping[str, Any]], *, benchmark: str | None = None) -> dict[str, Any]:
    """`tearsheet_html` inputs from a running book's stored snapshots (`TradeLogger.list_portfolio_snapshots`).

    The benchmark is `benchmark`'s mark from the same snapshots (default: the book's BTC instrument, else none).
    A strategy's daily return is the change in its attributed P&L over the previous day's equity.
    """
    ordered = sorted(snapshots, key=lambda snap: pd.Timestamp(snap["timestamp"]))
    if not ordered:
        raise ValueError("no snapshots")
    stamps = pd.DatetimeIndex([pd.Timestamp(snap["timestamp"]) for snap in ordered])
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    equity = pd.Series([float(snap["equity"]) for snap in ordered], index=stamps)
    instruments = sorted({name for snap in ordered for name in (snap.get("instruments") or {})})
    chosen = benchmark or next((name for name in instruments if "BTC" in name.upper() or "XBT" in name.upper()), None)
    price = None
    if chosen is not None:
        price = pd.Series([float(((snap.get("instruments") or {}).get(chosen) or {}).get("price") or np.nan) for snap in ordered], index=stamps).replace(0.0, np.nan).dropna()
    pnl = pd.DataFrame([{name: float(info.get("pnl") or 0.0) for name, info in (snap.get("sleeves") or {}).items()} for snap in ordered], index=stamps)
    daily_pnl = pnl.resample("1D").last().ffill()
    daily_equity = equity.resample("1D").last().ffill()
    unit_returns = daily_pnl.diff().div(daily_equity.shift(1), axis=0).dropna(how="all") if len(pnl.columns) else None
    keys = ("gross", "net", "beta_exposure", "volatility", "stressed_volatility")
    exposure = pd.DataFrame([{key: (snap.get("exposure") or {}).get(key) for key in keys} for snap in ordered], index=stamps).astype(float)
    return {"equity": equity, "benchmark": price if price is not None and len(price) > 1 else None, "benchmark_name": chosen or "benchmark",
            "unit_returns": unit_returns, "exposure": exposure.resample("1D").last()}
