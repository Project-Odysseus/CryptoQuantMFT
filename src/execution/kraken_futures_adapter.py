"""Execution adapter for Kraken Futures perpetuals (real orders when used in `--runtime live`).

Endpoints, parameters and response fields follow Kraken's Derivatives REST
OpenAPI spec (https://docs.kraken.com/openapi/futures-rest.yaml). This code
has only been exercised against recorded response shapes, never against the
live API: run `python main.py --futures-verify-credentials` first, and start
with the minimum size.

How it trades:

- Every order is an immediate-or-cancel market order (`orderType=mkt`, which
  Kraken caps at 1% price protection) with a globally unique `cliOrdId`, so
  its outcome can be found again even if the HTTP response is lost.
- An order that only shrinks the position is sent `reduceOnly=true`, so a
  stale local view can never turn a close into a new position.
- Contract rules (size step, minimum) and this account's leverage cap are
  checked locally before anything is sent.
- Fills are reconciled from `/fills` by `cliOrdId` (Kraken's order-status
  endpoint only remembers orders for 5 seconds after they finish), then the
  position and margin are re-read from `/openpositions` and `/accounts`,
  which are treated as the source of truth.

Kraken's fills carry no fee, so fees are estimated from the contract's taker
rate and flagged as estimates in the order message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.execution.adapters import ExecutionOrder, ExecutionReport
from src.execution.perps import MarginAccountAdapter, PerpContract

API_BASE = "https://futures.kraken.com/derivatives"

# sendorder statuses that mean the order reached the matching engine.
_ACCEPTED_SEND_STATUSES = {"placed", "partiallyFilled", "filled"}

Transport = Callable[[str, str, dict[str, str], bytes | None], dict[str, Any]]


def sign_request(*, post_data: str, nonce: str, endpoint_path: str, api_secret: str) -> str:
    """Compute Kraken Futures' `Authent` header.

    SHA-256 of ``post_data + nonce + endpoint_path``, then HMAC-SHA-512 keyed
    with the base64-decoded secret, base64-encoded. `post_data` must be the
    URL-encoded parameter string exactly as sent (Kraken's current rule), and
    `endpoint_path` excludes the ``/derivatives`` prefix, e.g.
    ``/api/v3/sendorder``.
    """
    digest = hashlib.sha256((post_data + nonce + endpoint_path).encode("utf-8")).digest()
    signature = hmac.new(base64.b64decode(api_secret), digest, hashlib.sha512).digest()
    return base64.b64encode(signature).decode("utf-8")


def _default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method, headers=headers, data=body)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class KrakenFuturesPrivateClient:
    """Signed calls to Kraken Futures' private REST API (shared by the single- and multi-contract adapters)."""

    def __init__(self, *, api_key: str, api_secret: str, transport: Transport | None = None) -> None:
        """`transport` replaces the HTTP call in tests."""
        if not api_key or not api_secret:
            raise ValueError("Kraken Futures API key and secret are required")
        self.api_key = api_key
        self.api_secret = api_secret
        self.transport = transport or _default_transport
        self._last_nonce = 0

    def nonce(self) -> str:
        """A strictly increasing millisecond nonce, as Kraken requires."""
        nonce = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = nonce
        return str(nonce)

    def call(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an authenticated endpoint and return its JSON, raising on anything but `result == success`."""
        post_data = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
        nonce = self.nonce()
        headers = {
            "APIKey": self.api_key,
            "Nonce": nonce,
            "Authent": sign_request(post_data=post_data, nonce=nonce, endpoint_path=f"/api/v3/{endpoint}", api_secret=self.api_secret),
            "Accept": "application/json",
            "User-Agent": "CryptoQuantMFT/0.1",
        }
        url = f"{API_BASE}/api/v3/{endpoint}"
        body: bytes | None = None
        if method == "GET":
            if post_data:
                url = f"{url}?{post_data}"
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = post_data.encode("utf-8")
        payload = self.transport(method, url, headers, body)
        if not isinstance(payload, dict) or payload.get("result") != "success":
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise RuntimeError(f"Kraken Futures {endpoint} failed: {error}")
        return payload


class KrakenFuturesExecutionAdapter(MarginAccountAdapter):
    """Trades one Kraken Futures perpetual through the authenticated REST API."""

    name = "kraken_futures"

    def __init__(
        self,
        *,
        contract: PerpContract,
        api_key: str,
        api_secret: str,
        max_leverage: float = 2.0,
        transport: Transport | None = None,
        min_sync_seconds: float = 5.0,
    ) -> None:
        """Create an adapter for `contract` that refuses new exposure above `max_leverage`.

        Args:
            contract: Must be `verified` (built from Kraken's public data), so
                size rules and fees are the venue's, not placeholders.
            transport: Replaces the HTTP call in tests.
            min_sync_seconds: Minimum spacing of account syncs triggered by
                market updates, to stay well inside the API rate budget.
        """
        if not contract.verified:
            raise ValueError("refusing an unverified contract for real trading; build it with perp_contract_from_instrument()")
        if not api_key or not api_secret:
            raise ValueError("Kraken Futures API key and secret are required")
        super().__init__(contract=contract, max_leverage=max_leverage, exchange_name="kraken_futures")
        self._client = KrakenFuturesPrivateClient(api_key=api_key, api_secret=api_secret, transport=transport)
        self._min_sync_seconds = min_sync_seconds
        self._last_sync_monotonic: float | None = None
        self._session_prefix = uuid.uuid4().hex[:12]
        self.margin_equity: float | None = None
        self.available_margin: float | None = None
        self.unrealized_funding: float = 0.0

    # ---- HTTP -------------------------------------------------------------

    def _private(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an authenticated endpoint (see `KrakenFuturesPrivateClient.call`)."""
        return self._client.call(method, endpoint, params)

    # ---- orders -----------------------------------------------------------

    def client_order_id(self, order_id: str) -> str:
        """The globally unique id this adapter sends for a local order id."""
        return f"cqm-{self._session_prefix}-{order_id}"[:100]

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Send an IOC market order after local contract and leverage checks. Returns SUBMITTED or REJECTED."""
        self._mark_price = price
        rounded, reduces_only, rejection = self._validate_order(order_id=order_id, side=side, size=size, price=price, symbol=symbol)
        if rejection is not None:
            return rejection

        cli_ord_id = self.client_order_id(order_id)
        order = ExecutionOrder(
            order_id=order_id,
            side=side.lower(),
            size=rounded,
            symbol=symbol or self.contract.symbol,
            price=price,
            timestamp=timestamp,
            status="SUBMITTED",
            exchange=self.exchange_name,
        )
        params = {
            "orderType": "mkt",
            "symbol": self.contract.venue_symbol,
            "side": order.side,
            "size": self._format_size(rounded),
            "cliOrdId": cli_ord_id,
            "reduceOnly": "true" if reduces_only else None,
        }
        try:
            payload = self._private("POST", "sendorder", params)
        except Exception as exc:
            # The request may or may not have reached Kraken. Keep the order pending so reconciliation looks
            # its cliOrdId up in /fills instead of assuming either outcome.
            order.message = f"submission outcome unknown ({exc}); will reconcile by cliOrdId {cli_ord_id}"
            self._orders[order_id] = order
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=order.message)

        send_status = payload.get("sendStatus") or {}
        status = str(send_status.get("status", ""))
        if status not in _ACCEPTED_SEND_STATUSES:
            return self._reject(order_id, f"Kraken Futures rejected the order: {status or 'no status'}")
        order.remote_order_id = send_status.get("order_id")
        order.remote_status = status
        order.message = f"sent to Kraken Futures ({status}), cliOrdId {cli_ord_id}"
        self._orders[order_id] = order
        return ExecutionReport(order_id=order_id, status="SUBMITTED", message=order.message)

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel a resting order on Kraken (IOC orders rarely rest, but a lost response can leave one)."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if order.status in {"FILLED", "CANCELED", "REJECTED"}:
            return ExecutionReport(order_id=order_id, status=order.status, message="order already final")
        params = {"order_id": order.remote_order_id} if order.remote_order_id else {"cliOrdId": self.client_order_id(order_id)}
        payload = self._private("POST", "cancelorder", params)
        status = str((payload.get("cancelStatus") or {}).get("status", ""))
        if status == "cancelled":
            order.status = "CANCELED"
            order.message = "cancelled on Kraken Futures"
        return ExecutionReport(order_id=order_id, status=order.status, message=f"cancel result: {status or 'unknown'}")

    def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel every open order on this contract (used by the kill switch)."""
        payload = self._private("POST", "cancelallorders", {"symbol": self.contract.venue_symbol})
        for order in self._orders.values():
            if order.status == "SUBMITTED":
                order.message = (order.message or "") + "; cancel-all requested"
        return dict(payload.get("cancelStatus") or {})

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """The locally known state of an order (read-only; `recover_execution_state` updates it)."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(order_id=order_id, status=order.status, fill_price=order.fill_price, filled_size=order.filled_size, fee=order.fee, message=order.message)

    def recover_execution_state(self, *, remote_snapshot: dict[str, Any] | None = None, remote_orders: Any = None) -> dict[str, Any]:
        """Settle pending orders from Kraken's fills and open orders, then re-read position and margin."""
        pending = [order for order in self._orders.values() if order.status == "SUBMITTED"]
        recovered: list[str] = []
        if pending:
            fills = list(self._private("GET", "fills").get("fills", []))
            open_ids: set[str] = set()
            for item in self._private("GET", "openorders").get("openOrders", []):
                open_ids.update(str(item[key]) for key in ("cliOrdId", "order_id") if item.get(key))
            for order in pending:
                cli_ord_id = self.client_order_id(order.order_id)
                own_fills = [
                    fill for fill in fills
                    if fill.get("cliOrdId") == cli_ord_id or (order.remote_order_id and fill.get("order_id") == order.remote_order_id)
                ]
                filled = sum(float(fill["size"]) for fill in own_fills)
                still_open = cli_ord_id in open_ids or (order.remote_order_id in open_ids if order.remote_order_id else False)
                if filled <= 0.0:
                    if not still_open and order.remote_order_id is not None:
                        order.status = "CANCELED"
                        order.message = "IOC order expired without a fill"
                        recovered.append(order.order_id)
                    continue
                vwap = sum(float(fill["size"]) * float(fill["price"]) for fill in own_fills) / filled
                fee = filled * vwap * self.contract.taker_fee_rate
                status = "PARTIALLY_FILLED" if still_open else "FILLED"
                self.reconcile_order_state(order_id=order.order_id, remote_status=status, remote_filled_size=filled, remote_fill_price=vwap, remote_fee=fee)
                order.message = f"filled on Kraken Futures ({len(own_fills)} fill(s)); fee estimated from taker rate"
                if any(str(fill.get("fillType", "")).endswith("iquidation") for fill in own_fills):
                    order.message += "; includes liquidation fills"
                recovered.append(order.order_id)
        sync = self.sync_account(force=True)
        return {"recovered_order_ids": recovered, "recovered_order_count": len(recovered), "recovery_status": "reconciled" if recovered else "idle", "account": sync}

    # ---- account ----------------------------------------------------------

    def sync_account(self, *, force: bool = False) -> dict[str, Any]:
        """Adopt Kraken's view of the position and margin; returns what changed versus the local view."""
        now = time.monotonic()
        if not force and self._last_sync_monotonic is not None and now - self._last_sync_monotonic < self._min_sync_seconds:
            return {"synced": False}
        self._last_sync_monotonic = now

        accounts = self._private("GET", "accounts").get("accounts", {})
        flex = accounts.get("flex") or {}
        positions = self._private("GET", "openpositions").get("openPositions", [])
        mine = next((position for position in positions if position.get("symbol") == self.contract.venue_symbol), None)

        local_size = self.position_size()
        remote_size = 0.0
        if mine is not None:
            remote_size = float(mine["size"]) * (1.0 if mine.get("side") == "long" else -1.0)
        key = self.position_symbol
        if remote_size == 0.0:
            for store in (self._positions, self._position_entry_price, self._position_opened_at):
                store.pop(key, None)
        else:
            if (local_size > 0.0) != (remote_size > 0.0) or local_size == 0.0:
                self._position_opened_at[key] = datetime.now(timezone.utc)
            self._positions[key] = remote_size
            self._position_entry_price[key] = float(mine["price"])
            self.unrealized_funding = float(mine.get("unrealizedFunding") or 0.0)

        self.margin_equity = float(flex.get("marginEquity", 0.0))
        self.available_margin = float(flex.get("availableMargin", 0.0))
        total_unrealized = float(flex.get("totalUnrealized", 0.0))
        # Wallet in the MarginAccountAdapter sense: equity without unrealized PnL.
        self._balances[self._base_currency] = self.margin_equity - total_unrealized
        self.reconcile_account_state(balances={self._base_currency: self.margin_equity}, positions={key: remote_size} if remote_size else {})
        return {"synced": True, "local_size": local_size, "remote_size": remote_size, "position_changed_externally": abs(local_size - remote_size) > self.contract.size_step / 2.0}

    def fetch_balance_snapshot(self) -> dict[str, Any]:
        """Balances and positions for runtime startup: equity is Kraken's `marginEquity` in the contract currency."""
        self.sync_account(force=True)
        return {
            "balances": {self._base_currency: float(self.margin_equity or 0.0)},
            "positions": {self.position_symbol: self.position_size()} if self.position_size() else {},
        }

    def buying_power(self, mark_price: float | None = None) -> float:
        """The account's own leverage cap, further limited by the margin Kraken reports as available."""
        own_cap = super().buying_power(mark_price)
        if self.available_margin is None:
            return own_cap
        return min(own_cap, max(0.0, self.available_margin) * self.contract.max_leverage)

    def on_market_update(self, *, symbol: str | None, mark_price: float, timestamp: datetime) -> list[dict[str, Any]]:
        """Record the mark and resync the account; report a position that changed without our orders (e.g. liquidation)."""
        if mark_price <= 0.0 or (symbol is not None and self._position_symbol(symbol) != self.position_symbol):
            return []
        self._mark_price = mark_price
        if any(order.status == "SUBMITTED" for order in self._orders.values()):
            return []  # recover_execution_state will sync once our own fills are accounted for
        before = self.position_size()
        result = self.sync_account()
        if result.get("synced") and result.get("position_changed_externally"):
            kind = "liquidation" if before != 0.0 and self.position_size() == 0.0 else "external_position_change"
            return [{"type": kind, "size_before": before, "size_after": self.position_size(), "mark_price": mark_price}]
        return []

    def _format_size(self, size: float) -> str:
        decimals = max(0, round(-math.log10(self.contract.size_step)))
        return f"{size:.{decimals}f}"
