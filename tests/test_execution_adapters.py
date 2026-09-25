"""Tests for execution adapter routing and reconciliation."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
import pytest

from src.execution.adapters import ExecutionRouter, FiriExecutionAdapter, KrakenExecutionAdapter, SandboxExecutionAdapter
from src.execution.reconciliation import SessionAccountStateTracker


def _permissive_pair_metadata_response(pair_code: str) -> dict[str, object]:
    """Build an AssetPairs-shaped response with minimums low enough not to block test orders."""
    return {
        "error": [],
        "result": {
            pair_code: {
                "wsname": pair_code,
                "altname": pair_code,
                "status": "online",
                "ordermin": "0.00001",
                "costmin": "0.01",
                "tick_size": "0.1",
                "pair_decimals": 1,
                "lot_decimals": 8,
            }
        },
    }


def test_kraken_adapter_tracks_local_order_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test test kraken adapter tracks local order state."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return _permissive_pair_metadata_response("XXBTZEUR")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10000.0"}}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["abc123"]}}
        if endpoint == "QueryOrders":
            return {"error": [], "result": {"abc123": {"status": "closed", "vol_exec": "0.25", "price": "100.0", "fee": "0.25"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

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


def test_adapter_reconcile_account_state_merges_partial_snapshot() -> None:
    """Test test adapter preserves existing balances and positions when the remote snapshot is partial."""
    adapter = FiriExecutionAdapter(api_key="firi-key")
    adapter._balances = {"NOK": 1000.0, "EUR": 100.0}
    adapter._positions = {"BTC": 0.25, "ETH": 0.5}

    summary = adapter.reconcile_account_state(
        balances={"NOK": 950.0},
        positions={"BTC": 0.1},
    )

    assert summary["merged_balances"]["EUR"] == 100.0
    assert summary["merged_positions"]["ETH"] == 0.5
    assert adapter.get_account_snapshot()["balances"]["EUR"] == 100.0
    assert adapter.get_account_snapshot()["positions"]["ETH"] == 0.5


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


def test_sandbox_adapter_tracks_positions_from_order_symbol() -> None:
    """Adapter account reconciliation should derive the asset key from the traded symbol."""
    adapter = SandboxExecutionAdapter(exchange_name="firi")
    adapter._balances = {"EUR": 1000.0}
    adapter._base_currency = "EUR"

    report = adapter.submit_order(
        order_id="eth-buy",
        side="buy",
        size=1.0,
        price=200.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="ETH/EUR",
    )

    snapshot = adapter.get_account_snapshot()

    assert report.status == "FILLED"
    assert snapshot["positions"]["ETH"] == 1.0
    assert "BTC" not in snapshot["positions"]


def test_adapter_tracks_real_entry_price_and_open_timestamp_across_fills() -> None:
    """The base adapter should track a weighted-average entry price and open time, and clear them when flat again."""
    adapter = SandboxExecutionAdapter(exchange_name="kraken")
    adapter._balances = {"EUR": 1000.0}
    adapter._base_currency = "EUR"

    adapter.submit_order(
        order_id="btc-buy-1",
        side="buy",
        size=1.0,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )
    snapshot_after_open = adapter.get_account_snapshot()
    assert snapshot_after_open["position_opened_at"]["BTC"] == datetime(2024, 1, 1, 12, 0, 0)
    assert snapshot_after_open["position_entry_price"]["BTC"] == 100.0

    # Adding to the position should move the entry price but not the open time.
    adapter.submit_order(
        order_id="btc-buy-2",
        side="buy",
        size=1.0,
        price=200.0,
        timestamp=datetime(2024, 1, 1, 12, 5, 0),
        symbol="BTC/EUR",
    )
    snapshot_after_add = adapter.get_account_snapshot()
    assert snapshot_after_add["position_opened_at"]["BTC"] == datetime(2024, 1, 1, 12, 0, 0)
    assert snapshot_after_add["position_entry_price"]["BTC"] == 150.0

    # Fully closing the position should clear both.
    adapter.submit_order(
        order_id="btc-sell-1",
        side="sell",
        size=2.0,
        price=250.0,
        timestamp=datetime(2024, 1, 1, 12, 10, 0),
        symbol="BTC/EUR",
    )
    snapshot_after_close = adapter.get_account_snapshot()
    assert "BTC" not in snapshot_after_close["position_opened_at"]
    assert "BTC" not in snapshot_after_close["position_entry_price"]


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
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10000.0"}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    report = adapter.submit_order(
        order_id="kraken-2",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="ETH/EUR",
    )
    status_report = adapter.get_order_status(order_id="kraken-2")

    assert report.status == "SUBMITTED"
    add_order_calls = [call for call in calls if call[0] == "AddOrder"]
    assert add_order_calls
    assert add_order_calls[0][1]["pair"] == "XETHZEUR"
    assert status_report.status == "FILLED"
    assert status_report.filled_size == 0.25


def test_kraken_adapter_recover_execution_state_normalizes_kraken_payloads(monkeypatch) -> None:
    """Kraken-shaped balance and order payloads should reconcile into the generic adapter state."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        """Perform the fake private request operation."""
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["abc123"], "descr": {"order": "buy 0.25 BTC @ 100"}}}
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10000.0"}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return _permissive_pair_metadata_response("XXBTZEUR")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)
    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)

    report = adapter.submit_order(
        order_id="kraken-recovery",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )

    summary = adapter.recover_execution_state(
        remote_snapshot={"error": [], "result": {"ZEUR": "850.0", "XXBT": "0.25"}},
        remote_orders={
            "error": [],
            "result": {
                "abc123": {
                    "status": "closed",
                    "vol": "0.25",
                    "vol_exec": "0.25",
                    "price": "100.0",
                    "fee": "0.25",
                    "descr": {"pair": "XXBTZEUR", "type": "buy"},
                }
            },
        },
    )

    snapshot = adapter.get_account_snapshot()

    assert report.status == "SUBMITTED"
    assert summary["recovered_order_ids"] == ["kraken-recovery"]
    assert summary["account_reconciliation"]["remote_balances"]["EUR"] == 850.0
    assert summary["account_reconciliation"]["remote_positions"]["BTC"] == 0.25
    assert snapshot["balances"]["EUR"] == 850.0
    assert snapshot["positions"]["BTC"] == 0.25
    assert adapter._orders["kraken-recovery"].remote_order_id == "abc123"
    assert adapter._orders["kraken-recovery"].status == "FILLED"


def test_kraken_adapter_handles_missing_credentials_non_destructively(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit, status, and cancel should stay local when Kraken credentials are missing."""
    adapter = KrakenExecutionAdapter(api_key="", api_secret="")

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return _permissive_pair_metadata_response("XXBTZEUR")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)

    report = adapter.submit_order(
        order_id="kraken-local",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )
    status_report = adapter.get_order_status(order_id="kraken-local")
    cancel_report = adapter.cancel_order(order_id="kraken-local")

    assert report.status == "SUBMITTED"
    assert "not configured" in (report.message or "").lower()
    assert status_report.status == "SUBMITTED"
    assert "not configured" in (status_report.message or "").lower()
    assert cancel_report.status == "REJECTED"
    assert adapter._orders["kraken-local"].remote_order_id is None


