"""Four candidate signals as comparable daily return series, and a book that combines the ones chosen.

Each `*_sleeve` function runs one idea with its costs and returns a `Sleeve`: a daily net return series for 1x of
equity, so different ideas can be put side by side, correlated and combined. They reuse the studies that first
tested each idea (docs/research_log.md), with the same data, costs and definitions:

- `carry_sleeve`: long the coins with the lowest funding, short the highest (cross-sectional, weekly).
- `momentum_sleeve`: rank coins by their past 30-day return; the direction is fixed from the in-sample data.
- `taker_sleeve`: the taker-buy basket already in the paper book, for reference.
- `liquidation_sleeve`: fade forced-selling-shaped hours on BTC, ETH and SOL (a proxy; real liquidations are only
  being recorded since late September 2026).
- `variance_premium_sleeve`: sell BTC implied variance against the variance then realised. Theoretical: there is no
  option price history here, so no bid/ask, margin or hedging cost. Marked `tradable=False` and never auto-selected.

`combine` builds a book from them: either a preset of weights, or the sleeves whose in-sample Sharpe clears a bar,
weighted by inverse in-sample volatility. Selection only ever sees the in-sample period; the holdout shows what that
choice was worth afterwards. The frozen final holdout (2026 on) stays locked: every loader here cuts it off.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.data.binance_archive import base_asset, kraken_perp_bases, load_panel
from src.research.hypotheses import h2_liquidation, h3_vrp
from src.research.portfolio import PortfolioCosts, cross_sectional_ic, liquid_universe, rank_weights, simulate_portfolio, slippage_by_liquidity

START = pd.Timestamp("2020-06-01", tz="UTC")
HOLDOUT = pd.Timestamp("2024-01-01", tz="UTC")  # sleeves are chosen on data before this, judged on data after it
DAYS = 365.0


@dataclass(slots=True)
class Sleeve:
    """One idea as a daily net return series for 1x of equity."""

    name: str
    returns: pd.Series
    description: str
    tradable: bool = True  # False: a theoretical series (missing costs or instruments), shown but never auto-selected
    note: str = ""
    parts: dict[str, pd.Series] | None = None  # cross-sectional sleeves: daily price P&L, funding received and costs (negative)


def metrics(returns: pd.Series, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict[str, float]:
    """Yearly return, volatility, Sharpe and worst drawdown of a daily return series over [start, end)."""
    part = returns[(returns.index >= (start or returns.index[0])) & (returns.index < (end or returns.index[-1] + pd.Timedelta(days=1)))].dropna()
    if len(part) < 30 or part.std() == 0:
        return {"return": float("nan"), "vol": float("nan"), "sharpe": float("nan"), "max_drawdown": float("nan"), "days": float(len(part))}
    equity = (1.0 + part).cumprod()
    return {"return": float(part.mean() * DAYS), "vol": float(part.std() * np.sqrt(DAYS)), "sharpe": float(part.mean() / part.std() * np.sqrt(DAYS)),
            "max_drawdown": float((1.0 - equity / equity.cummax()).max()), "days": float(len(part))}


# --- cross-sectional sleeves (Binance perps, the coins Kraken lists) ----------------------------------------------------

LISTINGS = Path("research/data/kraken_perp_listings.csv")


def snapshot_kraken_listings(path: str | Path = LISTINGS) -> pd.DataFrame:
    """Write every Kraken Futures linear perp with the day it opened (one public request), for a point-in-time coin list."""
    from src.data.kraken_futures import fetch_instruments

    rows = [{"symbol": str(item["symbol"]), "base": "BTC" if str(item["symbol"])[3:-3] == "XBT" else str(item["symbol"])[3:-3], "opening_date": str(item["openingDate"])[:10]}
            for item in fetch_instruments() if str(item.get("symbol", "")).startswith("PF_") and str(item["symbol"]).endswith("USD") and item.get("openingDate")]
    frame = pd.DataFrame(rows).sort_values(["opening_date", "symbol"]).reset_index(drop=True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return frame


def kraken_opening_dates(path: str | Path = LISTINGS) -> dict[str, pd.Timestamp]:
    """Coin (as `base_asset` names it, e.g. PEPE for 1000PEPEUSDT) -> the day Kraken opened its perp, from the stored snapshot."""
    opened: dict[str, pd.Timestamp] = {}
    for row in pd.read_csv(path).itertuples():
        day = pd.Timestamp(row.opening_date, tz="UTC")
        for key in {row.base, base_asset(f"{row.base}USDT")}:
            opened[key] = min(day, opened.get(key, day))
    return opened


def load_universe(*, top_n: int = 50, kraken_only: bool = True, listing: str = "today") -> dict[str, pd.DataFrame]:
    """Daily close, volume, taker-buy volume and funding per coin, plus `universe` (the tradable coins each day).

    `kraken_only` keeps the coins Kraken Futures lists, since those are what could be traded. With `listing="today"`
    that is today's list (one public request), which looks ahead: a coin is in the past universe before Kraken had
    it. `listing="point_in_time"` admits a coin only from the day Kraken opened its perp (`LISTINGS`, a stored
    snapshot). Perps Kraken has since delisted are in neither list; that remaining bias can't be removed from
    public data.
    """
    klines = load_panel("klines_1d")
    if klines.empty:
        raise RuntimeError("No Binance archive cached: python scripts/research/cross_sectional_study.py --download (on wifi)")
    wide = {column: klines.pivot_table(index="date", columns="symbol", values=column) for column in ("close", "quote_volume", "taker_buy_quote_volume")}
    traded = wide["quote_volume"] > 0
    wide["close"], wide["quote_volume"] = wide["close"].where(traded), wide["quote_volume"].where(traded)
    funding = load_panel("funding")
    wide["funding"] = funding.pivot_table(index="date", columns="symbol", values="funding").reindex_like(wide["close"])
    universe = liquid_universe(wide["quote_volume"], top_n=top_n)
    if kraken_only and listing == "point_in_time":
        opened = kraken_opening_dates()
        for symbol in universe.columns:
            day = opened.get(base_asset(symbol))
            universe[symbol] = universe[symbol] & (universe.index > day) if day is not None else False  # tradable from the day after it opened
    elif kraken_only:
        listed = kraken_perp_bases()
        universe.loc[:, [symbol for symbol in universe.columns if base_asset(symbol) not in listed]] = False
    wide["universe"] = universe
    return wide


def features(wide: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """The ranking scores, each from data up to that day's close."""
    close, volume = wide["close"], wide["quote_volume"]
    return {"funding_7d": wide["funding"].rolling(7, min_periods=5).mean(), "mom_30d": close / close.shift(30) - 1.0,
            "taker_buy_share_7d": wide["taker_buy_quote_volume"].rolling(7, min_periods=5).sum() / volume.rolling(7, min_periods=5).sum() - 0.5}


