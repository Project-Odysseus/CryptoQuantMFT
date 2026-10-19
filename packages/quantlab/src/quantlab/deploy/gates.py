"""Promotion gates: what a strategy spec must show to move from research to paper, and from paper to live (plan 7.2).

    evidence = evaluate(spec)                                  # backtest, strategy card, power, costs, capacity
    report = promotion_check(spec, "paper", evidence=evidence, budget=RiskBudget(max_drawdown=0.20))
    report.table                                               # one row per gate: required?, passes?, detail
    sign_off(spec, "paper", by="owner")                        # the owner's sign-off, recorded with the spec's hash
    promote(spec, "paper", evidence=evidence, approved_by="owner", overrides={"cards": "why"})

| gate | paper | live |
| --- | --- | --- |
| cards: the strategy card's verdict | not "no evidence" / "short history" | "evidence of an edge" |
| power: the test could detect the expected Sharpe (pass probability >= 0.5) | reported | required |
| holdout: the hypothesis had its one look at the frozen holdout | reported | required |
| costs: Sharpe still positive at twice the assumed costs | required | required |
| capacity: the planned capital is within capacity | required | required |
| risk budget: target volatility and 1.5 x the backtest's worst drawdown within the budget | required | required |
| sign-off: the owner signed this exact spec (hash) for this stage | required | required |

A failing required gate blocks `promote` unless it is overridden with a written reason; overrides are recorded with
the promotion, so a book that runs despite a gate says so. Every check and promotion is written to the lab ledger.
`promoted(id, version, stage)` is what the host's portfolio check asks before a book may run the spec.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from quantlab.deploy.spec import StrategySpec, backtest_spec, spec_lab
from quantlab.validation import ledger

STAGES = ("paper", "live")
REQUIRED = {"cards": ("paper", "live"), "power": ("live",), "holdout": ("live",), "costs": ("paper", "live"), "capacity": ("paper", "live"),
            "risk budget": ("paper", "live"), "sign-off": ("paper", "live")}


@dataclass(frozen=True)
class RiskBudget:
    """What the book can give this strategy: its volatility and the drawdown it may reach."""

    max_volatility: float = 0.15
    max_drawdown: float = 0.30
    capital_usd: float = 1e6


@dataclass
class Evidence:
    """What `evaluate` measured on a spec; the gates read it."""

    spec_key: str
    spec_hash: str
    result: Any
    card: Any
    power: dict[str, Any]
    sharpe_double_costs: float
    capacity: dict[str, Any] | None
    years: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateReport:
    """One row per gate (see the module docstring) and whether the stage may go ahead."""

    spec_key: str
    stage: str
    table: pd.DataFrame

    @property
    def ok(self) -> bool:
        required = self.table[self.table["required"]]
        return bool(required["passes"].all())

    @property
    def failing(self) -> list[str]:
        required = self.table[self.table["required"]]
        return list(required.index[~required["passes"].astype(bool)])

    def __repr__(self) -> str:
        state = "may be promoted" if self.ok else f"blocked by {', '.join(self.failing)}"
        return f"GateReport({self.spec_key} -> {self.stage}: {state})"


def evaluate(spec: StrategySpec, *, lab: Any = None, sizes: tuple[float, ...] = (1e6, 1e7, 1e8, 1e9), **lab_options: Any) -> Evidence:
    """Backtest the spec, card it (noted in the ledger under its hypothesis), and measure power, costs and capacity."""
    from quantlab.analytics.performance import metrics
    from quantlab.deploy.capacity import capacity_curve
    from quantlab.validation.power import power_report
    from quantlab.validation.strategy_card import card_backtest

    lab = lab or spec_lab(spec, **lab_options)
    result = backtest_spec(spec, lab=lab)
    card = card_backtest(result, name=spec.key, baseline=lab.hold(), family=spec.strategy.hypothesis or spec.id)
    years = len(result.pnl.dropna()) / lab.engine.periods
    expected = spec.expectations.shrunk_sharpe if spec.expectations.shrunk_sharpe is not None else card.details.get("numbers", {}).get("shrunk_sharpe", 0.0)
    trials = max(ledger.total_trials(spec.strategy.hypothesis), 1) if spec.strategy.hypothesis else 1
    power = power_report(years, float(expected or 0.0), trials=trials)
    doubled = metrics(lab.backtest(lab.signal(spec.signal_fn(), name=spec.key, normalize=False, check=False), cost_multiplier=2.0)).get("Sharpe", float("nan"))
    capacity = None
    if spec.capacity.adv_usd:
        capacity = capacity_curve(lab, lab.signal(spec.signal_fn(), name=spec.key, normalize=False, check=False), spec.capacity.adv_usd, sizes=sizes,
                                  impact_coefficient=spec.capacity.impact_coefficient, max_participation=spec.capacity.max_participation)
    return Evidence(spec.key, spec.hash, result, card, power, float(doubled), capacity, years)


def _signoffs(spec: StrategySpec, stage: str) -> list[dict[str, Any]]:
    return [row for row in ledger.entries("signoff") if row.get("spec") == spec.key and row.get("hash") == spec.hash and row.get("stage") == stage]


def sign_off(spec: StrategySpec, stage: str, *, by: str, note: str = "") -> dict[str, Any]:
    """Record the owner's sign-off of this exact spec (its hash) for `stage`."""
    if stage not in STAGES or not by.strip():
        raise ValueError(f"stage must be one of {STAGES}, and say who signs")
    return ledger.append({"type": "signoff", "spec": spec.key, "hash": spec.hash, "stage": stage, "by": by, "note": note})


