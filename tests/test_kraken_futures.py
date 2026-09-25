"""Tests for the Kraken Futures public-data client and the verified-contract builder (payloads trimmed from real responses)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.data import kraken_futures
from src.data.kraken_futures import FundingRate, fetch_funding_history, fetch_instrument, summarize_funding, venue_symbol_for
from src.execution.perps import SandboxPerpExecutionAdapter, perp_contract_from_instrument

INSTRUMENT = {
    "symbol": "PF_XBTUSD",
    "type": "flexible_futures",
    "tickSize": 1,
    "contractSize": 1,
    "contractValueTradePrecision": 4,
    "feeScheduleUid": "723888f7",
    "base": "BTC",
    "quote": "USD",
    "retailMarginLevels": [
        {"numNonContractUnits": 1000000.0, "initialMargin": 0.02, "maintenanceMargin": 0.01},
        {"numNonContractUnits": 0.0, "initialMargin": 0.01, "maintenanceMargin": 0.005},
        {"numNonContractUnits": 3000000.0, "initialMargin": 0.04, "maintenanceMargin": 0.02},
    ],
}
FEE_SCHEDULE = {"uid": "723888f7", "tiers": [{"makerFee": 0.0175, "takerFee": 0.045, "usdVolume": 5000000.0}, {"makerFee": 0.02, "takerFee": 0.05, "usdVolume": 0.0}]}


def test_contract_is_built_from_venue_data_and_marked_verified() -> None:
    """Size step, tick, fees (entry tier), leverage and margin tiers all come from the instrument and fee schedule."""
    contract = perp_contract_from_instrument(INSTRUMENT, FEE_SCHEDULE, symbol="BTC/USD")

    assert contract.verified and contract.venue_symbol == "PF_XBTUSD"
    assert (contract.base_asset, contract.collateral_currency) == ("BTC", "USD")
    assert contract.size_step == pytest.approx(0.0001) and contract.min_size == pytest.approx(0.0001)
    assert contract.tick_size == 1.0
    assert (contract.taker_fee_rate, contract.maker_fee_rate) == (pytest.approx(0.0005), pytest.approx(0.0002))
    assert contract.max_leverage == pytest.approx(100.0)
    assert contract.maintenance_margin_rate == 0.005
    assert [tier[0] for tier in contract.margin_tiers] == [0.0, 1000000.0, 3000000.0]  # sorted regardless of input order


def test_maintenance_rate_steps_up_with_position_size() -> None:
    """Bigger positions face a higher maintenance rate."""
    contract = perp_contract_from_instrument(INSTRUMENT, FEE_SCHEDULE)
    assert contract.maintenance_rate_at(50_000.0) == 0.005
    assert contract.maintenance_rate_at(1_000_000.0) == 0.01
    assert contract.maintenance_rate_at(9_000_000.0) == 0.02


def test_sandbox_liquidates_using_the_venue_maintenance_rate() -> None:
    """With the real 0.5% maintenance rate the account is liquidated later than with the 1% placeholder."""
    contract = perp_contract_from_instrument(INSTRUMENT, FEE_SCHEDULE, symbol="BTC/USD")
    adapter = SandboxPerpExecutionAdapter(contract=contract, starting_collateral=100.0, max_leverage=2.0, funding_pct_per_day=0.0)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert adapter.submit_order(order_id="o1", side="buy", size=0.0039, price=50000.0, timestamp=t0, symbol="BTC/USD").status == "FILLED"
    liq = adapter.liquidation_price()
    assert liq is not None and liq < 24630.28  # the 1% placeholder gave 24630


def test_venue_symbol_mapping() -> None:
    """Runtime symbols map to Kraken's perpetual names, and unknown symbols fail clearly."""
    assert venue_symbol_for("btc/usd") == "PF_XBTUSD"
    with pytest.raises(ValueError, match="no Kraken perpetual"):
        venue_symbol_for("DOGE/USD")


def test_client_parses_instruments_and_funding_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public client reads the documented response shapes and sorts funding oldest first."""
    payloads = {
        "/v3/instruments": {"result": "success", "instruments": [INSTRUMENT]},
        "/v4/historicalfundingrates": {
            "result": "success",
            "rates": [
                {"timestamp": "2025-09-24T09:00:00Z", "fundingRate": 1.5, "relativeFundingRate": 0.000012},
                {"timestamp": "2025-09-24T08:00:00Z", "fundingRate": 1.3, "relativeFundingRate": -0.000004},
            ],
        },
    }
    monkeypatch.setattr(kraken_futures, "_request_json", lambda method, url, params=None: payloads[url.split("/derivatives/api")[1]])

    assert fetch_instrument("PF_XBTUSD")["base"] == "BTC"
    with pytest.raises(ValueError, match="not listed"):
        fetch_instrument("PF_NOPEUSD")
    rates = fetch_funding_history("PF_XBTUSD")
    assert [rate.timestamp.hour for rate in rates] == [8, 9]
    assert rates[1].pct_per_day == pytest.approx(0.000012 * 24 * 100)


def test_client_raises_when_the_api_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-success result must not be mistaken for empty data."""
    monkeypatch.setattr(kraken_futures, "_request_json", lambda method, url, params=None: {"result": "error", "error": "apiLimitExceeded"})
    with pytest.raises(RuntimeError, match="failed"):
        fetch_instrument("PF_XBTUSD")


def test_funding_summary_statistics() -> None:
    """Mean, share of negative hours and extremes are computed in percent per day."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rates = [FundingRate(base, hourly) for hourly in (-0.00001, 0.00001, 0.00003, 0.00001)]
    summary = summarize_funding(rates)

    assert summary["share_negative"] == 0.25
    assert summary["mean_pct_per_day"] == pytest.approx(0.00001 * 24 * 100)
    assert summary["max_pct_per_day"] == pytest.approx(0.00003 * 24 * 100)
    assert summary["min_pct_per_day"] == pytest.approx(-0.00001 * 24 * 100)
    with pytest.raises(ValueError):
        summarize_funding([])
