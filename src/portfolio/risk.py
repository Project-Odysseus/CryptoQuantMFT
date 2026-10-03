"""Portfolio risk overlay: limits on the whole book, applied after netting and before any order.

Sleeves only know about themselves. This layer sees every instrument at once
and enforces, in order:

1. stale instruments (no fresh bar) may shrink but not grow;
2. instruments that can't be shorted (spot) are clamped at 0;
3. the per-instrument cap;
4. group caps (`[risk.groups.<name>]`: the gross and the net exposure of the
   instruments tagged with that group, e.g. all altcoins), and the delta cap
   per underlying coin (`[risk.exposure] max_delta`: BTC spot and the BTC perp
   are one exposure);
5. per-venue caps (a venue's instruments scaled down together);
6. the net cap, then the gross cap, then the money cap `max_gross_notional`,
   then the scenario cap (`[risk.exposure] max_scenario_loss`: the loss if
   every coin moves 50% against the book), everything scaled down together;
7. the correlation-aware caps, from the book's risk model (`risk_model.py`):
   the beta cap (`max_beta_exposure`: the book's net exposure in benchmark
   terms, so 1.0x BTC plus 0.5x ETH counts as ~1.5x BTC, not as two bets) and
   the volatility cap (`max_portfolio_vol`: the book's annualised volatility at
   the larger of the estimated and the crash correlations). Both only ever
   scale the book down. Until the model has enough history they can't be
   checked, and positions may then only shrink (`risk_model_unavailable`);
8. drawdown de-risking (off unless `drawdown_derisk_start` is set): all
   targets scaled down linearly once the drawdown from the equity peak passes
   `drawdown_derisk_start`, reaching `drawdown_derisk_floor` at `max_drawdown`;
9. halts: at `max_drawdown` everything is flattened, and past the
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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.portfolio.risk_model import RiskEstimate

GROUP_LIMIT_KEYS = frozenset({"max_gross", "max_net"})
SCENARIO_MOVE = 0.5  # the largest move of the exposure scenario grid (exposure_limits.SPOT_MOVES)


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
    # After this many rejected orders in a row on one instrument, stop sending orders that add to it for
    # `rejection_cooldown_hours` (reductions still go out). Hours, not minutes: the book decides once per bar (4h), so
    # a shorter pause would end before the next decision. A fill resets the count. 0 switches it off.
    rejection_cooldown_after: int = 3
    rejection_cooldown_hours: float = 24.0
    # A reduce-only stop resting on the exchange for every open perp position, this far (a fraction of price) beyond
    # the price when the position last changed: the loss cap if the process or machine dies. None: no exchange stops.
    exchange_stop_pct: float | None = None
    # Limits on what the book is exposed to, per underlying, as shares of equity ([risk.exposure]; see exposure_limits.py)
    exposure: dict[str, float] = field(default_factory=dict)
    # Caps per instrument group ([risk.groups.<name>] with max_gross and/or max_net, shares of equity). An instrument
    # joins a group with `group = "<name>"` in its [instruments] table.
    groups: dict[str, dict[str, float]] = field(default_factory=dict)
    # The risk model (risk_model.py): an EWMA covariance of the instruments' grid-bar returns
    benchmark: str | None = None  # the instrument betas are measured against (reports default to the book's BTC instrument)
    max_beta_exposure: float | None = None  # |sum of weight x beta to the benchmark|, a share of equity
    max_portfolio_vol: float | None = None  # the book's annualised volatility (0.4 = 40%), at stressed correlations
    vol_halflife_days: float = 20.0
    vol_min_days: float = 20.0  # history an instrument (or a pair) needs before its own estimate is used
    correlation_shrinkage: float = 0.2  # share of `prior_correlation` in every estimated correlation
    prior_correlation: float = 0.5  # assumed for a pair without history
    stress_correlation: float = 0.9  # correlations are floored here for the volatility cap; 0 switches the stress off
    # How alike the strategies are, from their returns over this window: alerts only, nothing is resized
    correlation_lookback_days: int = 90
    max_average_correlation: float | None = None  # alert when the sleeves' average pairwise correlation is above this
    min_effective_bets: float | None = None  # alert when the sleeves are worth fewer independent bets than this

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
        if self.exchange_stop_pct is not None and not 0.01 <= self.exchange_stop_pct <= 0.9:
            raise ValueError("risk.exchange_stop_pct must be between 0.01 and 0.9 (a fraction of price), or left out")
        if self.rejection_cooldown_after < 0 or not self.rejection_cooldown_hours > 0:
            raise ValueError("risk.rejection_cooldown_after must be 0 (off) or more, and risk.rejection_cooldown_hours above 0")
        if self.max_gross_notional is not None and not self.max_gross_notional > 0:
            raise ValueError("risk.max_gross_notional must be above 0 (money, in the base currency)")
        for name in ("max_beta_exposure", "max_portfolio_vol", "max_average_correlation", "min_effective_bets"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0):
                raise ValueError(f"risk.{name} must be a number above 0, or left out")
        if self.max_beta_exposure is not None and not self.benchmark:
            raise ValueError("risk.max_beta_exposure needs risk.benchmark (the instrument betas are measured against)")
        if not self.vol_halflife_days > 0 or not self.vol_min_days > 0 or self.correlation_lookback_days < 2:
            raise ValueError("risk.vol_halflife_days and risk.vol_min_days must be above 0, and risk.correlation_lookback_days at least 2")
        if not 0 <= self.correlation_shrinkage <= 1 or not -1 < self.prior_correlation < 1 or not 0 <= self.stress_correlation < 1:
            raise ValueError("risk.correlation_shrinkage must be between 0 and 1, risk.prior_correlation between -1 and 1, "
                             "and risk.stress_correlation at least 0 (off) and below 1")
        for group, limits in self.groups.items():
            if not isinstance(limits, dict) or not limits or set(limits) - GROUP_LIMIT_KEYS:
                raise ValueError(f"risk.groups.{group} takes {sorted(GROUP_LIMIT_KEYS)} (at least one)")
            for key, value in limits.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                    raise ValueError(f"risk.groups.{group}.{key} must be a number above 0 (a share of equity)")
        from src.portfolio.exposure_limits import EXPOSURE_LIMIT_KEYS

        for key, value in self.exposure.items():
            if key not in EXPOSURE_LIMIT_KEYS:
                raise ValueError(f"risk.exposure.{key} is unknown; allowed: {sorted(EXPOSURE_LIMIT_KEYS)}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"risk.exposure.{key} must be a number above 0 (a share of equity)")

    @property
    def needs_risk_model(self) -> bool:
        """Whether a limit depends on the risk model (it is then stepped in research backtests too)."""
        return self.max_beta_exposure is not None or self.max_portfolio_vol is not None


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
    groups: Mapping[str, str] | None = None,
    underlyings: Mapping[str, str] | None = None,
    estimate: RiskEstimate | None = None,
) -> tuple[dict[str, float], list[RiskAction]]:
    """Apply the portfolio limits to net `targets` (instrument id to weight).

    Args:
        venues: Instrument id to venue name.
        can_short: Instrument id to whether it may go short (False for spot).
        current: Current weights per instrument (for stale holds and halts).
        equity, peak_equity, day_start_equity: Account state for drawdown rules.
        stale: Instruments without a fresh bar.
        groups: Instrument id to its group, for `config.groups`.
        underlyings: Instrument id to the coin it moves with, for `config.exposure`.
        estimate: The risk model's snapshot, for the beta and volatility caps.
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
    for group, limits in config.groups.items():
        members = [i for i in out if (groups or {}).get(i) == group]
        if "max_gross" in limits:
            scale_group("group_gross_cap", members, float(limits["max_gross"]))
        if "max_net" in limits:
            scale_group("group_net_cap", members, float(limits["max_net"]), net=True)
    if "max_delta" in config.exposure and underlyings:
        for coin in sorted({underlyings[i] for i in out if i in underlyings}):
            scale_group("delta_cap", [i for i in out if underlyings.get(i) == coin], float(config.exposure["max_delta"]), net=True)
    for venue, limit in config.max_venue_exposure.items():
        scale_group("venue_cap", [i for i in out if venues.get(i) == venue], float(limit))
    scale_group("net_cap", list(out), config.max_net_exposure, net=True)
    scale_group("gross_cap", list(out), config.max_gross_exposure)
    if config.max_gross_notional is not None and equity > 0:
        scale_group("notional_cap", list(out), config.max_gross_notional / equity)
    if "max_scenario_loss" in config.exposure:  # linear positions: every coin moving SCENARIO_MOVE against the net
        scale_group("scenario_cap", list(out), float(config.exposure["max_scenario_loss"]) / SCENARIO_MOVE, net=True)

    if config.needs_risk_model:
        if estimate is None:
            for instrument in list(out):
                change("risk_model_unavailable", instrument, _only_shrink(out[instrument], current.get(instrument, 0.0)))
        else:
            if config.max_beta_exposure is not None:
                beta = abs(estimate.beta_exposure(out, config.benchmark))
                if beta > config.max_beta_exposure:
                    for instrument in list(out):
                        change("beta_cap", instrument, out[instrument] * config.max_beta_exposure / beta)
            if config.max_portfolio_vol is not None:
                volatility = estimate.stressed_volatility(out)
                if volatility > config.max_portfolio_vol:
                    for instrument in list(out):
                        change("vol_cap", instrument, out[instrument] * config.max_portfolio_vol / volatility)

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


