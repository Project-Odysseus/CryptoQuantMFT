"""Research backtest of a portfolio config (src/portfolio/backtest.py) and its script."""

from __future__ import annotations

import runpy
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.portfolio import backtest
from src.portfolio.backtest import prepare_inputs, run_book
from src.portfolio.config import InstrumentSpec, parse_portfolio_config
from src.portfolio.sleeves import run_sleeve
from src.research.portfolio import PortfolioCosts, simulate_portfolio
from src.storage.bar_aggregator import OHLCVBar

START = datetime(2023, 1, 1, tzinfo=timezone.utc)
DAYS = 300


def _four_hour_closes(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, DAYS * 6)))


def _bar(symbol: str, timestamp: datetime, seconds: int, open_: float, high: float, low: float, close: float) -> OHLCVBar:
    return OHLCVBar(exchange="mock", symbol=symbol, interval_seconds=seconds, timestamp=timestamp, open=open_, high=high, low=low, close=close, volume=1.0)


def synthetic_loader(instrument: InstrumentSpec, interval: str) -> list[OHLCVBar]:
    """4h random walks per instrument, and daily bars built from them (stamped at the day's open)."""
    closes = _four_hour_closes(1 if "BTC" in instrument.symbol else 2)
    if interval == "4h":
        return [_bar(instrument.symbol, START + timedelta(hours=4 * i), 14400, c, c * 1.005, c * 0.995, c) for i, c in enumerate(closes)]
    assert interval == "1d"
    days = closes.reshape(DAYS, 6)
    return [_bar(instrument.symbol, START + timedelta(days=d), 86400, row[0], row.max() * 1.005, row.min() * 0.995, row[-1]) for d, row in enumerate(days)]


def _config(**portfolio: Any) -> Any:
    raw = {
        "portfolio": {"name": "unit", "allocation": "equal", **portfolio},
        "risk": {"max_drawdown": 0.9, "max_gross_exposure": 5.0, "max_net_exposure": 5.0, "max_instrument_weight": 5.0},
        "instruments": {
            "kraken_futures:BTC/USD": {"kind": "perp", "fee_pct": 0.0, "slippage_bps": 0.0},
            "kraken_futures:ETH/USD": {"kind": "perp", "fee_pct": 0.0, "slippage_bps": 0.0},
        },
        "sleeves": [
            {"id": "btc_1d", "instrument": "kraken_futures:BTC/USD", "interval": "1d", "strategy": "moving_average_crossover",
             "params": {"short_window": 5, "long_window": 20}, "warmup_bars": 30, "budget": 0.6},
            {"id": "eth_4h", "instrument": "kraken_futures:ETH/USD", "interval": "4h", "strategy": "keltner_breakout",
             "params": {"window": 30, "atr_multiplier": 1.0}, "warmup_bars": 60, "sizing": "vol_target", "sizing_params": {"target_annual_vol": 0.4}, "budget": 0.8},
        ],
    }
    return parse_portfolio_config(raw)


def test_sleeves_land_on_the_shortest_interval_grid_at_their_decision_time() -> None:
    config = _config()
    inputs = prepare_inputs(config, bar_loader=synthetic_loader)
    assert inputs.grid_interval == "4h" and inputs.bars_per_day == 6
    assert list(inputs.prices.columns) == ["kraken_futures:BTC/USD", "kraken_futures:ETH/USD"]
    # btc_1d's warmup (30 daily bars) ends later than eth_4h's (60 4h bars = 10 days): its 30th decision is at day 31's start
    assert inputs.measure_start == pd.Timestamp(START + timedelta(days=30, hours=20))

    daily = run_sleeve(config.sleeves[0], synthetic_loader(config.instruments["kraken_futures:BTC/USD"], "1d")).weights
    on_grid = inputs.sleeve_weights["btc_1d"]
    at_day_end = on_grid[on_grid.index.hour == 20]
    np.testing.assert_allclose(at_day_end.to_numpy(), daily[30 : 30 + len(at_day_end)])
    changes = on_grid.index[on_grid.diff().fillna(0.0) != 0.0]
    assert len(changes) > 0 and set(changes.hour) == {20}  # a daily sleeve only changes at the day's last 4h bar

    eth = run_sleeve(config.sleeves[1], synthetic_loader(config.instruments["kraken_futures:ETH/USD"], "4h")).weights
    np.testing.assert_allclose(inputs.sleeve_weights["eth_4h"].to_numpy(), eth[-len(inputs.prices) :])


