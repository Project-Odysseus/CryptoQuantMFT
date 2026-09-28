"""Benchmark-relative metrics on series with known answers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.benchmark import benchmark_metrics, benchmark_table, rolling_beta


def _bench(count: int = 2000, seed: int = 0) -> pd.Series:
    index = pd.date_range("2022-01-01", periods=count, freq="D", tz="UTC")
    return pd.Series(np.random.default_rng(seed).normal(0.001, 0.03, count), index=index)


def test_holding_the_benchmark_has_beta_one_and_no_alpha() -> None:
    bench = _bench()
    metrics = benchmark_metrics(bench, bench)
    assert metrics["beta"] == pytest.approx(1.0) and metrics["alpha_ann"] == pytest.approx(0.0, abs=1e-12)
    assert metrics["correlation"] == pytest.approx(1.0) and metrics["tracking_error_ann"] == pytest.approx(0.0)
    assert np.isnan(metrics["information_ratio"])
    assert metrics["up_capture"] == pytest.approx(1.0) and metrics["down_capture"] == pytest.approx(1.0)


def test_half_exposure_plus_constant_alpha_is_recovered() -> None:
    bench = _bench()
    daily_alpha = 0.0002
    strategy = 0.5 * bench + daily_alpha
    metrics = benchmark_metrics(strategy, bench)
    assert metrics["beta"] == pytest.approx(0.5)
    assert metrics["alpha_ann"] == pytest.approx(daily_alpha * 365)
    assert metrics["return_ann"] == pytest.approx(strategy.mean() * 365)


def test_a_long_flat_filter_captures_upside_not_downside() -> None:
    bench = _bench()
    strategy = bench.clip(lower=0.0)  # an oracle that is flat whenever the benchmark falls
    metrics = benchmark_metrics(strategy, bench)
    assert metrics["up_capture"] == pytest.approx(1.0) and metrics["down_capture"] == pytest.approx(0.0)


def test_alignment_uses_common_dates_only_and_short_samples_are_nan() -> None:
    bench = _bench()
    strategy = bench.iloc[100:300] * 2.0
    assert benchmark_metrics(strategy, bench)["periods"] == 200.0
    assert benchmark_metrics(strategy, bench)["beta"] == pytest.approx(2.0)
    assert np.isnan(benchmark_metrics(bench.iloc[:2], bench)["beta"])


def test_rolling_beta_and_table() -> None:
    bench = _bench(400)
    strategy = pd.concat([0.2 * bench.iloc[:200], 1.5 * bench.iloc[200:]])
    beta = rolling_beta(strategy, bench, window=60)
    assert beta.iloc[:59].isna().all()
    assert beta.iloc[150] == pytest.approx(0.2) and beta.iloc[-1] == pytest.approx(1.5)
    table = benchmark_table({"a": strategy, "b": bench}, bench)
    assert list(table.index) == ["a", "b"] and table.loc["b", "beta"] == pytest.approx(1.0)