def in_sample_sign(score: pd.DataFrame, wide: Mapping[str, pd.DataFrame]) -> float:
    """+1 or -1: whether a high score went with a high next-7-day return before the holdout."""
    forward = wide["close"].shift(-7) / wide["close"] - 1.0
    ic = cross_sectional_ic(score, forward, wide["universe"])
    return float(np.sign(ic[(ic.index >= START) & (ic.index < HOLDOUT - pd.Timedelta(days=7))].mean()) or 1.0)


def cross_sectional_sleeve(name: str, score: pd.DataFrame, wide: Mapping[str, pd.DataFrame], *, description: str, quantile: float = 0.2, rebalance_days: int = 7,
                           note: str = "", cost_multiplier: float = 1.0, start: pd.Timestamp = START) -> Sleeve:
    """Long the top `quantile` of the universe by `score`, short the bottom, dollar-neutral at 1x gross, with fees, slippage and funding."""
    weights = rank_weights(score, wide["universe"], quantile=quantile, gross=1.0)
    costs = PortfolioCosts(fee_pct=0.05 * cost_multiplier, slippage_bps=slippage_by_liquidity(wide["quote_volume"]) * cost_multiplier)
    result = simulate_portfolio(wide["close"], weights, funding=wide["funding"], costs=costs, rebalance_every=rebalance_days)
    returns = result.returns[result.returns.index >= start]
    # the result stores funding and costs as amounts paid
    parts = {label: series[series.index >= start] for label, series in (("price", result.long_pnl + result.short_pnl), ("funding received", -result.funding), ("costs", -result.costs))}
    drags = ", ".join(f"{label} {float(series.mean() * DAYS):+.1%}" for label, series in parts.items())
    return Sleeve(name, returns, description, note=(note + " " if note else "") + f"Per year: {drags}.", parts=parts)


