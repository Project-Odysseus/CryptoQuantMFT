"""Event studies (src/research/events.py): timing, statistics, drawdown episodes, regimes and data checks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.events import (
    by_regime,
    data_quality,
    drawdown_episodes,
    episode_events,
    episode_progression,
    event_frequency,
    event_study,
    excursions,
    first_crossings,
    forward_returns,
    plateau,
    regime_labels,
    robustness_grid,
)


def _frame(close: list[float] | np.ndarray, *, freq: str = "D", start: str = "2024-01-01", spread: float = 0.0) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    open_ = np.r_[close[0], close[:-1]]
    index = pd.date_range(start, periods=len(close), freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + spread), "low": np.minimum(open_, close) * (1 - spread), "close": close, "volume": 1.0}, index=index)


def test_first_crossings_count_a_lasting_condition_once_and_merge_flicker() -> None:
    active = [False, True, True, False, True, False, False, True, True]
    assert first_crossings(active).tolist() == [1, 4, 7]
    assert first_crossings(active, min_gap=4).tolist() == [1, 7]  # the event at 4 is within 4 bars of the one at 1
    assert first_crossings([True, True]).tolist() == [0] and first_crossings([False] * 3).tolist() == []


def test_forward_returns_enter_at_the_next_open_and_end_at_the_horizons_close() -> None:
    frame = _frame([100.0, 110.0, 121.0, 133.1, 146.41])
    frame.loc[frame.index[2], "open"] = 112.0  # the bar after the event gaps: the entry is this open, not the event's close
    assert forward_returns(frame, [1], 2)[0] == pytest.approx(np.log(133.1 / 112.0))
    assert forward_returns(frame, [1], 2, entry="close")[0] == pytest.approx(np.log(133.1 / 110.0))
    assert np.isnan(forward_returns(frame, [3], 2)[0])  # runs past the data
    with pytest.raises(ValueError, match="entry"):
        forward_returns(frame, [1], 1, entry="midnight")
    with pytest.raises(ValueError, match="open, high, low and close"):
        forward_returns(frame[["close"]], [1], 1)


def test_excursions_measure_the_worst_and_best_move_on_the_way_for_either_side() -> None:
    frame = _frame([100.0, 100.0, 90.0, 120.0, 105.0])
    worst, best = excursions(frame, [0], 4)  # entry at the open of bar 1 (100): low 90, high 120
    assert worst[0] == pytest.approx(np.log(0.9)) and best[0] == pytest.approx(np.log(1.2))
    short_worst, short_best = excursions(frame, [0], 4, side=-1.0)
    assert short_worst[0] == pytest.approx(-np.log(1.2)) and short_best[0] == pytest.approx(-np.log(0.9))
    calm = excursions(_frame([100.0, 100.0, 101.0, 102.0]), [0], 3)
    assert calm[0][0] == 0.0 and calm[1][0] == pytest.approx(np.log(1.02))  # never below the entry: the worst is 0, not positive


def _planted(seed: int = 0, bars: int = 3000, effect: float = 0.004) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Random-walk bars where the 5 bars after each event drift up by `effect` per bar."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, 0.01, bars)
    events = np.arange(100, bars - 100, 60)
    for position in events:
        returns[position + 1 : position + 6] += effect
    close = 100.0 * np.exp(np.cumsum(returns))
    frame = _frame(close, freq="h", spread=0.001)
    vol = pd.Series(np.log(frame["close"]).diff()).rolling(24).std().to_numpy()
    return frame, events, vol


def test_an_event_study_finds_a_planted_effect_and_its_controls_do_not() -> None:
    frame, events, vol = _planted()
    study = event_study(frame, events, horizons=(1, 5, 20), matching=vol, bootstrap_runs=500)
    row = study.summary.loc[5]
    assert row["events"] == len(events) and row["mean"] == pytest.approx(0.004 * 4, abs=0.006)  # the entry is after the first drifting bar's open
    assert row["ci_low"] > 0 and row["hit_rate"] > 0.6 and row["q05"] < row["median"] < row["q95"]
    assert abs(row["control_mean"]) < 0.004 and row["excess"] > 0.008 and row["excess_ci_low"] > 0
    assert row["mean_mae"] <= 0 <= row["mean_mfe"]
    assert study.summary.loc[20, "mean"] < 2 * row["mean"]  # the drift stops after 5 bars
    assert list(study.events.columns[:2]) == ["time", "position"] and {"ret_5", "mae_5", "mfe_5", "control_5"} <= set(study.events.columns)
    assert study.paths.loc[0, "mean"] == 0.0 and study.paths.loc[5, "mean"] > 0.015 and study.paths.loc[-10, "events"] == len(events)
    assert study.frequency["events"] == len(events) and study.frequency["median_gap_bars"] == 60

    short = event_study(frame, events, horizons=(5,), side=-1.0, bootstrap_runs=200)
    assert short.summary.loc[5, "mean"] == pytest.approx(-row["mean"]) and np.isnan(short.summary.loc[5, "control_mean"])  # no matching: no controls
    costly = event_study(frame, events, horizons=(5,), cost_bps=20.0, bootstrap_runs=200)
    assert costly.summary.loc[5, "mean"] == pytest.approx(row["mean"] - 0.002)
    by_time = event_study(frame, frame.index[events], horizons=(5,), bootstrap_runs=200)
    assert by_time.summary.loc[5, "mean"] == pytest.approx(row["mean"])


def test_an_event_study_of_random_moments_shows_nothing_and_an_empty_one_does_not_crash() -> None:
    frame, _events, vol = _planted(effect=0.0, seed=3)
    random_events = np.sort(np.random.default_rng(5).choice(np.arange(100, 2800), size=60, replace=False))
    study = event_study(frame, random_events, horizons=(5,), matching=vol, bootstrap_runs=500)
    assert study.summary.loc[5, "ci_low"] < 0 < study.summary.loc[5, "ci_high"]
    assert study.summary.loc[5, "excess_ci_low"] < 0 < study.summary.loc[5, "excess_ci_high"]
    empty = event_study(frame, [], horizons=(5,), matching=vol)
    assert empty.summary.loc[5, "events"] == 0 and np.isnan(empty.summary.loc[5, "mean"]) and empty.frequency["events"] == 0
    with pytest.raises(ValueError, match="not bar timestamps"):
        event_study(frame, pd.DatetimeIndex([pd.Timestamp("1999-01-01", tz="UTC")]))


def test_event_outcomes_do_not_depend_on_bars_before_the_event_being_different_later() -> None:
    frame, events, _vol = _planted()
    study = event_study(frame, events[:5], horizons=(5,), bootstrap_runs=100)
    changed = frame.copy()
    changed.iloc[events[4] + 30 :] *= 3.0  # a different future beyond every window
    again = event_study(changed, events[:5], horizons=(5,), bootstrap_runs=100)
    pd.testing.assert_series_equal(study.events["ret_5"], again.events["ret_5"])


def test_event_frequency_counts_per_year() -> None:
    index = pd.date_range("2023-06-01", periods=500, freq="D", tz="UTC")
    frequency = event_frequency(index, [10, 40, 300, 480])
    assert frequency["per_year"] == {2023: 2, 2024: 2} and frequency["events"] == 4 and frequency["median_gap_bars"] == 180.0
    assert frequency["years"] == 2 and frequency["years_with_events"] == 2


CLOSES = [100, 101, 102, 100, 96, 94, 91, 93, 97, 103, 104, 99, 98, 97.5, 90]


def test_a_drawdown_episode_keeps_every_depth_of_one_selloff_together() -> None:
    close = pd.Series(CLOSES, index=pd.date_range("2024-01-01", periods=len(CLOSES), freq="D", tz="UTC"), dtype=float)
    episodes = drawdown_episodes(close, thresholds=(0.05, 0.07, 0.10), peak_lookback=3)
    first, second = episodes.iloc[0], episodes.iloc[1]
    assert first["peak_price"] == 102 and first["peak_time"] == close.index[2] and first["start_time"] == close.index[4]  # 96 is 5.9% below 102
    assert (first["cross_5_pos"], first["cross_7_pos"], first["cross_10_pos"]) == (4, 5, 6)  # 96, 94 (7.8%), 91 (10.8%)
    assert first["trough_price"] == 91 and first["max_drawdown"] == pytest.approx(1 - 91 / 102) and first["bars_to_trough"] == 2
    assert first["recovered"] and not first["censored"] and first["end_time"] == close.index[9] and first["bars_to_recovery"] == 5  # 103 >= 102
    assert second["peak_price"] == 104 and second["cross_5_pos"] == 12 and second["cross_10_pos"] == 14 and second["cross_7_pos"] == 14
    assert second["censored"] and not second["recovered"] and np.isnan(second["bars_to_recovery"]) and second["bars_observed"] == 2
    assert episode_events(episodes, 0.10).tolist() == [6, 14] and episode_events(episodes, 0.05).tolist() == [4, 12]
    with pytest.raises(ValueError, match="0.2 threshold"):
        episode_events(episodes, 0.2)
    with pytest.raises(ValueError, match="thresholds"):
        drawdown_episodes(close, thresholds=(0.0, 0.1))


def test_crossings_are_known_at_their_bar_and_a_long_episode_can_be_reset() -> None:
    close = pd.Series(CLOSES, index=pd.date_range("2024-01-01", periods=len(CLOSES), freq="D", tz="UTC"), dtype=float)
    full = drawdown_episodes(close, thresholds=(0.05, 0.10), peak_lookback=3)
    early = drawdown_episodes(close.iloc[:7], thresholds=(0.05, 0.10), peak_lookback=3)  # stop at the bar that crossed 10%
    assert early.iloc[0]["cross_5_pos"] == full.iloc[0]["cross_5_pos"] and early.iloc[0]["cross_10_pos"] == full.iloc[0]["cross_10_pos"]
    assert early.iloc[0]["censored"]
    reset = drawdown_episodes(close, thresholds=(0.05,), peak_lookback=3, max_bars=2)
    assert not reset.iloc[0]["recovered"] and not reset.iloc[0]["censored"] and reset.iloc[0]["bars_observed"] == 2
    assert len(reset) >= 2  # the scan carries on after the reset
    running = drawdown_episodes(close, thresholds=(0.05,), peak_lookback=None)
    assert running.iloc[0]["peak_price"] == 102 and running.iloc[1]["peak_price"] == 104


def test_progression_says_how_often_a_dip_deepened_and_leaves_unfinished_episodes_out_of_recovery_rates() -> None:
    close = pd.Series(CLOSES, index=pd.date_range("2024-01-01", periods=len(CLOSES), freq="D", tz="UTC"), dtype=float)
    table = episode_progression(drawdown_episodes(close, thresholds=(0.05, 0.07, 0.10), peak_lookback=3), recovery_bars=(3, 5, 20))
    assert table.loc[5.0, "episodes"] == 2 and table.loc[5.0, "went_on_to_10"] == 1.0 and table.loc[10.0, "episodes"] == 2
    assert table.loc[5.0, "recovered_within_3"] == 0.0  # the first took 5 bars; the second is still open with only 2 observed: left out
    assert table.loc[5.0, "recovered_within_5"] == 1.0 and table.loc[5.0, "recovered_within_20"] == 1.0  # of the one episode whose outcome is known
    assert table.loc[10.0, "recovered_within_3"] == 1.0  # from the 10% crossing the first episode needed 3 bars
    assert table.loc[5.0, "still_open"] == 1 and table.loc[5.0, "median_further_drawdown"] > 0.05


def test_regimes_use_only_the_past_and_split_the_events() -> None:
    frame, events, _vol = _planted(bars=2000)
    labels = regime_labels(frame, trend_window=50, vol_window=20, vol_history=200)
    assert set(labels["trend"]) == {"n/a", "up", "down"} and set(labels["vol"]) == {"n/a", "low", "mid", "high"}
    changed = frame.copy()
    changed.iloc[1500:] *= 2.0
    pd.testing.assert_frame_equal(regime_labels(changed, trend_window=50, vol_window=20, vol_history=200).iloc[:1500], labels.iloc[:1500])
    study = event_study(frame, events, horizons=(5,), bootstrap_runs=100)
    table = by_regime(study, labels, 5)
    assert table.loc["trend", "events"].sum() == len(events) and table.loc["vol", "events"].sum() == len(events)
    assert (table["mean"].dropna() > 0).all()  # the planted effect is there in every state
    with pytest.raises(ValueError, match="no 7-bar horizon"):
        by_regime(study, labels, 7)


def test_data_quality_finds_gaps_duplicates_and_broken_candles() -> None:
    frame = _frame(np.linspace(100, 110, 50), freq="h")
    clean = data_quality(frame, 3600)
    assert clean["bars"] == 50 and clean["missing_bars"] == 0 and clean["duplicate_timestamps"] == 0 and clean["inconsistent_candles"] == 0 and clean["sorted"]
    broken = frame.drop(frame.index[[10, 11, 12]])
    broken = pd.concat([broken, broken.iloc[[5]]]).sort_index()
    broken.iloc[20, broken.columns.get_loc("high")] = 1.0  # a high below the close
    broken.iloc[30, broken.columns.get_loc("close")] = 0.0
    broken.iloc[40:, broken.columns.get_loc("volume")] = 0.0
    report = data_quality(broken, 3600)
    assert report["missing_bars"] == 3 and report["longest_gap_bars"] == 4 and report["duplicate_timestamps"] == 1
    assert report["inconsistent_candles"] >= 1 and report["nonpositive_or_nan_prices"] == 1 and 0 < report["zero_volume_share"] < 0.3


def test_a_robustness_grid_shows_a_plateau_not_a_best_cell() -> None:
    frame, events, _vol = _planted()

    def study(shift: int, horizon_scale: int) -> object:
        return event_study(frame, events + shift, horizons=(5 * horizon_scale,), bootstrap_runs=100)

    table = robustness_grid(lambda shift, horizon_scale: event_study(frame, events + shift, horizons=(5,), bootstrap_runs=100), {"shift": [0, 1, 2], "horizon_scale": [1]}, horizon=5)
    assert len(table) == 3 and list(table.columns) == ["shift", "horizon_scale", "events", "mean", "ci_low", "ci_high"]
    assert table["mean"].is_monotonic_decreasing  # entering later catches less of the drift, smoothly
    flat = plateau(table)
    assert flat["cells"] == 3 and flat["same_sign_share"] == 1.0 and flat["min"] <= flat["median"] <= flat["max"]
    assert plateau(table.iloc[:0])["cells"] == 0 and study(0, 1).summary.index.tolist() == [5]
