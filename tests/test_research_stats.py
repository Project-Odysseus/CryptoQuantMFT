"""Probabilistic and deflated Sharpe ratios (src/research/stats.py)."""

from __future__ import annotations

import numpy as np
import pytest

from src.research.stats import deflated_sharpe_ratio, expected_max_sharpe, probabilistic_sharpe_ratio, sharpe_per_period


def test_the_probabilistic_sharpe_grows_with_evidence() -> None:
    rng = np.random.default_rng(0)
    skilled = rng.normal(0.001, 0.01, 2000)  # a per-day Sharpe of about 0.1 (1.9 a year)
    assert probabilistic_sharpe_ratio(skilled[:100]) < probabilistic_sharpe_ratio(skilled) > 0.99
    assert probabilistic_sharpe_ratio(rng.normal(0.0, 0.01, 2000)) == pytest.approx(0.5, abs=0.35)
    assert np.isnan(probabilistic_sharpe_ratio([0.01, 0.02]))


def test_fat_tails_and_negative_skew_lower_the_confidence() -> None:
    rng = np.random.default_rng(1)
    normal = rng.normal(0.001, 0.01, 1000)
    crashes = normal.copy()
    crashes[::50] -= 0.05  # rare large losses, same count
    crashes += (normal.mean() - crashes.mean())  # same mean, so only the shape differs
    assert sharpe_per_period(crashes) < sharpe_per_period(normal)
    assert probabilistic_sharpe_ratio(crashes) < probabilistic_sharpe_ratio(normal)


def test_luck_rises_with_the_number_of_trials_and_deflates_the_winner() -> None:
    assert expected_max_sharpe(1, 0.01) == 0.0
    assert 0 < expected_max_sharpe(10, 0.01) < expected_max_sharpe(1000, 0.01)
    rng = np.random.default_rng(2)
    sharpes = [sharpe_per_period(rng.normal(0, 0.01, 750)) for _ in range(300)]  # 300 skill-less strategies
    luckiest = max(range(300), key=lambda i: sharpes[i])
    returns = np.random.default_rng(2)
    series = [returns.normal(0, 0.01, 750) for _ in range(300)][luckiest]
    assert probabilistic_sharpe_ratio(series) > 0.95  # looks significant on its own...
    assert deflated_sharpe_ratio(series, trials=300, sharpe_variance=float(np.var(sharpes))) < 0.6  # ...but not after the search
