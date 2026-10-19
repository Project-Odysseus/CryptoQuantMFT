"""Experiment store, hypothesis registry, charts and the report (plan Phase 5)."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pytest

from quantlab.data.quality import data_card
from quantlab.engine.backtest import EngineConfig, Sizer, run_backtest
from quantlab.engine.components import Targets
from quantlab.reporting import charts
from quantlab.reporting.report import build_report
from quantlab.tracking.store import STATUSES, ExperimentStore
from quantlab.validation.strategy_card import StrategyStandard, card_backtest

matplotlib.use("Agg")  # tests only; the package itself never chooses a backend
DAYS = pd.bdate_range("2008-01-02", periods=8 * 252)
QUICK = StrategyStandard(bootstrap_runs=200, placebo_runs=50)


@pytest.fixture(scope="module")
def result():
    rng = np.random.default_rng(0)
    returns = pd.DataFrame({"A": rng.normal(0.0004, 0.01, len(DAYS)), "B": rng.normal(0.0002, 0.012, len(DAYS))}, index=DAYS)
    target = np.sign(returns.rolling(20).mean()).fillna(0.0)
    parts = {"trend": target * 0.7, "carry": target * 0.3}
    return run_backtest(Targets(target, parts), returns, costs_bps=1.0, sizer=Sizer(0.10), config=EngineConfig(band=0.02))


def test_runs_are_recorded_with_metrics_cards_series_and_config_differences(tmp_path, result) -> None:
    store = ExperimentStore(tmp_path / "lab.db")
    store.register("H99", "Synthetic trend", status="pre-registered")
    for lookback in (20, 40):
        with store.run("H99", f"lookback {lookback}", config={"signal": {"lookback": lookback}, "costs": 1.0}, snapshots=["yahoo/^NDX/1d@abc"]) as run:
            run.log_result(result)
            run.log_card(card_backtest(result, name=f"H99 {lookback}", standard=QUICK, ledger=False))
    runs = store.runs("H99")
    assert list(runs["state"]) == ["finished", "finished"] and runs["Sharpe"].notna().all() and runs["verdict"].notna().all()
    pd.testing.assert_series_equal(store.series(1).rename(None), result.pnl.rename(None), check_freq=False, check_index_type=False)
    compared = store.compare([1, 2])
    assert "config: signal.lookback" in compared.index and "config: costs" not in compared.index
    detail = store.get(2)
    assert detail["snapshots"] == ["yahoo/^NDX/1d@abc"] and len(detail["cards"]) == 1 and detail["config"]["signal"]["lookback"] == 40
    with pytest.raises(RuntimeError):
        with store.run("H99", "broken"):
            raise RuntimeError("data missing")
    assert store.runs("H99")["state"].iloc[-1].startswith("failed: RuntimeError")


def test_hypothesis_registry_keeps_history_and_shows_the_lab_state(tmp_path) -> None:
    store = ExperimentStore(tmp_path / "lab.db")
    assert list(store.lab_state().columns)[:2] == ["title", "status"]  # an empty lab still has its columns
    store.register("H2", "second", status="idea")
    store.register("H10", "tenth", status="pre-registered")
    store.set_status("H2", "failed", reason="card: no evidence", decision="Reject.")
    with pytest.raises(ValueError):
        store.set_status("H2", "abandoned", reason="x")
    with pytest.raises(ValueError):
        store.set_status("H2", "failed", reason=" ")
    with pytest.raises(KeyError):
        store.set_status("H3", "failed", reason="x")
    assert list(store.history("H2")["status"]) == ["idea", "failed"]
    state = store.lab_state()
    assert list(state.index) == ["H10", "H2"] and state.at["H2", "decision"] == "Reject."  # ordered by status, then naturally
    assert set(state["status"]) <= set(STATUSES)


def test_store_schema_is_plain_sql_and_runs_on_any_connection(tmp_path) -> None:
    path = tmp_path / "other.db"
    store = ExperimentStore(connect=lambda: sqlite3.connect(path))
    store.register("H1", "one")
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0] == 1


def test_the_report_is_one_self_contained_page(tmp_path, result) -> None:
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.linspace(100, 200, len(DAYS)), "volume": 1.0}, index=DAYS)
    labels = pd.Series(np.where(np.arange(len(DAYS)) % 300 < 150, "TREND", "RANGE"), index=DAYS)
    card = card_backtest(result, name="synthetic", standard=QUICK, ledger=False)
    path = build_report(result, name="Synthetic trend", path=tmp_path / "report.html", definition="Sign of the 20-day mean.", config={"lookback": 20},
                        card=card, data_cards=[data_card(bars, name="A")], labels=labels, cost_run=lambda m: result, hypothesis="H99")
    page = Path(path).read_text()
    for section in ("Five-minute summary", "Definition", "Data", "Performance", "Costs and attribution", "Regimes", "Robustness",
                    "Scenarios and stress", "Strategy card", "Reproducibility", "Recommendation"):
        assert section in page, section
    assert page.count("data:image/png;base64") >= 8 and "<script" not in page and 'src="http' not in page  # nothing loaded from outside


def test_charts_draw_on_given_axes_and_the_package_never_picks_a_backend(result) -> None:
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 2)
    charts.equity({"s": result}, ax=axes[0, 0])
    charts.drawdown(result, ax=axes[0, 1])
    charts.attribution(result, by="instrument", ax=axes[1, 0])
    charts.parameter_surface(pd.DataFrame({"a": [1, 1, 2, 2], "b": [1, 2, 1, 2], "Sharpe": [0.1, 0.2, 0.3, 0.4]}), "a", "b", ax=axes[1, 1], chosen={"a": 2, "b": 1})
    assert len(charts.to_base64(figure)) > 1000
    package = Path(charts.__file__).parents[1]
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "use"
                 and getattr(getattr(node.func, "value", None), "id", "") == "matplotlib"]
        assert not calls, f"{path} selects a matplotlib backend"
