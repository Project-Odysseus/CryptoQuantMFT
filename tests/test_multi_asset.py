"""The multi-asset research book (H11): returns above cash, signals, sizing, the trading lag, costs and contracts."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import multi_asset as ma

MARKETS = pd.DataFrame(
    {"sector": ["equity", "equity", "fx", "fx", "fx"], "currency": ["USD", "EUR", "EUR", "JPY", "NOK"],
     "price_symbol": ["^A", "^B", "EUR", "JPY", "NOK"], "rate_area": ["USA", "EA20", "EA20", "JPN", "NOR"],
     "dividend_yield": [0.025, 0.0, np.nan, np.nan, np.nan], "liquid": ["major", "minor", "major", "major", "minor"],
     "contract_size": [5.0, 1.0, 12500.0, 1250000.0, 2000000.0], "contract_currency": ["USD", "EUR", "EUR", "JPY", "NOK"]},
    index=pd.Index(["SPX", "DAX", "EUR", "JPY", "NOK"], name="market"))


def make_raw(days: int = 700, seed: int = 1) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2001-01-01", periods=days)
    walk = lambda vol: np.exp(np.cumsum(rng.normal(0.0, vol, days)))  # noqa: E731
    fx_nok = pd.DataFrame({"USD": 8.0 * walk(0.006), "EUR": 9.0 * walk(0.004), "JPY": 0.07 * walk(0.007)}, index=index)
    equity = pd.DataFrame({"SPX": 1000.0 * walk(0.01), "DAX": 5000.0 * walk(0.012)}, index=index)
    months = pd.period_range("2000-11", periods=days // 20 + 4, freq="M")
    rates = pd.DataFrame({"USA": 3.0, "EA20": 2.0, "JPN": 0.5, "NOR": 5.0}, index=months)
    return {"fx_nok": fx_nok, "rates": rates, "equity": equity}


@pytest.fixture(scope="module")
def panel() -> ma.Panel:
    return ma.build_panel(MARKETS, make_raw())


def test_parse_norges_bank_scales_currencies_quoted_per_hundred() -> None:
    text = ("﻿FREQ;BASE_CUR;QUOTE_CUR;UNIT_MULT;TIME_PERIOD;OBS_VALUE\n"
            "B;USD;NOK;0;2024-01-02;10.20\nB;JPY;NOK;2;2024-01-02;7.20\nB;JPY;NOK;2;2024-01-03;ND\n")
    frame = ma.parse_norges_bank(text)
    assert frame.loc["2024-01-02", "USD"] == pytest.approx(10.20)
    assert frame.loc["2024-01-02", "JPY"] == pytest.approx(0.072)
    assert len(frame) == 1  # the day without a fixing is dropped


def test_parse_oecd_rates_and_yahoo_closes() -> None:
    rates = ma.parse_oecd_rates("REF_AREA,TIME_PERIOD,OBS_VALUE\nUSA,2024-01,5.3\nNOR,2024-01,4.7\nUSA,2024-02,5.2\n")
    assert rates.loc["2024-01", "NOR"] == 4.7 and np.isnan(rates.loc["2024-02", "NOR"])
    payload = {"chart": {"result": [{"meta": {"gmtoffset": 32400}, "timestamp": [1704153600, 1704240000],  # 00:00 UTC = 09:00 Tokyo
                                     "indicators": {"quote": [{"close": [100.0, None]}]}}]}}
    closes = ma.parse_yahoo_closes(payload)
    assert list(closes.index) == [pd.Timestamp("2024-01-02")] and closes.iloc[0] == 100.0


def test_daily_rates_use_last_months_value() -> None:
    monthly = pd.DataFrame({"USA": [1.0, 2.0, 3.0]}, index=pd.period_range("2024-01", periods=3, freq="M"))
    daily = ma.daily_rates(monthly, pd.bdate_range("2024-02-01", "2024-03-29"))
    assert daily.loc["2024-02-15", "USA"] == pytest.approx(0.01)  # February uses January's rate
    assert daily.loc["2024-03-15", "USA"] == pytest.approx(0.02)


def test_returns_above_cash(panel: ma.Panel) -> None:
    raw = make_raw()
    day, before = raw["fx_nok"].index[300], raw["fx_nok"].index[299]
    eur_usd = raw["fx_nok"]["EUR"] / raw["fx_nok"]["USD"]
    assert panel.returns.at[day, "EUR"] == pytest.approx(eur_usd[day] / eur_usd[before] - 1 + (0.02 - 0.03) / 252)
    nok_usd = 1.0 / raw["fx_nok"]["USD"]
    assert panel.returns.at[day, "NOK"] == pytest.approx(nok_usd[day] / nok_usd[before] - 1 + (0.05 - 0.03) / 252)
    assert panel.returns.at[day, "SPX"] == pytest.approx(raw["equity"]["SPX"][day] / raw["equity"]["SPX"][before] - 1 + (0.025 - 0.03) / 252)
    assert panel.returns.at[day, "DAX"] == pytest.approx(raw["equity"]["DAX"][day] / raw["equity"]["DAX"][before] - 1 - 0.02 / 252)


def test_a_day_without_a_price_is_a_zero_move() -> None:
    raw = make_raw()
    hole = raw["equity"].index[300]
    raw["equity"] = raw["equity"].drop(index=hole)
    panel = ma.build_panel(MARKETS, raw)
    assert panel.returns.at[hole, "SPX"] == pytest.approx((0.025 - 0.03) / 252)


def test_rank_weights_are_market_neutral() -> None:
    scores = pd.DataFrame([[0.1, 0.3, -0.2, np.nan], [np.nan] * 4], columns=list("abcd"))
    weights = ma.rank_weights(scores)
    assert weights.iloc[0].sum(skipna=True) == pytest.approx(0.0) and weights.iloc[0].abs().sum() == pytest.approx(1.0)
    assert weights.iloc[0].idxmax() == "b" and weights.iloc[0].idxmin() == "c" and weights.iloc[1].isna().all()


def test_trend_signal_follows_the_direction() -> None:
    up = pd.DataFrame({"x": np.linspace(1.0, 2.0, 400), "y": np.linspace(2.0, 1.0, 400)})
    signal = ma.trend_signal(up)
    assert signal["x"].iloc[-1] == 1.0 and signal["y"].iloc[-1] == -1.0 and signal["x"].iloc[100:200].isna().all()


def test_scale_to_vol_hits_the_target() -> None:
    covariance = np.array([[0.04, 0.01], [0.01, 0.09]])
    scaled = ma.scale_to_vol(np.array([1.0, -0.5]), covariance)
    assert np.sqrt(scaled @ covariance @ scaled) == pytest.approx(0.10)
    assert np.array_equal(ma.scale_to_vol(np.zeros(2), covariance), np.zeros(2))


def test_rebalance_days_are_the_last_trading_day_of_each_week() -> None:
    days = pd.bdate_range("2024-01-01", "2024-01-19").drop(pd.Timestamp("2024-01-12"))
    assert list(ma.rebalance_days(days)) == [pd.Timestamp("2024-01-05"), pd.Timestamp("2024-01-11"), pd.Timestamp("2024-01-19")]


def test_positions_never_use_later_data(panel: ma.Panel) -> None:
    positions = ma.book_positions(panel, ma.raw_sleeves(panel))["book"]
    cut = positions.index[len(positions) // 2]
    days = panel.returns.index[panel.returns.index <= cut]
    early = ma.book_positions(ma.slice_panel(panel, days), ma.raw_sleeves(ma.slice_panel(panel, days)))["book"]
    pd.testing.assert_frame_equal(early, positions.loc[:cut])


def test_positions_earn_from_two_days_after_the_decision(panel: ma.Panel) -> None:
    decided = ma.rebalance_days(panel.returns.index)[60]
    positions = pd.DataFrame([[1.0, 0.0, 0.0, 0.0, 0.0]], index=[decided], columns=panel.returns.columns)
    result = ma.performance(panel, positions)
    where = panel.returns.index.get_loc(decided)
    first = panel.returns.index[where + 2]
    assert result.held.loc[panel.returns.index[where + 1], "SPX"] == 0.0 and result.held.at[first, "SPX"] == 1.0
    assert result.gross[first] == pytest.approx(panel.returns.at[first, "SPX"])
    roll = 4 * 1.0 / 1e4 / 252
    assert result.net[first] == pytest.approx(result.gross[first] - 1.5 / 1e4 - roll)  # one side of a major market, plus the roll
    later = panel.returns.index[where + 3]
    assert result.net[later] == pytest.approx(result.gross[later] - roll)
    doubled = ma.performance(panel, positions, cost_multiplier=2.0)
    assert doubled.net[first] == pytest.approx(doubled.gross[first] - 2 * (1.5 / 1e4 + roll))


def test_book_and_sleeves_run_at_the_target_volatility(panel: ma.Panel) -> None:
    result = ma.run_book(panel)
    assert set(result.results) == {"fx_trend", "fx_carry", "fx_momentum", "equity_trend", "equity_momentum", "fx", "equity", "book"}
    assert result.positions["fx_carry"][["SPX", "DAX"]].abs().to_numpy().sum() == 0.0
    assert result.positions["equity_trend"][["EUR", "JPY", "NOK"]].abs().to_numpy().sum() == 0.0
    day = result.positions["book"].index[-1]
    end = panel.returns.index.get_loc(day) + 1
    covariance = np.cov(panel.returns.fillna(0.0).iloc[end - ma.COV_WINDOW:end].to_numpy(), rowvar=False) * 252
    book = result.positions["book"].loc[day].to_numpy()
    assert np.sqrt(book @ covariance @ book) == pytest.approx(0.10)
    carry = result.positions["fx_carry"].loc[day]
    assert carry["NOK"] > 0 > carry["JPY"]  # long the highest rate, short the lowest


def test_whole_contracts(panel: ma.Panel) -> None:
    day = ma.rebalance_days(panel.returns.index)[80]
    notional = ma.contract_notional_nok(panel).loc[day]
    assert notional["SPX"] == pytest.approx(5.0 * panel.prices.at[day, "SPX"] * panel.nok_per.at[day, "SPX"])
    assert notional["EUR"] == pytest.approx(12500.0 * panel.nok_per.at[day, "EUR"]) and notional["NOK"] == pytest.approx(2_000_000.0)
    positions = pd.DataFrame([[1.0, 0.0, -0.5, 0.0, 0.4]], index=[day], columns=panel.returns.columns)
    rounded, contracts = ma.whole_contracts(panel, positions, capital_nok=1_000_000.0)
    assert contracts.at[day, "SPX"] == round(1_000_000.0 / notional["SPX"]) and contracts.at[day, "NOK"] == 0.0
    assert rounded.at[day, "EUR"] == pytest.approx(contracts.at[day, "EUR"] * notional["EUR"] / 1_000_000.0) and rounded.at[day, "NOK"] == 0.0


def test_statistics() -> None:
    returns = pd.Series([0.01, -0.005, 0.02, -0.01, 0.0], index=pd.bdate_range("2024-01-29", periods=5))
    assert ma.sharpe(returns) == pytest.approx(returns.mean() / returns.std() * np.sqrt(252))
    assert ma.sortino(returns) == pytest.approx(returns.mean() / np.sqrt((0.005**2 + 0.01**2) / 5) * np.sqrt(252))
    assert ma.max_drawdown(returns) == pytest.approx(0.99 - 1.0)
    assert list(ma.monthly(returns).round(6)) == [round(1.01 * 0.995 * 1.02 - 1, 6), -0.01]