def test_kraken_adapter_uses_remote_order_id_for_cancel(monkeypatch) -> None:
    """Cancel should target Kraken's remote txid when it is known."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        """Perform the fake private request operation."""
        calls.append((endpoint, params))
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["abc123"]}}
        if endpoint == "CancelOrder":
            return {"error": [], "result": {"count": 1}}
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10000.0"}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return _permissive_pair_metadata_response("XXBTZEUR")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)
    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)

    adapter.submit_order(
        order_id="kraken-cancel",
        side="buy",
        size=0.25,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )
    cancel_report = adapter.cancel_order(order_id="kraken-cancel")

    assert cancel_report.status == "CANCELED"
    assert calls[-1] == ("CancelOrder", {"txid": "abc123"})


def test_kraken_adapter_submit_order_rejects_size_below_exchange_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    """A strategy-driven order below Kraken's minimum size should be rejected before AddOrder is called."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[str] = []

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return {
            "error": [],
            "result": {
                "XXBTZEUR": {
                    "wsname": "XBT/EUR",
                    "altname": "XBTEUR",
                    "status": "online",
                    "ordermin": "0.00005",
                    "costmin": "0.45",
                    "tick_size": "0.1",
                    "pair_decimals": 1,
                    "lot_decimals": 8,
                }
            },
        }

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        calls.append(endpoint)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    report = adapter.submit_order(
        order_id="kraken-too-small",
        side="buy",
        size=0.00001,
        price=50000.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )

    assert report.status == "REJECTED"
    assert "minimum size" in (report.message or "").lower()
    assert calls == []


