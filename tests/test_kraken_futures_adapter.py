"""Tests for the real Kraken Futures adapter, driven by a fake transport that replays documented response shapes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone

import pytest

from src.execution import PaperTradingEngine
from src.execution.kraken_futures_adapter import KrakenFuturesExecutionAdapter, sign_request
from src.execution.perps import assumed_perp_contract, perp_contract_from_instrument
from src.risk.controls import RiskControlConfig, RiskManager
from src.risk.kill_switch import KillSwitchController
from src.storage.bar_aggregator import OHLCVBar

SECRET = base64.b64encode(b"not-a-real-secret-0123456789abcdef").decode()
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
INSTRUMENT = {
    "symbol": "PF_XBTUSD",
    "tickSize": 1,
    "contractValueTradePrecision": 4,
    "feeScheduleUid": "fees",
    "base": "BTC",
    "quote": "USD",
    "retailMarginLevels": [{"numNonContractUnits": 0.0, "initialMargin": 0.01, "maintenanceMargin": 0.005}],
}
CONTRACT = perp_contract_from_instrument(INSTRUMENT, {"tiers": [{"makerFee": 0.02, "takerFee": 0.05, "usdVolume": 0.0}]}, symbol="BTC/USD")


class FakeKraken:
    """Records requests and answers like Kraken Futures would, keeping a simple position and fill book."""

    def __init__(self, *, equity: float = 1000.0) -> None:
        self.requests: list[dict] = []
        self.equity = equity
        self.position = 0.0
        self.entry = 0.0
        self.fills: list[dict] = []
        self.open_orders: list[dict] = []
        self.next_send_status = "placed"
        self.fail_next_send = False
        self.fill_price = 50000.0

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict:
        path, _, query = url.partition("?")
        endpoint = path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(body.decode() if body else query))
        self.requests.append({"method": method, "endpoint": endpoint, "params": params, "headers": headers, "raw": body.decode() if body else query})
        if endpoint == "sendorder":
            if self.fail_next_send:
                self.fail_next_send = False
                self._fill(params)  # the order reached Kraken, but the response was lost
                raise TimeoutError("read timed out")
            if self.next_send_status in {"placed", "filled"}:
                self._fill(params)
            return {"result": "success", "sendStatus": {"status": self.next_send_status, "order_id": f"uuid-{len(self.requests)}"}}
        if endpoint == "fills":
            return {"result": "success", "fills": list(self.fills)}
        if endpoint == "openorders":
            return {"result": "success", "openOrders": list(self.open_orders)}
        if endpoint == "openpositions":
            if self.position == 0.0:
                return {"result": "success", "openPositions": []}
            return {"result": "success", "openPositions": [{"symbol": "PF_XBTUSD", "side": "long" if self.position > 0 else "short", "size": abs(self.position), "price": self.entry, "unrealizedPnl": 0.0, "unrealizedFunding": 0.0}]}
        if endpoint == "accounts":
            return {"result": "success", "accounts": {"flex": {"type": "multiCollateralMarginAccount", "marginEquity": self.equity, "availableMargin": self.equity, "totalUnrealized": 0.0}}}
        if endpoint == "cancelallorders":
            return {"result": "success", "cancelStatus": {"status": "cancelled", "cancelledOrders": []}}
        if endpoint == "cancelorder":
            return {"result": "success", "cancelStatus": {"status": "cancelled"}}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def _fill(self, params: dict) -> None:
        size = float(params["size"]) * (1.0 if params["side"] == "buy" else -1.0)
        if self.position == 0.0 or (self.position > 0) == (size > 0):
            self.entry = self.fill_price
        self.position = round(self.position + size, 8)
        self.fills.append({"fill_id": f"f{len(self.fills)}", "cliOrdId": params["cliOrdId"], "order_id": "x", "side": params["side"], "size": float(params["size"]), "price": self.fill_price, "fillTime": "2026-01-01T00:00:00Z", "fillType": "taker"})

    def sent_orders(self) -> list[dict]:
        return [request["params"] for request in self.requests if request["endpoint"] == "sendorder"]


def _adapter(fake: FakeKraken, leverage: float = 2.0) -> KrakenFuturesExecutionAdapter:
    adapter = KrakenFuturesExecutionAdapter(contract=CONTRACT, api_key="key", api_secret=SECRET, max_leverage=leverage, transport=fake, min_sync_seconds=0.0)
    adapter.sync_account(force=True)
    return adapter


def test_signature_matches_krakens_documented_algorithm() -> None:
    """sha256(postData + nonce + path) then HMAC-SHA512 keyed with the decoded secret, base64 encoded."""
    digest = hashlib.sha256(b"symbol=PF_XBTUSD&size=0.001" + b"1700000000000" + b"/api/v3/sendorder").digest()
    expected = base64.b64encode(hmac.new(base64.b64decode(SECRET), digest, hashlib.sha512).digest()).decode()
    assert sign_request(post_data="symbol=PF_XBTUSD&size=0.001", nonce="1700000000000", endpoint_path="/api/v3/sendorder", api_secret=SECRET) == expected


def test_requests_are_signed_over_the_exact_encoded_body_sent() -> None:
    """Kraken now hashes the URL-encoded parameters as they appear in the request; nonces must increase."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    adapter.submit_order(order_id="order-1", side="buy", size=0.002, price=50000.0, timestamp=T0, symbol="BTC/USD")
    send = next(request for request in fake.requests if request["endpoint"] == "sendorder")
    headers = send["headers"]
    assert headers["APIKey"] == "key"
    assert headers["Authent"] == sign_request(post_data=send["raw"], nonce=headers["Nonce"], endpoint_path="/api/v3/sendorder", api_secret=SECRET)
    nonces = [int(request["headers"]["Nonce"]) for request in fake.requests]
    assert nonces == sorted(nonces) and len(set(nonces)) == len(nonces)


