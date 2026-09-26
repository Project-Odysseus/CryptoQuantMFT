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
    expected = simulate_portfolio(prices, weights, costs=PortfolioCosts(fee_pct=0.0, slippage_bps=0.0), rebalance_band=config.rebalance_band)
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
