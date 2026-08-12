"""Tests for the local market snapshot store."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.data.exchanges import MarketTick
from src.storage.market_store import MarketStore


def test_market_store_persists_and_reads_ticks(tmp_path: Path) -> None:
    """The store should write MarketTick rows and return them in reverse order."""
    store = MarketStore(database_path=tmp_path / "market.db")
    tick = MarketTick(
        exchange="firi",
        symbol="BTC/NOK",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        bid=100.0,
        ask=101.0,
        last=100.5,
        volume=2.5,
        raw={"source": "test"},
    )

    row_id = store.save_tick(tick)
    assert row_id > 0

    rows = store.list_ticks(limit=5)
    assert len(rows) == 1
    assert rows[0]["exchange"] == "firi"
    assert rows[0]["symbol"] == "BTC/NOK"
    assert rows[0]["last"] == 100.5
