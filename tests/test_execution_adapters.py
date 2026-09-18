"""Tests for execution adapter routing and reconciliation."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
import pytest

from src.execution.adapters import ExecutionRouter, FiriExecutionAdapter, KrakenExecutionAdapter, SandboxExecutionAdapter
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
        symbol="ETH/EUR",
    )
    status_report = adapter.get_order_status(order_id="kraken-2")

    assert report.status == "SUBMITTED"
    assert calls[0][0] == "AddOrder"
    assert calls[0][1]["pair"] == "XETHZEUR"
    assert status_report.status == "FILLED"
    assert status_report.filled_size == 0.25


def test_kraken_adapter_recover_execution_state_normalizes_kraken_payloads(monkeypatch) -> None:
    """Kraken-shaped balance and order payloads should reconcile into the generic adapter state."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        """Perform the fake private request operation."""
        if endpoint == "AddOrder":
            return {"error": [], "result": {"txid": ["abc123"], "descr": {"order": "buy 0.25 BTC @ 100"}}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

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


def test_kraken_adapter_handles_missing_credentials_non_destructively() -> None:
    """Submit, status, and cancel should stay local when Kraken credentials are missing."""
    adapter = KrakenExecutionAdapter(api_key="", api_secret="")

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
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(KrakenExecutionAdapter, "_private_request", fake_private_request)

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
        "recovery_state",
        "order_status",
        "cancel_order",
        "validate_order",
    ]
    assert summary["recovered_order_count"] == 1
    assert summary["recovered_order_ids"] == ["abc123"]
    assert calls[2] == ("QueryOrders", {"txid": "abc123", "trades": "false"})
    assert calls[3] == ("CancelOrder", {"txid": "DRYRUNVERIFY-CANCEL"})


def test_kraken_adapter_verify_dry_run_uses_expected_unknown_order_probes_when_no_open_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without open orders, the verification should still exercise status/cancel paths safely."""
    adapter = KrakenExecutionAdapter(api_key="kraken-key", api_secret="kraken-secret")

    def fake_private_request(self: KrakenExecutionAdapter, *, endpoint: str, params: dict[str, object]) -> dict[str, object]:
        if endpoint == "Balance":
            return {"error": [], "result": {"ZEUR": "1000.0"}}
        if endpoint == "OpenOrders":
            return {"error": [], "result": {"open": {}}}
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


def test_kraken_adapter_verify_dry_run_fails_without_credentials() -> None:
    """Verification should fail fast when Kraken credentials are missing."""
    adapter = KrakenExecutionAdapter(api_key="", api_secret="")

    summary = adapter.verify_dry_run()

    assert summary["status"] == "failed"
    assert summary["checks"][0]["name"] == "credentials"
    assert summary["checks"][0]["ok"] is False


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
