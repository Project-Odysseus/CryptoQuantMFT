"""What a book of perps, spot, calls and puts is exposed to: greeks per underlying and P&L under shock scenarios.

A position's notional says little about its risk once options are in the
book: a short call and a long perp can cancel each other's direction yet
leave a large bet on volatility. So risk is measured in the same units for
every instrument, per underlying:

- **delta**: the money gained or lost for a 1% rise in the underlying (and the
  equivalent position in units);
- **gamma**: how much that 1% delta changes after a 1% move (short options
  make it negative, and hedges then have to chase the market);
- **vega**: money per 1 vol point rise in implied volatility;
- **theta**: money gained or lost per day with nothing else changing.

All of them come from **full revaluation** with the pricing model (bump the
market, reprice every position), so they are exact for any model: Black-76,
the SVI surface, Merton, the PDE. `scenario_grid` reprices the whole book
under large moves (-50% to +50% crossed with vol shocks), which greeks alone
can't capture for options.

Everything is pure: positions and a market state in, numbers out. The
portfolio risk overlay can limit these numbers the way it limits weights now.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from src.options.pricing import Black76, PricingModel

SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass(frozen=True, slots=True)
class Position:
    """One holding. `quantity` is signed: units of the underlying for perps and spot, contracts for options.

    Attributes:
        kind: "perp", "spot" or "option".
        underlying: The asset whose price drives it, e.g. "BTC".
        strike, expiry, right: Options only ("call" or "put").
        multiplier: Units of the underlying per option contract (1 on Deribit).
        label: A name for reports.
    """

    kind: str
    underlying: str
    quantity: float
    strike: float | None = None
    expiry: datetime | None = None
    right: str | None = None
    multiplier: float = 1.0
    label: str = ""

    def __post_init__(self) -> None:
        """Reject options without strike, expiry or right."""
        if self.kind not in {"perp", "spot", "option"}:
            raise ValueError(f"kind must be perp, spot or option, not {self.kind!r}")
        if self.kind == "option" and (self.strike is None or self.expiry is None or self.right not in {"call", "put"}):
            raise ValueError("an option needs a strike, an expiry and right = 'call' or 'put'")


@dataclass(frozen=True, slots=True)
class MarketState:
    """Prices and a pricing model per underlying at one moment.

    Attributes:
        spot: Underlying price per asset.
        models: The pricing model per asset (default: Black-76 at `default_vol`).
        forward: Optional `forward(asset, expiry, spot)` for the forward at an
            expiry (default: spot, since crypto basis is small).
        rate: Continuous rate for discounting option payoffs.
    """

    now: datetime
    spot: Mapping[str, float]
    models: Mapping[str, PricingModel] = field(default_factory=dict)
    forward: Callable[[str, datetime, float], float] | None = None
    rate: float = 0.0
    default_vol: float = 0.6

    def model_for(self, asset: str) -> PricingModel:
        """The asset's model, or Black-76 at the default vol."""
        return self.models.get(asset) or Black76(self.default_vol)


def _vol_shifted(model: PricingModel, shift: float) -> PricingModel:
    if shift == 0:
        return model
    if hasattr(model, "shifted"):
        return model.shifted(shift)
    if hasattr(model, "sigma"):
        return replace(model, sigma=max(1e-4, model.sigma + shift))
    raise ValueError(f"{type(model).__name__} has no way to shift its volatility (give it a `sigma` or a `shifted` method)")


def position_value(position: Position, market: MarketState, *, spot_move: float = 0.0, vol_shift: float = 0.0, days: float = 0.0) -> float:
    """Value in money after the underlying moves by `spot_move` (0.1 = +10%), vols move by `vol_shift` and `days` pass.

    Perps and spot are valued as their position value (units x price); a
    change in it is their P&L. Options are valued with the asset's model.
    """
    spot = market.spot[position.underlying] * (1.0 + spot_move)
    if position.kind in {"perp", "spot"}:
        return position.quantity * spot
    when = market.now + timedelta(days=days)
    t = max((position.expiry - when).total_seconds() / SECONDS_PER_YEAR, 0.0)
    forward = market.forward(position.underlying, position.expiry, spot) if market.forward else spot
    model = _vol_shifted(market.model_for(position.underlying), vol_shift)
    discount = float(np.exp(-market.rate * t))
    return position.quantity * position.multiplier * model.price(forward, position.strike, t, position.right, discount)


def book_value(positions: Iterable[Position], market: MarketState, **shocks: float) -> float:
    """The whole book's value under the same shocks."""
    return sum(position_value(position, market, **shocks) for position in positions)


def exposures(positions: Sequence[Position], market: MarketState) -> pd.DataFrame:
    """Greeks per underlying (and a total row), in money, by full revaluation.

    Columns: `delta_1pct` (money for a +1% move), `delta_units` (the same as a
    position in the underlying), `gamma_1pct` (change in `delta_1pct` after a
    +1% move), `vega_1pt` (money for +1 vol point), `theta_1d` (money per day).
    """
    rows = []
    for asset in sorted({position.underlying for position in positions}):
        held = [position for position in positions if position.underlying == asset]
        base = book_value(held, market)
        up, down = book_value(held, market, spot_move=0.01), book_value(held, market, spot_move=-0.01)
        delta = (up - down) / 2
        gamma = up - 2 * base + down
        vega = (book_value(held, market, vol_shift=0.005) - book_value(held, market, vol_shift=-0.005)) if any(p.kind == "option" for p in held) else 0.0
        theta = book_value(held, market, days=1.0) - base if any(p.kind == "option" for p in held) else 0.0
        rows.append({"underlying": asset, "value": base, "delta_1pct": delta, "delta_units": delta / (0.01 * market.spot[asset]),
                     "gamma_1pct": gamma, "vega_1pt": vega, "theta_1d": theta, "positions": len(held)})
    frame = pd.DataFrame(rows).set_index("underlying")
    if len(frame) > 1:
        frame.loc["total"] = frame.drop(columns=["delta_units"]).sum().reindex(frame.columns)
        frame.loc["total", "delta_units"] = np.nan
    return frame


def scenario_grid(positions: Sequence[Position], market: MarketState, *, spot_moves: Sequence[float] = (-0.5, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5),
                  vol_shifts: Sequence[float] = (-0.2, -0.1, 0.0, 0.1, 0.2), days: float = 0.0) -> pd.DataFrame:
    """Book P&L (money) for every (spot move, vol shift) pair, all underlyings moving together.

    Crypto assets move together in a crash, so the default grid shocks them as
    one. Rows are spot moves; columns are vol shifts in vol points.
    """
    base = book_value(positions, market)
    table = {f"{shift * 100:+.0f} vol pts": [book_value(positions, market, spot_move=move, vol_shift=shift, days=days) - base for move in spot_moves]
             for shift in vol_shifts}
    return pd.DataFrame(table, index=pd.Index([f"{move:+.0%}" for move in spot_moves], name="spot move"))
