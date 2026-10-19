"""The research engine: target signals in, positions, P&L and attribution out (plan 2.2-2.6).

Every study runs through `run_backtest`, so every result is built with exactly the same execution timing, sizing,
banding and costs. The steps, per instrument and bar:

1. **Size.** position = target signal x sizer scale x weight, clipped to +/-`max_position`. The sizer is volatility
   targeting by default: scale = target_vol / annualised EWMA volatility, optionally capped (`Sizer.cap`).
2. **Portfolio limits.** Gross, net and per-instrument caps scale positions down (never up), applied at decision time.
3. **Band.** The held position changes only when the new one differs by more than `band`; with `band_exits_to_zero`
   a move to exactly zero always goes through (a strategy that says "flat" gets flat).
4. **Execution.** Decided at the close of t; traded at the close of t+1 (`Execution.NEXT_CLOSE`), so it earns from
   t+2. The engine applies the lag: a strategy cannot trade on the price that produced its signal.
5. **P&L and costs.** gross = held position x the instrument's return (excess of cash for futures, per the returns
   given). Costs: traded notional x the instrument's cost per side, plus a roll cost on the held notional
   (`rolls_per_year` x `roll_bps`), all times `cost_multiplier`. Optional square-root market impact when capital and
   volume are given.
6. **Attribution.** P&L by instrument, long/short, and by the target's named parts (proportional to each part's share
   of the target at decision time).

Index proxies earn the returns of their proxy; the caller passes excess returns (quantlab.engine.returns).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd

from quantlab.conventions import Execution
from quantlab.engine.components import Targets
from quantlab.signals.forecast import ewma_volatility


@dataclass(frozen=True)
class Sizer:
    """Volatility targeting: scale = target_vol / annualised EWMA volatility (span bars), capped at `cap` if given.

    `target_vol=None` means no volatility scaling (scale 1: the target is already a weight).
    """

    target_vol: float | None = 0.10
    span: int = 20
    cap: float | None = None
    floor_annual: float = 0.05
    periods: float = 252.0

    def scale(self, returns: pd.DataFrame) -> pd.DataFrame:
        if self.target_vol is None:
            return pd.DataFrame(1.0, index=returns.index, columns=returns.columns)
        out = {}
        for column in returns.columns:
            sigma = ewma_volatility(returns[column].to_numpy(dtype=float), span=self.span, floor_annual=self.floor_annual) * math.sqrt(self.periods)
            out[column] = self.target_vol / sigma
        frame = pd.DataFrame(out, index=returns.index)
        return frame.clip(upper=self.cap) if self.cap is not None else frame


@dataclass(frozen=True)
class Limits:
    """Portfolio limits on decided positions (fractions of equity). None = no limit."""

    max_gross: float | None = None
    max_net: float | None = None
    max_instrument: float | None = None


@dataclass(frozen=True)
class EngineConfig:
    """How positions become trades and P&L."""

    execution: Execution = Execution.NEXT_CLOSE
    band: float = 0.02
    band_exits_to_zero: bool = True
    max_position: float = 2.0
    cost_multiplier: float = 1.0
    rolls_per_year: float = 4.0
    roll_bps: float = 1.0
    periods: float = 252.0
    impact_coefficient: float = 0.0  # square-root impact: bps = coefficient x 1e4 x sqrt(traded notional / daily volume notional)
    capital: float | None = None  # needed for impact (and lot rounding reports)


@dataclass
class BacktestResult:
    """Daily series per instrument and their totals."""

    targets: pd.DataFrame
    decided: pd.DataFrame
    held: pd.DataFrame
    traded: pd.DataFrame
    gross: pd.DataFrame
    costs: pd.DataFrame
    returns: pd.DataFrame
    parts_pnl: dict[str, pd.DataFrame] = field(default_factory=dict)
    diagnostics: dict[str, pd.DataFrame] = field(default_factory=dict)
    periods: float = 252.0

    @property
    def net(self) -> pd.DataFrame:
        return self.gross - self.costs

    @property
    def pnl(self) -> pd.Series:
        """Portfolio net return per bar (positions are fractions of equity, so instrument P&L adds)."""
        return self.net.sum(axis=1)

    @property
    def gross_pnl(self) -> pd.Series:
        return self.gross.sum(axis=1)

    def window(self, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None) -> "BacktestResult":
        cut = lambda frame: frame.loc[start:end]  # noqa: E731
        return BacktestResult(cut(self.targets), cut(self.decided), cut(self.held), cut(self.traded), cut(self.gross), cut(self.costs), cut(self.returns),
                              {name: cut(frame) for name, frame in self.parts_pnl.items()}, {name: cut(frame) for name, frame in self.diagnostics.items()},
                              self.periods)


def _band(sized: np.ndarray, band: float, exits_to_zero: bool) -> np.ndarray:
    decided = np.zeros_like(sized)
    current = np.zeros(sized.shape[1])
    for row, values in enumerate(sized):
        move = np.abs(values - current) > band
        if exits_to_zero:
            move |= (values == 0.0) & (current != 0.0)
        current = np.where(move, values, current)
        decided[row] = current
    return decided


def _apply_limits(positions: pd.DataFrame, limits: Limits) -> pd.DataFrame:
    out = positions
    if limits.max_instrument is not None:
        out = out.clip(-limits.max_instrument, limits.max_instrument)
    scale = pd.Series(1.0, index=out.index)
    if limits.max_gross is not None:
        gross = out.abs().sum(axis=1)
        scale = np.minimum(scale, (limits.max_gross / gross.where(gross > 0)).fillna(1.0).clip(upper=1.0))
    if limits.max_net is not None:
        net = out.sum(axis=1).abs()
        scale = np.minimum(scale, (limits.max_net / net.where(net > 0)).fillna(1.0).clip(upper=1.0))
    return out.mul(scale, axis=0)


def run_backtest(targets: Targets | pd.DataFrame, returns: pd.DataFrame, *, costs_bps: Mapping[str, float] | pd.DataFrame | float = 1.0,
                 sizer: Sizer | None = None, weights: Mapping[str, float] | float = 1.0, limits: Limits | None = None, config: EngineConfig | None = None,
                 scale: pd.DataFrame | None = None, volume_notional: pd.DataFrame | None = None) -> BacktestResult:
    """Positions, trades, P&L and costs for target signals (see the module docstring).

    Args:
        targets: Target signals (dates x instruments), or a Targets with parts and diagnostics.
        returns: Each instrument's return per bar (excess of cash for futures), same shape.
        costs_bps: Cost per side in basis points of traded notional: one number, per instrument, or per bar.
        sizer: Volatility targeting (default 10%); `Sizer(target_vol=None)` for targets that are already weights.
        weights: A multiplier per instrument (e.g. 1/3 each when three markets share a book).
        limits: Portfolio limits on decided positions.
        scale: A precomputed sizer scale (dates x instruments), instead of `sizer`.
        volume_notional: Daily traded notional per instrument, for the impact model.
    """
    config = config or EngineConfig()
    if config.execution is not Execution.NEXT_CLOSE:
        raise NotImplementedError("only NEXT_CLOSE execution is implemented (decide at the close of t, trade at the close of t+1)")
    bundle = targets if isinstance(targets, Targets) else Targets(targets)
    target = bundle.target.reindex(returns.index).astype(float)
    columns = list(target.columns)
    returns = returns.reindex(columns=columns).astype(float)
    sizer_scale = scale.reindex(index=target.index, columns=columns) if scale is not None else (sizer or Sizer()).scale(returns)
    weight = pd.Series(weights if isinstance(weights, Mapping) else {column: float(weights) for column in columns}).reindex(columns).fillna(0.0)

    sized = (target.fillna(0.0) * sizer_scale.fillna(0.0)).mul(weight, axis=1).clip(-config.max_position, config.max_position)
    if limits is not None:
        sized = _apply_limits(sized, limits)
    decided = pd.DataFrame(_band(sized.to_numpy(), config.band, config.band_exits_to_zero), index=target.index, columns=columns)
    lag = 2  # NEXT_CLOSE: traded at the close of t+1, earning from t+2
    held = decided.shift(lag).fillna(0.0)
    traded = decided.diff().abs().fillna(decided.abs()).shift(lag - 1).fillna(0.0)
    gross = held * returns.fillna(0.0)

    if isinstance(costs_bps, pd.DataFrame):
        cost_rate = costs_bps.reindex(index=target.index, columns=columns).ffill().fillna(0.0) / 10_000.0
    else:
        per_side = pd.Series(costs_bps if isinstance(costs_bps, Mapping) else {column: float(costs_bps) for column in columns}).reindex(columns).fillna(0.0)
        cost_rate = pd.DataFrame(np.tile(per_side.to_numpy() / 10_000.0, (len(target), 1)), index=target.index, columns=columns)
    costs = traded * cost_rate + held.abs() * config.rolls_per_year * config.roll_bps / 10_000.0 / config.periods
    if config.impact_coefficient and config.capital and volume_notional is not None:
        participation = (traded * config.capital / volume_notional.reindex(index=target.index, columns=columns)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        costs = costs + traded * config.impact_coefficient * np.sqrt(participation)
    costs = costs * config.cost_multiplier

    parts_pnl: dict[str, pd.DataFrame] = {}
    if bundle.parts:
        total = target.where(target.abs() > 1e-12)
        for name, part in bundle.parts.items():
            share = (part.reindex(index=target.index, columns=columns) / total).fillna(0.0)
            parts_pnl[name] = gross * share.shift(lag).fillna(0.0)
    return BacktestResult(target, decided, held, traded, gross, costs, returns, parts_pnl, bundle.diagnostics, config.periods)