def test_a_sleeve_alone_is_its_own_weights_simulated_at_full_size() -> None:
    config = _config()
    inputs = prepare_inputs(config, bar_loader=synthetic_loader)
    alone = run_book(config, inputs, allocation="equal", sleeves=["eth_4h"], risk_overlay=False, funding_pct_per_day=0.0)
    assert alone.sleeves == ("eth_4h",)
    weights = inputs.sleeve_weights[["eth_4h"]].rename(columns={"eth_4h": "kraken_futures:ETH/USD"})
    prices = inputs.prices[["kraken_futures:ETH/USD"]]
    expected = simulate_portfolio(prices, weights, costs=PortfolioCosts(fee_pct=0.0, slippage_bps=0.0), rebalance_band=config.rebalance_band,
                                  initial_equity=config.initial_equity)
    np.testing.assert_allclose(alone.result.equity.to_numpy(), expected.equity.to_numpy())
    assert (alone.targets["kraken_futures:BTC/USD"] == 0.0).all()


def test_books_scale_sleeves_by_allocation_and_charge_perp_funding() -> None:
    config = _config()
    inputs = prepare_inputs(config, bar_loader=synthetic_loader)
    equal = run_book(config, inputs, funding_pct_per_day=0.0)
    np.testing.assert_allclose(equal.allocated.to_numpy(), inputs.sleeve_weights.to_numpy() * 0.5)

    fixed = run_book(config, inputs, allocation="fixed", funding_pct_per_day=0.0)  # budgets 0.6 + 0.8 scaled to sum to 1
    np.testing.assert_allclose(fixed.allocated["btc_1d"].to_numpy(), inputs.sleeve_weights["btc_1d"].to_numpy() * 0.6 / 1.4)

    funded = run_book(config, inputs, funding_pct_per_day=0.05)
    assert funded.result.funding.abs().sum() > 0.0 and funded.result.equity.iloc[-1] != equal.result.equity.iloc[-1]


def test_the_script_runs_from_a_config_file_alone(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "book.toml"
    config_path.write_text("""
[portfolio]
name = "script-test"
[instruments."kraken_futures:BTC/USD"]
kind = "perp"
[instruments."kraken_futures:ETH/USD"]
kind = "perp"
[[sleeves]]
id = "btc_ma"
instrument = "kraken_futures:BTC/USD"
interval = "1d"
strategy = "moving_average_crossover"
params = { short_window = 5, long_window = 20 }
warmup_bars = 30
[[sleeves]]
id = "eth_ma"
instrument = "kraken_futures:ETH/USD"
interval = "4h"
strategy = "moving_average_crossover"
params = { short_window = 10, long_window = 60 }
warmup_bars = 60
""")
    monkeypatch.setattr(backtest, "default_bar_loader", synthetic_loader)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["portfolio_backtest.py", str(config_path), "--holdout-start", "2023-08-01", "--out", str(out)])
    runpy.run_path("scripts/research/portfolio_backtest.py", run_name="__main__")

    books = pd.read_csv(out / "books.csv")
    assert set(books["book"]) == {"fixed", "equal", "inverse_vol", "equal without risk limits"} and set(books["period"]) == {"is", "ho"}
    assert set(pd.read_csv(out / "sleeves.csv")["book"]) == {"btc_ma", "eth_ma"}
    assert pd.read_csv(out / "correlation.csv", index_col=0).shape == (2, 2)
    text = capsys.readouterr().out
    assert "Each sleeve alone at full size" in text and "Correlation of the sleeves' daily returns" in text


def test_a_venue_without_a_history_loader_is_reported() -> None:
    with pytest.raises(ValueError, match="no history loader for venue 'binance'"):
        backtest.default_bar_loader(InstrumentSpec(id="binance:BTC/USDT"), "1d")


