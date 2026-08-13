"""Execution adapters and sandbox routing helpers for safe order placement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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


class ExecutionAdapter:
    """Base interface for exchange execution adapters."""

    name: str = "base"

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime) -> ExecutionReport:
        raise NotImplementedError

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        raise NotImplementedError

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        raise NotImplementedError


class SandboxExecutionAdapter(ExecutionAdapter):
    """A safe in-process adapter that simulates order acceptance and fills."""

    name = "sandbox"

    def __init__(self, *, exchange_name: str = "sandbox", fee_rate: float = 0.001) -> None:
        self.exchange_name = exchange_name
        self.fee_rate = fee_rate
        self._orders: dict[str, ExecutionOrder] = {}

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

    def list_orders(self) -> list[ExecutionOrder]:
        return list(self._orders.values())


class LiveExecutionAdapter(ExecutionAdapter):
    """Placeholder adapter for real exchange routing; intentionally not implemented yet."""

    name = "live"

    def __init__(self, *, exchange_name: str = "live") -> None:
        self.exchange_name = exchange_name

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime) -> ExecutionReport:
        raise NotImplementedError(f"Live execution for {self.exchange_name} is not implemented yet")

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        raise NotImplementedError(f"Live execution for {self.exchange_name} is not implemented yet")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        raise NotImplementedError(f"Live execution for {self.exchange_name} is not implemented yet")


class ExecutionRouter:
    """Select an adapter for the requested runtime mode."""

    def __init__(self, *, mode: str, adapter: ExecutionAdapter | None = None) -> None:
        self.mode = mode
        self.adapter = adapter or self._build_adapter(mode)

    def _build_adapter(self, mode: str) -> ExecutionAdapter | None:
        if mode in {"paper", None}:
            return None
        if mode == "live_dry_run":
            return SandboxExecutionAdapter(exchange_name="sandbox")
        if mode == "live":
            return LiveExecutionAdapter(exchange_name="live")
        raise ValueError(f"unsupported runtime mode: {mode}")