def buffered_rank_weights(score: pd.DataFrame, universe: pd.DataFrame, *, entry: float = 0.2, exit: float = 0.35, rebalance_days: int = 10, min_names: int = 10,
                          start: pd.Timestamp = START) -> pd.DataFrame:
    """Long/short weights with a buffer on the ranking, decided every `rebalance_days` from `start` and held in between.

    A coin enters the long side when its score ranks in the top `entry` share of the universe and stays until it
    is out of the top `exit` share (or out of the universe); the short side mirrors it at the bottom. With
    `exit == entry` this is the plain rule (in whenever in the top `entry`). Equal weight within a side, half long
    and half short, 1x gross; flat on a rebalance day with fewer than `min_names` coins. Each decision uses that
    day's close only.
    """
    weights = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    longs: set[str] = set()
    shorts: set[str] = set()
    current = pd.Series(0.0, index=score.columns)
    first = int(score.index.searchsorted(start))
    for position in range(first, len(score.index)):
        if (position - first) % rebalance_days == 0:
            ranked = score.iloc[position].where(universe.iloc[position]).dropna()
            if len(ranked) < min_names:
                longs, shorts = set(), set()
            else:
                pct = ranked.rank(pct=True, method="first")
                longs = {coin for coin in longs if coin in pct.index and pct[coin] > 1.0 - exit} | set(pct.index[pct > 1.0 - entry])
                shorts = {coin for coin in shorts if coin in pct.index and pct[coin] <= exit} | set(pct.index[pct <= entry])
                both = longs & shorts  # only possible in a tiny universe: the newer rank decides
                longs -= {coin for coin in both if pct[coin] <= 0.5}
                shorts -= {coin for coin in both if pct[coin] > 0.5}
            current = pd.Series(0.0, index=score.columns)
            if longs and shorts:
                current[sorted(longs)] = 0.5 / len(longs)
                current[sorted(shorts)] = -0.5 / len(shorts)
        weights.iloc[position] = current
    return weights


def carry_sleeve(wide: Mapping[str, pd.DataFrame], **settings: Any) -> Sleeve:
    """Funding carry: long the coins where longs are paid (or pay least), short the ones where longs pay most."""
    return cross_sectional_sleeve("carry", -features(wide)["funding_7d"], wide,
                                  description="Long the lowest 7-day funding, short the highest; weekly, market-neutral", **settings)


def momentum_sleeve(wide: Mapping[str, pd.DataFrame]) -> Sleeve:
    """30-day momentum, traded in the direction the in-sample data supports (it has been reversal: past winners lag)."""
    score = features(wide)["mom_30d"]
    sign = in_sample_sign(score, wide)
    return cross_sectional_sleeve("momentum", score * sign, wide, note=f"In-sample sign {sign:+.0f} ({'momentum' if sign > 0 else 'reversal'}).",
                                  description="Rank by past 30-day return, direction fixed in-sample; weekly, market-neutral")


