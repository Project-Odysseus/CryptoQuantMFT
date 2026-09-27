"""Deribit options trading adapter: limit orders with labels, fills settled from Deribit, cancels and positions.

Testnet first. The adapter refuses a live (www.deribit.com) client unless `allow_live=True`, which only the live
gates will pass, after testnet paper trading. Nothing here is wired to a command yet: no order is sent until the
testnet step is explicitly started.

How it mirrors the Kraken Futures adapters:

- **Labels are client order ids** (`cqm-<prefix>-<order id>`, at most 64 characters), so an order whose response
  was lost is found by its label (`private/get_order_state_by_label`) instead of being sent again. Order
  placement is never retried; reads and cancels are.
- **Fills are settled from the exchange** (`settle_orders`): the order's filled amount and average price, and the
  fees from its trades (`private/get_user_trades_by_order`), reported as the change since the last check.
- **Limit orders by default**, optionally post-only: option spreads are wide, and market orders would pay them.

Amounts are in the underlying (BTC for BTC options; USDC-settled BTC options trade in steps of 0.01 BTC); prices
are in the settlement currency (USDC for linear options).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.execution.adapters import ExecutionReport
from src.execution.deribit_client import DeribitClient
from src.options.deribit import underlying_and_settlement

LOST_ORDER_CHECKS = 3  # an order with no trace on Deribit after this many checks...
MIN_UNKNOWN_AGE_SECONDS = 15.0  # ...and at least this old was never placed
FINAL_STATES = {"filled", "cancelled", "rejected"}


@dataclass(slots=True)
class DeribitOrder:
    """What the adapter knows about one of its orders."""

    order_id: str
    label: str
    instrument: str
    side: str
    amount: float
    price: float | None
    status: str = "SUBMITTED"  # SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED
    exchange_order_id: str | None = None
    filled: float = 0.0
    average_price: float = 0.0
    fees: float = 0.0
    message: str = ""
    submitted_monotonic: float = field(default_factory=time.monotonic)
    checks: int = 0


def settlement_currency(instrument: str) -> str:
    """The currency an instrument settles in, which Deribit's label lookups need: "BTC_USDC-..." -> "USDC", "BTC-..." -> "BTC"."""
    currency = instrument.split("-", 1)[0]
    base, settlement = underlying_and_settlement(currency)
    return currency.split("_", 1)[1] if settlement == "linear" else base


