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
- **Protective stops rest on Kraken** (`sync_protective_stops`): one reduce-only
  stop-market order per open position, triggered by the mark price, so a dead
  process or machine can't lose more than the stop distance. Their client ids
  carry the book's prefix plus `-stop-<contract>-`, which is how they are found
  again after a restart. A replacement is placed before the old stop is
  cancelled, so a position is never left unprotected in between. Their fills
  are read from `/fills` (`protective_stop_fills`) and booked by the engine.

Endpoints and fields follow Kraken's Derivatives REST spec; the tests use
a fake that replays those shapes. Verify credentials and permissions with
`python main.py --futures-verify-credentials` before any live run.
"""

from __future__ import annotations

import math
import time
from decimal import Decimal
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from src.execution.adapters import ExecutionAdapter, ExecutionOrder, ExecutionReport
from src.execution.kraken_futures_adapter import (
    _ACCEPTED_SEND_STATUSES,
    LOST_ORDER_SETTLE_ATTEMPTS,
    MIN_UNFILLED_AGE_SECONDS,
    KrakenFuturesPrivateClient,
    Transport,
)
from src.execution.perps import PerpContract

LATE_FILL_WATCH_SECONDS = 3600.0  # a written-off order is still watched this long: a fill that shows up late is booked
STOP_TAG = "stop"


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
        self._written_off: dict[str, float] = {}  # order id -> when it was written off (monotonic), still watched for late fills
        self.margin_equity: float | None = None
        self.available_margin: float | None = None
        self.total_unrealized: float = 0.0
        self.unrealized_funding: dict[str, float] = {}
        self.foreign_positions: dict[str, float] = {}  # positions in contracts this portfolio doesn't trade
        self._last_synced_positions: dict[str, float] | None = None
        self._stop_counter = 0
        self._stop_fills_seen: set[str] = set()  # stop fills already folded into the local positions (this process)

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
        now = time.monotonic()
        for order_id, since in list(self._written_off.items()):
            if now - since > LATE_FILL_WATCH_SECONDS:
                del self._written_off[order_id]
        pending = self.pending_orders()
        watched = [self._orders[order_id] for order_id in self._written_off if order_id in self._orders]
        if not pending and not watched:
            return []
        fills = list(self._client.call("GET", "fills").get("fills", []))
        open_ids: set[str] = set()
        for item in self._client.call("GET", "openorders").get("openOrders", []):
            open_ids.update(str(item[key]) for key in ("cliOrdId", "order_id") if item.get(key))
        settled = []
        for order in pending + watched:
            late = order.order_id in self._written_off
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
                if not late:  # a late fill is usually in Kraken's position already, adopted by the last account sync
                    signed = new if order.side == "buy" else -new
                    self._positions[str(order.symbol)] = self._positions.get(str(order.symbol), 0.0) + signed
            if late:
                # Written off as unfilled, but its fill has now appeared in /fills: settle it after all
                if "filled_size" in item:
                    del self._written_off[order.order_id]
                    order.status, order.message = "FILLED", "late fill: found in /fills after the order was written off"
                    item.update({"status": "FILLED", "late_fill": True})
                    settled.append(item)
                continue
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
                    self._written_off[order.order_id] = time.monotonic()
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

    # --- protective stops resting on the exchange ----------------------------------------------------------------

    def _stop_prefix(self, symbol: str | None = None) -> str:
        base = f"{self.client_id_prefix}-{STOP_TAG}-"
        return base if symbol is None else f"{base}{self.contracts[symbol].venue_symbol}-"

    def _format_price(self, symbol: str, price: float, *, side: str) -> str:
        """A stop price on the contract's tick grid, rounded away from the market (a sell stop down, a buy stop up)."""
        tick = self.contracts[symbol].tick_size or 0.01
        ticks = math.floor(price / tick + 1e-9) if side == "sell" else math.ceil(price / tick - 1e-9)
        decimals = max(0, -Decimal(str(tick)).normalize().as_tuple().exponent)  # 0.5 -> 1 decimal, 0.01 -> 2, 1 -> 0
        return f"{ticks * tick:.{decimals}f}"

    def protective_stops(self) -> dict[str, list[dict[str, Any]]]:
        """This book's resting stop orders per runtime symbol, read from Kraken's open orders."""
        out: dict[str, list[dict[str, Any]]] = {}
        for item in self._client.call("GET", "openorders").get("openOrders", []):
            client_id = str(item.get("cliOrdId") or "")
            for symbol in self.contracts:
                if client_id.startswith(self._stop_prefix(symbol)):
                    out.setdefault(symbol, []).append({
                        "cli_ord_id": client_id, "order_id": item.get("order_id"), "side": str(item.get("side", "")),
                        "size": float(item.get("unfilledSize") or item.get("size") or 0.0), "stop_price": float(item.get("stopPrice") or 0.0)})
        return out

    def sync_protective_stops(self, desired: Mapping[str, tuple[str, float, float] | None], *, now: datetime) -> list[dict[str, Any]]:
        """Make the resting stops match `desired`: symbol -> (side, size, stop price), or None for no stop.

        Symbols missing from `desired` are left as they are (e.g. while an order on them is pending). A stop already
        resting with the same side, size and price is kept. Otherwise the new stop is placed first and the old ones
        are cancelled only once it is accepted, so the position is never unprotected; if placing fails, the old stop
        stays. Returns one record per action: placed, kept, cancelled or failed.
        """
        existing = self.protective_stops()
        actions: list[dict[str, Any]] = []
        for symbol, want in desired.items():
            if symbol not in self.contracts:
                continue
            current = existing.get(symbol, [])
            if want is None:
                for order in current:
                    actions.append(self._cancel_stop(symbol, order, "position closed"))
                continue
            side, size, stop_price = want
            contract = self.contracts[symbol]
            rounded = math.floor(size / contract.size_step + 1e-9) * contract.size_step
            price_text = self._format_price(symbol, stop_price, side=side)
            same = [order for order in current if order["side"] == side and abs(order["size"] - rounded) < contract.size_step / 2.0
                    and abs(order["stop_price"] - float(price_text)) < (contract.tick_size or 0.01) / 2.0]
            if same:
                actions.append({"action": "kept", "symbol": symbol, "side": side, "size": rounded, "stop_price": float(price_text)})
                for order in current:
                    if order is not same[0]:
                        actions.append(self._cancel_stop(symbol, order, "duplicate"))
                continue
            if rounded < contract.min_size:
                actions.append({"action": "failed", "symbol": symbol, "message": f"size {size} is below the contract minimum"})
                continue
            self._stop_counter += 1
            client_id = f"{self._stop_prefix(symbol)}{int(now.timestamp())}-{self._stop_counter}"[:100]
            params = {"orderType": "stp", "symbol": contract.venue_symbol, "side": side, "size": self._format_size(symbol, rounded),
                      "stopPrice": price_text, "triggerSignal": "mark", "reduceOnly": "true", "cliOrdId": client_id}
            try:
                status = str((self._client.call("POST", "sendorder", params).get("sendStatus") or {}).get("status", ""))
            except Exception as exc:  # noqa: BLE001 - keep the old stop; the next sync retries (a duplicate is cancelled then)
                actions.append({"action": "failed", "symbol": symbol, "message": f"{type(exc).__name__}: {exc}"})
                continue
            if status != "placed":
                actions.append({"action": "failed", "symbol": symbol, "message": f"Kraken Futures did not place the stop: {status or 'no status'}"})
                continue
            actions.append({"action": "placed", "symbol": symbol, "side": side, "size": rounded, "stop_price": float(price_text), "cli_ord_id": client_id})
            for order in current:
                actions.append(self._cancel_stop(symbol, order, "replaced"))
        return actions

    def _cancel_stop(self, symbol: str, order: Mapping[str, Any], why: str) -> dict[str, Any]:
        params = {"order_id": order["order_id"]} if order.get("order_id") else {"cliOrdId": order["cli_ord_id"]}
        try:
            status = str((self._client.call("POST", "cancelorder", params).get("cancelStatus") or {}).get("status", ""))
        except Exception as exc:  # noqa: BLE001 - reported; the next sync tries again
            return {"action": "failed", "symbol": symbol, "message": f"cancel failed: {type(exc).__name__}: {exc}"}
        return {"action": "cancelled", "symbol": symbol, "why": why, "stop_price": order.get("stop_price"), "status": status}

    def protective_stop_fills(self) -> list[dict[str, Any]]:
        """Fills of this book's protective stops in Kraken's recent fills, oldest first, with an estimated fee.

        Each has `fill_id` (to book it once), `symbol`, `side`, `size`, `price`, `fee`. Fills this process hasn't
        seen are also added to the local positions (once the account has been synced), so the next sync doesn't
        mistake them for someone else's trade.
        """
        prefix = self._stop_prefix()
        out = []
        for fill in self._client.call("GET", "fills").get("fills", []):
            if not str(fill.get("cliOrdId") or "").startswith(prefix):
                continue
            symbol = self.by_venue_symbol.get(str(fill.get("symbol")))
            if symbol is None:
                continue
            size, price = float(fill["size"]), float(fill["price"])
            fill_id = str(fill.get("fill_id") or f"{fill.get('order_id')}:{size}:{price}")
            side = str(fill.get("side") or "")
            if fill_id not in self._stop_fills_seen:
                self._stop_fills_seen.add(fill_id)
                if self._last_synced_positions is not None:
                    self._positions[symbol] = self._positions.get(symbol, 0.0) + (size if side == "buy" else -size)
            out.append({"fill_id": fill_id, "symbol": symbol, "side": side, "size": size, "price": price,
                        "fee": size * price * self.contracts[symbol].taker_fee_rate, "fee_estimated": True, "time": fill.get("fillTime")})
        return out

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
