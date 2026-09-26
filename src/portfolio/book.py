"""The portfolio book: what the portfolio holds, what it's worth, and which sleeve earned what.

State only, no I/O. The engine feeds it fills, funding and prices, and
checkpoints it as JSON after every cycle, so a restart resumes exactly where
the last cycle ended. Money is `Decimal`: these numbers become balances,
reports and tax records.

Accounting per instrument kind:

- **spot**: buying spends cash (price x units plus the fee), selling returns
  it. The holding is worth units x mark.
- **perp** (linear, e.g. Kraken Futures PF_ contracts): opening only pays
  the fee from collateral. Reducing realises (price - average entry) x
  units into cash. The open position is worth units x (mark - average
  entry). Funding moves cash: longs pay when the rate is positive.

Each venue keeps its cash in its own currency (Kraken spot in EUR, Kraken
Futures in USD). Equity in the base currency converts each venue at a
recorded FX rate and never adds EUR to USD raw.

Sleeve attribution: each sleeve holds a *virtual* position, its allocated
weight at its decision prices, as if it traded alone without costs. Book P&L
minus the sum of sleeve P&L is the residual from netting, the rebalance band,
lot rounding, fees and funding. It is reported, not hidden.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from src.portfolio.config import InstrumentSpec, PortfolioConfig

VENUE_CURRENCY = {"kraken": "EUR", "kraken_futures": "USD"}
ZERO = Decimal(0)


def _d(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(slots=True)
class BookPosition:
    """One instrument's position: signed units, average entry, and what it has cost and earned so far."""

    units: Decimal = ZERO
    avg_entry: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO  # paid (positive) or received (negative)


@dataclass(slots=True)
class SleevePosition:
    """A sleeve's virtual position in its instrument, for attribution."""

    instrument: str
    units: Decimal = ZERO
    last_price: Decimal = ZERO
    pnl: Decimal = ZERO  # realised and marked so far, in the instrument's venue currency


