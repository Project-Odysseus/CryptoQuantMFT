"""One standard report per signal: the same screens for every idea, on one page.

A study that prints whatever its author thought of that day can't be compared with the last one. `signal_report`
runs the same checks on any event list (`src/research/events.py`) and writes them as one HTML page and a set of
tables:

1. **Data quality:** gaps, duplicates and broken candles in the bars the study stands on.
2. **Frequency:** how often the signal fires, per year. A handful of events in one year is an anecdote.
3. **Event study:** forward returns by horizon with confidence intervals, excursions, and matched controls.
4. **Regimes:** the primary horizon's outcome by trend and volatility state at the event.
5. **Robustness:** the primary outcome under nearby definitions of the same signal (`variants`), to look for a
   plateau instead of one good cell. Each variant is a trial for the ledger.

For a continuous feature (a z-score, a ratio), pass `feature`: the report adds its information coefficient by
horizon and whether its best bucket clears the round-trip cost (`features.ic_decay`).

It describes; it does not decide. Passing the hypothesis is judged against the criteria written in its
pre-registration, and overlap with the book is `src/portfolio/backtest.candidate_report`.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt

from src.research.events import HORIZONS, EventStudy, by_regime, data_quality, event_study, plateau, regime_labels
from src.research.features import ic_decay
from src.utils.report import AXIS, SERIES, html_page, html_table, image, legend, png, style_axis

PERCENT = "{:+.2%}"


@dataclass(slots=True)
class SignalReport:
    """A finished report: its tables (also for CSVs and assertions), the event study behind them, and the page."""

    name: str
    tables: dict[str, pd.DataFrame]
    study: EventStudy
    html: str
    trials: int  # the configurations this report tried (the signal itself plus its variants), for the trial ledger

    def write(self, folder: str | Path) -> Path:
        """Write `report.html` and one CSV per table into `folder`; returns the page's path."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        for title, table in self.tables.items():
            table.to_csv(folder / f"{title.replace(' ', '_')}.csv")
        self.study.events.to_csv(folder / "events.csv", index=False)
        page = folder / "report.html"
        page.write_text(self.html, encoding="utf-8")
        return page


def _path_chart(study: EventStudy) -> str:
    paths = study.paths
    figure, axis = plt.subplots(figsize=(10, 3.4))
    axis.plot(paths.index, paths["mean"], color=SERIES[0], linewidth=1.8, label="mean")
    axis.plot(paths.index, paths["median"], color=SERIES[1], linewidth=1.4, label="median")
    axis.axvline(0, color=AXIS, linewidth=1.0)
    axis.axhline(0, color=AXIS, linewidth=0.8)
    axis.yaxis.set_major_formatter(lambda value, _: f"{value:+.1%}")
    axis.set_xlabel("bars from the event", fontsize=8, color="#52514e")
    style_axis(axis, f"Price path around {int(paths['events'].max())} events (log move from the event bar's close)")
    legend(axis)
    return png(figure)


def _horizon_chart(study: EventStudy) -> str:
    summary = study.summary
    positions = np.arange(len(summary))
    figure, axis = plt.subplots(figsize=(10, 3.2))
    has_controls = summary["control_mean"].notna().any()
    offset = 0.12 if has_controls else 0.0
    lower, upper = summary["mean"] - summary["ci_low"], summary["ci_high"] - summary["mean"]
    axis.errorbar(positions - offset, summary["mean"], yerr=[lower.clip(lower=0), upper.clip(lower=0)], fmt="o", color=SERIES[0], markersize=7, capsize=3, linewidth=1.4,
                  label="after the event (95% interval)")
    if has_controls:
        axis.plot(positions + offset, summary["control_mean"], "s", color=SERIES[1], markersize=7, label="matched controls")
    axis.plot(positions, summary["unconditional_mean"], "_", color="#898781", markersize=18, markeredgewidth=1.6, label="all bars")
    axis.axhline(0, color=AXIS, linewidth=0.8)
    axis.set_xticks(positions, [f"{horizon} bar{'s' if horizon != 1 else ''}\n(n={int(count)})" for horizon, count in zip(summary.index, summary["events"])])
    axis.yaxis.set_major_formatter(lambda value, _: f"{value:+.1%}")
    style_axis(axis, "Mean forward return by horizon")
    handles, labels = axis.get_legend_handles_labels()
    order = sorted(range(len(labels)), key=lambda item: ("after the event" not in labels[item], "controls" not in labels[item]))  # the event first
    axis.legend([handles[item] for item in order], [labels[item] for item in order], frameon=False, fontsize=8, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncols=3, borderaxespad=0.2)
    return png(figure)


