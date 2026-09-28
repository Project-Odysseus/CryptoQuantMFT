"""H1, leverage crowding: OI-weighted funding z-score, OI change and quarterly basis, and the two pre-registered rules.

Definitions follow research/prereg/H1.txt exactly. Every input comes from `src.research.pit` (point-in-time, holdout
locked), and every value at decision time T uses only rows with `available_at <= T`. `build_signals(coin, end=T)`
restricts every loader to data available by T, which the look-ahead test uses to show that the signal at T does not
change when later data is removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.research import pit
from src.research.bar_engine import TargetRun, run_targets
from src.research.costs import VenueCosts
from src.research.hypotheses.common import cost_at, daily_bars

VENUES = ("binance", "bybit")
SMOOTH_DAYS = 3
Z_WINDOW, Z_MIN = 90, 60
HORIZONS = (1, 3, 7)


def normalised_rates(settlements: pd.DataFrame) -> pd.DataFrame:
    """Settlements with `rate_8h`: each rate scaled to an 8-hour equivalent by the time since the previous settlement."""
    frame = settlements.sort_values("timestamp").reset_index(drop=True)
    interval = frame["timestamp"].diff().dt.total_seconds().div(3600).fillna(8.0).clip(1.0, 8.0)
    return frame.assign(rate_8h=frame["rate"] * 8.0 / interval)


def trailing_mean(times: pd.Series, values: pd.Series, decisions: pd.DatetimeIndex, days: int) -> np.ndarray:
    """Mean of `values` whose time falls in (T - days, T] for each decision T (NaN if none)."""
    t = pd.DatetimeIndex(pd.to_datetime(times, utc=True)).asi8
    cs = np.concatenate([[0.0], np.cumsum(values.to_numpy(dtype=float))])
    hi = np.searchsorted(t, decisions.asi8, side="right")
    lo = np.searchsorted(t, (decisions - pd.Timedelta(days=days)).asi8, side="right")
    count = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, (cs[hi] - cs[lo]) / np.maximum(count, 1), np.nan)


def rolling_z(values: pd.Series, window: int = Z_WINDOW, min_periods: int = Z_MIN) -> pd.Series:
    """z-score against the trailing `window` values including the current one."""
    mean = values.rolling(window, min_periods=min_periods).mean()
    std = values.rolling(window, min_periods=min_periods).std()
    return (values - mean) / std


def build_signals(coin: str, *, end: Any = None) -> pd.DataFrame:
    """Daily H1 inputs per decision time T (00:00 UTC): F, F_ann, z, dOI3, basis, basis z, open and forward returns.

    Forward returns are outcomes, not inputs, and are NaN where the horizon runs past the available bars.
    """
    hourly = pit.load_bars(coin, "1h", end=end)
    first, last = hourly.index[0].ceil("D"), hourly["available_at"].iloc[-1].floor("D")
    decisions = pd.date_range(first, last, freq="D", tz="UTC")
    closes = pit.asof(hourly[["available_at", "close"]], decisions, ["close"])["close"]

    funding, weights = {}, {}
    for venue in VENUES:
        settlements = normalised_rates(pit.load_funding(coin, venue, end=end))
        funding[venue] = pd.Series(trailing_mean(settlements["timestamp"], settlements["rate_8h"], decisions, SMOOTH_DAYS), index=decisions)
        oi = pit.load_open_interest(coin, venue, end=end)
        oi_at = pit.asof(oi, decisions, ["oi"])["oi"]
        stale = pit.asof(oi.assign(seen=oi["available_at"]), decisions, ["seen"])["seen"]
        oi_at[(decisions - pd.DatetimeIndex(stale)) > pd.Timedelta(days=1)] = np.nan  # a gap, not a value
        weights[venue] = oi_at * closes if venue == "bybit" else pit.asof(oi, decisions, ["oi_usd"])["oi_usd"].where(oi_at.notna())
        weights[f"{venue}_coins"] = oi_at

    f = pd.DataFrame(funding)
    w = pd.DataFrame({v: weights[v] for v in VENUES}).where(f.notna())
    have_all_weights = w.notna().sum(axis=1) == f.notna().sum(axis=1)
    equal = f.notna().astype(float)
    w = w.where(have_all_weights, equal.where(equal > 0))
    frame = pd.DataFrame(index=decisions)
    frame["F"] = (f * w).sum(axis=1, min_count=1) / w.sum(axis=1, min_count=1)
    frame["oi_weighted"] = have_all_weights & f.notna().any(axis=1)
    frame["F_ann"] = frame["F"] * 3 * 365
    frame["z"] = rolling_z(frame["F"])

    coins = pd.DataFrame({v: weights[f"{v}_coins"] for v in VENUES})
    earlier = coins.shift(SMOOTH_DAYS)
    both = coins.notna() & earlier.notna()
    frame["dOI3"] = coins.where(both).sum(axis=1, min_count=1) / earlier.where(both).sum(axis=1, min_count=1) - 1.0

    if coin != "SOL":
        basis = pit.load_quarterly_basis(coin, end=end)
        frame["basis_ann"] = pit.asof(basis, decisions, ["basis_ann"])["basis_ann"]
        seen = pit.asof(basis.assign(seen=basis["available_at"]), decisions, ["seen"])["seen"]
        frame.loc[(decisions - pd.DatetimeIndex(seen)) > pd.Timedelta(days=2), "basis_ann"] = np.nan
        frame["basis_z"] = rolling_z(frame["basis_ann"])
    else:
        frame["basis_ann"] = frame["basis_z"] = np.nan

    opens = hourly["open"].reindex(decisions)
    frame["open"] = opens
    for h in HORIZONS:
        frame[f"fwd_{h}d"] = np.log(opens.shift(-h) / opens)
    frame["coin"] = coin
    return frame


# --- rule (a): directional tilt ---------------------------------------------------------------------------------------

def tilt_state(signal: np.ndarray, doi3: np.ndarray, *, min_hold: int, crowded: float = 2.0, washout: float = -1.5,
               require_oi: bool = True) -> np.ndarray:
    """-1 (crowded), +1 (washed out) or 0 per day; a trigger's state is kept at least `min_hold` days."""
    state, left, out = 0, 0, np.zeros(len(signal))
    for i, (value, change) in enumerate(zip(signal, doi3)):
        trigger = 0
        if np.isfinite(value):
            if value > crowded and (not require_oi or (np.isfinite(change) and change > 0)):
                trigger = -1
            elif value < washout:
                trigger = 1
        if trigger:
            state, left = trigger, min_hold
        elif left > 1:
            left -= 1
        else:
            state, left = 0, 0
        out[i] = state
    return out


