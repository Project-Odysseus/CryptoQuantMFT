"""Order-book-based signal helpers for Phase 3 feature engineering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class OrderBookImbalanceSignal:
    """A lightweight signal derived from top-of-book volume imbalance."""

    value: float
    bid_volume: float
    ask_volume: float
    total_volume: float


@dataclass(slots=True)
class MicroPriceSignal:
    """A price signal derived from the volume-weighted top-of-book liquidity."""

    value: float
    bid_volume: float
    ask_volume: float
    total_volume: float


@dataclass(slots=True)
class VolumeDeltaSignal:
    """A simple flow-pressure signal derived from the signed volume imbalance."""

    value: float
    bid_volume: float
    ask_volume: float
    total_volume: float


class OrderBookSignalEngine:
    """Compute simple, order-book-driven signals from a depth snapshot."""

    def __init__(self, depth_levels: int = 5) -> None:
        """Initialize the object with its runtime state."""
        self.depth_levels = depth_levels

    def compute_obi(self, snapshot: Any) -> OrderBookImbalanceSignal:
        """Compute an order-book imbalance signal from the top depth levels."""
        bids = snapshot.bids[: self.depth_levels]
        asks = snapshot.asks[: self.depth_levels]

        if not bids or not asks:
            raise ValueError("Order book snapshot must contain both bids and asks")

        bid_volume = sum(volume for _, volume in bids)
        ask_volume = sum(volume for _, volume in asks)
        total_volume = bid_volume + ask_volume
        value = 0.0 if total_volume == 0 else (bid_volume - ask_volume) / total_volume

        return OrderBookImbalanceSignal(
            value=round(value, 10),
            bid_volume=round(bid_volume, 10),
            ask_volume=round(ask_volume, 10),
            total_volume=round(total_volume, 10),
        )

    def compute_micro_price(self, snapshot: Any) -> MicroPriceSignal:
        """Compute a volume-weighted micro-price signal from the top depth levels."""
        bids = snapshot.bids[: self.depth_levels]
        asks = snapshot.asks[: self.depth_levels]

        if not bids or not asks:
            raise ValueError("Order book snapshot must contain both bids and asks")

        bid_volume = sum(volume for _, volume in bids)
        ask_volume = sum(volume for _, volume in asks)
        total_volume = bid_volume + ask_volume

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        value = (best_bid * ask_volume + best_ask * bid_volume) / total_volume if total_volume > 0 else (best_bid + best_ask) / 2.0

        return MicroPriceSignal(
            value=round(value, 10),
            bid_volume=round(bid_volume, 10),
            ask_volume=round(ask_volume, 10),
            total_volume=round(total_volume, 10),
        )

    def compute_volume_delta(self, snapshot: Any) -> VolumeDeltaSignal:
        """Compute a simple flow-pressure signal from signed top-of-book volume imbalance."""
        bids = snapshot.bids[: self.depth_levels]
        asks = snapshot.asks[: self.depth_levels]

        if not bids or not asks:
            raise ValueError("Order book snapshot must contain both bids and asks")

        bid_volume = sum(volume for _, volume in bids)
        ask_volume = sum(volume for _, volume in asks)
        total_volume = bid_volume + ask_volume
        value = 0.0 if total_volume == 0 else (bid_volume - ask_volume) / total_volume

        return VolumeDeltaSignal(
            value=round(value, 10),
            bid_volume=round(bid_volume, 10),
            ask_volume=round(ask_volume, 10),
            total_volume=round(total_volume, 10),
        )
