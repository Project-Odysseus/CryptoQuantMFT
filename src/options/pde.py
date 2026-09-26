"""Local volatility with state-dependent jumps, priced by a PIDE on the log-forward (from the options notebook, fixed).

The model (the user's notebook, `main.ipynb`):

    volatility   sigma(x) = sigma + skew / (1 + exp(skew_steepness * (x - ln(anchor))))
    jump rate    lambda(x) = intensity + crash_sensitivity * max(ln(anchor) - x, 0)^2
    jump size    log jump ~ N(jump_mean, jump_vol)

where x = ln(forward). Volatility and the jump rate rise as the price falls
below `anchor`, which gives the downside skew crypto options show.

What changed from the notebook, and why (docs/options_plan.md, section 6):
- It prices on the forward with a discount factor (Black-76 conventions).
  The notebook passed Deribit's forward as spot and added a 4.5% drift,
  which overpriced calls by 3-10%.
- The skew and jump rate are anchored at a fixed `anchor` (default: the
  forward the model is built for), not at the price being valued. Anchored
  at the valuation price, a bump of the forward also moved the skew, and
  the notebook's grid delta came out above 1.
- It prices puts as well as calls (their own boundary conditions), so
  put-call parity can be checked.
- The jump integral is a convolution on the uniform log grid, done with an
  FFT (N log N per step instead of a dense N x N matrix), and uses the
  boundary values for jumps that land outside the grid.
- Greeks come from `src.options.pricing.greeks` (bump and reprice), which
  holds the anchor still.

With `skew = intensity = crash_sensitivity = 0` it is Black-76, and the
validation harness checks that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.linalg import solve_banded
from scipy.signal import fftconvolve
from scipy.stats import norm

from src.options.pricing import CALL


@dataclass(frozen=True, slots=True)
class LocalVolJumpPDE:
    """The notebook's local-vol + state-dependent-jump model as a `PricingModel` (fully implicit diffusion, explicit jumps)."""

    sigma: float
    skew: float = 0.0
    skew_steepness: float = 5.0
    intensity: float = 0.0
    crash_sensitivity: float = 0.0
    jump_mean: float = -0.05
    jump_vol: float = 0.15
    anchor: float | None = None  # None: anchor at the forward being priced (fine for a single price, not for greeks)
    grid_points: int = 600
    time_steps: int = 300
    width_sigmas: float = 6.0
    name: str = "local_vol_jump_pde"

    def _grid(self, anchor: float, forward: float, strike: float, t: float) -> tuple[np.ndarray, float]:
        """A grid set by the anchor and strike only, so bumping the forward for a greek doesn't move the grid (that was noise)."""
        spread = max(self.sigma + self.skew, 0.2) * math.sqrt(t) * self.width_sigmas + abs(self.jump_mean) * 3 + self.jump_vol * 4
        low = min(math.log(anchor), math.log(strike)) - spread
        high = max(math.log(anchor), math.log(strike)) + spread
        if not low + spread / 2 < math.log(forward) < high - spread / 2:  # a forward far from the anchor: widen to cover it
            low, high = min(low, math.log(forward) - spread), max(high, math.log(forward) + spread)
        return np.linspace(low, high, self.grid_points, retstep=True)

    def price(self, forward: float, strike: float, t: float, right: str, discount: float = 1.0) -> float:
        """Solve backwards from expiry on x = ln(forward) and read the value at today's forward."""
        if t <= 0:
            return discount * max(forward - strike, 0.0) if right == CALL else discount * max(strike - forward, 0.0)
        rate = -math.log(discount) / t if discount < 1.0 else 0.0
        anchor = self.anchor or forward
        x, dx = self._grid(anchor, forward, strike, t)
        x0 = math.log(anchor)
        vol = self.sigma + self.skew / (1.0 + np.exp(self.skew_steepness * (x - x0)))
        jump_rate = self.intensity + self.crash_sensitivity * np.maximum(x0 - x, 0.0) ** 2
        kappa = math.exp(self.jump_mean + 0.5 * self.jump_vol**2) - 1.0
        drift = -0.5 * vol**2 - jump_rate * kappa  # keeps the forward a martingale
        dt = t / self.time_steps

        # Jump kernel on the grid spacing: P(jump lands in each cell), for the convolution
        reach = int(math.ceil((abs(self.jump_mean) + 6 * self.jump_vol) / dx))
        offsets = np.arange(-reach, reach + 1) * dx
        kernel = norm.cdf(offsets + dx / 2, self.jump_mean, self.jump_vol) - norm.cdf(offsets - dx / 2, self.jump_mean, self.jump_vol)
        jumps = bool(np.any(jump_rate > 0))

        levels = np.exp(x)
        value = np.maximum(levels - strike, 0.0) if right == CALL else np.maximum(strike - levels, 0.0)
        # Implicit operator (tridiagonal): (1 + r dt) V - dt [a V_{i-1} + b V_i + c V_{i+1}] = V_old + dt * jumps
        diffusion = 0.5 * vol**2 / dx**2
        advection = drift / (2 * dx)
        lower = -dt * (diffusion - advection)
        main = 1.0 + rate * dt + dt * (2 * diffusion + jump_rate)
        upper = -dt * (diffusion + advection)
        banded = np.zeros((3, self.grid_points))
        banded[0, 1:] = upper[:-1]
        banded[1, :] = main
        banded[2, :-1] = lower[1:]
        banded[1, 0] = banded[1, -1] = 1.0
        banded[0, 1] = 0.0
        banded[2, -2] = 0.0
        for step in range(1, self.time_steps + 1):
            remaining = step * dt
            df = math.exp(-rate * remaining)
            low_bc = 0.0 if right == CALL else df * (strike - levels[0])
            high_bc = df * (levels[-1] - strike) if right == CALL else 0.0
            rhs = value.copy()
            if jumps:
                padded = np.concatenate([np.full(reach, low_bc), value, np.full(reach, high_bc)])
                landed = fftconvolve(padded, kernel[::-1], mode="valid")  # E[V(x + jump)] on the grid
                rhs += dt * jump_rate * landed
            rhs[0], rhs[-1] = low_bc, high_bc
            value = solve_banded((1, 1), banded, rhs)
        # A cubic spline, not linear interpolation: linear in ln(F) is piecewise straight, so bump greeks smaller than a grid
        # cell saw zero or noisy gamma, and its slope overstated delta by up to exp(dx / 2).
        return float(CubicSpline(x, value)(math.log(forward)))

    def anchored(self, forward: float) -> "LocalVolJumpPDE":
        """The same model with its skew anchored at `forward` (do this once per chain, then price and bump freely)."""
        from dataclasses import replace

        return replace(self, anchor=forward)
