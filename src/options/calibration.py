"""Fit any pricing model to an option chain, measured in implied-volatility points.

The error that matters is in vol points, not in price percent. A 5% miss on a
cheap far out-of-the-money option is a tiny vol difference, while a 1% miss
at the money can be several vol points. So the objective divides each price
error by the option's vega (price error / vega ~ IV error) and minimises the
mean square. That is how quotes and model risk are compared on a desk.

`market_quotes` turns a chain snapshot (`src/options/deribit.py`) into clean
quotes: it uses mids where both sides exist, drops quotes with wide spreads
or no open interest, and adds each quote's market IV and vega.
`calibrate(build, parameters, quotes)` works for any `PricingModel`: `build`
turns a parameter dict into a model. `iv_errors` reports the fit quote by
quote, so a bad fit shows where it fails (short expiries, the put wing, ...).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from src.options.pricing import Black76, PricingModel, implied_vol

MIN_VEGA_SHARE = 1e-4  # quotes with vega below this share of the forward carry no vol information


@dataclass(frozen=True, slots=True)
class Parameter:
    """One calibrated parameter: a starting value and bounds."""

    name: str
    initial: float
    low: float
    high: float


@dataclass(slots=True)
class CalibrationResult:
    """The fitted model, its parameters and how well it fits (IV RMSE in vol points, quote by quote)."""

    model: PricingModel
    params: dict[str, float]
    iv_rmse: float
    errors: pd.DataFrame
    evaluations: int
    seconds: float
    success: bool
    message: str
    extra: dict[str, Any] = field(default_factory=dict)


def market_quotes(chain: pd.DataFrame, *, price: str = "mid", max_relative_spread: float = 0.5, min_open_interest: float = 0.0,
                  moneyness: tuple[float, float] = (0.5, 2.0), min_days: float = 1.0, out_of_the_money: bool = True) -> pd.DataFrame:
    """Clean quotes from one chain snapshot, with market IV and Black-76 vega added.

    Args:
        price: Which column to fit: "mid" (needs a bid and an ask) or "mark".
        max_relative_spread: Drop quotes whose (ask - bid) / mid is wider.
        moneyness: Keep strikes with K / F inside this range.
        out_of_the_money: Keep only out-of-the-money options (puts below the
            forward, calls above). They are the liquid side, and by put-call
            parity they carry the same information.
    """
    quotes = chain.copy()
    quotes["mid"] = (quotes["bid"] + quotes["ask"]) / 2.0
    quotes["market_price"] = quotes[price]
    spread = (quotes["ask"] - quotes["bid"]) / quotes["mid"]
    keep = quotes["market_price"].notna() & (quotes["market_price"] > 0) & (quotes["t"] * 365 >= min_days)
    keep &= quotes["strike"].between(quotes["forward"] * moneyness[0], quotes["forward"] * moneyness[1])
    if price == "mid":
        keep &= spread.le(max_relative_spread)
    if min_open_interest > 0:
        keep &= quotes["open_interest"] >= min_open_interest
    if out_of_the_money:
        keep &= ((quotes["right"] == "put") & (quotes["strike"] < quotes["forward"])) | ((quotes["right"] == "call") & (quotes["strike"] >= quotes["forward"]))
    quotes = quotes[keep].copy()
    quotes["market_iv"] = [implied_vol(row.market_price, row.forward, row.strike, row.t, row.right, row.discount) for row in quotes.itertuples()]
    quotes = quotes[np.isfinite(quotes["market_iv"])]
    quotes["vega"] = [_vega(row.forward, row.strike, row.t, row.market_iv, row.discount) for row in quotes.itertuples()]
    quotes = quotes[quotes["vega"] > MIN_VEGA_SHARE * quotes["forward"]]
    return quotes.sort_values(["t", "strike"]).reset_index(drop=True)


def _vega(forward: float, strike: float, t: float, sigma: float, discount: float) -> float:
    spread = sigma * math.sqrt(t)
    d1 = (math.log(forward / strike) + 0.5 * spread**2) / spread
    return discount * forward * math.sqrt(t) * math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)


def iv_errors(model: PricingModel, quotes: pd.DataFrame) -> pd.DataFrame:
    """Model price, model IV and the IV error (model - market, in vol points) for every quote."""
    out = quotes[["instrument", "expiry", "t", "strike", "right", "forward", "market_price", "market_iv"]].copy() if "instrument" in quotes else quotes.copy()
    prices = [model.price(row.forward, row.strike, row.t, row.right, row.discount) for row in quotes.itertuples()]
    out["model_price"] = prices
    out["model_iv"] = [implied_vol(price, row.forward, row.strike, row.t, row.right, row.discount) for price, row in zip(prices, quotes.itertuples())]
    out["iv_error_points"] = (out["model_iv"] - out["market_iv"]) * 100
    out["price_error_pct"] = (out["model_price"] / out["market_price"] - 1) * 100
    return out


def calibrate(
    build: Callable[[dict[str, float]], PricingModel],
    parameters: Sequence[Parameter],
    quotes: pd.DataFrame,
    *,
    method: str = "L-BFGS-B",
    maxiter: int = 200,
) -> CalibrationResult:
    """Fit `build(params)` to `quotes` by minimising the mean squared vega-scaled price error (about the IV error squared).

    Works for any model; a slow one (a PDE) just takes longer. Bounds keep
    the search inside sensible ranges, and a parameter that ends on a bound
    usually means the model can't fit the market that way.
    """
    names = [parameter.name for parameter in parameters]
    bounds = [(parameter.low, parameter.high) for parameter in parameters]
    rows = list(quotes.itertuples())
    market = quotes["market_price"].to_numpy()
    vega = quotes["vega"].to_numpy()
    evaluations = 0

    def objective(vector: np.ndarray) -> float:
        nonlocal evaluations
        evaluations += 1
        model = build(dict(zip(names, map(float, vector))))
        prices = np.array([model.price(row.forward, row.strike, row.t, row.right, row.discount) for row in rows])
        errors = (prices - market) / vega
        return float(np.mean(errors**2)) if np.all(np.isfinite(errors)) else 1e6

    started = time.time()
    options = {"maxiter": maxiter} if method != "Nelder-Mead" else {"maxiter": maxiter, "xatol": 1e-4, "fatol": 1e-8}
    result = minimize(objective, np.array([parameter.initial for parameter in parameters]), method=method, bounds=bounds, options=options)
    params = dict(zip(names, map(float, result.x)))
    model = build(params)
    errors = iv_errors(model, quotes)
    at_bounds = [name for name, value, (low, high) in zip(names, result.x, bounds) if math.isclose(value, low, rel_tol=1e-6, abs_tol=1e-9) or math.isclose(value, high, rel_tol=1e-6, abs_tol=1e-9)]
    return CalibrationResult(model=model, params=params, iv_rmse=float(np.sqrt(np.nanmean(errors["iv_error_points"] ** 2))), errors=errors,
                             evaluations=evaluations, seconds=time.time() - started, success=bool(result.success), message=str(result.message),
                             extra={"parameters_at_bounds": at_bounds})


def black76_per_expiry(quotes: pd.DataFrame) -> dict[float, float]:
    """The single volatility per expiry that fits best (a flat smile): the simplest baseline any model must beat."""
    out = {}
    for t, group in quotes.groupby("t"):
        fit = calibrate(lambda params: Black76(params["sigma"]), [Parameter("sigma", float(group["market_iv"].median()), 0.05, 3.0)], group)
        out[float(t)] = fit.params["sigma"]
    return out
