"""Order planner (src/portfolio/orders.py): targets to orders, with bands, lot steps, minimum sizes and flips."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from src.portfolio.config import InstrumentSpec
from src.portfolio.orders import plan_orders, round_toward_zero

BTC, ETH, SPOT = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD", "kraken:BTC/EUR"
SPECS = {
    BTC: InstrumentSpec(id=BTC, lot_step=0.0001, min_order_size=0.0001),
    ETH: InstrumentSpec(id=ETH, lot_step=0.001, min_order_size=0.01),
    SPOT: InstrumentSpec(id=SPOT, kind="spot", lot_step=0.00001, min_order_size=0.0001),
}
PRICES = {BTC: 50_000.0, ETH: 2_500.0, SPOT: 45_000.0}


def _plan(current: dict[str, float], targets: dict[str, float], *, equity: float = 10_000.0, band: float = 0.0):
    return plan_orders(current, targets, prices=PRICES, equity=equity, instruments=SPECS, band=band)


def _summary(plan) -> list[tuple[str, str, str, bool, str]]:
    return [(order.instrument, order.side, str(order.units), order.reduce_only, order.reason) for order in plan.orders]


def test_weights_become_decimal_units_on_the_lot_grid() -> None:
    plan = _plan({}, {BTC: 0.5, ETH: -0.3})
    # 0.5 x 10,000 / 50,000 = 0.1 BTC; -0.3 x 10,000 / 2,500 = -1.2 ETH
    assert _summary(plan) == [(BTC, "buy", "0.1000", False, "open"), (ETH, "sell", "1.200", False, "open")]
    assert all(isinstance(order.units, Decimal) for order in plan.orders)
    assert plan.orders[0].notional == pytest.approx(5_000.0)


def test_rounding_never_makes_a_position_bigger_than_its_target() -> None:
    rng = np.random.default_rng(0)
    for _ in range(300):
        current = {BTC: float(rng.normal(0, 0.1)), ETH: float(rng.normal(0, 2.0))}
        current = {key: float(round_toward_zero(Decimal(str(value)), Decimal(str(SPECS[key].lot_step)))) for key, value in current.items()}
        targets = {BTC: float(rng.normal(0, 0.6)), ETH: float(rng.normal(0, 0.6))}
        plan = _plan(current, targets)
        final = {key: Decimal(str(value)) for key, value in current.items()}
        for order in plan.orders:
            final[order.instrument] += order.units if order.side == "buy" else -order.units
            assert order.units > 0 and order.units % Decimal(str(SPECS[order.instrument].lot_step)) == 0
        for instrument, units in final.items():
            if any(skip.instrument == instrument for skip in plan.skipped):
                continue
            target_units = targets[instrument] * 10_000.0 / PRICES[instrument]
            assert abs(float(units)) <= abs(target_units) + 1e-12
            assert units == 0 or np.sign(float(units)) == np.sign(target_units)


def test_the_band_suppresses_churn_but_closes_and_flips_always_trade() -> None:
    current = {BTC: 0.1, ETH: 1.2}  # 50% and 30% of equity
    assert _plan(current, {BTC: 0.51, ETH: 0.29}, band=0.02).orders == []
    skipped = _plan(current, {BTC: 0.51, ETH: 0.29}, band=0.02).skipped
    assert {(skip.instrument, skip.reason) for skip in skipped} == {(BTC, "within_band"), (ETH, "within_band")}

    assert _summary(_plan(current, {BTC: 0.6, ETH: 0.3}, band=0.02)) == [(BTC, "buy", "0.0200", False, "increase")]
    assert _summary(_plan({BTC: 0.002}, {BTC: 0.0}, band=0.5)) == [(BTC, "sell", "0.002", True, "close")]  # 1% of equity, still closed
    assert [order.reason for order in _plan({BTC: 0.002}, {BTC: -0.01}, band=0.5).orders] == ["flip_close", "flip_open"]


def test_a_flip_is_a_reduce_only_close_then_an_open_and_exits_go_first() -> None:
    plan = _plan({BTC: 0.1, ETH: -1.2}, {BTC: 0.8, ETH: 0.2})
    assert _summary(plan) == [
        (ETH, "buy", "1.2", True, "flip_close"),
        (BTC, "buy", "0.0600", False, "increase"),
        (ETH, "buy", "0.800", False, "flip_open"),
    ]
    reductions = _plan({BTC: 0.1, ETH: 1.2}, {BTC: 0.9, ETH: 0.1})
    assert [order.reason for order in reductions.orders] == ["reduce", "increase"]
    assert reductions.orders[0].reduce_only and reductions.orders[0].instrument == ETH


def test_minimum_sizes_lot_steps_and_missing_targets() -> None:
    small = _plan({}, {ETH: 0.002})  # 0.008 ETH, below the 0.01 minimum
    assert small.orders == [] and [skip.reason for skip in small.skipped] == ["below_min_size"]
    dust = _plan({}, {BTC: 0.0004})  # 0.00008 BTC, below one lot
    assert dust.orders == [] and [skip.reason for skip in dust.skipped] == ["below_lot_step"]
    stale_close = _plan({ETH: 0.005}, {})  # held, but below the minimum: can't be sent either
    assert stale_close.orders == [] and stale_close.skipped[0].reason == "below_min_size"

    orphan = _plan({BTC: 0.1, ETH: 1.2}, {BTC: 0.5})  # ETH lost its sleeve: close it
    assert _summary(orphan) == [(ETH, "sell", "1.2", True, "close")]


def test_spot_never_goes_short_and_missing_prices_or_specs_are_reported() -> None:
    plan = _plan({SPOT: 0.05}, {SPOT: -0.2})
    assert _summary(plan) == [(SPOT, "sell", "0.05", True, "close")]
    assert [skip.reason for skip in plan.skipped] == ["no_short"]

    no_price = plan_orders({}, {BTC: 0.5}, prices={}, equity=10_000.0, instruments=SPECS)
    assert no_price.orders == [] and no_price.skipped[0].reason == "no_price"
    with pytest.raises(ValueError, match="no instrument spec"):
        plan_orders({}, {"kraken_futures:SOL/USD": 0.1}, prices={"kraken_futures:SOL/USD": 100.0}, equity=10_000.0, instruments=SPECS)


def test_round_toward_zero_uses_a_fallback_step_when_unknown() -> None:
    assert round_toward_zero(Decimal("-1.23456"), Decimal("0.01")) == Decimal("-1.23")
    assert round_toward_zero(Decimal("0.123456789123"), Decimal(0)) == Decimal("0.12345678")
