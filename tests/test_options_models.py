"""The PDE model, calibration, the SVI surface, Deribit chain data and the exposure layer."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.options import deribit
from src.options.calibration import Parameter, black76_per_expiry, calibrate, iv_errors, market_quotes
from src.options.pde import LocalVolJumpPDE
from src.options.pricing import Black76, MertonJump, greeks
from src.options.surface import SVISlice, fit_svi_slice, fit_svi_surface
from src.options.validation import validate_model
from src.portfolio.exposure import MarketState, Position, exposures, scenario_grid

F = 100_000.0
NOW = datetime(2026, 9, 26, 8, tzinfo=timezone.utc)


def test_the_pde_is_black76_without_its_extras_and_merton_with_constant_jumps() -> None:
    plain = LocalVolJumpPDE(sigma=0.5, anchor=F, grid_points=400, time_steps=200)
    checks = validate_model(plain, forward=F, strikes=[60e3, 90e3, 100e3, 110e3, 150e3], expiries=[0.1, 0.5], reference=Black76(0.5), tolerance=2e-4)
    assert all(check.passed for check in checks), [check for check in checks if not check.passed]
    jumps = LocalVolJumpPDE(sigma=0.4, intensity=1.5, jump_mean=-0.08, jump_vol=0.2, anchor=F)
    for strike, right in ((60_000, "put"), (100_000, "call"), (140_000, "call")):
        assert jumps.price(F, strike, 0.25, right) == pytest.approx(MertonJump(0.4, 1.5, -0.08, 0.2).price(F, strike, 0.25, right), rel=2e-3)


def test_the_full_pde_model_passes_the_gate_and_its_deltas_stay_in_bounds() -> None:
    model = LocalVolJumpPDE(sigma=0.2859, skew=0.10, intensity=1.48, crash_sensitivity=0.2, jump_mean=-0.0567, jump_vol=0.25, anchor=86_305.0)
    checks = validate_model(model, forward=86_305.0, strikes=[30e3, 60e3, 86e3, 110e3, 180e3], expiries=[0.05, 0.5], tolerance=2e-4)
    assert all(check.passed for check in checks), [check for check in checks if not check.passed]
    deep = greeks(model, 86_305.0, 20_000, 0.25, "call", bump=1e-3)
    assert 0.99 < deep.delta <= 1.0 + 1e-6 and deep.gamma >= 0  # the notebook's grid delta was 1.07 here


def _synthetic_chain(model, *, forward: float = F, expiries_days=(7, 30, 90, 180), strikes=np.arange(50_000, 160_001, 10_000)) -> pd.DataFrame:
    rows = []
    for days in expiries_days:
        t = days / 365
        for strike in strikes:
            right = "put" if strike < forward else "call"
            price = model.price(forward, float(strike), t, right)
            rows.append({"instrument": f"X-{days}-{strike}-{right[0].upper()}", "expiry": NOW + timedelta(days=days), "t": t, "strike": float(strike), "right": right,
                         "forward": forward, "discount": 1.0, "bid": price * 0.99, "ask": price * 1.01, "mark": price, "open_interest": 10.0})
    return pd.DataFrame(rows)


def test_calibration_recovers_the_parameters_that_made_the_chain() -> None:
    truth = MertonJump(sigma=0.45, intensity=1.2, jump_mean=-0.07, jump_vol=0.2)
    quotes = market_quotes(_synthetic_chain(truth), price="mark")
    assert len(quotes) > 20 and set(quotes["right"]) == {"put", "call"}
    fit = calibrate(lambda p: MertonJump(p["sigma"], p["intensity"], p["jump_mean"], p["jump_vol"]),
                    [Parameter("sigma", 0.3, 0.05, 1.5), Parameter("intensity", 0.5, 0.0, 5.0), Parameter("jump_mean", -0.02, -0.5, 0.3), Parameter("jump_vol", 0.1, 0.02, 0.8)], quotes)
    assert fit.iv_rmse < 0.05  # vol points
    assert fit.params["sigma"] == pytest.approx(0.45, abs=0.02)
    flat = black76_per_expiry(quotes)
    assert len(flat) == 4 and all(0.3 < sigma < 0.9 for sigma in flat.values())
    errors = iv_errors(Black76(0.5), quotes)
    assert {"model_iv", "iv_error_points", "price_error_pct"} <= set(errors.columns)


def test_market_quotes_keep_liquid_out_of_the_money_quotes_only() -> None:
    chain = _synthetic_chain(Black76(0.5))
    chain.loc[0, "ask"] = chain.loc[0, "bid"] * 3  # a wide market
    chain.loc[1, "bid"] = np.nan  # one-sided
    quotes = market_quotes(chain, max_relative_spread=0.3)
    assert chain.loc[0, "instrument"] not in set(quotes["instrument"]) and chain.loc[1, "instrument"] not in set(quotes["instrument"])
    assert ((quotes["right"] == "put") == (quotes["strike"] < quotes["forward"])).all()


def test_svi_recovers_a_smile_and_the_surface_passes_the_gate() -> None:
    truth = SVISlice(t=0.25, a=0.02, b=0.12, rho=-0.4, m=0.02, s=0.15)
    k = np.linspace(-0.6, 0.5, 25)
    fitted = fit_svi_slice(k, np.sqrt(truth.total_variance(k) / 0.25), 0.25)
    assert np.allclose(fitted.total_variance(k), truth.total_variance(k), atol=1e-6) and fitted.min_variance_ok
    # 30 days and longer: a 7-day Merton smile is spikier than five SVI parameters can bend (live BTC smiles fit fine)
    quotes = market_quotes(_synthetic_chain(MertonJump(0.45, 1.2, -0.07, 0.2), expiries_days=(30, 90, 180)), price="mark")
    surface = fit_svi_surface(quotes)
    errors = iv_errors(surface, quotes)["iv_error_points"].to_numpy()
    assert np.sqrt(np.average(errors**2, weights=quotes["vega"])) < 0.3  # vega-weighted: where pricing risk is
    assert all(check.passed for check in validate_model(surface, forward=F, strikes=[60e3, 90e3, 100e3, 120e3, 150e3], expiries=[10 / 365, 60 / 365, 0.4]))
    assert surface.shifted(0.05).iv(F, 100_000, 0.25) == pytest.approx(surface.iv(F, 100_000, 0.25) + 0.05)


def test_deribit_chains_are_parsed_stored_and_loaded(tmp_path, monkeypatch) -> None:
    assert deribit.parse_instrument("BTC-25DEC26-100000-C") == ("BTC", datetime(2026, 12, 25, 8, tzinfo=timezone.utc), 100000.0, "call")
    assert deribit.parse_instrument("XRP_USDC-27SEP26-0d625-P")[2:] == (0.625, "put")
    rows = [
        {"instrument_name": "BTC-25DEC26-100000-C", "bid_price": 0.02, "ask_price": 0.022, "mark_price": 0.021, "mark_iv": 40.0, "underlying_price": 88_000.0,
         "estimated_delivery_price": 84_000.0, "interest_rate": 0.0, "open_interest": 12.0, "volume": 3.0},
        {"instrument_name": "BTC-25DEC26-60000-P", "bid_price": 0.0, "ask_price": 0.004, "mark_price": 0.003, "mark_iv": 55.0, "underlying_price": 88_000.0,
         "estimated_delivery_price": 84_000.0, "interest_rate": 0.0, "open_interest": 0.0, "volume": 0.0},
        {"instrument_name": "BTC-26SEP26-90000-C", "bid_price": 0.001, "ask_price": 0.002, "mark_price": 0.0015, "mark_iv": 30.0, "underlying_price": 84_000.0},  # expired
    ]
    chain = deribit.normalize_chain(rows, NOW)
    assert list(chain["instrument"]) == ["BTC-25DEC26-60000-P", "BTC-25DEC26-100000-C"]
    call = chain.iloc[1]
    assert call["mark"] == pytest.approx(0.021 * 88_000) and call["forward"] == 88_000 and call["mark_iv"] == pytest.approx(0.40)
    assert np.isnan(chain.iloc[0]["bid"])  # no bid is missing, not zero

    path = deribit.save_chain(chain, tmp_path)
    assert path.parent.name == "2026-09-26" and deribit.load_chains("BTC", root=tmp_path).equals(chain)
    assert deribit.load_chains("BTC", start="2026-09-27", root=tmp_path).empty and deribit.latest_chain("BTC", root=tmp_path).equals(chain)

    calls = []
    monkeypatch.setattr(deribit, "fetch_chain", lambda currency: calls.append(currency) or chain.assign(currency=currency))
    saved = asyncio.run(deribit.record_chains(("BTC", "ETH"), every_seconds=0, root=tmp_path / "rec", iterations=2))
    assert saved == 4 and calls == ["BTC", "ETH", "BTC", "ETH"]


def _market(vol: float = 0.5) -> MarketState:
    return MarketState(now=NOW, spot={"BTC": F, "ETH": 3_000.0}, models={"BTC": Black76(vol)})


def test_exposures_of_perps_and_options_match_closed_forms() -> None:
    expiry = NOW + timedelta(days=91.25)
    call = Position("option", "BTC", 2.0, strike=110_000, expiry=expiry, right="call")
    perp = Position("perp", "BTC", -0.5)
    table = exposures([call, perp], _market())
    t = 91.25 / 365
    g = greeks(Black76(0.5), F, 110_000, t, "call")
    assert table.loc["BTC", "delta_units"] == pytest.approx(2 * g.delta - 0.5, rel=1e-3)
    assert table.loc["BTC", "vega_1pt"] == pytest.approx(2 * g.vega * 0.01, rel=1e-3)
    assert table.loc["BTC", "gamma_1pct"] == pytest.approx(2 * g.gamma * (0.01 * F) ** 2, rel=0.02)
    assert table.loc["BTC", "theta_1d"] < 0
    assert exposures([perp], _market()).loc["BTC", "delta_1pct"] == pytest.approx(-0.5 * F * 0.01)


def test_a_collar_caps_the_crash_loss_and_the_scenario_grid_shows_it() -> None:
    expiry = NOW + timedelta(days=30)
    long_btc = [Position("perp", "BTC", 1.0)]
    collar = long_btc + [Position("option", "BTC", 1.0, strike=90_000, expiry=expiry, right="put"), Position("option", "BTC", -1.0, strike=115_000, expiry=expiry, right="call")]
    naked, hedged = scenario_grid(long_btc, _market()), scenario_grid(collar, _market())
    assert naked.loc["-30%", "+0 vol pts"] == pytest.approx(-30_000)
    assert hedged.loc["-30%", "+0 vol pts"] > -15_000 and hedged.loc["+30%", "+0 vol pts"] < naked.loc["+30%", "+0 vol pts"]
    table = exposures(collar + [Position("spot", "ETH", 2.0)], _market())
    assert "total" in table.index and table.loc["ETH", "delta_units"] == pytest.approx(2.0)
    with pytest.raises(ValueError, match="needs a strike"):
        Position("option", "BTC", 1.0)


def test_svi_wings_respect_lees_bound_and_surface_vega_comes_from_shifting_it() -> None:
    k = np.linspace(-0.9, 0.6, 30)
    steep = 0.45 + 1.6 * np.maximum(-k, 0) + 0.8 * np.maximum(k, 0)  # a smile whose wings rise faster than no-arbitrage allows
    fitted = fit_svi_slice(k, steep, 30 / 365)
    assert fitted.wings_ok and fitted.min_variance_ok
    quotes = market_quotes(_synthetic_chain(MertonJump(0.45, 1.2, -0.07, 0.2), expiries_days=(30, 90)), price="mark")
    surface = fit_svi_surface(quotes)
    vega = greeks(surface, F, 100_000, 60 / 365, "call").vega
    assert vega == pytest.approx(greeks(Black76(surface.iv(F, 100_000, 60 / 365)), F, 100_000, 60 / 365, "call").vega, rel=1e-3)


def test_a_narrowly_quoted_slice_keeps_flat_wings_instead_of_extrapolating_its_curve() -> None:
    steep = SVISlice(t=20 / 365, a=-0.058, b=1.028, rho=-0.946, m=-0.546, s=0.191, k_low=-0.19, k_high=0.17)  # fitted live, quoted only near the money
    assert steep.iv(F, F * 0.45) == pytest.approx(steep.iv(F, F * math.exp(-0.19)))  # held at the edge, not 150%
    from src.options.surface import SVISurface

    surface = SVISurface(slices=(steep, SVISlice(t=34 / 365, a=-0.037, b=0.128, rho=-0.442, m=-0.192, s=0.418, k_low=-0.43, k_high=0.31)))
    checks = validate_model(surface, forward=F, strikes=[F * m for m in (0.45, 0.55, 0.7, 0.85, 1.0, 1.2, 1.5)], expiries=[25 / 365, 30 / 365], tolerance=2e-4)
    assert all(check.passed for check in checks), [check for check in checks if not check.passed]
