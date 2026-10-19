"""quantlab's engine reproduces the recorded study numbers (docs/quantlab_migration.md, golden numbers).

Runs the ported H14 strategy (quantlab.strategies.regime_overlay) through the one engine (quantlab.engine) on the
H14 study's own cached data, and the H13 forecast likewise, and compares with packages/quantlab/tests/golden.
Skipped when the cached study data is not on this machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import metrics, sharpe
from quantlab.data import align
from quantlab.data.panel import asof_daily
from quantlab.engine.backtest import EngineConfig, Sizer, run_backtest
from quantlab.engine.components import Targets
from quantlab.strategies.regime_overlay import TRANSITION, OverlayConfig, RegimeOverlayStrategy

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "packages/quantlab/tests/golden/studies.json").read_text())
RATES = ROOT / "data/research_cache/multi_asset/rates.csv"


def _monthly_rate(area: str) -> pd.DataFrame:
    """The H11/H14 OECD cache as a point-in-time series: a month's rate known from the next month."""
    raw = pd.read_csv(RATES, index_col=0)
    months = pd.PeriodIndex(raw.index, freq="M")
    frame = pd.DataFrame({"value": raw[area].to_numpy() / 100.0}, index=months.to_timestamp())
    frame["available_at"] = (months + 1).to_timestamp()
    return frame.dropna()


def _excess(close: pd.Series, *, dividend: float, area: str) -> pd.Series:
    days = pd.bdate_range(close.index.min(), close.index.max())
    level = close.reindex(close.index.union(days)).ffill().reindex(days)
    rate = asof_daily(_monthly_rate(area), days)
    return level.pct_change(fill_method=None) + (dividend - rate) / 252.0


@pytest.fixture(scope="module")
def h14():
    path = ROOT / "data/research_cache/h14/NDX.csv"
    if not path.exists() or not RATES.exists():
        pytest.skip("H14's cached data is not on this machine")
    raw = pd.read_csv(path, index_col=0, parse_dates=True)
    raw = raw[raw.index < "2026-01-01"]
    days = pd.bdate_range(raw.index.min(), raw.index.max())
    bars = raw.reindex(raw.index.union(days)).reindex(raw.index)  # H14 computes on the trading days it has
    returns = _excess(raw["close"], dividend=0.009, area="USA").reindex(raw.index)
    panel = align({"NDX": bars[["open", "high", "low", "close", "volume"]]})
    config = OverlayConfig.from_toml(str(ROOT / "research/h14_regime_switch.toml"))
    return panel, returns.to_frame("NDX"), config


def _run(panel, returns, strategy_or_targets) -> object:
    targets = strategy_or_targets(panel) if callable(strategy_or_targets) else strategy_or_targets
    return run_backtest(targets, returns, costs_bps={"NDX": 1.5}, sizer=Sizer(target_vol=0.10, span=20, cap=2.0),
                        config=EngineConfig(band=0.02, max_position=2.0, rolls_per_year=4, roll_bps=1.0))


def test_h14_reproduces_on_the_engine(h14) -> None:
    panel, returns, config = h14
    golden, tolerance = GOLDEN["H14"]["values"], GOLDEN["H14"]["tolerance"]
    start, end = GOLDEN["H14"]["window"]
    variants = {"1_trend_alone_sharpe": RegimeOverlayStrategy(config, variant="trend_only"),
                "2A_range_alone_sharpe": RegimeOverlayStrategy(config, variant="range_only", standalone_range=True),
                "3A_hard_switch_sharpe": RegimeOverlayStrategy(config, variant="hard"),
                "4A_hysteresis_sharpe": RegimeOverlayStrategy(config, variant="hysteresis"),
                "5A_primary_sharpe": RegimeOverlayStrategy(config, variant="blend"),
                "6A_static_sharpe": RegimeOverlayStrategy(config, variant="static"),
                "7A_ma200_filter_sharpe": RegimeOverlayStrategy(config, variant="filter", filter_name="M1 above 200-day MA")}
    targets = {name: strategy(panel) for name, strategy in variants.items()}
    results = {name: _run(panel, returns, target) for name, target in targets.items()}
    for name, result in results.items():
        assert metrics(result, start, end)["Sharpe"] == pytest.approx(golden[name], abs=tolerance), name
    primary, static = targets["5A_primary_sharpe"], targets["6A_static_sharpe"]
    matched = Targets(primary.target.abs() * np.sign(static.target))
    assert metrics(_run(panel, returns, matched), start, end)["Sharpe"] == pytest.approx(golden["9A_exposure_matched_sharpe"], abs=tolerance)
    long = Targets(pd.DataFrame({"NDX": 1.0}, index=panel.index))
    assert metrics(_run(panel, returns, long), start, end)["Sharpe"] == pytest.approx(golden["8v_vol_managed_long_sharpe"], abs=tolerance)
    state = primary.diagnostics["state"]["NDX"].loc[start:end]
    assert (state == 0).mean() == pytest.approx(golden["primary_regime_share_transition"], abs=tolerance)
    assert (state == 1).mean() == pytest.approx(golden["primary_regime_share_trend"], abs=tolerance)
    attribution = results["5A_primary_sharpe"].parts_pnl
    assert set(attribution) == {"trend", "range"}


@pytest.fixture(scope="module")
def h13():
    path = ROOT / "data/research_cache/h13/closes.csv"
    if not path.exists() or not RATES.exists():
        pytest.skip("H13's cached data is not on this machine")
    closes = pd.read_csv(path, index_col=0, parse_dates=True)
    closes = closes[closes.index < "2026-01-01"]
    markets = {"NDX": (0.009, "USA", 1.5), "SPX": (0.019, "USA", 1.5), "STOXX600": (0.032, "EA20", 3.0)}
    days = pd.bdate_range(closes.index.min(), closes.index.max())
    levels = closes.reindex(closes.index.union(days)).ffill().reindex(days)
    returns = pd.DataFrame({market: levels[market].pct_change(fill_method=None) + (dividend - asof_daily(_monthly_rate(area), days)) / 252.0
                            for market, (dividend, area, _cost) in markets.items()})
    returns = returns.where(levels.notna() & levels.shift(1).notna())
    return levels, returns, {market: cost for market, (_d, _a, cost) in markets.items()}


def test_h13_reproduces_on_the_engine(h13) -> None:
    from quantlab.signals.forecast import trend_forecast

    levels, returns, costs = h13
    forecasts = {}
    for market in returns:
        valid = levels.index[levels[market].notna()]
        forecasts[market] = pd.Series(trend_forecast(levels[market].loc[valid].to_numpy(), activation="compound_sigmoid", returns=returns[market].loc[valid].to_numpy()),
                                      index=valid).reindex(returns.index).fillna(0.0)
    result = run_backtest(pd.DataFrame(forecasts), returns, costs_bps=costs, sizer=Sizer(target_vol=0.10, span=20), weights=1 / 3,
                          config=EngineConfig(band=0.02, band_exits_to_zero=False, max_position=1.0, rolls_per_year=4, roll_bps=1.0))
    start, end = GOLDEN["H13"]["window"]
    assert metrics(result, start, end)["Sharpe"] == pytest.approx(GOLDEN["H13"]["values"]["P_sharpe_net"], abs=GOLDEN["H13"]["tolerance"])
