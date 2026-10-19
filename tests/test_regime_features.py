"""Causal regime features (src/signals/regime_features.py): known values and no look-ahead."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.signals import regime_features as rf


def _ohlc(count: int = 1500, *, seed: int = 0, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.01, count)))
    spread = np.abs(rng.normal(0.0, 0.006, count)) * close
    days = pd.bdate_range("2000-01-03", periods=count)
    return pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1.0}, index=days)


def test_previous_high_and_low_exclude_the_current_bar() -> None:
    high = np.array([1.0, 2.0, 3.0, 10.0, 4.0])
    low = np.array([5.0, 4.0, 3.0, 0.0, 2.0])
    assert rf.previous_high(high, 2)[3] == 3.0  # bars 1-2, not bar 3's 10
    assert rf.previous_low(low, 2)[3] == 3.0
    assert rf.previous_high(high, 2)[4] == 10.0
    assert np.isnan(rf.previous_high(high, 2)[1])


def test_a_breakout_cannot_widen_its_own_boundary() -> None:
    frame = _ohlc(300)
    frame.iloc[-1, frame.columns.get_loc("close")] = frame["high"].max() * 1.10
    frame.iloc[-1, frame.columns.get_loc("high")] = frame["close"].iloc[-1]
    atr_values = np.full(len(frame), 1.0)
    upper, _lower = rf.breakout_distance(frame["high"].to_numpy(), frame["low"].to_numpy(), frame["close"].to_numpy(), 60, atr_values)
    assert upper[-1] > 0  # still beyond the boundary of the 60 bars before it


def test_efficiency_ratio_is_one_on_a_line_and_low_on_a_zigzag() -> None:
    line = np.arange(100, dtype=float)
    zigzag = 100.0 + np.tile([0.0, 1.0], 50)
    assert rf.efficiency_ratio(line, 40)[-1] == pytest.approx(1.0)
    assert rf.efficiency_ratio(zigzag, 40)[-1] < 0.05
    assert np.isnan(rf.efficiency_ratio(line, 40)[39]) and np.isfinite(rf.efficiency_ratio(line, 40)[40])


def test_adx_is_high_in_a_steady_trend_and_low_in_chop() -> None:
    n = 200
    up = np.arange(n, dtype=float) + 100.0
    trending = rf.adx(up + 0.5, up - 0.5, up, 14)
    chop_close = 100.0 + np.tile([0.0, 1.0], n // 2)
    choppy = rf.adx(chop_close + 0.6, chop_close - 0.6, chop_close, 14)
    assert trending[-1] > 50 and choppy[-1] < 20


def test_the_variance_ratio_is_about_one_for_a_random_walk_and_reacts_to_persistence() -> None:
    rng = np.random.default_rng(3)
    noise = rng.normal(0.0, 0.01, 20000)
    assert np.nanmean(rf.variance_ratio(noise, 40, 5)) == pytest.approx(1.0, abs=0.05)
    persistent = np.convolve(noise, np.ones(5) / 5, mode="same")
    reverting = np.diff(noise, prepend=0.0)
    assert np.nanmean(rf.variance_ratio(persistent, 40, 5)) > 1.5
    assert np.nanmean(rf.variance_ratio(reverting, 40, 5)) < 0.6


def test_calendar_percentiles_use_only_the_years_before_and_are_frozen_within_a_year() -> None:
    values = pd.Series(np.arange(1000, dtype=float), index=pd.bdate_range("2000-01-03", periods=1000))
    scores = rf.calendar_percentile(values, calibration_days=500, min_calibration=250)
    assert scores[values.index.year == 2000].isna().all()  # no history before 2000
    in_2002 = scores[values.index.year == 2002]
    assert (in_2002 == 1.0).all()  # every 2002 value is above everything before 2002 (a rising series)


def test_appending_future_data_does_not_change_past_features_or_scores() -> None:
    frame = _ohlc(1500, seed=5)
    short = frame.iloc[:1200]
    full = rf.build_features(frame, horizon=40, adx_window=14, vr_lag=5, short_vol=10, long_vol=60, breakout=60)
    part = rf.build_features(short, horizon=40, adx_window=14, vr_lag=5, short_vol=10, long_vol=60, breakout=60)
    pd.testing.assert_frame_equal(full.iloc[:1200], part)
    score_full = rf.calendar_percentile(full["er"], calibration_days=500, min_calibration=250).iloc[:1200]
    score_part = rf.calendar_percentile(part["er"], calibration_days=500, min_calibration=250)
    pd.testing.assert_series_equal(score_full, score_part)


def test_the_adf_diagnostic_separates_a_stationary_series_from_a_random_walk() -> None:
    rng = np.random.default_rng(4)
    walk = np.cumsum(rng.normal(size=3000))
    stationary = np.zeros(3000)
    for index in range(1, 3000):
        stationary[index] = 0.5 * stationary[index - 1] + rng.normal()
    assert np.nanmean(rf.adf_tstat(stationary, 200)) < -5.0
    assert np.nanmean(rf.adf_tstat(walk, 200)) > -2.5
