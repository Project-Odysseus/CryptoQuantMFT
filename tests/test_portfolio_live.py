"""Live portfolio trading against a fake Kraken Futures account: the multi-contract adapter and the engine's live path.

Nothing here reaches Kraken: `FakeKraken` answers the documented request and response shapes, keeps positions and
fills, and can lose a response after the order reached the exchange or change a position behind our back.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.data.kraken_futures import FundingRate
from src.execution.kraken_futures_cross import LOST_ORDER_SETTLE_ATTEMPTS, KrakenFuturesCrossMarginAdapter
from src.execution.perps import perp_contract_from_instrument
from src.portfolio.book import PortfolioBook
from src.portfolio.engine import PortfolioEngine
from src.storage.trade_logger import TradeLogger
from test_portfolio_engine import BTC, ETH, FIRST, _config, _now, bars_until

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
FEES = {"tiers": [{"makerFee": 0.02, "takerFee": 0.05, "usdVolume": 0.0}]}


def _contract(venue_symbol: str, base: str, symbol: str, precision: int):
    instrument = {"symbol": venue_symbol, "tickSize": 0.1, "contractValueTradePrecision": precision, "feeScheduleUid": "fees", "base": base, "quote": "USD",
                  "retailMarginLevels": [{"numNonContractUnits": 0.0, "initialMargin": 0.02, "maintenanceMargin": 0.01}]}
    return perp_contract_from_instrument(instrument, FEES, symbol=symbol)


CONTRACTS = [_contract("PF_XBTUSD", "BTC", "BTC/USD", 4), _contract("PF_ETHUSD", "ETH", "ETH/USD", 3)]


class FakeKraken:
    """A multi-collateral account: IOC market orders fill at `prices`, positions and fills are kept per contract."""

    def __init__(self, *, equity: float = 10_000.0) -> None:
        self.collateral = equity
        self.positions: dict[str, list[float]] = {}  # venue symbol -> [signed size, entry]
        self.fills: list[dict[str, Any]] = []
        self.prices = {"PF_XBTUSD": 50_000.0, "PF_ETHUSD": 2_500.0, "PF_SOLUSD": 150.0}
        self.requests: list[dict[str, Any]] = []
        self.lose_next_response = False
        self.delay_fills = False  # when set, new fills stay out of /fills until release_fills()
        self.hidden_fills: list[dict[str, Any]] = []
        self.realized = 0.0
        self.resting: list[dict[str, Any]] = []  # stop orders waiting for their trigger
        self.refuse_stops = False
        self.fee_rate = 0.0005  # what Kraken really charges (the adapter estimates the contract's taker rate)
        self.log: list[dict[str, Any]] = []  # the account log: two entries per fill, as Kraken writes them
        self.unrealized_funding: dict[str, float] = {}  # accrues per position; realized into the log at the next fill
        self.now = T0

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict[str, Any]:
        path, _, query = url.partition("?")
        endpoint = path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(body.decode() if body else query))
        self.requests.append({"endpoint": endpoint, "params": params})
        if endpoint == "account-log":
            entries = sorted(self.log, key=lambda e: e["id"], reverse=params.get("sort", "desc") == "desc")
            if "from" in params:
                entries = [e for e in entries if e["id"] >= int(params["from"])]
            return {"accountUid": "fake", "logs": entries[: int(params.get("count", 500))]}
        if endpoint == "sendorder" and params.get("orderType") == "stp":
            if self.refuse_stops:
                return {"result": "success", "sendStatus": {"status": "invalidPrice"}}
            order_id = f"stop-uuid-{len(self.requests)}"
            self.resting.append({"order_id": order_id, "cliOrdId": params["cliOrdId"], "symbol": params["symbol"], "side": params["side"],
                                 "size": float(params["size"]), "stopPrice": float(params["stopPrice"]), "triggerSignal": params["triggerSignal"],
                                 "reduceOnly": params["reduceOnly"] == "true"})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": order_id}}
        if endpoint == "cancelorder":
            before = len(self.resting)
            self.resting = [o for o in self.resting if o["order_id"] != params.get("order_id") and o["cliOrdId"] != params.get("cliOrdId")]
            return {"result": "success", "cancelStatus": {"status": "cancelled" if len(self.resting) < before else "notFound"}}
        if endpoint == "sendorder":
            size, entry = self.positions.get(params["symbol"], [0.0, 0.0])
            signed = float(params["size"]) * (1 if params["side"] == "buy" else -1)
            if params.get("reduceOnly") == "true" and (abs(size + signed) > abs(size) + 1e-12 or (size + signed) * size < 0):
                return {"result": "success", "sendStatus": {"status": "wouldNotReducePosition"}}
            order_id = self._fill(params["symbol"], signed, params["cliOrdId"])
            if self.lose_next_response:
                self.lose_next_response = False
                raise TimeoutError("read timed out")  # the order reached Kraken; our process never heard back
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": order_id}}
        if endpoint == "fills":
            return {"result": "success", "fills": list(self.fills)}
        if endpoint == "openorders":
            return {"result": "success", "openOrders": [{"order_id": o["order_id"], "cliOrdId": o["cliOrdId"], "symbol": o["symbol"], "side": o["side"],
                                                         "orderType": "stp", "stopPrice": o["stopPrice"], "unfilledSize": o["size"], "reduceOnly": o["reduceOnly"],
                                                         "triggerSignal": o["triggerSignal"], "status": "untouched"} for o in self.resting]}
        if endpoint == "openpositions":
            return {"result": "success", "openPositions": [{"symbol": symbol, "side": "long" if size > 0 else "short", "size": abs(size), "price": entry,
                                                            "unrealizedFunding": self.unrealized_funding.get(symbol, 0.0)} for symbol, (size, entry) in self.positions.items() if size]}
        if endpoint == "accounts":
            unrealized = sum(size * (self.prices[symbol] - entry) for symbol, (size, entry) in self.positions.items()) + sum(self.unrealized_funding.values())
            equity = self.collateral + unrealized
            used = sum(abs(size) * self.prices[symbol] / 2.0 for symbol, (size, _entry) in self.positions.items())
            return {"result": "success", "accounts": {"flex": {"marginEquity": equity, "availableMargin": equity - used, "totalUnrealized": unrealized}}}
        if endpoint == "cancelallorders":
            self.resting = []
            return {"result": "success", "cancelStatus": {"status": "cancelled"}}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def _fill(self, symbol: str, signed: float, cli_ord_id: str) -> str:
        price = self.prices[symbol]
        size, entry = self.positions.get(symbol, [0.0, 0.0])
        realized_pnl = 0.0
        if size and (size > 0) != (signed > 0):
            closed = min(abs(size), abs(signed))
            realized_pnl = closed * (price - entry) * (1 if size > 0 else -1)
            self.collateral += realized_pnl
        realized_funding = self.unrealized_funding.pop(symbol, 0.0)  # Kraken realizes accrued funding at a fill
        self.collateral += realized_funding
        new = size + signed
        if abs(new) < 1e-12:
            self.positions.pop(symbol, None)
        elif size == 0 or (size > 0) != (new > 0):
            self.positions[symbol] = [new, price]
        elif abs(new) > abs(size):
            self.positions[symbol] = [new, (entry * abs(size) + price * abs(signed)) / abs(new)]
        else:
            self.positions[symbol] = [new, entry]
        fee = abs(signed) * price * self.fee_rate
        self.collateral -= fee
        number = len(self.fills) + len(self.hidden_fills) + 1
        fill = {"cliOrdId": cli_ord_id, "order_id": f"uuid-{number}", "fill_id": f"fill-{number}", "symbol": symbol, "size": abs(signed), "price": price,
                "side": "buy" if signed > 0 else "sell", "fillType": "taker", "realized_funding": realized_funding}
        stamp = self.now.isoformat().replace("+00:00", "Z")
        base = {"date": stamp, "info": "futures trade", "contract": symbol.lower(), "execution": fill["fill_id"], "trade_price": price, "margin_account": "flex"}
        self.log.append({**base, "id": len(self.log) + 1, "asset": symbol.lower(), "old_balance": size, "new_balance": size + signed, "fee": None, "realized_funding": None})
        rounded_funding = round(realized_funding, 4) if size else 0.0  # the log keeps USD to 4 decimals, as Kraken's does
        self.log.append({**base, "id": len(self.log) + 1, "asset": "usd", "old_balance": 0.0, "new_balance": 0.0, "fee": fee, "realized_pnl": realized_pnl,
                         "realized_funding": rounded_funding or None})
        (self.hidden_fills if self.delay_fills else self.fills).append(fill)
        return fill["order_id"]  # unique, as Kraken's order ids are

    def accrue_funding(self, hourly_rate: float, hours: float = 4.0) -> None:
        """Funding accrues on every position (longs pay a positive rate) into unrealized funding, as on Kraken's flex account."""
        for symbol, (size, _entry) in self.positions.items():
            self.unrealized_funding[symbol] = self.unrealized_funding.get(symbol, 0.0) - size * self.prices[symbol] * hourly_rate * hours

    def trigger_stops(self) -> list[dict[str, Any]]:
        """Fire every resting stop whose mark has crossed it: a reduce-only market fill at the current price."""
        fired = []
        for order in list(self.resting):
            mark = self.prices[order["symbol"]]
            if (order["side"] == "sell" and mark <= order["stopPrice"]) or (order["side"] == "buy" and mark >= order["stopPrice"]):
                self.resting.remove(order)
                size, _entry = self.positions.get(order["symbol"], [0.0, 0.0])
                closable = min(order["size"], abs(size)) if size and (size > 0) == (order["side"] == "sell") else 0.0
                if closable:
                    self._fill(order["symbol"], closable if order["side"] == "buy" else -closable, order["cliOrdId"])
                    fired.append(order)
        return fired

    def release_fills(self) -> None:
        self.fills += self.hidden_fills
        self.hidden_fills, self.delay_fills = [], False

    def sent_orders(self) -> list[dict[str, Any]]:
        return [request["params"] for request in self.requests if request["endpoint"] == "sendorder"]


