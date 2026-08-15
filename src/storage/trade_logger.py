"""Simple SQLite-backed trade, equity, and operational event logger for backtests and paper trading."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_summary_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    total_trades INTEGER NOT NULL,
                    starting_equity REAL NOT NULL,
                    ending_equity REAL NOT NULL,
                    total_pnl REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    max_drawdown_pct REAL NOT NULL,
                    alert_count INTEGER NOT NULL,
                    active_alerts TEXT NOT NULL,
                    runtime_status TEXT NOT NULL,
                    research_status TEXT NOT NULL,
                    summary_text TEXT NOT NULL
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

    def write_daily_summary(
        self,
        *,
        timestamp: datetime | None = None,
        total_trades: int,
        starting_equity: float,
        ending_equity: float,
        total_pnl: float,
        max_drawdown: float,
        max_drawdown_pct: float,
        alert_count: int,
        active_alerts: Sequence[str] | None = None,
        runtime_status: str = "unknown",
        research_status: str = "not_configured",
        summary_text: str | None = None,
    ) -> dict[str, Any]:
        """Persist a daily summary report for later review."""
        report_date = (timestamp or datetime.now(timezone.utc)).date().isoformat()
        created_at = (timestamp or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
        active_alerts_payload = json.dumps(list(active_alerts or []))
        summary_text = summary_text or (
            f"date={report_date} trades={total_trades} pnl={total_pnl:.4f} drawdown={max_drawdown:.4f} alerts={alert_count}"
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO daily_summary_reports (
                    report_date,
                    created_at,
                    total_trades,
                    starting_equity,
                    ending_equity,
                    total_pnl,
                    max_drawdown,
                    max_drawdown_pct,
                    alert_count,
                    active_alerts,
                    runtime_status,
                    research_status,
                    summary_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_date) DO UPDATE SET
                    created_at=excluded.created_at,
                    total_trades=excluded.total_trades,
                    starting_equity=excluded.starting_equity,
                    ending_equity=excluded.ending_equity,
                    total_pnl=excluded.total_pnl,
                    max_drawdown=excluded.max_drawdown,
                    max_drawdown_pct=excluded.max_drawdown_pct,
                    alert_count=excluded.alert_count,
                    active_alerts=excluded.active_alerts,
                    runtime_status=excluded.runtime_status,
                    research_status=excluded.research_status,
                    summary_text=excluded.summary_text
                """,
                (
                    report_date,
                    created_at,
                    int(total_trades),
                    float(starting_equity),
                    float(ending_equity),
                    float(total_pnl),
                    float(max_drawdown),
                    float(max_drawdown_pct),
                    int(alert_count),
                    active_alerts_payload,
                    runtime_status,
                    research_status,
                    summary_text,
                ),
            )
            connection.commit()

        return {
            "report_date": report_date,
            "created_at": created_at,
            "total_trades": int(total_trades),
            "starting_equity": float(starting_equity),
            "ending_equity": float(ending_equity),
            "total_pnl": float(total_pnl),
            "max_drawdown": float(max_drawdown),
            "max_drawdown_pct": float(max_drawdown_pct),
            "alert_count": int(alert_count),
            "active_alerts": list(active_alerts or []),
            "runtime_status": runtime_status,
            "research_status": research_status,
            "summary_text": summary_text,
        }

    def get_daily_summary(
        self,
        *,
        report_date: datetime | date | None = None,
        runtime_status: str = "unknown",
        research_status: str = "not_configured",
        active_alerts: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Return the daily summary for the requested day, computing it if needed."""
        if isinstance(report_date, datetime):
            report_day = report_date.date().isoformat()
        elif isinstance(report_date, date):
            report_day = report_date.isoformat()
        else:
            report_day = datetime.now(timezone.utc).date().isoformat()

        with closing(sqlite3.connect(self.database_path)) as connection:
            trades = connection.execute(
                "SELECT timestamp, side, price, size, fee FROM trades ORDER BY id DESC"
            ).fetchall()
            snapshots = connection.execute(
                "SELECT timestamp, equity FROM equity_snapshots ORDER BY id DESC"
            ).fetchall()
            events = connection.execute(
                "SELECT timestamp, event_type FROM operational_events ORDER BY id DESC"
            ).fetchall()

        parsed_trades = []
        for timestamp, side, price, size, fee in trades:
            parsed_timestamp = self._parse_timestamp(timestamp)
            if parsed_timestamp is None:
                continue
            if parsed_timestamp.date().isoformat() != report_day:
                continue
            parsed_trades.append((parsed_timestamp, side, price, size, fee))

        all_snapshots = []
        for timestamp, equity in snapshots:
            parsed_timestamp = self._parse_timestamp(timestamp)
            if parsed_timestamp is None:
                continue
            all_snapshots.append((parsed_timestamp, float(equity)))

        all_snapshots.sort(key=lambda item: item[0])
        parsed_snapshots = []
        for parsed_timestamp, equity in all_snapshots:
            if parsed_timestamp.date().isoformat() != report_day:
                continue
            parsed_snapshots.append((parsed_timestamp, equity))

        parsed_alerts = []
        for timestamp, event_type in events:
            parsed_timestamp = self._parse_timestamp(timestamp)
            if parsed_timestamp is None:
                continue
            if parsed_timestamp.date().isoformat() != report_day:
                continue
            if event_type == "runtime_alert":
                parsed_alerts.append(event_type)

        if parsed_snapshots:
            parsed_snapshots.sort(key=lambda item: item[0])
            previous_snapshot = None
            for snapshot_timestamp, snapshot_equity in all_snapshots:
                if snapshot_timestamp < parsed_snapshots[0][0]:
                    previous_snapshot = (snapshot_timestamp, snapshot_equity)
            starting_equity = float(previous_snapshot[1]) if previous_snapshot is not None else 1000.0
            ending_equity = float(parsed_snapshots[-1][1])
            peak_equity = max(float(snapshot[1]) for snapshot in parsed_snapshots)
            trough_equity = min(float(snapshot[1]) for snapshot in parsed_snapshots)
            max_drawdown = max(0.0, peak_equity - trough_equity)
            max_drawdown_pct = max_drawdown / peak_equity if peak_equity > 0 else 0.0
        else:
            starting_equity = 1000.0
            ending_equity = 1000.0
            max_drawdown = 0.0
            max_drawdown_pct = 0.0

        total_pnl = ending_equity - starting_equity
        return self.write_daily_summary(
            timestamp=datetime.fromisoformat(report_day + "T00:00:00+00:00"),
            total_trades=len(parsed_trades),
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            total_pnl=total_pnl,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            alert_count=len(parsed_alerts),
            active_alerts=list(active_alerts or []),
            runtime_status=runtime_status,
            research_status=research_status,
            summary_text=f"date={report_day} trades={len(parsed_trades)} pnl={total_pnl:.4f} drawdown={max_drawdown:.4f} alerts={len(parsed_alerts)}",
        )

    @staticmethod
    def _parse_timestamp(value: str | datetime | None) -> datetime | None:
        """Parse a persisted timestamp into a timezone-aware datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            text = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        return None