def test_kraken_adapter_submit_order_rejects_notional_below_exchange_minimum_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A strategy-driven order sized under Kraken's minimum notional should be rejected before AddOrder is called."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[str] = []

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return {
            "error": [],
            "result": {
                "XXBTZEUR": {
                    "wsname": "XBT/EUR",
                    "altname": "XBTEUR",
                    "status": "online",
                    "ordermin": "0.00005",
                    "costmin": "10.0",
                    "tick_size": "0.1",
                    "pair_decimals": 1,
                    "lot_decimals": 8,
                }
            },
        }

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        calls.append(endpoint)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    # size (0.0001) is above ordermin (0.00005) but the notional (0.0001 * 100 = 0.01)
    # is well under costmin (10.0).
    report = adapter.submit_order(
        order_id="kraken-too-cheap",
        side="buy",
        size=0.0001,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )

    assert report.status == "REJECTED"
    assert "minimum cost" in (report.message or "").lower()
    assert calls == []


def test_kraken_adapter_submit_order_rejects_insufficient_quote_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A strategy-driven buy order that exceeds the available EUR balance should be rejected before AddOrder is called."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    add_order_calls: list[str] = []

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return _permissive_pair_metadata_response("XXBTZEUR")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10.0"}}
        add_order_calls.append(endpoint)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    report = adapter.submit_order(
        order_id="kraken-underfunded",
        side="buy",
        size=1.0,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )

    assert report.status == "REJECTED"
    assert "insufficient" in (report.message or "").lower()
    assert add_order_calls == []


def test_kraken_adapter_submit_order_rejects_insufficient_base_position_for_sell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A strategy-driven sell order larger than the held position should be rejected before AddOrder is called."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    add_order_calls: list[str] = []

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return _permissive_pair_metadata_response("XXBTZEUR")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if endpoint == "Balance":
            return {"error": [], "result": {"XXBT": "0.01"}}
        add_order_calls.append(endpoint)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    report = adapter.submit_order(
        order_id="kraken-oversold",
        side="sell",
        size=1.0,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )

    assert report.status == "REJECTED"
    assert "insufficient" in (report.message or "").lower()
    assert add_order_calls == []


