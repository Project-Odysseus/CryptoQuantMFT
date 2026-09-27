"""Linear (USDC) chains, the contract picker and roll rules, on hand-built chains (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.options.contracts import ContractSpec, needs_roll, pick_contract, round_amount, with_deltas
from src.options.deribit import normalize_chain, underlying_and_settlement
from src.options.pricing import Black76

NOW = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)
FORWARD = 86_000.0


def _row(expiry: str, strike: float, flag: str, *, iv: float = 0.5, spread: float = 0.04, oi: float = 10.0, linear: bool = True) -> dict:
    moment = datetime.strptime(expiry, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
    t = (moment - NOW).total_seconds() / (365 * 86400)
    price = Black76(iv).price(FORWARD, strike, t, "call" if flag == "C" else "put")
    quote = price if linear else price / FORWARD
    name = f"BTC{'_USDC' if linear else ''}-{expiry}-{int(strike)}-{flag}"
    return {"instrument_name": name, "underlying_price": FORWARD, "bid_price": quote * (1 - spread / 2), "ask_price": quote * (1 + spread / 2),
            "mark_price": quote, "mark_iv": iv * 100, "open_interest": oi, "volume": 1.0, "interest_rate": 0.0, "estimated_delivery_price": FORWARD}


def _chain(rows: list[dict]) -> pd.DataFrame:
    return normalize_chain(rows, NOW, chain="USDC")


def test_linear_prices_are_already_dollars_and_inverse_prices_are_converted() -> None:
    assert underlying_and_settlement("BTC_USDC") == ("BTC", "linear") and underlying_and_settlement("ETH") == ("ETH", "inverse")
    linear = normalize_chain([_row("27NOV26", 70000, "P")], NOW, chain="USDC").iloc[0]
    inverse = normalize_chain([_row("27NOV26", 70000, "P", linear=False)], NOW, chain="BTC").iloc[0]
    assert linear["settlement"] == "linear" and linear["underlying"] == "BTC" and linear["chain"] == "USDC"
    assert linear["mark"] == pytest.approx(inverse["mark"]) and linear["mark_btc"] == pytest.approx(inverse["mark_btc"])


def test_the_picker_finds_the_nearest_delta_in_the_nearest_tenor_among_liquid_contracts() -> None:
    strikes = [60000, 65000, 70000, 75000, 80000]
    chain = _chain([_row("27NOV26", strike, "P") for strike in strikes] + [_row("25DEC26", strike, "P") for strike in strikes]
                   + [_row("30OCT26", strike, "P") for strike in strikes])
    picked = pick_contract(chain, underlying="BTC", right="put", target_delta=-0.10, days=60)
    assert picked is not None and picked.instrument.startswith("BTC_USDC-27NOV26") and picked.days == pytest.approx(61, abs=1)
    same_expiry = with_deltas(chain[chain["instrument"].str.startswith("BTC_USDC-27NOV26")])
    assert abs(picked.delta + 0.10) == pytest.approx((same_expiry["delta"] + 0.10).abs().min())  # the closest delta in that expiry


def test_illiquid_or_wide_contracts_are_never_picked() -> None:
    chain = _chain([_row("27NOV26", 70000, "P", spread=0.9), _row("27NOV26", 60000, "P", oi=0.0), _row("27NOV26", 75000, "P")])
    picked = pick_contract(chain, underlying="BTC", right="put", target_delta=-0.10, days=60, min_open_interest=1.0)
    assert picked is not None and picked.instrument == "BTC_USDC-27NOV26-75000-P"  # the only tradeable one, even if its delta is further off
    assert pick_contract(chain, underlying="BTC", right="put", target_delta=-0.10, days=200) is None  # no expiry near 200 days
    assert pick_contract(chain, underlying="ETH", right="put", target_delta=-0.10, days=60) is None


def test_roll_rules_and_amount_rounding() -> None:
    expiry = NOW + timedelta(days=10)
    assert needs_roll(expiry, NOW, roll_days=7) is None
    assert needs_roll(expiry, NOW + timedelta(days=4), roll_days=7) == "expiry"
    assert needs_roll(expiry, NOW, roll_days=7, delta=-0.35, target_delta=-0.10, max_delta_drift=0.15) == "delta_drift"
    spec = ContractSpec("BTC_USDC-27NOV26-70000-P", min_trade_amount=0.01, contract_size=1.0, tick_size=5.0)
    assert round_amount(0.037, spec) == pytest.approx(0.03) and round_amount(-0.019, spec) == pytest.approx(-0.01) and round_amount(0.004, spec) == 0.0
