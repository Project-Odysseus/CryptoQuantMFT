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

    parquet_rows = store.read_parquet_ticks()
    assert len(parquet_rows) == 1
    assert parquet_rows[0]["exchange"] == "firi"
    assert parquet_rows[0]["raw_json"] == {"source": "test"}


def test_market_store_round_trips_raw_json_and_syncs_parquet_on_read(tmp_path: Path) -> None:
    """Structured raw payloads should round-trip through SQLite and parquet without per-save rewrites."""
    store = MarketStore(database_path=tmp_path / "market.db")
    first_tick = MarketTick(
        exchange="kraken",
        symbol="ETH/EUR",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        bid=200.0,
        ask=201.0,
        last=200.5,
        volume=1.5,
        raw={"source": "test", "sequence": 1},
    )
    second_tick = MarketTick(
        exchange="kraken",
        symbol="ETH/EUR",
        timestamp=datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc),
        bid=201.0,
        ask=202.0,
        last=201.5,
        volume=1.0,
        raw={"source": "test", "sequence": 2},
    )

    store.save_tick(first_tick)
    assert store.parquet_path.exists() is False

    store.save_tick(second_tick)
    rows = store.list_ticks(limit=2)
    parquet_rows = store.read_parquet_ticks()

    assert rows[0]["raw_json"] == {"source": "test", "sequence": 2}
    assert rows[1]["raw_json"] == {"source": "test", "sequence": 1}
    assert len(parquet_rows) == 2
    assert parquet_rows[-1]["raw_json"] == {"source": "test", "sequence": 1}
