"""Equity index futures and currencies on daily data: the returns, signals and book of hypothesis H11.

Everything here is research on free data (index closes, central-bank fixings, interbank rates), built so a fund can
judge a proof of concept. A position is a fraction of capital held as futures notional, and a market's return is
what that future earns above cash. Positions decided on day t are traded at the close of t+1 and earn from t+2,
which also covers markets that close at different hours.

    panel = asyncio.run(load_panel())                 # cached under data/research_cache/multi_asset/
    result = run_book(panel)                          # sleeves, sector books and the book, after costs
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import aiohttp
import numpy as np
import pandas as pd

from src.research import governance

MARKETS_PATH = Path("research/data/multi_asset_markets.csv")
CACHE_DIR = Path("data/research_cache/multi_asset")
DATA_START = "1998-01-01"
DAYS_PER_YEAR = 252.0
TARGET_VOL = 0.10
VOL_WINDOW, VOL_FLOOR, COV_WINDOW = 60, 0.02, 126
SHORT_AVERAGE, LONG_AVERAGE, YEAR, SKIP = 4, 48, 252, 21
TRADE_LAG = 2  # decided on t, traded at the close of t+1, earning from t+2
COST_BPS = {"major": 1.5, "minor": 3.0}
ROLLS_PER_YEAR, ROLL_BPS = 4, 1.0
JAPAN_RATE_BEFORE_SOURCE = 0.1  # % a year, before the OECD series starts in 2002-04
HKD_PER_USD = 7.8  # the peg; only used to size Hang Seng contracts in NOK
SECTORS = ("fx", "equity")
SLEEVES = {"fx": ("fx_trend", "fx_carry", "fx_momentum"), "equity": ("equity_trend", "equity_momentum")}
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_NORGES_BANK = "https://data.norges-bank.no/api/data/EXR/B.{currencies}.NOK.SP?format=csv&startPeriod={start}&locale=en&bom=include"
_OECD = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/{areas}.M.IR3TIB.PA._Z._Z._Z._Z.N?startPeriod={start}&format=csvfile"
_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start}&period2={end}&interval=1d"


def load_markets(path: Path | str = MARKETS_PATH) -> pd.DataFrame:
    """The market table, indexed by market: sector, currency, data source, cost class and the contract a fund would trade."""
    return pd.read_csv(path).set_index("market")


# --- parsing what the sources send ------------------------------------------------------------------------------------

def parse_norges_bank(text: str) -> pd.DataFrame:
    """NOK per one unit of each currency, by day, from Norges Bank's CSV (which quotes some currencies per 100 units)."""
    raw = pd.read_csv(io.StringIO(text.lstrip("﻿")), sep=";", dtype=str)
    value = pd.to_numeric(raw["OBS_VALUE"], errors="coerce") / 10.0 ** pd.to_numeric(raw["UNIT_MULT"], errors="coerce")
    frame = pd.DataFrame({"day": pd.to_datetime(raw["TIME_PERIOD"]), "currency": raw["BASE_CUR"], "value": value}).dropna()
    return frame.pivot(index="day", columns="currency", values="value").sort_index()


def parse_oecd_rates(text: str) -> pd.DataFrame:
    """3-month interbank rates in % a year, by month (period index) and OECD area."""
    raw = pd.read_csv(io.StringIO(text))
    frame = raw.pivot(index="TIME_PERIOD", columns="REF_AREA", values="OBS_VALUE").sort_index()
    frame.index = pd.PeriodIndex(frame.index, freq="M")
    return frame.astype(float)


def parse_yahoo_closes(payload: Mapping[str, Any]) -> pd.Series:
    """Daily closes indexed by the exchange's local date."""
    result = payload["chart"]["result"][0]
    offset = int(result["meta"].get("gmtoffset", 0))
    days = pd.to_datetime(np.asarray(result["timestamp"], dtype="int64") + offset, unit="s").normalize()
    closes = pd.Series(result["indicators"]["quote"][0]["close"], index=days, dtype=float).dropna()
    return closes[~closes.index.duplicated(keep="last")].sort_index()