def _adapter(fake: FakeKraken) -> KrakenFuturesCrossMarginAdapter:
    adapter = KrakenFuturesCrossMarginAdapter(contracts=CONTRACTS, api_key="key", api_secret="c2VjcmV0", max_leverage=2.0, transport=fake)
    adapter.min_unfilled_age_seconds = 0.0  # simulated cycles take no wall-clock time
    return adapter


def test_orders_are_ioc_market_with_stable_client_ids_and_settle_from_fills() -> None:
    fake = FakeKraken()
    adapter = _adapter(fake)
    adapter.client_id_prefix = "cqm-book1"
    report = adapter.submit_order(order_id="pf-7-0", side="buy", size=0.10009, price=50_000.0, timestamp=T0, symbol="BTC/USD")
    assert report.status == "SUBMITTED"
    sent = fake.sent_orders()[-1]
    assert sent == {"orderType": "mkt", "symbol": "PF_XBTUSD", "side": "buy", "size": "0.1000", "cliOrdId": "cqm-book1-pf-7-0"}
    [settled] = adapter.settle_orders()
    assert settled["status"] == "FILLED" and settled["filled_size"] == pytest.approx(0.1) and settled["fill_price"] == 50_000.0
    assert settled["fee"] == pytest.approx(0.1 * 50_000.0 * 0.0005) and settled["fee_estimated"]
    assert adapter.position_size("BTC/USD") == pytest.approx(0.1) and adapter.settle_orders() == []

    adapter.submit_order(order_id="pf-7-1", side="sell", size=0.1, price=50_000.0, timestamp=T0, symbol="BTC/USD", reduce_only=True)
    assert fake.sent_orders()[-1]["reduceOnly"] == "true"


