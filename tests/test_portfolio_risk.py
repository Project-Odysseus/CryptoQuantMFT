"""Portfolio risk overlay (src/portfolio/risk.py): one test per rule, cap properties, and the research hook."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.portfolio.risk import PortfolioRiskConfig, apply_portfolio_risk, array_overlay, drawdown_multiplier
from src.research.portfolio import PortfolioCosts, simulate_portfolio

BTC, ETH, SPOT = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD", "kraken:BTC/EUR"
VENUES = {BTC: "kraken_futures", ETH: "kraken_futures", SPOT: "kraken"}
CAN_SHORT = {BTC: True, ETH: True, SPOT: False}
LOOSE = PortfolioRiskConfig(max_gross_exposure=10.0, max_net_exposure=10.0, max_instrument_weight=10.0, max_drawdown=0.9,
                            daily_loss_limit=None, drawdown_derisk_start=None)
FREE = PortfolioCosts(fee_pct=0.0, slippage_bps=0.0)


def _apply(targets: dict[str, float], config: PortfolioRiskConfig = LOOSE, **kwargs: object) -> tuple[dict[str, float], list[tuple[str, str | None]]]:
    out, actions = apply_portfolio_risk(targets, config=config, venues=VENUES, can_short=CAN_SHORT, **kwargs)  # type: ignore[arg-type]
    return out, [(action.rule, action.instrument) for action in actions]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_gross_exposure": 0.0}, "max_gross_exposure"),
        ({"max_net_exposure": -1.0}, "max_net_exposure"),
        ({"max_instrument_weight": 0.0}, "max_instrument_weight"),
        ({"max_drawdown": 0.0}, "max_drawdown"),
        ({"max_drawdown": 0.2, "drawdown_derisk_start": 0.2}, "drawdown_derisk_start"),
        ({"drawdown_derisk_floor": 1.5}, "drawdown_derisk_floor"),
        ({"daily_loss_limit": 0.0}, "daily_loss_limit"),
        ({"max_venue_exposure": {"kraken": -0.5}}, "max_venue_exposure"),
        ({"stale_after_bars": 0}, "stale_after_bars"),
    ],
)
def test_risk_limits_that_cannot_work_are_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PortfolioRiskConfig(**overrides)  # type: ignore[arg-type]


def test_drawdown_multiplier_falls_linearly_from_the_start_to_the_floor_then_flattens() -> None:
    config = PortfolioRiskConfig(max_drawdown=0.3, drawdown_derisk_start=0.1, drawdown_derisk_floor=0.5)
    assert drawdown_multiplier(1.2, 1.0, config) == 1.0  # a new high
    assert drawdown_multiplier(0.9, 1.0, config) == 1.0
    assert drawdown_multiplier(0.8, 1.0, config) == pytest.approx(0.75)  # halfway from 10% to 30%
    assert drawdown_multiplier(0.7001, 1.0, config) == pytest.approx(0.5, abs=1e-3)
    assert drawdown_multiplier(0.7, 1.0, config) == 0.0
    assert drawdown_multiplier(0.5, 0.0, config) == 1.0  # no peak yet
    assert drawdown_multiplier(0.75, 1.0, PortfolioRiskConfig(max_drawdown=0.3, drawdown_derisk_start=None)) == 1.0


def test_a_stale_instrument_may_shrink_but_not_grow_or_flip() -> None:
    current = {BTC: 0.4, ETH: -0.3}
    out, actions = _apply({BTC: 0.6, ETH: 0.2}, current=current, stale=[BTC, ETH])
    assert out == {BTC: 0.4, ETH: 0.0}
    assert actions == [("stale_instrument", BTC), ("stale_instrument", ETH)]

    out, actions = _apply({BTC: 0.1, ETH: 0.5}, current=current, stale=[BTC])
    assert out == {BTC: 0.1, ETH: 0.5} and actions == []  # shrinking is allowed; ETH isn't stale
    assert _apply({BTC: 0.5}, stale=[BTC])[0] == {BTC: 0.0}  # nothing held: nothing new


def test_an_instrument_that_cannot_short_is_clamped_at_zero() -> None:
    out, actions = _apply({SPOT: -0.3, BTC: -0.3})
    assert out == {SPOT: 0.0, BTC: -0.3} and actions == [("no_short", SPOT)]


def test_the_instrument_cap_keeps_the_side() -> None:
    config = PortfolioRiskConfig(max_instrument_weight=0.5, max_gross_exposure=10.0, max_net_exposure=10.0)
    out, actions = _apply({BTC: 0.8, ETH: -0.9, SPOT: 0.2}, config)
    assert out == {BTC: 0.5, ETH: -0.5, SPOT: 0.2}
    assert actions == [("instrument_cap", BTC), ("instrument_cap", ETH)]


def test_a_venue_cap_scales_only_that_venues_instruments_together() -> None:
    config = PortfolioRiskConfig(max_gross_exposure=10.0, max_net_exposure=10.0, max_venue_exposure={"kraken_futures": 0.8})
    out, actions = _apply({BTC: 0.6, ETH: -0.6, SPOT: 0.9}, config)
    assert out == pytest.approx({BTC: 0.4, ETH: -0.4, SPOT: 0.9})
    assert actions == [("venue_cap", BTC), ("venue_cap", ETH)]


def test_the_net_cap_then_the_gross_cap_scale_everything() -> None:
    net = PortfolioRiskConfig(max_net_exposure=1.0, max_gross_exposure=10.0, max_instrument_weight=10.0)
    out, actions = _apply({BTC: 0.8, ETH: 0.6, SPOT: 0.0}, net)
    assert out[BTC] == pytest.approx(0.8 / 1.4) and out[ETH] == pytest.approx(0.6 / 1.4) and out[SPOT] == 0.0
    assert {rule for rule, _ in actions} == {"net_cap"}

    gross = PortfolioRiskConfig(max_net_exposure=10.0, max_gross_exposure=1.5, max_instrument_weight=10.0)
    out, actions = _apply({BTC: 0.8, ETH: -0.8}, gross)
    assert out == pytest.approx({BTC: 0.75, ETH: -0.75})
    assert {rule for rule, _ in actions} == {"gross_cap"}


def test_drawdown_derisks_then_flattens_everything() -> None:
    config = PortfolioRiskConfig(max_drawdown=0.3, drawdown_derisk_start=0.1, drawdown_derisk_floor=0.5, daily_loss_limit=None)
    out, actions = _apply({BTC: 0.8, ETH: -0.4}, config, equity=0.8, peak_equity=1.0)
    assert out == pytest.approx({BTC: 0.6, ETH: -0.3}) and {rule for rule, _ in actions} == {"drawdown_derisk"}

    out, actions = _apply({BTC: 0.8, ETH: -0.4}, config, equity=0.69, peak_equity=1.0, current={BTC: 0.8})
    assert out == {BTC: 0.0, ETH: 0.0} and {rule for rule, _ in actions} == {"max_drawdown_halt"}


def test_past_the_daily_loss_limit_positions_may_only_shrink() -> None:
    config = PortfolioRiskConfig(daily_loss_limit=0.05, drawdown_derisk_start=None, max_drawdown=0.5, max_net_exposure=10.0, max_gross_exposure=10.0)
    current = {BTC: 0.5, ETH: 0.3}
    out, actions = _apply({BTC: 0.9, ETH: 0.1, SPOT: 0.2}, config, current=current, equity=0.94, peak_equity=1.0, day_start_equity=1.0)
    assert out == {BTC: 0.5, ETH: 0.1, SPOT: 0.0}
    assert actions == [("daily_loss_halt", BTC), ("daily_loss_halt", SPOT)]
    assert _apply({BTC: 0.9}, config, current=current, equity=0.96, day_start_equity=1.0)[0] == {BTC: 0.9}  # inside the limit


def test_caps_always_hold_and_the_actions_explain_every_change() -> None:
    config = PortfolioRiskConfig(max_gross_exposure=1.2, max_net_exposure=0.7, max_instrument_weight=0.6, max_venue_exposure={"kraken_futures": 0.9},
                                 daily_loss_limit=None, drawdown_derisk_start=None)
    rng = np.random.default_rng(0)
    for _ in range(300):
        targets = dict(zip((BTC, ETH, SPOT), rng.normal(0.0, 0.8, 3)))
        out, actions = apply_portfolio_risk(targets, config=config, venues=VENUES, can_short=CAN_SHORT)
        tolerance = 1e-9
        assert all(abs(weight) <= config.max_instrument_weight + tolerance for weight in out.values())
        assert abs(out[BTC]) + abs(out[ETH]) <= 0.9 + tolerance
        assert abs(sum(out.values())) <= config.max_net_exposure + tolerance
        assert sum(abs(weight) for weight in out.values()) <= config.max_gross_exposure + tolerance
        assert out[SPOT] >= 0.0
        for instrument, start in targets.items():
            value = start
            for action in (a for a in actions if a.instrument == instrument):
                assert action.before == pytest.approx(value)
                value = action.after
            assert value == pytest.approx(out[instrument])


def _days(count: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=count, freq="D", tz="UTC")


def test_the_research_hook_flattens_at_max_drawdown_and_stays_flat() -> None:
    config = PortfolioRiskConfig(max_drawdown=0.25, drawdown_derisk_start=None, daily_loss_limit=None)
    prices = pd.DataFrame({"BTC": [100.0, 90.0, 80.0, 74.0, 60.0, 90.0]}, index=_days(6))
    weights = pd.DataFrame({"BTC": 1.0}, index=prices.index)
    hook = array_overlay(["BTC"], config=config, venues={"BTC": "kraken_futures"}, can_short={"BTC": True})
    result = simulate_portfolio(prices, weights, costs=FREE, adjust_targets=hook)
    assert result.gross_exposure.tolist() == pytest.approx([1.0, 1.0, 1.0, 0.0, 0.0, 0.0])
    assert result.equity.iloc[-1] == pytest.approx(0.74)


@pytest.mark.parametrize("freq", ["1D", "4h"])
def test_the_research_hook_counts_the_first_bar_of_a_day_toward_that_days_loss(freq: str) -> None:
    """A bar's P&L happens during the day it is stamped with, so the day starts at the equity before that bar."""
    config = PortfolioRiskConfig(daily_loss_limit=0.05, drawdown_derisk_start=None, max_drawdown=0.9)
    index = pd.DatetimeIndex([pd.Timestamp("2024-01-01 20:00", tz="UTC"), pd.Timestamp("2024-01-02 00:00", tz="UTC")]) if freq == "4h" else _days(2)
    prices = pd.DataFrame({"BTC": [100.0, 85.0]}, index=index)  # the day's first bar loses 15%, 7.5% of equity
    weights = pd.DataFrame({"BTC": [0.5, 1.0]}, index=index)  # and the sleeves want to add
    hook = array_overlay(["BTC"], config=config, venues={"BTC": "kraken_futures"}, can_short={"BTC": True})
    result = simulate_portfolio(prices, weights, costs=FREE, adjust_targets=hook)
    held = 0.5 * 0.85 / 0.925  # the drifted half position, not topped up
    assert result.gross_exposure.iloc[-1] == pytest.approx(held)


def test_the_money_cap_limits_total_position_value() -> None:
    config = PortfolioRiskConfig(max_gross_exposure=10.0, max_net_exposure=10.0, max_instrument_weight=10.0, max_gross_notional=6_000.0)
    out, actions = _apply({BTC: 0.6, ETH: -0.4}, config, equity=10_000.0, peak_equity=10_000.0)
    assert out == pytest.approx({BTC: 0.36, ETH: -0.24}) and {rule for rule, _ in actions} == {"notional_cap"}
    assert _apply({BTC: 0.3, ETH: -0.2}, config, equity=10_000.0, peak_equity=10_000.0)[1] == []
    with pytest.raises(ValueError, match="max_gross_notional"):
        PortfolioRiskConfig(max_gross_notional=0.0)