def test_kraken_adapter_submit_order_rounds_size_to_exchange_precision(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid order should be rounded down to Kraken's lot precision before submission."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    add_order_params: dict[str, object] = {}

    def fake_request_json(self: KrakenExecutionAdapter, method: str, url: str, *, params=None, headers=None, data=None) -> dict[str, object]:
        return {
            "error": [],
            "result": {
                "XXBTZEUR": {
                    "wsname": "XBT/EUR",
                    "altname": "XBTEUR",
                    "status": "online",
                    "ordermin": "0.00005",
                    "costmin": "0.45",
                    "tick_size": "0.1",
                    "pair_decimals": 1,
                    "lot_decimals": 4,
                }
            },
        }

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10000.0"}}
        if endpoint == "AddOrder":
            add_order_params.update(params)
            return {"error": [], "result": {"txid": ["abc123"]}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    report = adapter.submit_order(
        order_id="kraken-precision",
        side="buy",
        size=0.123456789,
        price=100.0,
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTC/EUR",
    )

    assert report.status == "SUBMITTED"
    assert add_order_params["volume"] == "0.1234"


def test_kraken_adapter_verify_dry_run_exercises_private_endpoints_non_destructively(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run verification should authenticate, normalize state, and probe non-destructive private endpoints."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((endpoint, params))
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "1000.0", "XXBT": "0.1"}}
        if endpoint == "OpenOrders":
            return {
                "error": [],
                "result": {
                    "open": {
                        "abc123": {
                            "status": "open",
                            "vol": "0.1",
                            "vol_exec": "0.0",
                            "price": "25000.0",
                            "descr": {"pair": "XXBTZEUR", "type": "buy"},
                        }
                    }
                },
            }
        if endpoint == "ClosedOrders":
            return {"error": [], "result": {"closed": {}}}
        if endpoint == "QueryOrders":
            return {
                "error": [],
                "result": {
                    "abc123": {
                        "status": "open",
                        "vol": "0.1",
                        "vol_exec": "0.0",
                        "price": "25000.0",
                        "descr": {"pair": "XXBTZEUR", "type": "buy"},
                    }
                },
            }
        if endpoint == "CancelOrder":
            return {"error": ["EOrder:Unknown order"]}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"descr": {"order": "buy 0.0002 BTC/EUR @ market"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    summary = adapter.verify_dry_run(symbol="BTC/EUR", size=0.0002)

    assert summary["status"] == "passed"
    assert summary["balance_snapshot"]["balances"]["EUR"] == 1000.0
    assert summary["balance_snapshot"]["positions"]["BTC"] == 0.1
    assert summary["open_order_count"] == 1
    assert [check["name"] for check in summary["checks"]] == [
        "balance_snapshot",
        "open_orders",
        "closed_orders",
        "recovery_state",
        "order_status",
        "cancel_order",
        "validate_order",
    ]
    assert summary["recovered_order_count"] == 1
    assert summary["recovered_order_ids"] == ["abc123"]
    assert calls[3] == ("QueryOrders", {"txid": "abc123", "trades": "false"})
    assert calls[4] == ("CancelOrder", {"txid": "DRYRUNVERIFY-CANCEL"})


def test_kraken_adapter_verify_dry_run_uses_expected_unknown_order_probes_when_no_open_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without open orders, the verification should still exercise status/cancel paths safely."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "1000.0"}}
        if endpoint == "OpenOrders":
            return {"error": [], "result": {"open": {}}}
        if endpoint == "ClosedOrders":
            return {"error": [], "result": {"closed": {}}}
        if endpoint in {"QueryOrders", "CancelOrder"}:
            return {"error": ["EOrder:Unknown order"]}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"descr": {"order": "buy 0.0002 BTC/EUR @ market"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    summary = adapter.verify_dry_run(symbol="BTC/EUR", size=0.0002)

    assert summary["status"] == "passed"
    assert summary["recovered_order_count"] == 0
    status_check = next(check for check in summary["checks"] if check["name"] == "order_status")
    cancel_check = next(check for check in summary["checks"] if check["name"] == "cancel_order")
    assert status_check["ok"] is True
    assert cancel_check["ok"] is True
    assert "expected non-destructive error" in status_check["message"]


def test_kraken_adapter_verify_dry_run_uses_closed_order_history_for_recovery_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed-order history should still validate symbol/order-id normalization when no open orders exist."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((endpoint, params))
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "900.0"}}
        if endpoint == "OpenOrders":
            return {"error": [], "result": {"open": {}}}
        if endpoint == "ClosedOrders":
            return {
                "error": [],
                "result": {
                    "closed": {
                        "closed123": {
                            "status": "closed",
                            "vol": "0.05",
                            "vol_exec": "0.05",
                            "price": "30000.0",
                            "fee": "1.50",
                            "descr": {"pair": "XXBTZEUR", "type": "sell"},
                        }
                    }
                },
            }
        if endpoint == "QueryOrders":
            return {
                "error": [],
                "result": {
                    "closed123": {
                        "status": "closed",
                        "vol": "0.05",
                        "vol_exec": "0.05",
                        "price": "30000.0",
                        "fee": "1.50",
                        "descr": {"pair": "XXBTZEUR", "type": "sell"},
                    }
                },
            }
        if endpoint == "CancelOrder":
            return {"error": ["EOrder:Invalid order"]}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"descr": {"order": "buy 0.0002 BTC/EUR @ market"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    summary = adapter.verify_dry_run(symbol="BTC/EUR", size=0.0002)

    assert summary["status"] == "passed"
    assert summary["open_order_count"] == 0
    assert summary["closed_order_count"] == 1
    assert summary["recovered_order_count"] == 1
    assert summary["recovered_order_ids"] == ["closed123"]
    assert calls[3] == ("QueryOrders", {"txid": "closed123", "trades": "false"})
    assert adapter.list_orders()[0].symbol == "BTC/EUR"
    assert adapter.list_orders()[0].remote_order_id == "closed123"


def test_kraken_adapter_verify_dry_run_fails_without_credentials() -> None:
    """Verification should fail fast when Kraken credentials are missing."""
    adapter = KrakenExecutionAdapter(api_key="", api_secret="")

    summary = adapter.verify_dry_run()

    assert summary["status"] == "failed"
    assert summary["checks"][0]["name"] == "credentials"
    assert summary["checks"][0]["ok"] is False


def test_kraken_adapter_fetches_asset_pair_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pair metadata should expose Kraken minimums and precision in normalized form."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    def fake_request_json(
        self: KrakenExecutionAdapter,
        method: str,
        url: str,
        *,
        params=None,
        headers=None,
        data=None,
    ) -> dict[str, object]:
        assert method == "GET"
        assert url == "https://api.kraken.com/0/public/AssetPairs"
        assert params == {"pair": "XXBTZEUR"}
        return {
            "error": [],
            "result": {
                "XXBTZEUR": {
                    "wsname": "XBT/EUR",
                    "altname": "XBTEUR",
                    "status": "online",
                    "ordermin": "0.00005",
                    "costmin": "0.45",
                    "tick_size": "0.1",
                    "pair_decimals": 1,
                    "lot_decimals": 8,
                }
            },
        }

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)

    metadata = adapter.fetch_asset_pair_metadata(symbol="BTC/EUR")

    assert metadata["pair_code"] == "XXBTZEUR"
    assert metadata["status"] == "online"
    assert metadata["ordermin"] == 0.00005
    assert metadata["costmin"] == 0.45
    assert metadata["tick_size"] == 0.1
    assert metadata["lot_decimals"] == 8