@dataclass(frozen=True, slots=True)
class CoinMarket:
    """What the rules trade for one coin, computed once: daily perp and spot bars at T, per-side costs, funding."""

    coin: str
    perp_days: pd.DataFrame
    spot_days: pd.DataFrame
    perp_cost: np.ndarray  # per side at each perp day's open (vol-scaled), before any multiplier
    spot_costs: dict[str, np.ndarray]  # per spot venue
    perp_costs: dict[str, np.ndarray]  # per perp venue
    funding: pd.DataFrame  # Binance settlements (timestamp, rate)


def prepare_market(coin: str, signals: pd.DataFrame, venues: dict[str, VenueCosts], *, end: Any = None) -> CoinMarket:
    """Daily bars on the signal's decision dates and costs for every venue in `venues` (names ending _spot or _perp)."""
    perp_hourly, spot_hourly = pit.load_bars(coin, "1h", end=end), pit.load_bars(coin, "1h", market="spot", end=end)
    perp_days = daily_bars(perp_hourly).reindex(signals.index).dropna(subset=["open"])
    spot_days = daily_bars(spot_hourly).reindex(perp_days.index).dropna(subset=["open"])
    perp_days = perp_days.loc[spot_days.index]
    perp_costs = {name: cost_at(perp_hourly, perp_days.index, venue, coin) for name, venue in venues.items() if name.endswith("_perp")}
    spot_costs = {name: cost_at(spot_hourly, spot_days.index, venue, coin) for name, venue in venues.items() if name.endswith("_spot")}
    return CoinMarket(coin, perp_days, spot_days, next(iter(perp_costs.values())), spot_costs, perp_costs, pit.load_funding(coin, "binance", end=end))


