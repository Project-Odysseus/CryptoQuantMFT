"""Tests for the research toolkit: catalog, runs, sweeps, summaries and plots."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
import numpy as np
import pytest

matplotlib.use("Agg")

from src.research.engine import apply_funding
from src.research import CATALOG, CostSettings, build_strategy, catalog_table, compare, plot_heatmap, plot_run, run_strategy, summarize, sweep
from src.storage.bar_aggregator import OHLCVBar


def _random_walk(seed: int, count: int = 400) -> list[OHLCVBar]:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, count)))
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        OHLCVBar(
            exchange="mock",
            symbol=f"SYM{seed}/EUR",
            interval_seconds=14400,
            timestamp=start + timedelta(hours=4 * index),
            open=close,
            high=close * 1.004,
            low=close * 0.996,
            close=close,
            volume=float(rng.uniform(1.0, 3.0)),
        )
        for index, close in enumerate(closes)
    ]


def _always_long(history, index, current_bar) -> int:
    return 1


def test_every_catalog_grid_combo_builds_against_its_factory() -> None:
    """A typo in a catalog grid or default should fail here, not halfway through a sweep."""
    for spec in CATALOG.values():
        for combo in spec.combos():
            assert callable(build_strategy(spec.name, **combo))
            assert spec.warmup_bars(combo) >= 2
    assert set(catalog_table()["strategy"]) == set(CATALOG)


def test_build_strategy_rejects_unknown_parameters() -> None:
    """Unlike resolve_strategy, a misspelled parameter must raise instead of silently using defaults."""
    with pytest.raises(TypeError, match="does not take"):
        build_strategy("donchian_breakout", entry_windw=20)


def test_build_strategy_long_only_wrapper_suppresses_shorts() -> None:
    """long_only=True should turn a short signal into flat."""
    falling = _random_walk(1)[:60]
    for bar in falling[40:]:
        bar.close = bar.low = bar.high = 50.0
    short_strategy = build_strategy("donchian_breakout", entry_window=10, exit_window=5)
    long_only_strategy = build_strategy("donchian_breakout", entry_window=10, exit_window=5, long_only=True)
    assert short_strategy(falling[:41], 40, falling[40]) == -1
    assert long_only_strategy(falling[:41], 40, falling[40]) == 0


def test_run_strategy_segments_match_buy_and_hold_for_an_always_long_strategy() -> None:
    """With no costs, holding from bar 1 should reproduce buy-and-hold over each segment exactly."""
    bars = _random_walk(2)
    run = run_strategy(bars, _always_long, costs=CostSettings(fee_pct=0.0, slippage_bps=0.0), holdout_fraction=0.25, measure_start=50)

    assert run.split_index == 300
    assert run.metrics["in_sample"]["bars"] == 250
    assert run.metrics["holdout"]["bars"] == 100
    for segment in ("in_sample", "holdout"):
        metrics = run.metrics[segment]
        assert metrics["return"] == pytest.approx(metrics["buy_hold_return"])
        assert metrics["sharpe"] == pytest.approx(metrics["buy_hold_sharpe"])
    assert run.metrics["in_sample"]["exposure"] == 1.0
    # The backtester force-closes on the final bar, so the last holdout bar is flat.
    assert run.metrics["holdout"]["exposure"] == pytest.approx(0.99)


def test_run_strategy_costs_reduce_returns() -> None:
    """The same strategy should earn less once fees and slippage are charged."""
    bars = _random_walk(3)
    free = run_strategy(bars, "moving_average_crossover", costs=CostSettings(fee_pct=0.0, slippage_bps=0.0))
    costly = run_strategy(bars, "moving_average_crossover", costs=CostSettings())
    assert free.metrics["in_sample"]["trades"] > 0
    assert costly.metrics["in_sample"]["return"] < free.metrics["in_sample"]["return"]


def test_run_strategy_refuses_when_warmup_eats_the_in_sample_period() -> None:
    """A clear error beats silently measuring nothing."""
    with pytest.raises(ValueError, match="in-sample bars"):
        run_strategy(_random_walk(4)[:60], "trend_tstat", params={"window": 96})


def test_sweep_and_summarize_produce_one_row_per_combo_symbol_and_side() -> None:
    """Rows = combos x symbols x side modes; the summary has one row per strategy/side."""
    data = {"A/EUR": _random_walk(5), "B/EUR": _random_walk(6)}
    grid = {"entry_window": [10, 20], "exit_window": [5, 10]}
    results = sweep(data, "donchian_breakout", grid=grid)

    assert len(results) == 4 * 2 * 2
    assert results["is_from"].nunique() == 1  # every combo measured over the same dates
    summary = summarize(results)
    assert len(summary) == 2
    row = summary.iloc[0]
    assert row["combos"] == 4
    assert 0.0 <= row["share_positive_is"] <= 1.0
    assert row["best_params_key"] in set(results["params"])
    assert set(json.loads(row["best_params"])) == {"entry_window", "exit_window"}
    assert np.isfinite(row["best_neighbors_is"])


def test_sweep_accepts_a_custom_factory_with_an_explicit_grid() -> None:
    """Research on a strategy that isn't registered anywhere yet."""

    def threshold_factory(level: float):
        def strategy(history, index, current_bar):
            return 1 if current_bar.close > level else 0

        return strategy

    results = sweep({"A/EUR": _random_walk(7)}, threshold_factory, grid={"level": [90.0, 110.0]}, long_only=False)
    assert list(results["strategy"].unique()) == ["threshold_factory"]
    assert sorted(results["p_level"]) == [90.0, 110.0]


