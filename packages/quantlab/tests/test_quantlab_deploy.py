"""From research to paper to live (plan Phase 7): specs, gates, capacity, parity, expectations, execution, factsheet."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pytest

from quantlab.deploy import capacity, execution, gates, monitor
from quantlab.deploy.spec import Expectations, backtest_spec, load_spec, spec_lab
from quantlab.reporting.factsheet import build_factsheet
from quantlab.tracking.store import ExperimentStore
from quantlab.validation.strategy_card import StrategyStandard

matplotlib.use("Agg")
DAYS = pd.bdate_range("2006-01-02", "2019-12-31")

SPEC = """
[strategy]
id = "demo_trend"
version = 1
title = "Demo trend"
hypothesis = "H97"
description = "Sign-free smooth trend on three synthetic markets."

[signal]
name = "trend_forecast"
params = { activation = "compound_sigmoid", speeds = [[10, 20], [20, 40], [40, 80]] }

[sizing]
target_vol = 0.10
span = 20

[engine]
band = 0.02
cost_bps = 1.0

[universe]
instruments = ["A", "B", "C"]

[expectations]
shrunk_sharpe = 0.3
max_drawdown = -0.2
volatility = 0.08

[capacity]
adv_usd = { A = 1e9, B = 1e9, C = 5e7 }
"""


def _bars(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-1, 1], len(DAYS) // 120 + 1), 120)[: len(DAYS)] * 0.0008  # trending stretches
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01)))
    return pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996, "close": close, "volume": 1.0}, index=DAYS)


@pytest.fixture()
def spec(tmp_path):
    path = tmp_path / "demo_trend.toml"
    path.write_text(SPEC)
    return load_spec(path)


@pytest.fixture()
def lab(spec, tmp_path):
    return spec_lab(spec, bars={name: _bars(i) for i, name in enumerate("ABC")}, store=ExperimentStore(tmp_path / "lab.db"))


def test_a_spec_is_strict_versioned_and_runs_the_library_signal(spec, lab, tmp_path) -> None:
    assert spec.key == "demo_trend@v1" and len(spec.hash) == 16
    bad = tmp_path / "bad.toml"
    bad.write_text(SPEC.replace("span = 20", "span = 20\nspam = 1"))
    with pytest.raises(ValueError, match="spam"):
        load_spec(bad)
    changed = tmp_path / "changed.toml"
    changed.write_text(SPEC.replace("band = 0.02", "band = 0.03"))
    assert load_spec(changed).hash != spec.hash
    result = backtest_spec(spec, lab=lab)
    forecast = spec.forecast(lab.panel.bars("A"))
    assert forecast.abs().max() <= 2.0 and result.pnl.std() > 0


def test_gates_block_until_signed_and_overrides_are_recorded(spec, lab) -> None:
    evidence = gates.evaluate(spec, lab=lab, sizes=(1e6, 1e8, 1e10))
    report = gates.promotion_check(spec, "paper", evidence=evidence, budget=gates.RiskBudget(capital_usd=1e6))
    assert "sign-off" in report.failing and set(report.table.index) == set(gates.REQUIRED)
    with pytest.raises(PermissionError, match="sign-off"):
        gates.promote(spec, "paper", evidence=evidence, approved_by="owner", overrides={"sign-off": "trust me"})
    gates.sign_off(spec, "paper", by="owner")
    failing = gates.promotion_check(spec, "paper", evidence=evidence, budget=gates.RiskBudget(capital_usd=1e6)).failing
    record = gates.promote(spec, "paper", evidence=evidence, approved_by="owner", budget=gates.RiskBudget(capital_usd=1e6),
                           overrides={gate: "test" for gate in failing})
    assert record["hash"] == spec.hash and gates.promoted("demo_trend", 1, "paper")["stage"] == "paper"
    assert gates.promoted("demo_trend", 1, "live") is None and gates.promoted("demo_trend", 2, "paper") is None
    live = gates.promotion_check(spec, "live", evidence=evidence)
    assert {"power", "holdout"} <= set(live.table.index[live.table["required"]])


def test_capacity_shrinks_with_size_and_shortfall_is_measured(spec, lab) -> None:
    curve = capacity.capacity_curve(lab, lab.signal(spec.signal_fn(), normalize=False, check=False), spec.capacity.adv_usd, sizes=(1e6, 1e9, 1e11))
    sharpe = curve["table"]["Sharpe"]
    assert sharpe.iloc[0] > sharpe.iloc[-1] and curve["capacity (participation limit)"] <= 1e11
    fills = pd.DataFrame({"instrument": ["A", "A", "B"], "side": [1, -1, 1], "quantity": [2, 1, 3], "fill_price": [100.1, 99.9, 50.0],
                          "decision_price": [100.0, 100.0, 50.0]})
    table = capacity.implementation_shortfall(fills, assumed_bps=1.0)
    assert table.at["A", "shortfall bps (notional-weighted)"] == pytest.approx(10.0) and table.at["B", "median bps"] == 0.0


def test_parity_finds_timing_and_sizing_differences() -> None:
    days = pd.bdate_range("2024-01-01", periods=120)
    rng = np.random.default_rng(1)
    research = pd.DataFrame({"A": np.sign(rng.normal(size=120)) * 0.3, "B": np.full(120, 0.2)}, index=days)
    same = monitor.parity(research, research)
    assert same["ok"]
    live = research.copy()
    live["A"] = research["A"].shift(1).fillna(0.0)  # a day late
    live["B"] = research["B"] * 1.6  # sized differently
    report = monitor.parity(live, research, live_pnl=pd.Series(rng.normal(0, 0.01, 120), index=days), research_pnl=pd.Series(rng.normal(0, 0.01, 120), index=days))
    assert "timing" in report["positions"].at["A", "flags"] and "sizing" in report["positions"].at["B", "flags"]
    assert not report["ok"] and report["pnl"]["flag"] != "ok"


def test_expectation_check_flags_decay_and_drawdown() -> None:
    days = pd.bdate_range("2024-01-01", periods=400)
    rng = np.random.default_rng(2)
    expected = Expectations(shrunk_sharpe=1.0, max_drawdown=-0.05, volatility=0.10)
    good = monitor.expectation_check(pd.Series(rng.normal(0.0004, 0.0063, 400), index=days), expected)
    assert good.at["Sharpe", "status"] in ("ok", "watch")
    bad = monitor.expectation_check(pd.Series(rng.normal(-0.002, 0.0063, 400), index=days), expected)
    assert bad.at["Sharpe", "status"] == "flag" and bad.at["drawdown", "status"] == "flag" and bad.attrs["overall"] == "flag"
    early = monitor.expectation_check(pd.Series(rng.normal(0, 0.0063, 30), index=days[:30]), expected)
    assert early.at["Sharpe", "status"] == "too early"


def test_orders_trade_whole_contracts_and_adapters_share_one_interface(tmp_path) -> None:
    orders = execution.plan_orders({"MES": 0.33, "MNQ": 0.0, "FXXP": 0.005}, {"MES": 2, "MNQ": 3}, equity=1_000_000,
                                   prices={"MES": 7700.0, "MNQ": 29000.0, "FXXP": 550.0}, multipliers={"MES": 5, "MNQ": 2, "FXXP": 50})
    by = {order.instrument: order for order in orders}
    assert by["MNQ"].reason == "close" and by["MNQ"].quantity == Decimal(3) and orders[0].reason == "close"
    assert by["MES"].quantity == Decimal(6) and by["MES"].side == 1  # 0.33 x 1m / 38,500 = 8.57 -> 8 held; 2 held
    assert "FXXP" not in by  # inside the band
    paper = execution.PaperAdapter(cost_bps=2.0)
    fills = paper.submit(orders)
    assert fills[0].fill_price == pytest.approx(29000.0 * (1 - 2e-4))
    desk = execution.OrderFileAdapter(tmp_path, book="demo")
    assert desk.submit(orders) == [] and desk.last_file.read_text().count("\n") == len(orders) + 1


def test_factsheet_has_every_section(spec, lab, tmp_path) -> None:
    evidence = gates.evaluate(spec, lab=lab, sizes=(1e6, 1e9))
    report = gates.promotion_check(spec, "live", evidence=evidence)
    path = build_factsheet(spec, evidence, gates=report, correlations=pd.Series({"trend": 0.9}), path=tmp_path / "f.html")
    page = Path(path).read_text()
    for section in ("Mechanism", "Evidence", "Risks", "Capacity", "Correlations", "Operational needs", "Status"):
        assert f"<h2>{section}</h2>" in page
