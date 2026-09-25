"""Tests for the SQLite-backed trade logger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.storage.trade_logger import TradeLogger


def test_trade_logger_persists_trade_and_equity_records(tmp_path: Path) -> None:
    """The trade logger should write trade and equity rows to SQLite."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    timestamp = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    trade_id, tax_event_error = logger.log_trade(
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
    assert tax_event_error is None
    assert equity_id > 0
    assert len(logger.list_trades()) == 1
    assert len(logger.list_equity_snapshots()) == 1


def test_trade_logger_persists_strategy_id(tmp_path: Path) -> None:
    """Trades should carry an optional strategy_id for future portfolio attribution."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    timestamp = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    logger.log_trade(
        timestamp=timestamp,
        source="paper_trading",
        exchange="mock",
        pair="BTC/NOK",
        side="buy",
        price=100.0,
        size=1.0,
        fee=0.4,
        strategy_id="moving_average_crossover",
    )
    logger.log_trade(
        timestamp=timestamp,
        source="cli_manual_live_order",
        exchange="kraken",
        pair="BTC/EUR",
        side="buy",
        price=100.0,
        size=1.0,
        fee=0.4,
    )

    trades = logger.list_trades()
    assert {trade["strategy_id"] for trade in trades} == {"moving_average_crossover", None}


def test_trade_logger_adds_strategy_id_column_to_pre_existing_database(tmp_path: Path) -> None:
    """A database created before strategy_id existed should be migrated in place."""
    import sqlite3
    from contextlib import closing

    database_path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE trades (
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
        connection.commit()

    logger = TradeLogger(database_path=database_path)
    trade_id, _ = logger.log_trade(
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        source="paper_trading",
        exchange="mock",
        pair="BTC/NOK",
        side="buy",
        price=100.0,
        size=1.0,
        fee=0.4,
        strategy_id="baseline",
    )

    assert trade_id > 0
    assert logger.list_trades()[0]["strategy_id"] == "baseline"


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


def test_trade_logger_round_trips_structured_event_metadata(tmp_path: Path) -> None:
    """Operational event metadata should be stored as JSON and returned as a structured object."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    timestamp = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    logger.log_event(
        timestamp=timestamp,
        level="INFO",
        event_type="runtime_execution_context",
        message="context updated",
        source="runtime",
        metadata={"timestamp": timestamp, "path": tmp_path / "state.json", "latest_signal": 1.0},
    )

    events = logger.list_events(limit=1)

    assert events[0]["metadata"]["timestamp"] == timestamp.isoformat()
    assert events[0]["metadata"]["path"] == str(tmp_path / "state.json")
    assert events[0]["metadata"]["latest_signal"] == 1.0


def test_trade_logger_list_events_filters_by_event_type(tmp_path: Path) -> None:
    """list_events should find a rare event type even when it is buried under high-frequency noise."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    timestamp = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    logger.log_event(timestamp=timestamp, level="WARNING", event_type="kraken_manual_order_submission", message="manual order", source="main")
    for _ in range(50):
        logger.log_event(timestamp=timestamp, level="INFO", event_type="runtime_cycle_completed", message="cycle", source="runtime")

    events = logger.list_events(limit=10, event_types=["kraken_manual_order_submission", "kill_switch_activated"])

    assert len(events) == 1
    assert events[0]["event_type"] == "kraken_manual_order_submission"


def test_trade_logger_builds_tax_ledger_for_eur_trades_and_fiat_pool(tmp_path: Path) -> None:
    """EUR-denominated trades should create tax-ledger entries with FIFO basis tracking."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    logger.fx_rate_collector.get_rate = lambda pair="EUR/NOK", at=None: 11.0  # type: ignore[method-assign]
    buy_time = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    sell_time = datetime(2024, 1, 3, 10, 0, tzinfo=timezone.utc)

    logger.log_fiat_conversion(
        timestamp=buy_time,
        amount_eur=1000.0,
        source="manual_seed",
        fx_rate=11.0,
        reference="initial_capital",
    )
    logger.log_trade(
        timestamp=buy_time,
        source="live_trading",
        exchange="kraken",
        pair="BTC/EUR",
        side="buy",
        price=100.0,
        size=2.0,
        fee=1.0,
        record_tax_event=True,
    )
    logger.log_trade(
        timestamp=sell_time,
        source="live_trading",
        exchange="kraken",
        pair="BTC/EUR",
        side="sell",
        price=120.0,
        size=1.0,
        fee=1.0,
        record_tax_event=True,
    )

    tax_events = logger.list_tax_events(tax_year=2024)
    summary = logger.get_tax_year_summary(2024)

    realized_pnl_event = next(event for event in tax_events if event["transaction_type"] == "REALIZED_PNL")
    fee_events = [event for event in tax_events if event["transaction_type"] == "TRADING_FEE"]
    fiat_events = [event for event in tax_events if event["transaction_type"] == "FIAT_CONVERSION"]

    assert realized_pnl_event["amount_eur"] == 20.0
    assert realized_pnl_event["amount_nok"] == 220.0
    assert len(fee_events) == 2
    assert len(fiat_events) == 5
    assert summary["total_gross_taxable_gains_nok"] == 220.0
    assert summary["total_gross_deductible_losses_nok"] == 22.0
    assert summary["net_foreign_currency_gain_loss_nok"] == 0.0


def test_trade_logger_exports_tax_ledger_and_persists_year_end_holdings(tmp_path: Path) -> None:
    """The tax-ledger export and year-end valuation should be persisted for later reporting."""
    logger = TradeLogger(database_path=tmp_path / "trades.db")
    logger.fx_rate_collector.get_rate = lambda pair="EUR/NOK", at=None: 11.5  # type: ignore[method-assign]
    timestamp = datetime(2024, 12, 31, 22, 59, tzinfo=timezone.utc)

    logger.log_fiat_conversion(
        timestamp=timestamp,
        amount_eur=500.0,
        source="manual_seed",
        fx_rate=11.5,
        reference="initial_capital",
    )
    holdings = logger.write_year_end_holdings(
        timestamp=timestamp,
        holdings={"EUR": 500.0, "BTC": 0.1},
        prices_eur={"BTC": 40000.0},
    )
    export_path = logger.export_tax_ledger(path=tmp_path / "tax_2024.json", tax_year=2024)
    summary = logger.get_tax_year_summary(2024)

    assert export_path.exists() is True
    assert holdings["total_value_eur"] == 4500.0
    assert holdings["total_value_nok"] == 51750.0
    assert summary["wealth_tax_snapshot"]["total_value_nok"] == 51750.0
