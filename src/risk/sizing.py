"""Position sizing: how big each new position is, as a share of equity.

Every sizer answers one question: given the market and the account at the
moment of entry, what share of equity should this position be? 0.25 means a
position worth 25% of equity; 1.5 means 1.5x equity (leverage, perps only).
Returning a share, never a unit count, keeps sizing independent of the coin's
price, so the same setup works for BTC at 80,000 and DOGE at 0.10. The
`RiskManager` then applies the caps (position limit, per-trade notional,
exchange capacity), and `PaperTradingEngine` converts the share into units at
the entry price. That conversion happens in one place only.

Sizers, by name (build with `build_sizer(name, **params)`):

- `fixed_fraction`: always the same share, e.g. `{"fraction": 0.1}`. The simplest to reason about.
- `fixed_notional`: always the same amount of money, e.g. `{"notional": 50}` in
  the account currency. For small live tests that must clear an exchange
  minimum.
- `vol_target`: bigger when markets are calm, smaller when turbulent, e.g.
  `{"target_annual_vol": 0.5}`. Share = target / EWMA volatility forecast.
  Research (docs/research_log.md, 2026-09-26) found it helps MA crossover and
  long/short Keltner, not breakout long-only rules.
- `atr_risk`: lose about `risk_fraction` of equity if a stop `atr_multiplier`
  ATRs away is hit, e.g. `{"risk_fraction": 0.01, "atr_multiplier": 2}`.
  Meant to be paired with the same `atr_stop_multiplier` in `RiskControlConfig`.
- `kelly`: sized from the strategy's own closed-trade returns: a fraction of the
  growth-optimal Kelly leverage, mean / mean-square of the trade returns. Until
  `min_trades` trades exist it uses `fallback_fraction`. A research estimate
  can be passed as a prior (`prior_mean`, `prior_std`, `prior_trades`),
  because daily strategies trade too rarely to estimate Kelly from live trades
  alone. It declines with reason `kelly_no_edge` when the estimated edge is
  zero or negative.

A sizer that can't size (not enough history, no edge) returns a share of 0
with a `reason`, and the entry is refused with that reason rather than
silently sized to zero.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class SizingContext:
    """What a sizer may look at when a position is about to open.

    Attributes:
        bars: Bar history up to and including the decision bar (oldest first).
        equity: Account equity now, in the account currency.
        price: The expected entry price (the decision bar's close).
        side: "buy" for a long, "sell" for a short, None if unknown.
        trade_returns: Closed round trips of this strategy, oldest first, as
            the return on the position's notional (0.05 = the price moved 5%
            in the position's favour).
    """

    bars: Sequence[Any]
    equity: float
    price: float
    side: str | None = None
    trade_returns: Sequence[float] = ()


@dataclass(frozen=True, slots=True)
class SizingResult:
    """A sizer's answer: the share of equity, and why it is zero when it is."""

    fraction: float
    reason: str | None = None
    details: dict[str, float] = field(default_factory=dict)


class PositionSizer(Protocol):
    """Anything with a `name` and a `size(context) -> SizingResult` method can size positions."""

    name: str

    def size(self, context: SizingContext) -> SizingResult:
        """Return the share of equity for a new position."""
        ...


def _field(bar: Any, name: str) -> Any:
    if hasattr(bar, name):
        return getattr(bar, name)
    if isinstance(bar, dict):
        return bar.get(name)
    return None


def bar_seconds(bars: Sequence[Any]) -> float | None:
    """Median spacing of the bars' timestamps in seconds, or None without timestamps."""
    stamps = [_field(bar, "timestamp") for bar in bars[-50:]]
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(stamps, stamps[1:]) if earlier is not None and later is not None]
    gaps = [gap for gap in gaps if gap > 0.0]
    return float(np.median(gaps)) if gaps else None


def ewma_annual_volatility(bars: Sequence[Any], *, halflife_days: float, min_returns: int = 20) -> float | None:
    """Annualised EWMA volatility of the bars' log returns, or None without enough history.

    The half-life is in days, so the forecast means the same on 1m, 4h or 1d
    bars; the bar length is read from the timestamps. Same estimator as
    `src.research.volatility.ewma_vol` (zero-mean, weights halving every
    half-life), truncated after eight half-lives.
    """
    seconds = bar_seconds(bars)
    if seconds is None:
        return None
    halflife_bars = max(1.0, halflife_days * 86_400.0 / seconds)
    window = list(bars[-(int(8 * halflife_bars) + 2) :])
    closes = np.array([float(_field(bar, "close")) for bar in window], dtype=float)
    valid = (closes[1:] > 0.0) & (closes[:-1] > 0.0)
    returns = np.log(closes[1:][valid] / closes[:-1][valid])
    if len(returns) < min_returns:
        return None
    weights = 0.5 ** (np.arange(len(returns))[::-1] / halflife_bars)
    variance = float(np.sum(weights * returns**2) / np.sum(weights))
    return float(np.sqrt(variance * 365.0 * 86_400.0 / seconds))


