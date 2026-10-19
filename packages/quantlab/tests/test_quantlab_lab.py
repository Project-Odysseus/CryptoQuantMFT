"""The notebook interface (plan Phase 6): five-line idea tests, hindsight checks, modes, the book, speed."""

from __future__ import annotations

import time
import warnings

import matplotlib
import numpy as np
import pandas as pd
import pytest

from quantlab.lab import EXPLORATORY, Lab
from quantlab.tracking.store import ExperimentStore
from quantlab.validation import ledger

matplotlib.use("Agg")


def _bars(seed: int, days: pd.DatetimeIndex) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(days))))
    spread = np.abs(rng.normal(0, 0.004, len(days)))
    return pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread), "close": close, "volume": 1.0}, index=days)


@pytest.fixture()
def lab(tmp_path) -> Lab:
    days = pd.bdate_range("2008-01-01", "2019-12-31")
    return Lab(bars={f"M{i}": _bars(i, days) for i in range(6)}, store=ExperimentStore(tmp_path / "lab.db"), show=False)


def test_the_five_line_idea_test(lab) -> None:
    idea = lab.signal(lambda px: (px.close.rolling(20).mean() / px.close.rolling(100).mean() - 1).rank(axis=1, pct=True) - 0.5)
    assert idea.causal and idea.name == "idea 1" and idea.forecast.abs().max().max() <= 2.0
    report = lab.test(idea)
    assert report.strategy_card.verdict in ("no evidence", "weak", "evidence of an edge") and report.signal_card is not None
    assert {"trend", "breakout", "momentum", "reversion"} <= set(report.versus_book.index)
    combined = lab.combine(idea, "trend", overlay="h14_regime")
    assert {"overlay", "hard switch", "static blend", "exposure-matched static", "trend alone"} <= set(combined["table"].index)
    blend = lab.combine(idea, "momentum", weights={"idea 1": 0.3, "momentum": 0.7})
    assert "blend" in blend["table"].index
    with pytest.raises(ValueError):
        lab.combine(idea, "momentum", overlay="volatility")  # a non-H14 overlay needs explicit state weights
    book = lab.compare_with_book(idea)
    assert -1 <= book["table"]["correlation with the book"] <= 1 and set(book["correlations"].index) == {"trend", "breakout", "momentum", "reversion"}


def test_hindsight_is_caught(lab) -> None:
    with pytest.warns(UserWarning, match="uses the future"):
        leaky = lab.signal(lambda px: px.close.rank(pct=True), name="ranked over all dates")
    assert not leaky.causal
    with pytest.warns(UserWarning, match="uses the future"):
        lab.signal(lambda px: px.returns.shift(-1), name="tomorrow")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert lab.signal(lambda bars: bars["close"].pct_change(20), name="per bar", per_instrument=True).causal


def test_exploratory_runs_are_counted_but_never_trials(lab) -> None:
    idea = lab.signal(lambda px: np.sign(px.close.pct_change(60)), name="sign 60")
    lab.test(idea)
    lab.test(idea)
    assert lab.exploration_count() == 2 and EXPLORATORY in lab.store.hypotheses().index
    assert ledger.entries("trials") == [] and ledger.entries("card") == []


def test_confirmatory_needs_a_locked_prereg_and_counts_trials(tmp_path) -> None:
    days = pd.bdate_range("2008-01-01", "2026-06-30")
    bars = {f"M{i}": _bars(i, days) for i in range(5)}
    with pytest.raises(ledger.PreregNotLocked):
        Lab(bars=bars, hypothesis="H98", show=False, record=False)
    path = ledger.prereg_path("H98")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("H98: momentum on synthetic markets")
    ledger.lock_prereg("H98")
    lab = Lab(bars=bars, hypothesis="H98", store=ExperimentStore(tmp_path / "lab.db"), show=False)
    assert lab.panel.index.max() < pd.Timestamp("2026-01-01")  # the frozen holdout is cut
    lab.test("momentum")
    assert ledger.total_trials("H98") == 3 and len(ledger.entries("card")) >= 1  # the strategy test, plus the signal card's two horizons
    with lab.final_look(approved_by="owner"):
        assert lab.panel.index.max() > pd.Timestamp("2026-01-01")
    assert lab.panel.index.max() < pd.Timestamp("2026-01-01")
    with pytest.raises(ledger.HoldoutRefused):
        with lab.final_look(approved_by="owner"):
            pass


def test_rerunning_an_idea_takes_seconds(lab) -> None:
    lab.test("trend")  # the book is built once
    started = time.perf_counter()
    lab.test(lab.signal(lambda px: np.sign(px.close.pct_change(40)), name="sign 40"))
    assert time.perf_counter() - started < 10.0


def test_the_project_root_is_found_from_a_notebook_folder(tmp_path) -> None:
    from quantlab.lab import project_root

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.toml").write_text("")
    (tmp_path / "notebooks" / "templates").mkdir(parents=True)
    assert project_root(tmp_path / "notebooks" / "templates") == tmp_path.resolve()
    assert project_root(tmp_path.parent / "elsewhere") is None or project_root(tmp_path.parent / "elsewhere") != tmp_path.resolve()
