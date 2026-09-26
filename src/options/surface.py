"""The market's volatility surface: an SVI smile per expiry, joined across expiries, usable as a `PricingModel`.

Raw SVI (Gatheral) gives total implied variance w = sigma^2 T at log-moneyness
k = ln(K / F) as

    w(k) = a + b (rho (k - m) + sqrt((k - m)^2 + s^2))

Five numbers describe a whole smile: the level (a), the wing slope (b), the
skew (rho), the shift (m) and the ATM curvature (s). It fits liquid crypto
smiles well and extrapolates the wings linearly, as theory requires.

`SVISurface` joins the fitted slices. Between two expiries it interpolates
total variance linearly in time at the same moneyness (which keeps calendar
spreads non-negative when the slices don't cross). The price is Black-76 at
the surface's volatility. This is what risk should use (greeks and scenarios
that agree with market prices), and it is the baseline any model's fit is
compared with.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from src.options.pricing import Black76


@dataclass(frozen=True, slots=True)
class SVISlice:
    """One expiry's smile in raw SVI parameters, trusted between the quoted log-moneyness `k_low` and `k_high`."""

    t: float
    a: float
    b: float
    rho: float
    m: float
    s: float
    k_low: float = -math.inf
    k_high: float = math.inf

    def total_variance(self, k: float | np.ndarray) -> float | np.ndarray:
        """w(k) = a + b (rho (k - m) + sqrt((k - m)^2 + s^2))."""
        shifted = np.asarray(k, dtype=float) - self.m
        return self.a + self.b * (self.rho * shifted + np.sqrt(shifted**2 + self.s**2))

    def wing_variance(self, k: float) -> float:
        """Total variance with flat-vol wings: beyond the quoted strikes, the edge of the smile, not an extrapolated curve.

        A slice fitted on a narrow strike range is unconstrained outside it, and
        extrapolating the fitted curve there produced impossible prices (a far
        put worth more than a nearer one). Holding the edge vol is the standard
        remedy, and Black-76 at a constant vol is arbitrage-free out there.
        """
        return float(self.total_variance(min(max(k, self.k_low), self.k_high)))

    def iv(self, forward: float, strike: float) -> float:
        """Implied volatility at `strike` (flat beyond the quoted strikes)."""
        return float(math.sqrt(max(self.wing_variance(math.log(strike / forward)), 1e-12) / self.t))

    @property
    def wings_ok(self) -> bool:
        """Lee's moment bound: the wings' total-variance slope b (1 + |rho|) is at most 2."""
        return self.b * (1 + abs(self.rho)) <= 2.0 + 1e-9

    @property
    def min_variance_ok(self) -> bool:
        """The smile never goes below zero variance (a + b s sqrt(1 - rho^2) >= 0)."""
        return self.a + self.b * self.s * math.sqrt(1 - self.rho**2) >= 0


def fit_svi_slice(log_moneyness: Sequence[float], ivs: Sequence[float], t: float, *, weights: Sequence[float] | None = None) -> SVISlice:
    """Least-squares SVI fit of one expiry's smile (total variance), keeping the parameters admissible."""
    k = np.asarray(log_moneyness, dtype=float)
    w = np.asarray(ivs, dtype=float) ** 2 * t
    weight = np.ones_like(w) if weights is None else np.asarray(weights, dtype=float)
    atm = float(np.interp(0.0, np.sort(k), w[np.argsort(k)]))

    def residuals(p: np.ndarray) -> np.ndarray:
        slice_ = SVISlice(t, *p)
        negative_variance = min(0.0, p[0] + p[1] * p[4] * math.sqrt(max(1 - p[2] ** 2, 0)))
        # Roger Lee's bound: total variance can grow at most 2x as fast as |ln(K/F)| in the wings, or far-out prices
        # stop falling with the strike (an arbitrage the validation gate catches in the extrapolated wings)
        steep_wings = max(0.0, p[1] * (1 + abs(p[2])) - 2.0)
        return np.append((slice_.total_variance(k) - w) * weight, [negative_variance * 10, steep_wings * 10])

    start = np.array([max(atm * 0.5, 1e-4), 0.1, -0.3, 0.0, 0.1])
    lower, upper = [-1.0, 1e-6, -0.999, -2.0, 1e-4], [max(w.max() * 2, 1.0), 5.0, 0.999, 2.0, 3.0]
    fit = least_squares(residuals, start, bounds=(lower, upper), max_nfev=5000)
    return SVISlice(t, *map(float, fit.x), k_low=float(k.min()), k_high=float(k.max()))


@dataclass(frozen=True, slots=True)
class SVISurface:
    """Fitted SVI slices joined in time; prices with Black-76 at the surface's vol. `vol_shift` moves every vol in parallel."""

    slices: tuple[SVISlice, ...]
    vol_shift: float = 0.0
    name: str = "svi_surface"
    forwards: dict[float, float] = field(default_factory=dict)

    def iv(self, forward: float, strike: float, t: float) -> float:
        """Implied vol at (strike, t), from total variance interpolated linearly in time at fixed moneyness."""
        k = math.log(strike / forward)
        ordered = sorted(self.slices, key=lambda slice_: slice_.t)
        times = [slice_.t for slice_ in ordered]
        if t <= times[0]:
            variance = ordered[0].wing_variance(k) * t / times[0]
        elif t >= times[-1]:
            variance = ordered[-1].wing_variance(k) * t / times[-1]
        else:
            index = int(np.searchsorted(times, t)) - 1
            left, right = ordered[index], ordered[index + 1]
            weight = (t - left.t) / (right.t - left.t)
            variance = (1 - weight) * left.wing_variance(k) + weight * right.wing_variance(k)
        return math.sqrt(max(variance, 1e-12) / t) + self.vol_shift

    def price(self, forward: float, strike: float, t: float, right: str, discount: float = 1.0) -> float:
        """Black-76 at the surface's implied vol for this strike and expiry."""
        if t <= 0:
            return Black76(0.0).price(forward, strike, t, right, discount)
        return Black76(max(self.iv(forward, strike, t), 1e-4)).price(forward, strike, t, right, discount)

    def shifted(self, shift: float) -> "SVISurface":
        """Every implied vol moved by `shift` (0.05 = +5 vol points), for vega and scenarios."""
        from dataclasses import replace

        return replace(self, vol_shift=self.vol_shift + shift)


def fit_svi_surface(quotes: pd.DataFrame, *, min_quotes: int = 5) -> SVISurface:
    """One SVI slice per expiry with enough quotes (from `market_quotes`), weighted by vega so liquid strikes count most."""
    slices, forwards = [], {}
    for t, group in quotes.groupby("t"):
        if len(group) < min_quotes:
            continue
        k = np.log(group["strike"] / group["forward"]).to_numpy()
        weights = (group["vega"] / group["vega"].max()).to_numpy() ** 0.5
        slices.append(fit_svi_slice(k, group["market_iv"].to_numpy(), float(t), weights=weights))
        forwards[float(t)] = float(group["forward"].iloc[0])
    if not slices:
        raise ValueError("no expiry has enough quotes for an SVI fit")
    return SVISurface(slices=tuple(slices), forwards=forwards)
