"""Local persistence helpers for market snapshots."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.exchanges import MarketTick


class MarketStore:
    """Persist normalized market ticks in SQLite and parquet for quick reuse."""

    def __init__(self, database_path: str | Path | None = None, parquet_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or "data/market_snapshots.db")
        self.parquet_path = Path(parquet_path or self.database_path.with_suffix(".parquet"))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    bid REAL NOT NULL,
                    ask REAL NOT NULL,
                    last REAL,
                    volume REAL,
                    raw_json TEXT
                )
                """
            )
            connection.commit()

    def save_tick(self, tick: MarketTick) -> int:
        """Persist a normalized market tick and return its row id."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO market_ticks (
                    exchange,
                    symbol,
                    timestamp,
                    bid,
                    ask,
                    last,
                    volume,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tick.exchange,
                    tick.symbol,
                    tick.timestamp.isoformat(),
                    tick.bid,
                    tick.ask,
                    tick.last,
                    tick.volume,
                    self._serialize_raw(tick.raw),
                ),
            )
            connection.commit()

        self._write_parquet()
        return int(cursor.lastrowid)

    def list_ticks(self, limit: int | None = 10) -> list[dict[str, Any]]:
        """Return recent persisted ticks."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT exchange, symbol, timestamp, bid, ask, last, volume, raw_json
                    FROM market_ticks
                    ORDER BY id DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT exchange, symbol, timestamp, bid, ask, last, volume, raw_json
                    FROM market_ticks
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

        return [
            {
                "exchange": exchange,
                "symbol": symbol,
                "timestamp": timestamp,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": volume,
                "raw_json": raw_json,
            }
            for exchange, symbol, timestamp, bid, ask, last, volume, raw_json in rows
        ]

    def read_parquet_ticks(self) -> list[dict[str, Any]]:
        """Load persisted ticks from parquet when available."""
        if not self.parquet_path.exists():
            return []

        dataframe = pd.read_parquet(self.parquet_path)
        return dataframe.to_dict(orient="records")

    def _write_parquet(self) -> None:
        rows = self.list_ticks(limit=None)
        if not rows:
            empty_frame = pd.DataFrame(columns=["exchange", "symbol", "timestamp", "bid", "ask", "last", "volume", "raw_json"])
            empty_frame.to_parquet(self.parquet_path, index=False)
            return

        dataframe = pd.DataFrame(rows)
        dataframe.to_parquet(self.parquet_path, index=False)

    def _serialize_raw(self, raw: dict[str, Any] | None) -> str | None:
        if raw is None:
            return None
        return str(raw)
