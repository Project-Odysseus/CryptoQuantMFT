"""The pairs book's target weights (src/research/pairs.py) on hand-made prices."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.pairs import pair_weights

DAYS = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")


def _prices() -> pd.DataFrame:
    """A and B move together for 30 days, then A jumps 10% on day 35 and comes back on day 40. C wanders on its own."""
    wave = 100.0 + np.sin(np.arange(60) / 3.0)
    a, b = wave.copy(), wave * 1.0005
    b[::2] *= 0.999  # a little noise, so the formation spread has a standard deviation
    a[35:40] *= 1.10
    c = 100.0 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.03, 60)))
    return pd.DataFrame({"A": a, "B": b, "C": c}, index=DAYS)


def _run(**settings):
    close = _prices()
    everything = pd.DataFrame(True, index=DAYS, columns=close.columns)
    volume = pd.DataFrame({"A": 3.0, "B": 2.0, "C": 1.0}, index=DAYS)
    return pair_weights(close, everything, volume, formation=30, trading=30, coins=3, pairs=1, **settings)


def test_the_closest_pair_is_traded_when_it_diverges_and_closed_when_it_comes_back() -> None:
    result = _run(delay=0)
    assert result.periods[0]["pairs"] == [("A", "B")] and result.periods[0]["start"] == DAYS[30]
    weights = result.weights
    assert (weights.iloc[:35] == 0).all().all()  # nothing before the divergence, and nothing during formation
    assert weights.loc[DAYS[35], ["A", "B", "C"]].tolist() == [-0.10, 0.10, 0.0]  # short the coin that ran ahead, long the other
    # A is back on day 40 but still a hair above B (B's noise); the spread is through zero at the next close
    assert (weights.loc[DAYS[35]:DAYS[40], "A"] == -0.10).all() and (weights.loc[DAYS[41]:] == 0).all().all()  # out once the gap has closed
    [trade] = result.trades.to_dict("records")
    assert (trade["a"], trade["b"], trade["side"], trade["opened"], trade["closed"], trade["days"], trade["converged"]) == ("A", "B", -1, DAYS[35], DAYS[41], 6, True)
    assert weights.sum(axis=1).abs().max() < 1e-12  # dollar-neutral every day


def test_a_decision_is_traded_one_day_later_by_default_and_never_uses_later_prices() -> None:
    delayed, immediate = _run(), _run(delay=0)
    pd.testing.assert_frame_equal(delayed.weights.iloc[1:].reset_index(drop=True), immediate.weights.iloc[:-1].reset_index(drop=True))
    close = _prices()
    changed = close.copy()
    changed.iloc[45:] *= 3.0  # the future changes
    everything = pd.DataFrame(True, index=DAYS, columns=close.columns)
    volume = pd.DataFrame({"A": 3.0, "B": 2.0, "C": 1.0}, index=DAYS)
    again = pair_weights(changed, everything, volume, formation=30, trading=30, coins=3, pairs=1)
    pd.testing.assert_frame_equal(again.weights.iloc[:45], delayed.weights.iloc[:45])  # and the past doesn't


def test_a_pair_still_open_on_the_last_day_is_closed_and_marked_unconverged_and_ineligible_coins_are_left_out() -> None:
    close = _prices()
    close.loc[DAYS[35]:, "A"] = close.loc[DAYS[35]:, "A"].where(close.index[35:] < DAYS[40], close.loc[DAYS[35]:, "A"] * 1.10)  # the gap never closes
    everything = pd.DataFrame(True, index=DAYS, columns=close.columns)
    volume = pd.DataFrame({"A": 3.0, "B": 2.0, "C": 1.0}, index=DAYS)
    result = pair_weights(close, everything, volume, formation=30, trading=30, coins=3, pairs=1, delay=0)
    [trade] = result.trades.to_dict("records")
    assert trade["closed"] == DAYS[59] and not trade["converged"] and (result.weights.loc[DAYS[59]] == 0).all()
    no_b = everything.copy()
    no_b["B"] = False
    assert pair_weights(close, no_b, volume, formation=30, trading=30, coins=3, pairs=1).periods == []  # fewer than `coins` tradable: no period
