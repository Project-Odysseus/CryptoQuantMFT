from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.research.volatility import (
    daily_forecast_to_bars,
    daily_realized_variance,
    ewma_vol,
    forecast_losses,
    har_forecast,
    log_returns,
    rolling_vol,
    vol_scaled_positions,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_daily_realized_variance_sums_squared_hourly_returns_per_day_and_drops_partial_days() -> None:
    hours = 24 * 2 + 5  # two full days and five hours of a third
    timestamps = [START + timedelta(hours=i) for i in range(hours)]
    returns = np.where(np.arange(hours) < 24, 0.01, 0.02)
    close = 100.0 * np.exp(np.cumsum(np.concatenate([[0.0], returns[1:]])))

    rv = daily_realized_variance(timestamps, close)

    assert list(rv.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02"]
    assert rv.iloc[0] == pytest.approx(23 * 0.01**2)  # the first bar has no previous close
    assert rv.iloc[1] == pytest.approx(24 * 0.02**2)


def test_rolling_and_ewma_vol_are_annualised_and_use_the_past_only() -> None:
    returns = np.array([np.nan, 0.01, -0.01, 0.01, -0.01, 0.05])
    rolling = rolling_vol(returns, 4, 365.0)
    assert np.isnan(rolling[3])
    assert rolling[4] == pytest.approx(0.01 * np.sqrt(365.0))

    halflife = 2.0
    ewma = ewma_vol(returns, halflife, 365.0)
    weights = 0.5 ** (np.arange(4)[::-1] / halflife)  # returns 1..4, newest weighted most
    expected = np.sqrt(np.sum(weights * returns[1:5] ** 2) / weights.sum() * 365.0)
    assert ewma[4] == pytest.approx(expected)
    assert ewma[5] > ewma[4]  # reacts to the new shock without it touching earlier values


def test_har_forecast_never_uses_later_data() -> None:
    rng = np.random.default_rng(3)
    days = pd.date_range("2021-01-01", periods=700, freq="D", tz="UTC")
    log_vol = np.zeros(len(days))
    for i in range(1, len(days)):
        log_vol[i] = 0.95 * log_vol[i - 1] + rng.normal(0, 0.3)
    rv = pd.Series(np.exp(2 * (log_vol - 4)) * rng.chisquare(24, len(days)) / 24, index=days)

    forecast = har_forecast(rv, horizon_days=7, train_min_days=365, refit_every_days=30)
    shocked = rv.copy()
    shocked.iloc[500:] *= 50.0
    forecast_shocked = har_forecast(shocked, horizon_days=7, train_min_days=365, refit_every_days=30)

    assert forecast.iloc[:365].isna().all() and (forecast.iloc[365:] > 0).all()
    pd.testing.assert_series_equal(forecast.iloc[:500], forecast_shocked.iloc[:500])
    naive = forecast_losses(np.full(len(rv), rv.iloc[:365].mean())[365:-7], rv.rolling(7).mean().shift(-7).iloc[365:-7].to_numpy())
    har = forecast_losses(forecast.iloc[365:-7].to_numpy(), rv.rolling(7).mean().shift(-7).iloc[365:-7].to_numpy())
    assert har["qlike"] < naive["qlike"]  # persistent volatility is forecastable


def test_daily_forecast_maps_to_the_last_completed_day() -> None:
    daily = pd.Series([0.4, 0.6], index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], tz="UTC"))
    closes = [datetime(2024, 1, 2, 20, tzinfo=timezone.utc), datetime(2024, 1, 3, 0, tzinfo=timezone.utc)]
    assert list(daily_forecast_to_bars(daily, closes)) == [0.4, 0.6]


def test_forecast_losses_are_zero_for_a_perfect_forecast() -> None:
    realized = np.array([1.0, 2.0, 4.0])
    losses = forecast_losses(realized, realized)
    assert losses["qlike"] == pytest.approx(0.0) and losses["mean_ratio"] == pytest.approx(1.0) and losses["r2_log"] == pytest.approx(1.0)
    assert forecast_losses(realized / 2, realized)["mean_ratio"] == pytest.approx(2.0)


