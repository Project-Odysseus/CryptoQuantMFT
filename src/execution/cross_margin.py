"""A sandbox cross-margin account for several linear perpetuals at once (portfolio paper trading).

`SandboxPerpExecutionAdapter` holds one contract. A portfolio holds BTC and
ETH perps (and more) on the same venue, and on Kraken Futures they share one
multi-collateral account: every position draws on the same wallet, and one
margin check covers all of them. This adapter simulates that account and
never touches a real exchange.

Account model (collateral in one currency):

    wallet      = deposited collateral + realized PnL - fees - funding
    unrealized  = sum over positions of size * (mark - entry)       (size is signed)
    equity      = wallet + unrealized
    initial     = sum of |size| * mark / leverage cap               (needed to open or increase)
    maintenance = sum of |size| * mark * maintenance rate (tiered)
    liquidation: when equity <= maintenance, every position is closed at the mark

A real venue liquidates step by step, starting with the largest position.
Closing everything at once is the conservative simplification.

Orders fill immediately at the submitted price, moved against the order by
`slippage_bps`, and pay the contract's taker fee. A `reduce_only` order is
rejected if it would grow or flip a position. Figures are floats like the
other execution adapters; the portfolio book keeps its own Decimal records.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from src.execution.adapters import ExecutionAdapter, ExecutionOrder, ExecutionReport
from src.execution.perps import PerpContract, initial_margin, maintenance_margin, unrealized_pnl

_EPSILON = 1e-12


class SandboxCrossMarginPerpAdapter(ExecutionAdapter):
    """Several perpetual contracts in one cross-margin sandbox account."""

    name = "sandbox_perp_cross"
    margin_account = True

    def __init__(
        self,
        *,
        contracts: Iterable[PerpContract],
        starting_collateral: float = 1000.0,
        max_leverage: float = 2.0,
        funding_pct_per_day: float | Mapping[str, float] = 0.01,
        slippage_bps: float | Mapping[str, float] = 0.0,
        exchange_name: str = "kraken_futures",
        state_path: str | Path | None = None,
    ) -> None:
        """Open an account holding `starting_collateral` for `contracts` (keyed by their runtime symbol).

        Args:
            max_leverage: The account's cap on new exposure. Each contract's own
                venue cap applies when it is lower.
            funding_pct_per_day: % of notional per day that longs pay and shorts
                receive, one number or one per symbol.
            slippage_bps: How far fills land from the submitted price, against
                the order, one number or one per symbol.
            state_path: JSON file saved after every change and restored at
                startup, so a restarted paper run keeps its wallet and positions.
                `starting_collateral` applies only when the file doesn't exist.
        """
        super().__init__()
        self.contracts: dict[str, PerpContract] = {contract.symbol: contract for contract in contracts}
        if not self.contracts:
            raise ValueError("the account needs at least one contract")
        currencies = {contract.collateral_currency for contract in self.contracts.values()}
        if len(currencies) != 1:
            raise ValueError(f"one account holds one collateral currency, got {sorted(currencies)}")
        bases = [contract.base_asset for contract in self.contracts.values()]
        if len(set(bases)) != len(bases):
            raise ValueError("two contracts share a base asset; positions are keyed by it")
        if max_leverage < 1.0:
            raise ValueError("max_leverage must be at least 1")
        if starting_collateral < 0.0:
            raise ValueError("starting_collateral cannot be negative")
        self.exchange_name = exchange_name
        self.max_leverage = max_leverage
        self._base_currency = currencies.pop()
        self._balances = {self._base_currency: float(starting_collateral)}
        self._remote_balances = dict(self._balances)
        self._funding = funding_pct_per_day
        self._slippage = slippage_bps
        self._marks: dict[str, float] = {}
        self._last_market_update: datetime | None = None
        self._liquidation_count = 0
        self.funding_paid_total = 0.0
        self.realized_pnl_total = 0.0
        self.fees_paid_total = 0.0
        self.state_path = Path(state_path) if state_path is not None else None
        self.restored_from_state = self._load_state()

    # --- account views -------------------------------------------------------------------------------------------

    def leverage_cap(self, symbol: str) -> float:
        """The leverage allowed on new exposure in `symbol`: the account cap or the contract's, whichever is lower."""
        return min(self.max_leverage, self.contracts[symbol].max_leverage)

    def position_size(self, symbol: str) -> float:
        """Signed position in `symbol`, in base units (0.0 when flat)."""
        return self._positions.get(self.contracts[symbol].base_asset, 0.0)

    def entry_price(self, symbol: str) -> float | None:
        """Average entry of the open position in `symbol`."""
        return self._position_entry_price.get(self.contracts[symbol].base_asset)

    def positions(self) -> dict[str, float]:
        """Open positions by runtime symbol."""
        return {symbol: self.position_size(symbol) for symbol in self.contracts if self.position_size(symbol) != 0.0}

    def marks(self) -> dict[str, float]:
        """The latest mark price per symbol."""
        return dict(self._marks)

    def wallet_balance(self) -> float:
        """Collateral after realized PnL, fees and funding (no unrealized PnL)."""
        return self._balances.get(self._base_currency, 0.0)

    def _mark(self, symbol: str, marks: Mapping[str, float] | None) -> float | None:
        if marks is not None and symbol in marks:
            return float(marks[symbol])
        return self._marks.get(symbol) or self.entry_price(symbol)

    def equity(self, marks: Mapping[str, float] | None = None) -> float:
        """Wallet plus the unrealized PnL of every position (at `marks`, else the latest marks)."""
        total = self.wallet_balance()
        for symbol, size in self.positions().items():
            mark, entry = self._mark(symbol, marks), self.entry_price(symbol)
            if mark is not None and entry is not None:
                total += unrealized_pnl(size, entry, mark)
        return total

    def used_initial_margin(self, marks: Mapping[str, float] | None = None) -> float:
        """Initial margin locked by all positions at their leverage caps."""
        return sum(initial_margin(size, self._mark(symbol, marks) or 0.0, self.leverage_cap(symbol)) for symbol, size in self.positions().items())

    def maintenance_requirement(self, marks: Mapping[str, float] | None = None) -> float:
        """Equity below which the account is liquidated: every position's tiered maintenance margin."""
        total = 0.0
        for symbol, size in self.positions().items():
            mark = self._mark(symbol, marks) or 0.0
            total += maintenance_margin(size, mark, self.contracts[symbol].maintenance_rate_at(abs(size) * mark))
        return total

    def buying_power(self, symbol: str, mark: float | None = None) -> float:
        """Notional of new `symbol` exposure the free margin supports at its leverage cap, after the entry fee."""
        marks = {symbol: mark} if mark is not None else None
        free = self.equity(marks) - self.used_initial_margin(marks)
        leverage = self.leverage_cap(symbol)
        return max(0.0, free) * leverage / (1.0 + leverage * self.contracts[symbol].taker_fee_rate)

    def round_size(self, symbol: str, size: float) -> float:
        """`size` rounded down to the contract's size step."""
        step = self.contracts[symbol].size_step
        return math.floor(size / step + 1e-9) * step

    def get_account_snapshot(self) -> dict[str, Any]:
        """Balances and positions as the base adapter reports them, plus margin, equity and per-symbol detail."""
        snapshot = super().get_account_snapshot()
        per_symbol = {}
        for symbol, size in self.positions().items():
            mark, entry = self._mark(symbol, None), self.entry_price(symbol)
            per_symbol[symbol] = {"size": size, "entry_price": entry, "mark_price": mark,
                                  "unrealized_pnl": unrealized_pnl(size, entry, mark) if mark is not None and entry is not None else 0.0}
        equity = self.equity()
        maintenance = self.maintenance_requirement()
        snapshot.update({
            "account_type": "cross_margin",
            "collateral_currency": self._base_currency,
            "wallet": self.wallet_balance(),
            "equity": equity,
            "used_initial_margin": self.used_initial_margin(),
            "maintenance_margin": maintenance,
            "margin_ratio": maintenance / equity if equity > 0 else float("inf"),
            "max_leverage": self.max_leverage,
            "positions_by_symbol": per_symbol,
            "marks": self.marks(),
            "funding_paid_total": self.funding_paid_total,
            "realized_pnl_total": self.realized_pnl_total,
            "fees_paid_total": self.fees_paid_total,
            "liquidation_count": self._liquidation_count,
        })
        return snapshot

    # --- orders --------------------------------------------------------------------------------------------------

    def _per_symbol(self, setting: float | Mapping[str, float], symbol: str) -> float:
        return float(setting.get(symbol, 0.0)) if isinstance(setting, Mapping) else float(setting)

    def _reject(self, order_id: str, message: str) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="REJECTED", message=message)

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
        """Fill the order now if it passes the contract, reduce-only and account-wide margin checks, else reject it."""
        side = side.lower()
        if symbol not in self.contracts:
            return self._reject(order_id, f"this account trades {sorted(self.contracts)}, not {symbol}")
        if side not in {"buy", "sell"}:
            return self._reject(order_id, f"unknown side {side!r}")
        if size <= 0.0 or price <= 0.0:
            return self._reject(order_id, "size and price must be positive")
        contract = self.contracts[symbol]
        rounded = self.round_size(symbol, size)
        if rounded < contract.min_size:
            return self._reject(order_id, f"size {size} is below the contract minimum {contract.min_size}")

        current = self.position_size(symbol)
        after = current + rounded if side == "buy" else current - rounded
        grows = abs(after) > abs(current) + _EPSILON or (current != 0.0 and after != 0.0 and (after > 0) != (current > 0))
        if reduce_only and grows:
            return self._reject(order_id, f"reduce_only: {side} {rounded} would grow or flip the {symbol} position of {current}")

        slip = self._per_symbol(self._slippage, symbol) / 10_000.0
        fill_price = price * (1.0 + slip) if side == "buy" else price * (1.0 - slip)
        fee = rounded * fill_price * contract.taker_fee_rate
        if grows:
            marks = {**self._marks, symbol: fill_price}
            others = sum(initial_margin(size_, self._mark(other, marks) or 0.0, self.leverage_cap(other)) for other, size_ in self.positions().items() if other != symbol)
            required = others + initial_margin(after, fill_price, self.leverage_cap(symbol))
            available = self.equity(marks) - fee
            if available < required:
                return self._reject(order_id, f"insufficient margin: all positions need {required:.2f} {self._base_currency} after this order, "
                                              f"equity after the fee is {available:.2f}")

        order = ExecutionOrder(order_id=order_id, side=side, size=rounded, symbol=symbol, price=price, timestamp=timestamp, status="FILLED",
                               fill_price=fill_price, filled_size=rounded, fee=fee, exchange=self.exchange_name, message=f"filled in {self.exchange_name} cross-margin sandbox")
        self._orders[order_id] = order
        self._marks.setdefault(symbol, price)
        self._settle_fill(order)
        self.save_state()
        return ExecutionReport(order_id=order_id, status="FILLED", fill_price=fill_price, filled_size=rounded, fee=fee, message=order.message)

    def _settle_fill(self, order: ExecutionOrder) -> None:
        """Track the position like the base adapter, but move only realized PnL and the fee through the wallet."""
        contract = self.contracts[str(order.symbol)]
        key = contract.base_asset
        size_before, entry_before = self._positions.get(key, 0.0), self._position_entry_price.get(key)
        signed = order.filled_size if order.side == "buy" else -float(order.filled_size or 0.0)
        realized = 0.0
        if size_before != 0.0 and entry_before is not None and (size_before > 0.0) != (signed > 0.0):
            closed = min(abs(size_before), abs(signed))
            realized = closed * (float(order.fill_price) - entry_before) * (1.0 if size_before > 0.0 else -1.0)
        wallet_before = self.wallet_balance()
        # The base class books spot-style cash movements and the position, entry price and open time per base asset.
        order.symbol, original_symbol = key, order.symbol
        super()._apply_fill_to_account_state(order=order, filled_size=order.filled_size, fill_price=order.fill_price, fee=order.fee, previous_fill_size=0.0, previous_fee=0.0)
        order.symbol = original_symbol
        self._balances[self._base_currency] = wallet_before + realized - float(order.fee)
        self.realized_pnl_total += realized
        self.fees_paid_total += float(order.fee)
        residue = self._positions.get(key, 0.0)
        if residue != 0.0 and abs(residue) < contract.size_step / 2.0:
            self._positions.pop(key, None)
            self._position_opened_at.pop(key, None)
            self._position_entry_price.pop(key, None)

    def cancel_order(self, *, order_id: str) -> ExecutionReport:
        """Orders fill instantly here, so there is never anything to cancel."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(order_id=order_id, status=order.status, message="order already final")

    def get_order_status(self, *, order_id: str) -> ExecutionReport:
        """Return the stored state of an order."""
        order = self._orders.get(order_id)
        if order is None:
            return ExecutionReport(order_id=order_id, status="NOT_FOUND", message="order not found")
        return ExecutionReport(order_id=order_id, status=order.status, fill_price=order.fill_price, filled_size=order.filled_size, fee=order.fee, message=order.message)

    # --- market updates ------------------------------------------------------------------------------------------

    def on_market_update(self, *, prices: Mapping[str, float], timestamp: datetime) -> list[dict[str, Any]]:
        """Accrue funding since the last update, take the new marks, and liquidate if equity fell to maintenance margin.

        Funding for the elapsed time is charged at the new mark. Returns
        `funding` and `liquidation` event dicts for the caller to log.
        """
        events: list[dict[str, Any]] = []
        previous = self._last_market_update
        for symbol, price in prices.items():
            if symbol in self.contracts and price and price > 0:
                self._marks[symbol] = float(price)
        self._last_market_update = timestamp
        if previous is not None:
            days = (timestamp - previous).total_seconds() / 86400.0
            for symbol, size in self.positions().items():
                rate = self._per_symbol(self._funding, symbol)
                mark = self._marks.get(symbol)
                if days > 0.0 and rate and mark:
                    payment = size * mark * rate / 100.0 * days
                    self._balances[self._base_currency] = self.wallet_balance() - payment
                    self.funding_paid_total += payment
                    events.append({"type": "funding", "symbol": symbol, "payment": payment, "size": size, "mark_price": mark, "elapsed_days": days})
        if self.positions():
            maintenance = self.maintenance_requirement()
            if self.equity() <= maintenance:
                events.append(self._liquidate_all(timestamp=timestamp, maintenance=maintenance))
        self.save_state()
        return events

    def _liquidate_all(self, *, timestamp: datetime, maintenance: float) -> dict[str, Any]:
        equity_before = self.equity()
        closed = {}
        for symbol, size in self.positions().items():
            self._liquidation_count += 1
            mark = self._marks[symbol]
            order = ExecutionOrder(order_id=f"liquidation-{self._liquidation_count}", side="sell" if size > 0 else "buy", size=abs(size), symbol=symbol, price=mark,
                                   timestamp=timestamp, status="FILLED", fill_price=mark, filled_size=abs(size),
                                   fee=abs(size) * mark * self.contracts[symbol].taker_fee_rate, exchange=self.exchange_name,
                                   message="liquidated: account equity fell to maintenance margin")
            self._orders[order.order_id] = order
            self._settle_fill(order)
            closed[symbol] = size
        bad_debt = 0.0
        if self.wallet_balance() < 0.0:
            bad_debt = -self.wallet_balance()  # a real venue's insurance fund absorbs this; the sandbox floors the wallet
            self._balances[self._base_currency] = 0.0
        return {"type": "liquidation", "closed": closed, "equity_before": equity_before, "maintenance_margin": maintenance,
                "wallet_after": self.wallet_balance(), "bad_debt": bad_debt}

    # --- persistence ---------------------------------------------------------------------------------------------

    def save_state(self) -> None:
        """Write the account to `state_path` atomically (a temp file, then rename); no-op without a path."""
        if self.state_path is None:
            return
        payload = {
            "account": "cross_margin",
            "symbols": sorted(self.contracts),
            "collateral_currency": self._base_currency,
            "wallet": self.wallet_balance(),
            "positions": {symbol: self._position_record(symbol, size) for symbol, size in self.positions().items()},
            "marks": self._marks,
            "last_market_update": self._last_market_update.isoformat() if self._last_market_update else None,
            "funding_paid_total": self.funding_paid_total,
            "realized_pnl_total": self.realized_pnl_total,
            "fees_paid_total": self.fees_paid_total,
            "liquidation_count": self._liquidation_count,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True))
        temporary.replace(self.state_path)

    def _position_record(self, symbol: str, size: float) -> dict[str, Any]:
        opened = self._position_opened_at.get(self.contracts[symbol].base_asset)
        return {"size": size, "entry_price": self.entry_price(symbol), "opened_at": opened.isoformat() if opened else None}

    def _load_state(self) -> bool:
        if self.state_path is None or not self.state_path.exists():
            return False
        payload = json.loads(self.state_path.read_text())
        unknown = sorted(set(payload.get("positions", {})) - set(self.contracts))
        if payload.get("account") != "cross_margin" or unknown:
            raise ValueError(f"{self.state_path} doesn't match this account (unknown positions {unknown}); move it aside or use another path")
        if payload.get("collateral_currency") != self._base_currency:
            raise ValueError(f"{self.state_path} holds {payload.get('collateral_currency')} collateral, not {self._base_currency}")
        self._balances[self._base_currency] = float(payload["wallet"])
        for symbol, position in payload.get("positions", {}).items():
            key = self.contracts[symbol].base_asset
            self._positions[key] = float(position["size"])
            self._position_entry_price[key] = float(position["entry_price"])
            if position.get("opened_at"):
                self._position_opened_at[key] = datetime.fromisoformat(position["opened_at"])
        self._marks = {symbol: float(price) for symbol, price in payload.get("marks", {}).items()}
        if payload.get("last_market_update"):
            self._last_market_update = datetime.fromisoformat(payload["last_market_update"])
        self.funding_paid_total = float(payload.get("funding_paid_total", 0.0))
        self.realized_pnl_total = float(payload.get("realized_pnl_total", 0.0))
        self.fees_paid_total = float(payload.get("fees_paid_total", 0.0))
        self._liquidation_count = int(payload.get("liquidation_count", 0))
        self._remote_balances = dict(self._balances)
        return True
