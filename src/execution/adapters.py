"""Execution adapters and sandbox routing helpers for safe order placement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import settings


@dataclass(slots=True)
class ExecutionReport:
    """Outcome of an order submission or cancellation request."""

    order_id: str
    status: str
    fill_price: float | None = None
    filled_size: float | None = None
    fee: float = 0.0
    message: str | None = None


@dataclass(slots=True)
class ExecutionOrder:
    """Normalized representation of a routed order."""

    order_id: str
    side: str
    size: float
    symbol: str | None = None
    price: float | None = None
    timestamp: datetime | None = None
    status: str = "SUBMITTED"
    fill_price: float | None = None
    filled_size: float | None = None
    fee: float = 0.0
    exchange: str | None = None
    remote_status: str | None = None
    remote_order_id: str | None = None
    reconciled: bool = False
    message: str | None = None


class ExecutionAdapter:
    """Base interface for exchange execution adapters."""

    name: str = "base"

    def __init__(self) -> None:
        """Initialize the object with its runtime state."""
        self._orders: dict[str, ExecutionOrder] = {}
        self._balances: dict[str, float] = {}
        self._positions: dict[str, float] = {}
        self._base_currency = "USD"
        self._remote_balances: dict[str, float] = {}
        self._remote_positions: dict[str, float] = {}
        self._account_reconciliation: dict[str, Any] = {}

    def _coerce_float(self, value: Any) -> float | None:
        """Convert a value to a float when possible."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Submit an order through the adapter and capture the execution result."""
        raise NotImplementedError

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel an existing order and return the execution outcome."""
        raise NotImplementedError

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the latest status for the requested order."""
        raise NotImplementedError

    def reconcile_order_state(
        self,
        *,
        order_id: str,
        remote_status: str | None = None,
        remote_filled_size: float | None = None,
        remote_fill_price: float | None = None,
        remote_fee: float | None = None,
    ) -> ExecutionReport:
        """Reconcile the local order state against the remote execution state."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")

        previous_fill_size = order.filled_size or 0.0
        previous_fee = order.fee or 0.0
        previous_status = order.status

        if remote_status is not None:
            order.status = remote_status
            order.remote_status = remote_status
        if remote_filled_size is not None:
            order.filled_size = remote_filled_size
        if remote_fill_price is not None:
            order.fill_price = remote_fill_price
        if remote_fee is not None:
            order.fee = remote_fee

        if remote_status in {"FILLED", "PARTIALLY_FILLED"} and previous_status not in {"FILLED", "PARTIALLY_FILLED"}:
            self._apply_fill_to_account_state(
                order=order,
                filled_size=remote_filled_size if remote_filled_size is not None else order.size,
                fill_price=remote_fill_price if remote_fill_price is not None else order.price,
                fee=remote_fee if remote_fee is not None else 0.0,
                previous_fill_size=previous_fill_size,
                previous_fee=previous_fee,
            )
        elif remote_status in {"FILLED", "PARTIALLY_FILLED"}:
            filled_delta = (remote_filled_size or 0.0) - previous_fill_size
            fee_delta = (remote_fee or 0.0) - previous_fee
            if filled_delta > 0.0 or fee_delta > 0.0:
                self._apply_fill_to_account_state(
                    order=order,
                    filled_size=filled_delta,
                    fill_price=remote_fill_price if remote_fill_price is not None else order.price,
                    fee=fee_delta,
                    previous_fill_size=0.0,
                    previous_fee=0.0,
                )

        order.reconciled = True
        if order.message is None:
            order.message = "order state reconciled"
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message="order state reconciled",
        )

    def get_account_snapshot(self) -> dict[str, Any]:
        """Return the current account balance and position snapshot."""
        return {
            "balances": dict(self._balances),
            "positions": dict(self._positions),
            "remote_balances": dict(self._remote_balances),
            "remote_positions": dict(self._remote_positions),
            "account_reconciliation": dict(self._account_reconciliation),
        }

    def reconcile_account_state(
        self,
        *,
        balances: dict[str, Any] | None = None,
        positions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile the local account state against the provided remote snapshot."""
        remote_balances = self._normalize_account_values(balances) if balances is not None else dict(self._remote_balances)
        remote_positions = self._normalize_account_values(positions) if positions is not None else dict(self._remote_positions)

        if balances is not None:
            self._remote_balances = remote_balances
        if positions is not None:
            self._remote_positions = remote_positions

        local_balances = dict(self._balances)
        local_positions = dict(self._positions)
        if not remote_balances and not remote_positions:
            remote_balances = dict(local_balances)
            remote_positions = dict(local_positions)

        merged_balances = dict(local_balances)
        merged_balances.update(remote_balances)
        merged_positions = dict(local_positions)
        merged_positions.update(remote_positions)

        balance_mismatches = {
            currency: {"local": local_balances.get(currency), "remote": remote_balances.get(currency)}
            for currency in sorted(set(local_balances) | set(remote_balances))
            if local_balances.get(currency) != remote_balances.get(currency)
        }
        position_mismatches = {
            symbol: {"local": local_positions.get(symbol), "remote": remote_positions.get(symbol)}
            for symbol in sorted(set(local_positions) | set(remote_positions))
            if local_positions.get(symbol) != remote_positions.get(symbol)
        }

        self._balances = merged_balances
        self._positions = merged_positions
        self._account_reconciliation = {
            "matched": not balance_mismatches and not position_mismatches,
            "balance_mismatches": balance_mismatches,
            "position_mismatches": position_mismatches,
            "remote_balances": dict(remote_balances),
            "remote_positions": dict(remote_positions),
            "merged_balances": dict(merged_balances),
            "merged_positions": dict(merged_positions),
        }
        return dict(self._account_reconciliation)

    def recover_execution_state(
        self,
        *,
        remote_snapshot: dict[str, Any] | None = None,
        remote_orders: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Reconcile the adapter's account and orders against a recent remote snapshot after reconnects."""
        recovered_order_ids: list[str] = []
        if remote_orders:
            for payload in remote_orders:
                if not isinstance(payload, dict):
                    continue
                order_id = str(payload.get("order_id", "")).strip() or None
                if not order_id:
                    continue
                order = self._orders.get(order_id)
                if order is None:
                    order = ExecutionOrder(
                        order_id=order_id,
                        side=str(payload.get("side", "unknown") or "unknown"),
                        size=self._coerce_float(payload.get("size")) or 0.0,
                        symbol=str(payload.get("symbol")) if payload.get("symbol") is not None else None,
                        price=self._coerce_float(payload.get("price")),
                        timestamp=datetime.now(),
                        status=str(payload.get("status", "SUBMITTED") or "SUBMITTED").upper(),
                        fill_price=self._coerce_float(payload.get("fill_price")),
                        filled_size=self._coerce_float(payload.get("filled_size")),
                        fee=self._coerce_float(payload.get("fee")) or 0.0,
                        exchange=self.name,
                    )
                    self._orders[order_id] = order

                self.reconcile_order_state(
                    order_id=order_id,
                    remote_status=str(payload.get("status", "")).upper() or None,
                    remote_filled_size=self._coerce_float(payload.get("filled_size")),
                    remote_fill_price=self._coerce_float(payload.get("fill_price")),
                    remote_fee=self._coerce_float(payload.get("fee")),
                )
                recovered_order_ids.append(order_id)
        else:
            for order in list(self._orders.values()):
                remote_report = self.get_order_status(order_id=order.order_id)
                if getattr(remote_report, "status", None) in {"NOT_FOUND", None}:
                    continue
                self.reconcile_order_state(
                    order_id=order.order_id,
                    remote_status=getattr(remote_report, "status", None),
                    remote_filled_size=getattr(remote_report, "filled_size", None),
                    remote_fill_price=getattr(remote_report, "fill_price", None),
                    remote_fee=getattr(remote_report, "fee", None),
                )
                recovered_order_ids.append(order.order_id)

        account_summary = self.reconcile_account_state(
            balances=remote_snapshot.get("balances") if remote_snapshot is not None else None,
            positions=remote_snapshot.get("positions") if remote_snapshot is not None else None,
        )

        return {
            "account_reconciliation": account_summary,
            "recovered_order_ids": recovered_order_ids,
            "recovered_order_count": len(recovered_order_ids),
            "recovery_status": "reconciled" if recovered_order_ids else "idle",
        }

    def list_orders(self) -> list[ExecutionOrder]:
        """Return the tracked orders for this adapter."""
        return list(self._orders.values())

    def _normalize_account_values(self, values: dict[str, Any] | None) -> dict[str, float]:
        if not values:
            return {}
        normalized: dict[str, float] = {}
        for key, value in values.items():
            try:
                normalized[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return normalized

    def _apply_fill_to_account_state(
        self,
        *,
        order: ExecutionOrder,
        filled_size: float | None,
        fill_price: float | None,
        fee: float | None,
        previous_fill_size: float,
        previous_fee: float,
    ) -> None:
        if filled_size is None or filled_size <= 0.0:
            return

        base_currency = self._base_currency
        position_symbol = self._position_symbol(order.symbol)
        self._balances.setdefault(base_currency, 0.0)
        if order.side == "buy":
            fill_delta = filled_size - previous_fill_size
            if fill_delta <= 0.0:
                return
            price = float(fill_price or order.price or 0.0)
            fee_delta = max(0.0, (fee or 0.0) - previous_fee)
            self._balances[base_currency] = self._balances.get(base_currency, 0.0) - (fill_delta * price) - fee_delta
            self._positions[position_symbol] = self._positions.get(position_symbol, 0.0) + fill_delta
        elif order.side == "sell":
            fill_delta = filled_size - previous_fill_size
            if fill_delta <= 0.0:
                return
            price = float(fill_price or order.price or 0.0)
            fee_delta = max(0.0, (fee or 0.0) - previous_fee)
            self._balances[base_currency] = self._balances.get(base_currency, 0.0) + (fill_delta * price) - fee_delta
            self._positions[position_symbol] = max(0.0, self._positions.get(position_symbol, 0.0) - fill_delta)
            if self._positions[position_symbol] == 0.0:
                self._positions.pop(position_symbol, None)

    def _position_symbol(self, symbol: str | None) -> str:
        if not symbol:
            return "BTC"
        normalized = str(symbol).strip().upper()
        for separator in ("/", "-", "_", ":"):
            if separator in normalized:
                base_asset = normalized.split(separator, 1)[0].strip()
                return base_asset or normalized
        return normalized


class SandboxExecutionAdapter(ExecutionAdapter):
    """A safe in-process adapter that simulates order acceptance and fills."""

    name = "sandbox"

    def __init__(self, *, exchange_name: str = "sandbox", fee_rate: float = 0.001) -> None:
        """Initialize the object with its runtime state."""
        super().__init__()
        self.exchange_name = exchange_name
        self.fee_rate = fee_rate
        self._base_currency = "EUR" if exchange_name == "kraken" else "NOK" if exchange_name == "firi" else "USD"
        self._balances = {self._base_currency: 1000.0}
        self._positions = {}
        self._remote_balances = dict(self._balances)
        self._remote_positions = dict(self._positions)

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Submit an order through the adapter and capture the execution result."""
        if size <= 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="size must be positive")

        execution_price = price
        filled_size = size
        fee = max(0.0, size * execution_price * self.fee_rate)
        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=size,
            symbol=symbol,
            price=price,
            timestamp=timestamp,
            status="FILLED",
            fill_price=execution_price,
            filled_size=filled_size,
            fee=fee,
            exchange=self.exchange_name,
        )
        self._orders[order_id] = order
        order.message = f"submitted to {self.exchange_name}"
        self._apply_fill_to_account_state(
            order=order,
            filled_size=filled_size,
            fill_price=execution_price,
            fee=fee,
            previous_fill_size=0.0,
            previous_fee=0.0,
        )
        return ExecutionReport(
            order_id=order_id,
            status="FILLED",
            fill_price=execution_price,
            filled_size=filled_size,
            fee=fee,
            message=f"submitted to {self.exchange_name}",
        )

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel an existing order and return the execution outcome."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if order.status == "FILLED":
            return ExecutionReport(order_id=order_id, status="FILLED", message="order already filled")
        order.status = "CANCELED"
        order.message = "order canceled"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the latest status for the requested order."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message=order.message or "sandbox order state",
        )


class ExchangeExecutionAdapter(ExecutionAdapter):
    """Base class for exchange-specific execution adapters with local reconciliation state."""

    def __init__(self, *, exchange_name: str, fee_rate: float = 0.001, api_key: str | None = None, api_secret: str | None = None) -> None:
        """Initialize the object with its runtime state."""
        super().__init__()
        self.exchange_name = exchange_name
        self.fee_rate = fee_rate
        self.api_key = api_key
        self.api_secret = api_secret

    def _coerce_float(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
    ) -> dict[str, Any] | list[Any]:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        body = None
        if data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data).encode("utf-8")
            else:
                body = json.dumps(data).encode("utf-8")

        request_headers = {"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"}
        if headers:
            request_headers.update(headers)

        request = urllib.request.Request(url, method=method, headers=request_headers, data=body)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload)
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Submit an order through the adapter and capture the execution result."""
        if size <= 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="size must be positive")

        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=size,
            symbol=symbol,
            price=price,
            timestamp=timestamp,
            status="SUBMITTED",
            exchange=self.exchange_name,
        )
        self._orders[order_id] = order
        order.message = f"staged locally for {self.exchange_name}"
        return ExecutionReport(
            order_id=order_id,
            status="SUBMITTED",
            message=f"staged locally for {self.exchange_name}",
        )

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel an existing order and return the execution outcome."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if order.status in {"FILLED", "CANCELED"}:
            return ExecutionReport(order_id=order_id, status=order.status, message="order already settled")
        order.status = "CANCELED"
        order.message = "order canceled"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the latest status for the requested order."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message=order.message or f"{self.exchange_name} order state",
        )


