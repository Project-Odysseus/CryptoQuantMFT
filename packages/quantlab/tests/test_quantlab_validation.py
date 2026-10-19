"""Validation (plan Phase 4): cards on the lab ledger, power, purged CV, FDR, robustness, scenarios, holdout looks."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quantlab.engine.backtest import EngineConfig, Sizer, run_backtest
from quantlab.validation import crossval, fdr, ledger, power, robustness, scenarios
from quantlab.validation.signal_card import CardGate, require_card, signal_card
from quantlab.validation.strategy_card import StrategyStandard, card_backtest, strategy_card

DAYS = pd.bdate_range("2005-01-03", periods=12 * 252)
QUICK = StrategyStandard(bootstrap_runs=300, placebo_runs=100)


def _returns(sharpe: float, seed: int, days: pd.DatetimeIndex = DAYS) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(sharpe / math.sqrt(252) * 0.01, 0.01, len(days)), index=days)


def test_power_matches_simulation_and_flags_underpowered_tests() -> None:
    assert power.pass_probability(0.3, 27) == pytest.approx(power.simulated_pass_rate(0.3, 27, runs=1500), abs=0.04)
    assert 0.5 < power.detectable_sharpe(27) < 0.6 and power.detectable_sharpe(27, trials=50) > power.detectable_sharpe(27)
    assert power.years_needed(1.0) == pytest.approx(7.9, abs=0.6)
    report = power.power_report(27, 0.3)
    assert "warning" in report and "warning" not in power.power_report(40, 1.0)
    assert power.detectable_ic(1000) == pytest.approx((1.96 + 0.8416) / math.sqrt(997), rel=1e-3)


def test_walk_forward_and_purged_splits_never_train_on_the_test_labels() -> None:
    splits = crossval.walk_forward_splits(1000, test=100, min_train=300)
    assert [len(test) for _, test in splits] == [100] * 7 and all(train.max() < test.min() for train, test in splits)
    rolling = crossval.walk_forward_splits(1000, test=100, train=200, min_train=300)
    assert all(len(train) == 200 for train, _ in rolling)
    for train, test in crossval.purged_kfold(1000, folds=5, horizon=10, embargo=5):
        assert not np.intersect1d(train, test).size
        assert not ((train < test.min()) & (train + 9 >= test.min())).any()  # no label reaches into the test block
        assert not ((train > test.max()) & (train <= test.max() + 5)).any()  # embargo after it
    splits = crossval.cpcv_splits(600, groups=6, test_groups=2)
    assert len(splits) == 15
    index = pd.RangeIndex(600)
    result = crossval.cpcv(lambda train, test: pd.Series(1e-3 + np.sin(test) * 1e-2, index=index[test]), index, groups=6, test_groups=2)
    assert result["paths"].shape == (600, 5) and not result["paths"].isna().any().any()


def test_choosing_among_noise_is_caught_by_walk_forward_and_pbo() -> None:
    noise = pd.DataFrame({f"c{i}": _returns(0.0, seed=i) for i in range(20)})
    chosen = crossval.select_walk_forward(noise, crossval.walk_forward_splits(len(noise), test=252, min_train=756))
    assert chosen["best in hindsight Sharpe"] > chosen["oos Sharpe"] and chosen["pbo"] > 0.3
    real = noise.copy()
    real["c0"] = _returns(1.5, seed=99)
    assert crossval.select_walk_forward(real, crossval.walk_forward_splits(len(real), test=252, min_train=756))["oos Sharpe"] > 0.8


def test_cards_are_noted_once_and_the_fdr_keeps_the_real_one() -> None:
    real = strategy_card(_returns(1.5, 1), name="real", standard=QUICK, family="demo")
    assert real.verdict in ("evidence of an edge", "weak")
    strategy_card(_returns(1.5, 1), name="real", standard=QUICK, family="demo")  # identical input: not noted again
    for seed in range(2, 12):
        strategy_card(_returns(0.0, seed), name=f"noise {seed}", standard=QUICK, family="demo")
    assert len(ledger.entries("card")) == 11
    table = fdr.lab_fdr(0.10)
    assert table.iloc[0].name == "strategy:real" and bool(table.iloc[0]["discovery at 0.1"])
    assert table["discovery at 0.1"].sum() <= 2
    assert ledger.lab_summary().loc["demo", "cards"] == 11


def test_benjamini_hochberg_and_storey() -> None:
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, np.nan])
    q = fdr.benjamini_hochberg(p)
    assert q[0] == pytest.approx(0.008) and q[4] == pytest.approx(0.042 * 8 / 5) and np.isnan(q[-1])
    assert np.all(np.diff(q[:-1]) >= -1e-12)
    q_storey, pi0 = fdr.storey(np.r_[np.full(50, 0.001), np.linspace(0, 1, 50)])
    assert fdr.storey(np.array([0.01, 0.5, 0.9]))[1] == 1.0
    assert 0.4 < pi0 < 0.7 and np.nanmax(q_storey) <= 1.0


def test_card_backtest_carries_positions_and_attribution() -> None:
    rng = np.random.default_rng(5)
    returns = pd.DataFrame({"A": rng.normal(0.0003, 0.01, len(DAYS)), "B": rng.normal(0.0002, 0.01, len(DAYS))}, index=DAYS)
    result = run_backtest(pd.DataFrame(1.0, index=DAYS, columns=["A", "B"]), returns, sizer=Sizer(target_vol=None), config=EngineConfig(band=0.0))
    card = card_backtest(result, name="long both", standard=QUICK)
    assert "Timing placebo p" in card.table.index and card.table.at["Timing placebo p", "value"] != "no positions given"
    assert set(card.details["instruments"].columns) == {"A", "B"}
    import matplotlib
    matplotlib.use("Agg")
    assert len(card.plot().axes) == 9


def test_signal_card_gate_uses_the_lab_ledger() -> None:
    rng = np.random.default_rng(6)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(DAYS)))), index=DAYS)
    noise_signal = pd.Series(rng.normal(size=len(DAYS)), index=DAYS)
    card = signal_card(noise_signal, close, horizons=(5,), name="noise")
    assert card.verdict == "no evidence"
    with pytest.raises(CardGate):
        require_card("noise")
    with pytest.raises(CardGate):
        require_card("never carded")


def test_holdout_is_cut_and_opened_once_per_candidate() -> None:
    series = pd.Series(1.0, index=pd.bdate_range("2025-12-01", "2026-02-27"))
    assert ledger.holdout_cut(series).index.max() < pd.Timestamp("2026-01-01")
    with ledger.holdout_look("H99 final", approved_by="owner"):
        assert ledger.holdout_cut(series).index.max() > pd.Timestamp("2026-02-01")
    with pytest.raises(ledger.HoldoutRefused):
        with ledger.holdout_look("H99 final", approved_by="owner"):
            pass
    with ledger.holdout_look("H99 final", approved_by="owner", again="data vendor revised January"):
        pass
    assert [row["look"] for row in ledger.holdout_looks("H99 final")] == [1, 2]


def test_prereg_lock_detects_changes(tmp_path) -> None:
    path = ledger.prereg_path("H99")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hypothesis: TODO")
    with pytest.raises(ledger.PreregNotLocked):
        ledger.lock_prereg("H99")
    path.write_text("hypothesis: trend pays in bonds")
    ledger.lock_prereg("H99")
    assert ledger.require_prereg("H99")["version"] == 1
    path.write_text("hypothesis: trend pays in bonds, mostly")
    with pytest.raises(ledger.PreregNotLocked):
        ledger.require_prereg("H99")


def test_robustness_suite_on_a_plateau_and_a_spike() -> None:
    def run(lookback: int, threshold: float) -> pd.Series:
        edge = 1.0 if 20 <= lookback <= 60 else 0.0
        return _returns(edge, seed=lookback + int(threshold * 10))

    plateau = robustness.parameter_plateau(run, {"lookback": [10, 20, 40, 60, 80], "threshold": [0.0, 0.5, 1.0]}, chosen={"lookback": 40, "threshold": 0.5})
    assert plateau["chosen"]["plateau ratio"] > 0.6 and plateau["sweep"].shape[1] == 15
    costs = robustness.cost_sensitivity(lambda m: _returns(1.0, 1) - m * 0.0002)
    assert 1.0 < costs["breakeven multiplier"] < 5.0
    returns = _returns(1.0, 2)
    assert len(robustness.start_dates(returns)) == 9  # 12 years, each start leaving 3 and robustness.subperiods(returns).shape == (4, 3)
    labels = robustness.market_environments(_returns(0.5, 3))
    assert {"trend", "volatility"} <= set(robustness.environments(returns, labels).index.get_level_values(0))
    dependence = robustness.episode_dependence(returns)
    assert dependence["Sharpe without best months"].is_monotonic_decreasing
    assert robustness.cross_market(lambda name: _returns(1.0, hash(name) % 100), ["ES", "NQ"]).shape == (2, 2)


def test_scenarios_cover_only_known_windows_and_stress_hurts_a_long_book() -> None:
    days = pd.bdate_range("2006-01-02", "2012-12-31")
    rng = np.random.default_rng(8)
    market = pd.DataFrame({"A": rng.normal(0.0003, 0.01, len(days)), "B": rng.normal(0.0003, 0.01, len(days))}, index=days)
    table = scenarios.historical(market["A"], benchmark=market["B"])
    assert "2007-09 financial crisis" in table.index and "2020 Covid crash" not in table.index and "benchmark return" in table.columns
    held = pd.DataFrame(0.5, index=days, columns=["A", "B"])
    out = scenarios.stress(held, market)
    t = out["table"]
    assert t.at["volatility x2", "max drawdown"] < t.at["as it was", "max drawdown"]
    assert t.at["correlations to one", "max drawdown"] < t.at["as it was", "max drawdown"]
    assert out["gap loss"].min() < -0.04