def test_kraken_adapter_preview_quote_order_validates_without_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quote-order previews should round by Kraken lot size and use validate=true only after passing pre-checks."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    private_calls: list[tuple[str, dict[str, object]]] = []

    def fake_request_json(
        self: KrakenExecutionAdapter,
        method: str,
        url: str,
        *,
        params=None,
        headers=None,
        data=None,
    ) -> dict[str, object]:
        if url.endswith("/AssetPairs"):
            return {
                "error": [],
                "result": {
                    "XXBTZEUR": {
                        "status": "online",
                        "ordermin": "0.00005",
                        "costmin": "0.45",
                        "tick_size": "0.1",
                        "pair_decimals": 1,
                        "lot_decimals": 8,
                    }
                },
            }
        if url.endswith("/Ticker"):
            return {"error": [], "result": {"XXBTZEUR": {"a": ["68000.0"], "b": ["67999.9"], "c": ["68000.0"]}}}
        raise AssertionError(f"unexpected request: {method} {url}")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        private_calls.append((endpoint, params))
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "14.7"}}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"descr": {"order": "buy 0.00004411 BTC/EUR @ market"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    preview = adapter.preview_quote_order(symbol="BTC/EUR", quote_amount=3.0)

    assert preview["sufficient_balance"] is True
    assert preview["meets_minimum_size"] is False
    assert preview["meets_minimum_cost"] is True
    assert preview["rounded_size"] == pytest.approx(0.00004411)
    assert preview["validation"] is None
    assert preview["can_submit"] is False
    assert private_calls == [("Balance", {})]


def test_kraken_adapter_preview_quote_order_calls_validate_when_prechecks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Previews above Kraken minimums should invoke validate=true and still avoid live submission."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    private_calls: list[tuple[str, dict[str, object]]] = []

    def fake_request_json(
        self: KrakenExecutionAdapter,
        method: str,
        url: str,
        *,
        params=None,
        headers=None,
        data=None,
    ) -> dict[str, object]:
        if url.endswith("/AssetPairs"):
            return {
                "error": [],
                "result": {
                    "XXBTZEUR": {
                        "status": "online",
                        "ordermin": "0.00005",
                        "costmin": "0.45",
                        "tick_size": "0.1",
                        "pair_decimals": 1,
                        "lot_decimals": 8,
                    }
                },
            }
        if url.endswith("/Ticker"):
            return {"error": [], "result": {"XXBTZEUR": {"a": ["68000.0"], "b": ["67999.9"], "c": ["68000.0"]}}}
        raise AssertionError(f"unexpected request: {method} {url}")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        private_calls.append((endpoint, params))
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "14.7"}}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"descr": {"order": "buy 0.00007352 BTC/EUR @ market"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    preview = adapter.preview_quote_order(symbol="BTC/EUR", quote_amount=5.0)

    assert preview["rounded_size"] == pytest.approx(0.00007352)
    assert preview["estimated_cost"] == pytest.approx(4.99936)
    assert preview["meets_minimum_size"] is True
    assert preview["validation"]["validated"] is True
    assert preview["validation"]["description"] == "buy 0.00007352 BTC/EUR @ market"
    assert preview["can_submit"] is True
    assert private_calls[1] == (
        "AddOrder",
        {"pair": "XXBTZEUR", "type": "buy", "ordertype": "market", "volume": "0.00007352", "validate": "true"},
    )


def test_kraken_adapter_submit_quote_order_blocks_if_preview_cannot_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual submission should stop before AddOrder if preview checks fail."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    monkeypatch.setattr(
        KrakenExecutionAdapter,
        "preview_quote_order",
        lambda self, **_: {
            "can_submit": False,
            "sufficient_balance": True,
            "meets_minimum_size": False,
            "meets_minimum_cost": True,
            "validation_error": None,
        },
    )

    with pytest.raises(RuntimeError, match="below Kraken minimum size"):
        adapter.submit_quote_order(symbol="BTC/EUR", quote_amount=3.0)


