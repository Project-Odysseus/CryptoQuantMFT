"""Tests for the order book reconstruction helpers."""

from __future__ import annotations

from src.storage.order_book import OrderBookReconstructor, OrderBookSnapshot


def test_reconstructor_builds_depth_metrics() -> None:
    """The reconstructor should compute core L2 metrics from a simple book snapshot."""
    reconstructor = OrderBookReconstructor(depth_levels=3)
    snapshot = OrderBookSnapshot(
        bids=[(100.0, 2.0), (99.9, 1.0), (99.8, 0.5)],
        asks=[(100.2, 1.5), (100.3, 0.8), (100.4, 0.2)],
    )

    metrics = reconstructor.reconstruct(snapshot)

    assert metrics.mid_price == 100.1
    assert metrics.spread == 0.2
    assert metrics.bid_volume == 3.5
    assert metrics.ask_volume == 2.5
    assert metrics.imbalance == 0.1666666667
    assert metrics.micro_price > 100.0
