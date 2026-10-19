"""The data layer (quantlab.data, quantlab.calendars): known answers, no network."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quantlab.calendars import AlwaysCalendar, WeekdayCalendar, get_calendar, shift_sessions
from quantlab.config import ConfigError
from quantlab.data import DataCatalog, InstrumentMaster, align, asof_daily, data_card, load_panel, universe
from quantlab.data import adapters as registry
from quantlab.data.adapters.files import FileAdapter
from quantlab.data.adapters.public import YahooAdapter, parse_norges_bank, parse_oecd_rates, parse_yahoo_chart
from quantlab.data.bars import BarError, available_at, from_open_stamped, to_daily, validate_bars
from quantlab.data.futures import Contract, adjusted_series, held_contract, held_returns, last_trade_day, roll_schedule
from quantlab.data.instruments import ContractSpec

XNYS = get_calendar("XNYS")


def _bars(days: pd.DatetimeIndex, close: np.ndarray | None = None) -> pd.DataFrame:
    close = close if close is not None else 100.0 + np.arange(len(days), dtype=float)
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0}, index=days)
    frame.index.name = "time"
    return frame


# --- calendars -----------------------------------------------------------------------------------------------------

def test_calendars() -> None:
    assert XNYS.exact and not XNYS.is_session(date(2001, 9, 12)) and not XNYS.is_session(date(1990, 12, 25))
    assert len(XNYS.sessions("1999-01-01", "1999-12-31")) == 252
    assert AlwaysCalendar().is_session(date(2026, 10, 18)) and not WeekdayCalendar().is_session(date(2026, 10, 18))
    assert shift_sessions(XNYS, date(2026, 12, 18), -3) == date(2026, 12, 15)


# --- instruments -----------------------------------------------------------------------------------------------------

MASTER = """
[instruments."idx:NDX"]
name = "Nasdaq-100 index"
asset_class = "equity_index"
kind = "index"
currency = "USD"
calendar = "XNYS"
timezone = "America/New_York"
carry_yield = 0.009
rate_area = "USA"
proxy_for = "cme:NQ"
sources = { "1d" = "test:NDX" }

