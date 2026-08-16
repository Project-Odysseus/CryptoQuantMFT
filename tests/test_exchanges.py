"""Tests for the exchange connector scaffolding."""

from __future__ import annotations

import pytest

from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector
from src.storage.market_store import MarketStore
from src.storage.streaming_aggregator import StreamingAggregator


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
async def test_mock_exchange_connector_produces_realistic_price_random_walk() -> None:
    """The mock connector should produce a realistic BTC/NOK starting price and fluctuate."""
    connector = MockExchangeConnector(symbol="BTC/NOK")

    await connector.connect()
    snapshots = [await connector.fetch_snapshot() for _ in range(20)]

    prices = [s.last for s in snapshots]
    # Starting price should be realistic (~1,000,000 NOK range for BTC)
    assert prices[0] > 100_000, f"expected realistic BTC/NOK price, got {prices[0]}"
    # Price should have moved both up and down (random walk, not monotone drift)
    # With 20 ticks the probability of ALL going same direction is ~2 * (0.5^20) ≈ 0.000002
    # So this is effectively deterministic unless the RNG is degenerate
    diffs = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    assert any(d > 0 for d in diffs), "expected at least one upward tick"
    assert any(d < 0 for d in diffs), "expected at least one downward tick"

    await connector.disconnect()


@pytest.mark.asyncio
async def test_mock_connector_persists_to_store(tmp_path: pytest.TempPathFactory) -> None:
    """The connector should persist snapshots when a store is supplied."""
    store = MarketStore(database_path=tmp_path / "persisted.db")
    connector = MockExchangeConnector(symbol="BTC/NOK", store=store)

    await connector.connect()
    await connector.fetch_snapshot()

    rows = store.list_ticks(limit=5)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_mock_connector_updates_streaming_aggregator(tmp_path: pytest.TempPathFactory) -> None:
    """The connector should feed the streaming aggregator when one is supplied."""
    store = MarketStore(database_path=tmp_path / "aggregate.db")
    aggregator = StreamingAggregator(store=store, interval_seconds=60)
    connector = MockExchangeConnector(symbol="BTC/NOK", store=store, aggregator=aggregator)

    await connector.connect()
    await connector.fetch_snapshot()

    assert ("mock", "BTC/NOK") in aggregator.bars


@pytest.mark.asyncio
async def test_firi_connector_parses_market_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """FiriConnector should normalize the market payload into a MarketTick."""

    def fake_request_json(self: FiriConnector, method: str, url: str, **_: object) -> list[dict[str, object]]:
        """Perform the fake request json operation."""
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
        """Perform the fake request json operation."""
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
