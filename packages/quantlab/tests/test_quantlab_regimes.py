"""Signals, regime models, the synthetic bench, the regime card, overlays and conditional analytics (plan Phase 3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.conditional import after_changes, by_regime
from quantlab.data import align
from quantlab.engine.backtest import EngineConfig, Sizer, run_backtest
from quantlab.overlays.overlay import RegimeOverlay, benchmarks, static_weights
from quantlab.regimes.bench import bench, score_path, sweep
from quantlab.regimes.hmm import filter_probabilities, fit_hmm
from quantlab.regimes.models import FeatureBlendRegime, HMMRegime, PercentileRegime, VolatilityRegime, labels
from quantlab.regimes.simulate import RegimeProcess, simulate
from quantlab.signals import library as lib
from quantlab.validation.regime_card import RegimeStandard, regime_card


@pytest.fixture(scope="module")
def path() -> tuple[pd.DataFrame, pd.Series]:
    return simulate(RegimeProcess(), days=12 * 252, seed=7)


def test_every_library_signal_is_causal_and_on_the_forecast_scale(path) -> None:
    bars, _truth = path
    for name, factory in lib.CATALOG.items():
        signal = factory()
        full = signal(bars)
        cut = signal(bars.iloc[:1500])
        pd.testing.assert_series_equal(full.iloc[:1500], cut, check_names=False, obj=name)  # later data never changes the past
        assert full.iloc[: signal.warmup].isna().all(), name
        assert full.abs().max() <= 2.0 + 1e-9 and 0.2 < full.std() < 2.0, name
    momentum = lib.relative_momentum(pd.DataFrame({"A": bars["close"], "B": bars["close"] ** 0.5, "C": bars["close"] ** 1.5}))
    assert momentum.dropna().sum(axis=1).abs().max() < 1e-9


def test_the_hmm_recovers_two_volatility_states() -> None:
    rng = np.random.default_rng(0)
    truth = np.repeat(np.tile([0, 1], 10), 100)
    data = rng.normal(0, np.where(truth == 0, 0.5, 2.0))[:, None]
    params = fit_hmm(data, n_states=2)
    assert sorted(np.sqrt(params.variances[:, 0]).round(1)) == pytest.approx([0.5, 2.0], abs=0.15)
    probs = filter_probabilities(params, data)
    calm = int(np.argmin(params.variances[:, 0]))
    assert ((probs[:, calm] > 0.5) == (truth == 0)).mean() > 0.85
    np.testing.assert_allclose(filter_probabilities(params, data[:500]), probs[:500])  # filtered = causal


def test_regime_models_give_probabilities_that_do_not_change_with_later_data(path) -> None:
    bars, _truth = path
    for model in (FeatureBlendRegime(), FeatureBlendRegime(soft=True), PercentileRegime("er"), PercentileRegime("adx", soft=True), VolatilityRegime(),
                  HMMRegime(train_years=4, min_train=500)):
        full, cut = model(bars), model(bars.iloc[:2000])
        rows = full.dropna()
        assert len(rows) > 500 and np.allclose(rows.sum(axis=1), 1.0)
        pd.testing.assert_frame_equal(full.iloc[:2000], cut, check_dtype=False)


def test_the_bench_scores_a_perfect_and_a_useless_detector(path) -> None:
    _bars, truth = path
    perfect = score_path(truth.where(truth != "SHOCK"), truth)
    assert perfect["balanced accuracy"] == 1.0 and perfect["median lag (bars)"] == 0.0 and perfect["false switches a year"] == 0.0
    rng = np.random.default_rng(1)
    coin = score_path(pd.Series(rng.choice(["TREND", "RANGE"], len(truth)), index=truth.index), truth)
    assert abs(coin["balanced accuracy"] - 0.5) < 0.03 and coin["false switches a year"] > 50
    table = bench({"er": (PercentileRegime("er"), {"TREND": "TREND", "RANGE": "RANGE"})}, [simulate(RegimeProcess(), days=8 * 252, seed=s) for s in (1, 2)])
    assert table.loc["er", "balanced accuracy"] > 0.55  # a trend-efficiency detector finds simulated trends better than chance


def test_the_regime_card_finds_a_real_edge_and_rejects_a_fake_one() -> None:
    rng = np.random.default_rng(3)
    days = pd.bdate_range("2000-01-03", periods=5000)
    spell = np.repeat(rng.choice(["A", "B"], 5000 // 40 + 1), 40)[:5000]
    states = pd.Series(spell, index=days)
    noise_a, noise_b = rng.normal(0, 0.01, 5000), rng.normal(0, 0.01, 5000)
    drift = np.where(spell == "A", 0.001, -0.001)
    pnl_a, pnl_b = pd.Series(noise_a + drift, index=days), pd.Series(noise_b, index=days)
    # the label at t is known at t; it describes the coming bars only because spells persist
    probs = pd.DataFrame({"A": (states == "A").astype(float), "B": (states == "B").astype(float)})
    standard = RegimeStandard(runs=200, placebo_runs=200)
    card = regime_card(probs, pnl_a, pnl_b, state_a="A", state_b="B", standard=standard)
    assert card.verdict == "useful" and card.details["edge"] > 0
    fake = probs.sample(frac=1.0, random_state=0).set_axis(days)
    fake_card = regime_card(fake, pnl_a, pnl_b, state_a="A", state_b="B", standard=standard)
    assert fake_card.verdict in ("no evidence", "weak") and "A" in card.behaviour.index


def test_overlays_blend_by_probability_and_build_their_benchmarks(path) -> None:
    bars, _truth = path
    panel = align({"X": bars})
    overlay = RegimeOverlay({"trend": lib.ewmac(16, 64), "reversion": lib.zscore_reversion(40)}, PercentileRegime("er", soft=True),
                            {"TREND": {"trend": 1.0}, "RANGE": {"reversion": 1.0}})
    targets = overlay(panel)
    p = targets.diagnostics["p:TREND"]["X"]
    pd.testing.assert_series_equal(targets.diagnostics["weight:trend"]["X"].where(p.notna()).dropna(), p.dropna(), check_names=False)
    assert set(targets.parts) == {"trend", "reversion"} and targets.target.abs().max().max() <= 1.0
    assert static_weights(overlay) == {"trend": 0.5, "reversion": 0.5}
    candidates = benchmarks(overlay, panel, primary=targets)
    assert set(candidates) == {"static blend", "hard switch", "exposure-matched static", "trend alone", "reversion alone"}
    returns = bars["close"].pct_change().to_frame("X")
    results = {name: run_backtest(t, returns, costs_bps=1.0, sizer=Sizer(0.10), config=EngineConfig(band=0.02)) for name, t in {"overlay": targets, **candidates}.items()}
    regime = labels(PercentileRegime("er")(bars))
    table = by_regime({name: r.pnl for name, r in results.items()}, regime)
    assert ("overlay", "Sharpe") in table.index and ("all", "share of bars") in table.index
    assert after_changes({"overlay": results["overlay"].pnl}, regime).shape == (1, 4)


def test_noise_hides_regimes_and_a_break_changes_volatility() -> None:
    models = {"er": (PercentileRegime("er"), {"TREND": "TREND", "RANGE": "RANGE"})}
    table = sweep(models, RegimeProcess(), "noise", [0.5, 3.0], years=8, seeds=[1, 2])
    assert table.loc[0.5, "er"] > table.loc[3.0, "er"]
    bars, _truth = simulate(RegimeProcess(break_at=0.5, break_volatility=3.0), days=2000, seed=4)
    returns = np.log(bars["close"]).diff()
    assert returns.iloc[1000:].std() > 2.0 * returns.iloc[:1000].std()
