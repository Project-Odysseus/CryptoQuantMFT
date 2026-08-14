"""Session-level reconciliation and account-state tracking helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class ReconciliationEntry:
    """Summary of how a local order compared to the adapter-reported remote state."""

    order_id: str
    exchange: str
    status: str
    matched: bool
    local_status: str | None = None
    remote_status: str | None = None
    local_filled_size: float | None = None
    remote_filled_size: float | None = None
    local_fill_price: float | None = None
    remote_fill_price: float | None = None
    local_fee: float | None = None
    remote_fee: float | None = None
    message: str | None = None


class SessionAccountStateTracker:
    """Track balances, positions, and reconciliation state for the current session."""

    def __init__(self, *, exchange_name: str | None = None, base_currency: str | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.exchange_name = exchange_name or "paper"
        self.base_currency = base_currency or self._default_currency(exchange_name)
        self.balances: dict[str, float] = {self.base_currency: 0.0}
        self.positions: dict[str, float] = {}
        self.unsettled_orders: dict[str, dict[str, Any]] = {}
        self.reconciliation_results: list[ReconciliationEntry] = []
        self.last_reconciled_at: datetime | None = None
        self.account_reconciliation_summary: dict[str, Any] | None = None
        self.recovery_summary: dict[str, Any] | None = None

    def update_from_runtime(
        self,
        *,
        execution_result: Any | None = None,
        adapter: Any | None = None,
        exchange_name: str | None = None,
    ) -> dict[str, Any]:
        """Refresh balances, positions, and reconciliation state from runtime state."""
        if exchange_name is not None:
            self.exchange_name = exchange_name
            self.base_currency = self.base_currency or self._default_currency(exchange_name)
            self.balances.setdefault(self.base_currency, 0.0)

        if execution_result is not None:
            portfolio_history = getattr(execution_result, "portfolio_history", None) or []
            if portfolio_history:
                latest_snapshot = portfolio_history[-1]
                self.balances[self.base_currency] = float(getattr(latest_snapshot, "cash", 0.0))
                position_size = float(getattr(latest_snapshot, "position_size", 0.0))
                if position_size != 0.0:
                    self.positions["BTC"] = position_size
                else:
                    self.positions.pop("BTC", None)

        if adapter is not None:
            remote_snapshot = None
            if execution_result is not None:
                portfolio_history = getattr(execution_result, "portfolio_history", None) or []
                if portfolio_history:
                    latest_snapshot = portfolio_history[-1]
                    remote_snapshot = {
                        "balances": {self.base_currency: float(getattr(latest_snapshot, "cash", 0.0))},
                        "positions": {"BTC": float(getattr(latest_snapshot, "position_size", 0.0))},
                    }
            self.recover_execution_state(adapter, remote_snapshot=remote_snapshot)
            self._merge_account_snapshot(adapter.get_account_snapshot())
            self.reconcile_orders(adapter)

        return self.get_summary()

    def reconcile_account_state(self, adapter: Any, *, remote_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """Compare the adapter's local balances/positions to an external snapshot and sync them."""
        if adapter is None:
            self.account_reconciliation_summary = {"matched": True, "balance_mismatches": {}, "position_mismatches": {}}
            return self.account_reconciliation_summary

        balances = None
        positions = None
        if remote_snapshot is not None:
            balances = remote_snapshot.get("balances")
            positions = remote_snapshot.get("positions")

        summary = getattr(adapter, "reconcile_account_state", None)
        if callable(summary):
            self.account_reconciliation_summary = summary(balances=balances, positions=positions)
        else:
            self.account_reconciliation_summary = {"matched": True, "balance_mismatches": {}, "position_mismatches": {}}

        return self.account_reconciliation_summary or {}

    def recover_execution_state(self, adapter: Any, *, remote_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """Recover adapter state from the latest remote snapshot and order updates after reconnects."""
        if adapter is None:
            self.recovery_summary = {"recovered_order_count": 0, "recovered_order_ids": [], "account_reconciliation": {"matched": True, "balance_mismatches": {}, "position_mismatches": {}}}
            return self.recovery_summary

        recovery_summary = getattr(adapter, "recover_execution_state", None)
        if callable(recovery_summary):
            self.recovery_summary = recovery_summary(remote_snapshot=remote_snapshot)
            self.account_reconciliation_summary = (self.recovery_summary or {}).get("account_reconciliation")
        else:
            self.reconcile_account_state(adapter, remote_snapshot=remote_snapshot)
            self.reconcile_orders(adapter)
            self.recovery_summary = {
                "recovered_order_count": len(self.reconciliation_results),
                "recovered_order_ids": [entry.order_id for entry in self.reconciliation_results],
                "account_reconciliation": self.account_reconciliation_summary,
            }

        return self.recovery_summary or {}

    def reconcile_orders(self, adapter: Any) -> list[ReconciliationEntry]:
        """Compare local order state against the adapter's current remote view."""
        self.reconciliation_results = []
        self.unsettled_orders = {}
        if adapter is None:
            return self.reconciliation_results

        for order in getattr(adapter, "list_orders", lambda: [])():
            order_id = getattr(order, "order_id", None)
            if not order_id:
                continue
            local_status = getattr(order, "status", None)
            local_filled_size = getattr(order, "filled_size", None)
            local_fill_price = getattr(order, "fill_price", None)
            local_fee = getattr(order, "fee", None)

            remote_report = adapter.get_order_status(order_id=order_id)
            remote_status = getattr(remote_report, "status", None)
            remote_filled_size = getattr(remote_report, "filled_size", None)
            remote_fill_price = getattr(remote_report, "fill_price", None)
            remote_fee = getattr(remote_report, "fee", None)

            matches = (
                remote_status == local_status
                and remote_filled_size == local_filled_size
                and remote_fill_price == local_fill_price
                and remote_fee == local_fee
            )
            if not matches:
                adapter.reconcile_order_state(
                    order_id=order_id,
                    remote_status=remote_status,
                    remote_filled_size=remote_filled_size,
                    remote_fill_price=remote_fill_price,
                    remote_fee=remote_fee,
                )

            self.unsettled_orders[order_id] = {
                "order_id": order_id,
                "exchange": getattr(order, "exchange", self.exchange_name),
                "status": getattr(order, "status", remote_status),
                "filled_size": getattr(order, "filled_size", remote_filled_size),
                "fill_price": getattr(order, "fill_price", remote_fill_price),
                "fee": getattr(order, "fee", remote_fee),
            }
            if getattr(order, "status", None) not in {"FILLED", "CANCELED", "REJECTED", "NOT_FOUND"}:
                self.unsettled_orders[order_id]["pending"] = True
            self.reconciliation_results.append(
                ReconciliationEntry(
                    order_id=order_id,
                    exchange=getattr(order, "exchange", self.exchange_name),
                    status=getattr(order, "status", remote_status) or "UNKNOWN",
                    matched=matches,
                    local_status=local_status,
                    remote_status=remote_status,
                    local_filled_size=local_filled_size,
                    remote_filled_size=remote_filled_size,
                    local_fill_price=local_fill_price,
                    remote_fill_price=remote_fill_price,
                    local_fee=local_fee,
                    remote_fee=remote_fee,
                    message="order state matched" if matches else "order state reconciled",
                )
            )

        self.last_reconciled_at = datetime.now(timezone.utc)
        return self.reconciliation_results

    def _merge_account_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        if not snapshot:
            return
        balances = snapshot.get("balances", {}) or {}
        for currency, amount in balances.items():
            self.balances[str(currency)] = float(amount)
        positions = snapshot.get("positions", {}) or {}
        for symbol, size in positions.items():
            self.positions[str(symbol)] = float(size)

    def get_summary(self) -> dict[str, Any]:
        """Return a compact summary suitable for runtime diagnostics and dashboards."""
        return {
            "exchange": self.exchange_name,
            "base_currency": self.base_currency,
            "balances": dict(self.balances),
            "positions": dict(self.positions),
            "inventory": {
                "symbols": list(self.positions.keys()),
                "position_count": len(self.positions),
            },
            "unsettled_order_count": len(self.unsettled_orders),
            "reconciled_order_count": sum(1 for entry in self.reconciliation_results if entry.matched),
            "reconciliation_mismatches": [entry.order_id for entry in self.reconciliation_results if not entry.matched],
            "last_reconciled_at": self.last_reconciled_at.isoformat() if self.last_reconciled_at else None,
            "account_reconciliation": self.account_reconciliation_summary,
            "recovery_summary": self.recovery_summary,
            "reconciliation_status": "matched" if not self.reconciliation_results or all(entry.matched for entry in self.reconciliation_results) else "mismatched",
        }

    def _default_currency(self, exchange_name: str | None) -> str:
        if exchange_name == "kraken":
            return "EUR"
        if exchange_name == "firi":
            return "NOK"
        return "USD"
