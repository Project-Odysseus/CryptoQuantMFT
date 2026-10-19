"""One execution interface for paper, a desk and brokers (plan 7.5).

A strategy's book ends as target weights. `plan_orders` turns them into whole-contract orders against what is held;
an `ExecutionAdapter` sends them and reports fills. Paper, a desk handover file, and a broker or FIX connection are
interchangeable behind this interface, so promoting a book from paper to live changes the adapter, not the strategy.

- `PaperAdapter`: fills at the decision price moved by the assumed cost (the same costs research charged).
- `OrderFileAdapter`: writes the orders as a CSV for a desk to execute (fills come back later, `record_fills`).
- A broker or FIX adapter implements `submit` the same way; none ships here (it depends on the firm's broker and is
  written against their API, behind this interface). The host's crypto runtime is one such adapter.

Quantities are `Decimal`: they leave the process.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class Order:
    instrument: str
    side: int  # +1 buy, -1 sell
    quantity: Decimal  # contracts, always positive
    decision_price: float
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    instrument: str
    side: int
    quantity: Decimal
    fill_price: float
    decision_price: float
    at: str


class ExecutionAdapter(Protocol):
    """Sends orders; returns the fills known now (a desk or broker may report more later)."""

    name: str

    def submit(self, orders: Sequence[Order]) -> list[Fill]: ...


def plan_orders(target_weights: Mapping[str, float], held_contracts: Mapping[str, Decimal | int], *, equity: float, prices: Mapping[str, float],
                multipliers: Mapping[str, float], fx: Mapping[str, float] | None = None, band: float = 0.02) -> list[Order]:
    """Whole-contract orders from held contracts to target weights (rounded toward zero; inside `band` nothing trades).

    `fx` converts one unit of each instrument's currency to the book's currency (1 when the same). Closing and flipping
    always trade; exits come before entries.
    """
    fx = fx or {}
    orders = []
    for instrument in sorted(set(target_weights) | set(held_contracts)):
        price, multiplier = float(prices[instrument]), float(multipliers[instrument])
        notional = price * multiplier * float(fx.get(instrument, 1.0))
        held = Decimal(held_contracts.get(instrument, 0))
        target = float(target_weights.get(instrument, 0.0))
        held_weight = float(held) * notional / equity
        same_side = (held_weight >= 0) == (target >= 0) and target != 0.0
        if same_side and abs(target - held_weight) < band:
            continue
        wanted = Decimal(target * equity / notional).quantize(Decimal(1), rounding=ROUND_DOWN)
        change = wanted - held
        if change == 0:
            continue
        reason = "close" if wanted == 0 else "flip" if (held != 0 and (held > 0) != (wanted > 0)) else "reduce" if abs(wanted) < abs(held) else "increase"
        orders.append(Order(instrument, 1 if change > 0 else -1, abs(change), price, reason))
    return sorted(orders, key=lambda order: order.reason not in ("close", "reduce", "flip"))


class PaperAdapter:
    """Simulated fills at the decision price plus the assumed cost (basis points per side)."""

    name = "paper"

    def __init__(self, cost_bps: Mapping[str, float] | float = 1.0) -> None:
        self.cost_bps = cost_bps
        self.fills: list[Fill] = []

    def submit(self, orders: Sequence[Order]) -> list[Fill]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for order in orders:
            bps = self.cost_bps.get(order.instrument, 0.0) if isinstance(self.cost_bps, Mapping) else float(self.cost_bps)
            price = order.decision_price * (1.0 + order.side * bps / 1e4)
            out.append(Fill(order.instrument, order.side, order.quantity, price, order.decision_price, now))
        self.fills.extend(out)
        return out


class OrderFileAdapter:
    """Writes orders to a CSV for a desk (one file per submission); fills are recorded when the desk reports them."""

    name = "order_file"

    def __init__(self, folder: str | Path, *, book: str) -> None:
        self.folder, self.book = Path(folder), book

    def submit(self, orders: Sequence[Order]) -> list[Fill]:
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.folder / f"{self.book}_{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["book", "instrument", "side", "quantity", "decision_price", "reason"])
            writer.writeheader()
            for order in orders:
                writer.writerow({"book": self.book, **{key: value for key, value in asdict(order).items()}, "side": "BUY" if order.side > 0 else "SELL",
                                 "quantity": str(order.quantity)})
        self.last_file = path
        return []
