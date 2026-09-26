"""The checks a pricing model must pass before we trust it for risk or for trading signals.

A model that violates these can report an edge that is really its own
error: a call worth more than the forward, a delta above 1, prices that
aren't convex in strike (which a butterfly would arbitrage). Each check is
model-independent, so every new model (analytical, PDE, Monte Carlo, fitted
surface) runs through the same harness:

- **bounds**: discounted intrinsic <= price <= discounted forward (calls) or strike (puts).
- **monotone in strike**: calls fall and puts rise as the strike rises.
- **convex in strike**: no negative butterflies.
- **put-call parity**: C - P = D x (F - K).
- **delta bounds**: 0 <= call delta <= D, and -D <= put delta <= 0.
- **calendar**: at a fixed forward-moneyness, a longer expiry is worth at least as much (undiscounted).
- **reduces to Black-76** (optional): when the model's extra features are switched off.

`validate_model` returns one row per check with the worst violation found, so a
failure says where the model breaks (deep in-the-money, short expiries, ...).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from src.options.pricing import CALL, PUT, Black76, PricingModel, greeks


@dataclass(frozen=True, slots=True)
class Check:
    """One check's result: whether it passed and the worst violation (with where it happened)."""

    name: str
    passed: bool
    worst: float
    where: str


def validate_model(
    model: PricingModel,
    *,
    forward: float,
    strikes: Sequence[float],
    expiries: Sequence[float],
    discount_rate: float = 0.0,
    tolerance: float = 1e-6,
    reference: PricingModel | None = None,
) -> list[Check]:
    """Run every check over a strike x expiry grid. `reference`: a model this one should equal (e.g. Black-76 with jumps off)."""
    strikes = sorted(float(strike) for strike in strikes)
    checks: dict[str, tuple[float, str]] = {name: (0.0, "") for name in ("bounds", "monotone_in_strike", "convex_in_strike", "put_call_parity", "delta_bounds", "calendar")}
    if reference is not None:
        checks["matches_reference"] = (0.0, "")

    def record(name: str, violation: float, where: str) -> None:
        if violation > checks[name][0]:
            checks[name] = (violation, where)

    scale = forward
    for t in expiries:
        discount = float(np.exp(-discount_rate * t))
        calls = [model.price(forward, strike, t, CALL, discount) for strike in strikes]
        puts = [model.price(forward, strike, t, PUT, discount) for strike in strikes]
        for strike, call, put in zip(strikes, calls, puts):
            where = f"K={strike:g}, T={t:g}"
            record("bounds", max(discount * max(forward - strike, 0) - call, call - discount * forward, discount * max(strike - forward, 0) - put, put - discount * strike, 0) / scale, where)
            record("put_call_parity", abs((call - put) - discount * (forward - strike)) / scale, where)
            call_delta = greeks(model, forward, strike, t, CALL, discount).delta
            put_delta = greeks(model, forward, strike, t, PUT, discount).delta
            record("delta_bounds", max(-call_delta, call_delta - discount, -discount - put_delta, put_delta, 0), where)
            if reference is not None:
                record("matches_reference", abs(call - reference.price(forward, strike, t, CALL, discount)) / scale, where)
        for index in range(1, len(strikes)):
            record("monotone_in_strike", max(calls[index] - calls[index - 1], puts[index - 1] - puts[index], 0) / scale, f"K={strikes[index]:g}, T={t:g}")
        for index in range(1, len(strikes) - 1):
            left, middle, right = strikes[index - 1 : index + 2]
            weight = (right - middle) / (right - left)
            butterfly = weight * calls[index - 1] + (1 - weight) * calls[index + 1] - calls[index]
            record("convex_in_strike", max(-butterfly, 0) / scale, f"K={middle:g}, T={t:g}")
    ordered = sorted(expiries)
    for short, long in zip(ordered, ordered[1:]):
        for moneyness in (0.7, 0.9, 1.0, 1.1, 1.4):
            strike = forward * moneyness
            gap = model.price(forward, strike, short, CALL) - model.price(forward, strike, long, CALL)
            record("calendar", max(gap, 0) / scale, f"K/F={moneyness:g}, T={short:g}->{long:g}")
    return [Check(name, worst <= tolerance, worst, where) for name, (worst, where) in checks.items()]


def black76_reference(sigma: float) -> PricingModel:
    """The model a jump or stochastic-vol model must equal when its extra features are off."""
    return Black76(sigma)
