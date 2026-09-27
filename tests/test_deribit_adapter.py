"""The Deribit options adapter against a fake Deribit (nothing reaches the exchange; no test trades)."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

from src.execution.deribit_adapter import DeribitOptionsAdapter, settlement_currency
from src.execution.deribit_client import DeribitClient

PUT = "BTC_USDC-27NOV26-70000-P"


class FakeDeribit:
    """Deribit's JSON-RPC answers: limit orders fill up to `liquidity` at the book price, rest otherwise; positions and trades kept."""

    def __init__(self, *, book_price: float = 700.0, liquidity: float = 10.0, fee: float = 0.3) -> None:
        self.book_price, self.liquidity, self.fee = book_price, liquidity, fee
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: dict[str, list[dict[str, Any]]] = {}
        self.positions: dict[str, float] = {}
        self.calls: list[str] = []
        self.lose_next_order = False
        self.reject_next_order = False

    def __call__(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        path, _, query = url.partition("?")
        method = path.split("/api/v2/", 1)[1]
        params = dict(urllib.parse.parse_qsl(query))
        self.calls.append(method)
        if method == "public/auth":
            return {"result": {"access_token": "t", "expires_in": 900, "scope": "account:read trade:read_write wallet:none"}}
        if method in ("private/buy", "private/sell"):
            if self.reject_next_order:
                self.reject_next_order = False
                return {"error": {"code": 10009, "message": "not_enough_funds"}}
            order = self._place(method.rsplit("/", 1)[1], params)
            if self.lose_next_order:
                self.lose_next_order = False
                raise TimeoutError("read timed out")  # the order is live; our process never heard back
            return {"result": {"order": dict(order), "trades": list(self.trades.get(order["order_id"], []))}}
        if method == "private/get_order_state_by_label":
            return {"result": [dict(order) for order in self.orders.values() if order["label"] == params["label"]]}
        if method == "private/get_user_trades_by_order":
            return {"result": list(self.trades.get(params["order_id"], []))}
        if method == "private/cancel_by_label":
            for order in self.orders.values():
                if order["label"] == params["label"] and order["order_state"] == "open":
                    order["order_state"] = "cancelled"
            return {"result": 1}
        if method == "private/cancel_all":
            return {"result": sum(1 for order in self.orders.values() if order["order_state"] == "open")}
        if method == "private/get_positions":
            return {"result": [{"instrument_name": name, "size": size} for name, size in self.positions.items()]}
        if method == "private/get_account_summary":
            return {"result": {"currency": params["currency"], "equity": 100.0}}
        raise AssertionError(method)

    def _place(self, side: str, params: dict[str, str]) -> dict[str, Any]:
        order_id = f"USDC-{len(self.orders) + 1}"
        amount, price = float(params["amount"]), float(params.get("price", self.book_price))
        crosses = price >= self.book_price if side == "buy" else price <= self.book_price
        filled = min(amount, self.liquidity) if crosses and params.get("post_only") != "true" else 0.0
        order = {"order_id": order_id, "label": params["label"], "instrument_name": params["instrument_name"], "amount": amount, "filled_amount": filled,
                 "average_price": self.book_price if filled else 0.0, "order_state": "filled" if filled == amount else "open", "direction": side}
        self.orders[order_id] = order
        if filled:
            self.trades[order_id] = [{"amount": filled, "price": self.book_price, "fee": self.fee}]
            self.positions[params["instrument_name"]] = self.positions.get(params["instrument_name"], 0.0) + (filled if side == "buy" else -filled)
        return order

    def fill_rest(self, order_id: str, price: float) -> None:
        order = self.orders[order_id]
        rest = order["amount"] - order["filled_amount"]
        self.trades.setdefault(order_id, []).append({"amount": rest, "price": price, "fee": self.fee})
        total = sum(trade["amount"] * trade["price"] for trade in self.trades[order_id])
        order.update({"filled_amount": order["amount"], "average_price": total / order["amount"], "order_state": "filled"})


def _adapter(fake: FakeDeribit, *, testnet: bool = True, **kwargs: Any) -> DeribitOptionsAdapter:
    client = DeribitClient(client_id="id", client_secret="secret", testnet=testnet, transport=fake)
    client.sleep = lambda _: None
    adapter = DeribitOptionsAdapter(client, client_id_prefix="cqm-book1", **kwargs)
    adapter.min_unknown_age_seconds = 0.0
    return adapter


def test_the_adapter_refuses_the_live_exchange_until_allowed() -> None:
    with pytest.raises(ValueError, match="testnet"):
        _adapter(FakeDeribit(), testnet=False)
    assert _adapter(FakeDeribit(), testnet=False, allow_live=True).testnet is False
    assert _adapter(FakeDeribit()).client.base_url.startswith("https://test.deribit.com")
    assert settlement_currency(PUT) == "USDC" and settlement_currency("BTC-27NOV26-70000-P") == "BTC"


def test_a_marketable_limit_order_fills_and_settles_with_price_and_fees() -> None:
    fake = FakeDeribit()
    adapter = _adapter(fake)
    report = adapter.submit_order(order_id="o1", instrument=PUT, side="buy", amount=0.02, price=720.0)
    assert report.status == "FILLED" and adapter.orders["o1"].label == "cqm-book1-o1"
    [item] = adapter.settle_orders()
    assert item == {"order_id": "o1", "instrument": PUT, "side": "buy", "filled_amount": pytest.approx(0.02), "fill_price": pytest.approx(700.0),
                    "fee": pytest.approx(0.3), "status": "FILLED"}
    assert adapter.settle_orders() == [] and adapter.positions() == {PUT: pytest.approx(0.02)}


def test_a_resting_order_fills_later_in_parts_and_each_part_is_reported_once() -> None:
    fake = FakeDeribit(liquidity=0.01)
    adapter = _adapter(fake)
    assert adapter.submit_order(order_id="o1", instrument=PUT, side="buy", amount=0.03, price=700.0).status == "SUBMITTED"
    [first] = adapter.settle_orders()
    assert first["status"] == "PARTIALLY_FILLED" and first["filled_amount"] == pytest.approx(0.01)
    fake.fill_rest(adapter.orders["o1"].exchange_order_id, 710.0)
    [second] = adapter.settle_orders()
    assert second["status"] == "FILLED" and second["filled_amount"] == pytest.approx(0.02) and second["fill_price"] == pytest.approx(710.0)
    assert second["fee"] == pytest.approx(0.3)


def test_orders_are_never_resent_and_a_lost_response_is_found_by_label() -> None:
    fake = FakeDeribit()
    adapter = _adapter(fake)
    fake.lose_next_order = True
    report = adapter.submit_order(order_id="o1", instrument=PUT, side="sell", amount=0.01, price=690.0)
    assert report.status == "SUBMITTED" and "outcome unknown" in report.message
    assert fake.calls.count("private/sell") == 1  # not retried
    [item] = adapter.settle_orders()
    assert item["status"] == "FILLED" and item["filled_amount"] == pytest.approx(0.01) and adapter.positions() == {PUT: pytest.approx(-0.01)}


def test_rejections_unknown_orders_and_cancels() -> None:
    fake = FakeDeribit(book_price=700.0)
    adapter = _adapter(fake)
    fake.reject_next_order = True
    assert adapter.submit_order(order_id="o1", instrument=PUT, side="buy", amount=0.01, price=700.0).status == "REJECTED"
    assert adapter.submit_order(order_id="o2", instrument=PUT, side="buy", amount=0.01).status == "REJECTED"  # a limit order needs a price

    resting = adapter.submit_order(order_id="o3", instrument=PUT, side="buy", amount=0.01, price=500.0, post_only=True)
    assert resting.status == "SUBMITTED" and adapter.cancel_order("o3").status == "CANCELED"
    assert fake.orders[adapter.orders["o3"].exchange_order_id]["order_state"] == "cancelled"

    fake.orders.clear()  # an order Deribit never saw
    adapter.submit_order(order_id="o4", instrument=PUT, side="buy", amount=0.01, price=500.0)
    fake.orders.clear()
    results = [adapter.settle_orders() for _ in range(3)]
    assert results[:2] == [[], []] and results[2] == [{"order_id": "o4", "status": "CANCELED"}]
