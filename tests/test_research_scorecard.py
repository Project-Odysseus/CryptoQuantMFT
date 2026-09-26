"""Strategy scorecard (src/research/scorecard.py) and the notebook strategy helpers (rule_strategy, signal_strategy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest.indicators import rolling_mean
from src.backtest.strategies import rule_strategy, signal_strategy
from src.research.engine import CostSettings
from src.research.scorecard import CHECKS, checks, placebo_beaten, position_returns, positions_for, scorecard, yearly_returns
from src.research.engine import run_strategy
from src.storage.bar_aggregator import OHLCVBar

START = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _bars(closes: np.ndarray) -> list[OHLCVBar]:
    return [OHLCVBar(exchange="mock", symbol="X/USD", interval_seconds=86400, timestamp=START + timedelta(days=i), open=float(c), high=float(c) * 1.01,
                     low=float(c) * 0.99, close=float(c), volume=1.0) for i, c in enumerate(closes)]


def _trending_walk(count: int = 1200, seed: int = 0, momentum: float = 0.15) -> np.ndarray:
    """Returns with momentum (AR(1) on the daily return), so trend rules have real timing to find."""
    rng = np.random.default_rng(seed)
    returns = np.zeros(count)
    for t in range(1, count):
        returns[t] = momentum * returns[t - 1] + rng.normal(0.0, 0.02)
    return 100.0 * np.exp(np.cumsum(returns))


def _ma_rules(fast: int = 5, slow: int = 30):
    def rules(bars):
        above = rolling_mean(bars.close, fast) > rolling_mean(bars.close, slow)
        return above, ~above, None, None

    return rule_strategy(rules, warmup=slow)


def test_rule_and_signal_strategies_agree_bar_by_bar_with_their_vectorized_series() -> None:
    bars = _bars(_trending_walk(200))
    ruled = _ma_rules()
    signed = signal_strategy(lambda b: np.sign(rolling_mean(b.close, 5) - rolling_mean(b.close, 30)), warmup=30)
    for strategy in (ruled, signed):
        vectorized = strategy.signal_series(bars)
        per_bar = [strategy(bars[: i + 1], i, bar) for i, bar in enumerate(bars)]
        np.testing.assert_array_equal(vectorized, per_bar)
        assert (vectorized[:29] == 0).all() and set(np.unique(vectorized)) <= {-1, 0, 1}
    assert (signed.signal_series(bars) == -1).any() and (ruled.signal_series(bars) >= 0).all()


def test_position_returns_pay_costs_on_changes_and_funding_on_holdings() -> None:
    close = np.array([100.0, 110.0, 99.0, 99.0])
    positions = np.array([1.0, 1.0, -1.0, 0.0])
    returns = position_returns(positions, close, cost_per_side=0.001, funding_per_bar=0.0005)
    expected = [-0.001, 0.10 - 0.0005, -0.10 - 0.002 - 0.0005, 0.0 - 0.001 + 0.0005]
    assert returns == pytest.approx(expected)


def test_the_placebo_rewards_real_timing_and_not_exposure_alone() -> None:
    close = _trending_walk(1500, seed=1, momentum=0.35)
    bars = _bars(close)
    timed = positions_for(_ma_rules(), bars)
    assert placebo_beaten(timed, close, runs=100) >= 0.9
    assert placebo_beaten(np.ones(len(close)), close, runs=100) == 0.0  # always long: shifting changes nothing
    noise = np.random.default_rng(3).choice([-1.0, 1.0], len(close))
    assert 0.05 < placebo_beaten(noise, close, runs=200) < 0.95
    assert np.isnan(placebo_beaten(timed[:10], close[:10]))


def test_yearly_returns_chain_back_to_the_run() -> None:
    bars = _bars(_trending_walk(1100, seed=2))
    run = run_strategy(bars, _ma_rules(), costs=CostSettings(fee_pct=0.0, slippage_bps=0.0), measure_start=31)
    years = yearly_returns(run)
    assert list(years.index) == [2020, 2021, 2022, 2023]
    equity = np.asarray(run.result.mtm_equity_series)
    assert float(np.prod(1.0 + years.to_numpy())) == pytest.approx(equity[-1] / equity[30])


def test_scorecard_and_checks_on_a_named_and_a_custom_strategy() -> None:
    data = {"A": _bars(_trending_walk(1200, seed=4)), "B": _bars(_trending_walk(1200, seed=5))}
    custom = scorecard(data, _ma_rules(), measure_start=31, label="ma_rules", placebo_runs=50)
    named = scorecard(data, "moving_average_crossover", params={"short_window": 5, "long_window": 30}, placebo_runs=50)
    for card in (custom, named):
        assert list(card["symbol"]) == ["A", "B"]
        assert card[["is_sharpe", "ho_sharpe", "is_sharpe_maker", "is_sharpe_zero_cost", "is_sharpe_funding_x3", "placebo_beaten_is", "years_positive"]].notna().all().all()
        assert (card["is_sharpe_zero_cost"] >= card["is_sharpe"]).all()  # costs only ever hurt
        assert (card["is_sharpe_funding_x3"] <= card["is_sharpe"] + 1e-9).all()
    assert custom["strategy"].iloc[0] == "ma_rules"
    spot = scorecard(data, _ma_rules(), measure_start=31, costs=CostSettings.spot(), placebo_runs=10)
    assert spot["is_sharpe_funding_x3"].isna().all()  # no funding on spot

    verdict = checks(custom)
    assert list(verdict.columns) == ["strategy", "symbol", *CHECKS, "passed"]
    assert verdict["passed"].str.endswith(f"/{len(CHECKS)}").all()


def test_checks_apply_the_documented_thresholds() -> None:
    card = pd.DataFrame([
        {"strategy": "s", "symbol": "good", "is_sharpe": 1.2, "is_buy_hold_sharpe": 1.0, "ho_sharpe": 0.7, "placebo_beaten_is": 0.95, "is_sharpe_funding_x3": 1.0, "is_sharpe_zero_cost": 1.4, "years_positive": 0.7},
        {"strategy": "s", "symbol": "bad", "is_sharpe": 1.2, "is_buy_hold_sharpe": 1.5, "ho_sharpe": 0.5, "placebo_beaten_is": 0.5, "is_sharpe_funding_x3": 0.6, "is_sharpe_zero_cost": 1.4, "years_positive": 0.5},
        {"strategy": "s", "symbol": "weak_but_cheap", "is_sharpe": 0.3, "is_buy_hold_sharpe": 0.2, "ho_sharpe": 0.2, "placebo_beaten_is": 0.9, "is_sharpe_funding_x3": 0.25, "is_sharpe_zero_cost": 0.35, "years_positive": 0.6},
    ])
    verdict = checks(card).set_index("symbol")
    assert verdict.loc["good", "passed"] == "5/5"
    assert verdict.loc["bad", list(CHECKS)].tolist() == [False, False, False, False, False]
    assert verdict.loc["weak_but_cheap", "survives_costs"]  # a low Sharpe is a quality problem, not a cost problem
