"""Tests for execution adapter routing and reconciliation."""

from __future__ import annotations

from datetime import datetime

from src.execution.adapters import ExecutionRouter, FiriExecutionAdapter, KrakenExecutionAdapter
from src.execution.reconciliation import SessionAccountStateTracker


def test_kraken_adapter_tracks_local_order_state() -> None:
    """Test test kraken adapter tracks local order state."""
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
    """Test test firi adapter tracks local order state."""
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


def test_adapter_reconciles_account_state_against_remote_snapshot() -> None:
    """Test test adapter reconciles account state against remote snapshot."""
    adapter = FiriExecutionAdapter(api_key="firi-key")
    adapter._balances = {"NOK": 1000.0}
    adapter._positions = {"BTC": 0.0}

    summary = adapter.reconcile_account_state(
        balances={"NOK": 950.0},
        positions={"BTC": 0.5},
    )

    assert summary["matched"] is False
    assert summary["balance_mismatches"]["NOK"]["remote"] == 950.0
    assert summary["position_mismatches"]["BTC"]["remote"] == 0.5
    assert adapter.get_account_snapshot()["balances"]["NOK"] == 950.0


def test_adapter_recover_execution_state_reconciles_orders_and_account_snapshot() -> None:
    """Test test adapter can recover remote order and account state after reconnects."""
    adapter = FiriExecutionAdapter(api_key="firi-key")
    report = adapter.submit_order(
        order_id="firi-recovery",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
    )

    summary = adapter.recover_execution_state(
        remote_snapshot={"balances": {"NOK": 850.0}, "positions": {"BTC": 0.25}},
        remote_orders=[
            {
                "order_id": report.order_id,
                "side": "buy",
                "size": 0.25,
                "status": "FILLED",
                "filled_size": 0.25,
                "fill_price": 100.0,
                "fee": 0.25,
            }
        ],
    )

    assert summary["recovered_order_ids"] == [report.order_id]
    assert summary["recovery_status"] == "reconciled"
    assert adapter.get_account_snapshot()["balances"]["NOK"] == 850.0
    assert adapter.get_account_snapshot()["positions"]["BTC"] == 0.25
    assert adapter._orders[report.order_id].status == "FILLED"


def test_session_account_state_tracker_surfaces_recovery_summary() -> None:
    """Test test tracker exposes the recovery summary after adapter recovery."""
    adapter = FiriExecutionAdapter(api_key="firi-key")
    tracker = SessionAccountStateTracker(exchange_name="firi", base_currency="NOK")

    adapter.submit_order(
        order_id="firi-tracker",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
    )
    summary = tracker.recover_execution_state(
        adapter,
        remote_snapshot={"balances": {"NOK": 950.0}, "positions": {"BTC": 0.0}},
    )

    assert summary["recovered_order_count"] == 1
    assert tracker.get_summary()["recovery_summary"]["recovered_order_count"] == 1
    assert tracker.get_summary()["account_reconciliation"]["remote_balances"]["NOK"] == 950.0


def test_execution_router_builds_exchange_specific_adapter() -> None:
    """Test test execution router builds exchange specific adapter."""
    router = ExecutionRouter(mode="live", exchange="kraken")

    assert router.adapter is not None
    assert router.adapter.name == "kraken"


def test_execution_router_uses_sandbox_adapter_for_dry_run_exchange() -> None:
    """Test test execution router uses sandbox adapter for dry run exchange."""
    router = ExecutionRouter(mode="live_dry_run", exchange="firi")

    assert router.adapter is not None
    assert router.adapter.name == "sandbox"
    assert router.adapter.exchange_name == "firi"


def test_kraken_adapter_uses_private_api_for_submit_and_status(monkeypatch) -> None:
    """Test test kraken adapter uses private api for submit and status."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        """Perform the fake private request operation."""
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
    """Test test firi adapter uses rest endpoints for submit and status."""
    adapter = FiriExecutionAdapter(api_key="firi-key")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request_json(self: FiriExecutionAdapter, method: str, url: str, **_: object) -> dict[str, object]:
        """Perform the fake request json operation."""
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
