"""Exposure limits: greeks and scenario loss per underlying against [risk.exposure], and how much of a proposal fits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.options.pricing import Black76
from src.portfolio.exposure import MarketState, Position
from src.portfolio.exposure_limits import book_positions, check_exposure, option_position, scale_for_limits
from src.portfolio.risk import PortfolioRiskConfig

NOW = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
MARKET = MarketState(now=NOW, spot={"BTC": 80_000.0}, models={"BTC": Black76(0.5)})
EQUITY = 10_000.0


def test_book_units_become_exposure_positions() -> None:
    positions = book_positions({"kraken_futures:BTC/USD": 0.1, "deribit:BTC_USDC-27NOV26-70000-P": -0.05, "kraken:XBT/EUR": 0.0}, {"deribit:BTC_USDC-27NOV26-70000-P": "option"})
    assert [(p.kind, p.underlying, p.quantity) for p in positions] == [("perp", "BTC", 0.1), ("option", "BTC", -0.05)]
    assert positions[1].strike == 70_000.0 and positions[1].right == "put" and positions[1].expiry.month == 11


def test_a_perp_long_breaks_a_delta_limit_and_a_crash_limit() -> None:
    long = [Position(kind="perp", underlying="BTC", quantity=0.15)]  # 12,000 of BTC on 10,000 of equity
    _table, grid, breaches = check_exposure(long, MARKET, {"max_delta": 1.0, "max_scenario_loss": 0.5}, EQUITY)
    assert {breach.rule for breach in breaches} == {"max_delta", "max_scenario_loss"}
    assert next(b for b in breaches if b.rule == "max_delta").value == pytest.approx(1.2, rel=1e-3)
    assert -grid.to_numpy().min() == pytest.approx(0.5 * 12_000, rel=1e-6)  # the -50% row


def test_short_options_break_vega_and_short_gamma_limits() -> None:
    expiry = NOW + timedelta(days=30)
    short_straddle = [option_position("deribit:BTC_USDC-27OCT26-80000-C", -0.5), option_position("deribit:BTC_USDC-27OCT26-80000-P", -0.5)]
    short_straddle = [Position(kind="option", underlying="BTC", quantity=p.quantity, strike=p.strike, expiry=expiry, right=p.right) for p in short_straddle]
    table, _grid, breaches = check_exposure(short_straddle, MARKET, {"max_vega_per_point": 0.005, "max_short_gamma": 0.001}, EQUITY)
    assert table.loc["BTC", "vega_1pt"] < 0 and table.loc["BTC", "gamma_1pct"] < 0
    assert {breach.rule for breach in breaches} == {"max_vega_per_point", "max_short_gamma"}


def test_scale_for_limits_finds_how_much_of_a_hedge_or_a_bet_fits() -> None:
    held = [Position(kind="perp", underlying="BTC", quantity=0.1)]  # 8,000 long
    puts = [Position(kind="option", underlying="BTC", quantity=0.1, strike=72_000.0, expiry=NOW + timedelta(days=60), right="put")]
    # A crash limit the unhedged long breaks: protective puts fix it, so all of them fit
    assert check_exposure(held, MARKET, {"max_scenario_loss": 0.3}, EQUITY)[2]
    assert scale_for_limits(held, puts, MARKET, {"max_scenario_loss": 0.3}, EQUITY) == 1.0
    # A delta limit the long already breaks can't be fixed by adding more long delta
    more_long = [Position(kind="perp", underlying="BTC", quantity=0.1)]
    assert scale_for_limits(held, more_long, MARKET, {"max_delta": 0.5}, EQUITY) == 0.0
    share = scale_for_limits([], more_long, MARKET, {"max_delta": 0.5}, EQUITY)
    assert share == pytest.approx(0.6)  # 0.6 x 8,000 = 4,800 of delta <= 5,000


def test_risk_config_validates_the_exposure_table() -> None:
    assert PortfolioRiskConfig(exposure={"max_delta": 1.0}).exposure == {"max_delta": 1.0}
    with pytest.raises(ValueError, match="unknown"):
        PortfolioRiskConfig(exposure={"max_theta": 1.0})
    with pytest.raises(ValueError, match="above 0"):
        PortfolioRiskConfig(exposure={"max_delta": -1.0})
