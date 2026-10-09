"""Futures contracts, sessions, liquidity rules, the daily store and the order file."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pandas as pd
import pytest

from src.futures import contracts as fc
from src.futures import order_file, quotes
from src.futures.liquidity import LiquidityRules, allowed_contracts, average_volume
from src.futures.sessions import Sessions, Slot, DEFAULT_SLOTS, is_liquid, liquid_window, parse_slots, release_time


@pytest.fixture(scope="module")
def sessions() -> Sessions:
    return Sessions()


@pytest.fixture(scope="module")
def markets() -> dict[str, fc.Market]:
    return fc.load_markets()


def test_table_loads_and_is_consistent(markets: dict[str, fc.Market]) -> None:
    assert len(markets) == 14 and markets["cme_usd:MES"].multiplier == Decimal(5) and markets["cme_usd:MES"].venue == "cme_usd"
    assert markets["cme_jpy:MNI"].currency == "JPY" and markets["eurex_eur:FDXS"].quote_exchange == ""
    assert all(market.multiplier * market.tick_size == market.tick_value for market in markets.values())


def test_table_rejects_a_contradiction(tmp_path) -> None:
    text = fc.MARKETS_PATH.read_text().replace(",5.0,0.25,1.25,USD", ",5.0,0.25,12.5,USD", 1)
    assert text != fc.MARKETS_PATH.read_text()
    (tmp_path / "bad.csv").write_text(text)
    with pytest.raises(ValueError, match="tick value"):
        fc.load_markets(tmp_path / "bad.csv")


def test_contract_codes_and_months(markets: dict[str, fc.Market]) -> None:
    december = fc.Contract(markets["cme_usd:MES"], 2026, 12)
    assert december.code == "MESZ26" and december.quote_symbol == "MESZ26.CME"
    assert december.following().code == "MESH27" and december.following().following().code == "MESM27"
    assert fc.Contract(markets["eurex_eur:FDXS"], 2026, 12).quote_symbol is None
    assert fc.parse_code(markets["cme_usd:M6E"], "M6EH27", near=date(2026, 10, 9)) == fc.Contract(markets["cme_usd:M6E"], 2027, 3)
    with pytest.raises(ValueError):
        fc.parse_code(markets["cme_usd:MES"], "M6EH27", near=date(2026, 10, 9))


def test_last_trading_days(markets: dict[str, fc.Market], sessions: Sessions) -> None:
    assert fc.last_trade_day(fc.Contract(markets["cme_usd:MES"], 2026, 12), sessions) == date(2026, 12, 18)  # third Friday
    assert fc.last_trade_day(fc.Contract(markets["cme_usd:M6E"], 2026, 12), sessions) == date(2026, 12, 14)  # two sessions before Wednesday the 16th
    assert fc.last_trade_day(fc.Contract(markets["cme_usd:MCD"], 2026, 12), sessions) == date(2026, 12, 15)
    assert fc.last_trade_day(fc.Contract(markets["cme_jpy:MNI"], 2026, 12), sessions) == date(2026, 12, 10)  # Thursday before the second Friday
    april = replace(markets["cme_usd:MES"], months="J")
    assert fc.last_trade_day(fc.Contract(april, 2025, 4), sessions) == date(2025, 4, 17)  # the third Friday was Good Friday


def test_roll_day_and_the_contract_to_hold(markets: dict[str, fc.Market], sessions: Sessions) -> None:
    mes, m6e = markets["cme_usd:MES"], markets["cme_usd:M6E"]
    december = fc.Contract(mes, 2026, 12)
    assert fc.roll_day(december, sessions) == date(2026, 12, 15)  # three sessions before the 18th
    assert fc.scheduled_contract(mes, date(2026, 12, 14), sessions) == december
    assert fc.scheduled_contract(mes, date(2026, 12, 15), sessions).code == "MESH27"
    assert fc.scheduled_contract(mes, date(2026, 10, 9), sessions) == december
    assert fc.scheduled_contract(mes, date(2026, 12, 28), sessions).code == "MESH27"
    assert fc.roll_day(fc.Contract(m6e, 2026, 12), sessions) == date(2026, 12, 7)  # a delivered contract leaves five sessions early


def test_volume_moves_the_roll_earlier_and_never_back(markets: dict[str, fc.Market], sessions: Sessions) -> None:
    mes = markets["cme_usd:MES"]
    december, march = fc.Contract(mes, 2026, 12), fc.Contract(mes, 2027, 3)
    day = date(2026, 12, 9)
    assert fc.contract_to_hold(mes, day, sessions, current=december, volumes={"MESZ26": 900_000, "MESH27": 100_000}) == december
    assert fc.contract_to_hold(mes, day, sessions, current=december, volumes={"MESZ26": 300_000, "MESH27": 600_000}) == march
    assert fc.contract_to_hold(mes, day, sessions, current=march, volumes={"MESZ26": 900_000, "MESH27": 100_000}) == march  # never back
    assert fc.contract_to_hold(mes, day, sessions, current=december, volumes={}) == december
    assert fc.contract_to_hold(mes, date(2026, 12, 15), sessions, current=december) == march  # the calendar is the latest roll
    assert fc.contract_to_hold(mes, day, sessions) == december


def test_costs_and_contract_counts(markets: dict[str, fc.Market]) -> None:
    mes = markets["cme_usd:MES"]
    assert mes.contracts_for(15) == Decimal(3) and mes.cost(3) == Decimal("3.75") and mes.cost(-3) == Decimal("3.75")
    assert mes.slipped(6800.10, "buy") == Decimal("6800.25") and mes.slipped(6800.10, "sell") == Decimal("6799.75")


def test_sessions(sessions: Sessions) -> None:
    assert not sessions.is_session("XNYS", date(2026, 11, 26)) and sessions.is_session("XNYS", date(2026, 11, 27))  # Thanksgiving
    assert not sessions.is_session("XNYS", date(2026, 10, 10))  # a Saturday
    assert sessions.is_session("XNYS", date(2060, 7, 5))  # past the library's range: a weekday counts
    assert sessions.shift("XNYS", date(2026, 11, 27), -1) == date(2026, 11, 25) and sessions.shift("XNYS", date(2026, 11, 25), 2) == date(2026, 11, 30)
    assert sessions.on_or_before("XNYS", date(2026, 11, 26)) == date(2026, 11, 25)


def test_liquid_window_follows_local_time_holidays_and_half_days(markets: dict[str, fc.Market], sessions: Sessions) -> None:
    mes = markets["cme_usd:MES"]
    start, end = liquid_window(mes, date(2026, 10, 9), sessions)
    assert (start, end) == (datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc), datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc))  # New York summer time
    assert liquid_window(mes, date(2026, 12, 9), sessions)[0] == datetime(2026, 12, 9, 14, 30, tzinfo=timezone.utc)  # winter time
    assert liquid_window(mes, date(2026, 11, 26), sessions) is None
    assert liquid_window(mes, date(2026, 11, 27), sessions)[1] == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)  # closes at 13:00 New York
    assert is_liquid(mes, datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc), sessions)
    assert not is_liquid(mes, datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc), sessions)
    assert not is_liquid(mes, datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc), sessions)


def test_orders_are_released_at_the_first_slot_inside_the_liquid_window(markets: dict[str, fc.Market], sessions: Sessions) -> None:
    mes, dax = markets["cme_usd:MES"], markets["eurex_eur:FDXS"]
    evening = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)  # Thursday night, after the US close
    when, slot = release_time(dax, evening, DEFAULT_SLOTS, sessions)
    assert (when, slot.name) == (datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc), "europe")  # 10:00 Oslo, summer time
    when, slot = release_time(mes, evening, DEFAULT_SLOTS, sessions)
    assert (when, slot.name) == (datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc), "americas")  # 16:00 Oslo, half an hour after the US open
    friday_night = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
    assert release_time(mes, friday_night, DEFAULT_SLOTS, sessions)[0] == datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)  # over the weekend
    before_thanksgiving = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)
    assert release_time(mes, before_thanksgiving, DEFAULT_SLOTS, sessions)[0] == datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)  # skips the holiday; winter time
    assert release_time(mes, evening, (Slot("night", time(3, 0)),), sessions) is None  # no slot ever reaches US hours
    assert parse_slots([{"name": "late", "at": "17:30"}]) == (Slot("late", time(17, 30)),) and parse_slots(None) == DEFAULT_SLOTS


def test_liquidity_rules() -> None:
    rules = LiquidityRules(max_participation=0.01, min_daily_volume=200, volume_days=3)
    assert average_volume([1, 100, 200, 300], 3) == 200 and average_volume([], 3) is None
    assert allowed_contracts(5, 10_000.0, rules) == (5, "")
    assert allowed_contracts(500, 10_000.0, rules)[0] == 100
    assert allowed_contracts(5, 100.0, rules)[0] == 0 and "floor" in allowed_contracts(5, 100.0, rules)[1]
    assert allowed_contracts(5, 100.0, rules, reducing=True)[0] == 1  # may always get out, a little at a time
    assert allowed_contracts(5, None, rules) == (5, "volume unknown") and allowed_contracts(0, 10.0, rules) == (0, "")
    with pytest.raises(ValueError):
        LiquidityRules(max_participation=0.0)


CHART = {"chart": {"result": [{"meta": {"gmtoffset": -14400, "regularMarketPrice": 6801.5, "regularMarketTime": 1791547200},
                               "timestamp": [1791345600, 1791432000, 1791547200],
                               "indicators": {"quote": [{"open": [6700.0, 6750.0, None], "high": [6760.0, 6810.0, None], "low": [6690.0, 6740.0, None],
                                                         "close": [6750.0, 6800.0, None], "volume": [900000, 950000, None]}]}}]}}


def test_parse_chart_and_store(tmp_path) -> None:
    bars, latest = quotes.parse_chart(CHART)
    assert list(bars["close"]) == [6750.0, 6800.0] and latest["price"] == 6801.5 and latest["time"].tzinfo is not None
    store = quotes.DailyStore(tmp_path)
    assert store.load("MESZ26").empty and store.merge("MESZ26", bars) == 2 and store.merge("MESZ26", bars) == 0
    newer = bars.iloc[[1]].assign(close=6805.0, volume=990000.0)
    later = pd.concat([newer, newer.set_index(newer.index + pd.Timedelta(days=1))])
    assert store.merge("MESZ26", later) == 1
    saved = store.load("MESZ26")
    assert len(saved) == 3 and saved["close"].iloc[1] == 6805.0  # the newer value for a day replaces the old one
    assert store.latest_volumes(["MESZ26", "MESH27"]) == {"MESZ26": 990000.0} and store.volumes("MESZ26")[0] == 900000.0


def test_last_prices_caches_and_never_raises() -> None:
    calls, now = [], [0.0]

    def fetch(url: str):
        calls.append(url)
        if "BAD" in url:
            raise OSError("down")
        return CHART

    prices = quotes.LastPrices(max_age=60, fetch=fetch, clock_=lambda: now[0])
    assert prices("MESZ26.CME")[0] == 6801.5 and prices("MESZ26.CME")[0] == 6801.5 and len(calls) == 1
    now[0] = 61.0
    prices("MESZ26.CME")
    assert len(calls) == 2 and prices("BAD.CME") is None


def test_listed_contracts(markets: dict[str, fc.Market], sessions: Sessions) -> None:
    assert [c.code for c in quotes.listed_contracts(markets["cme_usd:MES"], date(2026, 10, 9), sessions)] == ["MESZ26", "MESH27"]


def test_order_file_round_trip_and_fills(tmp_path) -> None:
    released = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
    order = order_file.FileOrder(order_id="pf-1-0", book="fund", created_utc=released.isoformat(), slot="americas", market="cme_usd:MES", contract="MESZ26",
                                 exchange="CME", side="BUY", quantity=3, order_type="MKT", limit_price="", currency="USD", valid_until_utc="2026-10-09T20:00:00+00:00", reason="rebalance")
    path = order_file.write_orders(tmp_path / order_file.order_file_name("fund", released, "americas"), [order])
    assert path.name == "orders_fund_20261009_1400_americas.csv" and not list(tmp_path.glob("*.tmp"))
    assert order_file.read_orders(path) == [{name: str(getattr(order, name)) for name in order_file.ORDER_COLUMNS}]
    (tmp_path / "fills_1.csv").write_text("order_id,contract,side,quantity,price,fee,filled_utc\npf-1-0,MESZ26,BUY,3,6801.25,1.5,2026-10-09T14:01:00Z\npf-1-1,MESZ26,BUY,x,1,,\n")
    fills = order_file.read_fills(tmp_path)
    assert fills[0] == {"order_id": "pf-1-0", "contract": "MESZ26", "side": "buy", "quantity": 3, "price": 6801.25, "fee": 1.5, "filled_utc": "2026-10-09T14:01:00Z", "file": "fills_1.csv", "line": 2}
    assert "error" in fills[1] and "line 3" in fills[1]["error"]
