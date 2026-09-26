"""Tests for the feature-analysis and walk-forward forecast research helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.research.features import bucket_table, forward_return, ic_by_year, information_coefficient, past_return, seasonality, volatility_scaled
from src.research.forecast import threshold_positions, walk_forward_ridge


def test_forward_and_past_returns_line_up_without_look_ahead() -> None:
    """past_return at t uses closes up to t; forward_return at t uses closes after t."""
    close = np.array([100.0, 110.0, 99.0, 121.0])
    assert forward_return(close, 1)[0] == pytest.approx(np.log(1.1))
    assert np.isnan(forward_return(close, 1)[-1])
    assert past_return(close, 1)[1] == pytest.approx(np.log(1.1))
    assert np.isnan(past_return(close, 2)[1])


def test_bucket_table_reports_bps_and_ic_detects_a_planted_relationship() -> None:
    """A feature that really predicts the forward return shows a monotone bucket table and a positive IC."""
    rng = np.random.default_rng(0)
    feature = rng.normal(size=5000)
    forward = 0.001 * feature + rng.normal(0.0, 0.01, 5000)
    table = bucket_table(feature, forward, 1, buckets=5)
    assert list(table["bucket"]) == [1, 2, 3, 4, 5]
    assert table["mean_bps"].iloc[-1] > table["mean_bps"].iloc[0] + 10.0
    assert information_coefficient(feature, forward) > 0.05
    assert abs(information_coefficient(rng.normal(size=5000), forward)) < 0.05


def test_ic_by_year_and_seasonality_group_correctly() -> None:
    """Yearly IC has one entry per year; seasonality has one row per hour and counts positive years."""
    index = pd.date_range("2024-01-01", periods=24 * 800, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, len(index))))
    frame = pd.DataFrame({"close": close}, index=index)
    assert list(ic_by_year(rng.normal(size=len(index)), forward_return(close, 1), index).index) == [2024, 2025, 2026]
    table = seasonality(frame, by="hour")
    assert list(table.index) == list(range(24))
    assert set(["all", "t_stat", "years_positive"]) <= set(table.columns)


def test_volatility_scaling_makes_the_same_move_bigger_in_calm_markets() -> None:
    """A 1% move scores higher after a quiet week than after a volatile one."""
    rng = np.random.default_rng(2)
    calm = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, 200)))
    wild = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, 200)))
    move = np.full(200, 0.01)
    assert volatility_scaled(move, calm, 100)[-1] > 5 * volatility_scaled(move, wild, 100)[-1]


def test_walk_forward_ridge_recovers_a_relationship_out_of_sample() -> None:
    """With a real linear link, out-of-sample forecasts correlate with the target."""
    rng = np.random.default_rng(3)
    x = rng.normal(size=(3000, 3))
    y = 0.5 * x[:, 0] - 0.3 * x[:, 2] + rng.normal(0.0, 1.0, 3000)
    forecast = walk_forward_ridge(x, y, horizon=1, train_min=500, refit_every=250)
    assert np.isnan(forecast[:500]).all() and np.isfinite(forecast[500:]).all()
    assert np.corrcoef(forecast[500:], y[500:])[0, 1] > 0.4


def test_walk_forward_ridge_never_uses_future_targets() -> None:
    """Changing targets after a bar (and inside the purge window) cannot change that bar's forecast."""
    rng = np.random.default_rng(4)
    x = rng.normal(size=(2000, 2))
    y = x[:, 0] + rng.normal(0.0, 1.0, 2000)
    base = walk_forward_ridge(x, y, horizon=10, train_min=400, refit_every=100)
    tampered = y.copy()
    tampered[1190:] = 1e6  # targets from bar 1190 onwards; with horizon 10 they are unknown before bar 1200
    after = walk_forward_ridge(x, tampered, horizon=10, train_min=400, refit_every=100)
    np.testing.assert_array_equal(base[:1200], after[:1200])
    assert not np.allclose(base[1200:], after[1200:])


def test_threshold_positions_enter_on_strength_and_exit_on_sign_change() -> None:
    """Enter above the threshold, keep holding while the forecast stays positive, exit when it turns negative."""
    forecast = np.array([0.0, 3.0, 12.0, 4.0, 1.0, -2.0, -15.0, -1.0, 2.0])
    assert list(threshold_positions(forecast, 10.0)) == [0, 0, 1, 1, 1, 0, -1, -1, 0]
    assert list(threshold_positions(forecast, 10.0, allow_short=False)) == [0, 0, 1, 1, 1, 0, 0, 0, 0]
