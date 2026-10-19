"""The strategy factsheet: what a fund needs to adopt a strategy, on one page (plan 7.7).

    path = build_factsheet(spec, evidence, gates=promotion_check(spec, "live", evidence=evidence), correlations=table)

Sections: mechanism (what it trades and why it should pay), evidence (the card's verdict and numbers, history, power),
risks (drawdown, crises, stress), capacity (Sharpe and participation by capital), correlations (with the book's
signals, or whatever is given), operational needs (instruments, data, decision and execution times, costs assumed,
contracts), and status (stage and gates). It complements the full report (quantlab.reporting.report): the factsheet
is for the person deciding, the report for the person checking.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.analytics.performance import metrics
from quantlab.reporting import charts
from quantlab.reporting.report import CSS, _esc, _figure, _table, _verdict_class
from quantlab.validation import scenarios


def build_factsheet(spec: Any, evidence: Any, *, gates: Any = None, correlations: pd.Series | pd.DataFrame | None = None, stage: str = "research",
                    path: str | Path | None = None) -> Path:
    """Write the factsheet (see the module docstring) and return its path."""
    result, card = evidence.result, evidence.card
    numbers = metrics(result)
    card_numbers = card.details.get("numbers", {})
    headline = pd.Series({
        "Sharpe (net), interval": f"{numbers.get('Sharpe', float('nan')):.2f} ({card_numbers.get('sharpe_low', float('nan')):.2f} to {card_numbers.get('sharpe_high', float('nan')):.2f})",
        "Expected live Sharpe (shrunk)": f"{card_numbers.get('shrunk_sharpe', float('nan')):.2f}",
        "Annual return / volatility": f"{numbers.get('annual return', float('nan')):.1%} / {numbers.get('volatility', float('nan')):.1%}",
        "Worst drawdown": f"{numbers.get('max drawdown', float('nan')):.1%}",
        "Turnover / costs a year": f"{numbers.get('turnover', float('nan')):.1f}x / {numbers.get('costs per year', float('nan')):.2%}",
        "History": f"{evidence.years:.1f} years, {result.pnl.index[0]:%Y-%m} to {result.pnl.index[-1]:%Y-%m}",
        "Power": f"{evidence.power.get('probability of passing', float('nan')):.0%} chance a true Sharpe of {evidence.power.get('hypothesised Sharpe')} passes; "
                 f"detectable {evidence.power.get('detectable Sharpe (80% power)', float('nan')):.2f}",
    }, name="value")
    crisis = scenarios.historical(result.pnl, benchmark=result.returns.mean(axis=1))
    stress = scenarios.stress(result.held, result.returns)["table"]
    operations = pd.Series({
        "Instruments": ", ".join(spec.universe.instruments),
        "Data": "daily bars (open, high, low, close) per instrument; short rates for excess returns",
        "Decision and execution": "decided on each close, traded at the next close",
        "Sizing": f"{spec.sizing.target_vol:.0%} volatility per instrument (EWMA span {spec.sizing.span}), equal weights; band {spec.engine.band:.0%}",
        "Costs assumed": f"{spec.engine.cost_bps:g} bps per side" if spec.engine.cost_bps is not None else "per instrument, from the instrument master",
        "Contracts": "futures, rolled quarterly" if spec.engine.rolls_per_year else "-",
        "Spec": f"{spec.key} (hash {spec.hash})",
    }, name="value")
    sections = [
        ("Mechanism", f"<p>{_esc(spec.strategy.description or spec.strategy.title)}</p>"),
        ("Evidence", f"<p><span class='verdict {_verdict_class(card.verdict)}'>{_esc(card.verdict)}</span> {_esc(card.reason)}</p>" + _table(headline)
         + _figure(lambda ax: charts.equity({spec.id: result.pnl}, benchmark=result.returns.mean(axis=1), ax=ax), (10, 3.2))),
        ("Risks", (_table(crisis, percent=tuple(crisis.columns)) if len(crisis) else "<p>No named crisis inside the history.</p>") + _table(stress, digits=3)),
    ]
    if evidence.capacity is not None:
        sections.append(("Capacity", _table(evidence.capacity["table"], digits=4)
                         + f"<p>Capacity: the Sharpe halves at {evidence.capacity['capacity (Sharpe halves)']:,.0f} USD; participation reaches the "
                           f"{spec.capacity.max_participation:.0%} limit at {evidence.capacity['capacity (participation limit)']:,.0f} USD (inf: not within the sizes tested). "
                           "Daily traded values are the spec's assumptions.</p>"))
    if correlations is not None:
        sections.append(("Correlations", _table(correlations if isinstance(correlations, pd.DataFrame) else correlations.to_frame("correlation"), digits=2)))
    sections.append(("Operational needs", _table(operations)))
    status = f"<p>Stage: <b>{_esc(stage)}</b>.</p>"
    if gates is not None:
        status += f"<p>Gates for {_esc(gates.stage)}: {'all required gates pass' if gates.ok else 'blocked by ' + _esc(', '.join(gates.failing))}.</p>" + _table(gates.table)
    sections.append(("Status", status))
    body = "".join(f"<h2>{_esc(title)}</h2>{content}" for title, content in sections)
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(spec.strategy.title)}</title><style>{CSS}</style></head><body>"
            f"<h1>{_esc(spec.strategy.title)}</h1><p class='meta'>Factsheet · {_esc(spec.key)} · {_esc(spec.strategy.hypothesis)} · "
            f"{datetime.now(timezone.utc):%Y-%m-%d}</p>{body}</body></html>")
    out = Path(path) if path else Path(os.environ.get("QUANTLAB_DATA", "quantlab_data")) / "reports" / f"factsheet_{spec.id}_v{spec.version}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
