"""Option pricing (src/options/pricing.py) and the model validation harness (src/options/validation.py)."""

from __future__ import annotations

import math

import pytest

from src.options.pricing import CALL, PUT, Black76, MertonJump, greeks, implied_vol
from src.options.validation import validate_model

F, T = 100_000.0, 0.25
STRIKES = [40_000, 60_000, 80_000, 90_000, 100_000, 110_000, 120_000, 150_000, 200_000]
EXPIRIES = [7 / 365, 30 / 365, 90 / 365, 0.5, 1.0]


def test_black76_matches_known_values_and_parity() -> None:
    model = Black76(0.5)
    call, put = model.price(F, 100_000, T, CALL), model.price(F, 100_000, T, PUT)
    half_spread = 0.5 * math.sqrt(T) / 2  # ATM forward: C = F (2 N(sigma sqrt(T) / 2) - 1)
    assert call == pytest.approx(F * (2 * 0.5 * (1 + math.erf(half_spread / math.sqrt(2))) - 1), rel=1e-9)
    assert call == pytest.approx(put)  # ATM forward: parity with zero carry
    assert model.price(F, 50_000, 0.0, CALL) == 50_000 and model.price(F, 150_000, T, CALL, discount=0.99) < model.price(F, 150_000, T, CALL)


def test_greeks_of_black76_match_closed_forms() -> None:
    model, strike = Black76(0.6), 110_000.0
    spread = 0.6 * math.sqrt(T)
    d1 = (math.log(F / strike) + 0.5 * spread**2) / spread
    pdf = math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)
    g = greeks(model, F, strike, T, CALL)
    assert g.delta == pytest.approx(0.5 * (1 + math.erf(d1 / math.sqrt(2))), rel=1e-5)
    assert g.gamma == pytest.approx(pdf / (F * spread), rel=1e-3)
    assert g.vega == pytest.approx(F * pdf * math.sqrt(T), rel=1e-4)
    assert g.theta < 0


def test_implied_vol_round_trips_and_refuses_arbitrage_prices() -> None:
    for sigma in (0.2, 0.6, 1.5):
        for strike in (60_000, 100_000, 140_000):
            price = Black76(sigma).price(F, strike, T, CALL, 0.995)
            assert implied_vol(price, F, strike, T, CALL, 0.995) == pytest.approx(sigma, abs=1e-7)
    assert math.isnan(implied_vol(F * 1.1, F, 100_000, T, CALL))  # above the forward: impossible
    assert math.isnan(implied_vol(0.0, F, 150_000, T, CALL))


def test_merton_reduces_to_black76_without_jumps_and_prices_jump_risk_into_the_wings() -> None:
    no_jumps = MertonJump(sigma=0.5, intensity=0.0, jump_mean=-0.1, jump_vol=0.2)
    assert no_jumps.price(F, 80_000, T, PUT) == pytest.approx(Black76(0.5).price(F, 80_000, T, PUT), rel=1e-12)
    jumpy = MertonJump(sigma=0.5, intensity=1.5, jump_mean=-0.08, jump_vol=0.2)
    far_put_iv = implied_vol(jumpy.price(F, 50_000, T, PUT), F, 50_000, T, PUT)
    atm_iv = implied_vol(jumpy.price(F, 100_000, T, CALL), F, 100_000, T, CALL)
    assert far_put_iv > atm_iv  # negative jumps make downside puts dearer: a skew


@pytest.mark.parametrize("model", [Black76(0.6), MertonJump(sigma=0.45, intensity=1.5, jump_mean=-0.06, jump_vol=0.25)])
def test_sound_models_pass_the_whole_harness(model) -> None:
    results = validate_model(model, forward=F, strikes=STRIKES, expiries=EXPIRIES, discount_rate=0.04, tolerance=1e-7)
    assert all(check.passed for check in results), [check for check in results if not check.passed]


def test_the_harness_catches_a_broken_model() -> None:
    class DriftOnForward:
        """Treats the forward as spot and drifts it at 4.5% a year: prices calls too high, breaks parity and the delta bound."""

        name = "drift_on_forward"

        def price(self, forward, strike, t, right, discount=1.0):
            return Black76(0.6).price(forward * math.exp(0.045 * t), strike, t, right, discount)

    failed = {check.name for check in validate_model(DriftOnForward(), forward=F, strikes=STRIKES, expiries=EXPIRIES, discount_rate=0.04) if not check.passed}
    assert {"put_call_parity", "delta_bounds"} <= failed
    reference_check = validate_model(MertonJump(0.5, 0.0, -0.1, 0.2), forward=F, strikes=STRIKES, expiries=EXPIRIES, reference=Black76(0.51))
    assert not next(check for check in reference_check if check.name == "matches_reference").passed