def array_overlay(instruments: Sequence[str], *, config: PortfolioRiskConfig, venues: Mapping[str, str], can_short: Mapping[str, bool],
                  groups: Mapping[str, str] | None = None, underlyings: Mapping[str, str] | None = None,
                  estimates: Callable[[int], RiskEstimate | None] | None = None):
    """`apply_portfolio_risk` as a `simulate_portfolio(adjust_targets=...)` hook for research backtests.

    `estimates(index)` returns the risk model's snapshot at grid bar `index` (from bars up to it), for the beta
    and volatility caps; see `book_risk.estimate_stream`.
    """
    names = list(instruments)

    def adjust(row: np.ndarray, *, equity: float, peak_equity: float, day_start_equity: float, current_weights: np.ndarray | None = None,
               index: int = 0, **_: object) -> np.ndarray:
        current = dict(zip(names, current_weights)) if current_weights is not None else None
        adjusted, _actions = apply_portfolio_risk(
            dict(zip(names, row)), config=config, venues=venues, can_short=can_short,
            current=current, equity=equity, peak_equity=peak_equity, day_start_equity=day_start_equity,
            groups=groups, underlyings=underlyings, estimate=estimates(index) if estimates is not None else None,
        )
        return np.array([adjusted[name] for name in names])

    return adjust
