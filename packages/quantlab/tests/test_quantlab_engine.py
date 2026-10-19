"""The research engine (quantlab.engine, quantlab.portfolio, quantlab.analytics): known answers on synthetic data."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import attribution, benchmark_stats, metrics, sharpe, trades
from quantlab.data import align
from quantlab.data.instruments import Instrument, InstrumentMaster
from quantlab.engine.backtest import EngineConfig, Limits, Sizer, run_backtest
from quantlab.engine.cache import cached
from quantlab.engine.components import Targets, apply_signal, combine_targets, signal
from quantlab.engine.returns import cost_table, excess_returns
from quantlab.portfolio.construction import allocation_scales, build_portfolio, to_contracts

DAYS = pd.bdate_range("2020-01-01", periods=12)
FLAT = EngineConfig(band=0.0, rolls_per_year=0.0, roll_bps=0.0)


def _frame(values, columns=("A",)) -> pd.DataFrame:
    return pd.DataFrame(np.column_stack([values] * len(columns)) if np.ndim(values) == 1 else values, index=DAYS[: len(values)], columns=list(columns))


def test_a_decision_at_t_trades_at_t_plus_1_and_earns_from_t_plus_2() -> None:
    target = _frame([0.0, 0.0, 1.0, 1.0, 1.0, 1.0])
    returns = _frame([0.01] * 6)
    result = run_backtest(target, returns, costs_bps=10.0, sizer=Sizer(target_vol=None), config=FLAT)
    assert list(result.decided["A"]) == [0, 0, 1, 1, 1, 1] and list(result.traded["A"]) == [0, 0, 0, 1, 0, 0]
    assert list(result.held["A"]) == [0, 0, 0, 0, 1, 1] and result.costs["A"].iloc[3] == pytest.approx(0.001)
    assert result.pnl.iloc[4] == pytest.approx(0.01) and result.pnl.iloc[3] == pytest.approx(-0.001)


def test_the_band_and_its_exit_to_zero_rule() -> None:
    target = _frame([2.0, 2.1, 0.4, 0.0])
    returns = _frame([0.0] * 4)
    banded = run_backtest(target, returns, sizer=Sizer(target_vol=None), config=EngineConfig(band=1.5))
    assert list(banded.decided["A"]) == [2.0, 2.0, 0.4, 0.0]  # a small move waits; going flat goes through even inside the band
    sticky = run_backtest(target, returns, sizer=Sizer(target_vol=None), config=EngineConfig(band=1.5, band_exits_to_zero=False))
    assert list(sticky.decided["A"]) == [2.0, 2.0, 0.4, 0.4]


def test_volatility_sizing_caps_and_position_limits() -> None:
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2015-01-01", periods=800)
    returns = pd.DataFrame({"A": rng.normal(0, 0.02, 800), "B": rng.normal(0, 0.005, 800)}, index=days)
    scale = Sizer(target_vol=0.10, span=20).scale(returns)
    assert scale["A"].iloc[-100:].mean() == pytest.approx(0.10 / (0.02 * np.sqrt(252)), rel=0.2)
    assert Sizer(target_vol=0.10, cap=1.0).scale(returns)["B"].max() == 1.0
    target = pd.DataFrame(1.0, index=days, columns=["A", "B"])
    limited = run_backtest(target, returns, sizer=Sizer(target_vol=None), limits=Limits(max_gross=1.0), config=FLAT)
    assert limited.decided.abs().sum(axis=1).max() == pytest.approx(1.0)


def test_costs_from_ticks_and_parts_attribution(tmp_path) -> None:
    days = pd.bdate_range("2020-01-01", periods=60)
    bars = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": np.linspace(100, 160, 60), "volume": 1.0}, index=days)
    panel = align({"F": bars})
    master = InstrumentMaster({"F": Instrument(id="F", name="f", asset_class="equity_index", kind="index", currency="USD", tick_size=0.25, cost_ticks=2.0)})
    costs = cost_table(panel, master)
    assert costs["F"].iloc[0] == pytest.approx(2 * 0.25 / 100 * 1e4)
    returns = excess_returns(panel, master)
    trend = pd.DataFrame({"F": 0.6}, index=days)
    other = pd.DataFrame({"F": 0.4}, index=days)
    bundle = combine_targets({"trend": trend, "other": other}, {"trend": 1.0, "other": 1.0})
    result = run_backtest(bundle, returns, costs_bps=costs, sizer=Sizer(target_vol=None), config=FLAT)
    parts = {name: frame.sum().sum() for name, frame in result.parts_pnl.items()}
    assert parts["trend"] == pytest.approx(0.6 * result.gross.sum().sum()) and sum(parts.values()) == pytest.approx(result.gross.sum().sum())
    table = attribution(result)
    assert table.loc["total (net)", "per year"] == pytest.approx(result.pnl.sum() / (60 / 252))


def test_excess_returns_subtract_the_short_rate_and_add_carry() -> None:
    days = pd.bdate_range("2020-01-01", periods=5)
    panel = align({"I": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 100.0, "volume": 1.0}, index=days)})
    master = InstrumentMaster({"I": Instrument(id="I", name="i", asset_class="equity_index", kind="index", currency="USD", carry_yield=0.02, rate_area="USA")})
    returns = excess_returns(panel, master, rates={"USA": pd.Series(0.05, index=days)})
    assert returns["I"].iloc[1:].round(12).unique().tolist() == [round((0.02 - 0.05) / 252, 12)]


def test_metrics_and_trades() -> None:
    held = pd.Series([0, 1, 1, 0, -1, -1, 0], dtype=float)
    net = pd.Series([0, 0.01, 0.02, 0, -0.01, 0.03, 0], dtype=float)
    table = trades(held, net)
    assert list(table["side"]) == [1, -1] and list(table["pnl"].round(4)) == [0.03, 0.02]
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2015-01-01", periods=1000)
    returns = pd.DataFrame({"A": rng.normal(0.0004, 0.01, 1000)}, index=days)
    result = run_backtest(pd.DataFrame({"A": 1.0}, index=days), returns, sizer=Sizer(target_vol=None), config=FLAT)
    m = metrics(result)
    assert m["Sharpe"] == pytest.approx(sharpe(result.pnl)) and m["time invested"] > 0.99 and m["short contribution"] == 0.0
    assert benchmark_stats(result.pnl, returns["A"])["beta"] == pytest.approx(1.0, abs=0.01)


def test_portfolio_allocation_is_causal_and_equal_by_default() -> None:
    rng = np.random.default_rng(2)
    days = pd.bdate_range("2018-01-01", periods=600)
    returns = pd.DataFrame({"A": rng.normal(0, 0.01, 600), "B": rng.normal(0, 0.03, 600)}, index=days)
    sleeves = {"a": pd.DataFrame({"A": 1.0}, index=days), "b": pd.DataFrame({"B": 1.0}, index=days)}
    equal = build_portfolio(sleeves, returns)
    assert (equal.scales.dropna() == 0.5).all().all() and equal.weights["A"].iloc[-1] == 0.5
    inverse = allocation_scales(pd.DataFrame({"a": returns["A"], "b": returns["B"]}), {"a": 1.0, "b": 1.0}, "inverse_vol")
    assert inverse["a"].iloc[-1] > 0.7  # the calmer sleeve gets more
    later = allocation_scales(pd.DataFrame({"a": returns["A"], "b": returns["B"]}).iloc[:400], {"a": 1.0, "b": 1.0}, "inverse_vol")
    pd.testing.assert_frame_equal(inverse.iloc[:400], later)  # appending data does not change past scales
    rp = allocation_scales(pd.DataFrame({"a": returns["A"], "b": returns["B"]}), {"a": 1.0, "b": 1.0}, "risk_parity")
    assert rp.iloc[-1].sum() == pytest.approx(1.0) and rp["a"].iloc[-1] > rp["b"].iloc[-1]


def test_whole_contracts_report_rounding() -> None:
    days = pd.bdate_range("2020-01-01", periods=2)
    weights = pd.DataFrame({"MES": [0.33, 0.01]}, index=days)
    out = to_contracts(weights, equity=10_000_000, prices=pd.DataFrame({"MES": [7744.5, 7744.5]}, index=days), multipliers={"MES": 5}, fx=10.8)
    assert out["contracts"]["MES"].tolist() == [7.0, 0.0] and out["rounded_to_zero"]["MES"].tolist() == [False, True]


def test_signals_from_functions_and_the_cache() -> None:
    days = pd.bdate_range("2020-01-01", periods=100)
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.arange(1, 101, dtype=float), "volume": 1.0}, index=days)
    panel = align({"A": bars, "B": bars * 2})
    mom = signal("mom_10", lambda b: np.sign(b["close"].pct_change(10)), warmup=10)
    values = apply_signal(mom, panel)
    assert values.iloc[:10].isna().all().all() and (values.iloc[20:] == 1.0).all().all()
    calls = []

    @cached
    def slow(frame: pd.DataFrame, window: int) -> pd.Series:
        calls.append(window)
        return frame["close"].rolling(window).mean()

    slow(bars, 5), slow(bars, 5), slow(bars, 6)
    assert calls == [5, 6]


def test_the_engine_is_fast_enough_for_a_large_panel() -> None:
    rng = np.random.default_rng(3)
    days = pd.bdate_range("1996-01-01", periods=30 * 252)
    returns = pd.DataFrame(rng.normal(0, 0.01, (len(days), 30)), index=days, columns=[f"I{i}" for i in range(30)])
    targets = Targets(pd.DataFrame(np.sign(rng.normal(size=returns.shape)), index=days, columns=returns.columns))
    started = time.perf_counter()
    for _ in range(20):
        run_backtest(targets, returns)
    assert time.perf_counter() - started < 20.0  # 20 signals x 30 instruments x 30 years
