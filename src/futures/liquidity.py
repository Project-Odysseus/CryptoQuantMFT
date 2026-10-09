"""Liquidity as an input to order size: a cap on the share of daily volume, and a floor below which a contract is left alone.

Volume is read per contract, because a micro contract can be thin when its full-size sibling is deep. The cost of
trading (half the spread plus commission, in ticks) lives on the market (`contracts.Market.cost`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class LiquidityRules:
    """Limits on one order.

    Attributes:
        max_participation: The largest order as a share of the contract's average daily volume.
        min_daily_volume: Below this average volume (contracts a day) nothing new is opened in the contract.
        volume_days: Days in the average.
    """

    max_participation: float = 0.01
    min_daily_volume: float = 200.0
    volume_days: int = 20

    def __post_init__(self) -> None:
        if not 0.0 < self.max_participation <= 1.0 or self.min_daily_volume < 0 or self.volume_days < 1:
            raise ValueError("liquidity rules: max_participation in (0, 1], min_daily_volume >= 0, volume_days >= 1")


def average_volume(volumes: Sequence[float], days: int) -> float | None:
    """The mean of the last `days` daily volumes; None when there is no record."""
    recent = [float(value) for value in list(volumes)[-days:] if value is not None and float(value) == float(value)]
    return sum(recent) / len(recent) if recent else None


def allowed_contracts(wanted: int, average: float | None, rules: LiquidityRules, *, reducing: bool = False) -> tuple[int, str]:
    """How many of `wanted` contracts (a positive count) may be sent now, and why if fewer.

    An order that reduces a position is never blocked by the volume floor: getting out of a thin contract matters
    more than the price. It is still capped at the participation limit, so a large exit is spread over days. With no
    volume record the order is allowed and the reason says so: refusing would freeze a market whose data feed failed.
    """
    if wanted <= 0:
        return 0, ""
    if average is None:
        return wanted, "volume unknown"
    if average < rules.min_daily_volume and not reducing:
        return 0, f"average volume {average:,.0f} is below the floor of {rules.min_daily_volume:,.0f}"
    cap = max(int(average * rules.max_participation), 1)
    if wanted > cap:
        return cap, f"capped at {rules.max_participation:.1%} of average volume {average:,.0f}"
    return wanted, ""
