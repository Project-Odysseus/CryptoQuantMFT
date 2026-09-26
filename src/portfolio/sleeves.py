"""Sleeves: one strategy on one instrument, turned into a target weight bar by bar.

A sleeve's target weight is a signed share of equity it would hold if it had
the whole portfolio to itself (0.8 = long 80% of equity). Allocation
(`allocation.py`) later scales it by the sleeve's budget.

`SleeveRunner.step` is the single place a sleeve decides, one completed bar at
a time, in this order:
1. the re-entry gate (`gate_reentry`): after a stop, the stopped side waits
   until the signal has left it;
2. sleeve stops (ATR, time, drawdown), when configured;
3. an exit when the signal goes flat or flips;
4. an entry, sized by the sleeve's sizer at the entry bar and then held at that
   size until the position closes. This is what the runtime does, and research
   found resizing open positions hurt.

Research runs the same runner over history (`run_sleeve`), and the runtime
calls `step` once per new bar, so both make identical decisions by
construction. `SleeveState` holds everything a sleeve remembers between bars
and round-trips through JSON for restarts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from src.risk.controls import RiskControlConfig, RiskManager, gate_reentry
from src.risk.sizing import PositionSizer, SizingContext, build_sizer

STOP_KEYS = ("atr_stop_multiplier", "atr_window", "time_stop_bars", "position_drawdown_stop_pct")


@dataclass(frozen=True, slots=True)
class SleeveSpec:
    """What a sleeve trades and how (one `[[sleeves]]` block of a portfolio config).

    Attributes:
        id: Unique slug; also the `strategy_id` on its orders and events.
        instrument: "<venue>:<symbol>", e.g. "kraken_futures:BTC/USD".
        interval: Bar length, e.g. "1d" or "4h".
        strategy: A strategy name from the registry.
        params: The strategy's parameters.
        long_only: Drop the strategy's short signals.
        budget: The sleeve's share of the portfolio (see `allocation.py`).
        sizing, sizing_params: A sizer from `src/risk/sizing.py`; its share is the
            sleeve's weight at entry.
        stops: Optional exits: `atr_stop_multiplier`, `atr_window`, `time_stop_bars`,
            `position_drawdown_stop_pct`.
        warmup_bars: History to load before the first decision.
        enabled: A disabled sleeve holds nothing.
    """

    id: str
    instrument: str
    interval: str
    strategy: str
    params: dict[str, Any] = field(default_factory=dict)
    long_only: bool = False
    budget: float = 1.0
    sizing: str = "fixed_fraction"
    sizing_params: dict[str, Any] = field(default_factory=lambda: {"fraction": 1.0})
    stops: dict[str, Any] = field(default_factory=dict)
    warmup_bars: int = 200
    enabled: bool = True

    @property
    def venue(self) -> str:
        """The part of the instrument id before the colon."""
        return self.instrument.split(":", 1)[0]

    @property
    def symbol(self) -> str:
        """The part of the instrument id after the colon."""
        return self.instrument.split(":", 1)[1]


@dataclass(slots=True)
class SleeveState:
    """What a sleeve remembers between bars. Plain values only, so it serialises to JSON."""

    weight: float = 0.0
    entry_price: float | None = None
    bars_held: int = 0
    reentry_block: str | None = None
    last_signal: float = 0.0
    trade_returns: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready copy."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SleeveState":
        """Rebuild from `to_dict` output."""
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class SleeveDecision:
    """What a step did and why, for logs and reports."""

    action: str  # hold | enter | exit | flip | stop | blocked | declined | flat
    weight: float
    reason: str | None = None
    details: dict[str, float] = field(default_factory=dict)


class _Prefix(Sequence):
    """The first `end` items of a list without copying it (bar-by-bar loops over long histories)."""

    def __init__(self, items: Sequence[Any], end: int) -> None:
        self._items, self._end = items, end

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            start, stop, step = key.indices(self._end)
            if step < 0:  # indices() gives stop=-1 for "down to 0", which a list would read as its last item
                return [self._items[index] for index in range(start, stop, step)]
            return self._items[start:stop:step]
        if key < 0:
            key += self._end
        if not 0 <= key < self._end:
            raise IndexError(key)
        return self._items[key]


def _close(bar: Any) -> float:
    return float(bar.close if hasattr(bar, "close") else bar["close"])


class SleeveRunner:
    """Turns a sleeve's signals into target weights, one completed bar at a time."""

    def __init__(self, spec: SleeveSpec) -> None:
        """Build the strategy, the sizer and (if stops are configured) a risk manager for exits."""
        from src.research.catalog import build_strategy

        self.spec = spec
        self.strategy = build_strategy(spec.strategy, **dict(spec.params), long_only=spec.long_only)
        self.sizer: PositionSizer = build_sizer(spec.sizing, **dict(spec.sizing_params))
        unknown = sorted(set(spec.stops) - set(STOP_KEYS))
        if unknown:
            raise ValueError(f"sleeve {spec.id}: unknown stops {unknown}; use {list(STOP_KEYS)}")
        self._exits = RiskManager(RiskControlConfig(**spec.stops)) if spec.stops else None

    def signals(self, bars: Sequence[Any]) -> np.ndarray:
        """The strategy's signal (-1/0/1) at every bar, using data up to each bar only."""
        values = np.asarray(self.strategy.signal_series(list(bars)), dtype=float)
        return np.sign(np.nan_to_num(values))

    def step(self, state: SleeveState, bars: Sequence[Any], signal: float) -> tuple[SleeveState, SleeveDecision]:
        """Decide at the close of `bars[-1]` given the strategy's `signal` there; returns the new state and why."""
        if not self.spec.enabled:
            return SleeveState(trade_returns=list(state.trade_returns)), SleeveDecision("flat", 0.0, "sleeve_disabled")
        price = _close(bars[-1])
        new = SleeveState(
            weight=state.weight, entry_price=state.entry_price, bars_held=state.bars_held,
            reentry_block=state.reentry_block, last_signal=float(signal), trade_returns=list(state.trade_returns),
        )
        gated, new.reentry_block, blocked = gate_reentry(float(signal), state.reentry_block)
        side = 1.0 if new.weight > 0 else -1.0 if new.weight < 0 else 0.0

        if side != 0.0:
            new.bars_held += 1
            if self._exits is not None:
                decision = self._exits.evaluate_exit(
                    bars=bars, current_bar=bars[-1], position_side="long" if side > 0 else "short",
                    avg_entry_price=new.entry_price, bars_held=new.bars_held,
                )
                if decision.force_exit:
                    self._close_position(new, price)
                    new.reentry_block = "long" if side > 0 else "short"
                    return new, SleeveDecision("stop", 0.0, decision.reason)
            if gated == side:
                return new, SleeveDecision("hold", new.weight)
            self._close_position(new, price)
            if gated == 0.0:
                return new, SleeveDecision("exit", 0.0, "signal_flat")

        if gated == 0.0:
            return new, SleeveDecision("blocked" if blocked else "flat", 0.0, "reentry_after_stop" if blocked else None)
        sized = self.sizer.size(SizingContext(bars=bars, equity=1.0, price=price, side="buy" if gated > 0 else "sell", trade_returns=new.trade_returns))
        if sized.fraction <= 0.0:
            return new, SleeveDecision("declined", 0.0, sized.reason or "sizing_zero", dict(sized.details))
        new.weight, new.entry_price, new.bars_held = gated * sized.fraction, price, 0
        return new, SleeveDecision("flip" if side != 0.0 else "enter", new.weight, None, dict(sized.details))

    @staticmethod
    def _close_position(state: SleeveState, price: float) -> None:
        if state.entry_price and price > 0:
            direction = 1.0 if state.weight > 0 else -1.0
            state.trade_returns.append(direction * (price / state.entry_price - 1.0))
            del state.trade_returns[:-1000]
        state.weight, state.entry_price, state.bars_held = 0.0, None, 0


@dataclass(slots=True)
class SleeveRun:
    """A sleeve replayed over history: its signal, target weight and decision at every bar."""

    signals: np.ndarray
    weights: np.ndarray
    decisions: list[SleeveDecision]
    state: SleeveState


def run_sleeve(spec: SleeveSpec, bars: Sequence[Any], *, state: SleeveState | None = None) -> SleeveRun:
    """Replay `spec` over `bars` with the same `SleeveRunner.step` the runtime uses."""
    runner = SleeveRunner(spec)
    bars = list(bars)
    signals = runner.signals(bars)
    state = state or SleeveState()
    weights = np.zeros(len(bars))
    decisions: list[SleeveDecision] = []
    for index in range(len(bars)):
        state, decision = runner.step(state, _Prefix(bars, index + 1), signals[index])
        weights[index] = state.weight
        decisions.append(decision)
    return SleeveRun(signals=signals, weights=weights, decisions=decisions, state=state)
