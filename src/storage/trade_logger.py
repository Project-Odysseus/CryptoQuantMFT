"""Simple SQLite-backed trade, equity, and operational event logger for backtests and paper trading."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from src.data.fx import FXRateCollector


class TradeLogger:
    """Persist trade and equity snapshots for local review."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.database_path = Path(database_path or "data/trades.db")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()
        self.fx_rate_collector = FXRateCollector(cache_path=self.database_path)

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
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    strategy_id TEXT
                )
                """
            )
            existing_trade_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(trades)").fetchall()
            }
            if "strategy_id" not in existing_trade_columns:
                connection.execute("ALTER TABLE trades ADD COLUMN strategy_id TEXT")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tax_ledger (
                    id TEXT PRIMARY KEY,
                    timestamp_utc TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    amount_eur REAL NOT NULL,
                    norges_bank_fx_rate REAL NOT NULL,
                    amount_nok REAL NOT NULL,
                    cost_basis_nok REAL NOT NULL,
                    metadata TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tax_asset_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_symbol TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    remaining_size REAL NOT NULL,
                    unit_cost_eur REAL NOT NULL,
                    source_trade_id INTEGER,
                    metadata TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tax_eur_fiat_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    acquired_at TEXT NOT NULL,
                    remaining_amount_eur REAL NOT NULL,
                    cost_basis_nok REAL NOT NULL,
                    source_ref TEXT,
                    metadata TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS year_end_holdings (
                    valuation_year INTEGER PRIMARY KEY,
                    timestamp_utc TEXT NOT NULL,
                    holdings_json TEXT NOT NULL,
                    total_value_eur REAL NOT NULL,
                    norges_bank_fx_rate REAL NOT NULL,
                    total_value_nok REAL NOT NULL
                )
                """
            )
            # One row per portfolio decision (and an hourly heartbeat row): the dashboard's source of truth.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    portfolio TEXT NOT NULL,
                    equity REAL NOT NULL,
                    gross_exposure REAL NOT NULL,
                    net_exposure REAL NOT NULL,
                    drawdown REAL NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def log_trade(
        self,
        *,
        timestamp: datetime,
        source: str,
        exchange: str,
        pair: str,
        side: str,
        price: float,
        size: float,
        fee: float,
        role_maker_taker: str = "taker",
        latency_ms: int = 0,
        record_tax_event: bool = False,
        strategy_id: str | None = None,
    ) -> tuple[int, str | None]:
        """Persist a single trade record.

        Args:
            strategy_id: Identifies which strategy (or manual flow) placed the
                trade. Optional today since only one strategy runs at a time,
                but recorded now so portfolio-level attribution does not need
                a historical backfill once multiple strategies run concurrently.

        Returns:
            A ``(trade_id, tax_event_error)`` pair. ``tax_event_error`` is ``None``
            when tax-ledger recording was skipped or succeeded, and holds the
            error message when ``record_tax_event`` was requested but failed, so
            callers can surface the failure instead of assuming success.
        """
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
                    latency_ms,
                    strategy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    strategy_id,
                ),
            )
            connection.commit()
        trade_id = int(cursor.lastrowid)

        tax_event_error: str | None = None
        if record_tax_event:
            try:
                self.record_trade_tax_events(
                    trade_id=trade_id,
                    timestamp=timestamp,
                    source=source,
                    exchange=exchange,
                    pair=pair,
                    side=side,
                    price=price,
                    size=size,
                    fee=fee,
                    role_maker_taker=role_maker_taker,
                )
            except Exception as exc:
                tax_event_error = str(exc)
                self.log_event(
                    timestamp=timestamp,
                    level="ERROR",
                    event_type="tax_ledger_error",
                    message="failed to write trade tax events",
                    source=source,
                    metadata={"trade_id": trade_id, "pair": pair, "side": side, "error": str(exc)},
                )
        return trade_id, tax_event_error

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
                    self._serialize_json(metadata or {}),
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
                    "SELECT timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms, strategy_id FROM trades ORDER BY id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms, strategy_id FROM trades ORDER BY id DESC LIMIT ?",
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
                "strategy_id": strategy_id,
            }
            for timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms, strategy_id in rows
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

    def log_portfolio_snapshot(self, *, timestamp: datetime, snapshot: dict[str, Any]) -> int:
        """Persist one portfolio snapshot (`PortfolioEngine.snapshot()`): equity, exposure, instruments, sleeves, risk."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            cursor = connection.execute(
                "INSERT INTO portfolio_snapshots (timestamp, portfolio, equity, gross_exposure, net_exposure, drawdown, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (timestamp.isoformat(), str(snapshot["portfolio"]), float(snapshot["equity"]), float(snapshot["gross"]), float(snapshot["net"]),
                 float(snapshot["drawdown"]), self._serialize_json(snapshot)),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_portfolio_snapshots(self, *, portfolio: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Portfolio snapshots, newest first (optionally for one portfolio), with the full payload decoded."""
        query = "SELECT timestamp, payload FROM portfolio_snapshots"
        params: list[Any] = []
        if portfolio is not None:
            query += " WHERE portfolio = ?"
            params.append(portfolio)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(query, params).fetchall()
        return [{"timestamp": timestamp, **self._deserialize_json(payload)} for timestamp, payload in rows]

    def list_events(self, limit: int | None = None, *, event_types: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Return persisted operational events in reverse chronological order.

        Args:
            event_types: When given, restricts results to these event types via
                a SQL WHERE clause, so a small set of noteworthy event types
                (e.g. manual/live order actions) can be found even when they are
                far outnumbered by high-frequency operational noise
                (health snapshots, cycle-completed events, etc) in between.
        """
        query = "SELECT timestamp, level, event_type, message, source, metadata FROM operational_events"
        params: list[Any] = []
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            query += f" WHERE event_type IN ({placeholders})"
            params.extend(event_types)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "timestamp": timestamp,
                "level": level,
                "event_type": event_type,
                "message": message,
                "source": source,
                "metadata": self._deserialize_json(metadata),
            }
            for timestamp, level, event_type, message, source, metadata in rows
        ]

    def log_tax_event(
        self,
        *,
        timestamp: datetime,
        transaction_type: str,
        symbol: str,
        amount_eur: float,
        norges_bank_fx_rate: float,
        amount_nok: float,
        cost_basis_nok: float,
        metadata: dict[str, Any] | None = None,
        entry_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        """Persist a single tax-ledger row."""
        created_connection = connection is None
        current_connection = connection or sqlite3.connect(self.database_path)
        try:
            ledger_id = entry_id or str(uuid4())
            current_connection.execute(
                """
                INSERT INTO tax_ledger (
                    id,
                    timestamp_utc,
                    transaction_type,
                    symbol,
                    amount_eur,
                    norges_bank_fx_rate,
                    amount_nok,
                    cost_basis_nok,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledger_id,
                    timestamp.astimezone(timezone.utc).isoformat() if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc).isoformat(),
                    transaction_type,
                    symbol,
                    float(amount_eur),
                    float(norges_bank_fx_rate),
                    float(amount_nok),
                    float(cost_basis_nok),
                    self._serialize_json(metadata or {}),
                ),
            )
            if created_connection:
                current_connection.commit()
            return ledger_id
        finally:
            if created_connection:
                current_connection.close()

    def list_tax_events(
        self,
        *,
        limit: int | None = None,
        tax_year: int | None = None,
        transaction_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted tax-ledger rows in reverse chronological order."""
        clauses: list[str] = []
        params: list[Any] = []
        if tax_year is not None:
            clauses.append("substr(timestamp_utc, 1, 4) = ?")
            params.append(f"{tax_year:04d}")
        if transaction_types:
            placeholders = ", ".join("?" for _ in transaction_types)
            clauses.append(f"transaction_type IN ({placeholders})")
            params.extend(list(transaction_types))
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(int(limit))

        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                f"""
                SELECT id, timestamp_utc, transaction_type, symbol, amount_eur, norges_bank_fx_rate, amount_nok, cost_basis_nok, metadata
                FROM tax_ledger
                {where_clause}
                ORDER BY timestamp_utc DESC, id DESC
                {limit_clause}
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "id": row_id,
                "timestamp_utc": timestamp_utc,
                "transaction_type": transaction_type,
                "symbol": symbol,
                "amount_eur": float(amount_eur),
                "norges_bank_fx_rate": float(norges_bank_fx_rate),
                "amount_nok": float(amount_nok),
                "cost_basis_nok": float(cost_basis_nok),
                "metadata": self._deserialize_json(metadata),
            }
            for row_id, timestamp_utc, transaction_type, symbol, amount_eur, norges_bank_fx_rate, amount_nok, cost_basis_nok, metadata in rows
        ]

    def log_fiat_conversion(
        self,
        *,
        timestamp: datetime,
        amount_eur: float,
        source: str,
        symbol: str = "EUR",
        fx_rate: float | None = None,
        reference: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Persist a fiat-pool adjustment and update the EUR cost basis."""
        if amount_eur == 0.0:
            raise ValueError("amount_eur must be non-zero")
        applied_fx_rate = float(fx_rate if fx_rate is not None else self.fx_rate_collector.get_rate(at=timestamp))
        with closing(sqlite3.connect(self.database_path)) as connection:
            entry_id = self._apply_fiat_conversion(
                connection,
                timestamp=timestamp,
                amount_eur=float(amount_eur),
                fx_rate=applied_fx_rate,
                symbol=symbol,
                source=source,
                reference=reference,
                metadata=metadata,
            )
            connection.commit()
        return entry_id

    def log_funding_fee(
        self,
        *,
        timestamp: datetime,
        source: str,
        symbol: str,
        amount_eur: float,
        fx_rate: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Persist a funding-fee tax entry and mirror the related EUR cash movement."""
        if amount_eur == 0.0:
            raise ValueError("amount_eur must be non-zero")
        applied_fx_rate = float(fx_rate if fx_rate is not None else self.fx_rate_collector.get_rate(at=timestamp))
        with closing(sqlite3.connect(self.database_path)) as connection:
            if amount_eur > 0.0:
                self._add_eur_fiat_lot(
                    connection,
                    acquired_at=timestamp,
                    amount_eur=amount_eur,
                    cost_basis_nok=amount_eur * applied_fx_rate,
                    source_ref=f"funding:{symbol}",
                    metadata={"source": source},
                )
            else:
                self._apply_fiat_conversion(
                    connection,
                    timestamp=timestamp,
                    amount_eur=amount_eur,
                    fx_rate=applied_fx_rate,
                    symbol="EUR",
                    source=source,
                    reference=f"funding:{symbol}",
                    metadata={"reason": "funding_fee_cash_flow"},
                )
            entry_id = self.log_tax_event(
                timestamp=timestamp,
                transaction_type="FUNDING_FEE",
                symbol=symbol,
                amount_eur=float(amount_eur),
                norges_bank_fx_rate=applied_fx_rate,
                amount_nok=float(amount_eur) * applied_fx_rate,
                cost_basis_nok=self._current_eur_pool_cost_basis_nok(connection),
                metadata=metadata or {"source": source},
                connection=connection,
            )
            connection.commit()
        return entry_id

    def log_derivative_event(
        self,
        *,
        timestamp: datetime,
        venue_symbol: str,
        transaction_type: str,
        amount: float,
        currency: str = "USD",
        source: str = "runtime",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record a perpetual-futures cash flow (realized PnL, fee or funding) in the tax ledger.

        Uses the same ledger and transaction types as spot, so the yearly
        summary includes it, but no spot FIFO lots are touched: the futures
        wallet is separate from the spot EUR pool. `amount` is signed in the
        settlement currency (gain / received positive, cost negative) and is
        valued in NOK at Norges Bank's rate for that day; `amount_eur` holds
        the EUR equivalent so existing reports keep working. This is
        record-keeping, not a tax ruling: check how Skatteetaten treats
        derivative gains before filing.
        """
        if transaction_type not in {"REALIZED_PNL", "TRADING_FEE", "FUNDING_FEE"}:
            raise ValueError(f"unsupported derivative transaction type: {transaction_type}")
        currency = currency.upper()
        eur_nok = float(self.fx_rate_collector.get_rate("EUR/NOK", at=timestamp))
        currency_nok = eur_nok if currency == "EUR" else float(self.fx_rate_collector.get_rate(f"{currency}/NOK", at=timestamp))
        amount_nok = float(amount) * currency_nok
        return self.log_tax_event(
            timestamp=timestamp,
            transaction_type=transaction_type,
            symbol=venue_symbol,
            amount_eur=amount_nok / eur_nok,
            norges_bank_fx_rate=eur_nok,
            amount_nok=amount_nok,
            cost_basis_nok=0.0,
            metadata={
                "instrument": "perpetual",
                "currency": currency,
                "amount": float(amount),
                "currency_nok_rate": currency_nok,
                "source": source,
                **(metadata or {}),
            },
        )

    def record_trade_tax_events(
        self,
        *,
        trade_id: int,
        timestamp: datetime,
        source: str,
        exchange: str,
        pair: str,
        side: str,
        price: float,
        size: float,
        fee: float,
        role_maker_taker: str,
    ) -> list[str]:
        """Persist tax-ledger entries for a EUR-quoted trade using FIFO basis tracking."""
        base_asset, quote_asset = self._split_pair(pair)
        if quote_asset != "EUR":
            return []

        applied_fx_rate = float(self.fx_rate_collector.get_rate(at=timestamp))
        created_entries: list[str] = []
        normalized_side = side.lower()
        with closing(sqlite3.connect(self.database_path)) as connection:
            if normalized_side == "buy":
                created_entries.append(
                    self._apply_fiat_conversion(
                        connection,
                        timestamp=timestamp,
                        amount_eur=-(float(price) * float(size)),
                        fx_rate=applied_fx_rate,
                        symbol="EUR",
                        source=source,
                        reference=f"trade:{trade_id}:buy_notional",
                        metadata={"pair": pair, "exchange": exchange, "trade_id": trade_id, "role": role_maker_taker},
                    )
                )
                self._add_asset_lot(
                    connection,
                    asset_symbol=base_asset,
                    acquired_at=timestamp,
                    remaining_size=float(size),
                    unit_cost_eur=float(price),
                    source_trade_id=trade_id,
                    metadata={"pair": pair, "exchange": exchange},
                )
            elif normalized_side == "sell":
                basis_summary = self._consume_asset_lots(connection, asset_symbol=base_asset, size=float(size))
                proceeds_eur = float(price) * float(size)
                created_entries.append(
                    self._apply_fiat_conversion(
                        connection,
                        timestamp=timestamp,
                        amount_eur=proceeds_eur,
                        fx_rate=applied_fx_rate,
                        symbol="EUR",
                        source=source,
                        reference=f"trade:{trade_id}:sell_proceeds",
                        metadata={"pair": pair, "exchange": exchange, "trade_id": trade_id, "role": role_maker_taker},
                    )
                )
                realized_pnl_eur = proceeds_eur - float(basis_summary["allocated_cost_basis_eur"])
                created_entries.append(
                    self.log_tax_event(
                        timestamp=timestamp,
                        transaction_type="REALIZED_PNL",
                        symbol=pair,
                        amount_eur=realized_pnl_eur,
                        norges_bank_fx_rate=applied_fx_rate,
                        amount_nok=realized_pnl_eur * applied_fx_rate,
                        cost_basis_nok=self._current_eur_pool_cost_basis_nok(connection),
                        metadata={
                            "pair": pair,
                            "exchange": exchange,
                            "trade_id": trade_id,
                            "role": role_maker_taker,
                            "matched_asset_lots": basis_summary["consumed_lots"],
                            "cost_basis_eur": basis_summary["allocated_cost_basis_eur"],
                            "gross_proceeds_eur": proceeds_eur,
                        },
                        connection=connection,
                    )
                )
            else:
                raise ValueError(f"unsupported trade side for tax logging: {side}")

            if float(fee) > 0.0:
                created_entries.append(
                    self._apply_fiat_conversion(
                        connection,
                        timestamp=timestamp,
                        amount_eur=-float(fee),
                        fx_rate=applied_fx_rate,
                        symbol="EUR",
                        source=source,
                        reference=f"trade:{trade_id}:fee",
                        metadata={"pair": pair, "exchange": exchange, "trade_id": trade_id, "reason": "trading_fee_cash_flow"},
                    )
                )
                created_entries.append(
                    self.log_tax_event(
                        timestamp=timestamp,
                        transaction_type="TRADING_FEE",
                        symbol=pair,
                        amount_eur=-float(fee),
                        norges_bank_fx_rate=applied_fx_rate,
                        amount_nok=-float(fee) * applied_fx_rate,
                        cost_basis_nok=self._current_eur_pool_cost_basis_nok(connection),
                        metadata={"pair": pair, "exchange": exchange, "trade_id": trade_id, "role": role_maker_taker},
                        connection=connection,
                    )
                )

            connection.commit()
        return created_entries

    def write_year_end_holdings(
        self,
        *,
        timestamp: datetime,
        holdings: dict[str, float],
        prices_eur: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Persist a year-end holdings valuation for Norwegian wealth-tax reporting."""
        valuation_time = timestamp.astimezone(timezone.utc) if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
        applied_fx_rate = float(self.fx_rate_collector.get_rate(at=valuation_time))
        normalized_holdings: dict[str, dict[str, float]] = {}
        total_value_eur = 0.0
        price_lookup = {str(key).upper(): float(value) for key, value in (prices_eur or {}).items()}

        for raw_symbol, raw_amount in holdings.items():
            symbol = str(raw_symbol).upper()
            amount = float(raw_amount)
            if symbol == "EUR":
                price_eur = 1.0
            else:
                price_eur = price_lookup.get(symbol)
                if price_eur is None:
                    price_eur = price_lookup.get(f"{symbol}/EUR")
                if price_eur is None:
                    raise ValueError(f"missing EUR price for holding: {symbol}")
            value_eur = amount * price_eur
            total_value_eur += value_eur
            normalized_holdings[symbol] = {
                "amount": amount,
                "price_eur": price_eur,
                "value_eur": value_eur,
            }

        total_value_nok = total_value_eur * applied_fx_rate
        valuation_year = int(valuation_time.astimezone(timezone.utc).year)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO year_end_holdings (
                    valuation_year,
                    timestamp_utc,
                    holdings_json,
                    total_value_eur,
                    norges_bank_fx_rate,
                    total_value_nok
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(valuation_year) DO UPDATE SET
                    timestamp_utc = excluded.timestamp_utc,
                    holdings_json = excluded.holdings_json,
                    total_value_eur = excluded.total_value_eur,
                    norges_bank_fx_rate = excluded.norges_bank_fx_rate,
                    total_value_nok = excluded.total_value_nok
                """,
                (
                    valuation_year,
                    valuation_time.isoformat(),
                    self._serialize_json(normalized_holdings),
                    total_value_eur,
                    applied_fx_rate,
                    total_value_nok,
                ),
            )
            connection.commit()
        return {
            "valuation_year": valuation_year,
            "timestamp_utc": valuation_time.isoformat(),
            "holdings": normalized_holdings,
            "total_value_eur": total_value_eur,
            "norges_bank_fx_rate": applied_fx_rate,
            "total_value_nok": total_value_nok,
        }

    def get_tax_year_summary(self, tax_year: int) -> dict[str, Any]:
        """Return a Norwegian-tax summary for the requested year."""
        events = self.list_tax_events(tax_year=tax_year)
        gains_nok = sum(event["amount_nok"] for event in events if event["transaction_type"] == "REALIZED_PNL" and event["amount_nok"] > 0.0)
        realized_losses_nok = sum(-event["amount_nok"] for event in events if event["transaction_type"] == "REALIZED_PNL" and event["amount_nok"] < 0.0)
        trading_fees_nok = sum(-event["amount_nok"] for event in events if event["transaction_type"] == "TRADING_FEE" and event["amount_nok"] < 0.0)
        funding_fees_nok = sum(-event["amount_nok"] for event in events if event["transaction_type"] == "FUNDING_FEE" and event["amount_nok"] < 0.0)
        fx_gain_loss_nok = sum(float((event.get("metadata") or {}).get("realized_gain_nok", 0.0)) for event in events if event["transaction_type"] == "FIAT_CONVERSION")

        wealth_snapshot = self._load_year_end_holdings(tax_year)
        return {
            "tax_year": int(tax_year),
            "total_gross_taxable_gains_nok": float(gains_nok),
            "total_gross_deductible_losses_nok": float(realized_losses_nok + trading_fees_nok + funding_fees_nok),
            "net_foreign_currency_gain_loss_nok": float(fx_gain_loss_nok),
            "total_trading_fees_nok": float(trading_fees_nok),
            "total_funding_fees_nok": float(funding_fees_nok),
            "tax_event_count": len(events),
            "wealth_tax_snapshot": wealth_snapshot,
            "derivatives": self._derivative_totals(events),
        }

    def _derivative_totals(self, events: list[dict[str, Any]]) -> dict[str, float]:
        """Perpetual-futures part of the year, in NOK (already included in the totals above)."""
        derivative_events = [event for event in events if (event.get("metadata") or {}).get("instrument") == "perpetual"]

        def total(kind: str, sign: float) -> float:
            return float(sum(sign * event["amount_nok"] for event in derivative_events if event["transaction_type"] == kind and sign * event["amount_nok"] > 0.0))

        return {
            "event_count": float(len(derivative_events)),
            "realized_gains_nok": total("REALIZED_PNL", 1.0),
            "realized_losses_nok": total("REALIZED_PNL", -1.0),
            "fees_nok": total("TRADING_FEE", -1.0),
            "funding_paid_nok": total("FUNDING_FEE", -1.0),
            "funding_received_nok": total("FUNDING_FEE", 1.0),
        }

    def export_tax_ledger(self, *, path: str | Path, tax_year: int | None = None, export_format: str | None = None) -> Path:
        """Export tax-ledger rows to CSV or JSON."""
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        format_name = (export_format or target_path.suffix.lstrip(".") or "csv").lower()
        events = self.list_tax_events(tax_year=tax_year)
        if format_name == "json":
            target_path.write_text(self._serialize_json(events), encoding="utf-8")
            return target_path
        if format_name != "csv":
            raise ValueError(f"unsupported tax export format: {format_name}")

        with target_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = [
                "id",
                "timestamp_utc",
                "transaction_type",
                "symbol",
                "amount_eur",
                "norges_bank_fx_rate",
                "amount_nok",
                "cost_basis_nok",
                "metadata",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for event in events:
                writer.writerow(
                    {
                        **event,
                        "metadata": self._serialize_json(event.get("metadata") or {}),
                    }
                )
        return target_path

    def _serialize_json(self, payload: Any) -> str:
        return json.dumps(payload, default=self._json_default, sort_keys=True)

    def _deserialize_json(self, payload: str | None) -> Any:
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        return str(value)

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

    def _apply_fiat_conversion(
        self,
        connection: sqlite3.Connection,
        *,
        timestamp: datetime,
        amount_eur: float,
        fx_rate: float,
        symbol: str,
        source: str,
        reference: str | None,
        metadata: dict[str, Any] | None,
    ) -> str:
        entry_metadata = dict(metadata or {})
        entry_metadata["source"] = source
        if reference is not None:
            entry_metadata["reference"] = reference

        if amount_eur > 0.0:
            self._add_eur_fiat_lot(
                connection,
                acquired_at=timestamp,
                amount_eur=amount_eur,
                cost_basis_nok=amount_eur * fx_rate,
                source_ref=reference,
                metadata=entry_metadata,
            )
            entry_metadata["realized_gain_nok"] = 0.0
            entry_metadata["allocated_cost_basis_nok"] = amount_eur * fx_rate
        else:
            consumption = self._consume_eur_fiat_lots(connection, amount_eur=abs(amount_eur))
            entry_metadata["consumed_lots"] = consumption["consumed_lots"]
            entry_metadata["allocated_cost_basis_nok"] = consumption["allocated_cost_basis_nok"]
            entry_metadata["realized_gain_nok"] = (abs(amount_eur) * fx_rate) - consumption["allocated_cost_basis_nok"]

        return self.log_tax_event(
            timestamp=timestamp,
            transaction_type="FIAT_CONVERSION",
            symbol=symbol,
            amount_eur=amount_eur,
            norges_bank_fx_rate=fx_rate,
            amount_nok=amount_eur * fx_rate,
            cost_basis_nok=self._current_eur_pool_cost_basis_nok(connection),
            metadata=entry_metadata,
            connection=connection,
        )

    def _add_eur_fiat_lot(
        self,
        connection: sqlite3.Connection,
        *,
        acquired_at: datetime,
        amount_eur: float,
        cost_basis_nok: float,
        source_ref: str | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO tax_eur_fiat_lots (
                acquired_at,
                remaining_amount_eur,
                cost_basis_nok,
                source_ref,
                metadata
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                self._normalize_timestamp(acquired_at),
                float(amount_eur),
                float(cost_basis_nok),
                source_ref,
                self._serialize_json(metadata or {}),
            ),
        )

    def _consume_eur_fiat_lots(self, connection: sqlite3.Connection, *, amount_eur: float) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT id, acquired_at, remaining_amount_eur, cost_basis_nok
            FROM tax_eur_fiat_lots
            WHERE remaining_amount_eur > 0
            ORDER BY acquired_at ASC, id ASC
            """
        ).fetchall()
        remaining_to_consume = float(amount_eur)
        consumed_lots: list[dict[str, Any]] = []
        allocated_cost_basis_nok = 0.0

        for row_id, acquired_at, remaining_amount_eur, lot_cost_basis_nok in rows:
            if remaining_to_consume <= 1e-12:
                break
            available_amount = float(remaining_amount_eur)
            if available_amount <= 0.0:
                continue
            take_amount = min(available_amount, remaining_to_consume)
            lot_basis = float(lot_cost_basis_nok) * (take_amount / available_amount)
            new_remaining_amount = available_amount - take_amount
            new_cost_basis = max(0.0, float(lot_cost_basis_nok) - lot_basis)
            connection.execute(
                "UPDATE tax_eur_fiat_lots SET remaining_amount_eur = ?, cost_basis_nok = ? WHERE id = ?",
                (new_remaining_amount, new_cost_basis, row_id),
            )
            remaining_to_consume -= take_amount
            allocated_cost_basis_nok += lot_basis
            consumed_lots.append(
                {
                    "lot_id": row_id,
                    "acquired_at": acquired_at,
                    "amount_eur": take_amount,
                    "cost_basis_nok": lot_basis,
                }
            )

        if remaining_to_consume > 1e-9:
            raise ValueError("EUR fiat pool is insufficient for this taxable conversion")

        return {
            "consumed_lots": consumed_lots,
            "allocated_cost_basis_nok": allocated_cost_basis_nok,
        }

    def _current_eur_pool_cost_basis_nok(self, connection: sqlite3.Connection) -> float:
        row = connection.execute(
            "SELECT COALESCE(SUM(cost_basis_nok), 0.0) FROM tax_eur_fiat_lots WHERE remaining_amount_eur > 0"
        ).fetchone()
        return 0.0 if row is None else float(row[0] or 0.0)

    def _add_asset_lot(
        self,
        connection: sqlite3.Connection,
        *,
        asset_symbol: str,
        acquired_at: datetime,
        remaining_size: float,
        unit_cost_eur: float,
        source_trade_id: int | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO tax_asset_lots (
                asset_symbol,
                acquired_at,
                remaining_size,
                unit_cost_eur,
                source_trade_id,
                metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                asset_symbol,
                self._normalize_timestamp(acquired_at),
                float(remaining_size),
                float(unit_cost_eur),
                source_trade_id,
                self._serialize_json(metadata or {}),
            ),
        )

    def _consume_asset_lots(self, connection: sqlite3.Connection, *, asset_symbol: str, size: float) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT id, acquired_at, remaining_size, unit_cost_eur, source_trade_id
            FROM tax_asset_lots
            WHERE asset_symbol = ? AND remaining_size > 0
            ORDER BY acquired_at ASC, id ASC
            """,
            (asset_symbol,),
        ).fetchall()
        remaining_to_consume = float(size)
        consumed_lots: list[dict[str, Any]] = []
        allocated_cost_basis_eur = 0.0

        for row_id, acquired_at, remaining_size, unit_cost_eur, source_trade_id in rows:
            if remaining_to_consume <= 1e-12:
                break
            available_size = float(remaining_size)
            if available_size <= 0.0:
                continue
            take_size = min(available_size, remaining_to_consume)
            connection.execute(
                "UPDATE tax_asset_lots SET remaining_size = ? WHERE id = ?",
                (available_size - take_size, row_id),
            )
            remaining_to_consume -= take_size
            allocated_cost_basis_eur += take_size * float(unit_cost_eur)
            consumed_lots.append(
                {
                    "lot_id": row_id,
                    "acquired_at": acquired_at,
                    "size": take_size,
                    "unit_cost_eur": float(unit_cost_eur),
                    "source_trade_id": source_trade_id,
                }
            )

        if remaining_to_consume > 1e-9:
            raise ValueError(f"asset lot inventory is insufficient for {asset_symbol}")

        return {
            "consumed_lots": consumed_lots,
            "allocated_cost_basis_eur": allocated_cost_basis_eur,
        }

    def _load_year_end_holdings(self, tax_year: int) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT valuation_year, timestamp_utc, holdings_json, total_value_eur, norges_bank_fx_rate, total_value_nok
                FROM year_end_holdings
                WHERE valuation_year = ?
                """,
                (int(tax_year),),
            ).fetchone()
        if row is None:
            return None
        valuation_year, timestamp_utc, holdings_json, total_value_eur, norges_bank_fx_rate, total_value_nok = row
        return {
            "valuation_year": int(valuation_year),
            "timestamp_utc": timestamp_utc,
            "holdings": self._deserialize_json(holdings_json),
            "total_value_eur": float(total_value_eur),
            "norges_bank_fx_rate": float(norges_bank_fx_rate),
            "total_value_nok": float(total_value_nok),
        }

    @staticmethod
    def _split_pair(pair: str) -> tuple[str, str]:
        normalized = pair.strip().upper()
        for separator in ("/", "-", "_", ":"):
            if separator in normalized:
                base_asset, quote_asset = normalized.split(separator, 1)
                return base_asset.strip(), quote_asset.strip()
        raise ValueError(f"unsupported trading pair format: {pair}")

    @staticmethod
    def _normalize_timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()

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