def test_local_checks_reject_before_anything_is_sent() -> None:
    fake = FakeKraken(equity=1_000.0)
    adapter = _adapter(fake)
    adapter.sync_account()
    assert "trades" in adapter.submit_order(order_id="a", side="buy", size=1, price=100.0, timestamp=T0, symbol="SOL/USD").message
    assert "below the contract minimum" in adapter.submit_order(order_id="b", side="buy", size=0.00001, price=50_000.0, timestamp=T0, symbol="BTC/USD").message
    assert "insufficient margin" in adapter.submit_order(order_id="c", side="buy", size=0.1, price=50_000.0, timestamp=T0, symbol="BTC/USD").message
    assert "reduce_only" in adapter.submit_order(order_id="d", side="buy", size=0.01, price=50_000.0, timestamp=T0, symbol="BTC/USD", reduce_only=True).message
    assert fake.sent_orders() == []
    with pytest.raises(ValueError, match="unverified"):
        from src.execution.perps import assumed_perp_contract

        KrakenFuturesCrossMarginAdapter(contracts=[assumed_perp_contract("BTC/USD")], api_key="k", api_secret="c2VjcmV0")


def test_an_ioc_fill_that_reaches_fills_late_is_not_written_off() -> None:
    fake = FakeKraken()
    adapter = _adapter(fake)
    fake.delay_fills = True
    adapter.submit_order(order_id="late", side="buy", size=0.1, price=50_000.0, timestamp=T0, symbol="BTC/USD")
    assert adapter.settle_orders() == [] and adapter.settle_orders() == []  # not open, no fill yet: keep waiting
    fake.release_fills()
    [settled] = adapter.settle_orders()
    assert settled["status"] == "FILLED" and settled["filled_size"] == pytest.approx(0.1)


def test_a_lost_response_is_settled_by_client_id_and_a_lost_order_without_fill_ends() -> None:
    fake = FakeKraken()
    adapter = _adapter(fake)
    fake.lose_next_response = True
    assert adapter.submit_order(order_id="x", side="sell", size=0.5, price=2_500.0, timestamp=T0, symbol="ETH/USD").status == "SUBMITTED"
    [settled] = adapter.settle_orders()
    assert settled["status"] == "FILLED" and settled["filled_size"] == pytest.approx(0.5)

    adapter.track_order(order_id="never-arrived", symbol="ETH/USD", side="buy", size=0.5, price=2_500.0, timestamp=T0)
    results = [adapter.settle_orders() for _ in range(LOST_ORDER_SETTLE_ATTEMPTS)]
    assert results[:-1] == [[]] * (LOST_ORDER_SETTLE_ATTEMPTS - 1) and results[-1][0]["status"] == "CANCELED"


def test_sync_adopts_krakens_positions_and_reports_changes_we_did_not_make() -> None:
    fake = FakeKraken()
    adapter = _adapter(fake)
    fake.positions["PF_SOLUSD"] = [3.0, 150.0]  # someone trades SOL in the same account
    assert adapter.sync_account()["foreign_positions"] == {"PF_SOLUSD": 3.0}
    adapter.submit_order(order_id="1", side="buy", size=0.1, price=50_000.0, timestamp=T0, symbol="BTC/USD")
    adapter.settle_orders()
    assert adapter.on_market_update(prices={"BTC/USD": 50_000.0}, timestamp=T0) == []
    fake.positions.pop("PF_XBTUSD")  # liquidated, or closed by hand
    [event] = adapter.on_market_update(prices={"BTC/USD": 50_000.0}, timestamp=T0)
    assert event["type"] == "liquidation" and event["symbol"] == "BTC/USD"