def taker_sleeve(wide: Mapping[str, pd.DataFrame], **settings: Any) -> Sleeve:
    """The taker-buy basket (already in the paper book), on the same footing as the others."""
    return cross_sectional_sleeve("taker", features(wide)["taker_buy_share_7d"], wide,
                                  description="Long the coins with the most aggressive buying over 7 days, short the least; weekly", **settings)


# --- event and volatility sleeves ----------------------------------------------------------------------------------------

def liquidation_sleeve(coins: Sequence[str] = ("BTC", "ETH", "SOL"), *, hold_hours: int = 24, round_trip_bps: float = 20.0) -> Sleeve:
    """Fade liquidation-shaped hours (a 3-sigma move with open interest down 3% and 5x volume), held `hold_hours`.

    The proxy and its entry rule are H2's (research/prereg/H2.txt). Each coin gets an equal share of equity; a
    trade's whole net return is booked on the day it closes.
    """
    daily: dict[str, pd.Series] = {}
    trades = 0
    for coin in coins:
        frame = h2_liquidation.hourly_frame(coin)
        events = h2_liquidation.entries(frame, h2_liquidation.detect_events(frame, h2_liquidation.PRIMARY))
        forward = h2_liquidation.forward_log_returns(frame, hold_hours)
        rows = {}
        for event in events.itertuples():
            if event.entry + hold_hours < len(frame) and np.isfinite(forward[event.entry]):
                closed = frame.index[event.entry + hold_hours].floor("D")
                rows[closed] = rows.get(closed, 0.0) + float(np.expm1(event.direction * forward[event.entry]) - round_trip_bps / 10_000.0)
                trades += 1
        calendar = pd.date_range(frame.index[0].floor("D"), frame.index[-1].floor("D"), freq="D")
        daily[coin] = pd.Series(rows, dtype=float).reindex(calendar).fillna(0.0)
    returns = pd.DataFrame(daily).fillna(0.0).mean(axis=1)
    return Sleeve("liquidation", returns[returns.index >= START], f"Fade forced-selling-shaped hours on {', '.join(coins)}; hold {hold_hours} h",
                  note=f"{trades} trades in total: flat almost every day. A proxy, not real liquidation data.")


def variance_premium_sleeve(coin: str = "BTC", *, vega_share: float = 0.25) -> Sleeve:
    """Sell 30-day implied variance once every 30 days and pay what was realised (theoretical, no costs).

    Each period earns `vega_share` x (iv^2 - rv^2) / (2 iv): a variance swap sized so that one point of volatility
    (0.01) is worth `vega_share` percent of equity. Booked on the day the 30 days end, so nothing looks ahead.
    """
    frame = h3_vrp.vrp_frame(coin).dropna(subset=["iv", "rv_fwd"])
    picks = frame.iloc[::30]
    pnl = vega_share * (picks["iv"] ** 2 - picks["rv_fwd"] ** 2) / (2.0 * picks["iv"])
    pnl.index = pnl.index + pd.Timedelta(days=30)
    calendar = pd.date_range(frame.index[0], pnl.index[-1], freq="D")
    returns = pnl.reindex(calendar).fillna(0.0)
    return Sleeve("variance_premium", returns[returns.index >= START], f"Sell {coin} 30-day implied variance against realised, monthly", tradable=False,
                  note="Theoretical: no option bid/ask, margin or hedging cost, and its losses are far larger than its typical gain.")


def build_sleeves(*, top_n: int = 50) -> dict[str, Sleeve]:
    """Every sleeve, by name."""
    wide = load_universe(top_n=top_n)
    sleeves = [carry_sleeve(wide), momentum_sleeve(wide), taker_sleeve(wide), liquidation_sleeve(), variance_premium_sleeve()]
    return {sleeve.name: sleeve for sleeve in sleeves}


# --- side by side, and combined ------------------------------------------------------------------------------------------

