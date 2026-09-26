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

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict[str, Any]:
        path, _, query = url.partition("?")
        endpoint = path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(body.decode() if body else query))
        self.requests.append({"endpoint": endpoint, "params": params})
        if endpoint == "sendorder":
            size, entry = self.positions.get(params["symbol"], [0.0, 0.0])
            signed = float(params["size"]) * (1 if params["side"] == "buy" else -1)
            if params.get("reduceOnly") == "true" and (abs(size + signed) > abs(size) + 1e-12 or (size + signed) * size < 0):
                return {"result": "success", "sendStatus": {"status": "wouldNotReducePosition"}}
            self._fill(params["symbol"], signed, params["cliOrdId"])
            if self.lose_next_response:
                self.lose_next_response = False
                raise TimeoutError("read timed out")  # the order reached Kraken; our process never heard back
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": f"uuid-{len(self.fills)}"}}
        if endpoint == "fills":
            return {"result": "success", "fills": list(self.fills)}
        if endpoint == "openorders":
            return {"result": "success", "openOrders": []}
        if endpoint == "openpositions":
            return {"result": "success", "openPositions": [{"symbol": symbol, "side": "long" if size > 0 else "short", "size": abs(size), "price": entry,
                                                            "unrealizedFunding": 0.0} for symbol, (size, entry) in self.positions.items() if size]}
        if endpoint == "accounts":
            unrealized = sum(size * (self.prices[symbol] - entry) for symbol, (size, entry) in self.positions.items())
            equity = self.collateral + unrealized
            used = sum(abs(size) * self.prices[symbol] / 2.0 for symbol, (size, _entry) in self.positions.items())
            return {"result": "success", "accounts": {"flex": {"marginEquity": equity, "availableMargin": equity - used, "totalUnrealized": unrealized}}}
        if endpoint == "cancelallorders":
            return {"result": "success", "cancelStatus": {"status": "cancelled"}}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def _fill(self, symbol: str, signed: float, cli_ord_id: str) -> None:
        price = self.prices[symbol]
        size, entry = self.positions.get(symbol, [0.0, 0.0])
        if size and (size > 0) != (signed > 0):
            closed = min(abs(size), abs(signed))
            self.collateral += closed * (price - entry) * (1 if size > 0 else -1)
        new = size + signed
        if abs(new) < 1e-12:
            self.positions.pop(symbol, None)
        elif size == 0 or (size > 0) != (new > 0):
            self.positions[symbol] = [new, price]
        elif abs(new) > abs(size):
            self.positions[symbol] = [new, (entry * abs(size) + price * abs(signed)) / abs(new)]
        else:
            self.positions[symbol] = [new, entry]
        self.collateral -= abs(signed) * price * 0.0005  # the taker fee Kraken charges (our estimate uses the same rate)
        fill = {"cliOrdId": cli_ord_id, "order_id": f"uuid-{len(self.fills) + len(self.hidden_fills) + 1}", "symbol": symbol, "size": abs(signed), "price": price,
                "side": "buy" if signed > 0 else "sell", "fillType": "taker"}
        (self.hidden_fills if self.delay_fills else self.fills).append(fill)

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


def _live_engine(fake: FakeKraken, tmp_path, *, logger=None, notifier=None, funding=None) -> PortfolioEngine:
    config = _config()
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
        _cycle(engine, fake, index)
    booked = sum(float(position.funding) for position in engine.book.positions.values())
    taxed = [event for event in logger.list_tax_events(transaction_types=["FUNDING_FEE"])]
    assert booked != 0.0 and len(taxed) > 20
    assert sum(event["metadata"]["amount"] for event in taxed) == pytest.approx(-booked)
    assert all(event["metadata"]["estimate"] for event in taxed)


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
