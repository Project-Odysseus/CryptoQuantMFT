"""The regime card: on real data, does a regime model's state tell which strategy will earn more next? (plan 3.4)

A regime model earns its place in an overlay only if its state, known at the close of t, predicts the difference
between the strategies it switches between over the following days. The card measures that and the model's behaviour:

- **edge**: mean forward `horizon`-bar P&L of strategy A minus strategy B on bars labelled A's state, minus the same
  on bars labelled B's state. Positive = the label points the right way. A circular block bootstrap gives its interval.
- **placebo**: the same edge with the labels shifted in time (keeping their durations and frequencies); the share of
  shifts that do as well is the p-value. A model that only describes the past, or tracks a slow drift, fails here.
- **behaviour**: share of time per state, switches a year, mean and median durations, share of spells under 5 bars.
- **lag after events**: bars from a fresh 60-bar breakout until the model says A's state (median).
- **redundancy**: rank correlation between P(A's state) and |a signal| it is meant to add to (e.g. the trend signal).

Pass marks are in `RegimeStandard` (version 1, fixed before use): the interval excludes zero, placebo p <= 0.05, and
at least 20 spells of each state. Verdicts: "useful", "weak" (one of interval/placebo), "no evidence", "too few spells".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from quantlab.regimes.models import labels as most_likely


@dataclass(frozen=True)
class RegimeStandard:
    """Pass marks for a regime card (version 1)."""

    version: int = 1
    level: float = 0.95
    placebo_p: float = 0.05
    min_spells: int = 20
    block: int = 60
    runs: int = 1000
    placebo_runs: int = 500
    placebo_min_shift: int = 250


@dataclass
class RegimeCard:
    """The checks of one regime model on one market."""

    name: str
    verdict: str
    reason: str
    table: pd.DataFrame
    behaviour: pd.DataFrame
    details: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        return "\n".join([f"# Regime card: {self.name}", "", f"**{self.verdict}**: {self.reason}", "", self.table.to_markdown() if hasattr(self.table, "to_markdown") else str(self.table),
                          "", self.behaviour.round(2).to_string(), ""])


def _spells(states: pd.Series) -> pd.DataFrame:
    valid = states.dropna()
    change = valid.ne(valid.shift())
    ids = change.cumsum()
    grouped = valid.groupby(ids)
    return pd.DataFrame({"state": grouped.first(), "days": grouped.size()})


def _edge(label_values: np.ndarray, difference: np.ndarray, a: str, b: str) -> float:
    in_a, in_b = label_values == a, label_values == b
    if not in_a.any() or not in_b.any():
        return float("nan")
    return float(np.nanmean(difference[in_a]) - np.nanmean(difference[in_b]))


def regime_card(probabilities: pd.DataFrame, pnl_a: pd.Series, pnl_b: pd.Series, *, state_a: str, state_b: str, name: str = "", horizon: int = 20,
                bars: pd.DataFrame | None = None, redundancy_signal: pd.Series | None = None, standard: RegimeStandard | None = None, seed: int = 0) -> RegimeCard:
    """Every check (see the module docstring). `pnl_a`/`pnl_b`: daily net returns of the two strategies the model chooses
    between (A should win in `state_a`)."""
    standard = standard or RegimeStandard()
    states = most_likely(probabilities)
    difference = (pnl_a - pnl_b).reindex(states.index)
    forward = difference[::-1].rolling(horizon, min_periods=horizon).sum()[::-1].shift(-1)  # bars t+1 .. t+horizon, after the label at t
    frame = pd.DataFrame({"label": states, "forward": forward}).dropna()
    label_values, forward_values = frame["label"].to_numpy(dtype=object), frame["forward"].to_numpy(dtype=float)
    edge = _edge(label_values, forward_values, state_a, state_b)

    rng = np.random.default_rng(seed)
    count = len(frame)
    block = max(1, min(standard.block, count))
    draws = []
    for _ in range(standard.runs):
        starts = rng.integers(0, count, size=int(np.ceil(count / block)))
        index = ((starts[:, None] + np.arange(block)[None, :]).ravel()[:count]) % count
        draws.append(_edge(label_values[index], forward_values[index], state_a, state_b))
    tail = (1.0 - standard.level) / 2.0
    low, high = np.nanquantile(draws, [tail, 1.0 - tail])
    shifts = rng.integers(min(standard.placebo_min_shift, count // 4), max(min(standard.placebo_min_shift, count // 4) + 1, count - standard.placebo_min_shift),
                          size=standard.placebo_runs)
    placebo = np.array([_edge(np.roll(label_values, int(shift)), forward_values, state_a, state_b) for shift in shifts])
    placebo_p = float((1 + np.sum(placebo >= edge)) / (1 + len(placebo)))

    spells = _spells(states)
    years = states.notna().sum() / 252.0
    behaviour = pd.DataFrame({state: {"share of bars": float((states == state).mean()), "spells": int((spells["state"] == state).sum()),
                                      "mean days": float(spells.loc[spells["state"] == state, "days"].mean()),
                                      "median days": float(spells.loc[spells["state"] == state, "days"].median()),
                                      "spells under 5 days": float((spells.loc[spells["state"] == state, "days"] < 5).mean())}
                              for state in probabilities.columns}).T
    switches = float((len(spells) - 1) / years) if years > 0 else float("nan")

    lag = float("nan")
    if bars is not None:
        previous_high = bars["high"].rolling(60).max().shift(1)
        previous_low = bars["low"].rolling(60).min().shift(1)
        fresh = (bars["close"] > previous_high) & ~(bars["close"].shift(1) > previous_high.shift(1)) | (bars["close"] < previous_low) & ~(bars["close"].shift(1) < previous_low.shift(1))
        lags = []
        values = states.to_numpy(dtype=object)
        for position in np.flatnonzero(fresh.reindex(states.index).fillna(False).to_numpy()):
            ahead = np.flatnonzero(values[position:position + 60] == state_a)
            if len(ahead):
                lags.append(int(ahead[0]))
        lag = float(np.median(lags)) if lags else float("nan")
    redundancy = float(probabilities[state_a].corr(redundancy_signal.abs().reindex(probabilities.index), method="spearman")) if redundancy_signal is not None else float("nan")

    enough = all(int((spells["state"] == state).sum()) >= standard.min_spells for state in (state_a, state_b))
    interval_ok, placebo_ok = bool(low > 0), placebo_p <= standard.placebo_p
    if not enough:
        verdict, reason = "too few spells", f"fewer than {standard.min_spells} spells of {state_a} or {state_b}"
    elif interval_ok and placebo_ok:
        verdict, reason = "useful", f"the label predicts the next {horizon} bars' winner, beyond chance and beyond its timing placebo"
    elif interval_ok or placebo_ok:
        verdict, reason = "weak", "only one of the interval and the placebo passes"
    else:
        verdict, reason = "no evidence", f"edge {edge:+.4f} is not distinguishable from zero or from shifted labels"
    table = pd.DataFrame({"value": {f"edge: A-B next {horizon} bars, {state_a} minus {state_b}": f"{edge:+.4f} ({low:+.4f} to {high:+.4f})",
                                    "placebo p (shifted labels)": f"{placebo_p:.3f}", "switches a year": f"{switches:.1f}",
                                    "median bars from a breakout to the A state": f"{lag:.0f}" if np.isfinite(lag) else "-",
                                    "rank correlation with |signal|": f"{redundancy:.2f}" if np.isfinite(redundancy) else "-"}})
    return RegimeCard(name, verdict, reason, table, behaviour, {"edge": edge, "interval": (float(low), float(high)), "placebo_p": placebo_p, "placebo": placebo,
                                                                 "switches": switches, "lag": lag, "redundancy": redundancy, "standard": standard})
