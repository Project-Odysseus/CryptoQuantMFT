"""Exposure limits for a book of perps, spot and options: `[risk.exposure]`, checked on greeks and the scenario grid.

Notional says little about an option book's risk, so these limits are on what the book is exposed to, per
underlying, as shares of equity (all computed by full revaluation in `exposure.py`):

    [risk.exposure]
    max_delta = 1.0            # |money made or lost per 1% move| x 100, i.e. delta in money, at most 100% of equity
    max_vega_per_point = 0.01  # at most 1% of equity per implied-vol point
    max_short_gamma = 0.002    # a 1% move changes the 1% delta by at most 0.2% of equity against us
    max_scenario_loss = 0.30   # the worst cell of the scenario grid loses at most 30% of equity

`book_positions` turns the portfolio book into exposure positions (perps and spot by their base coin, options by
their Deribit name). `check_exposure` lists breaches; `scale_for_limits` finds how much of a proposed set of
positions (e.g. an option sleeve's) fits next to what the book already holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.portfolio.exposure import MarketState, Position, exposures, scenario_grid

EXPOSURE_LIMIT_KEYS = frozenset({"max_delta", "max_vega_per_point", "max_short_gamma", "max_scenario_loss"})
SPOT_MOVES = (-0.5, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.5)
VOL_SHIFTS = (-0.2, 0.0, 0.2)


@dataclass(frozen=True, slots=True)
class Breach:
    """One limit the book exceeds: `value` and `limit` are shares of equity."""

    rule: str
    underlying: str
    value: float
    limit: float


def option_position(instrument_id: str, units: float) -> Position:
    """An exposure `Position` for a Deribit option id such as "deribit:BTC_USDC-27NOV26-70000-P" (units in the coin)."""
    from src.options.deribit import parse_instrument, underlying_and_settlement

    name = instrument_id.split(":", 1)[-1]
    currency, expiry, strike, right = parse_instrument(name)
    underlying, _settlement = underlying_and_settlement(currency)
    return Position(kind="option", underlying=underlying, quantity=units, strike=strike, expiry=expiry, right=right, label=name)


def base_coin(symbol: str) -> str:
    """The coin an instrument moves with: "BTC/USD" -> "BTC", "XBT/EUR" -> "BTC"."""
    base = symbol.split("/")[0].upper()
    return "BTC" if base == "XBT" else base


def book_positions(units: Mapping[str, Any], kinds: Mapping[str, str]) -> list[Position]:
    """Exposure positions for a book's signed units per instrument id (`kinds[id]` is "perp", "spot" or "option")."""
    positions = []
    for instrument, quantity in units.items():
        amount = float(quantity)
        if amount == 0:
            continue
        kind = kinds.get(instrument, "perp")
        if kind == "option":
            positions.append(option_position(instrument, amount))
        else:
            positions.append(Position(kind=kind, underlying=base_coin(instrument.split(":", 1)[-1]), quantity=amount, label=instrument))
    return positions


def check_exposure(positions: Sequence[Position], market: MarketState, limits: Mapping[str, float], equity: float) -> tuple[pd.DataFrame, pd.DataFrame, list[Breach]]:
    """The book's greeks per underlying, its scenario grid, and every limit it breaks."""
    if not positions or equity <= 0:
        return pd.DataFrame(), pd.DataFrame(), []
    table = exposures(positions, market)
    grid = scenario_grid(positions, market, spot_moves=SPOT_MOVES, vol_shifts=VOL_SHIFTS)
    breaches = []
    for underlying, row in table.drop(index="total", errors="ignore").iterrows():
        checks = {
            "max_delta": abs(row["delta_1pct"]) * 100.0 / equity,
            "max_vega_per_point": abs(row["vega_1pt"]) / equity,
            "max_short_gamma": max(0.0, -row["gamma_1pct"]) / equity,
        }
        for rule, value in checks.items():
            limit = limits.get(rule)
            if limit is not None and value > limit + 1e-12:
                breaches.append(Breach(rule, str(underlying), float(value), float(limit)))
    limit = limits.get("max_scenario_loss")
    worst = float(-grid.to_numpy().min()) / equity
    if limit is not None and worst > limit + 1e-12:
        breaches.append(Breach("max_scenario_loss", "all", worst, float(limit)))
    return table, grid, breaches


def scale_for_limits(held: Sequence[Position], proposed: Sequence[Position], market: MarketState, limits: Mapping[str, float], equity: float,
                     *, steps: Iterable[float] = tuple(np.round(np.linspace(1.0, 0.0, 21), 2))) -> float:
    """The largest share of `proposed` (from 100% down in 5% steps) that, added to `held`, breaks no limit; 0 if none fits.

    A grid rather than a bisection, because risk isn't monotone in size when the proposal hedges what is held
    (a smaller put hedge can mean *more* delta).
    """
    for share in steps:
        scaled = [_scaled(position, share) for position in proposed]
        _table, _grid, breaches = check_exposure(list(held) + scaled, market, limits, equity)
        if not breaches:
            return float(share)
    return 0.0


def _scaled(position: Position, share: float) -> Position:
    return Position(kind=position.kind, underlying=position.underlying, quantity=position.quantity * share, strike=position.strike,
                    expiry=position.expiry, right=position.right, multiplier=position.multiplier, label=position.label)