def _live_engine(fake: FakeKraken, tmp_path, *, logger=None, notifier=None, funding=None, config=None) -> PortfolioEngine:
    config = config or _config()
    book = PortfolioBook.from_config(config)
    engine = PortfolioEngine(config, adapters={"kraken_futures": _adapter(fake)}, book=book, trade_logger=logger, notifier=notifier, mode="live",
                             state_path=tmp_path / "engine.json", record_tax=logger is not None, funding_source=funding or (lambda venue_symbol: []))
    if not engine.restored:
        engine.adopt_exchange_state(now=_now(FIRST), reason="first live start")
    return engine


def _cycle(engine: PortfolioEngine, fake: FakeKraken, index: int):
    bars = bars_until(index)
    fake.prices.update({"PF_XBTUSD": bars[(BTC, "4h")][-1].close, "PF_ETHUSD": bars[(ETH, "4h")][-1].close})
    return engine.run_cycle(bars, now=_now(index))


def test_a_live_portfolio_trades_through_kraken_and_stays_reconciled(tmp_path) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path)
    assert float(engine.book.equity()) == pytest.approx(10_000.0)
    fills = []
    for index in range(FIRST, 330):
        report = _cycle(engine, fake, index)
        assert report.rejected == [] and report.mismatches == {} and engine.pending_orders == {}
        fills += report.fills
    assert len(fills) > 5 and {fill["instrument"] for fill in fills} == {BTC, ETH}
    held = {symbol: size for symbol, (size, _entry) in fake.positions.items()}
    assert {"PF_XBTUSD": float(engine.book.units().get(BTC, 0)), "PF_ETHUSD": float(engine.book.units().get(ETH, 0))} == pytest.approx(
        {"PF_XBTUSD": held.get("PF_XBTUSD", 0.0), "PF_ETHUSD": held.get("PF_ETHUSD", 0.0)})
    kraken_equity = fake("GET", "https://x/api/v3/accounts", {}, None)["accounts"]["flex"]["marginEquity"]
    assert float(engine.book.equity()) == pytest.approx(kraken_equity, rel=1e-6)  # fees estimated at Kraken's rate: the books agree
    assert all(order["reduceOnly"] == "true" for order in fake.sent_orders() if order.get("reduceOnly"))


def test_live_funding_is_booked_from_krakens_hourly_rates_into_book_and_tax(tmp_path) -> None:
    class Rates:
        def get_rate(self, pair: str = "EUR/NOK", at: Any = None) -> float:
            return 10.0

    def funding(venue_symbol: str) -> list[FundingRate]:
        start = _now(FIRST) - timedelta(days=2)
        return [FundingRate(timestamp=start + timedelta(hours=hour), hourly_rate=0.00001) for hour in range(24 * 40)]

    fake = FakeKraken()
    logger = TradeLogger(tmp_path / "trades.db")
    logger.fx_rate_collector = Rates()
    engine = _live_engine(fake, tmp_path, logger=logger, funding=funding)
    for index in range(FIRST, 260):
        fake.now = _now(index)
        fake.accrue_funding(0.00001)  # Kraken charges the same rate, at its own marks: the account log trues up the difference
        _cycle(engine, fake, index)
    engine._flush_tax(_now(260))  # the last cycle's records are written at the start of the next one
    booked = sum(float(position.funding) for position in engine.book.positions.values())
    taxed = [event for event in logger.list_tax_events(transaction_types=["FUNDING_FEE"])]
    estimates = [event for event in taxed if event["metadata"].get("estimate")]
    true_ups = [event for event in taxed if event["metadata"].get("kind") == "funding_true_up"]
    assert booked != 0.0 and len(estimates) > 20 and true_ups
    assert sum(event["metadata"]["amount"] for event in taxed) == pytest.approx(-booked)  # the ledger and the book agree
    assert abs(sum(event["metadata"]["amount"] for event in true_ups)) < 0.2 * abs(booked)  # estimates were close; the log corrects the rest


def test_a_restart_with_an_order_in_flight_settles_it_without_resending(tmp_path) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path)
    index = FIRST
    fake.delay_fills = True  # Kraken's /fills lags: the engine can't see the fill before it "crashes"
    while not fake.sent_orders():  # run until the first order goes out, losing its response
        fake.lose_next_response = True
        report = _cycle(engine, fake, index)
        index += 1
    fake.lose_next_response = False
    in_flight = list(engine.pending_orders)
    assert in_flight and report.fills == []  # sent, filled on Kraken, but unknown to the engine
    prefix = engine.adapters["kraken_futures"].client_id_prefix
    del engine  # the process dies with the order's fate unknown to it
    fake.release_fills()
    restarted = _live_engine(fake, tmp_path)
    assert restarted.restored and restarted.adapters["kraken_futures"].client_id_prefix == prefix
    report = _cycle(restarted, fake, index)
    assert restarted.pending_orders == {} and report.mismatches == {}
    assert {fill["order_id"] for fill in report.fills} >= set(in_flight)  # booked from Kraken's fills after the restart
    sent_ids = [order["cliOrdId"] for order in fake.sent_orders()]
    assert all(sent_ids.count(f"{prefix}-{order_id}") == 1 for order_id in in_flight)  # and never sent again
    for instrument, venue_symbol in ((BTC, "PF_XBTUSD"), (ETH, "PF_ETHUSD")):
        assert float(restarted.book.units().get(instrument, 0)) == pytest.approx(fake.positions.get(venue_symbol, [0.0])[0])


