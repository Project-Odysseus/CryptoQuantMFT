"""Helpers for reconstructing simple order book metrics from depth snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.signals.order_book_signals import (
    MicroPriceSignal,
    OrderBookImbalanceSignal,
    OrderBookSignalEngine,
    VolumeDeltaSignal,
)


@dataclass(slots=True)
class OrderBookSnapshot:
    """A simple L2-style order book snapshot."""

    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    timestamp: float | None = None


@dataclass(slots=True)
class OrderBookMetrics:
    """Summary metrics derived from an order book snapshot."""

    mid_price: float
    spread: float
    micro_price: float
    bid_volume: float
    ask_volume: float
    vwap: float
    imbalance: float
    raw: dict[str, Any] | None = None


class OrderBookReconstructor:
    """Compute simple depth-based metrics from a basic order book snapshot."""

    def __init__(self, depth_levels: int = 5) -> None:
        self.depth_levels = depth_levels
        self._signal_engine = OrderBookSignalEngine(depth_levels=depth_levels)

    def compute_obi(self, snapshot: OrderBookSnapshot) -> OrderBookImbalanceSignal:
        """Compute an order-book imbalance signal from the top depth levels."""
        return self._signal_engine.compute_obi(snapshot)

    def compute_micro_price(self, snapshot: OrderBookSnapshot) -> MicroPriceSignal:
        """Compute a volume-weighted micro-price signal from the top depth levels."""
        return self._signal_engine.compute_micro_price(snapshot)

    def compute_volume_delta(self, snapshot: OrderBookSnapshot) -> VolumeDeltaSignal:
        """Compute a simple volume-delta signal from the top depth levels."""
        return self._signal_engine.compute_volume_delta(snapshot)

    def reconstruct(self, snapshot: OrderBookSnapshot) -> OrderBookMetrics:
        """Compute summary metrics from a bid/ask book snapshot."""
        bids = snapshot.bids[: self.depth_levels]
        asks = snapshot.asks[: self.depth_levels]

        if not bids or not asks:
            raise ValueError("Order book snapshot must contain both bids and asks")

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

        bid_volume = sum(volume for _, volume in bids)
        ask_volume = sum(volume for _, volume in asks)
        total_volume = bid_volume + ask_volume
        imbalance = 0.0 if total_volume == 0 else (bid_volume - ask_volume) / total_volume

        weighted_bid_price = sum(price * volume for price, volume in bids)
        weighted_ask_price = sum(price * volume for price, volume in asks)
        vwap = 0.0
        if total_volume > 0:
            vwap = (weighted_bid_price + weighted_ask_price) / (bid_volume + ask_volume)

        micro_price = mid_price
        if total_volume > 0:
            micro_price = (best_bid * ask_volume + best_ask * bid_volume) / total_volume

        return OrderBookMetrics(
            mid_price=round(mid_price, 10),
            spread=round(spread, 10),
            micro_price=round(micro_price, 10),
            bid_volume=round(bid_volume, 10),
            ask_volume=round(ask_volume, 10),
            vwap=round(vwap, 10),
            imbalance=round(imbalance, 10),
            raw={
                "bids": bids,
                "asks": asks,
            },
        )
