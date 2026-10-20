"""Multi-asset research series: Treasury and crude contract rules, total-return ETF bars, FX carry."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantlab.calendars import get_calendar
from quantlab.data.adapters.public import parse_yahoo_chart
from quantlab.data.futures import Contract, last_trade_day
from quantlab.data.instruments import ContractSpec, Instrument, InstrumentMaster
from quantlab.data.panel import align
from quantlab.engine.returns import excess_returns


def test_treasury_and_crude_last_days() -> None:
    weekdays = get_calendar("weekdays")
    treasury = ContractSpec(root="ZN", months="HMUZ", last_trade_rule="first_notice_prior_month", settlement="physical")
    crude = ContractSpec(root="MCL", months="FGHJKMNQUVXZ", last_trade_rule="crude_oil", roll_sessions_before=5)
    assert last_trade_day(Contract(treasury, 2026, 12), weekdays) == date(2026, 11, 30)  # first notice: the last session of November
    assert last_trade_day(Contract(treasury, 2027, 3), weekdays) == date(2027, 2, 26)
    assert last_trade_day(Contract(crude, 2026, 12), weekdays) == date(2026, 11, 20)  # three sessions before Wednesday the 25th
    assert last_trade_day(Contract(crude, 2026, 11), weekdays) == date(2026, 10, 20)  # the 25th a Sunday: four sessions before it


def test_total_return_bars_include_distributions() -> None:
    stamps = [1_700_000_000 + 86_400 * i for i in range(3)]
    payload = {"chart": {"result": [{"meta": {"gmtoffset": 0}, "timestamp": stamps,
                                     "indicators": {"quote": [{"open": [100.0, 100.0, 99.0], "high": [101.0, 101.0, 100.0], "low": [99.0, 99.0, 98.0],
                                                               "close": [100.0, 100.0, 99.0], "volume": [1.0, 1.0, 1.0]}],
                                                    "adjclose": [{"adjclose": [99.0, 99.0, 99.0]}]}}]}}
    price = parse_yahoo_chart(payload, interval="1d")
    total = parse_yahoo_chart(payload, interval="1d", total_return=True)
    assert np.isclose(price["close"].iloc[-1] / price["close"].iloc[0] - 1, -0.01)  # a 1-point distribution looks like a loss in prices
    assert np.isclose(total["close"].iloc[-1] / total["close"].iloc[0], 1.0) and np.isclose(total["high"].iloc[0], 101.0 * 0.99)


def test_a_fixing_series_is_usable_whole_and_an_empty_instrument_is_an_error(tmp_path) -> None:
    from quantlab.data.quality import data_card

    index = pd.bdate_range("2010-01-01", "2012-12-31")
    fixing = pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 0.0}, index=index)
    assert data_card(fixing, name="fixing", calendar=get_calendar("weekdays")).usable_from == index[0]


def test_fx_spot_earns_its_interest_differential_and_etfs_pay_for_cash() -> None:
    index = pd.bdate_range("2024-01-01", periods=5)
    flat = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=index)
    master = InstrumentMaster({
        "fxusd:AUD": Instrument(id="fxusd:AUD", name="AUD", asset_class="fx", kind="spot", currency="USD", rate_area="USA", carry_area="AUS"),
        "etf:IEF": Instrument(id="etf:IEF", name="IEF", asset_class="rates", kind="etf", currency="USD", rate_area="USA"),
        "fx:AUDNOK": Instrument(id="fx:AUDNOK", name="AUD in NOK", asset_class="fx", kind="fx_rate", currency="NOK"),
    })
    panel = align({name: flat for name in ("fxusd:AUD", "etf:IEF", "fx:AUDNOK")})
    rates = {"USA": pd.Series(0.05, index=index), "AUS": pd.Series(0.03, index=index)}
    returns = excess_returns(panel, master, rates=rates, periods=250.0)
    assert np.isclose(returns["fxusd:AUD"].iloc[-1], (0.03 - 0.05) / 250.0)
    assert np.isclose(returns["etf:IEF"].iloc[-1], -0.05 / 250.0)
    assert returns["fx:AUDNOK"].iloc[-1] == 0.0