def test_an_external_change_blocks_new_risk_until_the_exchange_state_is_adopted(tmp_path) -> None:
    fake = FakeKraken()
    alerts = []

    class Notifier:
        def send_trade_alert(self, alert: Any) -> bool:
            return True

        def send_alert(self, **kwargs: Any) -> bool:
            alerts.append(kwargs)
            return True

    engine = _live_engine(fake, tmp_path, notifier=Notifier())
    for index in range(FIRST, 230):
        _cycle(engine, fake, index)
    held = next(iter(fake.positions))
    fake.positions[held][0] *= 2  # someone doubles the position by hand
    report = _cycle(engine, fake, 230)
    assert engine.unreconciled and any(alert["event_type"] == "external_position_change" for alert in alerts)
    for index in range(231, 260):
        report = _cycle(engine, fake, index)
        assert all(order.reduce_only for order in report.orders)
    engine.adopt_exchange_state(now=_now(260), reason="operator checked the manual trade")
    assert engine.unreconciled == {} and engine.reconcile() == {}


def test_the_kill_switch_on_a_live_account_cancels_everything_and_closes_every_position(tmp_path) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path)
    for index in range(FIRST, 260):
        _cycle(engine, fake, index)
    assert fake.positions, "the test needs open positions"
    report = engine.flatten(now=_now(260), reason="kill switch")
    assert any(request["endpoint"] == "cancelallorders" for request in fake.requests)
    assert fake.positions == {} and engine.book.units() == {} and engine.pending_orders == {}
    assert all(order.reduce_only for order in report.orders)


def test_the_live_plumbing_test_round_trips_the_minimum_size_and_records_it(tmp_path) -> None:
    from src.execution.live_test import run_futures_live_test

    class Rates:
        def get_rate(self, pair: str = "EUR/NOK", at: Any = None) -> float:
            return 10.0

    fake = FakeKraken(equity=20.0)  # about 200 NOK
    adapter = _adapter(fake)
    adapter.max_leverage = 3.0
    logger = TradeLogger(tmp_path / "trades.db")
    logger.fx_rate_collector = Rates()
    alerts = []

    class Notifier:
        def send_alert(self, **kwargs: Any) -> bool:
            alerts.append(kwargs)
            return True

    summary = run_futures_live_test(adapter, symbol="BTC/USD", mark_price=50_000.0, trade_logger=logger, notifier=Notifier(), sleep=lambda seconds: None)
    sent = fake.sent_orders()
    assert [(order["side"], order["size"], order.get("reduceOnly")) for order in sent] == [("buy", "0.0001", None), ("sell", "0.0001", "true")]
    assert fake.positions == {} and summary["steps"][0]["position_after"] == pytest.approx(0.0001) and summary["steps"][1]["position_after"] == 0.0
    assert summary["fees_estimated"] == pytest.approx(2 * 0.0001 * 50_000 * 0.0005)
    assert {row["strategy_id"] for row in logger.list_trades()} == {"live_test"} and len(logger.list_trades()) == 2
    assert {event["transaction_type"] for event in logger.list_tax_events()} == {"TRADING_FEE"}  # same price in and out: no P&L
    assert alerts and "[LIVE]" in alerts[0]["message"]


def test_the_live_plumbing_test_refuses_an_account_that_already_holds_the_contract_and_reports_a_stuck_fill(tmp_path) -> None:
    from src.execution.live_test import LiveTestError, run_futures_live_test

    fake = FakeKraken()
    fake.positions["PF_XBTUSD"] = [0.002, 50_000.0]
    with pytest.raises(LiveTestError, match="only runs from flat"):
        run_futures_live_test(_adapter(fake), symbol="BTC/USD", mark_price=50_000.0, sleep=lambda seconds: None)
    assert fake.sent_orders() == []

    fake = FakeKraken()
    fake.delay_fills = True  # the fill never shows up in /fills within the timeout
    with pytest.raises(LiveTestError, match="position on Kraken is 0.0001"):
        run_futures_live_test(_adapter(fake), symbol="BTC/USD", mark_price=50_000.0, sleep=lambda seconds: None, timeout=3)


def test_the_live_test_command_needs_the_live_gates_and_keys(monkeypatch, capsys, tmp_path) -> None:
    import argparse

    import main
    from config import settings

    monkeypatch.setattr(main, "PORTFOLIO_KILL_SWITCH_FILE", tmp_path / "kill.json")
    args = argparse.Namespace(enable_live_trading=False, live_confirmation=None, futures_symbol="BTC/USD")
    assert main.futures_live_test(args) == 2 and "places real orders" in capsys.readouterr().out
    args = argparse.Namespace(enable_live_trading=True, live_confirmation="ENABLE_LIVE_TRADING", futures_symbol="BTC/USD")
    monkeypatch.setattr(settings, "kraken_futures_api_key", "")
    assert main.futures_live_test(args) == 2 and "not set" in capsys.readouterr().out
    monkeypatch.setattr(settings, "kraken_futures_api_key", "key")
    monkeypatch.setattr(settings, "kraken_futures_secret", "c2VjcmV0")
    fake = FakeKraken(equity=20.0)
    code = main.futures_live_test(args, transport=fake, fetch_contract=lambda symbol: CONTRACTS[0], fetch_mark=lambda venue_symbol: 50_000.0, sleep=lambda seconds: None)
    assert code == 0 and "LIVE TEST OK" in capsys.readouterr().out and fake.positions == {}


