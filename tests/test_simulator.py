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

    assert len(trades) == 1
    assert trades[0].price < 100.0
    assert trades[0].cost < 0.0
    assert equity_curve[0] == 100.0
