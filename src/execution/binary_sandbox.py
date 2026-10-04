"""A paper exchange for prediction-market contracts: orders fill against the venue's real order book.

One instance is one venue account (Kalshi or Polymarket). Prices are Yes prices between 0 and 1 and sizes are whole
contracts. Buying is buying Yes. Selling Yes that is held closes it; selling Yes that is not held is buying No at
1 minus the price, which the account holds as negative Yes units and for which it sets aside 1 per contract (the
bet's stake), as the venue does.

Two ways to trade, as on the perp exchange:

- **Taker (the default): fill or cancel at a limit.** The order walks the real book from the best price and takes
  what is offered at or inside its limit. What isn't there is cancelled; nothing is left resting. A partial fill is a
  fill for the size that was there.
- **Maker (`post_only`): rest at the limit.** Refused if it would trade at once. It fills, whole, at its limit and at
  the maker fee, only once the other side of the book has come to its price (the best offer at or below a resting
  bid, the best bid at or above a resting offer). That is pessimistic: a real resting order is also filled by trades
  that never move the best quote. `on_market_update` makes that check; `cancel_order` withdraws the order.

Fees are `rate x price x (1 - price)` per contract, rounded up to a cent per order where the venue does (Kalshi):

- Kalshi: taker 0.07, maker 0.0175, on every order.
- Polymarket (crypto markets): taker 0.07, maker 0, and no taker fee on selling a token that is held, so only the
  part of an order that opens a position pays. Its maker rebate is not modelled.

The maker rates and Polymarket's free selling come from a secondhand summary (2026-10-04), not from the venues' fee
pages: check them there before relying on a maker strategy's paper result.

Like the perp paper exchange it keeps a state file, answers a repeated order id with the first outcome, and can say
after a restart whether an order rested, filled, was cancelled, or never arrived (`track_order`, `settle_orders`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.execution.adapters import ExecutionAdapter, ExecutionReport

KEPT = 500  # finished order ids remembered, so a repeat or a restart gets the first outcome back
TAKER_FEE_RATE = {"kalshi": 0.07, "polymarket": 0.07}
MAKER_FEE_RATE = {"kalshi": 0.0175, "polymarket": 0.0}
FEE_ON_CLOSING = {"kalshi": True, "polymarket": False}  # Polymarket charges no taker fee for selling a held token
ROUND_FEE_UP_TO_CENT = {"kalshi": True, "polymarket": False}

Books = Callable[[str], pd.DataFrame | None]  # market id -> rows of side ("bid"/"ask"), price (Yes), size; best first


def binary_fee(price: float, contracts: float, rate: float, *, round_up: bool = False) -> float:
    """The fee of one order: rate x p x (1 - p) per contract, optionally rounded up to a whole cent."""
    fee = rate * price * (1.0 - price) * contracts
    return math.ceil(fee * 100.0 - 1e-9) / 100.0 if round_up and fee > 0 else fee


class SandboxBinaryAdapter(ExecutionAdapter):
    """One prediction-market account on paper: cash, Yes and No positions, fills from the real book."""

    def __init__(self, *, venue: str, books: Books, starting_cash: float = 0.0, taker_fee_rate: float | None = None, maker_fee_rate: float | None = None,
                 state_path: str | Path | None = None) -> None:
        """
        Args:
            venue: "kalshi" or "polymarket" (sets the fee rounding and the default taker rate).
            books: Returns a market's current order book, or None when it can't be read.
            starting_cash: Used only when `state_path` has no file yet.
        """
        super().__init__()
        self.exchange_name = venue
        self.books = books
        self.cash = float(starting_cash)
        self.taker_fee_rate = TAKER_FEE_RATE.get(venue, 0.07) if taker_fee_rate is None else taker_fee_rate
        self.maker_fee_rate = MAKER_FEE_RATE.get(venue, 0.0) if maker_fee_rate is None else maker_fee_rate
        self._fee_on_closing = FEE_ON_CLOSING.get(venue, True)
        self._round_up = ROUND_FEE_UP_TO_CENT.get(venue, False)
        self.units: dict[str, float] = {}  # market id -> signed Yes contracts (negative: No)
        self.fees_paid_total = 0.0
        self._fills: dict[str, dict[str, Any]] = {}
        self._resting: dict[str, dict[str, Any]] = {}
        self._cancelled: list[str] = []
        self._ended: list[str] = []
        self._tracked: set[str] = set()
        self.state_path = Path(state_path) if state_path is not None else None
        self.restored_from_state = self._load_state()

    # --- views ---------------------------------------------------------------------------------------------------

    def position_size(self, market: str) -> float:
        """Signed Yes contracts held in `market` (negative: No contracts)."""
        return self.units.get(market, 0.0)

    def stake(self) -> float:
        """Cash set aside for No positions: 1 per contract."""
        return sum(-units for units in self.units.values() if units < 0)

    def available_cash(self) -> float:
        """Cash that can pay for new contracts: what is not a No position's stake or reserved by a resting order."""
        reserved = sum(self._cost(order["side"], order["size"], order["limit_price"], self.position_size(order["market"])) for order in self._resting.values())
        return self.cash - self.stake() - reserved

    def resting_orders(self) -> dict[str, dict[str, Any]]:
        """Orders waiting at their limit, by order id."""
        return {order_id: dict(order) for order_id, order in self._resting.items()}

    def get_account_snapshot(self) -> dict[str, Any]:
        """Cash, positions and fees so far."""
        return {"exchange": self.exchange_name, "cash": self.cash, "stake": self.stake(), "available_cash": self.available_cash(),
                "positions": {market: units for market, units in self.units.items() if units}, "fees_paid_total": self.fees_paid_total}

    # --- orders --------------------------------------------------------------------------------------------------

    @staticmethod
    def _cost(side: str, size: float, price: float, held: float) -> float:
        """Cash an order needs beyond what it frees: Yes bought at its price, No (Yes sold beyond what is held) at 1 - price."""
        if side == "buy":
            covering = min(size, max(-held, 0.0))  # buying back a No position frees its stake
            return size * price - covering
        opening = max(size - max(held, 0.0), 0.0)  # selling more Yes than held opens No, which needs its stake
        return opening * 1.0 - size * price

    def _fee_contracts(self, side: str, size: float, held: float) -> float:
        """The contracts of an order that pay the taker fee: all of them, or where closing is free only the part that opens a position."""
        if self._fee_on_closing:
            return size
        closing = min(size, max(held, 0.0)) if side == "sell" else min(size, max(-held, 0.0))
        return size - closing

    def _book(self, market: str) -> pd.DataFrame | None:
        try:
            return self.books(market)
        except Exception:  # noqa: BLE001 - an unreadable book means no fill, never a crash
            return None

    def _reject(self, order_id: str, message: str) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="REJECTED", message=message)

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None, post_only: bool = False) -> ExecutionReport:
        """Trade `size` Yes contracts of market `symbol` at `price` or better. See the module docstring for the two styles."""
        side = side.lower()
        if order_id in self._resting:
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="already resting under this order id")
        if order_id in self._fills:
            fill = self._fills[order_id]
            return ExecutionReport(order_id=order_id, status="FILLED", fill_price=fill["fill_price"], filled_size=fill["filled_size"], fee=fill["fee"], message="already filled under this order id")
        size = math.floor(size + 1e-9)
        if not symbol or side not in {"buy", "sell"} or size < 1 or not 0.0 < price < 1.0:
            return self._reject(order_id, "needs a market, side buy/sell, at least one whole contract and a price between 0 and 1")
        book = self._book(symbol)
        if book is None or book.empty:
            return self._reject(order_id, f"no order book for {symbol}")
        against = book[book["side"] == ("ask" if side == "buy" else "bid")]
        inside = against[against["price"] <= price + 1e-12] if side == "buy" else against[against["price"] >= price - 1e-12]
        held = self.position_size(symbol)

        if post_only:
            if len(inside):
                return self._reject(order_id, f"post-only would trade at once: the best {'offer' if side == 'buy' else 'bid'} is {float(against['price'].iloc[0]):.2f}")
            if self._cost(side, size, price, held) > self.available_cash() + 1e-9:
                return self._reject(order_id, f"insufficient cash: {self.available_cash():.2f} available")
            self._resting[order_id] = {"market": symbol, "side": side, "size": float(size), "limit_price": float(price), "placed_at": timestamp.isoformat()}
            self.save_state()
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"resting at {price:.2f} on {self.exchange_name} (paper)")

        remaining, value, filled = float(size), 0.0, 0.0
        for level in inside.itertuples():
            take = math.floor(min(remaining, float(level.size)) + 1e-9)
            if take < 1:
                continue
            value += take * float(level.price)
            filled += take
            remaining -= take
            if remaining < 1:
                break
        if filled < 1:
            return self._reject(order_id, f"nothing offered at {price:.2f} or better")
        fill_price = value / filled
        fee = binary_fee(fill_price, self._fee_contracts(side, filled, held), self.taker_fee_rate, round_up=self._round_up)
        if self._cost(side, filled, fill_price, held) + fee > self.available_cash() + 1e-9:
            return self._reject(order_id, f"insufficient cash: needs {self._cost(side, filled, fill_price, held) + fee:.2f}, {self.available_cash():.2f} available")
        return self._fill(order_id, symbol, side, filled, fill_price, fee, "taker")

    def _fill(self, order_id: str, market: str, side: str, size: float, price: float, fee: float, liquidity: str) -> ExecutionReport:
        signed = size if side == "buy" else -size
        self.cash -= signed * price + fee
        self.units[market] = self.units.get(market, 0.0) + signed
        if abs(self.units[market]) < 1e-9:
            del self.units[market]
        self.fees_paid_total += fee
        self._fills[order_id] = {"symbol": market, "side": side, "filled_size": size, "fill_price": price, "fee": fee, "liquidity": liquidity}
        for stale in list(self._fills)[:-KEPT]:
            del self._fills[stale]
        self.save_state()
        return ExecutionReport(order_id=order_id, status="FILLED", fill_price=price, filled_size=size, fee=fee, message=f"filled on {self.exchange_name} (paper, {liquidity})")

    def on_market_update(self, *, timestamp: datetime) -> list[dict[str, Any]]:
        """Fill every resting order the other side of the book has come to (see the module docstring)."""
        for order_id, order in list(self._resting.items()):
            book = self._book(order["market"])
            if book is None or book.empty:
                continue
            other = book[book["side"] == ("ask" if order["side"] == "buy" else "bid")]
            if other.empty:
                continue
            best = float(other["price"].iloc[0])
            if not (best <= order["limit_price"] + 1e-12 if order["side"] == "buy" else best >= order["limit_price"] - 1e-12):
                continue
            del self._resting[order_id]
            self._ended.append(order_id)
            self._fill(order_id, order["market"], order["side"], order["size"], order["limit_price"],
                       binary_fee(order["limit_price"], order["size"], self.maker_fee_rate, round_up=self._round_up), "maker")
        return []

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Withdraw a resting order. A taker order is never resting, so there is nothing to cancel."""
        if order_id in self._resting:
            del self._resting[order_id]
            self._cancelled = (self._cancelled + [order_id])[-KEPT:]
            self._ended.append(order_id)
            self.save_state()
            return ExecutionReport(order_id=order_id, status="CANCELED", message="resting order cancelled")
        return ExecutionReport(order_id=order_id, status="FILLED" if order_id in self._fills else "NOT_FOUND", message="nothing resting under this order id")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """What is known about an order id."""
        if order_id in self._fills:
            fill = self._fills[order_id]
            return ExecutionReport(order_id=order_id, status="FILLED", fill_price=fill["fill_price"], filled_size=fill["filled_size"], fee=fill["fee"])
        return ExecutionReport(order_id=order_id, status="SUBMITTED" if order_id in self._resting else "CANCELED" if order_id in self._cancelled else "NOT_FOUND")

    def track_order(self, *, order_id: str, **_details: Any) -> None:
        """Ask about an order after a restart; `settle_orders` then says what became of it."""
        self._tracked.add(order_id)

    def settle_orders(self) -> list[dict[str, Any]]:
        """The outcome, once, of every order asked about after a restart and of every resting order that has ended."""
        settled = []
        for order_id in sorted(self._tracked | set(self._ended)):
            if order_id in self._resting:
                continue
            if order_id in self._fills:
                settled.append({"order_id": order_id, "status": "FILLED", **self._fills[order_id]})
            elif order_id in self._cancelled:
                settled.append({"order_id": order_id, "status": "CANCELED"})
            else:
                settled.append({"order_id": order_id, "status": "CANCELED", "never_received": True})
        self._tracked = {order_id for order_id in self._tracked if order_id in self._resting}
        self._ended.clear()
        return settled

    def resolve(self, market: str, yes: bool) -> float:
        """The market has resolved: pay out 1 per Yes contract if Yes won (a No position pays it), close the position, cancel what rests on it."""
        for order_id, order in list(self._resting.items()):
            if order["market"] == market:
                self.cancel_order(order_id=order_id)
        units = self.units.pop(market, 0.0)
        payout = units * (1.0 if yes else 0.0)
        self.cash += payout
        self.save_state()
        return payout

    # --- state ---------------------------------------------------------------------------------------------------

    def save_state(self) -> None:
        """Write the account to `state_path` atomically; no-op without a path."""
        if self.state_path is None:
            return
        payload = {"account": "binary", "venue": self.exchange_name, "cash": self.cash, "units": self.units, "fees_paid_total": self.fees_paid_total,
                   "fills": self._fills, "resting_orders": self._resting, "cancelled_orders": self._cancelled}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True))
        temporary.replace(self.state_path)

    def _load_state(self) -> bool:
        if self.state_path is None or not self.state_path.exists():
            return False
        payload = json.loads(self.state_path.read_text())
        if payload.get("account") != "binary" or payload.get("venue") != self.exchange_name:
            raise ValueError(f"{self.state_path} is not a {self.exchange_name} paper account; move it aside or use another path")
        self.cash = float(payload["cash"])
        self.units = {market: float(units) for market, units in payload.get("units", {}).items()}
        self.fees_paid_total = float(payload.get("fees_paid_total", 0.0))
        self._fills = {order_id: dict(fill) for order_id, fill in payload.get("fills", {}).items()}
        self._resting = {order_id: dict(order) for order_id, order in payload.get("resting_orders", {}).items()}
        self._cancelled = [str(order_id) for order_id in payload.get("cancelled_orders", [])]
        return True