def test_kraken_adapter_submit_quote_order_submits_market_order_and_refreshes_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual submission should place a market order, reconcile status, and refresh balances."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    private_calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        KrakenExecutionAdapter,
        "preview_quote_order",
        lambda self, **_: {
            "can_submit": True,
            "symbol": "BTC/EUR",
            "side": "buy",
            "quote_currency": "EUR",
            "requested_quote_amount": 3.5,
            "reference_price": 68000.0,
            "rounded_size": 0.00005145,
            "estimated_cost": 3.4986,
            "validation": {"validated": True, "description": "buy 0.00005145 BTC/EUR @ market"},
        },
    )

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        private_calls.append((endpoint, params))
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["remote123"], "descr": {"order": "buy 0.00005145 BTC/EUR @ market"}}}
        if endpoint == "QueryOrders":
            return {
                "error": [],
                "result": {
                    "remote123": {
                        "status": "closed",
                        "vol": "0.00005145",
                        "vol_exec": "0.00005145",
                        "price": "68000.0",
                        "fee": "0.01",
                        "descr": {"pair": "XXBTZEUR", "type": "buy"},
                    }
                },
            }
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10.68", "XXBT": "0.00005145"}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    submission = adapter.submit_quote_order(symbol="BTC/EUR", quote_amount=3.5, order_id="manual-1")

    assert submission["order_id"] == "manual-1"
    assert submission["remote_order_id"] == "remote123"
    assert submission["status"] == "FILLED"
    assert submission["filled_size"] == pytest.approx(0.00005145)
    assert submission["fill_price"] == pytest.approx(68000.0)
    assert submission["fee"] == pytest.approx(0.01)
    assert submission["submit_description"] == "buy 0.00005145 BTC/EUR @ market"
    assert submission["account_snapshot"]["balances"]["EUR"] == pytest.approx(10.68)
    assert submission["account_snapshot"]["positions"]["BTC"] == pytest.approx(0.00005145)
    assert adapter.get_account_snapshot()["balances"]["EUR"] == pytest.approx(10.68)
    assert private_calls == [
        ("AddOrder", {"pair": "XXBTZEUR", "type": "buy", "ordertype": "market", "volume": "0.00005145"}),
        ("QueryOrders", {"txid": "remote123", "trades": "false"}),
        ("Balance", {}),
    ]