def test_refuses_unverified_contracts_and_missing_credentials() -> None:
    """Real trading needs the venue's own spec and real keys."""
    with pytest.raises(ValueError, match="unverified"):
        KrakenFuturesExecutionAdapter(contract=assumed_perp_contract("BTC/USD"), api_key="k", api_secret=SECRET)
    with pytest.raises(ValueError, match="required"):
        KrakenFuturesExecutionAdapter(contract=CONTRACT, api_key="", api_secret=SECRET)


def test_order_is_sent_as_ioc_market_with_unique_client_id_and_then_reconciled_from_fills() -> None:
    """Submit returns SUBMITTED; recovery reads /fills, marks it FILLED, and adopts Kraken's position and margin."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    report = adapter.submit_order(order_id="order-1", side="buy", size=0.00237, price=50000.0, timestamp=T0, symbol="BTC/USD")

    assert report.status == "SUBMITTED"
    sent = fake.sent_orders()[0]
    assert sent == {"orderType": "mkt", "symbol": "PF_XBTUSD", "side": "buy", "size": "0.0023", "cliOrdId": adapter.client_order_id("order-1")}

    adapter.recover_execution_state()
    order = adapter.list_orders()[0]
    assert order.status == "FILLED"
    assert order.filled_size == pytest.approx(0.0023) and order.fill_price == 50000.0
    assert order.fee == pytest.approx(0.0023 * 50000.0 * 0.0005)
    assert adapter.position_size() == pytest.approx(0.0023)
    assert adapter._position_entry_price["BTC"] == 50000.0
    assert adapter.equity(50000.0) == pytest.approx(1000.0)  # Kraken's marginEquity is the source of truth


def test_only_pure_reductions_are_sent_reduce_only() -> None:
    """A close or partial close is reduce-only; an opening order and a flip through zero are not."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    adapter.submit_order(order_id="open", side="buy", size=0.004, price=50000.0, timestamp=T0, symbol="BTC/USD")
    adapter.recover_execution_state()
    adapter.submit_order(order_id="partial", side="sell", size=0.001, price=50000.0, timestamp=T0, symbol="BTC/USD")
    adapter.submit_order(order_id="close", side="sell", size=0.004, price=50000.0, timestamp=T0, symbol="BTC/USD")
    adapter.submit_order(order_id="flip", side="sell", size=0.006, price=50000.0, timestamp=T0, symbol="BTC/USD")

    opening, partial, close, flip = fake.sent_orders()
    assert "reduceOnly" not in opening
    assert partial["reduceOnly"] == "true" and close["reduceOnly"] == "true"
    assert "reduceOnly" not in flip


def test_kraken_rejections_and_local_checks_never_leave_pending_orders() -> None:
    """A rejected sendStatus is REJECTED; oversize and sub-minimum orders never reach Kraken."""
    fake = FakeKraken(equity=100.0)
    adapter = _adapter(fake, leverage=2.0)
    fake.next_send_status = "insufficientAvailableFunds"
    rejected = adapter.submit_order(order_id="o1", side="buy", size=0.001, price=50000.0, timestamp=T0, symbol="BTC/USD")
    assert rejected.status == "REJECTED" and "insufficientAvailableFunds" in (rejected.message or "")

    sends_before = len(fake.sent_orders())
    assert adapter.submit_order(order_id="o2", side="buy", size=0.01, price=50000.0, timestamp=T0, symbol="BTC/USD").status == "REJECTED"  # 500 notional > 2x of 100
    assert adapter.submit_order(order_id="o3", side="buy", size=0.00001, price=50000.0, timestamp=T0, symbol="BTC/USD").status == "REJECTED"
    assert len(fake.sent_orders()) == sends_before
    assert not [order for order in adapter.list_orders() if order.status == "SUBMITTED"]


