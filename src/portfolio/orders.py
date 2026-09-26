"""Order planning: from the positions the book holds to the orders that reach the target weights.

Pure: positions, targets, prices and instrument limits in, orders out, with
no exchange calls. Order sizes are `Decimal` because they leave the process.

Rules, in the order they apply to each instrument:

1. An instrument that is held but has no target any more (for example, its
   last sleeve was disabled) is closed.
2. An instrument that can't be shorted (spot) never gets a short target
   (reason `no_short`); the risk overlay should already have clamped it.
3. The rebalance band: while the position stays on the same side and the
   target is within `band` (a share of equity) of the current weight, don't
   trade (`within_band`). Closing and flipping always trade. This matches
   `simulate_portfolio(rebalance_band=...)`, so research trades the same way.
4. The target position is rounded toward zero on the instrument's lot grid,
   so rounding never makes a position bigger than its target, and never
   pushes one past a cap.
5. A flip becomes two orders: a reduce-only close, then an open.
6. Orders smaller than the instrument's minimum size are skipped
   (`below_min_size`). Nothing is silently resized.

Exits and reductions come before increases, so margin is freed before it is
used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from src.portfolio.config import InstrumentSpec

FALLBACK_STEP = Decimal("0.00000001")  # when an instrument's lot step is unknown (0)


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    """One order to send.

    Attributes:
        instrument: "<venue>:<symbol>".
        side: "buy" or "sell".
        units: Size in the instrument's units, a positive multiple of its lot step.
        reduce_only: The order only shrinks or closes a position (safe to send first).
        reason: close | reduce | open | increase | flip_close | flip_open.
        target_weight: The weight this instrument is trading towards.
        price: The reference price used to size it.
    """

    instrument: str
    side: str
    units: Decimal
    reduce_only: bool
    reason: str
    target_weight: float
    price: float

    @property
    def notional(self) -> float:
        """Approximate value of the order at the reference price."""
        return float(self.units) * self.price


@dataclass(frozen=True, slots=True)
class SkippedChange:
    """A change that was not traded, and why (within_band, below_min_size, below_lot_step, no_price, no_short)."""

    instrument: str
    reason: str
    current_weight: float
    target_weight: float


@dataclass(slots=True)
class OrderPlan:
    """The orders to send, in sending order, and the changes deliberately not traded."""

    orders: list[PlannedOrder] = field(default_factory=list)
    skipped: list[SkippedChange] = field(default_factory=list)


def _decimal(value: Decimal | float | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_toward_zero(units: Decimal, step: Decimal) -> Decimal:
    """`units` rounded to a multiple of `step`, toward zero (never larger in size), with the step's decimals."""
    if step <= 0:
        step = FALLBACK_STEP
    return ((units / step).to_integral_value(rounding=ROUND_DOWN) * step).quantize(step)


def plan_orders(
    current_units: Mapping[str, Decimal | float],
    target_weights: Mapping[str, float],
    *,
    prices: Mapping[str, float],
    equity: float,
    instruments: Mapping[str, InstrumentSpec],
    band: float = 0.0,
) -> OrderPlan:
    """Plan the orders that move `current_units` to `target_weights` (signed shares of `equity`).

    Args:
        current_units: Signed position per instrument, in units (negative = short).
        target_weights: Signed target per instrument after the risk overlay.
            A held instrument missing here is closed.
        prices: Reference price per instrument, used to turn weights into units.
        equity: Portfolio equity in the base currency.
        instruments: Specs with `lot_step`, `min_order_size` and `can_short`.
        band: The rebalance band as a share of equity (0.02 = 2%).
    """
    plan = OrderPlan()
    reducing: list[PlannedOrder] = []
    adding: list[PlannedOrder] = []
    held = {instrument: _decimal(units) for instrument, units in current_units.items() if _decimal(units) != 0}
    for instrument in sorted(set(held) | set(target_weights)):
        spec = instruments.get(instrument)
        if spec is None:
            raise ValueError(f"no instrument spec for {instrument}; it must be in the config's [instruments]")
        current = held.get(instrument, Decimal(0))
        target_weight = float(target_weights.get(instrument, 0.0))
        price = prices.get(instrument)
        if price is None or not price > 0:
            if current != 0 or target_weight != 0.0:
                plan.skipped.append(SkippedChange(instrument, "no_price", float("nan"), target_weight))
            continue
        current_weight = float(current) * price / equity if equity > 0 else 0.0
        if target_weight < 0 and not spec.can_short:
            plan.skipped.append(SkippedChange(instrument, "no_short", current_weight, target_weight))
            target_weight = 0.0
        same_side = (current > 0 and target_weight > 0) or (current < 0 and target_weight < 0)
        if same_side and abs(target_weight - current_weight) <= band:
            if target_weight != current_weight:
                plan.skipped.append(SkippedChange(instrument, "within_band", current_weight, target_weight))
            continue

        raw_target = _decimal(target_weight) * _decimal(max(equity, 0.0)) / _decimal(price)
        target = round_toward_zero(raw_target, _decimal(spec.lot_step))
        if target == current:
            if raw_target != current:
                plan.skipped.append(SkippedChange(instrument, "below_lot_step", current_weight, target_weight))
            continue
        legs: list[tuple[Decimal, bool, str]] = []  # (signed change in units, reduce_only, reason)
        if current != 0 and (target == 0 or (target > 0) != (current > 0)):
            legs.append((-current, True, "close" if target == 0 else "flip_close"))
            if target != 0:
                legs.append((target, False, "flip_open"))
        elif abs(target) < abs(current):
            legs.append((target - current, True, "reduce"))
        else:
            legs.append((target - current, False, "open" if current == 0 else "increase"))

        minimum = _decimal(spec.min_order_size)
        for change, reduce_only, reason in legs:
            units = abs(change)
            if minimum > 0 and units < minimum:
                plan.skipped.append(SkippedChange(instrument, "below_min_size", current_weight, target_weight))
                break  # an open after a skipped close would leave both sides wrong
            order = PlannedOrder(instrument, "buy" if change > 0 else "sell", units, reduce_only, reason, target_weight, float(price))
            (reducing if reduce_only else adding).append(order)
    plan.orders = reducing + adding
    return plan
