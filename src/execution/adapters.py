"""Execution adapters and sandbox routing helpers for safe order placement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    price: float | None = None
    timestamp: datetime | None = None
    status: str = "SUBMITTED"
    fill_price: float | None = None
    filled_size: float | None = None
    fee: float = 0.0
    exchange: str | None = None
    remote_status: str | None = None
    reconciled: bool = False


class ExecutionAdapter:
    """Base interface for exchange execution adapters."""

    name: str = "base"

    def __init__(self) -> None:
        self._orders: dict[str, ExecutionOrder] = {}

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime) -> ExecutionReport:
        raise NotImplementedError

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        raise NotImplementedError

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
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
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")

        if remote_status is not None:
            order.status = remote_status
            order.remote_status = remote_status
        if remote_filled_size is not None:
            order.filled_size = remote_filled_size
        if remote_fill_price is not None:
            order.fill_price = remote_fill_price
        if remote_fee is not None:
            order.fee = remote_fee

        order.reconciled = True
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message="order state reconciled",
        )

    def list_orders(self) -> list[ExecutionOrder]:
        return list(self._orders.values())


class SandboxExecutionAdapter(ExecutionAdapter):
    """A safe in-process adapter that simulates order acceptance and fills."""

    name = "sandbox"

    def __init__(self, *, exchange_name: str = "sandbox", fee_rate: float = 0.001) -> None:
        super().__init__()
        self.exchange_name = exchange_name
        self.fee_rate = fee_rate

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime) -> ExecutionReport:
        if size <= 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="size must be positive")

        execution_price = price
        filled_size = size
        fee = max(0.0, size * execution_price * self.fee_rate)
        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=size,
            price=price,
            timestamp=timestamp,
            status="FILLED",
            fill_price=execution_price,
            filled_size=filled_size,
            fee=fee,
            exchange=self.exchange_name,
        )
        self._orders[order_id] = order
        return ExecutionReport(
            order_id=order_id,
            status="FILLED",
            fill_price=execution_price,
            filled_size=filled_size,
            fee=fee,
            message=f"submitted to {self.exchange_name}",
        )

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if order.status == "FILLED":
            return ExecutionReport(order_id=order_id, status="FILLED", message="order already filled")
        order.status = "CANCELED"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message="sandbox order state",
        )


class ExchangeExecutionAdapter(ExecutionAdapter):
    """Base class for exchange-specific execution adapters with local reconciliation state."""

    def __init__(self, *, exchange_name: str, fee_rate: float = 0.001, api_key: str | None = None, api_secret: str | None = None) -> None:
        super().__init__()
        self.exchange_name = exchange_name
        self.fee_rate = fee_rate
        self.api_key = api_key
        self.api_secret = api_secret

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime) -> ExecutionReport:
        if size <= 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="size must be positive")

        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=size,
            price=price,
            timestamp=timestamp,
            status="SUBMITTED",
            exchange=self.exchange_name,
        )
        self._orders[order_id] = order
        return ExecutionReport(
            order_id=order_id,
            status="SUBMITTED",
            message=f"staged locally for {self.exchange_name}",
        )

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if order.status in {"FILLED", "CANCELED"}:
            return ExecutionReport(order_id=order_id, status=order.status, message="order already settled")
        order.status = "CANCELED"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message=f"{self.exchange_name} order state",
        )


class KrakenExecutionAdapter(ExchangeExecutionAdapter):
    """Skeleton adapter for Kraken order routing with local reconciliation state."""

    name = "kraken"

    def __init__(self, *, fee_rate: float = 0.001, api_key: str | None = None, api_secret: str | None = None) -> None:
        super().__init__(
            exchange_name="kraken",
            fee_rate=fee_rate,
            api_key=api_key if api_key is not None else settings.kraken_api_key,
            api_secret=api_secret if api_secret is not None else settings.kraken_secret,
        )


class FiriExecutionAdapter(ExchangeExecutionAdapter):
    """Skeleton adapter for Firi order routing with local reconciliation state."""

    name = "firi"

    def __init__(self, *, fee_rate: float = 0.001, api_key: str | None = None) -> None:
        super().__init__(
            exchange_name="firi",
            fee_rate=fee_rate,
            api_key=api_key if api_key is not None else settings.firi_api_key,
            api_secret=None,
        )


class LiveExecutionAdapter(ExecutionAdapter):
    """Fallback adapter for real exchange routing; uses the exchange-specific adapters when available."""

    name = "live"

    def __init__(self, *, exchange_name: str = "live") -> None:
        super().__init__()
        self.exchange_name = exchange_name

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="REJECTED", message=f"Live execution for {self.exchange_name} is not implemented yet")

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="NOT_FOUND", message=f"Live execution for {self.exchange_name} is not implemented yet")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="NOT_FOUND", message=f"Live execution for {self.exchange_name} is not implemented yet")


class ExecutionRouter:
    """Select an adapter for the requested runtime mode."""

    def __init__(self, *, mode: str, adapter: ExecutionAdapter | None = None, exchange: str | None = None) -> None:
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
