"""The exposure summary and the risk model wired to a config (src/portfolio/book_risk.py)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.portfolio.book_risk import build_risk_model, default_benchmark, estimate_stream, exposure_history, exposure_summary, format_exposure
from src.portfolio.config import parse_portfolio_config
from src.portfolio.risk_model import RiskEstimate

BTC, ETH, SOL, SPOT = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD", "kraken_futures:SOL/USD", "kraken:BTC/EUR"


def _config(**risk: Any):
    sleeve = {"interval": "4h", "strategy": "moving_average_crossover"}
    return parse_portfolio_config({
        "portfolio": {"name": "unit"},
        "risk": {"max_gross_exposure": 5.0, "max_net_exposure": 5.0, "max_instrument_weight": 5.0, **risk},
        "instruments": {BTC: {"kind": "perp", "group": "majors"}, ETH: {"kind": "perp", "group": "alts"}, SOL: {"kind": "perp", "group": "alts"},
                        SPOT: {"kind": "spot", "group": "majors"}},
        "sleeves": [{"id": "btc", "instrument": BTC, **sleeve}, {"id": "eth", "instrument": ETH, **sleeve}, {"id": "sol", "instrument": SOL, **sleeve},
                    {"id": "spot", "instrument": SPOT, "long_only": True, **sleeve}],
    })


def _estimate() -> RiskEstimate:
    names = (SPOT, BTC, ETH, SOL)  # the model's order: sorted instrument ids
    std = np.array([0.02, 0.02, 0.03, 0.04]) / np.sqrt(6)  # per 4h bar
    correlation = np.array([[1.0, 1.0, 0.8, 0.6], [1.0, 1.0, 0.8, 0.6], [0.8, 0.8, 1.0, 0.7], [0.6, 0.6, 0.7, 1.0]])
    return RiskEstimate(names, correlation * np.outer(std, std), np.maximum(correlation, 0.9) * np.outer(std, std), 365.0 * 6)


def _prices(bars: int = 400, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    common = rng.normal(0.0, 0.008, bars)
    index = pd.date_range("2024-01-01", periods=bars, freq="4h", tz="UTC")
    return pd.DataFrame({name: 100.0 * np.exp(np.cumsum(common + rng.normal(0.0, 0.004, bars))) for name in (SPOT, BTC, ETH, SOL)}, index=index)


def test_the_summary_adds_exposure_up_by_side_group_venue_and_coin() -> None:
    summary = exposure_summary({BTC: 0.5, SPOT: 0.25, ETH: -0.3, SOL: 0.0}, config=_config())
    assert summary["long"] == pytest.approx(0.75) and summary["short"] == pytest.approx(0.3)
    assert summary["gross"] == pytest.approx(1.05) and summary["net"] == pytest.approx(0.45) and summary["positions"] == 3
    assert summary["groups"] == {"alts": pytest.approx({"gross": 0.3, "net": -0.3}), "majors": pytest.approx({"gross": 0.75, "net": 0.75})}
    assert summary["venues"]["kraken"] == pytest.approx({"gross": 0.25, "net": 0.25})
    assert summary["underlyings"]["BTC"] == pytest.approx({"gross": 0.75, "net": 0.75})  # the perp and the spot are one coin
    assert summary["volatility"] is None and summary["beta_exposure"] is None and summary["risk_share"] == {}
    assert "not enough history" in format_exposure(summary)[-1]


def test_the_summary_measures_the_book_in_benchmark_terms_and_shares_out_the_risk() -> None:
    estimate, config = _estimate(), _config()
    weights = {BTC: 0.5, ETH: 0.5, SOL: -0.2}
    summary = exposure_summary(weights, config=config, estimate=estimate, units={"eth_a": "eth", "eth_b": "eth"},
                               sleeve_weights={"btc": (BTC, 0.5), "eth_a": (ETH, 0.3), "eth_b": (ETH, 0.2), "sol": (SOL, -0.2)})
    assert summary["benchmark"] == BTC
    assert summary["beta_exposure"] == pytest.approx(0.5 + 0.5 * 1.2 - 0.2 * 1.2)  # betas: ETH 0.8 * 1.5, SOL 0.6 * 2
    assert summary["volatility"] == pytest.approx(estimate.volatility(weights)) and summary["stressed_volatility"] >= summary["volatility"]
    assert summary["var_95_1d"] == pytest.approx(1.6449 * summary["stressed_volatility"] / np.sqrt(365.0))
    assert sum(summary["risk_share"].values()) == pytest.approx(1.0) and summary["risk_share"][SOL] < 0  # the short offsets the longs
    assert sum(summary["sleeve_risk_share"].values()) == pytest.approx(1.0)
    assert summary["sleeve_risk_share"]["eth"] == pytest.approx(summary["risk_share"][ETH])  # two sleeves reported as their unit
    assert 1.0 < summary["effective_bets"] < 3.0 and summary["diversification_ratio"] > 1.0
    text = "\n".join(format_exposure(summary, limits={"groups": {"alts": {"max_gross": 1.0}}, "max_beta_exposure": 1.0, "max_portfolio_vol": 0.6}))
    assert "alts gross 0.70x of 1x" in text and "of 1x (sum of weight x beta)" in text and "(cap 60%)" in text and "Risk by sleeve" in text


def test_the_benchmark_defaults_to_the_books_bitcoin_instrument() -> None:
    assert default_benchmark(_config()) == BTC  # the perp, not the spot pair
    assert default_benchmark(_config(benchmark=ETH)) == ETH


def test_the_model_is_built_from_the_configs_settings_on_the_grid_interval() -> None:
    model = build_risk_model(_config(vol_halflife_days=10, vol_min_days=5, correlation_shrinkage=0.3, prior_correlation=0.6, stress_correlation=0.0), "4h")
    assert model.instruments == (SPOT, BTC, ETH, SOL)
    assert (model.halflife_bars, model.min_bars, model.periods_per_year) == (60.0, 30, 365.0 * 6)
    assert (model.shrinkage, model.prior_correlation, model.stress_correlation) == (0.3, 0.6, None)


def test_the_estimate_stream_steps_one_bar_at_a_time_from_past_prices_only() -> None:
    config, prices = _config(vol_min_days=5), _prices()
    stream = estimate_stream(config, "4h", prices.iloc[100:], warmup_prices=prices.iloc[:100])
    model = build_risk_model(config, "4h")
    returns = prices.pct_change(fill_method=None)
    for row in range(100):
        model.step(returns.iloc[row].to_dict())
    for index in (0, 1, 2, 50, 50, 299):  # the same bar may be read twice
        while model.bars_seen <= 100 + index:
            model.step(returns.iloc[model.bars_seen].to_dict())
        assert np.array_equal(stream(index).covariance, model.estimate().covariance)
    with pytest.raises(ValueError, match="bar order"):
        stream(10)
    changed = prices.copy()
    changed.iloc[250:] *= 1.5  # another future
    other = estimate_stream(config, "4h", changed.iloc[100:], warmup_prices=changed.iloc[:100])
    fresh = estimate_stream(config, "4h", prices.iloc[100:], warmup_prices=prices.iloc[:100])
    assert np.array_equal(other(120).covariance, fresh(120).covariance)
    cold = estimate_stream(config, "4h", prices.iloc[100:])
    assert cold(5) is None and cold(40) is not None  # without a warmup the model needs vol_min_days of bars first


def test_exposure_history_follows_the_held_weights_bar_by_bar() -> None:
    config, prices = _config(vol_min_days=5), _prices()
    weights = pd.DataFrame({SPOT: 0.0, BTC: 0.5, ETH: 0.25, SOL: -0.25}, index=prices.index)
    weights.iloc[200:, weights.columns.get_loc(SOL)] = 0.25
    history = exposure_history(config, "4h", prices, weights, every=6)
    assert len(history) == len(prices[::6]) and history.index[1] == prices.index[6]
    assert history["gross"].iloc[-1] == pytest.approx(1.0) and history["net"].iloc[0] == pytest.approx(0.5)
    assert history["alts gross"].iloc[-1] == pytest.approx(0.5) and history["alts net"].iloc[0] == pytest.approx(0.0)
    assert np.isnan(history["volatility"].iloc[0]) and history["volatility"].iloc[-1] > 0
    assert history["beta_exposure"].iloc[-1] > history["beta_exposure"].iloc[20]  # the SOL short became a long