def _book_matches_kraken(engine: PortfolioEngine, fake: FakeKraken) -> bool:
    held = {symbol: size for symbol, (size, _entry) in fake.positions.items()}
    return (float(engine.book.units().get(BTC, 0)) == pytest.approx(held.get("PF_XBTUSD", 0.0), abs=1e-9)
            and float(engine.book.units().get(ETH, 0)) == pytest.approx(held.get("PF_ETHUSD", 0.0), abs=1e-9))


def test_a_fill_that_appears_after_the_order_was_written_off_is_still_booked(tmp_path) -> None:
    from test_portfolio_engine import RecordingNotifier

    fake = FakeKraken()
    notifier = RecordingNotifier()
    engine = _live_engine(fake, tmp_path, notifier=notifier)
    fake.delay_fills = True  # the orders fill on Kraken, but /fills doesn't show them yet
    _cycle(engine, fake, FIRST)
    _cycle(engine, fake, FIRST + 1)
    assert engine.written_off_orders and not engine.pending_orders
    assert not _book_matches_kraken(engine, fake)
    watched_ids = set(engine.written_off_orders)

    fake.release_fills()
    report = _cycle(engine, fake, FIRST + 2)
    assert _book_matches_kraken(engine, fake) and not engine.written_off_orders
    assert any(alert["event_type"] == "late_fill" for alert in notifier.alerts)
    assert watched_ids <= {fill["order_id"] for fill in report.fills}  # booked as fills of the original orders


def test_written_off_orders_survive_a_restart_and_are_still_settled(tmp_path) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path)
    fake.delay_fills = True
    _cycle(engine, fake, FIRST)
    _cycle(engine, fake, FIRST + 1)
    watched = set(engine.written_off_orders)
    assert watched

    restarted = _live_engine(fake, tmp_path)  # same state dir: a new process
    assert set(restarted.written_off_orders) == watched
    fake.release_fills()
    _cycle(restarted, fake, FIRST + 2)
    assert _book_matches_kraken(restarted, fake)
    sent = [params["cliOrdId"] for params in fake.sent_orders()]
    assert len(sent) == len(set(sent))  # nothing was resent after the restart



def _stop_config(pct: float = 0.2):
    from dataclasses import replace

    config = _config()
    return replace(config, risk=replace(config.risk, exchange_stop_pct=pct))


def test_every_open_position_has_one_reduce_only_stop_resting_on_kraken(tmp_path) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path, config=_stop_config(0.2))
    seen_positions = 0
    for index in range(FIRST, 330):
        report = _cycle(engine, fake, index)
        assert report.rejected == [] and report.mismatches == {}
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for order in fake.resting:
            by_symbol.setdefault(order["symbol"], []).append(order)
        for venue_symbol, (size, _entry) in fake.positions.items():
            orders = by_symbol.get(venue_symbol, [])
            assert len(orders) == 1, (index, venue_symbol, orders)
            stop = orders[0]
            assert stop["reduceOnly"] and stop["triggerSignal"] == "mark" and stop["size"] == pytest.approx(abs(size))
            assert stop["side"] == ("sell" if size > 0 else "buy")
            instrument = BTC if venue_symbol == "PF_XBTUSD" else ETH
            anchor = engine.stop_anchors[instrument]
            assert stop["stopPrice"] == pytest.approx(anchor["reference_price"] * (0.8 if size > 0 else 1.2), abs=0.11)
            seen_positions += 1
        assert set(by_symbol) <= set(fake.positions)  # no stop left behind for a closed position
    assert seen_positions > 50
    # a replacement is placed before the stop it replaces is cancelled
    sequence = [(r["endpoint"], r["params"].get("orderType")) for r in fake.requests if r["endpoint"] in {"sendorder", "cancelorder"}]
    for position, (endpoint, _kind) in enumerate(sequence):
        if endpoint == "cancelorder" and position > 0:
            assert ("sendorder", "stp") in sequence[:position]


