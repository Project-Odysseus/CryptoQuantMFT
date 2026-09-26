"""Position sizing: every sizer returns a share of equity; the risk manager caps it; the engine converts it to units."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from src.execution.paper_trading import PaperTradingEngine
from src.risk.controls import RiskControlConfig, RiskManager
from src.risk.sizing import (
    AtrRiskSizer,
    FixedFractionSizer,
    FixedNotionalSizer,
    KellySizer,
    SizingContext,
    VolatilityTargetSizer,
    build_sizer,
    sizer_parameters,
)
from src.runtime.config import RuntimeConfig, build_runtime_config_from_args
from src.storage.bar_aggregator import OHLCVBar

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes: list[float], *, hours: int = 24, spread: float = 0.0) -> list[OHLCVBar]:
    return [
        OHLCVBar(exchange="mock", symbol="X/USD", interval_seconds=hours * 3600, timestamp=START + timedelta(hours=hours * i), open=c, high=c + spread, low=c - spread, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


def test_each_sizer_returns_a_share_of_equity() -> None:
    context = SizingContext(bars=_bars([100.0, 101.0, 102.0], spread=1.0), equity=2_000.0, price=102.0)
    assert FixedFractionSizer(0.25).size(context).fraction == 0.25
    assert FixedNotionalSizer(500.0).size(context).fraction == pytest.approx(0.25)
    # true ranges: max(2, 2, 0) = 2 each -> ATR 2; stop 2 ATR = 4 = 3.92% of price; 1% risk / 3.92% = 0.255
    assert AtrRiskSizer(risk_fraction=0.01, atr_multiplier=2.0, atr_window=14).size(context).fraction == pytest.approx(0.01 / (4.0 / 102.0))
    unavailable = VolatilityTargetSizer(0.5).size(context)
    assert unavailable.fraction == 0.0 and unavailable.reason == "volatility_forecast_unavailable"


def test_kelly_uses_trades_then_a_prior_and_declines_without_edge() -> None:
    bars = _bars([100.0, 101.0])
    few = KellySizer(min_trades=5, fallback_fraction=0.1).size(SizingContext(bars=bars, equity=1.0, price=101.0, trade_returns=[0.1]))
    assert few.fraction == 0.1 and few.details["kelly_fallback"] == 1.0

    trades = [0.10, -0.08] * 10
    full = 0.01 / ((0.10**2 + 0.08**2) / 2)
    assert KellySizer(kelly_fraction=0.5).size(SizingContext(bars=bars, equity=1.0, price=101.0, trade_returns=trades)).fraction == pytest.approx(0.5 * full)

    # A research prior counts as extra trades: 18 prior trades + 2 live ones reach min_trades=20
    prior = KellySizer(kelly_fraction=0.5, prior_mean=0.01, prior_std=0.09, prior_trades=18)
    assert prior.size(SizingContext(bars=bars, equity=1.0, price=101.0, trade_returns=[0.10, -0.08])).fraction > 0.0

    losing = KellySizer().size(SizingContext(bars=bars, equity=1.0, price=101.0, trade_returns=[-0.02] * 30))
    assert losing.fraction == 0.0 and losing.reason == "kelly_no_edge"


def test_registry_rejects_unknown_names_and_parameters_with_the_accepted_list() -> None:
    assert isinstance(build_sizer("vol_target", target_annual_vol=0.3), VolatilityTargetSizer)
    assert "target_annual_vol" in sizer_parameters("vol_target")
    with pytest.raises(ValueError, match="unknown sizing"):
        build_sizer("martingale")
    with pytest.raises(ValueError, match="takes"):
        build_sizer("kelly", kelly_frac=0.5)
    with pytest.raises(ValueError):
        FixedFractionSizer(0.0)
    with pytest.raises(ValueError):
        KellySizer(prior_trades=5)  # a prior needs a mean and a spread


def test_risk_manager_caps_the_share_and_reports_why_it_refuses() -> None:
    bars = _bars([100.0 + i for i in range(30)], spread=0.5)
    fixed = RiskManager(RiskControlConfig(risk_per_trade_pct=0.3, max_volatility_pct=1.0, paper_mode=True))
    assert fixed.evaluate(bars=bars, equity=1_000.0, peak_equity=1_000.0).position_size == pytest.approx(0.3)  # fixed_fraction at risk_per_trade_pct

    explicit = RiskManager(RiskControlConfig(sizing_params={"fraction": 3.0}, max_position_size=1.0, max_volatility_pct=1.0, paper_mode=True))
    assert explicit.evaluate(bars=bars, equity=1_000.0, peak_equity=1_000.0).position_size == 1.0  # capped at max_position_size

    # Kraken caps: 0.5x equity and 500 per trade -> with 10,000 equity the per-trade money limit wins
    kraken = RiskManager(RiskControlConfig(sizing_params={"fraction": 0.4}, max_volatility_pct=1.0, paper_mode=True, max_total_notional=0.0))
    assert kraken.evaluate(bars=bars, equity=10_000.0, peak_equity=10_000.0, exchange_name="kraken").position_size == pytest.approx(0.05)

    no_atr = RiskManager(RiskControlConfig(sizing="atr_risk", max_volatility_pct=1.0, paper_mode=True)).evaluate(bars=bars[:1], equity=1_000.0, peak_equity=1_000.0)
    assert not no_atr.allow_entry and no_atr.reason == "atr_unavailable"


def test_engine_converts_the_share_to_units_at_the_entry_price() -> None:
    """The old bug: the share was read as units, so a cheap coin got 0.1 units instead of 10% of equity."""
    cheap = _bars([0.10] * 5)
    manager = RiskManager(RiskControlConfig(risk_per_trade_pct=0.1, max_volatility_pct=1.0, paper_mode=True))
    result = PaperTradingEngine(initial_cash=1_000.0, risk_manager=manager).run(cheap, [0, 0, 0, 0, 1])
    (order,) = result.orders
    assert order.size * 0.10 == pytest.approx(100.0)  # 10% of 1,000 equity, i.e. 1,000 coins

    plain = PaperTradingEngine(initial_cash=1_000.0, default_order_size=2.0).run(cheap, [0, 0, 0, 0, 1])
    assert plain.orders[0].size == 2.0  # without a risk manager, orders are default_order_size units


def test_engine_reports_round_trips_so_kelly_can_learn() -> None:
    bars = _bars([100.0, 100.0, 110.0, 110.0, 99.0, 99.0])
    manager = RiskManager(RiskControlConfig(risk_per_trade_pct=0.5, max_volatility_pct=1.0, paper_mode=True))
    result = PaperTradingEngine(initial_cash=1_000.0, risk_manager=manager).run(bars, [1, 1, 0, 0, 0, 0])
    buy, sell = result.trades  # run() simulates order latency: the buy fills on bar 2, the exit on bar 4
    assert (buy.side, sell.side) == ("buy", "sell")
    assert manager.trade_returns == pytest.approx([99.0 / buy.price - 1.0])  # one closed long, measured to the exit bar


def _args(**overrides: object) -> argparse.Namespace:
    defaults = dict(runtime="paper", strategy="moving_average_crossover", strategy_params="{}", runtime_iterations=3, runtime_interval=1.0, use_mock_connector=True,
                    watchdog_timeout=30.0, watchdog_restarts=0, execution_exchange="auto", trading_symbol=None, kill_switch=False, kill_switch_reason="manual",
                    live_plot=False, live_plot_path=None, runtime_config_path=None, runtime_state_path=None, bar_interval=None, warmup_bars=0,
                    sizing=None, sizing_params="{}", target_annual_vol=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_runtime_config_resolves_and_persists_sizing(tmp_path) -> None:
    config = build_runtime_config_from_args(_args(sizing="kelly", sizing_params='{"kelly_fraction": 0.25}'))
    assert (config.sizing, config.sizing_params) == ("kelly", {"kelly_fraction": 0.25})
    assert RuntimeConfig.from_dict(config.to_dict()).sizing_params == {"kelly_fraction": 0.25}

    shorthand = build_runtime_config_from_args(_args(target_annual_vol=0.4))
    assert (shorthand.sizing, shorthand.sizing_params) == ("vol_target", {"target_annual_vol": 0.4})
    with pytest.raises(ValueError, match="shorthand"):
        build_runtime_config_from_args(_args(sizing="kelly", target_annual_vol=0.4))

    path = tmp_path / "runtime.json"
    build_runtime_config_from_args(_args(runtime_config_path=str(path), sizing="vol_target", sizing_params='{"target_annual_vol": 0.3}'), argv=["--sizing", "vol_target", "--sizing-params", "{}"])
    reloaded = build_runtime_config_from_args(_args(runtime_config_path=str(path)), argv=[])
    assert reloaded.sizing == "vol_target"