def _frequency_chart(per_year: pd.DataFrame) -> str:
    figure, axis = plt.subplots(figsize=(10, 2.4))
    axis.bar(per_year.index.astype(str), per_year["events"], color=SERIES[0], width=0.6)
    for position, count in enumerate(per_year["events"]):
        axis.text(position, count, str(int(count)), ha="center", va="bottom", fontsize=8, color="#0b0b0b")
    style_axis(axis, "Events per year")
    axis.grid(False, axis="x")
    return png(figure)


def signal_report(name: str, frame: pd.DataFrame, events: Sequence[int] | np.ndarray | pd.DatetimeIndex, *, interval_seconds: float,
                  horizons: Sequence[int] = HORIZONS, primary_horizon: int | None = None, entry: str = "next_open", side: float = 1.0,
                  matching: Sequence[float] | np.ndarray | pd.Series | None = None, cost_bps: float = 0.0,
                  variants: Mapping[str, Sequence[int] | np.ndarray] | None = None, feature: Sequence[float] | np.ndarray | None = None,
                  notes: Sequence[str] = (), bootstrap_runs: int = 2000, seed: int = 0) -> SignalReport:
    """Run the standard screens for one event list on one bar frame. See the module docstring.

    Args:
        frame: Bars indexed by open time with open, high, low, close (and volume if there is one).
        events: Bar positions (or timestamps) of the signal, each known at its bar's close.
        interval_seconds: The bar length, for the gap check.
        primary_horizon: The horizon the regime and robustness tables use (default: the middle one). Fix it in the
            pre-registration.
        matching: A value per bar for the matched controls (e.g. trailing realized volatility); None skips them.
        variants: Label -> event positions for nearby definitions of the same signal.
        feature: The continuous value behind the signal, one per bar, for the information-coefficient table.
        notes: Lines printed under the title (the hypothesis, the data window, caveats).
    """
    horizons = tuple(sorted(int(horizon) for horizon in horizons))
    primary = int(primary_horizon) if primary_horizon is not None else horizons[len(horizons) // 2]
    if primary not in horizons:
        raise ValueError(f"primary_horizon {primary} is not one of the horizons {horizons}")
    settings: dict[str, Any] = {"horizons": horizons, "entry": entry, "side": side, "matching": matching, "cost_bps": cost_bps, "bootstrap_runs": bootstrap_runs, "seed": seed}
    study = event_study(frame, events, **settings)

    quality = pd.DataFrame({"value": data_quality(frame, interval_seconds)})
    per_year = pd.DataFrame({"events": pd.Series(study.frequency["per_year"], dtype=float)})
    per_year.index.name = "year"
    frequency = pd.DataFrame({"value": {key: value for key, value in study.frequency.items() if key != "per_year"}})
    tables: dict[str, pd.DataFrame] = {"data quality": quality, "frequency": frequency, "events per year": per_year, "event study": study.summary}
    if len(study.events):
        tables["regimes"] = by_regime(study, regime_labels(frame), primary)
    rows = [{"variant": "as defined", "events": int(study.summary.loc[primary, "events"]), **{key: study.summary.loc[primary, key] for key in ("mean", "ci_low", "ci_high", "excess")}}]
    for label, positions in (variants or {}).items():
        other = event_study(frame, positions, **settings).summary.loc[primary]
        rows.append({"variant": label, "events": int(other["events"]), **{key: other[key] for key in ("mean", "ci_low", "ci_high", "excess")}})
    robustness = pd.DataFrame(rows).set_index("variant")
    flat = plateau(robustness)
    if variants:
        tables["robustness"] = robustness
    if feature is not None:
        tables["feature information"] = ic_decay(np.asarray(feature, dtype=float), frame["close"].to_numpy(dtype=float), {f"{horizon} bars": horizon for horizon in horizons},
                                                 round_trip_bps=cost_bps).set_index("horizon")

    row = study.summary.loc[primary]
    direction = "long" if side >= 0 else "short"
    headline = (f"{int(row['events'])} events. A {direction} entered at the {'next open' if entry == 'next_open' else 'close'} and held {primary} bars returned "
                f"{row['mean']:+.2%} on average (95% interval {row['ci_low']:+.2%} to {row['ci_high']:+.2%}), median {row['median']:+.2%}, "
                f"positive {row['hit_rate']:.0%} of the time" + (f", after {cost_bps:g} bps of costs" if cost_bps else "") + ". "
                + (f"Matched controls returned {row['control_mean']:+.2%}; the excess is {row['excess']:+.2%} ({row['excess_ci_low']:+.2%} to {row['excess_ci_high']:+.2%}). "
                   if np.isfinite(row["control_mean"]) else "No matched controls were run. ")
                + f"On the way the position was on average {row['mean_mae']:+.2%} at its worst.") if row["events"] else "The signal never fired in this sample."
    percent_columns = ("mean", "median", "std", "q05", "q25", "q75", "q95", "ci_low", "ci_high", "mean_mae", "mean_mfe", "unconditional_mean", "control_mean", "excess",
                       "excess_ci_low", "excess_ci_high")
    formats = {**{column: PERCENT for column in percent_columns}, "hit_rate": "{:.0%}"}
    body = [f"<h1>{html.escape(name)}</h1>", *[f"<p class='muted'>{html.escape(note)}</p>" for note in notes], f"<p>{html.escape(headline)}</p>"]
    if row["events"]:
        body += [image("Around the event", _path_chart(study)), image("By horizon", _horizon_chart(study))]
    body += ["<h2>Event study</h2>", html_table(study.summary.rename_axis("horizon (bars)"), formats)]
    if len(per_year):
        body += [image("How often it fires", _frequency_chart(per_year)), html_table(frequency, {"value": lambda value: f"{value:%Y-%m-%d %H:%M}" if isinstance(value, pd.Timestamp) else f"{value:g}"})]
    if "regimes" in tables:
        body += [f"<h2>By regime at the event ({primary}-bar return)</h2>", html_table(tables["regimes"], formats),
                 "<p class='muted'>Trend: the close against its 200-bar average. Volatility: 30-bar realized volatility ranked against its own past year. Both use bars up to the event only.</p>"]
    if variants:
        body += [f"<h2>Nearby definitions ({primary}-bar return)</h2>", html_table(robustness, formats),
                 f"<p class='muted'>{flat['same_sign_share']:.0%} of the {flat['cells']} definitions agree in sign; means run from {flat['min']:+.2%} to {flat['max']:+.2%} "
                 f"(median {flat['median']:+.2%}). Look for a plateau, not the best cell.</p>"]
    if "feature information" in tables:
        body += ["<h2>The feature behind the signal</h2>",
                 html_table(tables["feature information"], {"ic": "{:+.3f}", "ic_t": "{:+.1f}", "top_vs_avg_bps": "{:+.1f}", "bottom_vs_avg_bps": "{:+.1f}", "best_edge_bps": "{:.1f}"})]
    body += ["<h2>Data quality</h2>", html_table(quality, {"value": lambda value: f"{value:%Y-%m-%d %H:%M}" if isinstance(value, pd.Timestamp) else f"{value:.4g}" if isinstance(value, float) else str(value)})]
    return SignalReport(name=name, tables=tables, study=study, html=html_page(name, body), trials=1 + len(variants or {}))