[instruments."cme:NQ"]
name = "E-mini Nasdaq-100"
asset_class = "equity_index"
kind = "future"
currency = "USD"
calendar = "XNYS"
multiplier = 20.0
tick_size = 0.25
contract = { root = "NQ", months = "HMUZ", last_trade_rule = "third_friday", roll_sessions_before = 3 }
"""


def test_the_instrument_master_parses_strictly(tmp_path) -> None:
    path = tmp_path / "instruments.toml"
    path.write_text(MASTER)
    master = InstrumentMaster.load(path)
    assert len(master) == 2 and master["idx:NDX"].is_proxy and master["cme:NQ"].contract.root == "NQ"
    assert master["idx:NDX"].source("1d") == ("test", "NDX") and [item.id for item in master.select(kind="future")] == ["cme:NQ"]
    path.write_text(MASTER.replace('kind = "future"', 'kind = "forward"').replace("multiplier = 20.0", "multiplier = -1.0"))
    with pytest.raises(ConfigError) as error:
        InstrumentMaster.load(path)
    assert "kind: must be one of" in str(error.value)
    bad = tmp_path / "bad.toml"
    bad.write_text(MASTER.replace('proxy_for = "cme:NQ"', 'proxy_for = "cme:XX"'))
    with pytest.raises(ConfigError, match="unknown instruments"):
        InstrumentMaster.load(bad)


# --- bars --------------------------------------------------------------------------------------------------------------

def test_the_bar_schema_and_conversions() -> None:
    days = pd.bdate_range("2026-01-05", periods=5)
    validate_bars(_bars(days), interval="1d")
    with pytest.raises(BarError, match="naive session dates"):
        validate_bars(_bars(days.tz_localize("UTC")), interval="1d")
    opens = pd.date_range("2026-01-05 00:00", periods=12, freq="4h", tz="UTC")
    closed = from_open_stamped(_bars(opens), "4h")
    assert closed.index[0] == pd.Timestamp("2026-01-05 04:00", tz="UTC")
    daily = to_daily(closed, timezone="UTC", session_close="00:00")
    assert list(daily.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")] and daily["close"].iloc[0] == closed["close"].iloc[5]
    assert daily["high"].iloc[0] == closed["high"].iloc[:6].max()
    ny = to_daily(pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [1.0, 2.0], "volume": 1.0},
                               index=pd.DatetimeIndex(["2026-01-05 21:00", "2026-01-05 22:00"], tz="UTC")), timezone="America/New_York", session_close="16:00")
    assert list(ny.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]  # 16:00 NY closes the day, 17:00 starts the next
    assert available_at(pd.DatetimeIndex(["2026-01-05"]), interval="1d", timezone="America/New_York")[0] == pd.Timestamp("2026-01-05 21:00", tz="UTC")


# --- adapters ------------------------------------------------------------------------------------------------------

def test_yahoo_parsing_dates_daily_bars_by_the_exchange_day() -> None:
    payload = {"chart": {"result": [{"meta": {"gmtoffset": -14400}, "timestamp": [1760961000, 1761047400, 1761047500],
                                     "indicators": {"quote": [{"open": [1, 2, 2], "high": [2, 3, 3], "low": [0.5, 1, 1], "close": [1.5, None, 2.5], "volume": [10, 20, 20]}]}}]}}
    frame = parse_yahoo_chart(payload, interval="1d")
    assert list(frame.index) == [pd.Timestamp("2025-10-20"), pd.Timestamp("2025-10-21")] and list(frame["close"]) == [1.5, 2.5]
    adapter = YahooAdapter(fetch=lambda url: json.dumps(payload).encode())
    assert len(adapter.fetch("^NDX", interval="1d")) == 2


def test_oecd_rates_are_known_only_from_the_next_month() -> None:
    frame = parse_oecd_rates("TIME_PERIOD,OBS_VALUE,REF_AREA\n2024-01,5.3,USA\n2024-02,5.2,USA\n")
    assert frame["value"].iloc[0] == pytest.approx(0.053) and frame["available_at"].iloc[0] == pd.Timestamp("2024-02-01")
    daily = asof_daily(frame, pd.DatetimeIndex(["2024-01-15", "2024-02-01", "2024-02-20", "2024-03-05"]))
    assert np.isnan(daily.iloc[0]) and daily.iloc[1] == pytest.approx(0.053) and daily.iloc[3] == pytest.approx(0.052)


def test_norges_bank_scales_per_100_quotes() -> None:
    text = "﻿TIME_PERIOD;OBS_VALUE;UNIT_MULT;BASE_CUR\n2024-01-02;10.2;0;USD\n2024-01-02;7.1;2;JPY\n"
    frame = parse_norges_bank(text.replace(";BASE_CUR", ";BASE_CUR").split("\n2024-01-02;7.1")[0] + "\n")
    assert frame["value"].iloc[0] == pytest.approx(10.2)
    jpy = parse_norges_bank("TIME_PERIOD;OBS_VALUE;UNIT_MULT\n2024-01-02;7.1;2\n")
    assert jpy["value"].iloc[0] == pytest.approx(0.071)


def test_the_file_adapter_reads_your_own_exports(tmp_path) -> None:
    (tmp_path / "bbg").mkdir()
    pd.DataFrame({"Date": ["2026-01-05", "2026-01-06"], "PX_LAST": [100.0, 101.0], "PX_VOLUME": [5, 6]}).to_csv(tmp_path / "bbg" / "NQ1.csv", index=False)
    frame = FileAdapter(tmp_path).fetch("bbg/NQ1", interval="1d")
    assert list(frame["close"]) == [100.0, 101.0] and frame["high"].iloc[0] == 100.0 and frame["volume"].iloc[1] == 6


# --- the catalog ---------------------------------------------------------------------------------------------------

class FakeAdapter:
    name, version = "test", "1"

    def __init__(self) -> None:
        self.frames: dict[str, pd.DataFrame] = {}
        self.calls = 0

    def fetch(self, symbol: str, *, interval: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        self.calls += 1
        return self.frames[symbol]


@pytest.fixture()
def fake() -> FakeAdapter:
    adapter = FakeAdapter()
    registry.register(adapter)
    return adapter


def test_the_catalog_versions_snapshots_and_cuts_the_holdout(tmp_path, fake) -> None:
    days = pd.bdate_range("2025-12-26", "2026-01-09")
    fake.frames["NDX"] = _bars(days)
    catalog = DataCatalog(tmp_path, frozen_from="2026-01-01")
    frame, first = catalog.get("test", "NDX")
    assert frame.index.max() < pd.Timestamp("2026-01-01") and fake.calls == 1
    again, same = catalog.get("test", "NDX")
    assert fake.calls == 1 and same.id == first.id  # cached
    fake.frames["NDX"] = _bars(days, close=np.arange(len(days), dtype=float) + 200)
    _frame, revised = catalog.get("test", "NDX", refresh=True)
    assert revised.fingerprint != first.fingerprint and len(catalog.snapshots("test", "NDX", "1d")) == 2
    old, _ = catalog.get("test", "NDX", snapshot=first.id)
    assert old["close"].iloc[0] == 100.0  # the exact earlier version, reproducible
    catalog.unlock("test look")
    unlocked, _ = catalog.get("test", "NDX")
    assert unlocked.index.max() >= pd.Timestamp("2026-01-01") and (tmp_path / "unlocks.jsonl").exists()


# --- quality ---------------------------------------------------------------------------------------------------------

def test_the_data_card() -> None:
    days = XNYS.sessions("1997-01-01", "1999-12-31")
    bars = _bars(days)
    early = bars.index.year == 1997
    bars.loc[early, ["open", "high", "low"]] = bars.loc[early, "close"].to_numpy()[:, None]  # close-only history
    bars.loc[bars.index[400:406], "close"] = 5000.0  # a stale run, entered by an outlier
    bars.loc[bars.index[400:406], ["open", "high", "low"]] = 5000.0
    bars = bars.drop(bars.index[600])  # a missing session
    card = data_card(bars, name="x", calendar=XNYS)
    assert card.usable_from == pd.Timestamp("1998-01-01") and card.ok
    assert card.flagged["stale runs"] and card.flagged["outliers"] and len(card.flagged["missing sessions"]) == 1
    broken = bars.copy()
    broken.iloc[700, broken.columns.get_loc("high")] = 0.0
    assert not data_card(broken, calendar=XNYS).ok


# --- futures ---------------------------------------------------------------------------------------------------------

SPEC = ContractSpec(root="NQ", months="HMUZ", last_trade_rule="third_friday", roll_sessions_before=3)


def test_contract_rules_match_the_exchange() -> None:
    december = Contract(SPEC, 2026, 12)
    assert december.code == "NQZ26" and december.following().code == "NQH27"
    assert last_trade_day(december, XNYS) == date(2026, 12, 18)
    assert held_contract(SPEC, date(2026, 12, 14), XNYS).code == "NQZ26"
    assert held_contract(SPEC, date(2026, 12, 15), XNYS).code == "NQH27"  # the roll day's close moves to March


def test_held_returns_never_book_the_roll_gap_and_adjusted_series_remove_it() -> None:
    schedule = roll_schedule(SPEC, XNYS, "2026-11-02", "2027-01-29")
    assert schedule["roll"].sum() == 1 and schedule.loc["2026-12-15", "roll"]
    index = schedule.index
    trend = 1.0005 ** np.arange(len(index))
    closes = {"NQZ26": pd.Series(20000.0 * trend, index=index), "NQH27": pd.Series(20300.0 * trend, index=index)}  # March 1.5% above December
    pnl = held_returns(closes, schedule, cost_bps=1.0)
    assert np.allclose(pnl["return"].dropna(), 0.0005)  # every day, the roll included, earns the same as both contracts
    assert pnl.loc["2026-12-15", "roll_cost"] == pytest.approx(0.0002) and pnl["roll_cost"].sum() == pytest.approx(0.0002)
    ratio = adjusted_series(closes, schedule, method="ratio")
    assert np.allclose(ratio.pct_change().dropna(), 0.0005) and ratio.iloc[-1] == pytest.approx(closes["NQH27"].iloc[-1])
    difference = adjusted_series(closes, schedule, method="difference")
    assert difference.iloc[-1] == pytest.approx(closes["NQH27"].iloc[-1]) and difference.diff().abs().max() < 20


# --- panels ----------------------------------------------------------------------------------------------------------

def test_panels_align_with_stale_flags_and_universes(tmp_path, fake) -> None:
    us = XNYS.sessions("2024-01-01", "2025-06-30")
    eu = pd.bdate_range("2024-01-01", "2025-06-30")
    fake.frames["NDX"], fake.frames["DAX"] = _bars(us), _bars(eu, close=np.linspace(100, 120, len(eu)))
    path = tmp_path / "instruments.toml"
    path.write_text(MASTER + """
