import numpy as np
import pandas as pd
import pytest

from src.research.carry import carry_backtest, daily_funding

DAYS = pd.date_range("2024-01-01", periods=20, freq="D", tz="UTC")


def test_daily_funding_assigns_midnight_settlements_to_the_day_that_ended() -> None:
    times = pd.to_datetime(["2024-01-01 08:00", "2024-01-01 16:00", "2024-01-02 00:00", "2024-01-02 08:00"], utc=True)
    daily = daily_funding(times, [0.0001, 0.0002, 0.0003, 0.0004])
    assert daily.to_dict() == {pd.Timestamp("2024-01-01", tz="UTC"): pytest.approx(0.0006), pd.Timestamp("2024-01-02", tz="UTC"): pytest.approx(0.0004)}

    hourly = pd.date_range("2024-01-01 01:00", periods=24, freq="h", tz="UTC")
    assert daily_funding(hourly, [0.0008] * 24, payments_per_value=1 / 8).iloc[0] == pytest.approx(0.0024)  # an 8h rate paid hourly


def test_always_on_carry_pays_one_round_trip() -> None:
    funding = pd.Series(0.0003, index=DAYS)
    result = carry_backtest(funding, round_trip_cost=0.004)
    assert result.funding.sum() == pytest.approx(0.0003 * 20)
    assert result.costs.sum() == pytest.approx(0.004)
    assert result.net.sum() == pytest.approx(0.006 - 0.004)


def test_conditional_carry_enters_after_the_signal_and_pays_per_switch() -> None:
    values = np.array([0.0] * 5 + [0.001] * 8 + [-0.001] * 7)
    funding = pd.Series(values, index=DAYS)
    result = carry_backtest(funding, round_trip_cost=0.002, enter_above=0.10, exit_below=0.0, lookback_days=2)

    held = result.in_position.to_numpy()
    assert not held[:6].any()  # day 5's 2-day mean is 18%/yr, known at its close, so the position starts on day 6
    assert held[6:15].all() and not held[15:].any()  # day 14's mean turns negative; out from day 15
    assert result.costs.sum() == pytest.approx(0.002)  # one entry and one exit
    summary = result.summary(capital_per_notional=1.5)
    assert summary["net_on_capital_pct_per_year"] == pytest.approx(summary["net_pct_per_year"] / 1.5)
    assert summary["entries_per_year"] == pytest.approx(1 / (20 / 365))