class KrakenExecutionAdapter(ExchangeExecutionAdapter):
    """Adapter for Kraken order routing with authenticated API calls and reconciliation."""

    name = "kraken"

    def __init__(self, *, fee_rate: float = 0.001, api_key: str | None = None, api_secret: str | None = None) -> None:
        """Initialize the object with its runtime state."""
        super().__init__(
            exchange_name="kraken",
            fee_rate=fee_rate,
            api_key=api_key if api_key is not None else settings.kraken_api_key,
            api_secret=api_secret if api_secret is not None else settings.kraken_secret,
        )
        self._base_currency = "EUR"
        self._balances.setdefault(self._base_currency, 0.0)

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Submit an order through the adapter and capture the execution result."""
        if size <= 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="size must be positive")

        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=size,
            symbol=symbol,
            price=price,
            timestamp=timestamp,
            status="SUBMITTED",
            exchange=self.exchange_name,
        )
        self._orders[order_id] = order

        if not self.api_key or not self.api_secret:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            order.message = "staged locally because Kraken credentials are not configured"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="staged locally because Kraken credentials are not configured")

        try:
            payload = self._private_request(
                endpoint="AddOrder",
                params={
                    "pair": self._normalize_symbol(symbol or "BTC/EUR"),
                    "type": self._normalize_side(side),
                    "ordertype": "limit",
                    "price": str(price),
                    "volume": str(size),
                },
            )
        except RuntimeError as exc:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            order.message = f"staged locally: {exc}"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"staged locally: {exc}")

        if isinstance(payload, dict) and payload.get("error"):
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            order.message = f"staged locally: {payload.get('error')}"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"staged locally: {payload.get('error')}")

        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        remote_order_id = None
        if isinstance(result, dict):
            txid_value = result.get("txid")
            if isinstance(txid_value, list) and txid_value:
                remote_order_id = str(txid_value[0])
            elif isinstance(txid_value, str):
                remote_order_id = txid_value
        order.remote_order_id = remote_order_id
        order.remote_status = "SUBMITTED"
        order.message = "submitted to Kraken"
        return ExecutionReport(order_id=order_id, status="SUBMITTED", message="submitted to Kraken")

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel an existing order and return the execution outcome."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if not self.api_key or not self.api_secret:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="Kraken credentials not configured")
        if order.status in {"FILLED", "CANCELED"}:
            return ExecutionReport(order_id=order_id, status=order.status, message="order already settled")

        try:
            payload = self._private_request(endpoint="CancelOrder", params={"txid": order.remote_order_id or order_id})
        except RuntimeError as exc:
            return ExecutionReport(order_id=order_id, status="REJECTED", message=str(exc))

        if isinstance(payload, dict) and payload.get("error"):
            return ExecutionReport(order_id=order_id, status="REJECTED", message=str(payload.get("error")))

        order.status = "CANCELED"
        order.remote_status = "CANCELED"
        order.message = "order canceled"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the latest status for the requested order."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if not self.api_key or not self.api_secret:
            return ExecutionReport(order_id=order_id, status=order.status, message="Kraken credentials not configured")

        try:
            payload = self._private_request(endpoint="QueryOrders", params={"txid": order.remote_order_id or order_id, "trades": "false"})
        except RuntimeError as exc:
            return ExecutionReport(order_id=order_id, status=order.status, message=str(exc))

        if isinstance(payload, dict) and payload.get("error"):
            return ExecutionReport(order_id=order_id, status=order.status, message=str(payload.get("error")))

        normalized_orders = self._normalize_remote_orders_payload(payload)
        remote_order = None
        for candidate in normalized_orders:
            if candidate.get("order_id") == order_id:
                remote_order = candidate
                break
        if remote_order is None and normalized_orders:
            remote_order = normalized_orders[0]
        if not isinstance(remote_order, dict):
            return ExecutionReport(order_id=order_id, status=order.status, fill_price=order.fill_price, filled_size=order.filled_size, fee=order.fee, message="order state unavailable")

        normalized_status = str(remote_order.get("status", order.status) or order.status)
        fill_price = self._coerce_float(remote_order.get("fill_price")) or order.fill_price
        filled_size = self._coerce_float(remote_order.get("filled_size")) or order.filled_size
        fee = self._coerce_float(remote_order.get("fee")) or order.fee
        order.status = normalized_status
        order.remote_status = normalized_status
        order.fill_price = fill_price
        order.filled_size = filled_size if filled_size is not None else order.filled_size
        order.fee = fee if fee is not None else order.fee
        order.message = "Kraken order state"
        return ExecutionReport(
            order_id=order_id,
            status=normalized_status,
            fill_price=fill_price,
            filled_size=filled_size,
            fee=fee if fee is not None else order.fee,
            message="Kraken order state",
        )

    def recover_execution_state(
        self,
        *,
        remote_snapshot: dict[str, Any] | None = None,
        remote_orders: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile local adapter state against Kraken-shaped snapshots and order payloads."""
        normalized_snapshot = self._normalize_balance_snapshot_payload(remote_snapshot)
        normalized_orders = self._normalize_remote_orders_payload(remote_orders)
        return super().recover_execution_state(remote_snapshot=normalized_snapshot, remote_orders=normalized_orders or None)

    def fetch_balance_snapshot(self) -> dict[str, Any]:
        """Fetch and normalize the current Kraken account balance snapshot."""
        payload = self._private_request(endpoint="Balance", params={})
        errors = self._extract_api_errors(payload)
        if errors:
            raise RuntimeError(self._format_api_errors(errors))
        normalized = self._normalize_balance_snapshot_payload(payload)
        if normalized is None:
            raise RuntimeError("Kraken balance snapshot was empty")
        return normalized

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        """Fetch and normalize the current Kraken open-order snapshot."""
        payload = self._private_request(endpoint="OpenOrders", params={"trades": "false"})
        errors = self._extract_api_errors(payload)
        if errors:
            raise RuntimeError(self._format_api_errors(errors))
        return self._normalize_remote_orders_payload(payload)

    def validate_order_request(self, *, symbol: str, side: str, size: float) -> dict[str, Any]:
        """Exercise Kraken's validate-only order path without placing a live order."""
        if size <= 0:
            raise ValueError("size must be positive")

        payload = self._private_request(
            endpoint="AddOrder",
            params={
                "pair": self._normalize_symbol(symbol),
                "type": self._normalize_side(side),
                "ordertype": "market",
                "volume": str(size),
                "validate": "true",
            },
        )
        errors = self._extract_api_errors(payload)
        if errors:
            raise RuntimeError(self._format_api_errors(errors))

        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        description = None
        if isinstance(result, dict):
            descr = result.get("descr")
            if isinstance(descr, dict):
                description = descr.get("order")
            elif descr is not None:
                description = str(descr)

        return {
            "validated": True,
            "symbol": symbol,
            "side": self._normalize_side(side),
            "size": size,
            "description": description,
        }

    def verify_dry_run(
        self,
        *,
        symbol: str = "BTC/EUR",
        side: str = "buy",
        size: float = 0.0002,
        probe_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Run non-destructive Kraken private-endpoint verification for live_dry_run readiness."""
        summary: dict[str, Any] = {
            "exchange": self.exchange_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "size": size,
            "credentials_configured": bool(self.api_key and self.api_secret),
            "checks": [],
        }
        if not summary["credentials_configured"]:
            summary["status"] = "failed"
            summary["checks"].append(
                {
                    "name": "credentials",
                    "ok": False,
                    "message": "Kraken credentials are not configured",
                }
            )
            return summary

        checks: list[dict[str, Any]] = []

        balance_snapshot: dict[str, Any] | None = None
        try:
            balance_snapshot = self.fetch_balance_snapshot()
            checks.append(
                {
                    "name": "balance_snapshot",
                    "ok": True,
                    "message": "authenticated balance snapshot retrieved",
                    "balances": balance_snapshot.get("balances", {}),
                    "positions": balance_snapshot.get("positions", {}),
                }
            )
        except Exception as exc:
            checks.append({"name": "balance_snapshot", "ok": False, "message": str(exc)})

        open_orders: list[dict[str, Any]] = []
        try:
            open_orders = self.fetch_open_orders()
            checks.append(
                {
                    "name": "open_orders",
                    "ok": True,
                    "message": f"retrieved {len(open_orders)} open orders",
                    "open_order_count": len(open_orders),
                }
            )
        except Exception as exc:
            checks.append({"name": "open_orders", "ok": False, "message": str(exc)})

        status_probe_order_id = probe_order_id
        expected_status_errors: tuple[str, ...] = ()
        if status_probe_order_id is None and open_orders:
            status_probe_order_id = str(open_orders[0].get("remote_order_id") or open_orders[0].get("order_id") or "")
        if not status_probe_order_id:
            status_probe_order_id = "DRYRUNVERIFY-STATUS"
            expected_status_errors = ("unknown order", "invalid order")
        checks.append(
            self._probe_private_endpoint(
                name="order_status",
                endpoint="QueryOrders",
                params={"txid": status_probe_order_id, "trades": "false"},
                expected_errors=expected_status_errors,
            )
        )

        checks.append(
            self._probe_private_endpoint(
                name="cancel_order",
                endpoint="CancelOrder",
                params={"txid": "DRYRUNVERIFY-CANCEL"},
                expected_errors=("unknown order", "invalid order"),
            )
        )

        try:
            validation = self.validate_order_request(symbol=symbol, side=side, size=size)
            checks.append(
                {
                    "name": "validate_order",
                    "ok": True,
                    "message": "Kraken validate-only order probe succeeded",
                    "description": validation.get("description"),
                }
            )
        except Exception as exc:
            checks.append({"name": "validate_order", "ok": False, "message": str(exc)})

        summary["checks"] = checks
        summary["balance_snapshot"] = balance_snapshot or {}
        summary["open_order_count"] = len(open_orders)
        summary["status"] = "passed" if all(bool(check.get("ok")) for check in checks) else "failed"
        return summary

    def _private_request(self, *, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Kraken credentials not configured")

        nonce = str(int(time.time() * 1000))
        body = dict(params)
        body["nonce"] = nonce
        encoded_body = urllib.parse.urlencode(body).encode("utf-8")
        sha256_digest = hashlib.sha256(f"{nonce}{encoded_body.decode('utf-8')}".encode("utf-8")).digest()
        try:
            secret_bytes = base64.b64decode(self.api_secret)
        except Exception:
            secret_bytes = self.api_secret.encode("utf-8")
        signature = hmac.new(
            secret_bytes,
            f"/0/private/{endpoint}".encode("utf-8") + sha256_digest,
            hashlib.sha512,
        ).digest()
        headers = {
            "API-Key": self.api_key,
            "API-Sign": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self._request_json(
            "POST",
            f"https://api.kraken.com/0/private/{endpoint}",
            headers=headers,
            data=body,
        )

    def _normalize_symbol(self, symbol: str) -> str:
        mapping = {
            "BTC/EUR": "XXBTZEUR",
            "BTC/USD": "XXBTZUSD",
            "ETH/EUR": "XETHZEUR",
            "ETH/USD": "XETHZUSD",
        }
        return mapping.get(symbol, symbol.upper().replace("/", ""))

    def _denormalize_symbol(self, symbol: str | None) -> str | None:
        if not symbol:
            return None
        mapping = {
            "XXBTZEUR": "BTC/EUR",
            "XBT/EUR": "BTC/EUR",
            "XXBTZUSD": "BTC/USD",
            "XBT/USD": "BTC/USD",
            "XETHZEUR": "ETH/EUR",
            "ETH/EUR": "ETH/EUR",
            "XETHZUSD": "ETH/USD",
            "ETH/USD": "ETH/USD",
        }
        normalized = str(symbol).strip().upper()
        return mapping.get(normalized, normalized)

    def _normalize_balance_snapshot_payload(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        if "balances" in payload or "positions" in payload:
            return payload
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            return payload

        balances: dict[str, float] = {}
        positions: dict[str, float] = {}
        for raw_asset, raw_amount in result.items():
            asset = self._normalize_asset_code(raw_asset)
            amount = self._coerce_float(raw_amount)
            if asset is None or amount is None:
                continue
            if self._is_cash_asset(asset):
                balances[asset] = amount
            elif amount != 0.0:
                positions[asset] = amount

        balances.setdefault(self._base_currency, 0.0)
        return {"balances": balances, "positions": positions}

    def _normalize_remote_orders_payload(self, payload: list[dict[str, Any]] | dict[str, Any] | None) -> list[dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        result = payload.get("result", payload)
        if not isinstance(result, dict):
            return []

        if "open" in result and isinstance(result["open"], dict):
            result = result["open"]
        elif "closed" in result and isinstance(result["closed"], dict):
            result = result["closed"]

        normalized_orders: list[dict[str, Any]] = []
        for remote_order_id, remote_payload in result.items():
            if not isinstance(remote_payload, dict):
                continue
            normalized_order = self._normalize_remote_order_payload(str(remote_order_id), remote_payload)
            if normalized_order is not None:
                normalized_orders.append(normalized_order)
        return normalized_orders

    def _normalize_remote_order_payload(self, remote_order_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        descr = payload.get("descr")
        order_symbol = None
        side = str(payload.get("type", "unknown") or "unknown")
        if isinstance(descr, dict):
            order_symbol = self._denormalize_symbol(descr.get("pair"))
            side = str(descr.get("type", side) or side)

        local_order_id = self._resolve_local_order_id(remote_order_id)
        return {
            "order_id": local_order_id,
            "remote_order_id": remote_order_id,
            "side": side,
            "size": self._coerce_float(payload.get("vol")) or self._coerce_float(payload.get("volume")) or 0.0,
            "symbol": order_symbol,
            "status": self._normalize_order_status(payload.get("status")),
            "filled_size": self._coerce_float(payload.get("vol_exec")) or self._coerce_float(payload.get("filled_size")) or 0.0,
            "fill_price": self._coerce_float(payload.get("price")) or self._coerce_float(payload.get("avg_price")),
            "fee": self._coerce_float(payload.get("fee")) or 0.0,
        }

    def _resolve_local_order_id(self, remote_order_id: str) -> str:
        for local_order_id, order in self._orders.items():
            if getattr(order, "remote_order_id", None) == remote_order_id:
                return local_order_id
        return remote_order_id

    def _normalize_order_status(self, status: Any) -> str:
        normalized = str(status or "").strip().lower()
        if normalized in {"closed", "filled"}:
            return "FILLED"
        if normalized in {"open"}:
            return "OPEN"
        if normalized in {"pending"}:
            return "SUBMITTED"
        if normalized in {"canceled", "cancelled"}:
            return "CANCELED"
        if normalized in {"partial", "partially_filled", "partially-filled"}:
            return "PARTIALLY_FILLED"
        return "SUBMITTED"

    def _normalize_asset_code(self, asset_code: Any) -> str | None:
        if asset_code is None:
            return None
        mapping = {
            "ZEUR": "EUR",
            "ZUSD": "USD",
            "ZNOK": "NOK",
            "XXBT": "BTC",
            "XBT": "BTC",
            "XETH": "ETH",
        }
        normalized = str(asset_code).strip().upper()
        return mapping.get(normalized, normalized.lstrip("XZ"))

    def _is_cash_asset(self, asset: str) -> bool:
        return asset in {self._base_currency, "EUR", "USD", "NOK", "GBP", "USDT", "USDC"}

    def _normalize_side(self, side: str) -> str:
        normalized = side.lower()
        if normalized in {"buy", "long"}:
            return "buy"
        if normalized in {"sell", "short"}:
            return "sell"
        raise ValueError(f"unsupported side: {side}")

    def _probe_private_endpoint(
        self,
        *,
        name: str,
        endpoint: str,
        params: dict[str, Any],
        expected_errors: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        try:
            payload = self._private_request(endpoint=endpoint, params=params)
        except Exception as exc:
            return {"name": name, "ok": False, "message": str(exc)}

        errors = self._extract_api_errors(payload)
        if not errors:
            return {"name": name, "ok": True, "message": f"{endpoint} succeeded"}

        normalized_errors = [str(error).lower() for error in errors]
        if expected_errors and any(expected in error for expected in expected_errors for error in normalized_errors):
            return {
                "name": name,
                "ok": True,
                "message": f"{endpoint} returned expected non-destructive error",
                "errors": errors,
            }
        return {"name": name, "ok": False, "message": self._format_api_errors(errors), "errors": errors}

    def _extract_api_errors(self, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        raw_errors = payload.get("error")
        if isinstance(raw_errors, list):
            return [str(error) for error in raw_errors if str(error).strip()]
        if isinstance(raw_errors, str) and raw_errors.strip():
            return [raw_errors]
        return []

    def _format_api_errors(self, errors: list[str]) -> str:
        return "; ".join(error for error in errors if error) or "Kraken API returned an unknown error"


class FiriExecutionAdapter(ExchangeExecutionAdapter):
    """Adapter for Firi order routing with authenticated API calls and reconciliation."""

    name = "firi"

    def __init__(self, *, fee_rate: float = 0.001, api_key: str | None = None) -> None:
        """Initialize the object with its runtime state."""
        super().__init__(
            exchange_name="firi",
            fee_rate=fee_rate,
            api_key=api_key if api_key is not None else settings.firi_api_key,
            api_secret=None,
        )
        self._base_currency = "NOK"
        self._balances.setdefault(self._base_currency, 0.0)

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Submit an order through the adapter and capture the execution result."""
        if size <= 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="size must be positive")

        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=size,
            symbol=symbol,
            price=price,
            timestamp=timestamp,
            status="SUBMITTED",
            exchange=self.exchange_name,
        )
        self._orders[order_id] = order

        if not self.api_key:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            order.message = "staged locally because Firi credentials are not configured"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="staged locally because Firi credentials are not configured")

        try:
            request_data: dict[str, Any] = {
                "side": self._normalize_side(side),
                "amount": size,
                "price": price,
                "type": "limit",
            }
            if symbol:
                request_data["market"] = symbol.replace("/", "")
            payload = self._request_json(
                "POST",
                "https://api.firi.com/v2/orders",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                data=request_data,
            )
        except RuntimeError as exc:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            order.message = f"staged locally: {exc}"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"staged locally: {exc}")

        if isinstance(payload, dict) and payload.get("error"):
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            order.message = f"staged locally: {payload.get('error')}"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"staged locally: {payload.get('error')}")

        remote_order_id = None
        if isinstance(payload, dict):
            remote_order_id = payload.get("id")
            if remote_order_id is None and isinstance(payload.get("order"), dict):
                remote_order_id = payload["order"].get("id")
        order.remote_order_id = str(remote_order_id) if remote_order_id is not None else None

        status = self._normalize_remote_status(payload)
        order.remote_status = status
        order.status = status
        order.message = "submitted to Firi"
        return ExecutionReport(
            order_id=order_id,
            status=status,
            fill_price=self._coerce_float(payload.get("price")) if isinstance(payload, dict) else None,
            filled_size=self._coerce_float(payload.get("filled_size")) if isinstance(payload, dict) else None,
            fee=self._coerce_float(payload.get("fee")) if isinstance(payload, dict) else 0.0,
            message="submitted to Firi",
        )

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel an existing order and return the execution outcome."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if not self.api_key:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="Firi API key not configured")
        if order.status in {"FILLED", "CANCELED"}:
            return ExecutionReport(order_id=order_id, status=order.status, message="order already settled")

        try:
            payload = self._request_json(
                "DELETE",
                f"https://api.firi.com/v2/orders/{order.remote_order_id or order_id}",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            )
        except RuntimeError as exc:
            return ExecutionReport(order_id=order_id, status="REJECTED", message=str(exc))

        if isinstance(payload, dict) and payload.get("error"):
            return ExecutionReport(order_id=order_id, status="REJECTED", message=str(payload.get("error")))

        order.status = "CANCELED"
        order.remote_status = "CANCELED"
        order.message = "order canceled"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the latest status for the requested order."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if not self.api_key:
            return ExecutionReport(order_id=order_id, status=order.status, message="Firi API key not configured")

        try:
            payload = self._request_json(
                "GET",
                f"https://api.firi.com/v2/orders/{order.remote_order_id or order_id}",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            )
        except RuntimeError as exc:
            return ExecutionReport(order_id=order_id, status=order.status, message=str(exc))

        if isinstance(payload, dict) and payload.get("error"):
            return ExecutionReport(order_id=order_id, status=order.status, message=str(payload.get("error")))

        status = self._normalize_remote_status(payload)
        order.status = status
        order.remote_status = status
        if isinstance(payload, dict):
            fill_price = self._coerce_float(payload.get("price"))
            filled_size = self._coerce_float(payload.get("filled_size"))
            fee = self._coerce_float(payload.get("fee"))
            if fill_price is not None:
                order.fill_price = fill_price
            if filled_size is not None:
                order.filled_size = filled_size
            if fee is not None:
                order.fee = fee
        order.message = "Firi order state"
        return ExecutionReport(
            order_id=order_id,
            status=status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message="Firi order state",
        )

    def _normalize_remote_status(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return "SUBMITTED"

        raw_status = str(payload.get("status", "")).strip().lower()
        if raw_status in {"filled", "closed", "complete", "completed"}:
            return "FILLED"
        if raw_status in {"partially_filled", "partially-filled", "partial"}:
            return "PARTIALLY_FILLED"
        if raw_status in {"canceled", "cancelled", "cancel"}:
            return "CANCELED"
        if raw_status in {"open", "pending", "submitted", "active"}:
            return "OPEN"
        return "SUBMITTED"

    def _normalize_side(self, side: str) -> str:
        normalized = side.lower()
        if normalized in {"buy", "long"}:
            return "buy"
        if normalized in {"sell", "short"}:
            return "sell"
        raise ValueError(f"unsupported side: {side}")


class LiveExecutionAdapter(ExecutionAdapter):
    """Fallback adapter for real exchange routing; uses the exchange-specific adapters when available."""

    name = "live"

    def __init__(self, *, exchange_name: str = "live") -> None:
        """Initialize the object with its runtime state."""
        super().__init__()
        self.exchange_name = exchange_name

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Submit an order through the adapter and capture the execution result."""
        return ExecutionReport(order_id=order_id, status="REJECTED", message=f"Live execution for {self.exchange_name} is not implemented yet")

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel an existing order and return the execution outcome."""
        return ExecutionReport(order_id=order_id, status="NOT_FOUND", message=f"Live execution for {self.exchange_name} is not implemented yet")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the latest status for the requested order."""
        return ExecutionReport(order_id=order_id, status="NOT_FOUND", message=f"Live execution for {self.exchange_name} is not implemented yet")


class ExecutionRouter:
    """Select an adapter for the requested runtime mode."""

    def __init__(self, *, mode: str, adapter: ExecutionAdapter | None = None, exchange: str | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.mode = mode
        self.exchange = exchange
        self.adapter = adapter or self._build_adapter(mode, exchange=exchange)

    def _build_adapter(self, mode: str, *, exchange: str | None = None) -> ExecutionAdapter | None:
        if mode in {"paper", None}:
            return None
        normalized_exchange = (exchange or "").strip().lower()
        if mode == "live_dry_run":
            if normalized_exchange in {"kraken", "firi"}:
                return SandboxExecutionAdapter(exchange_name=normalized_exchange)
            return SandboxExecutionAdapter(exchange_name="sandbox")
        if mode == "live":
            if normalized_exchange == "kraken":
                return KrakenExecutionAdapter()
            if normalized_exchange == "firi":
                return FiriExecutionAdapter()
            return LiveExecutionAdapter(exchange_name="live")
        raise ValueError(f"unsupported runtime mode: {mode}")
