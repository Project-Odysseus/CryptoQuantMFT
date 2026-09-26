"""Portfolio risk overlay: limits on the whole book, applied after netting and before any order.

Sleeves only know about themselves. This layer sees every instrument at once
and enforces, in order:

1. stale instruments (no fresh bar) may shrink but not grow;
2. instruments that can't be shorted (spot) are clamped at 0;
3. the per-instrument cap;
4. per-venue caps (a venue's instruments scaled down together);
5. the net cap, then the gross cap, then the money cap `max_gross_notional`
   (everything scaled down together);
6. drawdown de-risking (off unless `drawdown_derisk_start` is set): all
   targets scaled down linearly once the drawdown from the equity peak passes
   `drawdown_derisk_start`, reaching `drawdown_derisk_floor` at `max_drawdown`;
7. halts: at `max_drawdown` everything is flattened, and past the
   `daily_loss_limit` positions may only shrink until the next UTC day.

The `max_drawdown` halt is a kill, not a pause. A flat book's drawdown can't
recover, so it stays flat until a person resets the equity peak. Set it above
the book's worst historical drawdown. The 2026-09-26 portfolio study
(docs/research_log.md) found that de-risking lowered Sharpe for trend sleeves,
which recover from drawdowns by trending again, so it is off by default.

Every change is returned as a `RiskAction` with its rule, so the dashboard and
logs can say why a target isn't what the sleeves asked for. The same function
runs in research (via `simulate_portfolio(adjust_targets=...)`) and in the
runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True, slots=True)
class PortfolioRiskConfig:
    """Limits on the whole book (the `[risk]` section of a portfolio config). Shares are of portfolio equity."""

    max_gross_exposure: float = 1.5
    max_net_exposure: float = 1.0
    max_instrument_weight: float = 1.0
    max_venue_exposure: dict[str, float] = field(default_factory=dict)
    max_drawdown: float = 0.25
    daily_loss_limit: float | None = 0.05
    drawdown_derisk_start: float | None = None
    drawdown_derisk_floor: float = 0.5
    stale_after_bars: int = 2
    max_gross_notional: float | None = None  # total position value cap in money (base currency); required for live trading

    def __post_init__(self) -> None:
        """Reject limits that can't work together."""
        for name in ("max_gross_exposure", "max_net_exposure", "max_instrument_weight", "max_drawdown"):
            if getattr(self, name) <= 0:
                raise ValueError(f"risk.{name} must be above 0")
        if self.drawdown_derisk_start is not None and not 0 < self.drawdown_derisk_start < self.max_drawdown:
            raise ValueError("risk.drawdown_derisk_start must be above 0 and below risk.max_drawdown")
        if not 0 <= self.drawdown_derisk_floor <= 1:
            raise ValueError("risk.drawdown_derisk_floor must be between 0 and 1")
        if self.daily_loss_limit is not None and not 0 < self.daily_loss_limit < 1:
            raise ValueError("risk.daily_loss_limit must be above 0 and below 1 (0.05 = 5% of the day's starting equity)")
        for venue, limit in self.max_venue_exposure.items():
            if not limit > 0:
                raise ValueError(f"risk.max_venue_exposure.{venue} must be above 0")
        if self.stale_after_bars < 1:
            raise ValueError("risk.stale_after_bars must be at least 1")
        if self.max_gross_notional is not None and not self.max_gross_notional > 0:
            raise ValueError("risk.max_gross_notional must be above 0 (money, in the base currency)")


@dataclass(frozen=True, slots=True)
class RiskAction:
    """One change the overlay made to a target."""

    rule: str
    instrument: str | None
    before: float
    after: float


def _only_shrink(target: float, current: float) -> float:
    """The closest weight to `target` that doesn't add risk relative to `current` (same side, no bigger)."""
    if current == 0.0 or np.sign(target) != np.sign(current):
        return 0.0
    return float(np.sign(current) * min(abs(target), abs(current)))


