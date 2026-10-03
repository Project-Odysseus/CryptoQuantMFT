"""The book's risk model (src/portfolio/risk_model.py): covariance estimation, the stressed case, and what a book is exposed to."""

from __future__ import annotations

import json

import numpy as np
import pytest

from src.portfolio.risk_model import ReturnWindow, RiskEstimate, RiskModel, average_correlation, effective_bets

A, B, C = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD", "kraken_futures:SOL/USD"
PER_YEAR = 365.0


def _estimate(correlation: float, *, sigma: tuple[float, float] = (0.02, 0.02), stress: float | None = None) -> RiskEstimate:
    std = np.array(sigma)
    matrix = np.array([[1.0, correlation], [correlation, 1.0]]) * np.outer(std, std)
    stressed = None
    if stress is not None:
        lifted = max(correlation, stress)
        stressed = np.array([[1.0, lifted], [lifted, 1.0]]) * np.outer(std, std)
    return RiskEstimate((A, B), matrix, stressed, PER_YEAR)


def _correlated(count: int, correlation: float, *, seed: int = 0, sigma: float = 0.02) -> np.ndarray:
    rng = np.random.default_rng(seed)
    first = rng.normal(0.0, sigma, count)
    second = correlation * first + np.sqrt(1.0 - correlation**2) * rng.normal(0.0, sigma, count)
    return np.column_stack([first, second])


def _model(instruments: tuple[str, ...] = (A, B), **overrides: object) -> RiskModel:
    settings = {"halflife_bars": 500.0, "min_bars": 20, "periods_per_year": PER_YEAR, "shrinkage": 0.0, "stress_correlation": None}
    return RiskModel(instruments, **{**settings, **overrides})  # type: ignore[arg-type]


def test_the_model_recovers_volatility_and_correlation_from_returns() -> None:
    model = _model()
    for first, second in _correlated(4000, 0.6):
        model.step({A: first, B: second})
    estimate = model.estimate()
    assert estimate is not None
    assert estimate.correlation().loc[A, B] == pytest.approx(0.6, abs=0.05)
    assert estimate.instrument_volatility()[A] == pytest.approx(0.02 * np.sqrt(PER_YEAR), rel=0.08)


def test_there_is_no_estimate_until_an_instrument_has_enough_bars() -> None:
    model = _model(min_bars=20)
    for first, second in _correlated(19, 0.5):
        model.step({A: first, B: second})
    assert model.estimate() is None
    model.step({A: 0.01, B: 0.01})
    assert model.estimate() is not None


def test_a_new_listing_gets_the_median_variance_and_the_prior_correlation_not_zero_risk() -> None:
    model = _model((A, B, C), prior_correlation=0.5)
    for first, second in _correlated(300, 0.8):
        model.step({A: first, B: second, C: float("nan")})
    for _ in range(5):  # C has just listed: five bars, below min_bars
        model.step({A: 0.0, B: 0.0, C: 0.001})
    estimate = model.estimate()
    assert estimate is not None
    volatility = estimate.instrument_volatility()
    assert volatility[C] ** 2 == pytest.approx(np.median([volatility[A] ** 2, volatility[B] ** 2]))
    assert estimate.correlation().loc[A, C] == pytest.approx(0.5)


def test_shrinkage_pulls_every_correlation_towards_the_prior() -> None:
    plain, shrunk = _model(), _model(shrinkage=0.5, prior_correlation=0.0)
    for first, second in _correlated(2000, 0.8):
        plain.step({A: first, B: second})
        shrunk.step({A: first, B: second})
    assert shrunk.estimate().correlation().loc[A, B] == pytest.approx(0.5 * plain.estimate().correlation().loc[A, B])


def test_book_volatility_matches_hand_calculation() -> None:
    uncorrelated = _estimate(0.0)
    assert uncorrelated.volatility({A: 0.5, B: 0.5}) == pytest.approx(np.sqrt(0.5) * 0.02 * np.sqrt(PER_YEAR))
    assert _estimate(1.0).volatility({A: 0.5, B: 0.5}) == pytest.approx(0.02 * np.sqrt(PER_YEAR))
    assert _estimate(1.0).volatility({A: 0.5, B: -0.5}) == pytest.approx(0.0, abs=1e-9)
    assert uncorrelated.volatility({A: 0.5, "unknown:X/USD": 3.0}) == pytest.approx(0.5 * 0.02 * np.sqrt(PER_YEAR))  # not in the model


def test_stress_raises_a_long_books_risk_and_never_lowers_a_hedged_books() -> None:
    estimate = _estimate(0.2, stress=0.9)
    long_book, hedged = {A: 0.5, B: 0.5}, {A: 0.5, B: -0.5}
    assert estimate.stressed_volatility(long_book) > estimate.volatility(long_book)
    assert estimate.stressed_volatility(long_book) == pytest.approx(np.sqrt(0.5 + 0.5 * 0.9) * 0.02 * np.sqrt(PER_YEAR))
    assert estimate.stressed_volatility(hedged) == pytest.approx(estimate.volatility(hedged))  # a crash correlation would flatter the hedge
    assert _estimate(0.2).stressed_volatility(long_book) == pytest.approx(_estimate(0.2).volatility(long_book))  # no stress configured


def test_beta_exposure_is_the_book_in_benchmark_terms() -> None:
    estimate = _estimate(0.8, sigma=(0.02, 0.03))  # B's beta to A: 0.8 * 0.03 / 0.02 = 1.2
    assert estimate.betas(A) == pytest.approx({A: 1.0, B: 1.2})
    assert estimate.beta_exposure({A: 0.5, B: 0.5}, A) == pytest.approx(1.1)
    assert estimate.beta_exposure({A: 0.6, B: -0.5}, A) == pytest.approx(0.0)
    with pytest.raises(ValueError, match="benchmark"):
        estimate.betas(C)


