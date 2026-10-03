"""Shared pieces for the static HTML reports (the book tearsheet, the signal report): one look, one palette.

Charts are matplotlib PNGs embedded in the page, so a report is one file that opens anywhere. The palette is a fixed
order checked for colour-blind separation; every chart has one y-axis, a legend above the plot, and its numbers
also appear in a table on the page.
"""

from __future__ import annotations

import base64
import html
import io
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

SERIES = ("#2a78d6", "#eb6834", "#1baf7a")  # blue, orange, aqua: a fixed order, checked for colour-blind separation
SURFACE, INK, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9", "#c3c2b7"
DIVERGING = LinearSegmentedColormap.from_list("red_gray_blue", ["#e34948", "#f0efec", "#2a78d6"])


def style_axis(axis: Any, title: str) -> None:
    """The reports' axis style: a left-aligned title, hairline grid, muted ticks, left and bottom spines only."""
    axis.set_title(title, loc="left", fontsize=11, color=INK)
    axis.set_facecolor(SURFACE)
    axis.grid(True, color=GRID, linewidth=0.6)
    axis.set_axisbelow(True)
    axis.tick_params(colors=MUTED, labelsize=8)
    for name, spine in axis.spines.items():
        spine.set_visible(name in ("left", "bottom"))
        spine.set_color(AXIS)


def legend(axis: Any) -> None:
    """A legend above the plot, right of the title: inside the axes it would sit on the lines."""
    axis.legend(frameon=False, fontsize=8, labelcolor=INK, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncols=3, borderaxespad=0.2)


def png(figure: Figure) -> str:
    """The figure as a base64 PNG (and closed)."""
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=110, facecolor=SURFACE, bbox_inches="tight")
    plt.close(figure)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def html_table(frame: pd.DataFrame, formats: Mapping[str, str | Callable[[Any], str]] | None = None, *, default: str = "{:.4g}", index: bool = True) -> str:
    """A frame as an HTML table. `formats` maps a column to a format string or a function; other numbers use `default`."""
    formats = dict(formats or {})

    def cell(column: Any, value: Any) -> str:
        if value is None or (isinstance(value, float) and not np.isfinite(value)) or value is pd.NaT:
            return ""
        chosen = formats.get(str(column))
        if callable(chosen):
            return html.escape(chosen(value))
        if isinstance(value, (bool, np.bool_)):
            return "yes" if value else "no"
        if isinstance(value, (int, np.integer)):
            return (chosen or "{:d}").format(int(value))
        if isinstance(value, (float, np.floating)):
            return (chosen or default).format(float(value))
        if isinstance(value, pd.Timestamp):
            return f"{value:%Y-%m-%d %H:%M}"
        return html.escape(str(value))

    head = ("<th></th>" if index else "") + "".join(f"<th>{html.escape(str(column))}</th>" for column in frame.columns)
    rows = []
    for label, row in frame.iterrows():
        name = " / ".join(str(part) for part in label) if isinstance(label, tuple) else str(label)
        rows.append("<tr>" + (f"<th>{html.escape(name)}</th>" if index else "") + "".join(f"<td>{cell(column, row[column])}</td>" for column in frame.columns) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def image(name: str, data: str) -> str:
    """An embedded chart under its heading."""
    return f"<h2>{html.escape(name)}</h2><img alt='{html.escape(name)}' src='data:image/png;base64,{data}'>"


def html_page(title: str, body: Sequence[str]) -> str:
    """One self-contained HTML page with the reports' style."""
    style = (f"body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#f9f9f7;color:{INK};max-width:1000px;margin:24px auto;padding:0 16px}}"
             f"h1{{font-size:22px}}h2{{font-size:15px;margin:28px 0 8px}}.muted{{color:#52514e}}img{{max-width:100%;height:auto}}"
             f"table{{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;margin:6px 0 14px}}"
             f"th,td{{padding:4px 14px 4px 0;text-align:right;border-bottom:1px solid {GRID}}}tbody th,thead th:first-child{{text-align:left;font-weight:500}}")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{style}</style></head><body>{''.join(body)}</body></html>")
