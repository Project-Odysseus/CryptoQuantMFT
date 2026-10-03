"""H4 and H5: Kalshi's daily BTC "above" markets against what Deribit's options imply, on one shared panel.

Both hypotheses ask about the same pair of prices at the same moments:

- what Kalshi quotes for "BTC above K at 5 PM New York" (a probability), and
- what the Deribit option chain recorded at that moment implies for the same level and time.

`build_panel` makes one row per (recorded chain snapshot, Kalshi market): Kalshi's bid and ask as of the snapshot,
the options-implied probability, and, once the market has settled, what happened. H5 (`h5_gap`) asks whether the gap
between the two predicts the outcome. H4 (`h4_protection`) asks what downside protection bought as Kalshi "No"
contracts just below the price costs and returns.

The options-implied probability of ending above K is minus the slope of the call price in the strike. It is taken
numerically from Black-76 prices on the chain's own volatility smile (`implied_prob_above`), which keeps the
smile's slope; the flat-volatility N(d2) is reported next to it. Deribit's dailies expire at 08:00 UTC and Kalshi's
daily at 21:00 or 22:00 UTC, so volatility is interpolated in total variance to Kalshi's time.

Observations overlap heavily (the same event at several snapshots and many levels), so every interval is a
bootstrap over whole events (`event_bootstrap`), never over rows.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.options.pricing import Black76
from src.research import prediction_markets as pm

SERIES = "KXBTCD"
CACHE = Path("data/research/prediction_markets/kalshi_events")
BUMP = 0.0025  # the strike bump for the call-price slope, as a share of the strike
_EVENTS_THIS_RUN: dict[str, list[dict[str, Any]]] = {}  # an unsettled event's markets, fetched once per process (each list is ~160 KB)


def daily_event_ticker(day: date) -> str:
    """Kalshi's event for the 5 PM New York settlement on `day`, e.g. KXBTCD-26SEP3017."""
    return f"{SERIES}-{day:%y}{day:%b}{day:%d}17".upper()


def implied_prob_above(chain: pd.DataFrame, strike: float, expiry: datetime, *, now: datetime, spot: float) -> tuple[float, float]:
    """(smile-consistent, flat-volatility) probability of ending at or above `strike`, from one chain snapshot.

    The first is -dC/dK from Black-76 calls priced at the smile's volatility for strikes just below and above; a
    falling smile (lower volatility at higher strikes) lowers the calls there and raises the probability, which
    N(d2) at one volatility misses. Forward = `spot` (hours to expiry: carry is negligible).
    """
    seconds = (pd.Timestamp(expiry) - pd.Timestamp(now)).total_seconds()
    years = seconds / pm.SECONDS_PER_YEAR
    if years <= 0:
        return (1.0, 1.0) if spot >= strike else (0.0, 0.0)
    width = strike * BUMP

    def call(level: float) -> float:
        return Black76(pm.chain_implied_vol(chain, level, expiry, now=now)).price(spot, level, years, "call")

    smile = (call(strike - width) - call(strike + width)) / (2.0 * width)
    flat = pm.prob_above(spot, strike, pm.chain_implied_vol(chain, strike, expiry, now=now), seconds)
    return float(min(max(smile, 0.0), 1.0)), flat


def put_spread_cost(chain: pd.DataFrame, strike: float, expiry: datetime) -> dict[str, float] | None:
    """What a Deribit put spread around `strike` costs per unit of payout, at the nearest listed expiry (a ramp, not a step)."""
    listed = min(chain["expiry"].unique(), key=lambda value: abs((pd.Timestamp(value) - pd.Timestamp(expiry)).total_seconds()))
    puts = chain[(chain["expiry"] == listed) & (chain["right"] == "put")].set_index("strike").sort_index()
    below, above = puts.index[puts.index < strike], puts.index[puts.index >= strike]
    if not len(below) or not len(above):
        return None
    low, high = puts.loc[below[-1]], puts.loc[above[0]]
    width = float(above[0] - below[-1])
    return {"gap_hours": abs((pd.Timestamp(listed) - pd.Timestamp(expiry)).total_seconds()) / 3600.0, "buy": float(high["ask"] - low["bid"]) / width,
            "mark": float(high["mark"] - low["mark"]) / width}