def test_vol_scaled_positions_size_at_entry_cap_leverage_and_rebalance_outside_the_band() -> None:
    targets = [0, 1, 1, 1, 1, -1, 0, 1]
    forecast = [0.5, 0.5, 0.45, 0.2, 1.0, 1.0, 1.0, np.nan]

    rebalanced = vol_scaled_positions(targets, forecast, target_vol=0.5, max_leverage=2.0, rebalance_band=0.25)
    assert list(rebalanced) == [0.0, 1.0, 1.0, 2.0, 0.5, -0.5, 0.0, 1.0]  # 0.45 is inside the band; NaN falls back to 1

    entry_only = vol_scaled_positions(targets, forecast, target_vol=0.5, max_leverage=2.0, entry_only=True)
    assert list(entry_only) == [0.0, 1.0, 1.0, 1.0, 1.0, -0.5, 0.0, 1.0]


def _bars(close: np.ndarray, hours: int) -> list:
    from src.storage.bar_aggregator import OHLCVBar

    return [
        OHLCVBar(exchange="mock", symbol="BTC/USD", interval_seconds=hours * 3600, timestamp=START + timedelta(hours=hours * i), open=c, high=c, low=c, close=c, volume=1.0)
        for i, c in enumerate(close)
    ]


def test_runtime_ewma_forecast_matches_the_research_estimator() -> None:
    from src.risk.sizing import ewma_annual_volatility

    rng = np.random.default_rng(11)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 800)))
    runtime = ewma_annual_volatility(_bars(close, 4), halflife_days=10)
    research = ewma_vol(log_returns(close), 10 * 6, 365.0 * 6)[-1]
    assert runtime == pytest.approx(research, rel=0.01)  # same estimator; the runtime truncates after 8 half-lives
    assert ewma_annual_volatility(_bars(close[:10], 4), halflife_days=10) is None  # too little history


def test_volatility_target_sizes_entries_as_a_share_of_equity() -> None:
    from src.execution.paper_trading import PaperTradingEngine
    from src.risk.controls import RiskControlConfig, RiskManager
    from src.risk.sizing import ewma_annual_volatility

    rng = np.random.default_rng(5)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))  # daily vol ~2%, ~38% a year
    bars = _bars(close, 24)
    config = RiskControlConfig(max_drawdown_pct=1.0, max_volatility_pct=1.0, sizing="vol_target", sizing_params={"target_annual_vol": 0.2}, max_position_size=1.0, max_total_notional=0.0, paper_mode=True)
    manager = RiskManager(config)

    decision = manager.evaluate(bars=bars, equity=10_000.0, peak_equity=10_000.0)
    forecast = ewma_annual_volatility(bars, halflife_days=10)
    assert decision.allow_entry and decision.equity_fraction == pytest.approx(0.2 / forecast)
    assert decision.sizing_details["annual_volatility_forecast"] == pytest.approx(forecast)

    limits = {"kraken": {"max_position_size": 1.0, "max_notional_per_trade": 1_000.0, "max_total_notional": 0.0}}
    capped = RiskManager(RiskControlConfig(**{**{f: getattr(config, f) for f in config.__slots__}, "exchange_risk_limits": limits}))
    assert capped.evaluate(bars=bars, equity=10_000.0, peak_equity=10_000.0, exchange_name="kraken").equity_fraction == pytest.approx(0.1)  # per-trade notional cap
    assert RiskManager(config).evaluate(bars=bars[:5], equity=10_000.0, peak_equity=10_000.0).reason == "volatility_forecast_unavailable"

    # The engine turns the share of equity into units at the entry bar's price (not 10% of equity, not 1 unit).
    signals = [0.0] * 299 + [1.0]
    result = PaperTradingEngine(initial_cash=10_000.0, default_order_size=1.0, risk_manager=manager).run(bars, signals)
    (order,) = result.orders
    expected_fraction = 0.2 / ewma_annual_volatility(bars, halflife_days=10)
    assert order.size * close[-1] == pytest.approx(expected_fraction * 10_000.0, rel=1e-6)
