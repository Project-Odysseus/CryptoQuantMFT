"""Simple SQLite-backed trade and equity logger for backtests and paper trading."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


class TradeLogger:
    """Persist trade and equity snapshots for local review."""

    def __init__(self, database_path: str | Path | None = None) -> None:
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