def _event_markets(client: pm.KalshiClient, event: str, cache_dir: Path) -> list[dict[str, Any]]:
    """An event's markets, from the cache once every one of them has settled (a settled event never changes)."""
    path = cache_dir / f"{event}.json"
    if path.exists():
        return json.loads(path.read_text())
    memo = (str(cache_dir), event, id(client))
    if memo in _EVENTS_THIS_RUN:
        return _EVENTS_THIS_RUN[memo]
    markets = [{key: raw.get(key) for key in ("ticker", "event_ticker", "floor_strike", "open_time", "close_time", "result", "expiration_value")} for raw in client.event_markets(event)]
    if markets and all(market["result"] in ("yes", "no") for market in markets):
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(markets))
    _EVENTS_THIS_RUN[memo] = markets
    return markets


def build_panel(chains: pd.DataFrame, client: pm.KalshiClient, *, cache_dir: Path | str = CACHE, moneyness: float = 0.03, min_hours: float = 1.0, max_hours: float = 26.0,
                progress: Callable[[str], None] | None = None) -> pd.DataFrame:
    """One row per (chain snapshot, Kalshi daily "above" market within `moneyness` of the index and `min_hours`-`max_hours` from settling).

    Columns: `time`, `event`, `ticker`, `strike`, `expiry`, `hours_left`, `index` (Deribit's), `log_moneyness`
    (ln K/S), `yes_bid`, `yes_ask`, `mid`, `implied` (smile), `implied_flat`, `put_spread_buy`, `put_spread_gap_hours`,
    `yes` (True/False once settled, else None) and `settled_at`.
    """
    cache_dir = Path(cache_dir)
    rows: list[dict[str, Any]] = []
    for stamp, chain in chains.groupby("timestamp"):
        now = pd.Timestamp(stamp).to_pydatetime()
        index = float(chain["index_price"].median())
        candidates = []
        for offset in (0, 1):
            event = daily_event_ticker((now + timedelta(days=offset)).date())
            for market in _event_markets(client, event, cache_dir):
                opened, closed, strike = pd.Timestamp(market["open_time"]), pd.Timestamp(market["close_time"]), float(market["floor_strike"])
                hours = (closed - pd.Timestamp(now)).total_seconds() / 3600.0
                if opened <= pd.Timestamp(now) and min_hours <= hours <= max_hours and abs(math.log(strike / index)) <= moneyness:
                    candidates.append((market, closed, strike, hours))
        if not candidates:
            continue
        quotes = client.quotes_at([market["ticker"] for market, *_rest in candidates], now)
        for market, closed, strike, hours in candidates:
            if market["ticker"] not in quotes:
                continue
            bid, ask = quotes[market["ticker"]]
            smile, flat = implied_prob_above(chain, strike, closed.to_pydatetime(), now=now, spot=index)
            spread = put_spread_cost(chain, strike, closed.to_pydatetime())
            rows.append({"time": pd.Timestamp(now), "event": market["event_ticker"], "ticker": market["ticker"], "strike": strike, "expiry": closed, "hours_left": hours, "index": index,
                         "log_moneyness": math.log(strike / index), "yes_bid": bid, "yes_ask": ask, "mid": (bid + ask) / 2.0, "implied": smile, "implied_flat": flat,
                         "put_spread_buy": spread["buy"] if spread else np.nan, "put_spread_gap_hours": spread["gap_hours"] if spread else np.nan,
                         "yes": {"yes": True, "no": False}.get(market["result"]), "settled_at": pm._number(market.get("expiration_value"))})
        if progress is not None:
            progress(f"{now:%Y-%m-%d %H:%M}: {len(candidates)} markets in range, {sum(1 for market, *_ in candidates if market['ticker'] in quotes)} quoted")
    return pd.DataFrame(rows)


def event_bootstrap(frame: pd.DataFrame, statistic: Callable[[pd.DataFrame], float], *, runs: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """(estimate, lower, upper): a 95% interval from resampling whole events with replacement.

    Rows of one event share its outcome, so they are one observation's worth of luck however many there are.
    With fewer than three events there is no interval.
    """
    estimate = float(statistic(frame)) if len(frame) else float("nan")
    groups = [part for _event, part in frame.groupby("event")]
    if len(groups) < 3:
        return estimate, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = []
    for _run in range(runs):
        value = statistic(pd.concat([groups[i] for i in rng.integers(0, len(groups), len(groups))], ignore_index=True))
        if np.isfinite(value):
            draws.append(value)
    if not draws:
        return estimate, float("nan"), float("nan")
    return estimate, float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))


