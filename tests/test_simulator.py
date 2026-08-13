"""Tests for the lightweight event-driven simulator."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backtest import EventDrivenSimulator
from src.backtest.costs import CostModel
from src.storage.order_book import OrderBookSnapshot


def test_simulator_applies_cost_model_to_trade_prices() -> None:
    """The simulator should adjust fill prices when a cost model is supplied."""
    cost_model = CostModel(exchange="mock", taker_fee=0.5, fx_spread_bps=100)
    simulator = EventDrivenSimulator(max_slippage=0.0, cost_model=cost_model)

    snapshots = [
        OrderBookSnapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0)],
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        ),
        OrderBookSnapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0)],
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
        ),
    ]

    trades, equity_curve = simulator.run(snapshots, [1.0, 0.0])

    assert len(trades) == 2
    assert trades[0].side == "buy"
    assert trades[1].side == "sell"
    assert trades[1].price < 100.0
    assert trades[1].cost > 0.0
    assert equity_curve[0] == 100.0


def test_simulator_uses_book_depth_for_partial_fills() -> None:
    """The simulator should fill against multiple book levels when the size exceeds the top level."""
    simulator = EventDrivenSimulator(latency_ms=0, max_slippage=0.05, position_size=2.0)
    snapshots = [
        OrderBookSnapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0), (102.0, 1.0)],
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
    ]

    trades, _ = simulator.run(snapshots, [1.0])

    assert len(trades) == 1
    assert trades[0].size == 2.0
    assert trades[0].price > 101.0


def test_simulator_adds_queue_and_adverse_selection_penalties() -> None:
    """The simulator should apply depth-based impact and adverse-selection penalties to fills."""
    simulator = EventDrivenSimulator(
        latency_ms=0,
        max_slippage=0.2,
        queue_position_penalty=0.001,
        impact_penalty=0.002,
        adverse_selection_penalty=0.02,
    )
    snapshots = [
        OrderBookSnapshot(
            bids=[(100.0, 0.5)],
            asks=[(101.0, 0.5)],
            timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        ),
        OrderBookSnapshot(
            bids=[(101.0, 0.5)],
            asks=[(102.0, 0.5)],
            timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
        ),
    ]

    trades, _ = simulator.run(snapshots, [0.0, 1.0])

    assert len(trades) == 1
    assert trades[0].side == "buy"
    assert trades[0].price > 102.0
