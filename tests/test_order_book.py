"""Tests for the order book reconstruction helpers."""

from __future__ import annotations

from src.signals import MicroPriceSignal, OrderBookImbalanceSignal, VolumeDeltaSignal
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


def test_obi_signal_is_computed_from_depth_levels() -> None:
    """The OBI helper should summarize bid/ask pressure into a bounded signal."""
    reconstructor = OrderBookReconstructor(depth_levels=3)
    snapshot = OrderBookSnapshot(
        bids=[(100.0, 2.0), (99.9, 1.0), (99.8, 0.5)],
        asks=[(100.2, 1.5), (100.3, 0.8), (100.4, 0.2)],
    )

    signal = reconstructor.compute_obi(snapshot)

    assert isinstance(signal, OrderBookImbalanceSignal)
    assert signal.value == 0.1666666667
    assert signal.bid_volume == 3.5
    assert signal.ask_volume == 2.5
    assert signal.total_volume == 6.0


def test_micro_price_signal_is_computed_from_depth_levels() -> None:
    """The micro-price helper should weight the best bid/ask by top-of-book depth."""
    reconstructor = OrderBookReconstructor(depth_levels=3)
    snapshot = OrderBookSnapshot(
        bids=[(100.0, 2.0), (99.9, 1.0), (99.8, 0.5)],
        asks=[(100.2, 1.5), (100.3, 0.8), (100.4, 0.2)],
    )

    signal = reconstructor.compute_micro_price(snapshot)

    assert isinstance(signal, MicroPriceSignal)
    assert signal.value == 100.1166666667
    assert signal.bid_volume == 3.5
    assert signal.ask_volume == 2.5
    assert signal.total_volume == 6.0


def test_volume_delta_signal_is_computed_from_depth_levels() -> None:
    """The volume-delta helper should summarize bid/ask flow pressure into a signed value."""
    reconstructor = OrderBookReconstructor(depth_levels=3)
    snapshot = OrderBookSnapshot(
        bids=[(100.0, 2.0), (99.9, 1.0), (99.8, 0.5)],
        asks=[(100.2, 1.5), (100.3, 0.8), (100.4, 0.2)],
    )

    signal = reconstructor.compute_volume_delta(snapshot)

    assert isinstance(signal, VolumeDeltaSignal)
    assert signal.value == 0.1666666667
    assert signal.bid_volume == 3.5
    assert signal.ask_volume == 2.5
    assert signal.total_volume == 6.0