def test_a_notebook_strategy_can_be_scored_as_a_candidate_sleeve() -> None:
    from src.backtest.indicators import rolling_mean
    from src.backtest.strategies import rule_strategy
    from src.portfolio.backtest import candidate_report
    from src.portfolio.sleeves import SleeveSpec

    def rules(bars):
        above = rolling_mean(bars.close, 10) > rolling_mean(bars.close, 40)
        return above, ~above, None, None

    config = _config()
    candidate = SleeveSpec(id="eth_notebook", instrument="kraken_futures:ETH/USD", interval="1d", strategy="notebook_ma", warmup_bars=45)
    report = candidate_report(config, candidate, strategy=rule_strategy(rules, warmup=40), holdout="2023-08-01", bar_loader=synthetic_loader, funding_pct_per_day=0.0)

    assert set(report["books"]["book"]) == {"without", "with eth_notebook"} and set(report["books"]["period"]) == {"is", "ho"}
    assert list(report["correlation"].index) == ["btc_1d", "eth_4h"] and report["correlation"]["eth_notebook"].between(-1, 1).all()
    assert report["alone"]["avg_gross_exposure"].gt(0).all()

    with pytest.raises(ValueError, match="already has a sleeve"):
        candidate_report(config, config.sleeves[0], bar_loader=synthetic_loader)
    with pytest.raises(ValueError, match="not in the config's"):
        candidate_report(config, SleeveSpec(id="sol", instrument="kraken_futures:SOL/USD", interval="1d", strategy="x"), bar_loader=synthetic_loader)


def test_a_prebuilt_strategy_replaces_the_registry_and_keeps_long_only() -> None:
    from src.backtest.indicators import rolling_mean
    from src.backtest.strategies import signal_strategy
    from src.portfolio.sleeves import SleeveSpec

    strategy = signal_strategy(lambda b: np.sign(rolling_mean(b.close, 5) - rolling_mean(b.close, 20)), warmup=20)
    bars = synthetic_loader(InstrumentSpec(id="kraken_futures:BTC/USD"), "1d")
    spec = SleeveSpec(id="x", instrument="kraken_futures:BTC/USD", interval="1d", strategy="not_registered")
    both_sides = run_sleeve(spec, bars, strategy=strategy).weights
    long_only = run_sleeve(replace(spec, long_only=True), bars, strategy=strategy).weights
    assert (both_sides < 0).any() and (long_only >= 0).all() and (long_only > 0).any()


@pytest.mark.parametrize("method", ["equal", "fixed", "inverse_vol"])
def test_the_runtime_path_bar_by_bar_matches_the_research_backtest(method: str) -> None:
    """Portfolio plan step 2.7: the same bars through the runtime core give the research targets at every grid bar.

    The runtime path sees bars as they complete. Each sleeve steps when one of its own bars closes (a daily sleeve
    once a day, on the day's last 4h bar), the allocator steps once per grid bar, and netting combines them. Every
    sleeve state and the allocator go through a JSON checkpoint on every bar, as a restart would.
    """
    import json

    from src.portfolio.allocation import Allocator
    from src.portfolio.netting import net_targets
    from src.portfolio.sleeves import SleeveRunner, SleeveState

    config = _config(allocation_lookback_days=20, allocation_refit_days=5)
    inputs = prepare_inputs(config, bar_loader=synthetic_loader)
    research = run_book(config, inputs, allocation=method, risk_overlay=False).targets

    grid_step = pd.Timedelta(hours=4)
    grid = pd.DatetimeIndex([bar.timestamp for bar in synthetic_loader(config.instruments["kraken_futures:BTC/USD"], "4h")])
    closes = {instrument: pd.Series([bar.close for bar in synthetic_loader(config.instruments[instrument], "4h")], index=grid) for instrument in config.instruments}
    sleeves = {}
    for spec in config.enabled_sleeves:
        bars = synthetic_loader(config.instruments[spec.instrument], spec.interval)
        lands_on = [pd.Timestamp(bar.timestamp) + pd.Timedelta(seconds=bar.interval_seconds) - grid_step for bar in bars]
        sleeves[spec.id] = {"spec": spec, "runner": SleeveRunner(spec), "bars": bars, "lands_on": lands_on, "done": 0, "state": SleeveState()}

    per_day = inputs.bars_per_day
    allocator = Allocator(config.budgets() if method != "fixed" else {k: v / 1.4 for k, v in config.budgets().items()}, method,
                          lookback=round(config.allocation_lookback_days * per_day), refit_every=round(config.allocation_refit_days * per_day))
    runtime: dict[pd.Timestamp, dict[str, float]] = {}
    previous_close: dict[str, float] | None = None
    for stamp in grid:
        for sleeve in sleeves.values():
            while sleeve["done"] < len(sleeve["bars"]) and sleeve["lands_on"][sleeve["done"]] <= stamp:
                history = sleeve["bars"][: sleeve["done"] + 1]
                state, _ = sleeve["runner"].step(sleeve["state"], history, sleeve["runner"].signals(history)[-1])
                sleeve["state"] = SleeveState.from_dict(json.loads(json.dumps(state.to_dict())))
                sleeve["done"] += 1
        if stamp < inputs.measure_start:
            continue
        returns = {sleeve_id: (closes[s["spec"].instrument][stamp] / previous_close[s["spec"].instrument] - 1.0) if previous_close else float("nan")
                   for sleeve_id, s in sleeves.items()}
        scales = allocator.step(returns)
        allocator = Allocator.from_dict(json.loads(json.dumps(allocator.to_dict())))
        net, _ = net_targets({sleeve_id: (s["spec"].instrument, s["state"].weight * scales[sleeve_id]) for sleeve_id, s in sleeves.items()})
        runtime[stamp] = net
        previous_close = {instrument: series[stamp] for instrument, series in closes.items()}

    runtime_frame = pd.DataFrame.from_dict(runtime, orient="index").reindex(columns=research.columns)
    assert len(runtime_frame) == len(research)
    np.testing.assert_allclose(runtime_frame.to_numpy(), research.to_numpy(), rtol=1e-9, atol=1e-12)
    assert (research.abs().sum(axis=1) > 0).mean() > 0.3  # the books actually hold positions


