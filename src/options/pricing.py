"""Option pricing models behind one interface, in forward terms (Black-76), so any model can be swapped in and tested.

Crypto options (Deribit) are quoted against a forward: each expiry has its own
underlying future, and Deribit's `underlying_price` for an option is that
forward, not spot. Pricing on the forward with a discount factor
(Black-76) avoids a classic mistake: treating the forward as spot and then
adding an interest-rate drift, which moves the forward a second time.

Every model implements `price(forward, strike, t, right, discount=1.0)`.
Greeks come from bump-and-reprice (`greeks`), so a new model (a PDE solver, a
Monte Carlo pricer, a fitted surface) gets them for free, and the validation
harness (`src/options/validation.py`) can test any model the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Protocol

from scipy.optimize import brentq
from scipy.stats import norm

CALL, PUT = "call", "put"


class PricingModel(Protocol):
    """Anything that prices a European option on a forward."""

    name: str

    def price(self, forward: float, strike: float, t: float, right: str, discount: float = 1.0) -> float:
        """Present value in the forward's currency; `t` in years, `discount` = the zero-coupon bond price to expiry."""
        ...


def _intrinsic(forward: float, strike: float, right: str, discount: float) -> float:
    return discount * max(forward - strike, 0.0) if right == CALL else discount * max(strike - forward, 0.0)


@dataclass(frozen=True, slots=True)
class Black76:
    """The market's quoting model: lognormal forward with volatility `sigma` (0.6 = 60% a year)."""

    sigma: float
    name: str = "black76"

    def price(self, forward: float, strike: float, t: float, right: str, discount: float = 1.0) -> float:
        """Black-76 value; at expiry or zero vol it is the discounted intrinsic value."""
        if t <= 0 or self.sigma <= 0:
            return _intrinsic(forward, strike, right, discount)
        spread = self.sigma * math.sqrt(t)
        d1 = (math.log(forward / strike) + 0.5 * spread**2) / spread
        d2 = d1 - spread
        if right == CALL:
            return discount * (forward * norm.cdf(d1) - strike * norm.cdf(d2))
        return discount * (strike * norm.cdf(-d2) - forward * norm.cdf(-d1))


@dataclass(frozen=True, slots=True)
class MertonJump:
    """Merton (1976) jump diffusion on the forward: diffusion `sigma` plus lognormal jumps.

    Jumps arrive at `intensity` per year with log size ~ N(`jump_mean`,
    `jump_vol`). Under the forward measure the forward is a martingale, so the
    diffusion drift compensates the jumps exactly and no interest-rate term
    appears. The price is the Poisson-weighted sum of Black-76 prices, which is
    exact (no grid) and fast enough to calibrate. This is the analytical model
    in the user's options notebook, restated on the forward.
    """

    sigma: float
    intensity: float
    jump_mean: float
    jump_vol: float
    terms: int = 40
    name: str = "merton_jump"

    def price(self, forward: float, strike: float, t: float, right: str, discount: float = 1.0) -> float:
        """Sum over n jumps of P(n) x Black-76 with the forward and variance conditioned on n jumps."""
        if t <= 0:
            return _intrinsic(forward, strike, right, discount)
        kappa = math.exp(self.jump_mean + 0.5 * self.jump_vol**2) - 1.0  # expected relative jump
        weight = math.exp(-self.intensity * t)
        total = 0.0
        for n in range(self.terms):
            if n > 0:
                weight *= self.intensity * t / n
            # conditional on n jumps: forward shifted so that the unconditional forward stays a martingale
            conditional_forward = forward * math.exp(n * (self.jump_mean + 0.5 * self.jump_vol**2) - self.intensity * kappa * t)
            conditional_sigma = math.sqrt(self.sigma**2 + n * self.jump_vol**2 / t)
            total += weight * Black76(conditional_sigma).price(conditional_forward, strike, t, right, discount)
        return total


@dataclass(frozen=True, slots=True)
class Greeks:
    """Sensitivities of one option: per unit of the underlying, per 1.00 of vol (x0.01 for a vol point), per year of time."""

    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    extra: dict[str, float] = field(default_factory=dict)


def greeks(model: PricingModel, forward: float, strike: float, t: float, right: str, discount: float = 1.0, *, bump: float = 1e-4) -> Greeks:
    """Bump-and-reprice greeks for any model.

    Delta and gamma are with respect to the forward (relative bump `bump`).
    Vega bumps the model's `sigma` (its base volatility), and theta shortens
    the time by one day (the value lost per year, i.e. x365 of a day's decay).
    """
    base = model.price(forward, strike, t, right, discount)
    step = forward * bump
    up, down = model.price(forward + step, strike, t, right, discount), model.price(forward - step, strike, t, right, discount)
    delta = (up - down) / (2 * step)
    gamma = (up - 2 * base + down) / step**2
    vega = 0.0
    if hasattr(model, "sigma"):
        vol_step = 1e-4
        vega = (replace(model, sigma=model.sigma + vol_step).price(forward, strike, t, right, discount)
                - replace(model, sigma=max(1e-8, model.sigma - vol_step)).price(forward, strike, t, right, discount)) / (2 * vol_step)
    day = 1.0 / 365.0
    theta = (model.price(forward, strike, max(t - day, 0.0), right, discount) - base) / day if t > day else 0.0
    return Greeks(price=base, delta=delta, gamma=gamma, vega=vega, theta=theta)


def implied_vol(price: float, forward: float, strike: float, t: float, right: str, discount: float = 1.0, *, low: float = 1e-4, high: float = 5.0) -> float:
    """The Black-76 volatility that reproduces `price`; NaN when the price is outside no-arbitrage bounds."""
    lower = _intrinsic(forward, strike, right, discount)
    upper = discount * (forward if right == CALL else strike)
    if t <= 0 or not lower < price < upper:
        return float("nan")
    return brentq(lambda sigma: Black76(sigma).price(forward, strike, t, right, discount) - price, low, high, xtol=1e-10)