[instruments."idx:DAX"]
name = "DAX"
asset_class = "equity_index"
kind = "index"
currency = "EUR"
calendar = "weekdays"
sources = { "1d" = "test:DAX" }
""")
    panel = load_panel(InstrumentMaster.load(path), ["idx:NDX", "idx:DAX"], catalog=DataCatalog(tmp_path / "data"))
    july4 = pd.Timestamp("2024-07-04")
    assert panel.stale.loc[july4, "idx:NDX"] and not panel.stale.loc[july4, "idx:DAX"]
    assert panel["close"].loc[july4, "idx:NDX"] == panel["close"].loc[pd.Timestamp("2024-07-03"), "idx:NDX"]
    assert set(panel.snapshots) == {"idx:NDX", "idx:DAX"} and panel.cards["idx:NDX"].ok
    members = universe(panel, min_history=100)
    assert not members.iloc[50].any() and members.iloc[200]["idx:DAX"] and not members.loc[july4, "idx:NDX"]


def test_appending_data_does_not_change_the_past(tmp_path, fake) -> None:
    days = XNYS.sessions("2020-01-01", "2024-12-31")
    fake.frames["NDX"] = _bars(days)
    short = align({"a": _bars(days[:800])})
    full = align({"a": _bars(days)})
    pd.testing.assert_frame_equal(full["close"].iloc[:800], short["close"])
