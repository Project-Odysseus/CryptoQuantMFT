"""The paper exchange for prediction-market contracts (src/execution/binary_sandbox.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src.execution.binary_sandbox import SandboxBinaryAdapter, binary_fee

NOW = datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)
MARKET = "KXBTCD-26OCT0416-T85399.99"


class Book:
    """A hand-set order book: Yes bids and asks, best first."""

    def __init__(self) -> None:
        self.bids, self.asks = [(0.36, 100.0), (0.35, 500.0)], [(0.38, 30.0), (0.39, 200.0), (0.45, 1000.0)]

    def __call__(self, market: str) -> pd.DataFrame:
        if market != MARKET:
            raise TimeoutError("no such book")
        return pd.DataFrame([("bid", p, s) for p, s in self.bids] + [("ask", p, s) for p, s in self.asks], columns=["side", "price", "size"])


def _account(book: Book, **settings) -> SandboxBinaryAdapter:
    return SandboxBinaryAdapter(**{"venue": "kalshi", "books": book, "starting_cash": 100.0, **settings})


def _order(account, order_id, side, size, price, **settings):
    return account.submit_order(order_id=order_id, side=side, size=size, price=price, timestamp=NOW, symbol=MARKET, **settings)


def test_a_taker_order_walks_the_book_to_its_limit_and_cancels_the_rest() -> None:
    account = _account(Book())
    report = _order(account, "a", "buy", 100, 0.39)  # 30 at 0.38, then 70 at 0.39
    assert report.status == "FILLED" and report.filled_size == 100 and report.fill_price == pytest.approx((30 * 0.38 + 70 * 0.39) / 100)
    assert report.fee == pytest.approx(binary_fee(report.fill_price, 100, 0.07, round_up=True)) and report.fee == 1.67  # 0.07 x 0.387 x 0.613 x 100 = 1.6606, up to the cent
    assert account.cash == pytest.approx(100 - 38.7 - 1.67) and account.position_size(MARKET) == 100

    part = _order(account, "b", "buy", 500, 0.38)  # only 30 are offered at 0.38: a partial fill, nothing left resting
    assert part.status == "FILLED" and part.filled_size == 30 and account.resting_orders() == {}
    assert _order(account, "c", "buy", 10, 0.37).status == "REJECTED"  # nothing offered at 0.37
    assert _order(account, "a", "buy", 100, 0.39).message == "already filled under this order id" and account.position_size(MARKET) == 130  # an order id fills once
    sold = _order(account, "d", "sell", 130, 0.36)  # closing: 100 at 0.36 is all the bid there is at that price
    assert sold.filled_size == 100 and account.position_size(MARKET) == 30 and account.stake() == 0
    for bad in (dict(side="buy", size=0.4, price=0.5), dict(side="buy", size=5, price=1.0), dict(side="hold", size=5, price=0.5)):
        assert _order(account, "x", **bad).status == "REJECTED"
    assert account.submit_order(order_id="y", side="buy", size=5, price=0.5, timestamp=NOW, symbol="OTHER").message == "no order book for OTHER"


def test_selling_yes_that_is_not_held_buys_no_and_sets_its_stake_aside() -> None:
    account = _account(Book())
    report = _order(account, "no", "sell", 50, 0.36)  # 50 No contracts at 64 cents each
    assert report.status == "FILLED" and account.position_size(MARKET) == -50 and account.stake() == 50
    assert account.cash == pytest.approx(100 + 50 * 0.36 - report.fee) and account.available_cash() == pytest.approx(100 - 50 * 0.64 - report.fee)
    assert _order(account, "more", "sell", 200, 0.35).status == "REJECTED"  # 200 more No would need 130: more than is free
    assert account.resolve(MARKET, False) == 0 and account.cash == pytest.approx(100 + 18 - report.fee) and account.stake() == 0  # No won: the stake is released
    lost = _account(Book())
    _order(lost, "no", "sell", 50, 0.36)
    assert lost.resolve(MARKET, True) == -50 and lost.cash == pytest.approx(100 + 18 - 50 - binary_fee(0.36, 50, 0.07, round_up=True))  # Yes won: it pays 1 a contract


def test_a_post_only_order_rests_and_fills_only_when_the_other_side_comes_to_it(tmp_path) -> None:
    book = Book()
    account = _account(book, state_path=tmp_path / "kalshi.json")
    assert _order(account, "cross", "buy", 10, 0.38, post_only=True).status == "REJECTED"  # it would trade at once
    resting = _order(account, "m1", "buy", 100, 0.37, post_only=True)
    assert resting.status == "SUBMITTED" and account.position_size(MARKET) == 0 and account.available_cash() == pytest.approx(100 - 37)  # its cash is reserved
    assert _order(account, "m1", "buy", 100, 0.37, post_only=True).message.startswith("already resting") and account.settle_orders() == []
    account.on_market_update(timestamp=NOW)
    assert account.position_size(MARKET) == 0  # the best offer is still 0.38
    book.asks = [(0.37, 20.0), (0.39, 200.0)]  # an offer at our price: someone sells where we bid
    account.on_market_update(timestamp=NOW)
    [filled] = account.settle_orders()
    assert filled["status"] == "FILLED" and filled["liquidity"] == "maker" and filled["fill_price"] == 0.37 and filled["filled_size"] == 100 and filled["fee"] == 0.0
    assert account.position_size(MARKET) == 100 and account.settle_orders() == []

    _order(account, "m2", "sell", 100, 0.50, post_only=True)  # an offer to sell what is held, above the market
    restored = _account(book, state_path=tmp_path / "kalshi.json")  # a restart keeps the position, the cash and the resting order
    assert restored.restored_from_state and restored.position_size(MARKET) == 100 and list(restored.resting_orders()) == ["m2"] and restored.cash == account.cash
    for order_id in ("m2", "never-sent"):
        restored.track_order(order_id=order_id)
    assert restored.settle_orders() == [{"order_id": "never-sent", "status": "CANCELED", "never_received": True}]  # m2 still rests: no outcome yet
    assert restored.cancel_order(order_id="m2").status == "CANCELED" and restored.settle_orders() == [{"order_id": "m2", "status": "CANCELED"}]
    again = _account(book, state_path=tmp_path / "kalshi.json")
    again.track_order(order_id="m2")
    assert again.settle_orders() == [{"order_id": "m2", "status": "CANCELED"}]  # remembered as cancelled, not "never received"
    with pytest.raises(ValueError, match="not a polymarket paper account"):
        SandboxBinaryAdapter(venue="polymarket", books=book, state_path=tmp_path / "kalshi.json")


def test_fees_follow_the_venues_formula() -> None:
    assert binary_fee(0.5, 100, 0.07) == pytest.approx(1.75) and binary_fee(0.95, 100, 0.07) == pytest.approx(0.3325)  # highest at even odds
    assert binary_fee(0.95, 1, 0.07, round_up=True) == 0.01 and binary_fee(0.5, 0, 0.07, round_up=True) == 0.0
    polymarket = SandboxBinaryAdapter(venue="polymarket", books=Book(), starting_cash=100.0)
    assert polymarket.submit_order(order_id="p", side="buy", size=10, price=0.38, timestamp=NOW, symbol=MARKET).fee == pytest.approx(0.07 * 0.38 * 0.62 * 10)  # not rounded there