def drawdown_multiplier(equity: float, peak_equity: float, config: PortfolioRiskConfig) -> float:
    """1 below the de-risk start, falling linearly to the floor at `max_drawdown`, and 0 at or beyond it."""
    if peak_equity <= 0:
        return 1.0
    drawdown = max(0.0, 1.0 - equity / peak_equity)
    if drawdown >= config.max_drawdown:
        return 0.0
    start = config.drawdown_derisk_start
    if start is None or drawdown <= start:
        return 1.0
    progress = (drawdown - start) / (config.max_drawdown - start)
    return 1.0 - progress * (1.0 - config.drawdown_derisk_floor)


def apply_portfolio_risk(
    targets: Mapping[str, float],
    *,
    config: PortfolioRiskConfig,
    venues: Mapping[str, str],
    can_short: Mapping[str, bool],
    current: Mapping[str, float] | None = None,
    equity: float = 1.0,
    peak_equity: float = 1.0,
    day_start_equity: float | None = None,
    stale: Sequence[str] = (),
) -> tuple[dict[str, float], list[RiskAction]]:
    """Apply the portfolio limits to net `targets` (instrument id to weight).

    Args:
        venues: Instrument id to venue name.
        can_short: Instrument id to whether it may go short (False for spot).
        current: Current weights per instrument (for stale holds and halts).
        equity, peak_equity, day_start_equity: Account state for drawdown rules.
        stale: Instruments without a fresh bar.
    """
    current = dict(current or {})
    out = {instrument: float(weight) for instrument, weight in targets.items()}
    actions: list[RiskAction] = []

    def change(rule: str, instrument: str | None, new: float) -> None:
        if instrument is not None and not np.isclose(out[instrument], new):
            actions.append(RiskAction(rule, instrument, out[instrument], new))
            out[instrument] = new

    def scale_group(rule: str, members: list[str], limit: float, *, net: bool = False) -> None:
        exposure = abs(sum(out[m] for m in members)) if net else sum(abs(out[m]) for m in members)
        if exposure > limit > 0:
            factor = limit / exposure
            for member in members:
                change(rule, member, out[member] * factor)

    for instrument in stale:
        if instrument in out:
            change("stale_instrument", instrument, _only_shrink(out[instrument], current.get(instrument, 0.0)))
    for instrument, weight in list(out.items()):
        if weight < 0 and not can_short.get(instrument, True):
            change("no_short", instrument, 0.0)
    for instrument, weight in list(out.items()):
        if abs(weight) > config.max_instrument_weight:
            change("instrument_cap", instrument, float(np.sign(weight) * config.max_instrument_weight))
    for venue, limit in config.max_venue_exposure.items():
        scale_group("venue_cap", [i for i in out if venues.get(i) == venue], float(limit))
    scale_group("net_cap", list(out), config.max_net_exposure, net=True)
    scale_group("gross_cap", list(out), config.max_gross_exposure)
    if config.max_gross_notional is not None and equity > 0:
        scale_group("notional_cap", list(out), config.max_gross_notional / equity)

    multiplier = drawdown_multiplier(equity, peak_equity, config)
    if multiplier == 0.0:
        for instrument in list(out):
            change("max_drawdown_halt", instrument, 0.0)
    elif multiplier < 1.0:
        for instrument in list(out):
            change("drawdown_derisk", instrument, out[instrument] * multiplier)
    if config.daily_loss_limit is not None and day_start_equity and day_start_equity > 0:
        if 1.0 - equity / day_start_equity > config.daily_loss_limit:
            for instrument in list(out):
                change("daily_loss_halt", instrument, _only_shrink(out[instrument], current.get(instrument, 0.0)))
    return out, actions


def array_overlay(instruments: Sequence[str], *, config: PortfolioRiskConfig, venues: Mapping[str, str], can_short: Mapping[str, bool]):
    """`apply_portfolio_risk` as a `simulate_portfolio(adjust_targets=...)` hook for research backtests."""
    names = list(instruments)

    def adjust(row: np.ndarray, *, equity: float, peak_equity: float, day_start_equity: float, current_weights: np.ndarray | None = None, **_: object) -> np.ndarray:
        current = dict(zip(names, current_weights)) if current_weights is not None else None
        adjusted, _actions = apply_portfolio_risk(
            dict(zip(names, row)), config=config, venues=venues, can_short=can_short,
            current=current, equity=equity, peak_equity=peak_equity, day_start_equity=day_start_equity,
        )
        return np.array([adjusted[name] for name in names])

    return adjust