# --- fetching ----------------------------------------------------------------------------------------------------------

async def _get(session: aiohttp.ClientSession, url: str, *, json: bool = False) -> Any:
    async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=180)) as response:
        response.raise_for_status()
        return await response.json(content_type=None) if json else await response.text()


async def fetch_raw(markets: pd.DataFrame, *, start: str = DATA_START) -> dict[str, pd.DataFrame]:
    """Download fixings, rates and index closes. Any source failing raises: a panel with a missing market is no test."""
    currencies = "+".join(sorted({"USD", *markets.loc[markets["sector"] == "fx", "price_symbol"]} - {"NOK"}))
    areas = "+".join(sorted({"USA", *markets["rate_area"]}))
    equity = markets[markets["sector"] == "equity"]
    first, last = int(pd.Timestamp(start).timestamp()), int(pd.Timestamp.now(tz="UTC").timestamp())
    async with aiohttp.ClientSession() as session:
        fx_text, rate_text, *charts = await asyncio.gather(
            _get(session, _NORGES_BANK.format(currencies=currencies, start=start)),
            _get(session, _OECD.format(areas=areas, start=start[:7])),
            *(_get(session, _YAHOO.format(symbol=quote(symbol), start=first, end=last), json=True) for symbol in equity["price_symbol"]))
    closes = pd.DataFrame({market: parse_yahoo_closes(chart) for market, chart in zip(equity.index, charts)}).sort_index()
    return {"fx_nok": parse_norges_bank(fx_text), "rates": parse_oecd_rates(rate_text), "equity": closes}


async def load_raw(markets: pd.DataFrame, *, cache_dir: Path | str = CACHE_DIR, refresh: bool = False) -> dict[str, pd.DataFrame]:
    """The raw series from the cache, downloading once (or again with `refresh`)."""
    cache = Path(cache_dir)
    paths = {name: cache / f"{name}.csv" for name in ("fx_nok", "rates", "equity")}
    if refresh or not all(path.exists() for path in paths.values()):
        raw = await fetch_raw(markets)
        cache.mkdir(parents=True, exist_ok=True)
        for name, frame in raw.items():
            frame.to_csv(paths[name])
    raw = {name: pd.read_csv(path, index_col=0) for name, path in paths.items()}
    raw["rates"].index = pd.PeriodIndex(raw["rates"].index, freq="M")
    for name in ("fx_nok", "equity"):
        raw[name].index = pd.to_datetime(raw[name].index)
    return raw


# --- the panel: what each market earns above cash ----------------------------------------------------------------------

@dataclass(slots=True)
class Panel:
    """Daily data on one Monday-to-Friday calendar.

    Attributes:
        markets: The market table.
        returns: What a futures position in each market earns above cash, per day.
        prices: The index level (equities) or the dollar price of the currency.
        rates: Each market's own 3-month rate as a fraction per year, known the month before.
        usd_rate: The US rate on the same footing.
        nok_per: NOK per unit of each market's contract currency, to size whole contracts.
    """

    markets: pd.DataFrame
    returns: pd.DataFrame
    prices: pd.DataFrame
    rates: pd.DataFrame
    usd_rate: pd.Series
    nok_per: pd.DataFrame

    def sector(self, name: str) -> list[str]:
        """The markets of one sector, in table order."""
        return [market for market in self.markets.index[self.markets["sector"] == name] if market in self.returns.columns]