def test_lost_response_is_recovered_by_client_order_id() -> None:
    """If the HTTP call dies after Kraken accepted the order, the fill is still found and booked once."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    fake.fail_next_send = True
    report = adapter.submit_order(order_id="o1", side="buy", size=0.002, price=50000.0, timestamp=T0, symbol="BTC/USD")
    assert report.status == "SUBMITTED" and "outcome unknown" in (report.message or "")

    adapter.recover_execution_state()
    assert adapter.list_orders()[0].status == "FILLED"
    assert adapter.position_size() == pytest.approx(0.002)


def test_ioc_order_without_a_fill_is_marked_cancelled() -> None:
    """Kraken accepted the order but nothing traded and it is no longer open: it expired."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    fake.next_send_status = "cancelled"
    assert adapter.submit_order(order_id="o1", side="buy", size=0.002, price=50000.0, timestamp=T0, symbol="BTC/USD").status == "REJECTED"

    fake.next_send_status = "partiallyFilled"  # accepted, but our fake books no fill for this status
    adapter.submit_order(order_id="o2", side="buy", size=0.002, price=50000.0, timestamp=T0, symbol="BTC/USD")
    adapter.recover_execution_state()
    assert adapter.get_order_status(order_id="o2").status == "CANCELED"


def test_position_closed_on_the_exchange_is_reported_as_a_liquidation() -> None:
    """If Kraken shows the position gone without an order from us, the market update reports it."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    adapter.submit_order(order_id="o1", side="buy", size=0.002, price=50000.0, timestamp=T0, symbol="BTC/USD")
    adapter.recover_execution_state()
    fake.position = 0.0
    events = adapter.on_market_update(symbol="BTC/USD", mark_price=30000.0, timestamp=T0)
    assert [event["type"] for event in events] == ["liquidation"]
    assert adapter.position_size() == 0.0


def test_engine_cycle_places_and_reconciles_a_real_style_order() -> None:
    """End to end through PaperTradingEngine: submit on one cycle, pick up the fill and log it on the next."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    risk = RiskManager(RiskControlConfig(risk_per_trade_pct=0.10, max_notional_per_trade=10000.0, max_total_notional=25000.0, paper_mode=False))
    engine = PaperTradingEngine(initial_cash=1000.0, default_order_size=1.0, execution_adapter=adapter, exchange_name="kraken_futures", allow_short=True, risk_manager=risk)

    def bar(minute: int) -> OHLCVBar:
        return OHLCVBar(exchange="kraken", symbol="BTC/USD", interval_seconds=60, timestamp=T0.replace(minute=minute), open=50000.0, high=50000.0, low=50000.0, close=50000.0, volume=1.0)

    first = engine.run_exchange_cycle([bar(0)], [1.0])
    assert len(fake.sent_orders()) == 1 and first.trades == []
    second = engine.run_exchange_cycle([bar(0), bar(1)], [1.0, 1.0])
    assert [trade.side for trade in second.trades] == ["buy"]
    assert len(fake.sent_orders()) == 1  # no duplicate entry while the first order was settling
    assert adapter.position_size() > 0.0


def test_kill_switch_also_sweeps_open_orders_on_the_exchange(tmp_path) -> None:
    """The kill switch calls cancelallorders for the contract, not just the orders this process knows."""
    fake = FakeKraken()
    adapter = _adapter(fake)
    state = KillSwitchController(state_file=tmp_path / "ks.json").activate("test", execution_adapter=adapter)
    assert any(request["endpoint"] == "cancelallorders" and request["params"] == {"symbol": "PF_XBTUSD"} for request in fake.requests)
    assert state["exchange_cancel_all"]["status"] == "cancelled"


def test_live_futures_needs_credentials_and_caps_leverage(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Live kraken_futures refuses to start without futures keys or above 3x; tax logging stays off for perps."""
    import main
    from src.runtime.config import RuntimeConfig

    controller = KillSwitchController(state_file=tmp_path / "ks.json")
    config = RuntimeConfig(mode="live", exchange="kraken_futures")
    monkeypatch.setattr(main.settings, "kraken_futures_api_key", "")
    with pytest.raises(SystemExit, match="KRAKEN_FUTURES_API_KEY"):
        main._validate_live_runtime_request(runtime_config=config, use_mock_connector=False, enable_live_trading=True, live_confirmation=main.LIVE_TRADING_CONFIRMATION, kill_switch_controller=controller)
    with pytest.raises(SystemExit, match="leverage"):
        main._validate_live_runtime_request(runtime_config=config, use_mock_connector=False, enable_live_trading=True, live_confirmation=main.LIVE_TRADING_CONFIRMATION, kill_switch_controller=controller, perp_max_leverage=5.0)
    with pytest.raises(SystemExit, match="KRAKEN_FUTURES_API_KEY"):
        main._build_perp_adapter(mode="live", symbol="BTC/USD", max_leverage=2.0)
