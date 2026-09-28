"""Tests for the hypothesis-research harness: holdout lock, point-in-time joins, next-open fills, stops, statistics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import governance, pit
from src.research.bar_engine import funding_by_bar, performance, run_targets, simulate_trade
from src.research.costs import VENUES, vol_ratio
from src.research.inference import (
    block_bootstrap_ci,
    deflated_sharpe,
    driscoll_kraay_ols,
    family_trials,
    log_trial,
    matched_baseline,
    newey_west_ols,
)


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CQM_TRIAL_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("CQM_FINAL_HOLDOUT", raising=False)


def _frame(start: str, periods: int, freq: str = "D") -> pd.DataFrame:
    times = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame({"timestamp": times, "available_at": times, "value": np.arange(periods, dtype=float)})


def _bars(opens: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(opens), freq="h", tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs or opens, "low": lows or opens, "close": opens}, index=index)


# --- holdout lock ---------------------------------------------------------------------------------------------------

def test_lock_drops_holdout_rows() -> None:
    frame = _frame("2025-12-20", 30)
    locked = pit.lock(frame, label="test")
    assert pd.to_datetime(locked["available_at"]).max() < pit.holdout_start()
    assert len(locked) == 12  # 2025-12-20 .. 2025-12-31


def test_lock_refuses_explicit_holdout_end() -> None:
    with pytest.raises(pit.HoldoutLocked):
        pit.lock(_frame("2025-12-20", 30), label="test", end="2026-03-01")


def test_lock_opens_only_when_unlocked_and_logs_the_look() -> None:
    frame = _frame("2025-12-20", 30)
    with governance.final_holdout("unit test"):
        opened = pit.lock(frame, label="unit-test-series", end="2026-01-10")
    assert pd.to_datetime(opened["available_at"]).max() == pd.Timestamp("2026-01-10", tz="UTC")
    looks = [entry for entry in governance.ledger_entries() if entry["type"] == "final_holdout_look"]
    assert looks and looks[-1]["data"] == "unit-test-series"
    assert len(pit.lock(frame, label="test")) == 12  # locked again after the block


# --- point-in-time join -----------------------------------------------------------------------------------------------

def test_asof_never_uses_rows_available_after_decision() -> None:
    rng = np.random.default_rng(1)
    available = pd.to_datetime("2024-01-01", utc=True) + pd.to_timedelta(np.sort(rng.integers(0, 10_000, 300)), unit="min")
    series = pd.DataFrame({"available_at": available, "value": np.arange(300.0)})
    decisions = pd.date_range("2024-01-01", periods=100, freq="97min", tz="UTC")
    joined = pit.asof(series, decisions, ["value"])
    for decision, value in joined["value"].items():
        if np.isfinite(value):
            assert series.loc[series["value"] == value, "available_at"].iloc[0] <= decision
            later = series[(series["available_at"] <= decision)]
            assert value == later["value"].iloc[-1]


# --- engine -----------------------------------------------------------------------------------------------------------

def test_targets_fill_at_next_open() -> None:
    bars = _bars([100, 110, 121, 121])
    targets = pd.Series([1.0, 0.0, 0.0, 0.0], index=bars.index)  # decided at the close of bar 0
    run = run_targets(bars, targets, cost_per_side=0.0)
    assert run.frame["position"].tolist() == [0.0, 1.0, 0.0]
    assert run.gross.iloc[1] == pytest.approx(0.1)  # open 1 -> open 2
    assert run.gross.sum() == pytest.approx(0.1)


def test_shifting_signal_forward_one_bar_changes_the_result() -> None:
    rng = np.random.default_rng(3)
    opens = list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 500))))
    bars = _bars(opens)
    returns = pd.Series(np.array(opens[1:] + [np.nan]) / np.array(opens) - 1, index=bars.index)
    peeking = np.sign(returns).fillna(0.0)  # a signal that knows the next bar's return
    honest = run_targets(bars, peeking, cost_per_side=0.0).gross.sum()
    cheat = run_targets(bars, peeking.shift(-1).fillna(0.0), cost_per_side=0.0).gross.sum()
    assert cheat > 1.0 and abs(honest) < cheat / 3  # the engine only rewards the look-ahead version, so its lag works


def test_costs_and_funding_are_charged() -> None:
    bars = _bars([100, 100, 100, 100, 100])
    targets = pd.Series([1.0, 1.0, -1.0, 0.0, 0.0], index=bars.index)
    settlements = pd.DataFrame({"timestamp": [bars.index[2], bars.index[3] + pd.Timedelta(minutes=30)], "rate": [0.001, 0.002]})
    run = run_targets(bars, targets, cost_per_side=0.0005, funding=settlements)
    assert run.frame["traded"].tolist() == [0.0, 1.0, 0.0, 2.0]
    assert run.frame["cost"].sum() == pytest.approx(0.0015)
    # settlement at the end of bar 1 is paid by the long held in bar 1; the one inside bar 3 is received by the short
    assert run.frame["funding"].tolist() == pytest.approx([0.0, 0.001, 0.0, -0.002])


def test_funding_by_bar_assigns_boundary_settlement_to_ending_bar() -> None:
    opens = pd.date_range("2024-01-01", periods=3, freq="8h", tz="UTC")
    rates = funding_by_bar(opens, pd.DataFrame({"timestamp": [opens[1]], "rate": [0.01]}))
    assert rates.tolist() == [0.01, 0.0, 0.0]


def test_stop_uses_intrabar_low_and_gaps_fill_at_open() -> None:
    bars = _bars([100, 101, 102, 103, 104], lows=[100, 101, 94, 103, 104])
    stopped = simulate_trade(bars, 0, 1, 4, stop=95.0)
    assert stopped["stopped"] and stopped["gross"] == pytest.approx(-0.05)  # closes never went below 95
    gapped = simulate_trade(_bars([100, 101, 90, 103, 104], lows=[100, 101, 88, 103, 104]), 0, 1, 4, stop=95.0)
    assert gapped["gross"] == pytest.approx(-0.10)  # opened through the stop
    short = simulate_trade(_bars([100, 99, 98, 97, 96], highs=[100, 99, 106, 97, 96]), 0, -1, 4, stop=105.0)
    assert short["stopped"] and short["gross"] == pytest.approx(-0.05)
    held = simulate_trade(bars.assign(low=bars["open"]), 0, 1, 4, stop=95.0, cost_per_side=0.001)
    assert not held["stopped"] and held["net"] == pytest.approx(0.04 - 0.002)


def test_venue_costs_scale_with_volatility_and_multiplier() -> None:
    venue = VENUES["kraken_perp"]
    calm, wild = venue.per_side(1.0), venue.per_side(3.0)
    assert calm == pytest.approx(0.0005 + 4e-4)
    assert wild > calm and venue.per_side(1.0, multiplier=2) == pytest.approx(2 * calm)
    assert venue.per_side(100.0) == venue.per_side(5.0)  # clipped
    closes = pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.01, 400))))
    assert vol_ratio(closes, window=24, normal_window=200).between(0.5, 5.0).all()


def test_performance_metrics() -> None:
    index = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    stats = performance(pd.Series([0.1, -0.5, 0.2, 0.0], index=index), periods_per_year=365)
    assert stats["max_drawdown"] == pytest.approx(0.5)


# --- statistics -------------------------------------------------------------------------------------------------------

def test_newey_west_matches_ols_coefficients_and_widens_with_overlap() -> None:
    rng = np.random.default_rng(7)
    x = rng.normal(size=2000)
    y = 0.5 * x + rng.normal(size=2000)
    result = newey_west_ols(y, x, lags=0)
    assert result.loc["x1", "coef"] == pytest.approx(np.polyfit(x, y, 1)[0])
    noise = rng.normal(size=2010)
    overlapping = np.convolve(noise, np.ones(10), "valid")[:2000]  # a 10-period overlapping sum
    signal = np.convolve(rng.normal(size=2010), np.ones(10), "valid")[:2000]
    naive, hac = newey_west_ols(overlapping, signal, lags=0), newey_west_ols(overlapping, signal, lags=9)
    assert hac.loc["x1", "se"] > 1.5 * naive.loc["x1", "se"]


def test_driscoll_kraay_runs_on_a_panel() -> None:
    rng = np.random.default_rng(2)
    dates = np.repeat(np.arange(300), 3)
    common = np.repeat(rng.normal(size=300), 3)
    x = rng.normal(size=900)
    frame = pd.DataFrame({"t": dates, "x": x, "y": 0.3 * x + common + rng.normal(size=900) * 0.1})
    result = driscoll_kraay_ols(frame, "y", ["x"], time="t", lags=2)
    assert result.loc["x", "coef"] == pytest.approx(0.3, abs=0.1) and result.loc["x", "dates"] == 300


def test_block_bootstrap_ci_covers_the_mean() -> None:
    values = np.random.default_rng(4).normal(1.0, 1.0, 500)
    estimate, low, high = block_bootstrap_ci(values, block=10, runs=500)
    assert low < 1.0 < high and low < estimate < high


def test_matched_baseline_uses_similar_volatility_only() -> None:
    matching = np.r_[np.full(100, 1.0), np.full(100, 3.0)]
    outcome = np.r_[np.zeros(100), np.ones(100)]
    exclude = np.zeros(200, dtype=bool)
    exclude[150] = True
    means, drawn = matched_baseline([150], outcome, matching, exclude=exclude, samples=10)
    assert means[0] == 1.0 and 150 not in drawn[0] and all(p >= 100 for p in drawn[0])


def test_trial_ledger_and_deflated_sharpe_by_family() -> None:
    returns = pd.Series(np.random.default_rng(5).normal(0.001, 0.01, 1000))
    for i in range(3):
        log_trial("HX", "unit", {"i": i}, data_range="x", gross_sharpe=1.0, net_sharpe=0.8, net_sharpe_2x=0.5, trades=10, sharpe_per_period_net=0.05 + i / 100)
    assert family_trials("HX")[0] == 3
    result = deflated_sharpe(returns, "HX")
    assert result["family_trials"] == 3 and result["global_trials"] >= 3
    assert 0.0 <= result["dsr_global"] <= result["dsr_family"] <= 1.0
