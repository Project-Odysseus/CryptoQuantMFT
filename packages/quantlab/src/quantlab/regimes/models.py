"""Regime models: one instrument's bars -> per-bar state probabilities (plan 3.2).

Every model follows the RegimeModel protocol (quantlab.engine.components): `states`, a declared decision `lag`
(bars between a real change and the model being able to say so), and `__call__(bars)` returning a frame of state
probabilities whose rows sum to 1 (NaN during warm-up). Labels are the most probable state (`labels`). Calibration
(percentiles, model fits) uses only data before each calendar year and is frozen within it, so appending data never
changes past probabilities.

- `FeatureBlendRegime`: H14's five-feature blend with confirmation and hysteresis (RANGE / TRANSITION / TREND).
- `PercentileRegime`: one feature's walk-forward percentile, two states (soft or hard, with optional confirmation).
- `VolatilityRegime`: CALM / NORMAL / STRESSED by short volatility's percentile.
- `HMMRegime`: a Gaussian hidden Markov model on chosen features, refitted each year on the past, filtered
  (causal) probabilities only; states named by their fitted volatility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from quantlab.features import regime as rf
from quantlab.regimes.hmm import filter_probabilities, fit_hmm


def labels(probabilities: pd.DataFrame) -> pd.Series:
    """The most probable state per bar (NaN where probabilities are missing)."""
    complete = probabilities.notna().all(axis=1)
    out = pd.Series(np.nan, index=probabilities.index, dtype=object)
    out[complete] = probabilities[complete].idxmax(axis=1)
    return out


def one_hot(states: pd.Series, names: tuple[str, ...]) -> pd.DataFrame:
    """Probability 1 on the given state (NaN rows stay NaN)."""
    frame = pd.DataFrame({name: (states == name).astype(float) for name in names}, index=states.index)
    return frame.where(states.notna())


# --- H14's feature blend -----------------------------------------------------------------------------------------------

@dataclass
class FeatureBlendRegime:
    """H14's regime machine as a regime model (states RANGE, TRANSITION, TREND; probability 1 on the committed state).

    `soft=True` instead gives the trend probability itself as P(TREND) and 1 - P(TREND) as P(RANGE), no states.
    """

    config: object = None  # quantlab.strategies.regime_overlay.OverlayConfig
    variant: str = "blend"
    soft: bool = False
    name: str = "feature_blend"
    states: tuple[str, ...] = ("RANGE", "TRANSITION", "TREND")
    lag: int = 3

    def __call__(self, bars: pd.DataFrame) -> pd.DataFrame:
        from quantlab.strategies.regime_overlay import OverlayConfig, classify, regime_scores

        config = self.config or OverlayConfig()
        scores = regime_scores(bars, config)
        if self.soft:
            p = scores["trend_probability"]
            return pd.DataFrame({"RANGE": 1.0 - p, "TREND": p}, index=bars.index)
        state = classify(scores, config, variant=self.variant)["state"]
        state = state.where(scores["trend_probability"].notna())
        return one_hot(state, self.states)


# --- one feature ---------------------------------------------------------------------------------------------------

FEATURES: dict[str, Callable[[pd.DataFrame, int], pd.Series]] = {
    "er": lambda bars, horizon: pd.Series(rf.efficiency_ratio(bars["close"].to_numpy(dtype=float), horizon), index=bars.index),
    "abs_slope_t": lambda bars, horizon: pd.Series(np.abs(rf.slope_tstat(bars["close"].to_numpy(dtype=float), horizon)), index=bars.index),
    "adx": lambda bars, horizon: pd.Series(rf.adx(bars["high"].to_numpy(dtype=float), bars["low"].to_numpy(dtype=float), bars["close"].to_numpy(dtype=float), 14), index=bars.index),
    "vr": lambda bars, horizon: pd.Series(rf.variance_ratio(np.log(bars["close"]).diff().to_numpy(), horizon, 5), index=bars.index),
    "short_vol": lambda bars, horizon: pd.Series(rf.realised_volatility(np.log(bars["close"]).diff().to_numpy(), 10), index=bars.index),
}


@dataclass
class PercentileRegime:
    """Two states from one feature's walk-forward percentile.

    soft: P(high state) = the percentile itself. Hard: the high state when the percentile is >= `high`, the low state
    when <= `low`, otherwise the previous state (hysteresis), entered only after `confirm` consecutive bars.
    """

    feature: str | Callable[[pd.DataFrame], pd.Series] = "er"
    horizon: int = 40
    low: float = 0.35
    high: float = 0.65
    confirm: int = 1
    soft: bool = False
    state_names: tuple[str, str] = ("RANGE", "TREND")  # (low, high)
    calibration_days: int = 500
    min_calibration: int = 250
    name: str = ""
    lag: int = 1

    @property
    def states(self) -> tuple[str, ...]:
        return self.state_names

    def score(self, bars: pd.DataFrame) -> pd.Series:
        raw = FEATURES[self.feature](bars, self.horizon) if isinstance(self.feature, str) else self.feature(bars)
        return rf.calendar_percentile(raw.replace([np.inf, -np.inf], np.nan), calibration_days=self.calibration_days, min_calibration=self.min_calibration)

    def __call__(self, bars: pd.DataFrame) -> pd.DataFrame:
        low_name, high_name = self.state_names
        p = self.score(bars)
        if self.soft:
            return pd.DataFrame({low_name: 1.0 - p, high_name: p}, index=bars.index)
        states = pd.Series(np.nan, index=bars.index, dtype=object)
        current, run_high, run_low = None, 0, 0
        for t, value in enumerate(p.to_numpy()):
            if not np.isfinite(value):
                continue
            run_high = run_high + 1 if value >= self.high else 0
            run_low = run_low + 1 if value <= self.low else 0
            if run_high >= self.confirm:
                current = high_name
            elif run_low >= self.confirm:
                current = low_name
            states.iloc[t] = current if current is not None else (high_name if value >= 0.5 else low_name)
        return one_hot(states, self.state_names)


@dataclass
class VolatilityRegime:
    """CALM / NORMAL / STRESSED by the walk-forward percentile of 10-day volatility (cuts at `cuts`)."""

    cuts: tuple[float, float] = (0.33, 0.67)
    window: int = 10
    name: str = "volatility"
    states: tuple[str, ...] = ("CALM", "NORMAL", "STRESSED")
    lag: int = 1

    def __call__(self, bars: pd.DataFrame) -> pd.DataFrame:
        vol = pd.Series(rf.realised_volatility(np.log(bars["close"]).diff().to_numpy(), self.window), index=bars.index)
        p = rf.calendar_percentile(vol, calibration_days=500, min_calibration=250)
        state = pd.Series(np.where(p <= self.cuts[0], "CALM", np.where(p >= self.cuts[1], "STRESSED", "NORMAL")), index=bars.index).where(p.notna())
        return one_hot(state, self.states)


# --- hidden Markov model -------------------------------------------------------------------------------------------

def default_hmm_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Daily log return and its 5-day sum: enough to separate calm drifting from volatile choppy states."""
    returns = np.log(bars["close"]).diff()
    return pd.DataFrame({"return": returns, "return_5d": returns.rolling(5).sum()}, index=bars.index)