def test_drawdown_stats_match_hand_calculation() -> None:
    from src.portfolio.risk_budget import drawdown_stats

    days = pd.date_range("2024-01-01", periods=7, freq="D", tz="UTC")
    stats = drawdown_stats(pd.Series([100, 110, 99, 88, 110, 121, 115.0], index=days))
    assert stats["max_drawdown"] == pytest.approx(0.2)  # 110 -> 88
    assert stats["longest_underwater_days"] == pytest.approx(2.0)  # below 110 from Jan 3 until Jan 5
    assert stats["current_drawdown"] == pytest.approx(1 - 115 / 121)


def test_the_scale_knob_sizes_the_whole_book_and_the_risk_budget_finds_it() -> None:
    from dataclasses import replace as replace_config

    from src.portfolio.risk_budget import risk_report, scale_for_max_drawdown

    config = _config()
    inputs = prepare_inputs(config, bar_loader=synthetic_loader)
    full = run_book(config, inputs, risk_overlay=False, funding_pct_per_day=0.0)
    half = run_book(replace_config(config, scale=0.5), inputs, risk_overlay=False, funding_pct_per_day=0.0)
    np.testing.assert_allclose(half.targets.to_numpy(), full.targets.to_numpy() * 0.5)

    report = risk_report(config, inputs, capital=20_000, scale=1.0, funding_pct_per_day=0.0)
    assert report["max_drawdown_money"] == pytest.approx(report["max_drawdown"] * 20_000)
    assert report["var_99_day"] >= report["var_95_day"] > 0 and report["es_99_day"] >= report["var_99_day"]
    assert report["max_margin_share"] > 0 and report["worst_month"] >= report["worst_week"] * 0.5

    target = 0.5 * report["max_drawdown"]
    scale = scale_for_max_drawdown(config, inputs, target, safety=1.0, funding_pct_per_day=0.0, tolerance=0.01)
    at_scale = risk_report(config, inputs, capital=20_000, scale=scale, funding_pct_per_day=0.0)
    assert 0.3 < scale < 1.0 and at_scale["max_drawdown"] <= target + 1e-9
    with pytest.raises(ValueError, match="between 0 and 1"):
        scale_for_max_drawdown(config, inputs, 1.5)


def test_the_money_cap_binds_in_research_at_the_configs_capital() -> None:
    from dataclasses import replace as replace_config

    config = _config(initial_equity=1_000)
    inputs = prepare_inputs(config, bar_loader=synthetic_loader)
    free = run_book(config, inputs, funding_pct_per_day=0.0)
    capped = run_book(replace_config(config, risk=replace_config(config.risk, max_gross_notional=200.0)), inputs, funding_pct_per_day=0.0)
    notional = capped.result.gross_exposure * capped.result.equity
    assert free.result.gross_exposure.max() > 0.3
    assert notional.min() >= 0 and (notional - 200.0).max() <= 0.025 * capped.result.equity.max()  # only drift inside the 2% rebalance band
    assert capped.result.metrics()["avg_gross_exposure"] < free.result.metrics()["avg_gross_exposure"]