def test_kraken_adapter_preview_close_position_uses_current_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close previews should validate the full current base-asset position without submitting it."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    private_calls: list[tuple[str, dict[str, object]]] = []

    def fake_request_json(
        self: KrakenExecutionAdapter,
        method: str,
        url: str,
        *,
        params=None,
        headers=None,
        data=None,
    ) -> dict[str, object]:
        if url.endswith("/AssetPairs"):
            return {
                "error": [],
                "result": {
                    "XXBTZEUR": {
                        "status": "online",
                        "ordermin": "0.00005",
                        "costmin": "0.45",
                        "tick_size": "0.1",
                        "pair_decimals": 1,
                        "lot_decimals": 8,
                    }
                },
            }
        if url.endswith("/Ticker"):
            return {"error": [], "result": {"XXBTZEUR": {"a": ["68000.1"], "b": ["68000.0"], "c": ["68000.0"]}}}
        raise AssertionError(f"unexpected request: {method} {url}")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        private_calls.append((endpoint, params))
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "10.0", "XXBT": "0.00005145"}}
        if endpoint == "AddOrder":
            return {"error": [], "result": {"descr": {"order": "sell 0.00005145 BTC/EUR @ market"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)
    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    preview = adapter.preview_close_position(symbol="BTC/EUR")

    assert preview["has_position"] is True
    assert preview["rounded_size"] == pytest.approx(0.00005145)
    assert preview["estimated_proceeds"] == pytest.approx(3.4986)
    assert preview["validation"]["validated"] is True
    assert preview["validation"]["side"] == "sell"
    assert private_calls[1] == (
        "AddOrder",
        {"pair": "XXBTZEUR", "type": "sell", "ordertype": "market", "volume": "0.00005145", "validate": "true"},
    )


def test_kraken_adapter_submit_close_position_submits_market_sell_and_refreshes_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual close submission should sell the full position and refresh account state."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")
    private_calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        KrakenExecutionAdapter,
        "preview_close_position",
        lambda self, **_: {
            "can_submit": True,
            "symbol": "BTC/EUR",
            "side": "sell",
            "base_asset": "BTC",
            "rounded_size": 0.00005145,
            "reference_price": 68000.0,
            "estimated_proceeds": 3.4986,
            "validation": {"validated": True, "description": "sell 0.00005145 BTC/EUR @ market"},
        },
    )

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        private_calls.append((endpoint, params))
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["close123"], "descr": {"order": "sell 0.00005145 BTC/EUR @ market"}}}
        if endpoint == "QueryOrders":
            return {
                "error": [],
                "result": {
                    "close123": {
                        "status": "closed",
                        "vol": "0.00005145",
                        "vol_exec": "0.00005145",
                        "price": "68000.0",
                        "fee": "0.01",
                        "descr": {"pair": "XXBTZEUR", "type": "sell"},
                    }
                },
            }
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "14.18", "XXBT": "0.0"}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

    submission = adapter.submit_close_position(symbol="BTC/EUR", order_id="close-1")

    assert submission["order_id"] == "close-1"
    assert submission["remote_order_id"] == "close123"
    assert submission["status"] == "FILLED"
    assert submission["filled_size"] == pytest.approx(0.00005145)
    assert submission["fill_price"] == pytest.approx(68000.0)
    assert submission["fee"] == pytest.approx(0.01)
    assert submission["submit_description"] == "sell 0.00005145 BTC/EUR @ market"
    assert submission["account_snapshot"]["balances"]["EUR"] == pytest.approx(14.18)
    assert "BTC" not in submission["account_snapshot"]["positions"]
    assert private_calls == [
        ("AddOrder", {"pair": "XXBTZEUR", "type": "sell", "ordertype": "market", "volume": "0.00005145"}),
        ("QueryOrders", {"txid": "close123", "trades": "false"}),
        ("Balance", {}),
    ]


def test_kraken_private_request_uses_base64_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kraken private requests should use the base64-encoded HMAC signature Kraken expects."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret=base64.b64encode(b"secret-bytes").decode("utf-8"))
    captured: dict[str, object] = {}

    def fake_request_json(
        self: KrakenExecutionAdapter,
        method: str,
        url: str,
        *,
        params=None,
        headers=None,
        data=None,
    ) -> dict[str, object]:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["data"] = data
        return {"error": [], "result": {}}

    monkeypatch.setattr("src.execution.adapters.time.time", lambda: 1700000000.123)
    monkeypatch.setattr(KrakenExecutionAdapter, "_request_json", fake_request_json)

    adapter._private_request(endpoint="Balance", params={})

    nonce = str(int(1700000000.123 * 1000))
    encoded_body = f"nonce={nonce}".encode("utf-8")
    sha256_digest = hashlib.sha256(f"{nonce}{encoded_body.decode('utf-8')}".encode("utf-8")).digest()
    expected_signature = base64.b64encode(
        hmac.new(
            b"secret-bytes",
            b"/0/private/Balance" + sha256_digest,
            hashlib.sha512,
        ).digest()
    ).decode("utf-8")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.kraken.com/0/private/Balance"
    assert captured["data"] == {"nonce": nonce}
    assert captured["headers"]["API-Sign"] == expected_signature


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
