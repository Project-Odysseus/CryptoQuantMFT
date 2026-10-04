"""Listed Deribit option contracts: pick one by delta, tenor and liquidity, and know when to roll it.

A sleeve never names a contract. It asks for a structure ("a put near -0.10 delta, about 60 days out"), and
`pick_contract` finds the listed contract that matches it best in the latest chain snapshot, among contracts
liquid enough to trade: a bid and an ask on the side we need, a spread no wider than `max_relative_spread`, and
at least `min_open_interest`. Delta comes from Black-76 at each contract's own mark IV and forward, so the choice
agrees with how the market marks it.

`needs_roll` says when a held contract should be replaced by a fresh one: when fewer than `roll_days` remain (time
decay and gamma speed up near expiry), or when its delta has drifted too far from the target.

Contract sizes and minimums come from Deribit's public instrument list (`fetch_specs`): for the USDC-settled
(linear) BTC options, amounts are in BTC with a minimum of 0.01.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.options.deribit import PUBLIC_API
from src.options.pricing import Black76, greeks
from src.utils.retry import retry_call


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Trading rules for one listed contract (amounts in the underlying: BTC for BTC options)."""

    instrument: str
    min_trade_amount: float
    contract_size: float
    tick_size: float


@dataclass(frozen=True, slots=True)
class PickedContract:
    """The contract `pick_contract` chose, with the market numbers it was chosen on."""

    instrument: str
    underlying: str
    settlement: str
    expiry: datetime
    strike: float
    right: str
    days: float
    delta: float
    iv: float
    forward: float
    bid: float
    ask: float
    mark: float
    open_interest: float

    @property
    def relative_spread(self) -> float:
        """(ask - bid) / mid."""
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid if mid > 0 else float("inf")


def fetch_specs(currency: str = "USDC", *, kind: str = "option") -> dict[str, ContractSpec]:
    """Deribit's listed instruments for a settlement currency (public, no key), as specs by instrument name."""

    def call() -> dict[str, Any]:
        url = f"{PUBLIC_API}/get_instruments?" + urllib.parse.urlencode({"currency": currency, "kind": kind, "expired": "false"})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "QuantMultiStrat/0.1"}), timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    payload = retry_call(call, label="deribit get_instruments")
    return {row["instrument_name"]: ContractSpec(row["instrument_name"], float(row["min_trade_amount"]), float(row["contract_size"]), float(row["tick_size"]))
            for row in payload.get("result", [])}


def with_deltas(chain: pd.DataFrame) -> pd.DataFrame:
    """The chain with each contract's Black-76 delta at its own mark IV and forward."""
    frame = chain.copy()
    frame["delta"] = [greeks(Black76(float(row.mark_iv)), float(row.forward), float(row.strike), float(row.t), str(row.right)).delta
                      if row.mark_iv > 0 and row.t > 0 else np.nan for row in frame.itertuples()]
    return frame


def pick_contract(
    chain: pd.DataFrame,
    *,
    underlying: str,
    right: str,
    target_delta: float,
    days: float,
    side: str = "buy",
    settlement: str = "linear",
    max_relative_spread: float = 0.3,
    min_open_interest: float = 0.0,
    tenor_tolerance: float = 0.5,
) -> PickedContract | None:
    """The listed contract closest to `target_delta` in the expiry closest to `days`, among liquid ones; None if none qualify.

    Args:
        target_delta: Signed Black-76 delta, e.g. -0.10 for a 10-delta put, 0.25 for a 25-delta call.
        days: Target days to expiry. Expiries within `days x (1 +- tenor_tolerance)` qualify.
        side: "buy" needs an ask to pay, "sell" needs a bid to hit; both need the other side for the spread.
    """
    if right not in ("call", "put"):
        raise ValueError("right must be 'call' or 'put'")
    frame = chain[(chain["underlying"] == underlying) & (chain["right"] == right) & (chain["settlement"] == settlement)].copy()
    frame["days"] = frame["t"] * 365.0
    frame = frame[frame["days"].between(days * (1 - tenor_tolerance), days * (1 + tenor_tolerance))]
    frame = frame[frame["bid"].gt(0) & frame["ask"].gt(0) & frame["open_interest"].ge(min_open_interest)]
    mid = (frame["bid"] + frame["ask"]) / 2.0
    frame = frame[((frame["ask"] - frame["bid"]) / mid).le(max_relative_spread)]
    if frame.empty:
        return None
    frame = with_deltas(frame).dropna(subset=["delta"])
    if frame.empty:
        return None
    expiry = frame.loc[(frame["days"] - days).abs().idxmin(), "expiry"]
    in_expiry = frame[frame["expiry"] == expiry]
    row = in_expiry.loc[(in_expiry["delta"] - target_delta).abs().idxmin()]
    return PickedContract(
        instrument=str(row["instrument"]), underlying=underlying, settlement=settlement, expiry=pd.Timestamp(row["expiry"]).to_pydatetime(),
        strike=float(row["strike"]), right=right, days=float(row["days"]), delta=float(row["delta"]), iv=float(row["mark_iv"]),
        forward=float(row["forward"]), bid=float(row["bid"]), ask=float(row["ask"]), mark=float(row["mark"]), open_interest=float(row["open_interest"]),
    )


def needs_roll(expiry: datetime, now: datetime, *, roll_days: float, delta: float | None = None, target_delta: float | None = None,
               max_delta_drift: float | None = None) -> str | None:
    """Why a held contract should be rolled now ("expiry" or "delta_drift"), or None to keep it."""
    if (expiry - now).total_seconds() / 86_400.0 <= roll_days:
        return "expiry"
    if max_delta_drift is not None and delta is not None and target_delta is not None and abs(delta - target_delta) > max_delta_drift:
        return "delta_drift"
    return None


def round_amount(amount: float, spec: ContractSpec) -> float:
    """`amount` rounded down to the contract's minimum trade amount (0 if below it)."""
    step = spec.min_trade_amount
    return float(np.floor(abs(amount) / step + 1e-9) * step) * (1 if amount >= 0 else -1)
