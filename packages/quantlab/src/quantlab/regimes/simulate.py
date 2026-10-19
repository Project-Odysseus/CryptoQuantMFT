"""Regime-switching price paths with known ground truth (plan 3.3).

A regime detector should first show it can find regimes that exist. These paths have three kinds of episodes, each
lasting a random time drawn around its mean, and the truth label of every bar:

- TREND: log returns with a drift (up or down, chosen per episode) and positive autocorrelation;
- RANGE: log price mean-reverting (Ornstein-Uhlenbeck) to the level where the episode began;
- SHOCK: zero drift, volatility several times higher (labelled SHOCK; scored as its own state or ignored).

`break_at` adds a structural break: a permanent change of volatility part-way through the path.

Bars get an open and an intrabar range from the close path, so range-based features (ATR, ADX) work.

    paths = simulate_paths(RegimeProcess(), years=20, seeds=range(20))
    bars, truth = paths[0]
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RegimeProcess:
    """Parameters of the simulated market (daily units)."""

    mean_days: dict[str, float] = field(default_factory=lambda: {"TREND": 90.0, "RANGE": 90.0, "SHOCK": 15.0})
    switch_to: dict[str, dict[str, float]] = field(default_factory=lambda: {"TREND": {"RANGE": 0.8, "SHOCK": 0.2}, "RANGE": {"TREND": 0.8, "SHOCK": 0.2},
                                                                            "SHOCK": {"TREND": 0.5, "RANGE": 0.5}})
    volatility: float = 0.012
    trend_drift: float = 0.0012  # per day, sign chosen per episode
    trend_autocorrelation: float = 0.10
    range_reversion: float = 0.06  # Ornstein-Uhlenbeck speed per day
    shock_multiplier: float = 3.0
    noise: float = 1.0  # multiplies every shock: a noisier market makes regimes harder to see
    min_days: int = 10
    break_at: float | None = None  # a structural break: from this fraction of the path on, volatility is multiplied by
    break_volatility: float = 2.0  # `break_volatility` (a detector calibrated on the past must adapt)


def simulate(process: RegimeProcess, *, days: int, seed: int) -> tuple[pd.DataFrame, pd.Series]:
    """One path: OHLCV bars on business days and the true state of each bar."""
    rng = np.random.default_rng(seed)
    states, log_price = [], [np.log(100.0)]
    state = "RANGE"
    sigma = process.volatility * process.noise
    while len(states) < days:
        length = max(process.min_days, int(rng.exponential(process.mean_days[state])))
        anchor, direction, previous = log_price[-1], rng.choice([-1.0, 1.0]), 0.0
        for _ in range(min(length, days - len(states))):
            if process.break_at is not None and len(states) == int(process.break_at * days):
                sigma *= process.break_volatility
            if state == "TREND":
                step = direction * process.trend_drift + process.trend_autocorrelation * previous + rng.normal(0, sigma)
            elif state == "RANGE":
                step = process.range_reversion * (anchor - log_price[-1]) + rng.normal(0, sigma)
            else:
                step = rng.normal(0, sigma * process.shock_multiplier)
            log_price.append(log_price[-1] + step)
            states.append(state)
            previous = step
        options = process.switch_to[state]
        state = rng.choice(list(options), p=np.array(list(options.values())) / sum(options.values()))
    index = pd.bdate_range("2000-01-03", periods=days)
    close = np.exp(np.array(log_price[1:days + 1]))
    base = process.volatility * process.noise
    open_ = np.exp(np.array(log_price[:days]) + rng.normal(0, base * 0.2, days))
    spread = np.abs(rng.normal(0, base * 0.6, days))
    bars = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * np.exp(spread), "low": np.minimum(open_, close) * np.exp(-spread),
                         "close": close, "volume": 1.0}, index=index)
    bars.index.name = "time"
    return bars, pd.Series(states, index=index, name="truth")


def simulate_paths(process: RegimeProcess, *, years: int = 20, seeds: range | list[int] = range(10)) -> list[tuple[pd.DataFrame, pd.Series]]:
    """Several independent paths of `years` business years each."""
    return [simulate(process, days=years * 252, seed=seed) for seed in seeds]