def daily_rates(monthly: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Monthly rates in % as daily fractions, each month's value used from the following month on (no look-ahead)."""
    known = monthly.shift(1)
    known.index = known.index.to_timestamp()
    return known.reindex(known.index.union(days)).ffill().reindex(days) / 100.0


def build_panel(markets: pd.DataFrame, raw: Mapping[str, pd.DataFrame]) -> Panel:
    """Put fixings, rates and closes on one weekday calendar and compute each market's return above cash.

    A currency future earns the spot move plus the interest difference against the dollar. An index future earns
    the index move plus the dividend yield minus the local interest rate. A day without a price is a zero return.
    """
    fx_nok, closes = raw["fx_nok"], raw["equity"]
    days = pd.bdate_range(max(fx_nok.index.min(), closes.index.min()), max(fx_nok.index.max(), closes.index.max()))
    fx_nok = fx_nok.reindex(fx_nok.index.union(days)).ffill().reindex(days)
    closes = closes.reindex(closes.index.union(days)).ffill().reindex(days)

    monthly = raw["rates"].copy()
    if "JPN" in monthly:
        first = monthly["JPN"].first_valid_index()
        monthly.loc[monthly.index < first, "JPN"] = JAPAN_RATE_BEFORE_SOURCE
    by_area = daily_rates(monthly, days)
    usd_rate = by_area["USA"]

    fx, equity = markets.index[markets["sector"] == "fx"], markets.index[markets["sector"] == "equity"]
    nok_per_currency = fx_nok.assign(NOK=1.0, HKD=fx_nok["USD"] / HKD_PER_USD)
    usd_price = pd.DataFrame({market: nok_per_currency[markets.at[market, "price_symbol"]] / fx_nok["USD"] for market in fx})
    prices = pd.concat([closes[list(equity)], usd_price], axis=1)[list(markets.index)]
    rates = pd.DataFrame({market: by_area[markets.at[market, "rate_area"]] for market in markets.index})

    carry = pd.DataFrame(index=days, columns=markets.index, dtype=float)
    for market in fx:
        carry[market] = (rates[market] - usd_rate) / DAYS_PER_YEAR
    for market in equity:
        carry[market] = (float(markets.at[market, "dividend_yield"]) - rates[market]) / DAYS_PER_YEAR
    returns = prices.pct_change(fill_method=None) + carry
    returns = returns.where(prices.notna() & prices.shift(1).notna())  # no return before a market's first price
    nok_per = pd.DataFrame({market: nok_per_currency[markets.at[market, "contract_currency"]] for market in markets.index})
    return Panel(markets=markets, returns=returns, prices=prices, rates=rates, usd_rate=usd_rate, nok_per=nok_per)


async def load_panel(*, markets_path: Path | str = MARKETS_PATH, cache_dir: Path | str = CACHE_DIR, refresh: bool = False) -> Panel:
    """The research panel without the frozen final holdout (see `governance.final_holdout`)."""
    markets = load_markets(markets_path)
    panel = build_panel(markets, await load_raw(markets, cache_dir=cache_dir, refresh=refresh))
    kept = governance.trim_frame(panel.returns, label="multi-asset daily panel").index
    return slice_panel(panel, kept)


def slice_panel(panel: Panel, days: pd.DatetimeIndex) -> Panel:
    """The same panel on a subset of its days."""
    return Panel(markets=panel.markets, returns=panel.returns.loc[days], prices=panel.prices.loc[days], rates=panel.rates.loc[days],
                 usd_rate=panel.usd_rate.loc[days], nok_per=panel.nok_per.loc[days])


# --- signals -----------------------------------------------------------------------------------------------------------

def return_index(returns: pd.DataFrame) -> pd.DataFrame:
    """Cumulative return index per market (1.0 before its first return)."""
    return (1.0 + returns.fillna(0.0)).cumprod().where(returns.notna().cummax())


def trend_signal(index: pd.DataFrame) -> pd.DataFrame:
    """The average of two signs: the 4-day average against the 48-day average, and the last 252 days' return."""
    crossover = np.sign(index.rolling(SHORT_AVERAGE).mean() - index.rolling(LONG_AVERAGE).mean())
    year = np.sign(index / index.shift(YEAR) - 1.0)
    return (crossover + year) / 2.0


def momentum_score(index: pd.DataFrame) -> pd.DataFrame:
    """The return from 252 days ago to 21 days ago (the last month is skipped, as it tends to reverse)."""
    return index.shift(SKIP) / index.shift(YEAR) - 1.0


def rank_weights(scores: pd.DataFrame) -> pd.DataFrame:
    """Long the high scores, short the low ones: rank minus the average rank, absolute weights summing to 1 per day."""
    ranks = scores.rank(axis=1)
    centred = ranks.sub(ranks.mean(axis=1), axis=0)
    total = centred.abs().sum(axis=1)
    return centred.div(total.where(total > 0), axis=0)


def market_vol(returns: pd.DataFrame) -> pd.DataFrame:
    """Yearly volatility from the last 60 days, floored so a quiet market cannot get an unbounded position."""
    return (returns.rolling(VOL_WINDOW).std() * np.sqrt(DAYS_PER_YEAR)).clip(lower=VOL_FLOOR)


def raw_sleeves(panel: Panel) -> dict[str, pd.DataFrame]:
    """Each sleeve's unscaled position per market and day (zero outside its sector), from data up to that day."""
    index, vol = return_index(panel.returns), market_vol(panel.returns)
    fx, equity = panel.sector("fx"), panel.sector("equity")
    trend = trend_signal(index)
    momentum = momentum_score(index)
    carry = panel.rates[fx].sub(panel.usd_rate, axis=0).where(panel.returns[fx].notna())
    parts = {"fx_trend": trend[fx] / len(fx), "fx_carry": rank_weights(carry), "fx_momentum": rank_weights(momentum[fx]),
             "equity_trend": trend[equity] / len(equity), "equity_momentum": rank_weights(momentum[equity])}
    return {name: (part / vol[part.columns]).reindex(columns=panel.returns.columns).fillna(0.0) for name, part in parts.items()}


# --- sizing and the book -----------------------------------------------------------------------------------------------

def rebalance_days(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The last trading day of each week."""
    week = days.to_period("W")
    last = np.append(week[1:] != week[:-1], True)
    return days[last]


def scale_to_vol(position: np.ndarray, covariance: np.ndarray, *, target: float = TARGET_VOL) -> np.ndarray:
    """`position` scaled so its expected yearly volatility under `covariance` is `target` (unchanged if it is flat)."""
    variance = float(position @ covariance @ position)
    return position * (target / np.sqrt(variance)) if variance > 0 else position


def book_positions(panel: Panel, sleeves: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Positions on each rebalance day for every sleeve, each sector book and the book, all at the target volatility.

    A sector book is the average of its sleeves, and the book the average of the sector books, each scaled again:
    averaging things that are not perfectly correlated lowers the volatility.
    """
    returns = panel.returns.fillna(0.0)
    days = [day for day in rebalance_days(returns.index) if returns.index.get_loc(day) >= YEAR]
    names = [*sleeves, *SECTORS, "book"]
    rows: dict[str, list[np.ndarray]] = {name: [] for name in names}
    for day in days:
        end = returns.index.get_loc(day) + 1
        covariance = np.cov(returns.iloc[end - COV_WINDOW:end].to_numpy(), rowvar=False) * DAYS_PER_YEAR
        scaled = {name: scale_to_vol(frame.loc[day].to_numpy(), covariance) for name, frame in sleeves.items()}
        sectors = {sector: scale_to_vol(np.mean([scaled[name] for name in SLEEVES[sector]], axis=0), covariance) for sector in SECTORS}
        book = scale_to_vol(np.mean([sectors[sector] for sector in SECTORS], axis=0), covariance)
        for name, value in {**scaled, **sectors, "book": book}.items():
            rows[name].append(value)
    return {name: pd.DataFrame(rows[name], index=pd.DatetimeIndex(days), columns=returns.columns) for name in names}


def per_side_cost(markets: pd.DataFrame, columns: Sequence[str], *, multiplier: float = 1.0) -> pd.Series:
    """Cost per side as a fraction of the amount traded, per market."""
    return pd.Series({market: COST_BPS[markets.at[market, "liquid"]] * multiplier / 1e4 for market in columns})


@dataclass(slots=True)
class Performance:
    """Daily returns of one set of positions.

    Attributes:
        gross: Before costs.
        net: After trading and roll costs.
        held: The positions held each day (decided two days earlier at the latest).
        turnover: Amount traded per day as a fraction of capital.
    """

    gross: pd.Series
    net: pd.Series
    held: pd.DataFrame
    turnover: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def performance(panel: Panel, positions: pd.DataFrame, *, cost_multiplier: float = 1.0) -> Performance:
    """Returns of `positions` (rows on rebalance days) held weekly: traded at the next close, earning the day after."""
    returns = panel.returns.fillna(0.0)
    held = positions.reindex(returns.index).ffill().shift(TRADE_LAG).fillna(0.0)
    held = held[held.index >= positions.index[0]]
    returns = returns.loc[held.index]
    traded = held.diff().abs().fillna(held.abs())
    costs = (traded * per_side_cost(panel.markets, held.columns, multiplier=cost_multiplier)).sum(axis=1)
    costs += held.abs().sum(axis=1) * ROLLS_PER_YEAR * ROLL_BPS * cost_multiplier / 1e4 / DAYS_PER_YEAR
    gross = (held * returns).sum(axis=1)
    return Performance(gross=gross, net=gross - costs, held=held, turnover=traded.sum(axis=1))


def contract_notional_nok(panel: Panel) -> pd.DataFrame:
    """What one contract of each market is worth in NOK each day: size x index level (equities) x the currency in NOK."""
    size = panel.markets["contract_size"].astype(float)
    level = panel.prices.copy()
    level[panel.sector("fx")] = 1.0  # a currency contract is a fixed amount of that currency
    return level * size * panel.nok_per


def whole_contracts(panel: Panel, positions: pd.DataFrame, *, capital_nok: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(`positions` rounded to whole contracts for `capital_nok`, the contract counts)."""
    notional = contract_notional_nok(panel).reindex(positions.index).ffill()
    contracts = (positions * capital_nok / notional).round()
    return (contracts * notional / capital_nok).fillna(0.0), contracts.fillna(0.0)


@dataclass(slots=True)
class BookResult:
    """Everything the study reports: positions on rebalance days and the returns of each sleeve, sector book and the book."""

    positions: dict[str, pd.DataFrame]
    results: dict[str, Performance]


def run_book(panel: Panel, *, cost_multiplier: float = 1.0) -> BookResult:
    """The pre-registered book on `panel`."""
    positions = book_positions(panel, raw_sleeves(panel))
    return BookResult(positions=positions, results={name: performance(panel, frame, cost_multiplier=cost_multiplier) for name, frame in positions.items()})


# --- statistics --------------------------------------------------------------------------------------------------------

def sharpe(returns: Sequence[float] | pd.Series) -> float:
    """Yearly Sharpe ratio of daily returns above cash."""
    values = np.asarray(returns, dtype=float)
    spread = values.std(ddof=1) if len(values) > 1 else 0.0
    return float(values.mean() / spread * np.sqrt(DAYS_PER_YEAR)) if spread > 0 else float("nan")


def sortino(returns: Sequence[float] | pd.Series) -> float:
    """Yearly Sortino ratio: like Sharpe, but only losses count as risk."""
    values = np.asarray(returns, dtype=float)
    downside = np.sqrt(np.mean(np.minimum(values, 0.0) ** 2)) if len(values) else 0.0
    return float(values.mean() / downside * np.sqrt(DAYS_PER_YEAR)) if downside > 0 else float("nan")


def max_drawdown(returns: pd.Series) -> float:
    """The largest fall from a peak of the compounded return, as a negative fraction."""
    wealth = (1.0 + returns).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min()) if len(wealth) else float("nan")


def summary(returns: pd.Series) -> dict[str, float]:
    """The headline numbers of a daily return series."""
    return {"yearly return": float(returns.mean() * DAYS_PER_YEAR), "volatility": float(returns.std(ddof=1) * np.sqrt(DAYS_PER_YEAR)),
            "Sharpe": sharpe(returns), "Sortino": sortino(returns), "worst drawdown": max_drawdown(returns)}


def monthly(returns: pd.Series) -> pd.Series:
    """Compounded monthly returns."""
    return (1.0 + returns).groupby(returns.index.to_period("M")).prod() - 1.0
