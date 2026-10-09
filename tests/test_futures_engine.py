"""Futures in the portfolio engine: the session-bound ledger (paper and order file), rolls, NOK sizing, the feed."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.futures import contracts as fc
from src.futures import wiring
from src.futures.adapter import ContractBoard, SessionFuturesAdapter
from src.futures.feed import FuturesFeed, completed, fill_holidays, splice
from src.futures.liquidity import LiquidityRules
from src.futures.order_file import read_orders
from src.futures.quotes import DailyStore
from src.futures.sessions import DEFAULT_SLOTS, Sessions, releasable
from src.portfolio.book import CASH_VENUE, PortfolioBook
from src.portfolio.config import PortfolioConfigError, parse_portfolio_config
from src.portfolio.engine import PortfolioEngine, build_paper_adapters
from src.portfolio.orders import plan_orders
from src.storage.bar_aggregator import OHLCVBar

MES, M6E = "cme_usd:MES", "cme_usd:M6E"
UTC = timezone.utc
EVENING = datetime(2026, 10, 8, 21, 0, tzinfo=UTC)      # Thursday, after the US close
SLOT = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)         # Friday 16:00 Oslo: the americas slot
SESSIONS = Sessions()
MARKETS = fc.load_markets()


def ledger(tmp_path, *, mode: str = "paper", prices: Any = None, store: DailyStore | None = None, board: ContractBoard | None = None, **extra: Any) -> SessionFuturesAdapter:
    board = board or ContractBoard(MARKETS, SESSIONS, tmp_path / "contracts.json")
    return SessionFuturesAdapter(venue="cme_usd", markets={"MES": MARKETS[MES], "M6E": MARKETS[M6E]}, board=board, sessions=SESSIONS, slots=DEFAULT_SLOTS,
                                 rules=LiquidityRules(), mode=mode, book_name="test", prices=prices, store=store, state_path=tmp_path / "ledger.json",
                                 order_dir=tmp_path / "orders", fills_dir=tmp_path / "fills", **extra)


def submit(adapter: SessionFuturesAdapter, order_id: str, side: str, contracts: int, *, symbol: str = "MES", price: float = 6800.0, when: datetime = EVENING, **extra: Any):
    return adapter.submit_order(order_id=order_id, side=side, size=contracts * float(adapter.markets[symbol].multiplier), price=price, timestamp=when, symbol=symbol, **extra)


# --- sessions ---------------------------------------------------------------------------------------------------------

def test_releasable_needs_a_liquid_market_and_a_slot_that_has_come_up() -> None:
    mes = MARKETS[MES]
    assert releasable(mes, SLOT - timedelta(minutes=10), DEFAULT_SLOTS, SESSIONS) is None  # liquid since 13:30, but the slot is at 14:00
    assert releasable(mes, SLOT, DEFAULT_SLOTS, SESSIONS).name == "americas"
    assert releasable(mes, SLOT + timedelta(hours=3), DEFAULT_SLOTS, SESSIONS).name == "americas"  # decided late: still goes out today
    assert releasable(mes, SLOT + timedelta(hours=7), DEFAULT_SLOTS, SESSIONS) is None  # after the close
    assert releasable(mes, datetime(2026, 10, 9, 8, 0, tzinfo=UTC), DEFAULT_SLOTS, SESSIONS) is None  # the europe slot is not in US hours


# --- the ledger on paper ----------------------------------------------------------------------------------------------

def test_paper_order_waits_for_its_slot_then_fills_at_the_price_plus_cost(tmp_path) -> None:
    adapter = ledger(tmp_path)
    report = submit(adapter, "o1", "buy", 3)
    assert report.status == "SUBMITTED" and "americas" in report.message and adapter.position_size("MES") == 0.0
    assert adapter.on_market_update(prices={"MES": 6810.0}, timestamp=SLOT - timedelta(hours=1)) == [] and adapter.settle_orders() == []
    adapter.on_market_update(prices={"MES": 6810.0}, timestamp=SLOT + timedelta(minutes=5))
    assert adapter.settle_orders() == [{"order_id": "o1", "status": "FILLED", "filled_size": 15.0, "fill_price": 6810.25, "fee": 0.0, "contract": "MESZ26"}]
    assert adapter.position_size("MES") == 15.0 and adapter.positions == {"MES": {"MESZ26": 3}} and adapter.settle_orders() == []
    submit(adapter, "o2", "sell", 1, when=SLOT + timedelta(hours=1), reduce_only=True)  # the market is liquid and the slot has passed: goes out now
    adapter.on_market_update(prices={"MES": 6800.0}, timestamp=SLOT + timedelta(hours=1))
    assert adapter.settle_orders()[0]["fill_price"] == 6799.75 and adapter.contracts_held("MES") == 2


def test_orders_the_ledger_refuses(tmp_path) -> None:
    adapter = ledger(tmp_path, max_contracts={MES: 2}, no_trade=[M6E])
    assert adapter.submit_order(order_id="a", side="buy", size=7.0, price=1.0, timestamp=EVENING, symbol="MES").status == "REJECTED"  # 1.4 contracts
    assert submit(adapter, "b", "sell", 1, reduce_only=True).status == "REJECTED"  # nothing to reduce
    assert "limit is 2" in submit(adapter, "c", "buy", 3).message
    assert "no-trade" in submit(adapter, "d", "buy", 1, symbol="M6E", price=1.16).message
    assert adapter.submit_order(order_id="e", side="buy", size=5.0, price=1.0, timestamp=EVENING, symbol="XXX").status == "REJECTED"
    assert submit(adapter, "f", "buy", 2).status == "SUBMITTED" and submit(adapter, "f", "buy", 2).message == "already received"
    assert len(adapter.orders) == 1


def test_volume_rules_cap_an_order_and_block_a_thin_contract(tmp_path) -> None:
    store = DailyStore(tmp_path / "daily")
    days = pd.bdate_range("2026-09-01", periods=20, name="day")
    frame = lambda volume: pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": volume}, index=days)  # noqa: E731
    store.merge("MESZ26", frame(300.0))   # 1% of 300 = 3 contracts
    store.merge("M6EZ26", frame(50.0))    # below the floor of 200
    adapter = ledger(tmp_path, store=store)
    submit(adapter, "big", "buy", 10)
    submit(adapter, "thin", "buy", 2, symbol="M6E", price=1.16)
    events = adapter.on_market_update(prices={"MES": 6800.0, "M6E": 1.16}, timestamp=SLOT)
    outcomes = {item["order_id"]: item for item in adapter.settle_orders()}
    assert outcomes["big"]["filled_size"] == 15.0 and outcomes["thin"] == {"order_id": "thin", "status": "CANCELED"}
    assert {event["order_id"]: event["problem"] for event in events if event["type"] == "liquidity"} == {"big": False, "thin": True}


def test_paper_fill_needs_a_fresh_price(tmp_path) -> None:
    quotes: dict[str, tuple[float, datetime]] = {"MESZ26.CME": (6805.0, SLOT - timedelta(hours=3))}
    adapter = ledger(tmp_path, prices=quotes.get)
    submit(adapter, "o1", "buy", 1)
    events = adapter.on_market_update(prices={"MES": 6700.0}, timestamp=SLOT)
    assert [event["type"] for event in events] == ["no_price"] and adapter.settle_orders() == []
    assert adapter.on_market_update(prices={"MES": 6700.0}, timestamp=SLOT + timedelta(minutes=5)) == []  # said once
    quotes["MESZ26.CME"] = (6805.0, SLOT)
    adapter.on_market_update(prices={"MES": 6700.0}, timestamp=SLOT + timedelta(minutes=10))
    assert adapter.settle_orders()[0]["fill_price"] == 6805.25  # the contract's own price, not the engine's mark


def test_state_survives_a_restart_and_lost_orders_are_named(tmp_path) -> None:
    adapter = ledger(tmp_path)
    submit(adapter, "o1", "buy", 2)
    again = ledger(tmp_path)
    assert list(again.orders) == ["o1"]
    again.track_order(order_id="o1", symbol="MES", side="buy", size=10.0, price=6800.0, timestamp=EVENING)
    again.track_order(order_id="ghost", symbol="MES", side="buy", size=5.0, price=6800.0, timestamp=EVENING)
    assert again.settle_orders() == [{"order_id": "ghost", "status": "CANCELED", "never_received": True}]  # o1 is still waiting: no outcome yet
    again.on_market_update(prices={"MES": 6800.0}, timestamp=SLOT)
    filled = again.settle_orders()
    third = ledger(tmp_path)
    third.track_order(order_id="o1", symbol="MES", side="buy", size=10.0, price=6800.0, timestamp=EVENING)
    assert third.settle_orders() == filled and third.position_size("MES") == 10.0  # the engine lost the fill: it is told again


# --- rolls ------------------------------------------------------------------------------------------------------------

ROLL_SLOT = datetime(2026, 12, 15, 15, 0, tzinfo=UTC)  # the roll day of MESZ26; 16:00 Oslo in winter


def test_paper_roll_moves_the_position_and_reports_both_prices(tmp_path) -> None:
    now = [SLOT]
    quotes = lambda symbol: ({"MESZ26.CME": 6800.0, "MESH27.CME": 6860.0}[symbol], now[0])  # noqa: E731
    adapter = ledger(tmp_path, prices=quotes)
    submit(adapter, "o1", "sell", 2)
    adapter.on_market_update(prices={}, timestamp=SLOT)
    adapter.settle_orders()
    mes_events = lambda: [event for event in adapter.on_market_update(prices={}, timestamp=now[0]) if event["symbol"] == "MES"]  # noqa: E731
    now[0] = ROLL_SLOT - timedelta(days=1)
    assert mes_events() == []  # the day before: nothing
    now[0] = ROLL_SLOT - timedelta(hours=3)
    assert mes_events() == []  # the roll day, before the slot
    now[0] = ROLL_SLOT
    assert mes_events() == [{"type": "roll", "symbol": "MES", "from_contract": "MESZ26", "to_contract": "MESH27", "units": -10.0, "contracts": -2,
                       "from_price": 6800.25, "to_price": 6859.75, "fee": 0.0}]  # a short buys back the old and sells the new, each a tick worse
    assert adapter.positions == {"MES": {"MESH27": -2}} and adapter.board.current(MES, now[0].date()).code == "MESH27"
    assert adapter.on_market_update(prices={}, timestamp=now[0] + timedelta(minutes=5)) == []


def test_a_flat_market_changes_contract_without_a_trade_and_volume_can_bring_it_forward(tmp_path) -> None:
    store = DailyStore(tmp_path / "daily")
    days = pd.bdate_range("2026-12-01", periods=5, name="day")
    for code, volume in (("MESZ26", 100_000.0), ("MESH27", 400_000.0)):
        store.merge(code, pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": volume}, index=days))
    adapter = ledger(tmp_path, store=store)
    assert adapter.board.current(MES, date(2026, 12, 9)).code == "MESZ26"
    events = adapter.on_market_update(prices={}, timestamp=datetime(2026, 12, 9, 9, 0, tzinfo=UTC))  # a week before the calendar roll
    assert [event["type"] for event in events] == ["contract_change"] and adapter.board.current(MES, date(2026, 12, 9)).code == "MESH27"


# --- the order file ---------------------------------------------------------------------------------------------------

def test_file_mode_writes_orders_and_books_only_reported_fills(tmp_path) -> None:
    adapter = ledger(tmp_path, mode="file")
    submit(adapter, "o1", "buy", 3)
    submit(adapter, "o2", "sell", 4, symbol="M6E", price=1.16)
    events = adapter.on_market_update(prices={}, timestamp=SLOT)
    written = [event for event in events if event["type"] == "order_file"]
    assert len(written) == 1 and written[0]["orders"] == ["o1", "o2"]
    rows = read_orders(written[0]["path"])
    assert [(row["order_id"], row["contract"], row["side"], row["quantity"], row["order_type"], row["slot"]) for row in rows] == [
        ("o1", "MESZ26", "BUY", "3", "MKT", "americas"), ("o2", "M6EZ26", "SELL", "4", "MKT", "americas")]
    assert rows[0]["valid_until_utc"] == "2026-10-09T20:00:00+00:00" and adapter.position_size("MES") == 0.0 and adapter.settle_orders() == []
    assert adapter.on_market_update(prices={}, timestamp=SLOT + timedelta(minutes=5)) == []  # not written twice

    (tmp_path / "fills").mkdir()
    (tmp_path / "fills" / "fills_1.csv").write_text("order_id,contract,side,quantity,price,fee\no1,MESZ26,BUY,2,6801.50,1.2\nother-book,MESZ26,BUY,9,1,0\n")
    adapter.on_market_update(prices={}, timestamp=SLOT + timedelta(minutes=10))
    assert adapter.settle_orders() == [{"order_id": "o1", "status": "PARTIAL", "filled_size": 10.0, "fill_price": 6801.5, "fee": 1.2, "contract": "MESZ26"}]
    (tmp_path / "fills" / "fills_2.csv").write_text("order_id,contract,side,quantity,price,fee\no1,MESZ26,BUY,1,6802.00,0.6\n")
    adapter.on_market_update(prices={}, timestamp=SLOT + timedelta(minutes=20))
    assert adapter.settle_orders()[0]["status"] == "FILLED" and adapter.position_size("MES") == 15.0 and "o1" not in adapter.orders

    events = adapter.on_market_update(prices={}, timestamp=SLOT + timedelta(hours=9))  # two hours past the close, o2 never reported
    assert [event["type"] for event in events] == ["order_unfilled"] and adapter.settle_orders() == [{"order_id": "o2", "status": "CANCELED"}]
    (tmp_path / "fills" / "fills_3.csv").write_text("order_id,contract,side,quantity,price,fee\no2,M6EZ26,SELL,4,1.1601,0\n")
    adapter.on_market_update(prices={}, timestamp=SLOT + timedelta(hours=10))
    assert adapter.settle_orders()[0]["filled_size"] == 50_000.0 and adapter.contracts_held("M6E") == -4  # a late fill is still booked


def test_file_mode_roll_is_two_orders_and_completes_on_their_fills(tmp_path) -> None:
    adapter = ledger(tmp_path, mode="file")
    adapter.positions = {"MES": {"MESZ26": 3}}
    adapter.board.current(MES, date(2026, 10, 9))
    events = adapter.on_market_update(prices={}, timestamp=ROLL_SLOT)
    rows = read_orders(events[0]["path"])
    assert [(row["order_id"], row["contract"], row["side"], row["quantity"], row["reason"]) for row in rows] == [
        ("roll-MESZ26-MESH27-close", "MESZ26", "SELL", "3", "roll"), ("roll-MESZ26-MESH27-open", "MESH27", "BUY", "3", "roll")]
    submit(adapter, "o1", "buy", 1, when=ROLL_SLOT)
    assert adapter.on_market_update(prices={}, timestamp=ROLL_SLOT + timedelta(minutes=5)) == []  # the new order waits for the roll
    (tmp_path / "fills").mkdir()
    (tmp_path / "fills" / "fills_1.csv").write_text("order_id,contract,side,quantity,price,fee\nroll-MESZ26-MESH27-close,MESZ26,SELL,3,6800,1\n")
    assert adapter.on_market_update(prices={}, timestamp=ROLL_SLOT + timedelta(minutes=10)) == [] and adapter.positions == {"MES": {"MESZ26": 3}}
    (tmp_path / "fills" / "fills_2.csv").write_text("order_id,contract,side,quantity,price,fee\nroll-MESZ26-MESH27-open,MESH27,BUY,3,6861,1\n")
    events = adapter.on_market_update(prices={}, timestamp=ROLL_SLOT + timedelta(minutes=15))
    roll = next(event for event in events if event["type"] == "roll")
    assert (roll["from_price"], roll["to_price"], roll["fee"], roll["units"]) == (6800.0, 6861.0, 2.0, 15.0)
    assert adapter.positions == {"MES": {"MESH27": 3}} and any(event["type"] == "order_file" and event["orders"] == ["o1"] for event in events)
    assert read_orders(next(event for event in events if event["type"] == "order_file")["path"])[0]["contract"] == "MESH27"


# --- book, planner, config --------------------------------------------------------------------------------------------

def futures_config(**futures: Any):
    sleeve = lambda name, instrument: {"id": name, "instrument": instrument, "interval": "1d", "strategy": "moving_average_crossover",  # noqa: E731
                                       "params": {"short_window": 4, "long_window": 48}, "sizing": "vol_target", "sizing_params": {"target_annual_vol": 0.10}}
    return parse_portfolio_config({
        "portfolio": {"name": "fut", "base_currency": "NOK", "initial_equity": 10_000_000, "scale": 2.0, "rebalance_band": 0.0},
        "risk": {"max_gross_exposure": 4.0, "max_net_exposure": 3.0, "stale_after_bars": 4},
        "instruments": {MES: {"kind": "future", "max_leverage": 5.0}, M6E: {"kind": "future", "max_leverage": 5.0}},
        "sleeves": [sleeve("mes", MES), sleeve("m6e", M6E)], **({"futures": futures} if futures else {})})


def test_config_takes_contract_size_from_the_table_and_checks_the_futures_section() -> None:
    config = futures_config(mode="file", max_contracts={MES: 20}, no_trade=[M6E], slots=[{"name": "late", "at": "17:00"}])
    assert config.instruments[MES].lot_step == 5.0 and config.instruments[M6E].min_order_size == 12500.0 and config.instruments[MES].can_short
    assert config.futures.mode == "file" and config.futures.max_contracts == {MES: 20} and config.futures.no_trade == (M6E,)
    with pytest.raises(PortfolioConfigError) as failure:
        parse_portfolio_config({"portfolio": {"name": "bad"}, "instruments": {"cme_usd:NOPE": {"kind": "future"}, MES: {"kind": "future", "lot_step": 1.0},
                                                                                 "cme_usd:BTC/USD": {"kind": "perp"}, M6E: {"kind": "future"}},
                                "sleeves": [{"id": "s", "instrument": M6E, "interval": "1d", "strategy": "moving_average_crossover"}],
                                "futures": {"mode": "live", "slots": [{"name": "x", "at": "25:00"}], "max_contracts": {"cme_usd:ZZZ": 1}, "no_trade": ["nope"], "typo": 1}})
    text = str(failure.value)
    for expected in ("not in the futures contract table", "come from the contract table", "mixes futures with other instruments", "mode must be", "slot", "max_contracts", "no_trade", "unknown key 'typo'"):
        assert expected in text


def test_a_futures_book_keeps_its_capital_in_the_base_currency() -> None:
    book = PortfolioBook.from_config(futures_config(), fx={"USD": 10.0})
    assert book.cash == {"cme_usd": Decimal(0), CASH_VENUE: Decimal(10_000_000)} and book.equity() == Decimal(10_000_000)
    book.apply_fill(MES, "buy", 15, 6800)
    book.mark({MES: 6900.0}, fx={"USD": 11.0})
    assert book.equity() == Decimal(10_000_000) + Decimal(15 * 100) * Decimal(11)  # only the profit is in dollars
    assert book.weights()[MES] == pytest.approx(15 * 6900 * 11 / float(book.equity()))


def test_book_roll_realises_the_old_contract_and_continues_at_the_new_price() -> None:
    book = PortfolioBook.from_config(futures_config(), fx={"USD": 10.0})
    book.apply_fill(MES, "sell", 10, 6800)
    book.mark({MES: 6790.0})
    book.set_sleeve_targets({"mes": (MES, -1.0)})
    before = book.equity()
    realized = book.roll(MES, 6790.0, 6850.0, fee=2)
    assert realized == Decimal(100) and book.positions[MES].avg_entry == Decimal(6850) and book.positions[MES].units == Decimal(-10)
    assert book.equity() == before - Decimal(20)  # the price gap between contract months is not a gain or a loss; the fee is, in NOK
    book.mark({MES: 6840.0})
    assert book.equity() == before - Decimal(20) + Decimal(10 * 10 * 10)  # short 10 units, the new contract fell 10 points, 10 NOK per dollar
    with pytest.raises(ValueError):
        book.roll(MES, 0, 1)


def test_planner_sizes_a_dollar_contract_in_a_nok_book() -> None:
    config = futures_config()
    plan = plan_orders({}, {MES: 0.20}, prices={MES: 6800.0}, equity=10_000_000.0, instruments=config.instruments, value_rates={MES: 10.0})
    order = plan.orders[0]
    assert order.units == Decimal(25) and order.price == 6800.0  # 2,000,000 NOK / (6,800 x 10 NOK per unit) = 29.4 units -> 5 contracts
    held = plan_orders({MES: Decimal(25)}, {MES: 0.18}, prices={MES: 6800.0}, equity=10_000_000.0, instruments=config.instruments, value_rates={MES: 10.0}, band=0.02)
    assert not held.orders and held.skipped[0].reason == "within_band"


# --- the feed ---------------------------------------------------------------------------------------------------------

def frame(days: pd.DatetimeIndex, closes: Any, volume: float = 1000.0) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": closes, "high": closes * 1.002, "low": closes * 0.998, "close": closes, "volume": volume}, index=pd.DatetimeIndex(days, name="day"))


def test_splice_joins_without_a_jump_and_ends_on_the_held_contract() -> None:
    days = pd.bdate_range("2026-06-01", periods=60)
    front = frame(days, np.linspace(100.0, 110.0, 60))
    held = frame(days[-30:], np.linspace(100.0, 110.0, 60)[-30:] * 1.01)  # the next contract month trades 1% higher
    series = splice(front, held, days=20)
    assert len(series) == 60 and series["close"].iloc[-1] == held["close"].iloc[-1]
    assert np.allclose(series["close"].pct_change().dropna(), front["close"].pct_change().dropna())  # same returns, no jump at the join
    assert splice(front, held.iloc[0:0]).equals(front) and splice(front.iloc[0:0], held).equals(held)


def test_completed_bars_and_holiday_fill() -> None:
    days = pd.bdate_range("2026-11-23", "2026-11-27")
    bars = frame(days, [1, 2, 3, 4, 5])
    seen = completed(bars, MARKETS[MES], datetime(2026, 11, 25, 22, 0, tzinfo=UTC))  # 17:00 New York on the 25th: not yet half an hour past
    assert list(seen.index.day) == [23, 24]
    assert list(completed(bars, MARKETS[MES], datetime(2026, 11, 25, 22, 31, tzinfo=UTC)).index.day) == [23, 24, 25]
    us = bars.drop(index=pd.Timestamp("2026-11-26"))  # Thanksgiving: no US bar
    filled = fill_holidays({"us": us, "other": bars}, {"us": MARKETS[MES], "other": MARKETS[M6E]}, SESSIONS)
    assert list(filled["us"].index.day) == [23, 24, 25, 26, 27] and filled["us"].loc["2026-11-26", "close"] == 3.0 and filled["us"].loc["2026-11-26", "volume"] == 0.0
    missing = fill_holidays({"us": bars.drop(index=pd.Timestamp("2026-11-24")), "other": bars}, {"us": MARKETS[MES], "other": MARKETS[M6E]}, SESSIONS)
    assert pd.Timestamp("2026-11-24") not in missing["us"].index  # an open day without a bar stays missing


def test_feed_serves_the_held_contract_and_refetches_when_it_changes(tmp_path) -> None:
    config = futures_config()
    days = pd.bdate_range("2026-07-01", "2026-10-09")
    base = np.linspace(6000.0, 6800.0, len(days))
    data = {"MES=F": frame(days, base), "MESZ26.CME": frame(days[-40:], base[-40:]), "MESH27.CME": frame(days[-40:], base[-40:] * 1.01),
            "M6E=F": frame(days, base / 6000.0), "M6EZ26.CME": frame(days[-40:], base[-40:] / 6000.0)}
    calls: list[str] = []

    async def fetch(_session: Any, symbol: str):
        calls.append(symbol)
        if symbol == "USDNOK=X":
            return frame(days[-1:], [10.5]), {"price": 10.5, "time": None}
        return data[symbol], {"price": float(data[symbol]["close"].iloc[-1]), "time": None}

    now = [datetime(2026, 10, 9, 23, 0, tzinfo=UTC)]
    parts = wiring.build(config, tmp_path, fetch=fetch, clock=lambda: now[0])
    assert asyncio.run(parts.feed.rates()) == {"USD": 10.5}
    result = asyncio.run(parts.feed.fetch([(MES, "1d"), (M6E, "1d"), (MES, "4h")]))
    bars = result.bars[(MES, "1d")]
    assert bars[-1].timestamp == datetime(2026, 10, 9, tzinfo=UTC) and bars[-1].close == base[-1] and bars[-1].symbol == "MES" and len(bars) == len(days)
    assert result.fx == {"USD": 10.5} and result.failed == {(MES, "4h"): "the futures feed serves daily bars of markets in the contract table"}
    count = len(calls)
    now[0] += timedelta(minutes=5)
    asyncio.run(parts.feed.fetch([(MES, "1d"), (M6E, "1d")]))
    assert len(calls) == count  # cached
    parts.board.advance(MES, fc.Contract(MARKETS[MES], 2027, 3))
    rolled = asyncio.run(parts.feed.fetch([(MES, "1d"), (M6E, "1d")]))
    assert len(calls) > count and rolled.bars[(MES, "1d")][-1].close == pytest.approx(base[-1] * 1.01)  # the new contract's price at once
    early = asyncio.run(parts.feed.fetch([(MES, "1d")], now=datetime(2026, 10, 9, 15, 0, tzinfo=UTC)))
    assert early.bars[(MES, "1d")][-1].timestamp == datetime(2026, 10, 8, tzinfo=UTC)  # the day in progress is not a bar yet


# --- the engine -------------------------------------------------------------------------------------------------------

DAYS = pd.bdate_range("2026-05-01", "2026-12-18")
TREND = {MES: 6000.0 * np.exp(np.linspace(0.0, 0.25, len(DAYS)) + 0.004 * np.sin(np.arange(len(DAYS)))),
         M6E: 1.10 * np.exp(np.linspace(0.0, 0.08, len(DAYS)) + 0.002 * np.cos(np.arange(len(DAYS))))}


def bars_through(day: str) -> dict[tuple[str, str], list[OHLCVBar]]:
    out = {}
    for instrument, closes in TREND.items():
        out[(instrument, "1d")] = [OHLCVBar(exchange="cme_usd", symbol=instrument.split(":")[1], interval_seconds=86400, timestamp=stamp.to_pydatetime().replace(tzinfo=UTC),
                                             open=float(close), high=float(close) * 1.003, low=float(close) * 0.997, close=float(close), volume=1000.0)
                                    for stamp, close in zip(DAYS, closes) if stamp <= pd.Timestamp(day)]
    return out


def price_on(instrument: str, day: str) -> float:
    return float(TREND[instrument][DAYS.get_loc(pd.Timestamp(day))])


def test_engine_trades_futures_in_whole_contracts_sized_in_nok_and_books_a_roll(tmp_path) -> None:
    config = futures_config()
    now = [EVENING]

    def quotes(symbol: str) -> tuple[float, datetime]:
        instrument = MES if symbol.startswith("MES") else M6E
        day = min(pd.Timestamp(now[0].date()), DAYS[-1])
        price = float(TREND[instrument][DAYS.searchsorted(day, side="right") - 1])
        return (price * 1.01 if symbol.endswith("H27.CME") else price), now[0]  # the March contracts trade 1% higher

    parts = wiring.build(config, tmp_path, prices=quotes)
    book = PortfolioBook.from_config(config, fx={"USD": 10.0})
    engine = PortfolioEngine(config, adapters=build_paper_adapters(config, book, state_dir=tmp_path, futures_ledger=parts.ledger), book=book, state_path=tmp_path / "engine.json")
    fx = {"USD": 10.0}

    decided = engine.run_cycle(bars_through("2026-10-08"), now=EVENING, fx=fx)
    assert decided.decided and len(decided.orders) == 2 and not decided.fills and not decided.rejected and len(engine.pending_orders) == 2
    assert not engine.run_cycle(bars_through("2026-10-08"), now=SLOT - timedelta(hours=2), fx=fx).fills  # before the slot

    now[0] = SLOT
    engine.run_cycle(bars_through("2026-10-08"), now=SLOT, fx=fx)  # released and filled in the ledger
    now[0] = SLOT + timedelta(minutes=5)
    settled = engine.run_cycle(bars_through("2026-10-08"), now=now[0], fx=fx)
    assert len(settled.fills) == 2 and not engine.pending_orders and not settled.mismatches
    units = engine.book.units()
    assert units[MES] % 5 == 0 and units[M6E] % 12500 == 0 and units[MES] > 0 and units[M6E] > 0  # whole contracts, long both trends
    weights = engine.book.weights()
    for instrument in (MES, M6E):
        one_contract = float(config.instruments[instrument].lot_step) * price_on(instrument, "2026-10-08") * 10.0 / 10_000_000
        assert 0 <= decided.adjusted[instrument] - weights[instrument] < one_contract + 0.01  # rounded down by less than one contract, in NOK
    fill = next(item for item in settled.fills if item["instrument"] == MES)
    assert fill["price"] == pytest.approx(price_on(MES, "2026-10-09") + 0.25, abs=0.25)  # the day's price plus one tick, on the tick grid
    assert engine.book.cash[CASH_VENUE] == Decimal(10_000_000)

    # A restart in between changes nothing
    engine = PortfolioEngine(config, adapters=build_paper_adapters(config, book, state_dir=tmp_path, futures_ledger=wiring.build(config, tmp_path, prices=quotes).ledger),
                             book=PortfolioBook.from_config(config, fx=fx), state_path=tmp_path / "engine.json")
    assert engine.restored and engine.book.units() == units and not engine.reconcile()

    # Roll day of the December contracts: MES on 2026-12-15, M6E (delivered, five sessions early) on 2026-12-07
    now[0] = datetime(2026, 12, 4, 22, 45, tzinfo=UTC)
    engine.run_cycle(bars_through("2026-12-04"), now=now[0], fx=fx)
    now[0] = datetime(2026, 12, 7, 15, 0, tzinfo=UTC)
    held = engine.book.units()[M6E]
    equity_before = engine.book.equity()
    rolled = engine.run_cycle(bars_through("2026-12-04"), now=now[0], fx=fx)
    roll = next(event for event in rolled.adapter_events if event["type"] == "roll")
    assert (roll["instrument"], roll["from_contract"], roll["to_contract"]) == (M6E, "M6EZ26", "M6EH27") and roll["units"] == float(held)
    assert float(engine.book.positions[M6E].avg_entry) == pytest.approx(roll["to_price"]) and roll["to_price"] == pytest.approx(roll["from_price"] * 1.01, rel=1e-3)
    assert engine.last_grid_close[M6E] == pytest.approx(price_on(M6E, "2026-12-04") * roll["to_price"] / roll["from_price"])
    # The 1% gap between contract months is not profit: equity moved only by the day's price change and two ticks of cost
    drift = float(held) * (roll["from_price"] - price_on(M6E, "2026-12-04")) * 10.0
    assert float(engine.book.equity() - equity_before) == pytest.approx(drift, abs=float(held) / 12500 * 1.25 * 10.0 * 2 + 1.0)
    # The orders decided on the 4th went out in the same slot, after the roll, into the new contract
    now[0] += timedelta(minutes=5)
    after = engine.run_cycle(bars_through("2026-12-04"), now=now[0], fx=fx)
    assert not rolled.mismatches and not after.mismatches and not engine.pending_orders
    assert engine.adapters["cme_usd"].positions["M6E"] == {"M6EH27": int(engine.book.units()[M6E] / 12500)}