def test_a_stop_that_fires_while_the_process_is_down_is_booked_once_and_the_sleeves_stay_out(tmp_path) -> None:
    fake = FakeKraken()
    logger = TradeLogger(tmp_path / "trades.db")
    engine = _live_engine(fake, tmp_path, config=_stop_config(0.2))
    index = FIRST
    while "PF_XBTUSD" not in fake.positions or fake.positions["PF_XBTUSD"][0] <= 0:
        _cycle(engine, fake, index)
        index += 1
        assert index < 400, "the test data never opened a BTC long"
    # the process dies; BTC crashes 30% below the stop's reference and the stop fires on Kraken
    reference = engine.stop_anchors[BTC]["reference_price"]
    fake.prices["PF_XBTUSD"] = reference * 0.7
    assert len(fake.trigger_stops()) == 1 and "PF_XBTUSD" not in fake.positions
    # restart from the checkpoint
    restarted = PortfolioEngine(engine.config, adapters={"kraken_futures": _adapter(fake)}, book=PortfolioBook.from_config(engine.config), trade_logger=logger,
                                mode="live", state_path=tmp_path / "engine.json", record_tax=False, funding_source=lambda venue_symbol: [])
    assert restarted.restored
    btc_sleeves = [sleeve_id for sleeve_id, spec in restarted.sleeves.items() if spec.instrument == BTC]
    report = restarted.run_cycle(bars_until(index), now=_now(index))
    stop_fills = [fill for fill in report.fills if fill["reason"] == "exchange_stop"]
    assert len(stop_fills) == 1 and stop_fills[0]["side"] == "sell" and stop_fills[0]["price"] == pytest.approx(reference * 0.7)
    assert float(restarted.book.units().get(BTC, 0)) == 0.0 and report.mismatches == {} and restarted.unreconciled == {}
    assert all(restarted.states[s].weight == 0.0 for s in btc_sleeves)
    assert any(restarted.states[s].reentry_block == "long" for s in btc_sleeves)
    # booked exactly once, and no BTC long is bought back while the sleeves' signals are still long
    for later in range(index + 1, index + 12):
        report = _cycle(restarted, fake, later)
        assert not [fill for fill in report.fills if fill["reason"] == "exchange_stop"]
        blocked = [s for s in btc_sleeves if restarted.states[s].reentry_block == "long"]
        if blocked:
            assert not [fill for fill in report.fills if fill["instrument"] == BTC and fill["side"] == "buy" and len(fill["sleeves"]) == 1
                        and next(iter(fill["sleeves"])) in blocked]
    assert [row for row in logger.list_trades(limit=100) if row["side"] == "sell"]


def test_a_stop_that_kraken_refuses_keeps_the_old_one_and_alerts_once(tmp_path) -> None:
    from test_portfolio_engine import RecordingNotifier

    # the adapter: a refused replacement leaves the old stop resting
    fake = FakeKraken()
    adapter = _adapter(fake)
    fake.positions["PF_XBTUSD"] = [0.01, 50_000.0]
    placed = adapter.sync_protective_stops({"BTC/USD": ("sell", 0.01, 40_000.0)}, now=T0)
    assert [a["action"] for a in placed] == ["placed"] and fake.resting[0]["stopPrice"] == 40_000.0
    assert [a["action"] for a in adapter.sync_protective_stops({"BTC/USD": ("sell", 0.01, 40_000.0)}, now=T0)] == ["kept"]
    fake.refuse_stops = True
    refused = adapter.sync_protective_stops({"BTC/USD": ("sell", 0.02, 39_000.0)}, now=T0)
    assert [a["action"] for a in refused] == ["failed"] and len(fake.resting) == 1 and fake.resting[0]["stopPrice"] == 40_000.0
    fake.refuse_stops = False
    replaced = adapter.sync_protective_stops({"BTC/USD": ("sell", 0.02, 39_000.0)}, now=T0)
    assert [a["action"] for a in replaced] == ["placed", "cancelled"] and [o["stopPrice"] for o in fake.resting] == [39_000.0]
    assert [a["action"] for a in adapter.sync_protective_stops({"BTC/USD": None}, now=T0)] == ["cancelled"] and fake.resting == []

    # the engine: one alert per episode (from the first refused sync until a sync needs no stop or succeeds), not per cycle
    fake = FakeKraken()
    notifier = RecordingNotifier()
    engine = _live_engine(fake, tmp_path, config=_stop_config(0.2), notifier=notifier)
    fake.refuse_stops = True
    episodes, failing_cycles, alerted_before = 0, 0, False
    for index in range(FIRST, 330):
        report = _cycle(engine, fake, index)
        failing_cycles += any(action["action"] == "failed" for action in report.stop_actions)
        episodes += engine.stop_failure_alerted and not alerted_before  # a sync needing no stop (all flat) ends an episode
        alerted_before = engine.stop_failure_alerted
    assert fake.positions and failing_cycles > episodes >= 1  # it traded, every stop was refused, and failures persisted
    assert [alert["event_type"] for alert in notifier.alerts].count("exchange_stop_failed") == episodes


def test_no_stops_are_sent_unless_configured(tmp_path) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path)
    for index in range(FIRST, 260):
        _cycle(engine, fake, index)
    assert not [order for order in fake.sent_orders() if order.get("orderType") == "stp"]