def test_compare_runs_catalog_defaults_on_a_common_start() -> None:
    """compare() runs each named strategy once per symbol and side, all measured from the same bar."""
    results = compare({"A/EUR": _random_walk(8)}, ["moving_average_crossover", "rsi_reversion"], long_only=True)
    assert set(results["strategy"]) == {"moving_average_crossover", "rsi_reversion"}
    assert results["is_from"].nunique() == 1


def test_plots_render_to_files(tmp_path: Path) -> None:
    """Smoke test: both chart types write a PNG."""
    data = {"A/EUR": _random_walk(9)}
    results = sweep(data, "keltner_breakout", grid={"window": [10, 20], "atr_multiplier": [1.0, 2.0]}, long_only=False)
    plot_heatmap(results, "keltner_breakout", path=tmp_path / "heatmap.png")
    plot_run(run_strategy(data["A/EUR"], "keltner_breakout"), path=tmp_path / "run.png")
    assert (tmp_path / "heatmap.png").stat().st_size > 0
    assert (tmp_path / "run.png").stat().st_size > 0


def test_cost_presets_match_the_documented_figures() -> None:
    """Spot and perp presets carry the fee, slippage and funding numbers the docs quote."""
    assert CostSettings.spot().round_trip_pct == pytest.approx(1.0)
    assert CostSettings.spot(maker=True).round_trip_pct == pytest.approx(0.5)
    perp = CostSettings.perp()
    assert (perp.fee_pct, perp.slippage_bps, perp.funding_pct_per_day) == (0.05, 5.0, 0.03)
    assert perp.round_trip_pct == pytest.approx(0.2)
    assert CostSettings.perp(maker=True).round_trip_pct == pytest.approx(0.04)


def test_funding_is_paid_by_longs_and_received_by_shorts() -> None:
    """A positive funding rate should cost a long and pay a short by the same amount per bar held."""
    bars = _random_walk(11)
    free = CostSettings(fee_pct=0.0, slippage_bps=0.0)
    funded = CostSettings(fee_pct=0.0, slippage_bps=0.0, funding_pct_per_day=0.10)

    def short_always(history, index, current_bar) -> int:
        return -1

    long_base = run_strategy(bars, _always_long, costs=free, measure_start=50).metrics["in_sample"]["return"]
    long_funded = run_strategy(bars, _always_long, costs=funded, measure_start=50).metrics["in_sample"]["return"]
    short_base = run_strategy(bars, short_always, costs=free, measure_start=50).metrics["in_sample"]["return"]
    short_funded = run_strategy(bars, short_always, costs=funded, measure_start=50).metrics["in_sample"]["return"]

    assert long_funded < long_base
    assert short_funded > short_base
    # 230 in-sample 4h bars (400 - 120 holdout - 50 warmup) is ~38.3 days at 0.10%/day, so ~3.8% of notional in log terms either way.
    assert np.log1p(long_base) - np.log1p(long_funded) == pytest.approx(0.0383, abs=0.001)
    assert np.log1p(short_funded) - np.log1p(short_base) == pytest.approx(0.0383, abs=0.001)


def test_zero_funding_leaves_results_untouched() -> None:
    """The default (no funding) must return the very same result object."""
    result = run_strategy(_random_walk(12), _always_long, costs=CostSettings(0.0, 0.0), measure_start=50).result
    assert apply_funding(result, 0.0, interval_seconds=14400) is result
