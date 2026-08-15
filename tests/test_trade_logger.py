"""Tests for the SQLite-backed trade logger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.storage.trade_logger import TradeLogger


def test_trade_logger_persists_trade_and_equity_records(tmp_path: Path) -> None:
    """The trade logger should write trade and equity rows to SQLite."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    timestamp = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    trade_id = logger.log_trade(
        timestamp=timestamp,
        source="paper_trading",
        exchange="mock",
        pair="BTC/NOK",
        side="buy",
        price=100.0,
        size=1.0,
        fee=0.4,
    )
    equity_id = logger.log_equity_snapshot(
        timestamp=timestamp,
        source="paper_trading",
        equity=1000.0,
        cash=1000.0,
        position_size=0.0,
    )

    assert trade_id > 0
    assert equity_id > 0
    assert len(logger.list_trades()) == 1
    assert len(logger.list_equity_snapshots()) == 1


def test_trade_logger_writes_daily_summary(tmp_path: Path) -> None:
    """The trade logger should persist a daily summary aggregation."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    timestamp = datetime.now(timezone.utc)

    logger.log_trade(
        timestamp=timestamp,
        source="paper_trading",
        exchange="mock",
        pair="BTC/NOK",
        side="buy",
        price=100.0,
        size=1.0,
        fee=0.4,
    )
    logger.log_equity_snapshot(
        timestamp=timestamp,
        source="paper_trading",
        equity=1100.0,
        cash=1100.0,
        position_size=0.0,
    )
    logger.log_event(
        timestamp=timestamp,
        level="WARNING",
        event_type="runtime_alert",
        message="heartbeat stalled",
        source="runtime",
    )

    summary = logger.get_daily_summary(
        report_date=timestamp,
        runtime_status="healthy",
        research_status="parallel_lane_pending",
        active_alerts=["heartbeat_lost"],
    )

    assert summary["total_trades"] == 1
    assert summary["total_pnl"] == 100.0
    assert summary["alert_count"] == 1
    assert summary["runtime_status"] == "healthy"
    assert summary["research_status"] == "parallel_lane_pending"
