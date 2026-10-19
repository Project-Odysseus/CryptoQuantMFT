"""H14 strategy pieces (src/research/regime_switch.py): state machine, weights, range rules, timing, and a full run.

Synthetic data only: these test the software, not the strategy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import regime_switch as rs

CONFIG = rs.load_config()


def _scores(p: list[float], **columns: list) -> pd.DataFrame:
    """A minimal score frame for the state machine: everything neutral unless given."""
    n = len(p)
    frame = pd.DataFrame({"trend_probability": p, "er_score": 0.5, "slope_score": 0.5, "adx_score": 0.5, "vr_score": 0.5, "breakout_score": 0.5,
                          "vol_expanding": False, "direction_confirmed": True, "upper_distance": -1.0, "lower_distance": -1.0},
                         index=pd.bdate_range("2020-01-01", periods=n))
    for name, values in columns.items():
        frame[name] = values
    return frame


def test_entry_needs_three_confirmations() -> None:
    regime = rs.classify(_scores([0.5] * 3 + [0.7] * 4), CONFIG)
    assert list(regime["state"].iloc[3:6]) == ["TRANSITION", "TRANSITION", "TREND"]
    regime = rs.classify(_scores([0.5] * 3 + [0.2] * 4), CONFIG)
    assert list(regime["state"].iloc[3:6]) == ["TRANSITION", "TRANSITION", "RANGE"]


def test_entry_and_exit_thresholds_differ() -> None:
    p = [0.7] * 4 + [0.55] * 5 + [0.45, 0.45]
    states = list(rs.classify(_scores(p), CONFIG)["state"])
    assert states[3:9] == ["TREND"] * 6  # 0.55 is below the 0.65 entry but above the 0.50 exit: stays in TREND
    assert states[-1] == "TRANSITION" and states[-2] == "TREND"  # two closes below 0.50 to leave
    assert set(rs.classify(_scores([0.6] * 10), CONFIG)["state"]) == {"TRANSITION"}  # 0.60 never enters TREND


def test_trend_to_range_passes_through_transition() -> None:
    states = list(rs.classify(_scores([0.7] * 4 + [0.2] * 6), CONFIG)["state"])
    first_range = states.index("RANGE")
    assert "TRANSITION" in states[states.index("TREND"):first_range]


def test_the_transition_weight_schedules() -> None:
    from_range = rs.classify(_scores([0.2] * 4 + [0.55, 0.55, 0.55, 0.55]), CONFIG)
    rows = from_range.iloc[4:]
    exit_day = list(rows["state"]).index("TRANSITION")
    assert (rows["trend_weight"].iloc[exit_day], rows["range_weight"].iloc[exit_day]) == (0.25, 0.25)
    assert (rows["trend_weight"].iloc[exit_day + 1], rows["range_weight"].iloc[exit_day + 1]) == (0.5, 0.0)
    from_trend = rs.classify(_scores([0.7] * 4 + [0.45] * 5), CONFIG)
    rows = from_trend.iloc[4:]
    exit_day = list(rows["state"]).index("TRANSITION")
    assert (rows["trend_weight"].iloc[exit_day], rows["range_weight"].iloc[exit_day]) == (0.5, 0.0)
    assert (rows["trend_weight"].iloc[exit_day + 1], rows["range_weight"].iloc[exit_day + 1]) == (0.25, 0.25)


def test_the_rapid_breakout_moves_to_transition_at_once_but_not_to_full_trend() -> None:
    n = 6
    upper = [-1.0] * 4 + [0.8, -1.0]
    scores = _scores([0.2] * 4 + [0.3, 0.3], upper_distance=upper, vol_expanding=[False] * 4 + [True, False], er_score=[0.5] * 4 + [0.7, 0.5])
    regime = rs.classify(scores, CONFIG)
    assert regime["state"].iloc[3] == "RANGE" and regime["state"].iloc[4] == "TRANSITION" and regime["rapid"].iloc[4] == 1.0
    assert regime["trend_weight"].iloc[4] == 0.25
    assert rs.classify(scores, CONFIG, rapid=False)["state"].iloc[4] == "RANGE"
    assert n == len(regime)


def test_volatility_expansion_without_direction_is_transition_not_trend() -> None:
    scores = _scores([0.5] * 2 + [0.7] * 5, vol_expanding=True, direction_confirmed=False)
    assert set(rs.classify(scores, CONFIG)["state"]) == {"TRANSITION"}
    fast = _scores([0.5] * 2 + [0.7] * 5, vol_expanding=True, direction_confirmed=True)
    assert rs.classify(fast, CONFIG)["state"].iloc[3] == "TREND"  # confirmed expansion: two observations suffice


def _combine(state: str, tw: float, rw: float, ts: float, rsig: float, stress: bool = False) -> float:
    index = pd.bdate_range("2020-01-01", periods=2)
    regime = pd.DataFrame({"state": [state] * 2, "trend_weight": [tw] * 2, "range_weight": [rw] * 2}, index=index)
    trend = pd.DataFrame({"trend_signal": [ts] * 2}, index=index)
    scores = pd.DataFrame({"stress": [stress] * 2}, index=index)
    return float(rs.combine(trend, pd.Series([rsig] * 2, index=index), regime, scores, CONFIG)["target_signal"].iloc[-1])


def test_conflicting_signals_respect_the_transition_cap_and_committed_regimes_use_one_model() -> None:
    assert _combine("TRANSITION", 0.5, 0.25, 1.0, -1.0) == pytest.approx(0.5 / 0.75 * 0.25)  # gross 0.75 scaled to 0.5, then netted
    assert _combine("RANGE", 0.0, 1.0, 1.0, -0.8) == pytest.approx(-0.8)
    assert _combine("TREND", 1.0, 0.0, 0.9, -0.8) == pytest.approx(0.9)


def test_the_dead_zone_and_stress_scaling() -> None:
    assert _combine("TREND", 1.0, 0.0, 0.08, 0.0) == 0.0
    assert _combine("TREND", 1.0, 0.0, 0.8, 0.0, stress=True) == pytest.approx(0.4)


def _range_fixture(z: list[float], **overrides: list) -> tuple[pd.DataFrame, ...]:
    n = len(z)
    index = pd.bdate_range("2020-01-01", periods=n)
    frame = pd.DataFrame({"close": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "open": [100.0] * n}, index=index)
    inputs = pd.DataFrame({"z": z, "range_high": [110.0] * n, "range_low": [90.0] * n, "atr": [2.0] * n}, index=index)
    trend = pd.DataFrame({"trend_signal": [0.0] * n, "breakout_state": [0.0] * n}, index=index)
    scores = pd.DataFrame({"vol_ratio": [1.0] * n, "stress": [False] * n}, index=index)
    regime = pd.DataFrame({"state": ["RANGE"] * n, "range_weight": [1.0] * n, "rapid": [0.0] * n}, index=index)
    for name, values in overrides.items():
        for table in (frame, inputs, trend, scores, regime):
            if name in table:
                table[name] = values
    return frame, inputs, trend, scores, regime


def test_the_range_model_enters_on_z_and_exits_when_z_crosses_zero() -> None:
    frame, inputs, trend, scores, regime = _range_fixture([0.0, -2.0, -1.0, -0.5, 0.1, 0.0])
    signal, trades = rs.range_model(frame, inputs, trend, scores, CONFIG, regime=regime)
    assert list(signal["range_signal"].round(2)) == [0.0, 0.8, 0.4, 0.2, 0.0, 0.0]
    assert trades.iloc[0]["reason"] == "z crossed zero"


def test_no_direct_reversal_on_the_same_observation() -> None:
    frame, inputs, trend, scores, regime = _range_fixture([-2.0, 2.0, 2.0])
    signal, _trades = rs.range_model(frame, inputs, trend, scores, CONFIG, regime=regime)
    assert list(np.sign(signal["range_signal"])) == [1.0, 0.0, -1.0]  # exits on bar 1, may go short only on bar 2


def test_the_maximum_holding_period() -> None:
    frame, inputs, trend, scores, regime = _range_fixture([-2.0] * 30)
    signal, trades = rs.range_model(frame, inputs, trend, scores, CONFIG, regime=regime)
    assert trades.iloc[0]["reason"] == "max hold" and trades.iloc[0]["days"] == 20
    assert (signal["range_signal"].iloc[:20] > 0).all() and signal["range_signal"].iloc[20] == 0.0


def test_stress_blocks_new_range_entries() -> None:
    frame, inputs, trend, scores, regime = _range_fixture([-2.0] * 6, stress=[True, False, False, False, False, False])
    signal, _trades = rs.range_model(frame, inputs, trend, scores, CONFIG, regime=regime)
    assert list(signal["range_signal"].iloc[:3]) == [0.0, 0.0, 0.0] and signal["range_signal"].iloc[3] > 0  # blocked for 3 observations


def test_a_breakout_closes_an_opposing_range_position() -> None:
    frame, inputs, trend, scores, regime = _range_fixture([2.0, 2.0, 2.0], rapid=[0.0, 1.0, 0.0])
    signal, trades = rs.range_model(frame, inputs, trend, scores, CONFIG, regime=regime)
    assert signal["range_signal"].iloc[0] < 0 and signal["range_signal"].iloc[1] == 0.0
    assert trades.iloc[0]["reason"] == "opposite breakout"


def test_version_b_caps_the_short_signal() -> None:
    frame, inputs, trend, scores, regime = _range_fixture([0.0, 3.0, 2.0])
    signal, _trades = rs.range_model(frame, inputs, trend, scores, CONFIG, regime=regime, version="B")
    assert signal["range_signal"].min() == -0.5


def test_a_signal_at_close_t_trades_at_t_plus_1_and_earns_from_t_plus_2() -> None:
    index = pd.bdate_range("2020-01-01", periods=6)
    signal = pd.Series([0.0, 0.0, 1.0, 1.0, 1.0, 1.0], index=index)
    returns = pd.Series(0.01, index=index)
    run = rs.backtest("t", signal, returns, CONFIG, cost_bps=1.0, scale=pd.Series(1.0, index=index))
    assert run.decided.iloc[2] == 1.0 and run.traded.iloc[2] == 0.0 and run.traded.iloc[3] == 1.0
    assert list(run.earning) == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0]


def _synthetic_market(count: int = 2600, seed: int = 1) -> pd.DataFrame:
    """Alternating range (mean-reverting), trend and shock segments: software fixture only."""
    rng = np.random.default_rng(seed)
    returns = []
    for segment in range(count // 200 + 1):
        kind = segment % 3
        if kind == 0:  # range: an AR(1) around a level
            level, shocks = 0.0, rng.normal(0.0, 0.01, 200)
            path = np.zeros(200)
            for i in range(1, 200):
                path[i] = 0.9 * path[i - 1] + shocks[i]
            returns.extend(np.diff(np.concatenate([[level], path])))
        elif kind == 1:
            returns.extend(rng.normal(0.003 * (1 if segment % 2 else -1), 0.008, 200))
        else:
            returns.extend(rng.normal(0.0, 0.03 if segment % 4 == 2 else 0.01, 200))
    close = 1000.0 * np.exp(np.cumsum(returns[:count]))
    spread = np.abs(rng.normal(0.0, 0.006, count)) * close
    return pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1.0},
                        index=pd.bdate_range("2000-01-03", periods=count))


def _rates(index: pd.DatetimeIndex) -> pd.DataFrame:
    months = pd.period_range(index[0] - pd.Timedelta(days=62), index[-1], freq="M")
    return pd.DataFrame({"USA": 0.0}, index=months)


def test_appending_future_data_does_not_change_past_regimes() -> None:
    frame = _synthetic_market()
    short = frame.iloc[:2200]
    full = rs.classify(rs.regime_scores(frame, CONFIG), CONFIG)
    part = rs.classify(rs.regime_scores(short, CONFIG), CONFIG)
    pd.testing.assert_frame_equal(full.iloc[:2200], part)


def test_index_returns_have_no_roll_artefacts() -> None:
    frame = _synthetic_market(600)
    frame["close"] = 100.0
    returns = rs.market_returns(CONFIG, "NDX", frame, _rates(frame.index))
    assert returns.iloc[1:].round(10).nunique() == 1  # a flat index earns only its carry, every day


def test_a_full_run_on_a_fixture_covers_every_state_and_trades_nothing_in_warm_up() -> None:
    frame = _synthetic_market()
    study = rs.run_market(CONFIG, "NDX", frame, _rates(frame.index), full=True)
    primary = study.runs[rs.PRIMARY]
    states = set(study.regimes[rs.PRIMARY]["state"])
    assert {"RANGE", "TRANSITION", "TREND"} <= states and study.scores["stress"].any()
    warm = study.scores["trend_probability"].isna()
    assert (primary.decided[warm] == 0).all() and np.isfinite(primary.net).all()
    assert (primary.decided.abs() > 0).any() and len(study.runs) > 30
    metrics = rs.metrics(primary)
    assert {"Sharpe", "turnover", "long contribution", "short contribution"} <= set(metrics)
