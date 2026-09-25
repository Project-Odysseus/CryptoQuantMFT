"""Perpetual-futures contract specs, margin math and a sandbox margin-account adapter.

This is the first slice of perpetual-futures support and it never touches a
real exchange. A perpetual is a linear (quote-margined) swap with no expiry:
you post collateral, hold a signed position in base units, realize profit
and loss in the collateral currency, and pay or receive periodic funding.

Account model (single contract, cross margin, collateral held in one
currency):

    wallet     = deposited collateral + realized PnL - fees - funding
    unrealized = size * (mark - entry_price)          (size is signed)
    equity     = wallet + unrealized
    initial    = |size| * mark / max_leverage         (needed to open/increase)
    maintenance= |size| * mark * maintenance_margin_rate
    liquidated when equity <= maintenance

All figures here are floats, matching the other execution adapters. The
contract defaults are ASSUMED values for sandbox runs and are not read from
any exchange; see `PerpContract.verified`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.execution.adapters import ExecutionAdapter, ExecutionOrder, ExecutionReport


@dataclass(frozen=True, slots=True)
class PerpContract:
    """Specification of one linear perpetual contract.

    Attributes:
        symbol: Symbol the runtime uses for market data, e.g. ``BTC/EUR``.
        venue_symbol: The exchange's own contract name (informational).
        base_asset: Asset the position is denominated in, e.g. ``BTC``.
        collateral_currency: Currency collateral and PnL are held in.
        size_step: Smallest size increment, in base units.
        min_size: Smallest order, in base units.
        max_leverage: Highest leverage the venue allows on this contract.
        maintenance_margin_rate: Fraction of notional that must stay as equity
            before the position is liquidated.
        taker_fee_rate: Fee per fill as a fraction of notional.
        maker_fee_rate: Maker fee as a fraction of notional (informational).
        verified: True only when built from the venue's own instrument and fee data.
        tick_size: Minimum price increment (0.0 when unknown).
        margin_tiers: Venue margin schedule by position notional, if known.
    """

    symbol: str
    venue_symbol: str
    base_asset: str
    collateral_currency: str
    size_step: float
    min_size: float
    max_leverage: float
    maintenance_margin_rate: float
    taker_fee_rate: float
    maker_fee_rate: float
    verified: bool = False
    tick_size: float = 0.0
    # (position notional threshold, initial margin rate, maintenance margin rate), ascending by threshold.
    margin_tiers: tuple[tuple[float, float, float], ...] = ()

    def maintenance_rate_at(self, notional: float) -> float:
        """Maintenance margin rate that applies to a position of `notional` (tiered venues charge more on bigger positions)."""
        rate = self.maintenance_margin_rate
        for threshold, _initial, maintenance in self.margin_tiers:
            if notional >= threshold:
                rate = maintenance
        return rate

    def __post_init__(self) -> None:
        """Reject specs whose numbers cannot describe a real contract."""
        if self.size_step <= 0.0 or self.min_size < self.size_step:
            raise ValueError("size_step must be positive and min_size at least one step")
        if self.max_leverage < 1.0:
            raise ValueError("max_leverage must be at least 1")
        if not 0.0 < self.maintenance_margin_rate < 1.0 / self.max_leverage:
            raise ValueError("maintenance_margin_rate must be positive and below the initial margin rate (1 / max_leverage)")
        if self.taker_fee_rate < 0.0 or self.maker_fee_rate < 0.0:
            raise ValueError("fee rates cannot be negative")


def assumed_perp_contract(symbol: str = "BTC/EUR", *, collateral_currency: str | None = None) -> PerpContract:
    """Build an unverified, sandbox-only contract for `symbol` (e.g. BTC/EUR).

    The numbers are round assumptions typical of a base-tier perpetual
    (0.05% taker, 0.02% maker, 1% maintenance margin, 5x venue leverage cap).
    They exist so dry runs behave like a margin product, not to describe any
    real contract. Check the venue's contract specification before using a
    real adapter.
    """
    base, _, quote = symbol.partition("/")
    base = (base or "BTC").upper()
    return PerpContract(
        symbol=symbol,
        venue_symbol=f"PF_{base}{(quote or 'USD').upper()}",
        base_asset=base,
        collateral_currency=(collateral_currency or quote or "USD").upper(),
        size_step=0.0001,
        min_size=0.0001,
        max_leverage=5.0,
        maintenance_margin_rate=0.01,
        taker_fee_rate=0.0005,
        maker_fee_rate=0.0002,
    )


def perp_contract_from_instrument(instrument: dict[str, Any], fee_schedule: dict[str, Any], *, symbol: str | None = None) -> PerpContract:
    """Build a verified `PerpContract` from a Kraken Futures instrument and its fee schedule (public data).

    The size step comes from `contractValueTradePrecision`, margin rates from
    the retail margin levels (falling back to the standard ones), and fees
    from the schedule's entry tier (the 30-day-volume-zero tier, which is
    what a small account pays). `max_leverage` is the venue's limit implied
    by the first tier's initial margin; the leverage a given account may
    actually use is set on the account, not in the public data.
    """
    levels = instrument.get("retailMarginLevels") or instrument.get("marginLevels") or []
    if not levels:
        raise ValueError(f"instrument {instrument.get('symbol')} has no margin levels")
    tiers = tuple(
        (float(level["numNonContractUnits"]), float(level["initialMargin"]), float(level["maintenanceMargin"]))
        for level in sorted(levels, key=lambda level: float(level["numNonContractUnits"]))
    )
    precision = int(instrument.get("contractValueTradePrecision", 4))
    step = 10.0 ** (-precision)
    entry_tier = sorted(fee_schedule["tiers"], key=lambda tier: float(tier["usdVolume"]))[0]
    base, quote = str(instrument["base"]).upper(), str(instrument["quote"]).upper()
    return PerpContract(
        symbol=symbol or f"{base}/{quote}",
        venue_symbol=str(instrument["symbol"]),
        base_asset=base,
        collateral_currency=quote,
        size_step=step,
        min_size=step,
        max_leverage=1.0 / tiers[0][1],
        maintenance_margin_rate=tiers[0][2],
        taker_fee_rate=float(entry_tier["takerFee"]) / 100.0,
        maker_fee_rate=float(entry_tier["makerFee"]) / 100.0,
        verified=True,
        tick_size=float(instrument.get("tickSize", 0.0)),
        margin_tiers=tiers,
    )


def unrealized_pnl(size: float, entry_price: float, mark_price: float) -> float:
    """Profit or loss of an open linear position at `mark_price` (size is signed)."""
    return size * (mark_price - entry_price)


def initial_margin(size: float, mark_price: float, max_leverage: float) -> float:
    """Collateral needed to hold `size` at `max_leverage`."""
    return abs(size) * mark_price / max_leverage


def maintenance_margin(size: float, mark_price: float, maintenance_margin_rate: float) -> float:
    """Equity below which the position is liquidated."""
    return abs(size) * mark_price * maintenance_margin_rate


def liquidation_price(size: float, entry_price: float, wallet: float, maintenance_margin_rate: float) -> float | None:
    """Mark price at which equity falls to maintenance margin, or None when flat.

    Solves ``wallet + size * (p - entry) = |size| * p * mmr`` for ``p``. For
    a long this is below the entry price and for a short above it. A result
    at or below zero means the position cannot be liquidated by a falling
    price (a long backed by more than its full notional).
    """
    if size == 0.0:
        return None
    direction = 1.0 if size > 0.0 else -1.0
    price = (size * entry_price - wallet) / (size * (1.0 - maintenance_margin_rate * direction))
    return price if price > 0.0 else None


class SandboxPerpExecutionAdapter(ExecutionAdapter):
    """In-process perpetual-futures account: margin checks, funding and liquidation, no real orders.

    Orders fill immediately at the submitted price (like the spot sandbox).
    Unlike the spot adapters, a fill moves no notional in or out of cash: the
    wallet only changes by realized PnL, fees and funding, and the position
    is backed by margin instead. `get_account_snapshot()` reports equity and
    liquidation prices so the engine can size and stop positions correctly.

    Use `on_market_update` once per cycle: it marks the position to the
    latest price, accrues funding for the time elapsed and liquidates the
    position if equity has fallen to maintenance margin.
    """

    name = "sandbox_perp"
    margin_account = True

    def __init__(
        self,
        *,
        contract: PerpContract | None = None,
        starting_collateral: float = 1000.0,
        max_leverage: float = 2.0,
        funding_pct_per_day: float = 0.03,
        exchange_name: str = "kraken_futures",
    ) -> None:
        """Create a margin account holding `starting_collateral` in the contract's collateral currency.

        Args:
            max_leverage: The cap this account enforces on new exposure. It is
                deliberately below the contract's own cap by default: a low
                cap keeps the liquidation price far from the entry price.
            funding_pct_per_day: Percent of notional per day longs pay and
                shorts receive (negative reverses it). A placeholder until a
                real funding feed exists.
        """
        super().__init__()
        self.contract = contract or assumed_perp_contract()
        if not 1.0 <= max_leverage <= self.contract.max_leverage:
            raise ValueError(f"max_leverage must be between 1 and the contract cap of {self.contract.max_leverage}")
        if starting_collateral < 0.0:
            raise ValueError("starting_collateral cannot be negative")
        self.exchange_name = exchange_name
        self.max_leverage = max_leverage
        self.funding_pct_per_day = funding_pct_per_day
        self._base_currency = self.contract.collateral_currency
        self._balances = {self._base_currency: float(starting_collateral)}
        self._remote_balances = dict(self._balances)
        self._mark_price: float | None = None
        self._last_market_update: datetime | None = None
        self._liquidation_count = 0
        self.funding_paid_total = 0.0
        self.realized_pnl_total = 0.0
        self.fees_paid_total = 0.0

    @property
    def position_symbol(self) -> str:
        """Key the contract's position is stored under (its base asset)."""
        return self.contract.base_asset

    def position_size(self) -> float:
        """Signed size of the open position in base units (0.0 when flat)."""
        return self._positions.get(self.position_symbol, 0.0)

    def wallet_balance(self) -> float:
        """Collateral after realized PnL, fees and funding (excludes unrealized PnL)."""
        return self._balances.get(self._base_currency, 0.0)

    def equity(self, mark_price: float | None = None) -> float:
        """Wallet plus unrealized PnL at `mark_price` (default: the latest mark)."""
        size = self.position_size()
        mark = self._resolve_mark(mark_price)
        entry = self._position_entry_price.get(self.position_symbol)
        if size == 0.0 or entry is None or mark is None:
            return self.wallet_balance()
        return self.wallet_balance() + unrealized_pnl(size, entry, mark)

    def used_initial_margin(self, mark_price: float | None = None) -> float:
        """Initial margin locked by the open position."""
        mark = self._resolve_mark(mark_price)
        if mark is None:
            return 0.0
        return initial_margin(self.position_size(), mark, self.max_leverage)

    def buying_power(self, mark_price: float | None = None) -> float:
        """Notional of new exposure the free margin can support at `max_leverage`."""
        free_margin = self.equity(mark_price) - self.used_initial_margin(mark_price)
        return max(0.0, free_margin) * self.max_leverage

    def liquidation_price(self) -> float | None:
        """Mark price at which the open position would be liquidated, or None when flat."""
        entry = self._position_entry_price.get(self.position_symbol)
        if entry is None:
            return None
        mark = self._resolve_mark(None) or entry
        rate = self.contract.maintenance_rate_at(abs(self.position_size()) * mark)
        return liquidation_price(self.position_size(), entry, self.wallet_balance(), rate)

    def round_size(self, size: float) -> float:
        """Round `size` down to the contract's size step."""
        step = self.contract.size_step
        return math.floor(size / step + 1e-9) * step

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None) -> ExecutionReport:
        """Fill an order at `price` if it passes the contract, margin and leverage checks, else reject it."""
        side = side.lower()
        if side not in {"buy", "sell"}:
            return self._reject(order_id, f"unknown side {side!r}")
        if size <= 0.0 or price <= 0.0:
            return self._reject(order_id, "size and price must be positive")
        if symbol is not None and self._position_symbol(symbol) != self.position_symbol:
            return self._reject(order_id, f"this adapter trades {self.contract.symbol} only, not {symbol}")

        rounded = self.round_size(size)
        if rounded < self.contract.min_size:
            return self._reject(order_id, f"size {size} is below the contract minimum {self.contract.min_size}")

        self._mark_price = price
        current = self.position_size()
        after = current + rounded if side == "buy" else current - rounded
        fee = rounded * price * self.contract.taker_fee_rate
        if abs(after) > abs(current) + 1e-12:
            # Exposure grows (or flips): the resulting position must fit inside the leverage cap.
            required = initial_margin(after, price, self.max_leverage)
            if self.equity(price) - fee < required:
                return self._reject(
                    order_id,
                    f"insufficient margin: need {required:.2f} {self._base_currency} for a {abs(after):.4f} position at {self.max_leverage:g}x, "
                    f"equity after fee is {self.equity(price) - fee:.2f}",
                )

        order = ExecutionOrder(
            order_id=order_id,
            side=side,
            size=rounded,
            symbol=symbol,
            price=price,
            timestamp=timestamp,
            status="FILLED",
            fill_price=price,
            filled_size=rounded,
            fee=fee,
            exchange=self.exchange_name,
            message=f"filled in {self.exchange_name} sandbox",
        )
        self._orders[order_id] = order
        self._apply_fill_to_account_state(order=order, filled_size=rounded, fill_price=price, fee=fee, previous_fill_size=0.0, previous_fee=0.0)
        return ExecutionReport(order_id=order_id, status="FILLED", fill_price=price, filled_size=rounded, fee=fee, message=order.message)

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

    def list_orders(self) -> list[ExecutionOrder]:
        """All orders this adapter has handled."""
        return list(self._orders.values())

    def on_market_update(self, *, symbol: str | None, mark_price: float, timestamp: datetime) -> list[dict[str, Any]]:
        """Mark to `mark_price`, accrue funding since the last update, and liquidate if equity hit maintenance margin.

        Returns event dicts (``funding``, ``liquidation``) for the caller to log.
        """
        if mark_price <= 0.0 or (symbol is not None and self._position_symbol(symbol) != self.position_symbol):
            return []
        events: list[dict[str, Any]] = []
        previous_update = self._last_market_update
        self._mark_price = mark_price
        self._last_market_update = timestamp

        size = self.position_size()
        if size != 0.0 and previous_update is not None and self.funding_pct_per_day != 0.0:
            elapsed_days = (timestamp - previous_update).total_seconds() / 86400.0
            if elapsed_days > 0.0:
                payment = size * mark_price * self.funding_pct_per_day / 100.0 * elapsed_days
                self._balances[self._base_currency] = self.wallet_balance() - payment
                self.funding_paid_total += payment
                events.append({"type": "funding", "payment": payment, "size": size, "mark_price": mark_price, "elapsed_days": elapsed_days})

        if size != 0.0:
            maintenance = maintenance_margin(size, mark_price, self.contract.maintenance_rate_at(abs(size) * mark_price))
            if self.equity(mark_price) <= maintenance:
                events.append(self._liquidate(mark_price=mark_price, timestamp=timestamp, maintenance=maintenance))
        return events

    def get_account_snapshot(self) -> dict[str, Any]:
        """Balances and positions as the base adapter reports them, plus margin, equity and liquidation data."""
        snapshot = super().get_account_snapshot()
        snapshot.update(
            {
                "account_type": "margin",
                "equity": self.equity(),
                "used_initial_margin": self.used_initial_margin(),
                "buying_power": self.buying_power(),
                "max_leverage": self.max_leverage,
                "liquidation_price": {self.position_symbol: self.liquidation_price()} if self.position_size() != 0.0 else {},
                "funding_paid_total": self.funding_paid_total,
                "realized_pnl_total": self.realized_pnl_total,
            }
        )
        return snapshot

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
        """Track the position like the base class, but settle only PnL and fees in the wallet (no notional moves)."""
        fill_delta = (filled_size or 0.0) - previous_fill_size
        if fill_delta <= 0.0:
            return
        symbol_key = self._position_symbol(order.symbol) if order.symbol else self.position_symbol
        size_before = self._positions.get(symbol_key, 0.0)
        entry_before = self._position_entry_price.get(symbol_key)
        price = float(fill_price or order.price or 0.0)
        fee_delta = max(0.0, (fee or 0.0) - previous_fee)
        signed_delta = fill_delta if order.side == "buy" else -fill_delta

        realized = 0.0
        if size_before != 0.0 and (size_before > 0.0) != (signed_delta > 0.0) and entry_before is not None:
            closed = min(abs(size_before), fill_delta)
            realized = closed * (price - entry_before) * (1.0 if size_before > 0.0 else -1.0)

        wallet_before = self.wallet_balance()
        super()._apply_fill_to_account_state(
            order=order,
            filled_size=filled_size,
            fill_price=fill_price,
            fee=fee,
            previous_fill_size=previous_fill_size,
            previous_fee=previous_fee,
        )
        self._balances[self._base_currency] = wallet_before + realized - fee_delta
        self.realized_pnl_total += realized
        self.fees_paid_total += fee_delta

        residue = self._positions.get(symbol_key, 0.0)
        if residue != 0.0 and abs(residue) < self.contract.size_step / 2.0:
            self._positions.pop(symbol_key, None)
            self._position_opened_at.pop(symbol_key, None)
            self._position_entry_price.pop(symbol_key, None)

    def _liquidate(self, *, mark_price: float, timestamp: datetime, maintenance: float) -> dict[str, Any]:
        size = self.position_size()
        equity_before = self.equity(mark_price)
        self._liquidation_count += 1
        order = ExecutionOrder(
            order_id=f"liquidation-{self._liquidation_count}",
            side="sell" if size > 0.0 else "buy",
            size=abs(size),
            symbol=self.contract.symbol,
            price=mark_price,
            timestamp=timestamp,
            status="FILLED",
            fill_price=mark_price,
            filled_size=abs(size),
            fee=abs(size) * mark_price * self.contract.taker_fee_rate,
            exchange=self.exchange_name,
            message="liquidated: equity fell to maintenance margin",
        )
        self._orders[order.order_id] = order
        self._apply_fill_to_account_state(order=order, filled_size=order.size, fill_price=mark_price, fee=order.fee, previous_fill_size=0.0, previous_fee=0.0)
        bad_debt = 0.0
        if self.wallet_balance() < 0.0:
            # A gap through zero: a real venue's insurance fund absorbs this, the sandbox just floors the wallet.
            bad_debt = -self.wallet_balance()
            self._balances[self._base_currency] = 0.0
        return {
            "type": "liquidation",
            "order_id": order.order_id,
            "size": size,
            "mark_price": mark_price,
            "equity_before": equity_before,
            "maintenance_margin": maintenance,
            "wallet_after": self.wallet_balance(),
            "bad_debt": bad_debt,
        }

    def _resolve_mark(self, mark_price: float | None) -> float | None:
        return mark_price if mark_price is not None else self._mark_price

    def _reject(self, order_id: str, message: str) -> ExecutionReport:
        return ExecutionReport(order_id=order_id, status="REJECTED", message=message)
