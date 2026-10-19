"""The continuous trend forecast (src/signals/trend_forecast.py), its strategy, and continuous sleeves (src/portfolio/sleeves.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.runner import StrategyRegistry
from src.portfolio.sleeves import SleeveRunner, SleeveSpec, SleeveState, run_sleeve
from src.signals.trend_forecast import ACTIVATIONS, ema_difference, ewma_volatility, trend_forecast
from src.storage.bar_aggregator import OHLCVBar

START = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _bars(closes: np.ndarray) -> list[OHLCVBar]:
    return [OHLCVBar(exchange="cme_usd", symbol="MNQ", interval_seconds=86_400, timestamp=START + timedelta(days=i),
                     open=float(c), high=float(c), low=float(c), close=float(c), volume=1.0) for i, c in enumerate(closes)]


def _walk(count: int, *, seed: int = 3, drift: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.01, count)))


@pytest.mark.parametrize("name", sorted(ACTIVATIONS))
def test_every_activation_has_unit_second_moment_and_is_odd(name: str) -> None:
    z = np.random.default_rng(0).standard_normal(400_000)
    psi = ACTIVATIONS[name]
    assert np.mean(psi(z) ** 2) == pytest.approx(1.0, abs=0.01)
    assert np.allclose(psi(-z[:100]), -psi(z[:100]))


def test_the_ema_difference_has_unit_variance_on_independent_unit_returns() -> None:
    returns = np.random.default_rng(1).standard_normal(100_000)
    z = ema_difference(returns, 20, 40)[1000:]
    assert z.std() == pytest.approx(1.0, abs=0.05)


def test_volatility_is_floored_and_skips_gaps() -> None:
    sigma = ewma_volatility(np.array([np.nan, 0.0, 0.0, np.nan, 0.0]))
    assert np.isnan(sigma[0])
    assert np.allclose(sigma[1:], 0.05 / np.sqrt(252.0))


def test_the_forecast_is_causal() -> None:
    closes = _walk(400)
    full = trend_forecast(closes)
    assert np.allclose(trend_forecast(closes[:250]), full[:250])


def test_a_steady_rise_gives_a_long_forecast_and_the_reverting_part_trims_it() -> None:
    rising = trend_forecast(_walk(600, drift=0.004))[-1]
    assert 0.0 < rising < 1.5
    assert trend_forecast(_walk(600, drift=-0.004))[-1] < 0.0


def test_the_strategy_is_registered_and_zero_during_warmup() -> None:
    strategy = StrategyRegistry().get("trend_forecast")(warmup=160)
    values = strategy.signal_series(_bars(_walk(300, drift=0.003)))
    assert np.all(values[:160] == 0.0)
    assert np.any(values[160:] != 0.0)
    with pytest.raises(ValueError):
        StrategyRegistry().get("trend_forecast")(speeds=[[40, 20]])


def _spec(**overrides: object) -> SleeveSpec:
    values: dict[str, object] = {"id": "mnq_forecast", "instrument": "cme_usd:MNQ", "interval": "1d", "strategy": "trend_forecast",
                                 "sizing": "vol_target", "sizing_params": {"target_annual_vol": 0.10}, "continuous": True}
    values.update(overrides)
    return SleeveSpec(**values)  # type: ignore[arg-type]


def test_a_continuous_sleeve_holds_signal_times_sizer_and_resizes_every_bar() -> None:
    bars = _bars(_walk(500, seed=5))
    runner = SleeveRunner(_spec())
    run = run_sleeve(_spec(), bars)
    signals = runner.signals(bars)
    held = run.weights[run.weights != 0.0]
    assert len(np.unique(np.round(held, 8))) > 50  # resized, not held at the entry size
    assert np.all(np.sign(run.weights) == np.sign(signals))
    assert np.max(np.abs(run.weights)) > 0.0


def test_an_ordinary_sleeve_takes_only_the_sign() -> None:
    bars = _bars(_walk(500, seed=5))
    signals = SleeveRunner(_spec(continuous=False)).signals(bars)
    assert set(np.unique(signals)) <= {-1.0, 0.0, 1.0}


def test_a_continuous_sleeve_closes_a_trade_on_a_sign_change() -> None:
    runner = SleeveRunner(_spec())
    bars = _bars(_walk(300, seed=2))
    state, decision = runner.step(SleeveState(), bars, 0.8)
    assert decision.action == "enter" and decision.weight > 0
    state, decision = runner.step(state, bars, 0.4)
    assert decision.action == "hold" and 0 < decision.weight < state.weight * 2.01
    state, decision = runner.step(state, bars, -0.5)
    assert decision.action == "flip" and decision.weight < 0 and len(state.trade_returns) == 1
    state, decision = runner.step(state, bars, 0.0)
    assert decision.action == "exit" and state.weight == 0.0


def test_a_continuous_sleeve_refuses_stops() -> None:
    with pytest.raises(ValueError, match="no stops"):
        SleeveRunner(_spec(stops={"time_stop_bars": 10}))


def test_the_per_bar_strategy_equals_the_vectorised_series_exactly() -> None:
    strategy = StrategyRegistry().get("trend_forecast")(warmup=100)
    bars = _bars(_walk(240, seed=8))
    series = strategy.signal_series(bars)
    assert np.allclose([strategy(bars[: i + 1], i, bars[i]) for i in range(len(bars))], series)