class DeribitOptionsAdapter:
    """Places and tracks option orders on one Deribit account (testnet unless `allow_live`)."""

    name = "deribit"

    def __init__(self, client: DeribitClient, *, client_id_prefix: str = "cqm", allow_live: bool = False) -> None:
        """Refuses a live client unless `allow_live`; the prefix makes labels unique per portfolio book."""
        if not client.testnet and not allow_live:
            raise ValueError("refusing a live Deribit trading adapter: use testnet keys (DERIBIT_TESTNET=true) until the live gates allow it")
        self.client = client
        self.client_id_prefix = client_id_prefix
        self.orders: dict[str, DeribitOrder] = {}
        self.min_unknown_age_seconds = MIN_UNKNOWN_AGE_SECONDS

    @property
    def testnet(self) -> bool:
        """Whether this adapter trades Deribit's testnet (fake money)."""
        return self.client.testnet

    def label(self, order_id: str) -> str:
        """The label sent with an order: unique per book and order, at most 64 characters (Deribit's limit)."""
        return f"{self.client_id_prefix}-{order_id}"[:64]

    # --- orders --------------------------------------------------------------------------------------------------

    def submit_order(self, *, order_id: str, instrument: str, side: str, amount: float, price: float | None = None, order_type: str = "limit",
                     post_only: bool = False, reduce_only: bool = False, time_in_force: str = "good_til_cancelled") -> ExecutionReport:
        """Send one order; returns SUBMITTED (settle it with `settle_orders`), FILLED/PARTIALLY_FILLED if Deribit says so at once, or REJECTED."""
        if side not in ("buy", "sell"):
            return ExecutionReport(order_id=order_id, status="REJECTED", message=f"side must be buy or sell, not {side!r}")
        if not amount > 0:
            return ExecutionReport(order_id=order_id, status="REJECTED", message="amount must be positive")
        if order_type == "limit" and not (price and price > 0):
            return ExecutionReport(order_id=order_id, status="REJECTED", message="a limit order needs a positive price")
        order = DeribitOrder(order_id=order_id, label=self.label(order_id), instrument=instrument, side=side, amount=amount, price=price)
        params: dict[str, Any] = {"instrument_name": instrument, "amount": amount, "type": order_type, "label": order.label, "time_in_force": time_in_force}
        if order_type == "limit":
            params["price"] = price
        if post_only:
            params["post_only"] = "true"
        if reduce_only:
            params["reduce_only"] = "true"
        try:
            result = self.client.private(f"private/{side}", params, retry=False)
        except RuntimeError as exc:  # Deribit answered with an error: the order was not placed
            order.status, order.message = "REJECTED", str(exc)
            self.orders[order_id] = order
            return ExecutionReport(order_id=order_id, status="REJECTED", message=order.message)
        except Exception as exc:  # noqa: BLE001 - a lost response: the order may or may not be live
            order.message = f"outcome unknown ({exc}); will look it up by label {order.label}"
            self.orders[order_id] = order
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=order.message)
        self.orders[order_id] = order
        state = (result or {}).get("order") or {}
        order.exchange_order_id = state.get("order_id")
        self._apply_state(order, state)
        order.message = f"sent to Deribit{' testnet' if self.testnet else ''} ({state.get('order_state', 'unknown')}), label {order.label}"
        return ExecutionReport(order_id=order_id, status="SUBMITTED" if order.status in {"SUBMITTED", "PARTIALLY_FILLED"} else order.status, message=order.message)

    def _apply_state(self, order: DeribitOrder, state: dict[str, Any]) -> None:
        """Update the order's status from a Deribit order state (fills and fees are booked by `settle_orders`)."""
        deribit_state = state.get("order_state")
        if deribit_state == "rejected":
            order.status = "REJECTED"
        elif deribit_state == "cancelled":
            order.status = "CANCELED"
        elif deribit_state == "filled":
            order.status = "FILLED"
        elif deribit_state in ("open", "untriggered"):
            order.status = "PARTIALLY_FILLED" if float(state.get("filled_amount") or 0.0) > 0 else "SUBMITTED"

    def pending(self) -> list[DeribitOrder]:
        """Orders still open, or reported filled before their fills (price, fees) were booked."""
        return [order for order in self.orders.values()
                if order.status in {"SUBMITTED", "PARTIALLY_FILLED"} or (order.status == "FILLED" and order.filled < order.amount - 1e-12)]

    def settle_orders(self) -> list[dict[str, Any]]:
        """Look pending orders up by label; return what changed: new fills (amount, average price, fees) and final states."""
        settled = []
        for order in self.pending():
            order.checks += 1
            states = self.client.private("private/get_order_state_by_label", {"currency": settlement_currency(order.instrument), "label": order.label}) or []
            if not states:
                age = time.monotonic() - order.submitted_monotonic
                if order.checks >= LOST_ORDER_CHECKS and age >= self.min_unknown_age_seconds:
                    order.status, order.message = "CANCELED", "no order with this label on Deribit: it was never placed"
                    settled.append({"order_id": order.order_id, "status": order.status})
                continue
            state = states[0]
            order.exchange_order_id = order.exchange_order_id or state.get("order_id")
            filled = float(state.get("filled_amount") or 0.0)
            item: dict[str, Any] = {"order_id": order.order_id, "instrument": order.instrument, "side": order.side}
            if filled > order.filled + 1e-12:
                trades = self.client.private("private/get_user_trades_by_order", {"order_id": order.exchange_order_id}) or []
                fees = sum(float(trade.get("fee") or 0.0) for trade in trades)
                value = sum(float(trade["amount"]) * float(trade["price"]) for trade in trades)
                amount = sum(float(trade["amount"]) for trade in trades)
                new = filled - order.filled
                price_now = value / amount if amount else float(state.get("average_price") or 0.0)
                new_value = price_now * filled - order.average_price * order.filled
                item.update({"filled_amount": new, "fill_price": new_value / new, "fee": fees - order.fees})
                order.filled, order.average_price, order.fees = filled, price_now, fees
            self._apply_state(order, state)
            if order.status != "SUBMITTED" or "filled_amount" in item:
                item["status"] = order.status
                settled.append(item)
        return settled

    def cancel_order(self, order_id: str) -> ExecutionReport:
        """Cancel one order by its label (safe to repeat)."""
        order = self.orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        if order.status in {"FILLED", "CANCELED", "REJECTED"}:
            return ExecutionReport(order_id=order_id, status=order.status, message="order already final")
        self.client.private("private/cancel_by_label", {"label": order.label, "currency": settlement_currency(order.instrument)})
        order.status = "CANCELED"
        return ExecutionReport(order_id=order_id, status="CANCELED", message="cancelled on Deribit")

    def cancel_all(self) -> int:
        """Cancel every open order on the account (the kill switch's sweep); returns how many Deribit cancelled."""
        return int(self.client.private("private/cancel_all") or 0)

    # --- account -------------------------------------------------------------------------------------------------

    def positions(self, currency: str = "USDC") -> dict[str, float]:
        """Signed option and future positions settled in `currency`, by Deribit instrument name."""
        return {str(row["instrument_name"]): float(row.get("size") or 0.0) for row in self.client.positions(currency) if float(row.get("size") or 0.0) != 0.0}

    def equity(self, currency: str = "USDC") -> float:
        """The account's equity in `currency`."""
        return float(self.client.account_summary(currency).get("equity", 0.0))
