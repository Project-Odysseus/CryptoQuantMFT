"""H15: the H13 trend design on equity, Treasury, crude oil and currency futures (research/prereg/H15.txt).

    python scripts/research/h15_study.py            # fetches the series once into the lab catalog, then prints the results

Confirmatory: the pre-registration is locked before anything runs, and the book's test is logged as H15's trials.
"""

from __future__ import annotations

import math

import pandas as pd

from quantlab.analytics.performance import metrics
from quantlab.deploy.gates import evaluate
from quantlab.deploy.spec import find_spec, load_spec, spec_lab
from quantlab.lab import use_project_root
from quantlab.validation import ledger

START, END = "2007-01-01", "2025-12-31"
HALVES = ((START, "2016-06-30"), ("2016-07-01", END))
CLASSES = {"equity": ["idx:SPX", "idx:NDX", "idx:RUT"], "treasuries": ["etf:IEI", "etf:IEF"], "crude oil": ["etf:USO"],
           "currencies": ["fxusd:EUR", "fxusd:GBP", "fxusd:AUD", "fxusd:JPY", "fxusd:CHF", "fxusd:CAD"]}


def sharpe(pnl: pd.Series) -> float:
    pnl = pnl.dropna()
    return float(pnl.mean() / pnl.std() * math.sqrt(252.0)) if len(pnl) > 2 and pnl.std() > 0 else float("nan")


def main() -> None:
    use_project_root()
    ledger.lock_prereg("H15")  # the same text again changes nothing; a changed text is a new, visible version
    spec = load_spec(find_spec("multi_asset_trend"))
    lab = spec_lab(spec, start=START, end=END, hypothesis="H15")
    idea = lab.signal(spec.signal_fn(), name=spec.key, normalize=False, check=False)
    report = lab.test(idea, show=False)
    result = report.result
    card = report.strategy_card
    print(f"\nH15 book {START}..{END}: strategy card '{card.verdict}' ({card.reason})")
    print(pd.Series(card.details.get("numbers", {})).to_string())
    print(f"signal card: {report.signal_card.verdict if report.signal_card else '-'}")
    print(pd.Series(metrics(result)).to_string())
    for start, end in HALVES:
        print(f"Sharpe {start}..{end}: {sharpe(result.pnl.loc[start:end]):.2f}")
    doubled = lab.backtest(idea, cost_multiplier=2.0)
    print(f"Sharpe at twice the costs: {sharpe(doubled.pnl):.2f}")

    spx = lab.returns["idx:SPX"]
    rows = {}
    for name, members in CLASSES.items():
        part = result.net[members].sum(axis=1)
        growth = (1.0 + part).cumprod()
        monthly = pd.concat({"class": part, "spx": spx}, axis=1).resample("ME").sum()
        rows[name] = {"Sharpe (net, of its own P&L)": sharpe(part), "share of book P&L": float(part.sum() / result.pnl.sum()),
                      "worst drawdown": float((growth / growth.cummax() - 1.0).min()), "corr with S&P 500 (monthly)": float(monthly["class"].corr(monthly["spx"]))}
    print("\nPer asset class (descriptive):")
    print(pd.DataFrame(rows).T.round(3).to_string())
    ledger.record_trials("H15 per-class breakdown", 4, family="H15", data="descriptive")

    evidence = evaluate(spec, lab=lab)
    print(f"\npower: {evidence.power}")
    if evidence.capacity is not None:
        print(f"capacity (Sharpe halves): {evidence.capacity['capacity (Sharpe halves)']:,.0f} USD; "
              f"(participation limit): {evidence.capacity['capacity (participation limit)']:,.0f} USD")


if __name__ == "__main__":
    main()
