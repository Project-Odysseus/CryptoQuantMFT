"""The component interfaces every strategy is assembled from (plan 2.1).

A strategy is: signals (per instrument, or across instruments) -> optionally a regime model -> an overlay or combiner
-> a target signal per instrument, in forecast units (quantlab.conventions: unit variance, capped). The engine then
sizes, allocates, nets, caps, executes and costs it. Components never see data after the bar they decide on, and never
trade: they only say what they would like to hold.

The protocols are deliberately small so a notebook lambda can be a component:

    momentum = signal("mom_60", lambda bars: np.sign(bars.close.pct_change(60)), warmup=60)

`Targets` carries the target signal plus named parts (e.g. the trend and range contributions) and diagnostics (the
regime state per bar), so attribution and reports can explain every position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, runtime_checkable

import pandas as pd

from quantlab.data.panel import Panel


@runtime_checkable
class Signal(Protocol):
    """One instrument's bars (OHLCV, indexed by bar) -> a forecast per bar, known at that bar's close."""

    name: str
    warmup: int

    def __call__(self, bars: pd.DataFrame) -> pd.Series: ...


@runtime_checkable
class PanelSignal(Protocol):
    """All instruments at once (cross-sectional signals): a panel -> forecasts (dates x instruments)."""

    name: str
    warmup: int

    def __call__(self, panel: Panel) -> pd.DataFrame: ...


@runtime_checkable
class RegimeModel(Protocol):
    """One instrument's bars -> per-bar state probabilities (columns = states, rows sum to 1), plus optional extra
    columns a matching overlay may read. `states` names the probability columns; `lag` is the model's decision lag."""

    name: str
    states: tuple[str, ...]
    lag: int

    def __call__(self, bars: pd.DataFrame) -> pd.DataFrame: ...


@dataclass(frozen=True)
class FunctionSignal:
    """A signal from a plain function (the notebook way)."""

    name: str
    function: Callable[[pd.DataFrame], pd.Series]
    warmup: int = 0

    def __call__(self, bars: pd.DataFrame) -> pd.Series:
        out = self.function(bars)
        out = pd.Series(out, index=bars.index) if not isinstance(out, pd.Series) else out
        if self.warmup:
            out = out.copy()
            out.iloc[: self.warmup] = float("nan")
        return out.rename(self.name)


def signal(name: str, function: Callable[[pd.DataFrame], pd.Series], *, warmup: int = 0) -> FunctionSignal:
    """Wrap a function of one instrument's bars as a Signal."""
    return FunctionSignal(name, function, warmup)


@dataclass
class Targets:
    """What a strategy wants to hold, per instrument, in forecast units, with its parts and diagnostics.

    Attributes:
        target: dates x instruments target signal.
        parts: name -> dates x instruments; the parts add up to `target` where the strategy is linear in them (they
            are used to attribute P&L, proportionally, when it is not).
        diagnostics: name -> dates x instruments (e.g. "state": the regime state), for reports only.
    """

    target: pd.DataFrame
    parts: dict[str, pd.DataFrame] = field(default_factory=dict)
    diagnostics: dict[str, pd.DataFrame] = field(default_factory=dict)


def per_instrument(panel: Panel, compute: Callable[[str, pd.DataFrame], pd.DataFrame | pd.Series]) -> dict[str, pd.DataFrame]:
    """Run `compute(instrument, bars)` for every instrument; returns column name -> dates x instruments.

    `compute` returns a Series (one column, "target") or a DataFrame of named columns for that instrument. Bars where an
    instrument is not alive (before its first or after its last bar) are passed through as NaN rows trimmed away.
    """
    columns: dict[str, dict[str, pd.Series]] = {}
    for instrument in panel.instruments:
        bars = panel.bars(instrument).dropna(subset=["close"])
        if bars.empty:
            continue
        out = compute(instrument, bars)
        frame = out.to_frame("target") if isinstance(out, pd.Series) else out
        for column in frame.columns:
            columns.setdefault(column, {})[instrument] = frame[column]
    return {column: pd.DataFrame(values).reindex(panel.index) for column, values in columns.items()}


def apply_signal(signal: Signal | PanelSignal, panel: Panel) -> pd.DataFrame:
    """A signal over every instrument of a panel: dates x instruments."""
    if isinstance(signal, FunctionSignal) or not _is_panel_signal(signal):
        return per_instrument(panel, lambda _instrument, bars: signal(bars))["target"]
    return signal(panel)


def _is_panel_signal(signal: object) -> bool:
    import inspect

    try:
        parameter = next(iter(inspect.signature(signal.__call__).parameters.values()))  # type: ignore[attr-defined]
    except (StopIteration, ValueError, TypeError):
        return False
    return parameter.annotation in (Panel, "Panel")


def combine_targets(parts: Mapping[str, pd.DataFrame], weights: Mapping[str, float] | None = None, *, cap: float = 1.0) -> Targets:
    """A static weighted sum of signals (equal weights by default), clipped to +/-cap, with each weighted part kept."""
    weights = dict(weights or {name: 1.0 / len(parts) for name in parts})
    weighted = {name: frame.fillna(0.0) * weights[name] for name, frame in parts.items()}
    total = sum(weighted.values())
    return Targets(total.clip(-cap, cap), weighted)
