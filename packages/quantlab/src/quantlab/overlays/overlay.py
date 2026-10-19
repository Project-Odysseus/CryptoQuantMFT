"""Overlays: any signals, any regime model, weights per state (plan 3.5).

    overlay = RegimeOverlay(signals={"trend": lib.ewmac(16, 64), "reversion": lib.zscore_reversion(40)},
                            regime=PercentileRegime("abs_slope_t"),
                            weights={"TREND": {"trend": 1.0}, "RANGE": {"reversion": 1.0}})
    targets = overlay(panel)                         # Targets: target, parts per signal, state probabilities
    candidates = benchmarks(overlay, panel)          # what it has to beat, computed the same way

Each signal's weight at a bar is the probability-weighted average of its weight in each state, so hard regimes
(probability 1) switch and soft ones blend. `gross_cap` limits the sum of |weighted signals| (H14 capped it at 0.5 in
TRANSITION); `cap` clips the target.

`benchmarks` builds the comparisons every overlay must beat (plan 3.5): the static blend (each signal at its average
weight over states), the hard switch (all weight on the most probable state), each signal alone, and the
exposure-matched static blend (the overlay's |target| path with the static blend's direction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from quantlab.data.panel import Panel
from quantlab.engine.components import Signal, Targets, per_instrument
from quantlab.regimes.models import labels, one_hot


@dataclass
class RegimeOverlay:
    """Signals weighted by regime-state probabilities (see the module docstring)."""

    signals: Mapping[str, Signal]
    regime: object  # a RegimeModel
    weights: Mapping[str, Mapping[str, float]]  # state -> signal -> weight
    cap: float = 1.0
    gross_cap: float | None = None
    name: str = "overlay"
    hard: bool = False  # use only the most probable state

    def _state_weights(self, probabilities: pd.DataFrame) -> dict[str, pd.Series]:
        probabilities = probabilities.copy()
        if self.hard:
            probabilities = one_hot(labels(probabilities), tuple(probabilities.columns))
        out = {}
        for signal_name in self.signals:
            weight = pd.Series(0.0, index=probabilities.index)
            for state in probabilities.columns:
                weight = weight + probabilities[state].fillna(0.0) * float(self.weights.get(state, {}).get(signal_name, 0.0))
            out[signal_name] = weight.where(probabilities.notna().all(axis=1), 0.0)
        return out

    def instrument(self, bars: pd.DataFrame) -> pd.DataFrame:
        probabilities = self.regime(bars)
        weights = self._state_weights(probabilities)
        parts = {name: signal(bars).fillna(0.0) * weights[name] for name, signal in self.signals.items()}
        total = sum(parts.values())
        if self.gross_cap is not None:
            gross = sum(part.abs() for part in parts.values())
            scale = (self.gross_cap / gross.where(gross > self.gross_cap)).fillna(1.0)
            parts = {name: part * scale for name, part in parts.items()}
            total = sum(parts.values())
        out = pd.DataFrame({"target": total.clip(-self.cap, self.cap), **{f"part:{name}": part for name, part in parts.items()},
                            **{f"weight:{name}": weight for name, weight in weights.items()}, **{f"p:{state}": probabilities[state] for state in probabilities.columns}})
        return out

    def __call__(self, panel: Panel) -> Targets:
        columns = per_instrument(panel, lambda _instrument, bars: self.instrument(bars))
        parts = {name.split(":", 1)[1]: frame for name, frame in columns.items() if name.startswith("part:")}
        diagnostics = {name: frame for name, frame in columns.items() if name.startswith(("weight:", "p:"))}
        return Targets(columns["target"], parts, diagnostics)


def static_weights(overlay: RegimeOverlay) -> dict[str, float]:
    """Each signal's average weight over the overlay's states."""
    states = list(overlay.weights)
    return {name: float(np.mean([overlay.weights[state].get(name, 0.0) for state in states])) for name in overlay.signals}


def benchmarks(overlay: RegimeOverlay, panel: Panel, *, primary: Targets | None = None) -> dict[str, Targets]:
    """The comparisons an overlay must beat, as Targets (see the module docstring)."""
    from quantlab.engine.components import apply_signal

    primary = primary or overlay(panel)
    signals = {name: apply_signal(signal, panel).fillna(0.0) for name, signal in overlay.signals.items()}
    static = static_weights(overlay)
    static_parts = {name: frame * static[name] for name, frame in signals.items()}
    static_target = sum(static_parts.values()).clip(-overlay.cap, overlay.cap)
    hard = RegimeOverlay(overlay.signals, overlay.regime, overlay.weights, overlay.cap, overlay.gross_cap, f"{overlay.name} hard", hard=True)
    out = {"static blend": Targets(static_target, static_parts), "hard switch": hard(panel),
           "exposure-matched static": Targets(primary.target.abs() * np.sign(static_target))}
    for name, frame in signals.items():
        out[f"{name} alone"] = Targets(frame.clip(-overlay.cap, overlay.cap), {name: frame})
    return out
