"""Tests for the exchange connector scaffolding."""

from __future__ import annotations

import pytest

from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector


@pytest.mark.asyncio
async def test_mock_exchange_connector_fetch_snapshot() -> None:
    """The mock connector should return a normalized tick after connecting."""
    connector = MockExchangeConnector(symbol="BTC/NOK")

    await connector.connect()
    snapshot = await connector.fetch_snapshot()

    assert snapshot.exchange == "mock"
    assert snapshot.symbol == "BTC/NOK"
    assert snapshot.bid <= snapshot.ask
    assert snapshot.last is not None

    await connector.disconnect()


@pytest.mark.asyncio
async def test_firi_connector_parses_market_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """FiriConnector should normalize the market payload into a MarketTick."""

    def fake_request_json(self: FiriConnector, method: str, url: str, **_: object) -> list[dict[str, object]]:
        return [{"id": "BTCNOK", "last": "123.45", "todays_volume": "1.23"}]

    monkeypatch.setattr(FiriConnector, "_request_json", fake_request_json)
    connector = FiriConnector(symbol="BTC/NOK", api_key="test-key")

    await connector.connect()
    snapshot = await connector.fetch_snapshot()

    assert snapshot.bid == pytest.approx(123.45)
    assert snapshot.ask == pytest.approx(123.45)
    assert snapshot.last == pytest.approx(123.45)
    assert snapshot.volume == pytest.approx(1.23)


@pytest.mark.asyncio
async def test_kraken_connector_parses_market_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """KrakenConnector should normalize the ticker payload into a MarketTick."""

    def fake_request_json(self: KrakenConnector, method: str, url: str, **_: object) -> dict[str, object]:
        return {
            "error": [],
            "result": {
                "XXBTZEUR": {
                    "a": ["100.50", "1", "1.000"],
                    "b": ["100.00", "1", "1.000"],
                    "c": ["100.25", "0.01"],
                    "v": ["1.23", "4.56"],
                }
            },
        }

    monkeypatch.setattr(KrakenConnector, "_request_json", fake_request_json)
    connector = KrakenConnector(symbol="BTC/EUR", api_key="test-key", api_secret="test-secret")

    await connector.connect()
    snapshot = await connector.fetch_snapshot()

    assert snapshot.bid == pytest.approx(100.00)
    assert snapshot.ask == pytest.approx(100.50)
    assert snapshot.last == pytest.approx(100.25)
    assert snapshot.volume == pytest.approx(4.56)
