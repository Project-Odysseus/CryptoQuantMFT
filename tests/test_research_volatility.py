from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.research.volatility import (
    daily_forecast_to_bars,
    daily_realized_variance,
    ewma_vol,
    forecast_losses,
    har_forecast,
    log_returns,
    rolling_vol,
    vol_scaled_positions,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_daily_realized_variance_sums_squared_hourly_returns_per_day_and_drops_partial_days() -> None:
    hours = 24 * 2 + 5  # two full days and five hours of a third
    timestamps = [START + timedelta(hours=i) for i in range(hours)]
    returns = np.where(np.arange(hours) < 24, 0.01, 0.02)
    close = 100.0 * np.exp(np.cumsum(np.concatenate([[0.0], returns[1:]])))

    rv = daily_realized_variance(timestamps, close)

    assert list(rv.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02"]
    assert rv.iloc[0] == pytest.approx(23 * 0.01**2)  # the first bar has no previous close
    assert rv.iloc[1] == pytest.approx(24 * 0.02**2)


def test_rolling_and_ewma_vol_are_annualised_and_use_the_past_only() -> None:
    returns = np.array([np.nan, 0.01, -0.01, 0.01, -0.01, 0.05])
    rolling = rolling_vol(returns, 4, 365.0)
    assert np.isnan(rolling[3])
    assert rolling[4] == pytest.approx(0.01 * np.sqrt(365.0))

    halflife = 2.0
    ewma = ewma_vol(returns, halflife, 365.0)
    weights = 0.5 ** (np.arange(4)[::-1] / halflife)  # returns 1..4, newest weighted most
    expected = np.sqrt(np.sum(weights * returns[1:5] ** 2) / weights.sum() * 365.0)
    assert ewma[4] == pytest.approx(expected)
    assert ewma[5] > ewma[4]  # reacts to the new shock without it touching earlier values


def test_har_forecast_never_uses_later_data() -> None:
    rng = np.random.default_rng(3)
    days = pd.date_range("2021-01-01", periods=700, freq="D", tz="UTC")
    log_vol = np.zeros(len(days))
    for i in range(1, len(days)):
        log_vol[i] = 0.95 * log_vol[i - 1] + rng.normal(0, 0.3)
    rv = pd.Series(np.exp(2 * (log_vol - 4)) * rng.chisquare(24, len(days)) / 24, index=days)

    forecast = har_forecast(rv, horizon_days=7, train_min_days=365, refit_every_days=30)
    shocked = rv.copy()
    shocked.iloc[500:] *= 50.0
    forecast_shocked = har_forecast(shocked, horizon_days=7, train_min_days=365, refit_every_days=30)

    assert forecast.iloc[:365].isna().all() and (forecast.iloc[365:] > 0).all()
    pd.testing.assert_series_equal(forecast.iloc[:500], forecast_shocked.iloc[:500])
    naive = forecast_losses(np.full(len(rv), rv.iloc[:365].mean())[365:-7], rv.rolling(7).mean().shift(-7).iloc[365:-7].to_numpy())
    har = forecast_losses(forecast.iloc[365:-7].to_numpy(), rv.rolling(7).mean().shift(-7).iloc[365:-7].to_numpy())
    assert har["qlike"] < naive["qlike"]  # persistent volatility is forecastable


def test_daily_forecast_maps_to_the_last_completed_day() -> None:
    daily = pd.Series([0.4, 0.6], index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], tz="UTC"))
    closes = [datetime(2024, 1, 2, 20, tzinfo=timezone.utc), datetime(2024, 1, 3, 0, tzinfo=timezone.utc)]
    assert list(daily_forecast_to_bars(daily, closes)) == [0.4, 0.6]


def test_forecast_losses_are_zero_for_a_perfect_forecast() -> None:
    realized = np.array([1.0, 2.0, 4.0])
    losses = forecast_losses(realized, realized)
    assert losses["qlike"] == pytest.approx(0.0) and losses["mean_ratio"] == pytest.approx(1.0) and losses["r2_log"] == pytest.approx(1.0)
    assert forecast_losses(realized / 2, realized)["mean_ratio"] == pytest.approx(2.0)


def test_vol_scaled_positions_size_at_entry_cap_leverage_and_rebalance_outside_the_band() -> None:
    targets = [0, 1, 1, 1, 1, -1, 0, 1]
    forecast = [0.5, 0.5, 0.45, 0.2, 1.0, 1.0, 1.0, np.nan]

    rebalanced = vol_scaled_positions(targets, forecast, target_vol=0.5, max_leverage=2.0, rebalance_band=0.25)
    assert list(rebalanced) == [0.0, 1.0, 1.0, 2.0, 0.5, -0.5, 0.0, 1.0]  # 0.45 is inside the band; NaN falls back to 1

    entry_only = vol_scaled_positions(targets, forecast, target_vol=0.5, max_leverage=2.0, entry_only=True)
    assert list(entry_only) == [0.0, 1.0, 1.0, 1.0, 1.0, -0.5, 0.0, 1.0]