def average_true_range(bars: Sequence[Any], window: int) -> float | None:
    """Mean true range of the last `window` bars, or None without enough bars."""
    recent = list(bars[-(window + 1) :])
    if len(recent) < 2:
        return None
    ranges = []
    for previous, current in zip(recent, recent[1:]):
        high, low, previous_close = float(_field(current, "high")), float(_field(current, "low")), float(_field(previous, "close"))
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return float(np.mean(ranges))


@dataclass(frozen=True, slots=True)
class FixedFractionSizer:
    """The same share of equity for every position."""

    fraction: float = 0.1
    name: str = "fixed_fraction"

    def __post_init__(self) -> None:
        """Reject shares that can't be a position size."""
        if not 0.0 < self.fraction <= 10.0:
            raise ValueError("fraction must be above 0 (0.1 = 10% of equity)")

    def size(self, context: SizingContext) -> SizingResult:
        """Return `fraction`."""
        return SizingResult(self.fraction)


@dataclass(frozen=True, slots=True)
class FixedNotionalSizer:
    """The same amount of money for every position, in the account currency."""

    notional: float = 100.0
    name: str = "fixed_notional"

    def __post_init__(self) -> None:
        """Reject non-positive amounts."""
        if self.notional <= 0.0:
            raise ValueError("notional must be above 0")

    def size(self, context: SizingContext) -> SizingResult:
        """Return notional / equity."""
        if context.equity <= 0.0:
            return SizingResult(0.0, reason="no_equity")
        return SizingResult(self.notional / context.equity, details={"notional": self.notional})


@dataclass(frozen=True, slots=True)
class VolatilityTargetSizer:
    """Size so the position's forecast annualised volatility is `target_annual_vol`."""

    target_annual_vol: float = 0.5
    halflife_days: float = 10.0
    min_returns: int = 20
    name: str = "vol_target"

    def __post_init__(self) -> None:
        """Keep the target and half-life in sensible ranges."""
        if not 0.0 < self.target_annual_vol <= 3.0:
            raise ValueError("target_annual_vol must be above 0 and at most 3 (0.5 = 50% a year)")
        if self.halflife_days <= 0.0:
            raise ValueError("halflife_days must be positive")

    def size(self, context: SizingContext) -> SizingResult:
        """Return target / EWMA forecast, or 0 with a reason when there is too little history."""
        forecast = ewma_annual_volatility(context.bars, halflife_days=self.halflife_days, min_returns=self.min_returns)
        if forecast is None or forecast <= 0.0:
            return SizingResult(0.0, reason="volatility_forecast_unavailable")
        return SizingResult(self.target_annual_vol / forecast, details={"annual_volatility_forecast": forecast})


@dataclass(frozen=True, slots=True)
class AtrRiskSizer:
    """Size so a stop `atr_multiplier` ATRs away loses about `risk_fraction` of equity."""

    risk_fraction: float = 0.01
    atr_multiplier: float = 2.0
    atr_window: int = 14
    name: str = "atr_risk"

    def __post_init__(self) -> None:
        """Keep the risk budget and stop distance positive."""
        if not 0.0 < self.risk_fraction <= 0.2:
            raise ValueError("risk_fraction must be above 0 and at most 0.2 (0.01 = lose 1% of equity at the stop)")
        if self.atr_multiplier <= 0.0 or self.atr_window < 1:
            raise ValueError("atr_multiplier and atr_window must be positive")

    def size(self, context: SizingContext) -> SizingResult:
        """Return risk_fraction / (stop distance as a share of price)."""
        atr = average_true_range(context.bars, self.atr_window)
        if atr is None or atr <= 0.0 or context.price <= 0.0:
            return SizingResult(0.0, reason="atr_unavailable")
        stop_distance = self.atr_multiplier * atr / context.price
        return SizingResult(self.risk_fraction / stop_distance, details={"stop_distance_pct": stop_distance})