def test_risk_contributions_sum_to_one_and_a_hedge_is_negative() -> None:
    estimate = _estimate(0.5, sigma=(0.02, 0.04))
    shares = estimate.risk_contributions({A: 0.5, B: 0.5})
    assert sum(shares.values()) == pytest.approx(1.0)
    assert shares[B] > shares[A]  # the more volatile leg carries more of the risk
    hedge = estimate.risk_contributions({A: 1.0, B: -0.1})
    assert hedge[B] < 0 and sum(hedge.values()) == pytest.approx(1.0)
    marginal = estimate.marginal_risk({A: 0.5, B: 0.5})
    assert 0.5 * marginal[A] == pytest.approx(shares[A])
    assert estimate.risk_contributions({A: 0.0, B: 0.0}) == {A: 0.0, B: 0.0}


def test_the_diversification_ratio_is_one_for_one_bet_and_higher_when_positions_offset() -> None:
    assert _estimate(1.0).diversification_ratio({A: 0.5, B: 0.5}) == pytest.approx(1.0)
    assert _estimate(0.0).diversification_ratio({A: 0.5, B: 0.5}) == pytest.approx(np.sqrt(2.0))
    assert np.isnan(_estimate(0.0).diversification_ratio({A: 0.0, B: 0.0}))


def test_a_restart_from_json_continues_exactly() -> None:
    returns = _correlated(400, 0.4, seed=3)
    straight, restarted = _model(shrinkage=0.2, stress_correlation=0.9), _model(shrinkage=0.2, stress_correlation=0.9)
    for index, (first, second) in enumerate(returns):
        straight.step({A: first, B: second})
        restarted.step({A: first, B: second})
        if index % 50 == 0:
            restarted = RiskModel.from_dict(json.loads(json.dumps(restarted.to_dict())))
    one, two = straight.estimate(), restarted.estimate()
    assert np.array_equal(one.covariance, two.covariance) and np.array_equal(one.stressed, two.stressed)
    assert restarted.bars_seen == 400


def test_the_estimate_uses_only_bars_already_stepped() -> None:
    returns = _correlated(200, 0.4, seed=5)
    model = _model()
    snapshots = []
    for first, second in returns:
        model.step({A: first, B: second})
        estimate = model.estimate()
        snapshots.append(None if estimate is None else estimate.covariance.copy())
    changed = returns.copy()
    changed[150:] *= 5.0  # a different future
    model = _model()
    for index, (first, second) in enumerate(changed[:150]):
        model.step({A: first, B: second})
        estimate = model.estimate()
        assert (estimate is None and snapshots[index] is None) or np.array_equal(estimate.covariance, snapshots[index])


def test_pairs_estimated_from_different_bars_still_give_a_valid_matrix() -> None:
    rng = np.random.default_rng(11)
    model = _model((A, B, C), min_bars=5, halflife_bars=20.0)
    for index in range(120):
        common = rng.normal(0.0, 0.02)
        row = {A: common + rng.normal(0.0, 0.002), B: -common + rng.normal(0.0, 0.002), C: common + rng.normal(0.0, 0.002)}
        if index % 3 == 0:
            row[A] = float("nan")
        if index % 3 == 1:
            row[B] = float("nan")
        if index % 3 == 2:
            row[C] = float("nan")
        model.step(row)
    estimate = model.estimate()
    assert np.linalg.eigvalsh(estimate.covariance).min() >= -1e-12
    assert estimate.volatility({A: 1.0, B: 1.0, C: -1.0}) >= 0.0


@pytest.mark.parametrize("overrides", [{"halflife_bars": 0.0}, {"min_bars": 1}, {"shrinkage": 1.5}, {"prior_correlation": 1.0}, {"stress_correlation": 1.0}])
def test_settings_that_cannot_work_are_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        _model(**overrides)


def test_effective_bets_and_average_correlation() -> None:
    assert effective_bets(np.eye(4)) == pytest.approx(4.0)
    assert effective_bets(np.ones((4, 4))) == pytest.approx(1.0)
    assert average_correlation(np.array([[1.0, 0.2, 0.4], [0.2, 1.0, 0.6], [0.4, 0.6, 1.0]])) == pytest.approx(0.4)
    assert np.isnan(average_correlation(np.ones((1, 1))))


def test_the_return_window_reports_how_alike_the_strategies_are_and_survives_a_restart() -> None:
    rng = np.random.default_rng(2)
    window = ReturnWindow(["trend_a", "trend_b", "basket", "idle"], length=100, min_bars=30)
    assert window.summary() is None
    for index in range(150):
        trend = rng.normal(0.0, 0.01)
        window.add({"trend_a": trend, "trend_b": trend + rng.normal(0.0, 0.002), "basket": rng.normal(0.0, 0.01), "idle": 0.0 if index % 2 else None})
        if index == 80:
            window = ReturnWindow.from_dict(json.loads(json.dumps(window.to_dict())))
    summary = window.summary()
    assert summary["bars"] == 100 and summary["units"] == 3  # the idle unit never moved
    assert summary["most_correlated"][:2] == ["trend_a", "trend_b"] and summary["most_correlated"][2] > 0.9
    assert 1.5 < summary["effective_bets"] < 2.5  # two trend sleeves and a basket are about two bets
    assert window.correlation().loc["trend_a", "basket"] == pytest.approx(0.0, abs=0.3)