def returns_frame(sleeves: Mapping[str, Sleeve]) -> pd.DataFrame:
    """The sleeves' daily returns on one calendar (a day a sleeve has no data for counts as flat)."""
    frame = pd.DataFrame({name: sleeve.returns for name, sleeve in sleeves.items()})
    frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC") if frame.index.tz is not None else pd.DatetimeIndex(frame.index).tz_localize("UTC")
    return frame.sort_index().fillna(0.0)


def summary(sleeves: Mapping[str, Sleeve] | pd.DataFrame) -> pd.DataFrame:
    """Per sleeve: Sharpe, yearly return, volatility and drawdown, in-sample and holdout."""
    frame = sleeves if isinstance(sleeves, pd.DataFrame) else returns_frame(sleeves)
    rows = {}
    for name in frame.columns:
        inside, after = metrics(frame[name], START, HOLDOUT), metrics(frame[name], HOLDOUT)
        rows[name] = {"sharpe_in_sample": inside["sharpe"], "sharpe_holdout": after["sharpe"], "return_in_sample": inside["return"], "return_holdout": after["return"],
                      "vol_holdout": after["vol"], "max_drawdown_holdout": after["max_drawdown"]}
    return pd.DataFrame(rows).T


def choose(sleeves: Mapping[str, Sleeve], *, min_sharpe: float = 0.5) -> dict[str, float]:
    """The book "it likes": tradable sleeves whose in-sample Sharpe is at least `min_sharpe`, weighted by inverse in-sample volatility."""
    frame = returns_frame(sleeves)
    inside = frame[(frame.index >= START) & (frame.index < HOLDOUT)]
    picked = {name: 1.0 / inside[name].std() for name, sleeve in sleeves.items()
              if sleeve.tradable and inside[name].std() > 0 and metrics(inside[name])["sharpe"] >= min_sharpe}
    total = sum(picked.values())
    return {name: weight / total for name, weight in picked.items()} if total else {}


def combine(sleeves: Mapping[str, Sleeve], weights: Mapping[str, float] | None = None, *, min_sharpe: float = 0.5) -> tuple[dict[str, float], pd.Series]:
    """A book of the sleeves: `weights` as given (a preset, scaled to sum to 1), or `choose`'s pick. Returns (weights, daily returns)."""
    if weights is None:
        weights = choose(sleeves, min_sharpe=min_sharpe)
    unknown = sorted(set(weights) - set(sleeves))
    if unknown:
        raise ValueError(f"no such sleeve: {unknown}; there are {sorted(sleeves)}")
    total = sum(abs(weight) for weight in weights.values())
    if not total:
        return {}, pd.Series(dtype=float)
    scaled = {name: weight / total for name, weight in weights.items()}
    frame = returns_frame(sleeves)
    return scaled, sum(frame[name] * weight for name, weight in scaled.items())


def recorded_coverage(root: str | Path = "data") -> pd.DataFrame:
    """What the live collectors have stored so far for the two ideas that wait on data (file counts only; no prices are read)."""
    root = Path(root)
    rows: list[dict[str, Any]] = []
    for label, pattern in (("Binance liquidations", "market_data/binance/liquidations/*.parquet"), ("Bybit liquidations", "market_data/bybit/liquidations/*.parquet"),
                           ("Deribit BTC option chains", "options/deribit/BTC/*/*.parquet"), ("Deribit ETH option chains", "options/deribit/ETH/*/*.parquet")):
        files = sorted(root.glob(pattern))
        days = sorted({path.stem if "liquidations" in pattern else path.parent.name for path in files})
        rows.append({"data": label, "files": len(files), "days": len(days), "first": days[0] if days else None, "last": days[-1] if days else None,
                     "megabytes": round(sum(path.stat().st_size for path in files) / 1e6, 2)})
    return pd.DataFrame(rows).set_index("data")
