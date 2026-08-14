"""Volatility and trend filters for simple signal gating."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.storage.bar_aggregator import OHLCVBar


@dataclass(slots=True)
class TrendFilterSignal:
    """A simple trend classification based on recent price momentum."""

    value: float
    direction: str
    momentum: float
    volatility: float


class VolatilityTrendFilter:
    """Compute simple ATR-based volatility and momentum trend filters."""

    def __init__(self, lookback: int = 14, atr_multiplier: float = 2.0) -> None:
        """Initialize the object with its runtime state."""
        self.lookback = lookback
        self.atr_multiplier = atr_multiplier

    def compute(self, bars: Sequence[OHLCVBar]) -> TrendFilterSignal:
        """Return a trend filter signal for the latest bar."""
        if not bars:
            raise ValueError("At least one bar is required")

        latest = bars[-1]
        if len(bars) < 2:
            return TrendFilterSignal(value=0.0, direction="neutral", momentum=0.0, volatility=0.0)

        closes = [bar.close for bar in bars[-self.lookback :]]
        previous_close = closes[-2]
        current_close = closes[-1]
        momentum = current_close - previous_close

        true_ranges = []
        for index in range(1, len(bars[-self.lookback :])):
            previous_bar = bars[-self.lookback :][index - 1]
            current_bar = bars[-self.lookback :][index]
            high_low = current_bar.high - current_bar.low
            high_prev_close = abs(current_bar.high - previous_bar.close)
            low_prev_close = abs(current_bar.low - previous_bar.close)
            true_ranges.append(max(high_low, high_prev_close, low_prev_close))

        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
        volatility = atr * self.atr_multiplier

        if momentum > 0 and abs(momentum) > volatility * 0.5:
            direction = "up"
        elif momentum < 0 and abs(momentum) > volatility * 0.5:
            direction = "down"
        else:
            direction = "neutral"

        value = momentum / volatility if volatility > 0 else 0.0

        return TrendFilterSignal(
            value=round(value, 10),
            direction=direction,
            momentum=round(momentum, 10),
            volatility=round(volatility, 10),
        )