@dataclass(frozen=True, slots=True)
class KellySizer:
    """A fraction of the Kelly leverage estimated from the strategy's own trade returns.

    For per-trade returns r on a fully sized position, the growth-optimal
    share of equity is about mean(r) / mean(r^2). Full Kelly is far too
    aggressive when the mean is estimated from few trades, so `kelly_fraction`
    (0.5 = "half Kelly") scales it down and `max_fraction` caps it.

    Attributes:
        kelly_fraction: Share of the Kelly estimate to use.
        min_trades: Trades needed (live trades plus `prior_trades`) before
            Kelly is used; until then `fallback_fraction` applies.
        fallback_fraction: Share used while there are too few trades.
        max_fraction: Upper limit on the resulting share.
        lookback_trades: Only the most recent trades count.
        prior_mean, prior_std, prior_trades: A research estimate of the
            strategy's trade returns, counted as `prior_trades` extra trades
            with that mean and spread.
    """

    kelly_fraction: float = 0.5
    min_trades: int = 20
    fallback_fraction: float = 0.1
    max_fraction: float = 1.0
    lookback_trades: int = 100
    prior_mean: float | None = None
    prior_std: float | None = None
    prior_trades: int = 0
    name: str = "kelly"

    def __post_init__(self) -> None:
        """Validate the fractions and the prior."""
        if not 0.0 < self.kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be above 0 and at most 1 (0.5 = half Kelly)")
        if self.fallback_fraction < 0.0 or self.max_fraction <= 0.0:
            raise ValueError("fallback_fraction must be >= 0 and max_fraction > 0")
        if self.prior_trades and (self.prior_mean is None or self.prior_std is None):
            raise ValueError("prior_trades needs prior_mean and prior_std")

    def size(self, context: SizingContext) -> SizingResult:
        """Return the scaled Kelly share, the fallback while trades are few, or 0 when there is no edge."""
        returns = np.asarray(list(context.trade_returns)[-self.lookback_trades :], dtype=float)
        returns = returns[np.isfinite(returns)]
        count = len(returns) + self.prior_trades
        if count < self.min_trades or count == 0:
            if self.fallback_fraction <= 0.0:
                return SizingResult(0.0, reason="kelly_too_few_trades", details={"trades": float(count)})
            return SizingResult(self.fallback_fraction, details={"trades": float(count), "kelly_fallback": 1.0})
        total = float(returns.sum())
        total_squares = float(np.sum(returns**2))
        if self.prior_trades:
            total += self.prior_trades * float(self.prior_mean)
            total_squares += self.prior_trades * (float(self.prior_std) ** 2 + float(self.prior_mean) ** 2)
        mean, mean_square = total / count, total_squares / count
        if mean <= 0.0 or mean_square <= 0.0:
            return SizingResult(0.0, reason="kelly_no_edge", details={"trade_mean": mean, "trades": float(count)})
        full_kelly = mean / mean_square
        fraction = min(self.max_fraction, self.kelly_fraction * full_kelly)
        return SizingResult(fraction, details={"full_kelly": full_kelly, "trade_mean": mean, "trades": float(count)})


SIZERS: dict[str, type] = {
    "fixed_fraction": FixedFractionSizer,
    "fixed_notional": FixedNotionalSizer,
    "vol_target": VolatilityTargetSizer,
    "atr_risk": AtrRiskSizer,
    "kelly": KellySizer,
}


def sizer_parameters(name: str) -> dict[str, Any]:
    """The parameters a sizer accepts, with their defaults."""
    if name not in SIZERS:
        raise ValueError(f"unknown sizing {name!r}; choose from {', '.join(SIZERS)}")
    signature = inspect.signature(SIZERS[name])
    return {key: parameter.default for key, parameter in signature.parameters.items() if key != "name"}


def build_sizer(name: str, **params: Any) -> PositionSizer:
    """Build a sizer by name. Unknown names or parameters raise, listing what is accepted."""
    accepted = sizer_parameters(name)
    unknown = sorted(set(params) - set(accepted))
    if unknown:
        raise ValueError(f"sizing {name!r} does not take {unknown}; it takes {sorted(accepted)}")
    return SIZERS[name](**params)


def describe_sizers() -> str:
    """One line per sizer with its parameters and defaults, for help text."""
    return "\n".join(f"{name}: " + ", ".join(f"{key}={value}" for key, value in sizer_parameters(name).items()) for name in SIZERS)