def test_fees_and_funding_are_trued_up_from_the_account_log_so_book_and_kraken_agree(tmp_path) -> None:
    fake = FakeKraken()
    fake.log.append({"id": 1, "date": "2026-01-01T00:00:00Z", "info": "futures trade", "contract": "pf_xbtusd", "asset": "usd", "execution": "old-test-trade",
                     "fee": 0.5, "realized_funding": -0.1, "trade_price": 50_000.0})  # before the book existed: never re-booked
    fake.fee_rate = 0.0004  # Kraken charges less than the 0.05% taker estimate (e.g. a better fee tier)
    engine = _live_engine(fake, tmp_path)  # funding estimates from public rates: none in this test, so all of it is a true-up
    engine.record_tax = True
    last = FIRST
    for index in range(FIRST, 330):
        fake.now = _now(index)
        fake.accrue_funding(0.00002)
        report = _cycle(engine, fake, index)
        last = index
        if index == 260:  # a restart halfway: nothing is booked twice
            engine = _live_engine(fake, tmp_path)
            engine.record_tax = True
    fake.now = _now(last + 1)
    report = _cycle(engine, fake, last + 1)
    while not report.decided:
        last += 1
        report = _cycle(engine, fake, last + 1)
    kraken = fake("GET", "https://x/api/v3/accounts", {}, None)["accounts"]["flex"]["marginEquity"]
    assert float(engine.book.equity()) == pytest.approx(kraken, abs=1e-6)  # to the cent and beyond, fees and funding included
    fee_ups = [r for r in engine.pending_tax if r["metadata"].get("kind") == "fee_true_up"]
    funding_ups = [r for r in engine.pending_tax if r["metadata"].get("kind") == "funding_true_up"]
    assert fee_ups and all(r["amount"] > 0 for r in fee_ups)  # paid less than estimated: the correction is a gain
    realized_on_kraken = sum(fill.get("realized_funding") or 0.0 for fill in fake.fills)  # full precision (the log is rounded)
    assert funding_ups and sum(r["amount"] for r in funding_ups) == pytest.approx(realized_on_kraken + sum(fake.unrealized_funding.values()), abs=1e-6)
    assert not any(r["metadata"].get("execution") == "old-test-trade" for r in fee_ups)
    assert engine.account_log_last_id["kraken_futures"] == len(fake.log)


def test_fees_of_other_senders_are_left_alone(tmp_path) -> None:
    fake = FakeKraken()
    fake.fee_rate = 0.0004
    engine = _live_engine(fake, tmp_path)
    _cycle(engine, fake, FIRST)  # the first decision starts the log reconciliation
    fake.now = _now(FIRST + 1)
    fake._fill("PF_XBTUSD", 0.001, "cqm-kill-20260926T214646-0")  # the kill switch or a manual trade, not this book
    fake._fill("PF_XBTUSD", -0.001, "cqm-kill-20260926T214647-0")
    engine.adopt_exchange_state(now=_now(FIRST + 1), reason="test")
    report = None
    for index in range(FIRST + 1, FIRST + 12):
        report = _cycle(engine, fake, index)
        if report.decided and report.account_log:
            break
    assert report is not None and report.account_log["kraken_futures"]["fee_gap"] == 0.0


def test_a_crash_right_after_kraken_took_an_order_is_settled_from_fills_and_never_resent(tmp_path) -> None:
    """The process dies inside the decision: Kraken has the order, the engine never saw the answer or finished its cycle."""

    class Rates:
        def get_rate(self, pair: str = "EUR/NOK", at: Any = None) -> float:
            return 10.0

    class Died(BaseException):
        pass

    fake = FakeKraken()
    logger = TradeLogger(tmp_path / "trades.db")
    logger.fx_rate_collector = Rates()
    engine = _live_engine(fake, tmp_path, logger=logger)
    adapter = engine.adapters["kraken_futures"]
    original = adapter.submit_order

    def submit(**kwargs: Any):
        original(**kwargs)
        raise Died()

    adapter.submit_order = submit
    index = FIRST
    with pytest.raises(Died):
        while True:
            _cycle(engine, fake, index)
            index += 1
    [sent] = fake.sent_orders()
    saved = json.loads((tmp_path / "engine.json").read_text())
    assert [f"{adapter.client_id_prefix}-{order_id}" for order_id in saved["pending_orders"]] == [sent["cliOrdId"]]  # written down before it was sent
    del engine

    restarted = _live_engine(fake, tmp_path, logger=logger)
    report = _cycle(restarted, fake, index - 1)  # the first poll after the restart, still within the same bar
    booked = [fill["order_id"] for fill in report.fills]
    assert booked[0] == next(iter(saved["pending_orders"])) and restarted.pending_orders == {}  # found in Kraken's fills, booked first
    assert booked[1:] == [entry["order_id"] for entry in saved["open_plan"]] and restarted.open_plan == []  # then the rest of the interrupted plan went out
    for later in range(index, index + 3):
        assert _cycle(restarted, fake, later).mismatches == {}
    sent_ids = [order["cliOrdId"] for order in fake.sent_orders()]
    assert len(sent_ids) == len(set(sent_ids)) and sent_ids.count(sent["cliOrdId"]) == 1  # nothing was sent twice
    for instrument, venue_symbol in ((BTC, "PF_XBTUSD"), (ETH, "PF_ETHUSD")):
        assert float(restarted.book.units().get(instrument, 0)) == pytest.approx(fake.positions.get(venue_symbol, [0.0])[0])
    restarted._flush_tax(_now(index + 3))
    fees = [event for event in logger.list_tax_events(transaction_types=["TRADING_FEE"]) if not event["metadata"].get("kind")]
    assert len(fees) == len(fake.sent_orders())  # one fee record per order: the recovered fill is in the ledger once