def _slope(frame: pd.DataFrame, implied: str) -> float:
    gap, miss = (frame[implied] - frame["mid"]).to_numpy(dtype=float), (frame["yes"].astype(float) - frame["mid"]).to_numpy(dtype=float)
    denominator = float(np.sum(gap * gap))
    return float(np.sum(gap * miss) / denominator) if denominator > 0 else float("nan")


def taker_trades(panel: pd.DataFrame, *, implied: str = "implied", margin: float = 0.02, fee_rate: float = pm.KALSHI_FEE_RATE) -> pd.DataFrame:
    """The pre-registered rule: take the side the options favour when its edge at the ask, after the fee, exceeds `margin`.

    Returns the panel's settled rows that trade, with `side` ("yes"/"no"), `cost` (the ask plus the fee) and `pnl`
    per contract held to settlement.
    """
    settled = panel[panel["yes"].notna()].copy()
    no_ask = 1.0 - settled["yes_bid"]
    yes_cost = settled["yes_ask"] + fee_rate * settled["yes_ask"] * (1.0 - settled["yes_ask"])
    no_cost = no_ask + fee_rate * no_ask * (1.0 - no_ask)
    buy_yes, buy_no = settled[implied] - yes_cost > margin, (1.0 - settled[implied]) - no_cost > margin
    happened = settled["yes"].astype(float)
    trades = settled[buy_yes | buy_no].copy()
    trades["side"] = np.where(buy_yes[trades.index], "yes", "no")
    trades["cost"] = np.where(buy_yes[trades.index], yes_cost[trades.index], no_cost[trades.index])
    trades["pnl"] = np.where(buy_yes[trades.index], happened[trades.index], 1.0 - happened[trades.index]) - trades["cost"]
    return trades


def h5_gap(panel: pd.DataFrame, *, margin: float = 0.02, runs: int = 2000) -> dict[str, Any]:
    """H5's outcomes on the settled part of a panel. See research/prereg/H5.txt.

    `slope` is b in (outcome - Kalshi mid) = b x (implied - Kalshi mid), through the origin: 1 means the options
    are right wherever the two disagree, 0 means Kalshi is. Also the same with the flat-volatility probability, the
    Brier score of each price, the average gap by moneyness, and the taker rule's profit per contract.
    """
    settled = panel[panel["yes"].notna()].copy()
    happened = settled["yes"].astype(float)
    trades = taker_trades(panel, margin=margin)
    buckets = pd.cut(settled["log_moneyness"], [-1.0, -0.01, -0.0025, 0.0025, 0.01, 1.0], labels=["K more than 1% below", "K 0.25-1% below", "K at the money", "K 0.25-1% above", "K more than 1% above"])
    by_moneyness = settled.assign(gap=settled["implied"] - settled["mid"], miss=happened - settled["mid"], spread=settled["yes_ask"] - settled["yes_bid"]).groupby(buckets, observed=True).agg(
        rows=("gap", "size"), kalshi_mid=("mid", "mean"), implied=("implied", "mean"), happened=("yes", lambda column: float(column.astype(float).mean())),
        mean_gap=("gap", "mean"), mean_spread=("spread", "mean"))
    return {
        "rows": int(len(settled)), "events": int(settled["event"].nunique()), "snapshots": int(settled["time"].nunique()),
        "slope": event_bootstrap(settled, lambda frame: _slope(frame, "implied"), runs=runs),
        "slope_flat": event_bootstrap(settled, lambda frame: _slope(frame, "implied_flat"), runs=runs),
        "mean_gap": event_bootstrap(settled, lambda frame: float((frame["implied"] - frame["mid"]).mean()), runs=runs),
        "mean_abs_gap": float((settled["implied"] - settled["mid"]).abs().mean()) if len(settled) else float("nan"),
        "brier_kalshi": float(((settled["mid"] - happened) ** 2).mean()) if len(settled) else float("nan"),
        "brier_implied": float(((settled["implied"] - happened) ** 2).mean()) if len(settled) else float("nan"),
        "trades": int(len(trades)), "trade_events": int(trades["event"].nunique()) if len(trades) else 0,
        "trade_pnl": event_bootstrap(trades, lambda frame: float(frame["pnl"].mean()), runs=runs) if len(trades) else (float("nan"),) * 3,
        "by_moneyness": by_moneyness,
    }


