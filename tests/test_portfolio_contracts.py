"""Contract sleeves in the portfolio engine: bets under a budget, taker and maker orders, restarts, settlement."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest

from src.portfolio import contracts
from src.portfolio.book import PortfolioBook
from src.portfolio.contracts import ContractTarget, fit_to_budget
from src.portfolio.engine import PortfolioEngine, build_paper_adapters
from src.storage.trade_logger import TradeLogger
from test_portfolio_engine import FIRST, _config, _now, bars_until

MARKET = "kalshi:KXBTCD-26OCT0416-T85399.99"
OTHER = "kalshi:KXBTCD-26OCT0416-T85499.99"


class Scripted:
    """A strategy that wants whatever the test last set."""

    def __init__(self) -> None:
        self.wanted: list[ContractTarget] = []
        self.seen: list[contracts.ContractContext] = []
        self.fail = False

    def targets(self, context):
        self.seen.append(context)
        if self.fail:
            raise RuntimeError("model blew up")
        return list(self.wanted)


class Books:
    def __init__(self) -> None:
        self.book = {MARKET: ([(0.36, 100.0), (0.35, 500.0)], [(0.38, 30.0), (0.39, 200.0)]), OTHER: ([(0.10, 100.0)], [(0.12, 100.0)])}

    def __call__(self, instrument: str):
        if instrument not in self.book:
            return None
        bids, asks = self.book[instrument]
        return pd.DataFrame([("bid", p, s) for p, s in bids] + [("ask", p, s) for p, s in asks], columns=["side", "price", "size"])


contracts.register("scripted", Scripted)
SLEEVE = {"id": "bets", "venue": "kalshi", "strategy": "scripted", "budget": 0.05}


def _engine(tmp_path, strategy: Scripted, books: Books, *, logger=None, execution=None) -> PortfolioEngine:
    config = _config(contract_sleeves=[SLEEVE], execution=execution)
    book = PortfolioBook.from_config(config)
    adapters = build_paper_adapters(config, book, state_dir=tmp_path, contract_books=books)
    return PortfolioEngine(config, adapters=adapters, book=book, trade_logger=logger, state_path=tmp_path / "engine.json", contract_strategies={"bets": strategy}, contract_books=books)


def _cycle(engine: PortfolioEngine, minutes: int = 0, index: int = FIRST):
    return engine.run_cycle(bars_until(index), now=_now(index) + timedelta(minutes=minutes))


def test_the_venue_is_funded_with_the_budget_and_a_taker_bet_fills_from_the_book(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    strategy, books = Scripted(), Books()
    engine = _engine(tmp_path, strategy, books, logger=logger)
    assert engine.book.cash == {"kalshi": Decimal("500"), "kraken_futures": Decimal("9500")} and float(engine.book.equity()) == 10_000.0
    strategy.wanted = [ContractTarget(MARKET, 20, 0.40, reason="model says 55%")]
    report = _cycle(engine)
    [fill] = [fill for fill in report.fills if fill["instrument"] == MARKET]
    assert (fill["side"], fill["units"], fill["price"], fill["reason"], fill["strategy_id"]) == ("buy", 20.0, 0.38, "model says 55%", "bets")  # at the offer, inside the limit
    assert engine.book.units()[MARKET] == 20 and engine.contract_owner == {MARKET: "bets"} and engine.reconcile() == {} and engine.pending_orders == {}
    context = strategy.seen[-1]
    assert context.budget == pytest.approx(500.0) and context.held == {} and context.books(MARKET) is not None
    sleeve = engine.snapshot()["contract_sleeves"]["bets"]
    assert sleeve["at_risk"] == pytest.approx(7.6) and sleeve["contracts"] == [MARKET] and sleeve["budget"] == pytest.approx(float(engine.book.equity()) * 0.05)
    assert [trade["pair"] for trade in logger.list_trades() if trade["exchange"] == "kalshi"] == [MARKET.split(":", 1)[1]]

    again = _cycle(engine, 5)
    assert not [fill for fill in again.fills if fill["instrument"] == MARKET] and strategy.seen[-1].held == {MARKET: 20.0}  # the target is held: nothing to do
    assert float(engine.book.marks[MARKET]) == pytest.approx(0.37)  # marked at the middle of the real book

    for index in range(FIRST + 1, FIRST + 40):  # weight sleeves keep deciding on new bars with a bet in the book
        report = _cycle(engine, 0, index)
    assert engine.book.units()[MARKET] == 20 and engine.reconcile() == {}

    strategy.wanted = []  # no longer named: closed at what the book bids
    closed = _cycle(engine, 5, FIRST + 39)
    [fill] = [fill for fill in closed.fills if fill["instrument"] == MARKET]
    assert (fill["side"], fill["units"], fill["price"]) == ("sell", 20.0, 0.36) and MARKET not in engine.book.units() and engine.contract_owner == {}


def test_targets_are_cut_to_the_budget_and_to_what_the_book_offers(tmp_path) -> None:
    scaled = fit_to_budget([ContractTarget(MARKET, 1_000, 0.40), ContractTarget(OTHER, -1_000, 0.10)], 500.0)  # costs 400 + 900 = 1,300
    assert [target.units for target in scaled] == [384.0, -384.0] and sum(contracts.at_risk(t.units, t.limit_price) for t in scaled) <= 500.0
    assert [target.units for target in fit_to_budget([ContractTarget(MARKET, 10.7, 0.40)], 500.0)] == [10.0]  # whole contracts

    strategy, books = Scripted(), Books()
    engine = _engine(tmp_path, strategy, books)
    strategy.wanted = [ContractTarget(MARKET, 5_000, 0.39)]  # 1,950 of bets on a budget of 500: 1,282 contracts; the book offers 230 inside the limit
    report = _cycle(engine)
    [fill] = [fill for fill in report.fills if fill["instrument"] == MARKET]
    assert fill["units"] == 230.0 and fill["price"] == pytest.approx((30 * 0.38 + 200 * 0.39) / 230) and engine.reconcile() == {}
    strategy.wanted = [ContractTarget(OTHER, -100, 0.10, reason="sell Yes = buy No"), ContractTarget(MARKET, 230, 0.39)]
    report = _cycle(engine, 5)
    [no] = [fill for fill in report.fills if fill["instrument"] == OTHER]
    assert (no["side"], no["units"], no["price"]) == ("sell", 100.0, 0.10) and engine.book.units()[OTHER] == -100 and engine.book.binary_stake("kalshi") == 100
    assert engine.adapters["kalshi"].stake() == 100 and engine.reconcile() == {}


def test_a_maker_bet_rests_then_fills_or_is_withdrawn_at_the_timeout_and_never_chases(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    strategy, books = Scripted(), Books()
    engine = _engine(tmp_path, strategy, books, logger=logger, execution={"policy": "taker", "maker_timeout_seconds": 600})
    strategy.wanted = [ContractTarget(MARKET, 50, 0.37, style="maker")]
    report = _cycle(engine)
    adapter = engine.adapters["kalshi"]
    assert not report.fills or all(fill["instrument"] != MARKET for fill in report.fills)
    [(order_id, meta)] = [(key, value) for key, value in engine.pending_orders.items() if value["instrument"] == MARKET]
    assert meta["style"] == "maker" and adapter.resting_orders()[order_id]["limit_price"] == 0.37
    assert _cycle(engine, 5).fills == [] and len(adapter.resting_orders()) == 1  # still waiting; no second order is stacked on it

    books.book[MARKET] = ([(0.36, 100.0)], [(0.37, 10.0), (0.39, 200.0)])  # an offer arrives at our price
    hit = _cycle(engine, 10)
    [fill] = hit.fills
    assert (fill["units"], fill["price"], fill["fee"], fill["liquidity"]) == (50.0, 0.37, 0.0, "maker") and engine.book.units()[MARKET] == 50 and engine.pending_orders == {}

    strategy.wanted = [ContractTarget(MARKET, 50, 0.37), ContractTarget(OTHER, 40, 0.09, style="maker")]  # a second bet, resting below its market
    _cycle(engine, 15)
    [first] = adapter.resting_orders()
    late = _cycle(engine, 30)  # 15 minutes later: past the 10-minute timeout
    # the stale order is withdrawn; the strategy still wants the bet, so a fresh order (at the strategy's price now) takes its place
    [second] = adapter.resting_orders()
    assert second != first and adapter.get_order_status(order_id=first).status == "CANCELED" and list(engine.pending_orders) == [second]
    outcomes = [event["metadata"]["outcome"] for event in logger.list_events(event_types=["portfolio_maker_result"])]
    assert "unfilled" in outcomes and not any(fill["instrument"] == OTHER for fill in late.fills)  # withdrawn, and not sent to market
    assert OTHER not in engine.book.units() and engine.reconcile() == {}


def test_a_restart_keeps_bets_resting_orders_and_books_a_fill_once(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    strategy, books = Scripted(), Books()
    engine = _engine(tmp_path, strategy, books, logger=logger)
    strategy.wanted = [ContractTarget(OTHER, 30, 0.12), ContractTarget(MARKET, 50, 0.37, style="maker")]
    _cycle(engine)
    assert engine.book.units()[OTHER] == 30 and len(engine.pending_orders) == 1

    restarted = _engine(tmp_path, strategy, books, logger=logger)  # the process died with a bet held and an order resting
    assert restarted.restored and restarted.book.units()[OTHER] == 30 and restarted.contract_owner == {OTHER: "bets", MARKET: "bets"} and len(restarted.pending_orders) == 1
    assert restarted.book.instruments[OTHER].kind == "binary" and restarted.adapters["kalshi"].position_size(OTHER.split(":", 1)[1]) == 30
    books.book[MARKET] = ([(0.36, 100.0)], [(0.37, 10.0)])
    hit = _cycle(restarted, 5)
    assert [fill["instrument"] for fill in hit.fills] == [MARKET] and restarted.book.units()[MARKET] == 50 and restarted.reconcile() == {}
    assert len([trade for trade in logger.list_trades() if trade["exchange"] == "kalshi"]) == 2  # each fill in the trade log once


def test_a_resolved_market_pays_out_in_the_book_and_on_the_paper_venue_and_a_broken_strategy_stops_nothing(tmp_path) -> None:
    strategy, books = Scripted(), Books()
    engine = _engine(tmp_path, strategy, books)
    strategy.wanted = [ContractTarget(MARKET, 20, 0.40), ContractTarget(OTHER, -50, 0.10)]
    _cycle(engine)
    adapter = engine.adapters["kalshi"]
    answers = {MARKET: True, OTHER: False}
    engine.binary_results = lambda instrument: answers.get(instrument)
    strategy.fail = True  # the strategy now raises every cycle
    report = _cycle(engine, 5)
    assert report is not None and engine.book.units().get(MARKET) is None and engine.book.units().get(OTHER) is None and engine.contract_owner == {}
    assert adapter.position_size(MARKET.split(":", 1)[1]) == 0 and adapter.stake() == 0
    assert float(engine.book.cash["kalshi"]) == pytest.approx(adapter.cash)  # 20 Yes paid 1 each; the 50 No kept their 0.10 a contract
    assert adapter.cash == pytest.approx(500 - 20 * 0.38 + 20 + 50 * 0.10 - sum(position.fees for position in [engine.book.positions[MARKET], engine.book.positions[OTHER]]).__float__())
    for index in range(FIRST + 1, FIRST + 10):
        assert _cycle(engine, 0, index) is not None  # the perp sleeves trade on


def test_the_config_checks_contract_sleeves() -> None:
    assert _config(contract_sleeves=[SLEEVE]).contract_sleeves[0].budget == 0.05
    for bad, message in (({**SLEEVE, "strategy": "nope"}, "is not registered"), ({**SLEEVE, "venue": "betfair"}, "venue must be one of"), ({**SLEEVE, "budget": 0.9}, "budget must be"),
                         ({**SLEEVE, "id": "btc_ma_1d"}, "needs an id that no other sleeve uses"), ({**SLEEVE, "size": 3}, "unknown key")):
        with pytest.raises(Exception, match=message):
            _config(contract_sleeves=[bad])
    with pytest.raises(ValueError, match="limit price is between 0 and 1"):
        ContractTarget(MARKET, 5, 1.2)
    with pytest.raises(ValueError, match="style must be"):
        ContractTarget(MARKET, 5, 0.5, style="market")