def promotion_check(spec: StrategySpec, stage: str, *, evidence: Evidence, budget: RiskBudget | None = None) -> GateReport:
    """Every gate for `stage` (see the module docstring); logged to the ledger."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    if evidence.spec_hash != spec.hash:
        raise ValueError("the evidence was measured on another version of the spec: evaluate it again")
    budget = budget or RiskBudget()
    rows: dict[str, dict[str, Any]] = {}
    verdict = evidence.card.verdict
    cards_ok = verdict == "evidence of an edge" if stage == "live" else verdict not in ("no evidence", "short history")
    rows["cards"] = {"passes": cards_ok, "detail": f"strategy card: {verdict} ({evidence.card.reason})"}
    probability = float(evidence.power.get("probability of passing", float("nan")))
    rows["power"] = {"passes": bool(probability >= 0.5), "detail": f"pass probability {probability:.0%} for a Sharpe of {evidence.power.get('hypothesised Sharpe')} "
                                                                     f"over {evidence.years:.0f} years; detectable {evidence.power.get('detectable Sharpe (80% power)', float('nan')):.2f}"}
    looks = ledger.holdout_looks(f"{spec.strategy.hypothesis} final") if spec.strategy.hypothesis else []
    rows["holdout"] = {"passes": bool(looks), "detail": f"looked on {looks[-1]['at'][:10]}" if looks else "no look at the frozen holdout yet"}
    rows["costs"] = {"passes": bool(evidence.sharpe_double_costs > 0), "detail": f"Sharpe {evidence.sharpe_double_costs:.2f} at twice the assumed costs"}
    if evidence.capacity is not None:
        limit = min(evidence.capacity["capacity (Sharpe halves)"], evidence.capacity["capacity (participation limit)"])
        rows["capacity"] = {"passes": bool(budget.capital_usd <= limit), "detail": f"capital {budget.capital_usd:,.0f} USD; capacity {limit:,.0f} USD" if math.isfinite(limit)
                            else f"capital {budget.capital_usd:,.0f} USD; no limit reached up to the largest size tested"}
    else:
        rows["capacity"] = {"passes": False, "detail": "no daily traded values in the spec's [capacity]"}
    from quantlab.analytics.performance import metrics

    numbers = metrics(evidence.result)
    worst = abs(numbers.get("max drawdown", float("nan")))
    vol_ok = spec.sizing.target_vol <= budget.max_volatility
    dd_ok = 1.5 * worst <= budget.max_drawdown
    rows["risk budget"] = {"passes": bool(vol_ok and dd_ok), "detail": f"target volatility {spec.sizing.target_vol:.0%} (budget {budget.max_volatility:.0%}); "
                                                                         f"1.5 x worst drawdown {1.5 * worst:.1%} (budget {budget.max_drawdown:.1%})"}
    signed = _signoffs(spec, stage)
    rows["sign-off"] = {"passes": bool(signed), "detail": f"signed by {signed[-1]['by']} on {signed[-1]['at'][:10]}" if signed else "not signed for this stage and hash"}
    table = pd.DataFrame(rows).T
    table.insert(0, "required", [stage in REQUIRED[gate] for gate in table.index])
    table["passes"] = table["passes"].astype(bool)
    report = GateReport(spec.key, stage, table)
    ledger.append({"type": "promotion_check", "spec": spec.key, "hash": spec.hash, "stage": stage, "ok": report.ok, "failing": report.failing,
                   "gates": {gate: bool(row["passes"]) for gate, row in table.iterrows()}})
    return report


def promote(spec: StrategySpec, stage: str, *, evidence: Evidence, approved_by: str, budget: RiskBudget | None = None,
            overrides: Mapping[str, str] | None = None, store: Any = None) -> dict[str, Any]:
    """Promote the spec to `stage` if every required gate passes or is overridden with a reason (all recorded)."""
    report = promotion_check(spec, stage, evidence=evidence, budget=budget)
    overrides = {gate: reason for gate, reason in (overrides or {}).items() if reason and reason.strip()}
    blocking = [gate for gate in report.failing if gate not in overrides]
    if "sign-off" in report.failing:
        blocking = sorted(set(blocking) | {"sign-off"})  # the owner's sign-off cannot be overridden
    if blocking:
        raise PermissionError(f"{spec.key} cannot go to {stage}: {', '.join(blocking)} fail. Fix them, or override a gate with a written reason.")
    record = ledger.append({"type": "promotion", "spec": spec.key, "id": spec.id, "version": spec.version, "hash": spec.hash, "stage": stage,
                            "approved_by": approved_by, "overrides": dict(overrides), "card": evidence.card.verdict})
    if store is not None and spec.strategy.hypothesis and spec.strategy.hypothesis in store.hypotheses().index:
        store.set_status(spec.strategy.hypothesis, stage, reason=f"{spec.key} promoted to {stage} by {approved_by}"
                         + (f"; overrides: {overrides}" if overrides else ""))
    return record


def promoted(spec_id: str, version: int, stage: str = "paper") -> dict[str, Any] | None:
    """The latest promotion of this spec version to `stage` or beyond (live counts as paper), else None."""
    wanted = {"paper": ("paper", "live"), "live": ("live",)}[stage]
    rows = [row for row in ledger.entries("promotion") if row.get("id") == spec_id and int(row.get("version", -1)) == int(version) and row.get("stage") in wanted]
    return rows[-1] if rows else None
