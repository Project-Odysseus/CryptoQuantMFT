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


def test_kraken_adapter_uses_private_api_for_submit_and_status(monkeypatch) -> None:
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((endpoint, params))
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["abc123"], "descr": {"order": "buy 0.25 BTC @ 100"}}}
        if endpoint == "QueryOrders":
            return {"error": [], "result": {"abc123": {"status": "closed", "vol_exec": "0.25", "price": "100.0", "fee": "0.25"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    report = adapter.submit_order(
        order_id="kraken-2",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
    )
    status_report = adapter.get_order_status(order_id="kraken-2")

    assert report.status == "SUBMITTED"
    assert calls[0][0] == "AddOrder"
    assert status_report.status == "FILLED"
    assert status_report.filled_size == 0.25


def test_firi_adapter_uses_rest_endpoints_for_submit_and_status(monkeypatch) -> None:
    adapter = FiriExecutionAdapter(api_key="firi-key")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request_json(self: FiriExecutionAdapter, method: str, url: str, **_: object) -> dict[str, object]:
        calls.append((method, url, _))
        if method == "POST" and url == "https://api.firi.com/v2/orders":
            return {"id": "firi-123", "status": "filled", "price": "101.0", "filled_size": "0.50", "fee": "0.50"}
        if method == "GET" and url == "https://api.firi.com/v2/orders/firi-123":
            return {"id": "firi-123", "status": "filled", "price": "101.0", "filled_size": "0.50", "fee": "0.50"}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(FiriExecutionAdapter, "_request_json", fake_request_json)

    report = adapter.submit_order(
        order_id="firi-2",
        side="sell",
        size=0.50,
        price=101.0,
        timestamp=datetime(2024, 1, 1, 12, 5, 0),
    )
    status_report = adapter.get_order_status(order_id="firi-2")

    assert report.status == "FILLED"
    assert calls[0][0] == "POST"
    assert status_report.status == "FILLED"
    assert status_report.filled_size == 0.50