@dataclass
class HMMRegime:
    """A Gaussian HMM refitted at each calendar year on the past `train_years`, with filtered probabilities only.

    States are named by fitted volatility of the first feature, lowest first (`state_names`), so a refit cannot swap
    their meaning.
    """

    n_states: int = 2
    features: Callable[[pd.DataFrame], pd.DataFrame] = default_hmm_features
    train_years: int = 8
    min_train: int = 750
    state_names: tuple[str, ...] = ("CALM", "VOLATILE")
    seed: int = 0
    name: str = "hmm"
    lag: int = 1
    fits: dict[int, object] = field(default_factory=dict, repr=False)

    @property
    def states(self) -> tuple[str, ...]:
        return self.state_names[: self.n_states]

    def __call__(self, bars: pd.DataFrame) -> pd.DataFrame:
        data = self.features(bars)
        out = pd.DataFrame(np.nan, index=bars.index, columns=list(self.states))
        for year in sorted(set(bars.index.year)):
            start = pd.Timestamp(year=year, month=1, day=1)
            train = data[(data.index < start) & (data.index >= start - pd.DateOffset(years=self.train_years))].dropna()
            if len(train) < self.min_train:
                continue
            params = fit_hmm(train.to_numpy(), n_states=self.n_states, seed=self.seed)
            order = np.argsort(params.variances[:, 0])  # name states by volatility, low to high
            self.fits[year] = params
            window = data[(data.index >= start - pd.DateOffset(years=1)) & (data.index.year <= year)]  # run the filter in from a year earlier
            probabilities = filter_probabilities(params, window.to_numpy())[:, order]
            frame = pd.DataFrame(probabilities, index=window.index, columns=list(self.states))
            out.loc[frame.index[frame.index.year == year]] = frame[frame.index.year == year].to_numpy()
        return out
