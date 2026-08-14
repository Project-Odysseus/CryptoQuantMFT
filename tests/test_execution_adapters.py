"""Tests for execution adapter routing and reconciliation."""

from __future__ import annotations

from datetime import datetime

from src.execution.adapters import ExecutionRouter, FiriExecutionAdapter, KrakenExecutionAdapter


def test_kraken_adapter_tracks_local_order_state() -> None:
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    report = adapter.submit_order(
        order_id="kraken-1",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
    )

    assert report.status == "SUBMITTED"
    reconciliation = adapter.reconcile_order_state(
        order_id="kraken-1",
        remote_status="FILLED",
        remote_filled_size=0.25,
        remote_fill_price=100.0,
        remote_fee=0.25,
    )

    assert reconciliation.status == "FILLED"
    assert reconciliation.filled_size == 0.25
    assert adapter.get_order_status(order_id="kraken-1").status == "FILLED"


def test_firi_adapter_tracks_local_order_state() -> None:
    adapter = FiriExecutionAdapter(api_key="firi-key")

    report = adapter.submit_order(
        order_id="firi-1",
        side="sell",
        size=0.50,
        price=101.0,
        timestamp=datetime(2024, 1, 1, 12, 5, 0),
    )

    assert report.status == "SUBMITTED"
    reconciliation = adapter.reconcile_order_state(
        order_id="firi-1",
        remote_status="PARTIALLY_FILLED",
        remote_filled_size=0.25,
        remote_fill_price=100.5,
    )

    assert reconciliation.status == "PARTIALLY_FILLED"
    assert reconciliation.filled_size == 0.25


def test_execution_router_builds_exchange_specific_adapter() -> None:
    router = ExecutionRouter(mode="live", exchange="kraken")

    assert router.adapter is not None
    assert router.adapter.name == "kraken"


def test_execution_router_uses_sandbox_adapter_for_dry_run_exchange() -> None:
    router = ExecutionRouter(mode="live_dry_run", exchange="firi")

    assert router.adapter is not None
    assert router.adapter.name == "sandbox"
    assert router.adapter.exchange_name == "firi"
