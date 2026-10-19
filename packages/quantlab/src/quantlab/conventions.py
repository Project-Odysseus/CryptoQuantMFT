"""The lab's conventions as code (plan 0.4; the reasoning is in CONVENTIONS.md).

Every component builds on these, so they are fixed in one place and tested:

- **Time.** A bar is indexed by the moment its data becomes final: a daily bar by its exchange-local session date,
  an intraday bar by its close time in UTC. A decision at bar t uses data up to and including bar t.
- **Execution.** A decision at bar t trades at the next tradable price (`Execution.NEXT_CLOSE` by default: the close
  of bar t+1, so it earns from t+2), never at the price that produced it.
- **Returns.** Log returns for features and statistics; simple returns for P&L (they add across instruments).
  Futures P&L is the excess return over cash (a future is funded by margin).
- **Positions.** Signed weight of equity in the base currency: notional / equity. Contracts and lots are derived at
  the portfolio/execution layer.
- **Signals.** A forecast scaled so its long-run standard deviation is 1, capped at +/-`FORECAST_CAP`. Sign = direction.
- **Regimes.** Per-bar probabilities over named states, each row summing to 1.
"""

from __future__ import annotations

import math
from enum import Enum

import numpy as np
import pandas as pd

FORECAST_CAP = 2.0
TRADING_DAYS = 252  # exchange calendars
CALENDAR_DAYS = 365  # markets that trade every day (crypto)


class Execution(str, Enum):
    """When a decision made at the close of bar t is traded."""

    NEXT_CLOSE = "next_close"  # the close of t+1 (default: matches a fund trading at the close)
    NEXT_OPEN = "next_open"  # the open of t+1


class PositionUnit(str, Enum):
    """What a position number means."""

    WEIGHT = "weight"  # notional / equity, base currency (the engine's unit)
    CONTRACTS = "contracts"
    NOTIONAL = "notional"  # base currency


class Calendar(str, Enum):
    """How many periods a year a bar interval has."""

    EXCHANGE = "exchange"  # weekdays minus holidays: 252 days
    ALWAYS = "always"  # 24/7: 365 days


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400}
EXCHANGE_HOURS_PER_DAY = 6.5  # intraday periods on an exchange calendar count only the regular session


def periods_per_year(interval: str, calendar: Calendar | str = Calendar.EXCHANGE) -> float:
    """Bars a year for annualising: 252 daily bars on an exchange calendar, 365 x bars-per-day on a 24/7 one."""
    calendar = Calendar(calendar)
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unknown interval {interval!r}; use one of {list(INTERVAL_SECONDS)}")
    seconds = INTERVAL_SECONDS[interval]
    if calendar is Calendar.ALWAYS:
        return CALENDAR_DAYS * 86_400 / seconds
    if seconds >= 86_400:
        return TRADING_DAYS * 86_400 / seconds
    return TRADING_DAYS * EXCHANGE_HOURS_PER_DAY * 3600 / seconds


def log_returns(prices: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """log(p_t / p_{t-1}); NaN for the first bar and across missing prices."""
    return np.log(prices / prices.shift(1))


def simple_returns(prices: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """p_t / p_{t-1} - 1; NaN for the first bar and across missing prices."""
    return prices / prices.shift(1) - 1.0


def to_simple(log_return: float | np.ndarray | pd.Series) -> float | np.ndarray | pd.Series:
    """exp(r) - 1."""
    return np.expm1(log_return)


def to_log(simple_return: float | np.ndarray | pd.Series) -> float | np.ndarray | pd.Series:
    """log(1 + r)."""
    return np.log1p(simple_return)


def excess_return(price_return: pd.Series, *, cash_rate: pd.Series | float = 0.0, carry: pd.Series | float = 0.0,
                  periods: float = TRADING_DAYS) -> pd.Series:
    """What a futures position earns above cash per period: price return + (carry - cash rate) / periods.

    `cash_rate` and `carry` are yearly fractions (e.g. a dividend yield for an index proxy; 0 for a real contract,
    whose price already carries it - then pass only what the contract itself does not reflect).
    """
    return price_return + (carry - cash_rate) / periods


def weight_to_contracts(weight: float, *, equity: float, price: float, multiplier: float, fx: float = 1.0) -> float:
    """Contracts (fractional) for a weight: weight x equity / (price x multiplier x fx). Rounding is the caller's."""
    notional_per_contract = price * multiplier * fx
    if notional_per_contract <= 0 or equity <= 0:
        raise ValueError("price, multiplier, fx and equity must be positive")
    return weight * equity / notional_per_contract


def contracts_to_weight(contracts: float, *, equity: float, price: float, multiplier: float, fx: float = 1.0) -> float:
    """The weight held by `contracts`: contracts x price x multiplier x fx / equity."""
    if equity <= 0:
        raise ValueError("equity must be positive")
    return contracts * price * multiplier * fx / equity


def cap_forecast(forecast: pd.Series | pd.DataFrame | np.ndarray, cap: float = FORECAST_CAP) -> pd.Series | pd.DataFrame | np.ndarray:
    """A forecast clipped to +/-cap (the signal convention)."""
    return np.clip(forecast, -cap, cap)


def check_regime_probabilities(probabilities: pd.DataFrame, *, tolerance: float = 1e-6) -> None:
    """Raise unless every complete row of state probabilities is within [0, 1] and sums to 1 (warm-up rows may be NaN)."""
    complete = probabilities.dropna()
    if ((complete < -tolerance) | (complete > 1 + tolerance)).to_numpy().any():
        raise ValueError("regime probabilities must lie in [0, 1]")
    sums = complete.sum(axis=1)
    if len(sums) and not np.allclose(sums.to_numpy(), 1.0, atol=tolerance):
        bad = sums.index[~np.isclose(sums.to_numpy(), 1.0, atol=tolerance)][0]
        raise ValueError(f"regime probabilities must sum to 1 on every bar; {bad} sums to {sums.loc[bad]}")


def annualised_sharpe(returns: pd.Series, periods: float) -> float:
    """Mean over standard deviation of per-period returns, times sqrt(periods); NaN without spread."""
    spread = returns.std()
    return float(returns.mean() / spread * math.sqrt(periods)) if spread and spread > 0 else float("nan")
