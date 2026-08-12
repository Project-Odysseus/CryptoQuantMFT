"""Tests for the exchange connector scaffolding."""

from __future__ import annotations

import pytest

from src.data.exchanges import MockExchangeConnector


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
