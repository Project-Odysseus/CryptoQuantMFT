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
    remote_order_id: str | None = None
    reconciled: bool = False


class ExecutionAdapter:
    """Base interface for exchange execution adapters."""

    name: str = "base"

    def __init__(self) -> None:
        self._orders: dict[str, ExecutionOrder] = {}
        self._balances: dict[str, float] = {}
        self._positions: dict[str, float] = {}
        self._base_currency = "USD"

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
        return ExecutionReport(
            order_id=order_id,
            status=order.status,
            fill_price=order.fill_price,
            filled_size=order.filled_size,
            fee=order.fee,
            message="order state reconciled",
        )

    def get_account_snapshot(self) -> dict[str, Any]:
        return {
            "balances": dict(self._balances),
            "positions": dict(self._positions),
        }

    def list_orders(self) -> list[ExecutionOrder]:
        return list(self._orders.values())

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
        self._balances.setdefault(base_currency, 0.0)
        if order.side == "buy":
            fill_delta = filled_size - previous_fill_size
            if fill_delta <= 0.0:
                return
            price = float(fill_price or order.price or 0.0)
            fee_delta = max(0.0, (fee or 0.0) - previous_fee)
            self._balances[base_currency] = self._balances.get(base_currency, 0.0) - (fill_delta * price) - fee_delta
            self._positions["BTC"] = self._positions.get("BTC", 0.0) + fill_delta
        elif order.side == "sell":
            fill_delta = filled_size - previous_fill_size
            if fill_delta <= 0.0:
                return
            price = float(fill_price or order.price or 0.0)
            fee_delta = max(0.0, (fee or 0.0) - previous_fee)
            self._balances[base_currency] = self._balances.get(base_currency, 0.0) + (fill_delta * price) - fee_delta
            self._positions["BTC"] = max(0.0, self._positions.get("BTC", 0.0) - fill_delta)


class SandboxExecutionAdapter(ExecutionAdapter):
    """A safe in-process adapter that simulates order acceptance and fills."""

    name = "sandbox"

    def __init__(self, *, exchange_name: str = "sandbox", fee_rate: float = 0.001) -> None:
        super().__init__()
        self.exchange_name = exchange_name
        self.fee_rate = fee_rate
        self._base_currency = "EUR" if exchange_name == "kraken" else "NOK" if exchange_name == "firi" else "USD"
        self._balances = {self._base_currency: 1000.0}
        self._positions = {}

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
    """Adapter for Kraken order routing with authenticated API calls and reconciliation."""

    name = "kraken"

    def __init__(self, *, fee_rate: float = 0.001, api_key: str | None = None, api_secret: str | None = None) -> None:
        super().__init__(
            exchange_name="kraken",
            fee_rate=fee_rate,
            api_key=api_key if api_key is not None else settings.kraken_api_key,
            api_secret=api_secret if api_secret is not None else settings.kraken_secret,
        )

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

        if not self.api_key or not self.api_secret:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="staged locally because Kraken credentials are not configured")

        try:
            payload = self._private_request(
                endpoint="AddOrder",
                params={
                    "pair": self._normalize_symbol("BTC/EUR"),
                    "type": self._normalize_side(side),
                    "ordertype": "limit",
                    "price": str(price),
                    "volume": str(size),
                },
            )
        except RuntimeError as exc:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"staged locally: {exc}")

        if isinstance(payload, dict) and payload.get("error"):
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
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
        return ExecutionReport(order_id=order_id, status="SUBMITTED", message="submitted to Kraken")

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
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
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
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

        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        remote_order = None
        if isinstance(result, dict):
            remote_order = result.get(order.remote_order_id or order_id)
            if remote_order is None and result:
                remote_order = next(iter(result.values()))
        if not isinstance(remote_order, dict):
            return ExecutionReport(order_id=order_id, status=order.status, fill_price=order.fill_price, filled_size=order.filled_size, fee=order.fee, message="order state unavailable")

        status = str(remote_order.get("status", order.status)).upper()
        if status == "CLOSED":
            normalized_status = "FILLED"
        elif status in {"OPEN", "PENDING"}:
            normalized_status = "OPEN"
        elif status == "CANCELED":
            normalized_status = "CANCELED"
        else:
            normalized_status = "SUBMITTED"

        fill_price = self._coerce_float(remote_order.get("price")) or self._coerce_float(remote_order.get("avg_price")) or order.fill_price
        filled_size = self._coerce_float(remote_order.get("vol_exec")) or self._coerce_float(remote_order.get("vol")) or order.filled_size
        fee = self._coerce_float(remote_order.get("fee")) or order.fee
        order.status = normalized_status
        order.remote_status = normalized_status
        order.fill_price = fill_price
        order.filled_size = filled_size if filled_size is not None else order.filled_size
        order.fee = fee if fee is not None else order.fee
        return ExecutionReport(
            order_id=order_id,
            status=normalized_status,
            fill_price=fill_price,
            filled_size=filled_size,
            fee=fee if fee is not None else order.fee,
            message="Kraken order state",
        )

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
        ).hexdigest()
        headers = {
            "API-Key": self.api_key,
            "API-Sign": signature,
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

    def _normalize_side(self, side: str) -> str:
        normalized = side.lower()
        if normalized in {"buy", "long"}:
            return "buy"
        if normalized in {"sell", "short"}:
            return "sell"
        raise ValueError(f"unsupported side: {side}")


class FiriExecutionAdapter(ExchangeExecutionAdapter):
    """Adapter for Firi order routing with authenticated API calls and reconciliation."""

    name = "firi"

    def __init__(self, *, fee_rate: float = 0.001, api_key: str | None = None) -> None:
        super().__init__(
            exchange_name="firi",
            fee_rate=fee_rate,
            api_key=api_key if api_key is not None else settings.firi_api_key,
            api_secret=None,
        )

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

        if not self.api_key:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="staged locally because Firi credentials are not configured")

        try:
            payload = self._request_json(
                "POST",
                "https://api.firi.com/v2/orders",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                data={
                    "side": self._normalize_side(side),
                    "amount": size,
                    "price": price,
                    "type": "limit",
                },
            )
        except RuntimeError as exc:
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"staged locally: {exc}")

        if isinstance(payload, dict) and payload.get("error"):
            order.status = "SUBMITTED"
            order.remote_status = "SUBMITTED"
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
        return ExecutionReport(
            order_id=order_id,
            status=status,
            fill_price=self._coerce_float(payload.get("price")) if isinstance(payload, dict) else None,
            filled_size=self._coerce_float(payload.get("filled_size")) if isinstance(payload, dict) else None,
            fee=self._coerce_float(payload.get("fee")) if isinstance(payload, dict) else 0.0,
            message="submitted to Firi",
        )

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
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
        return ExecutionReport(order_id=order_id, status="CANCELED", message="order canceled")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
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
