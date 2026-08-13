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
