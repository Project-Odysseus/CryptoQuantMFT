"""Simple SQLite-backed trade, equity, and operational event logger for backtests and paper trading."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


class TradeLogger:
    """Persist trade and equity snapshots for local review."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.database_path = Path(database_path or "data/trades.db")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    fee REAL NOT NULL,
                    role_maker_taker TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    position_size REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operational_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metadata TEXT
                )
                """
            )
            connection.commit()

    def log_trade(self, *, timestamp: datetime, source: str, exchange: str, pair: str, side: str, price: float, size: float, fee: float, role_maker_taker: str = "taker", latency_ms: int = 0) -> int:
        """Persist a single trade record."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO trades (
                    timestamp,
                    source,
                    exchange,
                    pair,
                    side,
                    price,
                    size,
                    fee,
                    role_maker_taker,
                    latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    source,
                    exchange,
                    pair,
                    side,
                    price,
                    size,
                    fee,
                    role_maker_taker,
                    latency_ms,
                ),
            )
            connection.commit()
        return int(cursor.lastrowid)

    def log_event(self, *, timestamp: datetime, level: str, event_type: str, message: str, source: str, metadata: dict[str, Any] | None = None) -> int:
        """Persist a single operational event."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO operational_events (
                    timestamp,
                    level,
                    event_type,
                    message,
                    source,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    level,
                    event_type,
                    message,
                    source,
                    str(metadata or {}),
                ),
            )
            connection.commit()
        return int(cursor.lastrowid)

    def log_equity_snapshot(self, *, timestamp: datetime, source: str, equity: float, cash: float, position_size: float) -> int:
        """Persist a single equity snapshot."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO equity_snapshots (
                    timestamp,
                    source,
                    equity,
                    cash,
                    position_size
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    source,
                    equity,
                    cash,
                    position_size,
                ),
            )
            connection.commit()
        return int(cursor.lastrowid)

    def list_trades(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return persisted trades in reverse chronological order."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms FROM trades ORDER BY id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms FROM trades ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "timestamp": timestamp,
                "source": source,
                "exchange": exchange,
                "pair": pair,
                "side": side,
                "price": price,
                "size": size,
                "fee": fee,
                "role_maker_taker": role_maker_taker,
                "latency_ms": latency_ms,
            }
            for timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms in rows
        ]

    def list_equity_snapshots(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return persisted equity snapshots in reverse chronological order."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT timestamp, source, equity, cash, position_size FROM equity_snapshots ORDER BY id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT timestamp, source, equity, cash, position_size FROM equity_snapshots ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "timestamp": timestamp,
                "source": source,
                "equity": equity,
                "cash": cash,
                "position_size": position_size,
            }
            for timestamp, source, equity, cash, position_size in rows
        ]

    def list_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return persisted operational events in reverse chronological order."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT timestamp, level, event_type, message, source, metadata FROM operational_events ORDER BY id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT timestamp, level, event_type, message, source, metadata FROM operational_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "timestamp": timestamp,
                "level": level,
                "event_type": event_type,
                "message": message,
                "source": source,
                "metadata": metadata,
            }
            for timestamp, level, event_type, message, source, metadata in rows
        ]