@dataclass(slots=True)
class PortfolioBook:
    """Positions, cash per venue, marks, FX, equity history for the risk rules, and sleeve attribution."""

    base_currency: str
    instruments: dict[str, InstrumentSpec]
    cash: dict[str, Decimal]
    venue_currency: dict[str, str] = field(default_factory=dict)
    positions: dict[str, BookPosition] = field(default_factory=dict)
    marks: dict[str, Decimal] = field(default_factory=dict)
    fx: dict[str, Decimal] = field(default_factory=dict)  # base currency per 1 unit of each currency
    sleeves: dict[str, SleevePosition] = field(default_factory=dict)
    initial_equity: Decimal = ZERO
    peak_equity: Decimal = ZERO
    day_start_equity: Decimal = ZERO
    day: date | None = None
    last_update: datetime | None = None
    liquidations_booked: list[str] = field(default_factory=list)  # exchange liquidation order ids already in the book

    def __post_init__(self) -> None:
        """Fill in venue currencies and a starting equity."""
        for venue in self.cash:
            self.venue_currency.setdefault(venue, VENUE_CURRENCY.get(venue, self.base_currency))
        self.fx.setdefault(self.base_currency, Decimal(1))
        self.cash = {venue: _d(amount) for venue, amount in self.cash.items()}
        if self.initial_equity == ZERO and all(self.venue_currency[venue] in self.fx for venue in self.cash):
            self.initial_equity = self.equity()
        self.peak_equity = self.peak_equity or self.initial_equity
        self.day_start_equity = self.day_start_equity or self.initial_equity

    @classmethod
    def from_config(cls, config: PortfolioConfig, *, fx: Mapping[str, float | Decimal] | None = None) -> "PortfolioBook":
        """A fresh paper book: `initial_equity` split equally across the config's venues, converted at `fx`."""
        rates = {currency: _d(rate) for currency, rate in (fx or {}).items()}
        rates[config.base_currency] = Decimal(1)
        venues = sorted({spec.venue for spec in config.instruments.values()})
        share = _d(config.initial_equity) / len(venues)
        cash = {}
        for venue in venues:
            currency = VENUE_CURRENCY.get(venue, config.base_currency)
            if currency not in rates:
                raise ValueError(f"venue {venue} keeps {currency}; pass fx={{'{currency}': <{config.base_currency} per {currency}>}}")
            cash[venue] = share / rates[currency]
        return cls(base_currency=config.base_currency, instruments=dict(config.instruments), cash=cash, fx=rates)

    # --- updates -------------------------------------------------------------------------------------------------

    def _spec(self, instrument: str) -> InstrumentSpec:
        if instrument not in self.instruments:
            raise KeyError(f"{instrument} is not in the book's instruments")
        return self.instruments[instrument]

    def apply_fill(self, instrument: str, side: str, units: Decimal | float, price: Decimal | float, fee: Decimal | float = ZERO) -> Decimal:
        """Book a fill (from the exchange, never from the order) and return the P&L it realised, in the venue's currency."""
        spec = self._spec(instrument)
        quantity, price, fee = _d(units), _d(price), _d(fee)
        if quantity <= 0 or price <= 0:
            raise ValueError(f"a fill needs positive units and price (got {quantity} at {price})")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', not {side!r}")
        signed = quantity if side == "buy" else -quantity
        position = self.positions.setdefault(instrument, BookPosition())
        venue = spec.venue
        self.cash.setdefault(venue, ZERO)
        self.venue_currency.setdefault(venue, VENUE_CURRENCY.get(venue, self.base_currency))

        realized = ZERO
        closing = min(quantity, abs(position.units)) if position.units and (position.units > 0) != (signed > 0) else ZERO
        if closing:
            direction = Decimal(1) if position.units > 0 else Decimal(-1)
            realized = (price - position.avg_entry) * closing * direction
        opening = quantity - closing
        new_units = position.units + signed
        if opening:
            # What's left after closing opens on the fill's side, at a weighted average with what was already held there
            held = abs(position.units) if not closing else ZERO
            position.avg_entry = (position.avg_entry * held + price * opening) / (held + opening)
        elif new_units == 0:
            position.avg_entry = ZERO

        if spec.kind == "spot":
            self.cash[venue] -= signed * price + fee
        else:
            self.cash[venue] += realized - fee
        position.units = new_units
        position.realized_pnl += realized
        position.fees += fee
        return realized

    def apply_funding(self, instrument: str, rate: Decimal | float, *, mark: Decimal | float | None = None) -> Decimal:
        """Book one funding payment on a perp: units x mark x rate, paid by longs when the rate is positive. Returns what was paid."""
        spec = self._spec(instrument)
        if spec.kind != "perp":
            return ZERO
        position = self.positions.get(instrument)
        if position is None or position.units == 0:
            return ZERO
        price = _d(mark) if mark is not None else self.marks.get(instrument, position.avg_entry)
        paid = position.units * price * _d(rate)
        self.cash[spec.venue] -= paid
        position.funding += paid
        return paid

    def book_funding(self, instrument: str, amount: Decimal | float) -> None:
        """Book a funding payment the exchange computed (positive = paid), so book and exchange stay equal to the cent."""
        spec = self._spec(instrument)
        paid = _d(amount)
        self.cash[spec.venue] -= paid
        self.positions.setdefault(instrument, BookPosition()).funding += paid

    def mark(self, prices: Mapping[str, Decimal | float], *, fx: Mapping[str, Decimal | float] | None = None, now: datetime | None = None) -> Decimal:
        """Take new prices (and FX rates), update sleeve P&L, the equity peak and the UTC day start; returns equity in base.

        The first mark of a new UTC day starts that day from the equity before
        the new prices, since the move they carry happened during the new day.
        """
        if now is not None and now.date() != self.day:
            self.day_start_equity = self.equity() if self.day is not None else self.day_start_equity
            self.day = now.date()
        for currency, rate in (fx or {}).items():
            self.fx[currency] = _d(rate)
        for instrument, price in prices.items():
            self.marks[instrument] = _d(price)
        for sleeve in self.sleeves.values():
            price = self.marks.get(sleeve.instrument)
            if price is not None and sleeve.last_price:
                sleeve.pnl += sleeve.units * (price - sleeve.last_price)
            if price is not None:
                sleeve.last_price = price
        equity = self.equity()
        self.peak_equity = max(self.peak_equity, equity)
        self.last_update = now or self.last_update
        return equity

    def set_sleeve_targets(self, targets: Mapping[str, tuple[str, float]]) -> None:
        """Re-size each sleeve's virtual position to its allocated weight of current equity, at the current marks.

        Call it when the sleeves decide, after `mark`. A sleeve missing from
        `targets` keeps its virtual position.
        """
        equity = self.equity()
        for sleeve_id, (instrument, weight) in targets.items():
            price = self.marks.get(instrument)
            if price is None or price <= 0:
                raise ValueError(f"no mark for {instrument}; call mark() with its price first")
            currency_rate = self.fx[self.venue_currency[self._spec(instrument).venue]]
            sleeve = self.sleeves.setdefault(sleeve_id, SleevePosition(instrument=instrument, last_price=price))
            sleeve.instrument = instrument
            sleeve.units = _d(weight) * equity / (price * currency_rate)
            sleeve.last_price = price

    # --- views ---------------------------------------------------------------------------------------------------

    def venue_equity(self, venue: str) -> Decimal:
        """Cash plus open positions on one venue, in the venue's currency."""
        total = self.cash.get(venue, ZERO)
        for instrument, position in self.positions.items():
            spec = self._spec(instrument)
            if spec.venue != venue or position.units == 0:
                continue
            mark = self.marks.get(instrument, position.avg_entry)
            total += position.units * mark if spec.kind == "spot" else position.units * (mark - position.avg_entry)
        return total

    def equity(self) -> Decimal:
        """Total equity in the base currency, every venue converted at its recorded FX rate."""
        total = ZERO
        for venue in self.cash:
            currency = self.venue_currency[venue]
            if currency not in self.fx:
                raise ValueError(f"no FX rate for {currency} (venue {venue}); pass fx={{'{currency}': <{self.base_currency} per {currency}>}} to mark()")
            total += self.venue_equity(venue) * self.fx[currency]
        return total

    def units(self) -> dict[str, Decimal]:
        """Signed units per instrument with a position."""
        return {instrument: position.units for instrument, position in self.positions.items() if position.units != 0}

    def weights(self) -> dict[str, float]:
        """Signed position value per instrument as a share of equity (what the risk overlay calls current weights)."""
        equity = self.equity()
        if equity <= 0:
            return {instrument: 0.0 for instrument in self.units()}
        out = {}
        for instrument, units in self.units().items():
            spec = self._spec(instrument)
            value = units * self.marks.get(instrument, self.positions[instrument].avg_entry) * self.fx[self.venue_currency[spec.venue]]
            out[instrument] = float(value / equity)
        return out

    def attribution(self) -> dict[str, Decimal]:
        """P&L per sleeve in the base currency, plus `residual`: what netting, bands, rounding, fees and funding added or cost."""
        out = {}
        for sleeve_id, sleeve in self.sleeves.items():
            out[sleeve_id] = sleeve.pnl * self.fx[self.venue_currency[self._spec(sleeve.instrument).venue]]
        out["residual"] = self.equity() - self.initial_equity - sum(out.values(), ZERO)
        return out

    # --- persistence ---------------------------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready state; Decimals as strings, so a round trip is exact. Instruments are not stored (they come from the config)."""
        return {
            "base_currency": self.base_currency,
            "cash": {venue: str(amount) for venue, amount in self.cash.items()},
            "venue_currency": dict(self.venue_currency),
            "positions": {instrument: {key: str(getattr(position, key)) for key in BookPosition.__slots__} for instrument, position in self.positions.items()},
            "marks": {instrument: str(price) for instrument, price in self.marks.items()},
            "fx": {currency: str(rate) for currency, rate in self.fx.items()},
            "sleeves": {sleeve_id: {"instrument": s.instrument, "units": str(s.units), "last_price": str(s.last_price), "pnl": str(s.pnl)} for sleeve_id, s in self.sleeves.items()},
            "initial_equity": str(self.initial_equity),
            "peak_equity": str(self.peak_equity),
            "day_start_equity": str(self.day_start_equity),
            "day": self.day.isoformat() if self.day else None,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "liquidations_booked": list(self.liquidations_booked),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, instruments: Mapping[str, InstrumentSpec]) -> "PortfolioBook":
        """Rebuild from `to_dict` output and the config's instruments."""
        book = cls(
            base_currency=payload["base_currency"],
            instruments=dict(instruments),
            cash={venue: Decimal(amount) for venue, amount in payload["cash"].items()},
            venue_currency=dict(payload["venue_currency"]),
            positions={instrument: BookPosition(**{key: Decimal(value) for key, value in fields.items()}) for instrument, fields in payload["positions"].items()},
            marks={instrument: Decimal(price) for instrument, price in payload["marks"].items()},
            fx={currency: Decimal(rate) for currency, rate in payload["fx"].items()},
            sleeves={sleeve_id: SleevePosition(instrument=s["instrument"], units=Decimal(s["units"]), last_price=Decimal(s["last_price"]), pnl=Decimal(s["pnl"])) for sleeve_id, s in payload["sleeves"].items()},
            initial_equity=Decimal(payload["initial_equity"]),
            peak_equity=Decimal(payload["peak_equity"]),
            day_start_equity=Decimal(payload["day_start_equity"]),
            day=date.fromisoformat(payload["day"]) if payload.get("day") else None,
            last_update=datetime.fromisoformat(payload["last_update"]) if payload.get("last_update") else None,
            liquidations_booked=list(payload.get("liquidations_booked", [])),
        )
        return book
