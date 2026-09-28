"""How a strategy or book relates to a benchmark (usually BTC buy-and-hold): beta, alpha, tracking error, capture.

A trend book on BTC that returns 40% a year is only interesting if that return isn't simply BTC exposure. These are
the standard ways to separate the two, on returns sampled at the same times (daily, typically):

- beta: covariance with the benchmark over the benchmark's variance (the OLS slope), full period and rolling;
- alpha: the annualised intercept, what is left after beta times the benchmark (and the risk-free rate);
- correlation, tracking error (annualised volatility of the active return) and information ratio (annualised
  active return over tracking error);
- up/down capture: the average return in benchmark-up and benchmark-down periods relative to the benchmark's.
  A trend book should capture less of the downside than the upside.

Crypto trades every day, so the default annualisation is 365 periods a year. Pass `periods_per_year` for other
sampling. Returns are simple per-period returns.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365.0


def align_returns(returns: pd.Series, benchmark: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Both series on their common timestamps, rows with a missing value in either dropped."""
    frame = pd.concat({"returns": returns, "benchmark": benchmark}, axis=1, join="inner").dropna()
    return frame["returns"], frame["benchmark"]


def benchmark_metrics(returns: pd.Series, benchmark: pd.Series, *, periods_per_year: float = DAYS_PER_YEAR, risk_free_rate: float = 0.0) -> dict[str, float]:
    """Beta, annualised alpha, correlation, tracking error, information ratio and up/down capture.

    Args:
        returns: The strategy's per-period simple returns.
        benchmark: The benchmark's per-period simple returns (any overlapping index).
        periods_per_year: Periods per year of the sampling (365 for daily crypto).
        risk_free_rate: Annual risk-free rate for alpha (0 by default: the book holds no cash yield).

    Returns:
        Empty values (NaN) when fewer than three common periods exist.
    """
    strategy, bench = align_returns(returns, benchmark)
    keys = ("periods", "beta", "alpha_ann", "correlation", "tracking_error_ann", "information_ratio", "up_capture", "down_capture",
            "return_ann", "benchmark_return_ann")
    if len(strategy) < 3:
        return dict.fromkeys(keys, float("nan")) | {"periods": float(len(strategy))}
    variance = float(bench.var())
    beta = float(strategy.cov(bench) / variance) if variance > 0 else float("nan")
    rf = risk_free_rate / periods_per_year
    alpha = (strategy.mean() - rf - beta * (bench.mean() - rf)) * periods_per_year if np.isfinite(beta) else float("nan")
    active = strategy - bench
    tracking = float(active.std() * np.sqrt(periods_per_year))
    up, down = bench > 0, bench < 0
    return {
        "periods": float(len(strategy)),
        "beta": beta,
        "alpha_ann": float(alpha),
        "correlation": float(strategy.corr(bench)),
        "tracking_error_ann": tracking,
        "information_ratio": float(active.mean() * periods_per_year / tracking) if tracking > 0 else float("nan"),
        "up_capture": float(strategy[up].mean() / bench[up].mean()) if up.any() else float("nan"),
        "down_capture": float(strategy[down].mean() / bench[down].mean()) if down.any() else float("nan"),
        "return_ann": float(strategy.mean() * periods_per_year),
        "benchmark_return_ann": float(bench.mean() * periods_per_year),
    }


def rolling_beta(returns: pd.Series, benchmark: pd.Series, *, window: int = 90) -> pd.Series:
    """Beta over a trailing window of `window` periods (NaN until the window is full)."""
    strategy, bench = align_returns(returns, benchmark)
    return (strategy.rolling(window, min_periods=window).cov(bench) / bench.rolling(window, min_periods=window).var()).rename("rolling_beta")


def benchmark_table(named_returns: dict[str, pd.Series], benchmark: pd.Series, **kwargs: Any) -> pd.DataFrame:
    """`benchmark_metrics` for several return series against one benchmark, one row each."""
    return pd.DataFrame({name: benchmark_metrics(series, benchmark, **kwargs) for name, series in named_returns.items()}).T
