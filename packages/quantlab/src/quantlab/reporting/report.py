"""The standard strategy report: one call, one self-contained HTML file (plan 5.3).

    path = build_report(result, name="H15 primary", definition="Bond trend with a carry tilt ...", config=cfg,
                        baseline=hold, benchmark=returns["NDX"], data_cards=[card_ndx], labels=regime_labels,
                        cost_run=lambda m: run(cost_multiplier=m), hypothesis="H15")

Sections, in the order a reader needs them:

1. **Five-minute summary** for a portfolio manager: the verdict, what the strategy is, the headline numbers, what can
   go wrong, and the recommendation that follows from the verdict.
2. Definition (text and config), 3. data cards, 4. performance (equity with regime shading, drawdown, rolling Sharpe,
   monthly returns), 5. costs and attribution, 6. regimes, 7. robustness, 8. scenarios and stress, 9. the strategy
   card in full, 10. reproducibility (commit, config hash, data snapshots).

Everything missing is computed: the strategy card (noted in the lab ledger like any card), market environments as
regimes when no labels are given, crises and stress from the result's own positions. Charts are embedded as PNG, so
the file can be mailed or printed to PDF from a browser as it is.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from quantlab.analytics.conditional import by_regime
from quantlab.analytics.performance import attribution, benchmark_stats, metrics
from quantlab.reporting import charts
from quantlab.validation import robustness, scenarios
from quantlab.validation.ledger import git_commit

RECOMMENDATIONS = {
    "evidence of an edge": "Candidate for paper trading: every check that applies passes. Size it at the shrunk Sharpe, not the backtest Sharpe.",
    "weak": "Not for capital. The mean return is significant but the checks listed under the verdict fail; fix the cause or reject it.",
    "no evidence": "Reject as specified. The return is not distinguishable from zero; a new idea needs a new pre-registration.",
    "short history": "No decision possible: too little history. Collect more data (paper trading counts) before judging it.",
}

CSS = """
body { font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1d1d1f; background: #fff; max-width: 1100px; margin: 0 auto; padding: 24px; }
h1 { font-size: 24px; margin-bottom: 4px; } h2 { font-size: 18px; border-bottom: 2px solid #1f4e79; padding-bottom: 4px; margin-top: 36px; }
h3 { font-size: 15px; margin-top: 20px; } .meta { color: #666; font-size: 12px; }
.verdict { display: inline-block; padding: 4px 12px; border-radius: 4px; font-weight: 600; color: #fff; }
.v-evidence { background: #548235; } .v-weak { background: #bf9000; } .v-no { background: #c00000; } .v-short { background: #7f7f7f; }
table { border-collapse: collapse; margin: 8px 0 16px; font-size: 12.5px; } th, td { border-bottom: 1px solid #ddd; padding: 4px 10px; text-align: right; }
th { background: #f3f5f8; } td:first-child, th:first-child { text-align: left; }
.summary { background: #f7f9fc; border-left: 4px solid #1f4e79; padding: 12px 20px; } img { max-width: 100%; }
pre { background: #f5f5f5; padding: 10px; overflow-x: auto; font-size: 12px; } ul { margin-top: 4px; }
"""


def _esc(text: Any) -> str:
    return html.escape(str(text))


def _table(frame: pd.DataFrame | pd.Series, *, digits: int = 3, percent: Sequence[str] = ()) -> str:
    frame = frame.to_frame() if isinstance(frame, pd.Series) else frame.copy()

    def fmt(value: Any, column: Any) -> str:
        if isinstance(value, (float, np.floating)):
            if not np.isfinite(value):
                return "–"
            return f"{value:.1%}" if column in percent else f"{value:,.{digits}f}"
        return _esc(value)

    head = "<tr><th></th>" + "".join(f"<th>{_esc(c)}</th>" for c in frame.columns) + "</tr>"
    body = "".join("<tr><td>" + _esc(index if not isinstance(index, tuple) else " / ".join(map(str, index))) + "</td>"
                   + "".join(f"<td>{fmt(value, column)}</td>" for column, value in row.items()) + "</tr>" for index, row in frame.iterrows())
    return f"<table>{head}{body}</table>"


def _image(figure: Any) -> str:
    return f'<img src="data:image/png;base64,{charts.to_base64(figure)}">'


def _figure(draw: Callable[[Any], Any], size: tuple[float, float] = (11, 4)) -> str:
    import matplotlib.pyplot as plt

    with plt.rc_context(charts.style()):
        figure, ax = plt.subplots(figsize=size)
        draw(ax)
        figure.tight_layout()
    return _image(figure)


def _verdict_class(verdict: str) -> str:
    return {"evidence of an edge": "v-evidence", "weak": "v-weak", "no evidence": "v-no"}.get(verdict, "v-short")


def build_report(result: Any, *, name: str, path: str | Path | None = None, definition: str = "", config: Any = None, hypothesis: str = "",
                 card: Any = None, baseline: Any = None, benchmark: pd.Series | None = None, data_cards: Sequence[Any] = (),
                 labels: pd.Series | None = None, cost_run: Callable[[float], Any] | None = None, extra_tables: Mapping[str, pd.DataFrame | pd.Series] | None = None,
                 snapshots: Sequence[str] = (), periods: float = 252.0) -> Path:
    """Write the report (see the module docstring) and return its path."""
    from quantlab.validation.strategy_card import card_backtest

    if card is None:
        card = card_backtest(result, name=name, baseline=baseline, family=hypothesis or None)
    pnl = result.pnl
    base_pnl = baseline.pnl if hasattr(baseline, "pnl") else baseline
    numbers = metrics(result)
    market = benchmark if benchmark is not None else result.returns.mean(axis=1)
    versus = benchmark_stats(pnl, market, periods=periods)
    crisis = scenarios.historical(pnl, benchmark=market)
    stress = scenarios.stress(result.held, result.returns)
    dependence = robustness.episode_dependence(pnl, periods_per_year=periods)
    costs = robustness.cost_sensitivity(cost_run) if cost_run is not None else None
    regimes = {"regime": labels} if labels is not None else robustness.market_environments(market)

    # --- 1. summary --------------------------------------------------------------------------------------------------
    card_numbers = card.details.get("numbers", {})
    def number(value: Any, form: str) -> str:
        return form.format(value) if isinstance(value, (int, float, np.floating)) and np.isfinite(value) else "–"

    headline = pd.Series({"Sharpe (net)": number(numbers.get("Sharpe"), "{:.2f}"),
                          "Sharpe interval": f"{number(card_numbers.get('sharpe_low'), '{:.2f}')} to {number(card_numbers.get('sharpe_high'), '{:.2f}')}",
                          "Shrunk Sharpe (expect live)": number(card_numbers.get("shrunk_sharpe"), "{:.2f}"), "Annual return": number(numbers.get("annual return"), "{:.1%}"),
                          "Volatility": number(numbers.get("volatility"), "{:.1%}"), "Max drawdown": number(numbers.get("max drawdown"), "{:.1%}"),
                          "Turnover (x equity a year)": number(numbers.get("turnover"), "{:.1f}"), "Costs a year": number(numbers.get("costs per year"), "{:.2%}"),
                          "Beta to benchmark": number(versus.get("beta"), "{:.2f}"), "Alpha a year": number(versus.get("alpha"), "{:.1%}")}, name="value")
    risks = []
    if len(crisis):
        worst = crisis["return"].idxmin()
        risks.append(f"Worst named crisis: {worst}, {crisis.at[worst, 'return']:.1%} (benchmark {crisis.at[worst, 'benchmark return']:.1%}).")
    risks.append(f"Worst drawdown {numbers.get('max drawdown', np.nan):.1%}; doubling market volatility takes it to {stress['table'].at['volatility x2', 'max drawdown']:.1%}.")
    risks.append(f"A 5-sigma gap against the book costs {abs(stress['gap loss'].min()):.1%} on the worst day held.")
    without = dependence["Sharpe without best months"]
    if 5 in without.index:
        risks.append(f"Without its best 5 months the Sharpe is {without[5]:.2f} (from {without[0]:.2f}).")
    if costs is not None:
        risks.append(f"Costs: the Sharpe reaches zero at {costs['breakeven multiplier']:.1f}x today's cost estimate." if np.isfinite(costs["breakeven multiplier"])
                     else "Costs: the Sharpe stays positive at five times today's cost estimate.")
    if card.versus_baseline == "not shown better":
        risks.append("It is not shown to beat its baseline.")
    summary = f"""
<div class="summary">
<p><span class="verdict {_verdict_class(card.verdict)}">{_esc(card.verdict)}</span> &nbsp; {_esc(card.reason)}</p>
<p><b>What it is.</b> {_esc(definition or 'See the definition below.')}</p>
{_table(headline, digits=2)}
<p><b>What can go wrong.</b></p><ul>{''.join(f'<li>{_esc(item)}</li>' for item in risks)}</ul>
<p><b>Recommendation.</b> {_esc(RECOMMENDATIONS.get(card.verdict, ''))}</p>
</div>"""

    # --- the rest --------------------------------------------------------------------------------------------------
    sections = [("1. Five-minute summary", summary)]
    config_text = json.dumps(_plain(config), indent=2, default=str) if config is not None else ""
    sections.append(("2. Definition", f"<p>{_esc(definition)}</p>" + (f"<pre>{_esc(config_text)}</pre>" if config_text else "")))
    data_html = ""
    for data_card in data_cards:
        problems = "".join(f"<li><b>problem:</b> {_esc(p)}</li>" for p in data_card.problems)
        data_html += (f"<h3>{_esc(data_card.name)}</h3><p>{data_card.rows} bars, {_esc(data_card.first)} to {_esc(data_card.last)}; usable from "
                      f"{data_card.usable_from:%Y-%m-%d}.</p>" + _table(pd.Series(data_card.checks, name="value").astype(str)) + (f"<ul>{problems}</ul>" if problems else ""))
    sections.append(("3. Data", data_html or "<p>No data cards given.</p>"))
    curves = {name: pnl} | ({"baseline": base_pnl} if base_pnl is not None else {})
    performance = _figure(lambda ax: (charts.equity(curves, benchmark=market, ax=ax), labels is not None and charts.regime_shading(ax, labels.reindex(pnl.index)))) \
        + _figure(lambda ax: charts.drawdown(pnl, ax=ax), (11, 2.6)) + _figure(lambda ax: charts.rolling_sharpe(pnl, ax=ax), (11, 2.6)) \
        + _figure(lambda ax: charts.monthly_returns(pnl, ax=ax), (11, max(4.0, 0.25 * len(set(pnl.index.year)) + 1)))
    performance += _table(pd.Series(numbers, name="value"), digits=3) + "<h3>Against the benchmark</h3>" + _table(pd.Series(versus, name="value"))
    sections.append(("4. Performance", performance))
    attribution_html = _figure(lambda ax: charts.attribution(result, by="part", ax=ax)) + _figure(lambda ax: charts.attribution(result, by="instrument", ax=ax)) \
        + _table(attribution(result), percent=("per year",))
    if costs is not None:
        attribution_html += "<h3>Cost sensitivity</h3>" + _table(costs["table"], digits=2)
    sections.append(("5. Costs and attribution", attribution_html))
    sections.append(("6. Regimes", "<p>Labels known before each bar (shifted one bar); the table is descriptive.</p>"
                     + _table(robustness.environments(pnl, regimes, periods_per_year=periods))
                     + (_table(by_regime({name: pnl} | ({"baseline": base_pnl} if base_pnl is not None else {}), labels)) if labels is not None else "")))
    robust_html = _figure(lambda ax: robustness.start_dates(pnl, periods_per_year=periods).plot.bar(ax=ax, title="Sharpe from each start year on", color=charts.PALETTE[0]), (11, 3))
    robust_html += "<h3>Sub-periods</h3>" + _table(robustness.subperiods(pnl, periods_per_year=periods), percent=("annual return", "max drawdown"))
    robust_html += "<h3>Best months removed</h3>" + _table(without, digits=2)
    robust_html += f"<p>Share of all P&amp;L from the best 1% of days: {dependence['share of P&L from best 1% of days']:.0%}.</p>"
    for title, table in (extra_tables or {}).items():
        robust_html += f"<h3>{_esc(title)}</h3>" + _table(table)
    sections.append(("7. Robustness", robust_html))
    sections.append(("8. Scenarios and stress", "<p>Positions held as they were (no reaction).</p>"
                     + (_table(crisis, percent=tuple(crisis.columns)) if len(crisis) else "<p>No named crisis inside the data.</p>")
                     + _table(stress["table"], digits=3)))
    card_html = _table(card.table.drop(columns="passes").assign(passes=card.table["passes"].map({True: "pass", False: "FAIL"}).fillna("")))
    if len(card.variants) > 1:
        card_html += "<h3>Variants</h3>" + _table(card.variants)
    card_html += _image(card.plot())
    sections.append(("9. Strategy card", card_html))
    reproducibility = pd.Series({"git commit": git_commit(), "config hash": _hash(config_text), "data snapshots": ", ".join(snapshots) or "–",
                                 "card fingerprint": getattr(card, "fingerprint", ""), "card standard version": card.standard.version,
                                 "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}, name="value")
    sections.append(("10. Reproducibility", _table(reproducibility)))

    span = f"{pnl.index[0]:%Y-%m-%d} to {pnl.index[-1]:%Y-%m-%d}" if len(pnl) else ""
    body = "".join(f"<h2>{_esc(title)}</h2>{content}" for title, content in sections)
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(name)}</title><style>{CSS}</style></head><body>"
            f"<h1>{_esc(name)}</h1><p class='meta'>{_esc(hypothesis)} · {span} · generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>{body}</body></html>")
    out = Path(path) if path else Path(os.environ.get("QUANTLAB_DATA", "quantlab_data")) / "reports" / f"{_slug(name)}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def _slug(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name.lower()).strip("_") or "report"


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16] if text else "–"


def _plain(config: Any) -> Any:
    from dataclasses import asdict, is_dataclass

    return asdict(config) if is_dataclass(config) and not isinstance(config, type) else config
