"""Tests for the Kraken Futures mark-price market-data connector."""

from __future__ import annotations

import pytest

from src.data.exchanges import KrakenFuturesConnector

TICKER = {"result": "success", "ticker": {"symbol": "PF_XBTUSD", "last": 84078, "markPrice": 84077.39, "bid": 84077, "ask": 84078, "vol24h": 4156.6, "indexPrice": 84079.7, "fundingRate": -0.92, "suspended": False}}


@pytest.mark.asyncio
async def test_snapshot_prices_bars_on_the_mark_price_and_keeps_the_index() -> None:
    """`last` (what bars close on) is the mark price; bid/ask are the contract's own book."""
    connector = KrakenFuturesConnector(symbol="BTC/USD")
    connector._request_json = lambda method, url, **kwargs: TICKER  # type: ignore[method-assign]
    await connector.connect()
    tick = await connector.fetch_snapshot()

    assert tick.exchange == "kraken_futures" and tick.symbol == "BTC/USD"
    assert tick.last == 84077.39
    assert (tick.bid, tick.ask) == (84077.0, 84078.0)
    assert tick.raw["index_price"] == 84079.7 and tick.raw["venue_symbol"] == "PF_XBTUSD"
    assert tick.volume == 0.0  # no baseline on the first poll


@pytest.mark.asyncio
async def test_volume_is_the_floored_change_in_rolling_24h_volume() -> None:
    """Rising rolling volume gives a positive delta; a falling rolling total never goes negative."""
    connector = KrakenFuturesConnector(symbol="BTC/USD")
    volumes = iter([100.0, 103.5, 101.0])
    connector._request_json = lambda method, url, **kwargs: {"result": "success", "ticker": {**TICKER["ticker"], "vol24h": next(volumes)}}  # type: ignore[method-assign]
    connector._connected = True
    assert (await connector.fetch_snapshot()).volume == 0.0
    assert (await connector.fetch_snapshot()).volume == pytest.approx(3.5)
    assert (await connector.fetch_snapshot()).volume == 0.0


@pytest.mark.asyncio
async def test_suspended_contract_refuses_to_connect_and_unmapped_symbols_fail() -> None:
    """A suspended market must not start a runtime; unknown symbols fail at construction."""
    connector = KrakenFuturesConnector(symbol="BTC/USD")
    connector._request_json = lambda method, url, **kwargs: {"result": "success", "ticker": {**TICKER["ticker"], "suspended": True}}  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="suspended"):
        await connector.connect()
    with pytest.raises(ValueError):
        KrakenFuturesConnector(symbol="BTC/EUR")


def test_perp_runtime_uses_the_futures_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-mock perp dry run reads prices from Kraken Futures, not the spot feed."""
    from main import build_runtime_orchestrator

    monkeypatch.setattr("src.data.kraken_futures.fetch_instrument", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    _orchestrator, pipeline = build_runtime_orchestrator(mode="live_dry_run", exchange="kraken_futures", use_mock_connector=False)
    assert [type(connector).__name__ for connector in pipeline.connectors] == ["KrakenFuturesConnector"]
