"""Real orders on several Kraken Futures perpetuals in one multi-collateral account (live portfolio trading).

The single-contract `KrakenFuturesExecutionAdapter` trades one perp. A
portfolio holds several perps that share one margin account, so this
adapter keeps positions per contract and reads margin for the whole
account. It follows the same rules as the single-contract adapter:

- Every order is an immediate-or-cancel market order (`orderType=mkt`,
  capped by Kraken at 1% price protection). Closing and reducing orders are
  sent `reduceOnly=true`, so a stale local view can never turn a close into
  a new position.
- **Client order ids are deterministic:** `<prefix>-<order id>`, where the
  prefix is fixed per portfolio book and the order id comes from the
  portfolio engine's checkpointed cycle counter. After a crash between
  sending and hearing back, the engine re-registers its pending orders
  (`track_order`) and `settle_orders` finds their fills on Kraken by
  client id. Nothing is re-sent and nothing is guessed.
- Fills are read from `/fills` (Kraken's order-status endpoint forgets
  finished orders after 5 seconds). Positions and margin are read from
  `/openpositions` and `/accounts`, which are the source of truth.
- Kraken's fills carry no fee, so fees are estimated from each contract's
  taker rate and flagged as estimates.

Endpoints and fields follow Kraken's Derivatives REST spec; the tests use
a fake that replays those shapes. Verify credentials and permissions with
`python main.py --futures-verify-credentials` before any live run.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from src.execution.adapters import ExecutionAdapter, ExecutionOrder, ExecutionReport
from src.execution.kraken_futures_adapter import KrakenFuturesPrivateClient, Transport
from src.execution.perps import PerpContract

_ACCEPTED_SEND_STATUSES = {"placed", "partiallyFilled", "filled"}
LOST_ORDER_SETTLE_ATTEMPTS = 3  # an IOC order with no fill after this many checks...
MIN_UNFILLED_AGE_SECONDS = 15.0  # ...and at least this old never filled (/fills can lag the order by a few seconds)


class KrakenFuturesCrossMarginAdapter(ExecutionAdapter):
    """Several Kraken Futures perps in one account: IOC market orders, fills by client id, account-wide margin."""

    name = "kraken_futures_cross"
    margin_account = True
    live = True

    def __init__(
        self,
        *,
        contracts: Iterable[PerpContract],
        api_key: str,
        api_secret: str,
        max_leverage: float = 2.0,
        client_id_prefix: str = "cqm",
        transport: Transport | None = None,
        exchange_name: str = "kraken_futures",
    ) -> None:
        """Trade `contracts` (each `verified`, i.e. built from Kraken's public instrument data).

        Args:
            max_leverage: The account-side cap on new exposure (Kraken's own
                limits still apply on top).
            client_id_prefix: Fixed per portfolio book, so client order ids
                survive restarts (see the module docstring).
        """
        super().__init__()
        self.contracts: dict[str, PerpContract] = {contract.symbol: contract for contract in contracts}
        if not self.contracts:
            raise ValueError("the account needs at least one contract")
        unverified = [symbol for symbol, contract in self.contracts.items() if not contract.verified]
        if unverified:
            raise ValueError(f"refusing unverified contracts for real trading: {unverified}; build them with perp_contract_from_instrument()")
        currencies = {contract.collateral_currency for contract in self.contracts.values()}
        if len(currencies) != 1:
            raise ValueError(f"one account settles in one currency, got {sorted(currencies)}")
        if max_leverage < 1.0:
            raise ValueError("max_leverage must be at least 1")
        self.by_venue_symbol = {contract.venue_symbol: symbol for symbol, contract in self.contracts.items()}
        self._client = KrakenFuturesPrivateClient(api_key=api_key, api_secret=api_secret, transport=transport)
        self.exchange_name = exchange_name
        self.max_leverage = max_leverage
        self.client_id_prefix = client_id_prefix
        self._base_currency = currencies.pop()
        self._marks: dict[str, float] = {}
        self._filled_so_far: dict[str, float] = {}
        self._settle_attempts: dict[str, int] = {}
        self._submitted_monotonic: dict[str, float] = {}
        self.min_unfilled_age_seconds = MIN_UNFILLED_AGE_SECONDS
        self.margin_equity: float | None = None
        self.available_margin: float | None = None
        self.total_unrealized: float = 0.0
        self.unrealized_funding: dict[str, float] = {}
        self.foreign_positions: dict[str, float] = {}  # positions in contracts this portfolio doesn't trade
        self._last_synced_positions: dict[str, float] | None = None

    # --- views -----------------------------------------------------------------------------------------------------

    def client_order_id(self, order_id: str) -> str:
        """The client id sent for a local order id; the same after a restart."""
        return f"{self.client_id_prefix}-{order_id}"[:100]

    def position_size(self, symbol: str) -> float:
        """Signed position in `symbol` as last read from Kraken, plus our fills since."""
        return self._positions.get(symbol, 0.0)

    def entry_price(self, symbol: str) -> float | None:
        """Kraken's average entry price for the open position in `symbol`."""
        return self._position_entry_price.get(symbol)

    def positions(self) -> dict[str, float]:
        """Open positions by runtime symbol."""
        return {symbol: size for symbol, size in self._positions.items() if size}

    def pending_orders(self) -> list[ExecutionOrder]:
        """Orders sent (or maybe sent) whose outcome isn't settled yet."""
        return [order for order in self._orders.values() if order.status in {"SUBMITTED", "PARTIALLY_FILLED"}]

    def equity(self) -> float | None:
        """Kraken's margin equity for the whole account (collateral plus unrealized PnL), as of the last sync."""
        return self.margin_equity

    def get_account_snapshot(self) -> dict[str, Any]:
        """Balances and positions as the base adapter reports them, plus Kraken's margin figures."""
        snapshot = super().get_account_snapshot()
        snapshot.update({"account_type": "cross_margin", "equity": self.margin_equity, "available_margin": self.available_margin,
                         "total_unrealized": self.total_unrealized, "unrealized_funding": dict(self.unrealized_funding),
                         "foreign_positions": dict(self.foreign_positions), "pending_orders": [order.order_id for order in self.pending_orders()]})
        return snapshot

    def _format_size(self, symbol: str, size: float) -> str:
        decimals = max(0, round(-math.log10(self.contracts[symbol].size_step)))
        return f"{size:.{decimals}f}"

    def _reject(self, order_id: str, message: str) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="REJECTED", message=message)

    # --- orders --------------------------------------------------------------------------------------------------

    def submit_order(
        self,
        *,
        order_id: str,
        side: str,
        size: float,
        price: float,
        timestamp: datetime,
        symbol: str | None = None,
        reduce_only: bool = False,
    ) -> ExecutionReport:
        """Send an IOC market order after local checks. Returns SUBMITTED (settle it with `settle_orders`) or REJECTED."""
        side = side.lower()
        if symbol not in self.contracts:
            return self._reject(order_id, f"this account trades {sorted(self.contracts)}, not {symbol}")
        if side not in {"buy", "sell"} or size <= 0.0 or price <= 0.0:
            return self._reject(order_id, "needs side buy/sell and a positive size and price")
        contract = self.contracts[symbol]
        rounded = math.floor(size / contract.size_step + 1e-9) * contract.size_step
        if rounded < contract.min_size:
            return self._reject(order_id, f"size {size} is below the contract minimum {contract.min_size}")
        current = self.position_size(symbol)
        after = current + rounded if side == "buy" else current - rounded
        if abs(after) < contract.size_step / 2.0:
            after = 0.0
        grows = abs(after) > abs(current) + 1e-12 or (current != 0.0 and after != 0.0 and (after > 0) != (current > 0))
        if reduce_only and grows:
            return self._reject(order_id, f"reduce_only: {side} {rounded} would grow or flip the {symbol} position of {current}")
        if grows and self.available_margin is not None:
            needed = (abs(after) - (abs(current) if (after > 0) == (current > 0) else 0.0)) * price / min(self.max_leverage, contract.max_leverage)
            if needed > self.available_margin:
                return self._reject(order_id, f"insufficient margin: needs {needed:.2f} {self._base_currency}, Kraken reports {self.available_margin:.2f} available")
        self._marks[symbol] = price
        self._submitted_monotonic[order_id] = time.monotonic()
        cli_ord_id = self.client_order_id(order_id)
        order = ExecutionOrder(order_id=order_id, side=side, size=rounded, symbol=symbol, price=price, timestamp=timestamp, status="SUBMITTED", exchange=self.exchange_name)
        params = {"orderType": "mkt", "symbol": contract.venue_symbol, "side": side, "size": self._format_size(symbol, rounded),
                  "cliOrdId": cli_ord_id, "reduceOnly": "true" if reduce_only else None}
        try:
            payload = self._client.call("POST", "sendorder", params)
        except Exception as exc:  # noqa: BLE001 - the request may have reached Kraken: settle by client id, don't guess
            order.message = f"submission outcome unknown ({exc}); settling by cliOrdId {cli_ord_id}"
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

    def track_order(self, *, order_id: str, symbol: str, side: str, size: float, price: float, timestamp: datetime) -> None:
        """Re-register an order sent before a restart, so `settle_orders` looks for its fills."""
        if order_id not in self._orders:
            self._orders[order_id] = ExecutionOrder(order_id=order_id, side=side, size=size, symbol=symbol, price=price, timestamp=timestamp,
                                                    status="SUBMITTED", exchange=self.exchange_name, message="re-registered after a restart")
            self._submitted_monotonic[order_id] = time.monotonic()

    def settle_orders(self) -> list[dict[str, Any]]:
        """Look pending orders up in Kraken's fills and open orders; return what changed since the last call.

        Each item has `order_id`, `status` (FILLED, PARTIALLY_FILLED,
        CANCELED) and, when more was filled since the last call, `filled_size`,
        `fill_price` (the VWAP of the new fills) and an estimated `fee`.
        """
        pending = self.pending_orders()
        if not pending:
            return []
        fills = list(self._client.call("GET", "fills").get("fills", []))
        open_ids: set[str] = set()
        for item in self._client.call("GET", "openorders").get("openOrders", []):
            open_ids.update(str(item[key]) for key in ("cliOrdId", "order_id") if item.get(key))
        settled = []
        for order in pending:
            cli_ord_id = self.client_order_id(order.order_id)
            own = [fill for fill in fills if fill.get("cliOrdId") == cli_ord_id or (order.remote_order_id and fill.get("order_id") == order.remote_order_id)]
            total = sum(float(fill["size"]) for fill in own)
            previously = self._filled_so_far.get(order.order_id, 0.0)
            still_open = cli_ord_id in open_ids or bool(order.remote_order_id and order.remote_order_id in open_ids)
            item: dict[str, Any] = {"order_id": order.order_id, "symbol": order.symbol}
            if total > previously + 1e-12:
                value_total = sum(float(fill["size"]) * float(fill["price"]) for fill in own)
                value_before = previously * (order.fill_price or 0.0)
                new = total - previously
                price = (value_total - value_before) / new
                contract = self.contracts[str(order.symbol)]
                item.update({"filled_size": new, "fill_price": price, "fee": new * price * contract.taker_fee_rate, "fee_estimated": True,
                             "liquidation": any(str(fill.get("fillType", "")).lower().endswith("liquidation") for fill in own)})
                self._filled_so_far[order.order_id] = total
                order.filled_size, order.fill_price = total, value_total / total
                order.fee = total * order.fill_price * contract.taker_fee_rate
                signed = new if order.side == "buy" else -new
                self._positions[str(order.symbol)] = self._positions.get(str(order.symbol), 0.0) + signed
            if still_open:
                order.status = "PARTIALLY_FILLED" if total > 0 else "SUBMITTED"
            elif total > 0:
                order.status = "FILLED"
            else:
                # Not open and no fill yet. Kraken's /fills can lag a filled IOC order by seconds, so only conclude
                # "never filled" after several checks and a minimum age; concluding early would lose a real fill.
                attempts = self._settle_attempts[order.order_id] = self._settle_attempts.get(order.order_id, 0) + 1
                age = time.monotonic() - self._submitted_monotonic.get(order.order_id, time.monotonic())
                if attempts >= LOST_ORDER_SETTLE_ATTEMPTS and age >= self.min_unfilled_age_seconds:
                    order.status = "CANCELED"
                    order.message = "IOC order ended without a fill" if order.remote_order_id else "no fill found for a lost-response order"
            if order.status != "SUBMITTED" or "filled_size" in item:
                item["status"] = order.status
                settled.append(item)
        return settled

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Cancel a resting order (IOC orders rarely rest, but a lost response can leave one)."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        params = {"order_id": order.remote_order_id} if order.remote_order_id else {"cliOrdId": self.client_order_id(order_id)}
        status = str((self._client.call("POST", "cancelorder", params).get("cancelStatus") or {}).get("status", ""))
        return ExecutionReport(order_id=order_id, status=order.status, message=f"cancel result: {status or 'unknown'}")

    def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel every open order in the account (the kill switch's sweep)."""
        return dict(self._client.call("POST", "cancelallorders", {}).get("cancelStatus") or {})

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """The locally known state of an order (`settle_orders` updates it)."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(order_id=order_id, status=order.status, fill_price=order.fill_price, filled_size=order.filled_size, fee=order.fee, message=order.message)

    # --- account -------------------------------------------------------------------------------------------------

    def sync_account(self) -> dict[str, Any]:
        """Adopt Kraken's positions and margin; returns them and any change not explained by our own settled fills."""
        flex = (self._client.call("GET", "accounts").get("accounts") or {}).get("flex") or {}
        remote: dict[str, float] = {}
        entries: dict[str, float] = {}
        self.foreign_positions, self.unrealized_funding = {}, {}
        for position in self._client.call("GET", "openpositions").get("openPositions", []):
            size = float(position["size"]) * (1.0 if position.get("side") == "long" else -1.0)
            symbol = self.by_venue_symbol.get(str(position.get("symbol")))
            if symbol is None:
                self.foreign_positions[str(position.get("symbol"))] = size
                continue
            remote[symbol], entries[symbol] = size, float(position["price"])
            self.unrealized_funding[symbol] = float(position.get("unrealizedFunding") or 0.0)
        changed = {symbol: {"expected": self._positions.get(symbol, 0.0), "exchange": remote.get(symbol, 0.0)}
                   for symbol in self.contracts
                   if abs(self._positions.get(symbol, 0.0) - remote.get(symbol, 0.0)) > self.contracts[symbol].size_step / 2.0}
        self._positions, self._position_entry_price = dict(remote), dict(entries)
        self.margin_equity = float(flex.get("marginEquity", 0.0))
        self.available_margin = float(flex.get("availableMargin", 0.0))
        self.total_unrealized = float(flex.get("totalUnrealized", 0.0))
        self._balances = {self._base_currency: self.margin_equity - self.total_unrealized}
        first_sync = self._last_synced_positions is None
        self._last_synced_positions = dict(remote)
        return {"positions": dict(remote), "entries": entries, "equity": self.margin_equity, "available_margin": self.available_margin,
                "changed": {} if first_sync else changed, "foreign_positions": dict(self.foreign_positions)}

    def on_market_update(self, *, prices: Mapping[str, float], timestamp: datetime) -> list[dict[str, Any]]:
        """Record marks and resync; report positions that changed without our orders (liquidations, manual trades)."""
        for symbol, price in prices.items():
            if symbol in self.contracts and price and price > 0:
                self._marks[symbol] = float(price)
        if self.pending_orders():
            return []  # settle our own fills first, or they'd look like external changes
        result = self.sync_account()
        events = []
        for symbol, change in result["changed"].items():
            kind = "liquidation" if change["expected"] != 0.0 and change["exchange"] == 0.0 else "external_position_change"
            events.append({"type": kind, "symbol": symbol, **change, "timestamp": timestamp.isoformat()})
        return events