def run_tilt(signals: pd.DataFrame, market: CoinMarket, *, source: str, min_hold: int, perp_venue: str = "kraken_perp",
             multiplier: float = 1.0, shift_days: int = 0) -> dict[str, TargetRun]:
    """Rule (a) on daily bars: the overlay (-0.5/0/+0.5), base+overlay, and the constant 0.5 base.

    `shift_days` circularly shifts the signal inputs against prices (placebo).
    """
    sig = signals.reindex(market.perp_days.index)
    values, change = sig[source].to_numpy(), sig["dOI3"].to_numpy()
    if shift_days:
        values, change = np.roll(values, shift_days), np.roll(change, shift_days)
    state = tilt_state(values, change, min_hold=min_hold)
    costs = market.perp_costs[perp_venue] * multiplier
    overlay = pd.Series(0.5 * state, index=sig.index)
    base = pd.Series(0.5, index=sig.index).where(sig[source].notna(), 0.0)
    runs = {"overlay": run_targets(market.perp_days, overlay, cost_per_side=costs, funding=market.funding)}
    if not shift_days:
        runs["total"] = run_targets(market.perp_days, base + overlay, cost_per_side=costs, funding=market.funding)
        runs["base"] = run_targets(market.perp_days, base, cost_per_side=costs, funding=market.funding)
    return runs


# --- rule (b): delta-neutral carry --------------------------------------------------------------------------------------

def carry_state(f_ann: np.ndarray, hurdle: float) -> np.ndarray:
    """1 while in the carry: enter above `hurdle`, exit below half of it."""
    state, out = 0, np.zeros(len(f_ann))
    for i, value in enumerate(f_ann):
        if np.isfinite(value):
            if not state and value > hurdle:
                state = 1
            elif state and value < 0.5 * hurdle:
                state = 0
        out[i] = state
    return out


def run_carry(signals: pd.DataFrame, market: CoinMarket, venues: dict[str, VenueCosts], *, fees: str,
              expected_hold_days: int, multiplier: float = 1.0, shift_days: int = 0, always_on: bool = False) -> tuple[pd.DataFrame, dict[str, float]]:
    """Rule (b): long spot, short perp per unit notional, with `fees` "binance" or "kraken" (both legs).

    Returns daily P&L parts (spot, perp, funding received, cost, gross, net) and the round trip and hurdle used.
    """
    spot_venue, perp_venue = f"{fees}_spot", f"{fees}_perp"
    round_trip = 2.0 * (float(venues[spot_venue].per_side(1.0, coin=market.coin)) + float(venues[perp_venue].per_side(1.0, coin=market.coin)))
    hurdle = round_trip * 365.0 / expected_hold_days
    index = market.perp_days.index
    raw = signals["F_ann"].reindex(index).to_numpy()
    f_ann = np.roll(raw, shift_days) if shift_days else raw
    state = np.where(np.isfinite(raw), 1.0, 0.0) if always_on else carry_state(f_ann, hurdle)
    target = pd.Series(state, index=index)
    spot = run_targets(market.spot_days, target, cost_per_side=market.spot_costs[spot_venue] * multiplier)
    perp = run_targets(market.perp_days, -target, cost_per_side=market.perp_costs[perp_venue] * multiplier, funding=market.funding)
    frame = pd.DataFrame({"position": spot.frame["position"], "spot": spot.frame["gross"], "perp": perp.frame["gross"],
                          "funding": -perp.frame["funding"], "cost": spot.frame["cost"] + perp.frame["cost"]})
    frame["gross"] = frame["spot"] + frame["perp"] + frame["funding"]
    frame["net"] = frame["gross"] - frame["cost"]
    return frame, {"round_trip": round_trip, "hurdle_ann": hurdle}