def h4_protection(panel: pd.DataFrame, *, nearest: float = 0.005, farthest: float = 0.02, min_hours: float = 2.0, fee_rate: float = pm.KALSHI_FEE_RATE, runs: int = 2000) -> dict[str, Any]:
    """H4's outcomes: buying "No" on levels `nearest` to `farthest` below the price, at the ask, held to settlement. See research/prereg/H4.txt.

    Per contract: `cost` = the No ask plus the fee; `payoff` = 1 if the price settled below the level; `pnl` their
    difference (the primary outcome, with an event-bootstrap interval). `richness` = cost minus the options-implied
    fair value (1 - implied). `cover_bps` = what it costs, in basis points of a long position's size, to hold enough
    contracts to be paid the whole loss down to the level if it is breached. The Deribit put spread around the same
    level is given per unit of payout for comparison; it is a ramp at another expiry, so it is not the same contract.
    """
    rows = panel[(panel["log_moneyness"] <= -nearest) & (panel["log_moneyness"] >= -farthest) & (panel["hours_left"] >= min_hours)].copy()
    no_ask = 1.0 - rows["yes_bid"]
    rows["cost"] = no_ask + fee_rate * no_ask * (1.0 - no_ask)
    rows["fair"] = 1.0 - rows["implied"]
    rows["richness"] = rows["cost"] - rows["fair"]
    rows["cover_bps"] = (1.0 - rows["strike"] / rows["index"]) * rows["cost"] * 10_000.0
    settled = rows[rows["yes"].notna()].copy()
    settled["payoff"] = 1.0 - settled["yes"].astype(float)
    settled["pnl"] = settled["payoff"] - settled["cost"]
    comparable = rows[rows["put_spread_gap_hours"] <= 14.0]
    return {
        "rows": int(len(rows)), "settled_rows": int(len(settled)), "events": int(settled["event"].nunique()),
        "mean_cost": float(rows["cost"].mean()) if len(rows) else float("nan"), "mean_fair": float(rows["fair"].mean()) if len(rows) else float("nan"),
        "richness": event_bootstrap(rows, lambda frame: float(frame["richness"].mean()), runs=runs),
        "pnl": event_bootstrap(settled, lambda frame: float(frame["pnl"].mean()), runs=runs),
        "breach_rate": float(settled["payoff"].mean()) if len(settled) else float("nan"),
        "mean_cover_bps": float(rows["cover_bps"].mean()) if len(rows) else float("nan"),
        "put_spread_rows": int(len(comparable)), "kalshi_cost_same_rows": float(comparable["cost"].mean()) if len(comparable) else float("nan"),
        "put_spread_buy": float(comparable["put_spread_buy"].mean()) if len(comparable) else float("nan"),
        "detail": settled,
    }


def window(panel: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    """The panel's rows with a snapshot time in [start, end) (UTC dates; None = open-ended)."""
    mask = pd.Series(True, index=panel.index)
    if start:
        mask &= panel["time"] >= pd.Timestamp(start, tz="UTC")
    if end:
        mask &= panel["time"] < pd.Timestamp(end, tz="UTC")
    return panel[mask]


def confirmatory_rows(panel: pd.DataFrame, start: str, end: str, *, min_events: int = 20) -> tuple[pd.DataFrame, str | None]:
    """The confirmatory window's rows and the end date actually used, as the pre-registrations define it.

    The window is [start, end). If it holds fewer than `min_events` settled daily events, its end moves forward one
    day at a time until it does. Returns (rows, end used), or (the rows so far, None) while even every snapshot
    recorded after `start` holds too few settled events: the test is not ready.
    """
    after = panel[panel["time"] >= pd.Timestamp(start, tz="UTC")] if len(panel) else panel
    settled = after[after["yes"].notna()] if len(after) else after
    stop = pd.Timestamp(end, tz="UTC")
    last = after["time"].max() if len(after) else stop
    while True:
        inside = settled[settled["time"] < stop] if len(settled) else settled
        if len(inside) and inside["event"].nunique() >= min_events:
            return after[after["time"] < stop], f"{stop:%Y-%m-%d}"
        if stop > last:
            return after, None
        stop += pd.Timedelta(days=1)


def describe_interval(values: Sequence[float], *, percent: bool = False) -> str:
    """"estimate (low to high)" for an `event_bootstrap` result."""
    pattern = "{:+.1%}" if percent else "{:+.3f}"
    estimate, low, high = values
    if not np.isfinite(estimate):
        return "n/a"
    return pattern.format(estimate) + (f" ({pattern.format(low)} to {pattern.format(high)})" if np.isfinite(low) else " (too few events for an interval)")
