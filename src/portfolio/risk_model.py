"""The book's risk model: how the instruments move together, and what a set of weights is exposed to because of it.

Gross and net exposure add weights up as if every instrument were its own bet. In crypto they are mostly one:
1.0x BTC plus 0.5x ETH is 1.5x gross but moves like ~1.4x of BTC. This module measures that, so the risk overlay
(`risk.py`) and the reports can work in risk terms:

- `RiskModel` keeps an exponentially weighted covariance of the instruments' returns, one grid bar at a time, from
  past bars only. Returns are taken as zero-mean, like the EWMA sizing forecasts. An instrument can be missing on
  a bar (not listed yet, a failed fetch): each pair is estimated from the bars both have.
- Correlations are shrunk towards one prior value (the constant-correlation target of Ledoit and Wolf, 2004). A
  pair without enough shared history gets the prior, and an instrument without enough history the median variance
  of the others, so a new listing is never treated as riskless.
- The **stressed** covariance lifts every correlation to at least `stress_correlation`: crypto correlations go to
  1 in a crash, so limits sized on the average correlation are too loose exactly when they matter. For a hedged
  (long/short) book a higher correlation means *less* measured risk, so `stressed_volatility` is the larger of the
  normal and the stressed figure.

`RiskEstimate` is one frozen snapshot of the model with the questions the overlay asks: the book's volatility,
its beta to a benchmark instrument, each position's share of the risk, the diversification ratio.

`ReturnWindow` keeps the recent returns of each allocation unit (a sleeve, or a basket as a whole) for the
correlation between strategies: the average correlation and the effective number of independent bets.

Everything is pure and steps bar by bar with a JSON checkpoint, so research replays exactly what the runtime does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

VAR_95_Z = 1.6449  # one-sided 95% quantile of a normal
ES_95_Z = 2.0627  # mean of a normal beyond that quantile


def effective_bets(correlation: np.ndarray | pd.DataFrame) -> float:
    """(sum of eigenvalues)^2 / sum of squares: 1 when everything moves together, N when all are independent."""
    matrix = np.asarray(correlation, dtype=float)
    if matrix.size == 0:
        return 0.0
    eigenvalues = np.clip(np.linalg.eigvalsh(matrix), 0.0, None)
    squares = float((eigenvalues**2).sum())
    return float(eigenvalues.sum() ** 2 / squares) if squares > 0 else 0.0


def average_correlation(correlation: np.ndarray | pd.DataFrame) -> float:
    """The mean of the off-diagonal correlations (NaN for fewer than two series)."""
    matrix = np.asarray(correlation, dtype=float)
    if len(matrix) < 2:
        return float("nan")
    return float(matrix[np.triu_indices(len(matrix), k=1)].mean())


def _nearest_correlation(correlation: np.ndarray) -> np.ndarray:
    """A valid correlation matrix close to `correlation`: negative eigenvalues clipped, the diagonal restored to 1.

    Pairs estimated from different bars need not form a consistent matrix, and an inconsistent one can report a
    negative variance for some book.
    """
    values, vectors = np.linalg.eigh((correlation + correlation.T) / 2.0)
    if values.min() >= 0.0:
        return correlation
    repaired = (vectors * np.clip(values, 1e-10, None)) @ vectors.T
    scale = np.sqrt(np.diag(repaired))
    repaired = repaired / np.outer(scale, scale)
    np.fill_diagonal(repaired, 1.0)
    return repaired


@dataclass(frozen=True, slots=True)
class RiskEstimate:
    """The risk model at one moment. Covariances are per grid bar; volatilities are returned annualised.

    Attributes:
        instruments: The order of the matrices' rows and columns.
        covariance: Per-bar covariance of instrument returns.
        stressed: The same with correlations floored at the stress level, or None when no stress is configured.
        periods_per_year: Grid bars per year.
    """

    instruments: tuple[str, ...]
    covariance: np.ndarray
    stressed: np.ndarray | None
    periods_per_year: float

    def vector(self, weights: Mapping[str, float]) -> np.ndarray:
        """`weights` in the model's order (instruments the model doesn't know are left out)."""
        return np.array([float(weights.get(instrument, 0.0)) for instrument in self.instruments])

    def volatility(self, weights: Mapping[str, float]) -> float:
        """Annualised volatility of the book as a share of equity, at the estimated correlations."""
        w = self.vector(weights)
        return float(np.sqrt(max(w @ self.covariance @ w, 0.0) * self.periods_per_year))

    def stressed_volatility(self, weights: Mapping[str, float]) -> float:
        """The larger of the normal volatility and the one at crash correlations (see the module docstring)."""
        normal = self.volatility(weights)
        if self.stressed is None:
            return normal
        w = self.vector(weights)
        return max(normal, float(np.sqrt(max(w @ self.stressed @ w, 0.0) * self.periods_per_year)))

    def instrument_volatility(self) -> dict[str, float]:
        """Annualised volatility per instrument."""
        return {instrument: float(np.sqrt(self.covariance[i, i] * self.periods_per_year)) for i, instrument in enumerate(self.instruments)}

    def correlation(self) -> pd.DataFrame:
        """The estimated correlation matrix."""
        std = np.sqrt(np.diag(self.covariance))
        return pd.DataFrame(self.covariance / np.outer(std, std), index=list(self.instruments), columns=list(self.instruments))

    def betas(self, benchmark: str) -> dict[str, float]:
        """Each instrument's beta to `benchmark` (an instrument of the model): its move per 1% move of the benchmark."""
        if benchmark not in self.instruments:
            raise ValueError(f"benchmark {benchmark!r} is not in the risk model ({list(self.instruments)})")
        column = self.instruments.index(benchmark)
        variance = self.covariance[column, column]
        return {instrument: float(self.covariance[i, column] / variance) for i, instrument in enumerate(self.instruments)}

    def beta_exposure(self, weights: Mapping[str, float], benchmark: str) -> float:
        """The book's net exposure in benchmark terms: sum of weight x beta. 0.5 = moves like 50% of equity in the benchmark."""
        betas = self.betas(benchmark)
        return float(sum(float(weights.get(instrument, 0.0)) * beta for instrument, beta in betas.items()))

    def risk_contributions(self, weights: Mapping[str, float]) -> dict[str, float]:
        """Each instrument's share of the book's variance, w_i (Cov w)_i / w'Cov w; the shares sum to 1 (a hedge is negative)."""
        w = self.vector(weights)
        marginal = self.covariance @ w
        variance = float(w @ marginal)
        if variance <= 0.0:
            return {instrument: 0.0 for instrument in self.instruments}
        return {instrument: float(w[i] * marginal[i] / variance) for i, instrument in enumerate(self.instruments)}

    def marginal_risk(self, weights: Mapping[str, float]) -> dict[str, float]:
        """(Cov w)_i / w'Cov w per instrument: multiply by any weight on it (a sleeve's) for that weight's share of the risk."""
        w = self.vector(weights)
        marginal = self.covariance @ w
        variance = float(w @ marginal)
        return {instrument: float(marginal[i] / variance) if variance > 0 else 0.0 for i, instrument in enumerate(self.instruments)}

    def diversification_ratio(self, weights: Mapping[str, float]) -> float:
        """Sum of |weight| x volatility over the book's volatility: 1 for one bet, higher the more the positions offset."""
        w = self.vector(weights)
        volatility = float(np.sqrt(max(w @ self.covariance @ w, 0.0)))
        standalone = float(np.abs(w) @ np.sqrt(np.diag(self.covariance)))
        return standalone / volatility if volatility > 0 else float("nan")


class RiskModel:
    """An exponentially weighted covariance of instrument returns, stepped one grid bar at a time.

    Call `step` once per grid bar with each instrument's return over that bar, then `estimate()` for the snapshot
    to decide with. The state round-trips through `to_dict` / `from_dict` for checkpoints.
    """

    def __init__(self, instruments: Sequence[str], *, halflife_bars: float, min_bars: int, periods_per_year: float,
                 shrinkage: float = 0.2, prior_correlation: float = 0.5, stress_correlation: float | None = 0.9) -> None:
        """Validate the settings now, so a bad config fails at startup.

        Args:
            instruments: Instrument ids, in the order of the matrices.
            halflife_bars: Bars after which an observation counts half.
            min_bars: Shared bars a pair (or an instrument, for its variance) needs before its own estimate is used.
            periods_per_year: Grid bars per year, for annualising.
            shrinkage: Share of the prior in every correlation (0 = the sample correlation, 1 = the prior).
            prior_correlation: The correlation assumed without evidence, and the shrinkage target.
            stress_correlation: The floor on correlations in the stressed covariance; None for no stress.
        """
        if halflife_bars <= 0 or min_bars < 2 or periods_per_year <= 0:
            raise ValueError("halflife_bars and periods_per_year must be above 0 and min_bars at least 2")
        if not 0.0 <= shrinkage <= 1.0 or not -1.0 < prior_correlation < 1.0:
            raise ValueError("shrinkage must be between 0 and 1 and prior_correlation between -1 and 1")
        if stress_correlation is not None and not 0.0 <= stress_correlation < 1.0:
            raise ValueError("stress_correlation must be at least 0 and below 1")
        self.instruments = tuple(instruments)
        self.halflife_bars = float(halflife_bars)
        self.min_bars = int(min_bars)
        self.periods_per_year = float(periods_per_year)
        self.shrinkage = float(shrinkage)
        self.prior_correlation = float(prior_correlation)
        self.stress_correlation = stress_correlation
        count = len(self.instruments)
        self._decay = 0.5 ** (1.0 / self.halflife_bars)
        self._products = np.zeros((count, count))  # decayed sums of r_i r_j over bars where both exist
        self._weights = np.zeros((count, count))  # the decayed number of such bars
        self._counts = np.zeros((count, count), dtype=int)  # the plain number of such bars
        self.bars_seen = 0

    def step(self, returns: Mapping[str, float]) -> None:
        """Add one grid bar's returns (instrument id to simple return; missing or NaN = no observation)."""
        row = np.array([float(value) if (value := returns.get(instrument)) is not None else np.nan for instrument in self.instruments])
        seen = np.isfinite(row)
        both = np.outer(seen, seen)
        clean = np.where(seen, row, 0.0)
        self._products = self._decay * self._products + np.outer(clean, clean)
        self._weights = self._decay * self._weights + both
        self._counts = self._counts + both
        self.bars_seen += 1

    def estimate(self) -> RiskEstimate | None:
        """The current snapshot, or None while no instrument has `min_bars` of history."""
        count = len(self.instruments)
        measured = np.diag(self._counts) >= self.min_bars
        with np.errstate(divide="ignore", invalid="ignore"):
            variance = np.where(measured, np.diag(self._products) / np.diag(self._weights), np.nan)
        known = variance[np.isfinite(variance) & (variance > 0)]
        if not len(known):
            return None
        variance = np.where(np.isfinite(variance) & (variance > 0), variance, float(np.median(known)))
        std = np.sqrt(variance)
        with np.errstate(divide="ignore", invalid="ignore"):
            sample = (self._products / self._weights) / np.outer(std, std)
        usable = (self._counts >= self.min_bars) & np.outer(measured, measured) & np.isfinite(sample)
        correlation = np.where(usable, np.clip(sample, -1.0, 1.0), self.prior_correlation)
        correlation = (1.0 - self.shrinkage) * correlation + self.shrinkage * self.prior_correlation
        np.fill_diagonal(correlation, 1.0)
        correlation = _nearest_correlation(correlation) if count > 1 else correlation
        stressed = None
        if self.stress_correlation is not None:
            lifted = np.maximum(correlation, self.stress_correlation)
            np.fill_diagonal(lifted, 1.0)
            stressed = (_nearest_correlation(lifted) if count > 1 else lifted) * np.outer(std, std)
        return RiskEstimate(self.instruments, correlation * np.outer(std, std), stressed, self.periods_per_year)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready state."""
        return {"instruments": list(self.instruments), "halflife_bars": self.halflife_bars, "min_bars": self.min_bars,
                "periods_per_year": self.periods_per_year, "shrinkage": self.shrinkage, "prior_correlation": self.prior_correlation,
                "stress_correlation": self.stress_correlation, "bars_seen": self.bars_seen,
                "products": self._products.tolist(), "weights": self._weights.tolist(), "counts": self._counts.tolist()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RiskModel":
        """Rebuild from `to_dict` output."""
        model = cls(payload["instruments"], halflife_bars=payload["halflife_bars"], min_bars=payload["min_bars"],
                    periods_per_year=payload["periods_per_year"], shrinkage=payload["shrinkage"],
                    prior_correlation=payload["prior_correlation"], stress_correlation=payload["stress_correlation"])
        count = len(model.instruments)
        model._products = np.asarray(payload["products"], dtype=float).reshape(count, count)
        model._weights = np.asarray(payload["weights"], dtype=float).reshape(count, count)
        model._counts = np.asarray(payload["counts"], dtype=int).reshape(count, count)
        model.bars_seen = int(payload["bars_seen"])
        return model


class ReturnWindow:
    """The last `length` bars of returns per allocation unit (a sleeve, or a basket as a whole), for their correlations.

    A unit's return over a bar is the weight it held into the bar times its instrument's return (summed over a
    basket's members), so a flat unit has a return of 0 and a unit flat for the whole window has no correlation.
    """

    def __init__(self, names: Sequence[str], *, length: int, min_bars: int = 30) -> None:
        """`min_bars`: rows needed before any correlation is reported."""
        if length < 2 or min_bars < 2:
            raise ValueError("length and min_bars must be at least 2")
        self.names = tuple(names)
        self.length = int(length)
        self.min_bars = int(min_bars)
        self.rows: list[list[float]] = []

    def add(self, returns: Mapping[str, float]) -> None:
        """Add one bar (a missing or NaN return counts as 0: the unit earned nothing)."""
        self.rows.append([float(value) if (value := returns.get(name)) is not None and np.isfinite(value) else 0.0 for name in self.names])
        del self.rows[: -self.length]

    def correlation(self) -> pd.DataFrame | None:
        """Correlations between the units that moved in the window; None before `min_bars` or with fewer than two."""
        if len(self.rows) < self.min_bars:
            return None
        block = np.asarray(self.rows, dtype=float)
        active = [index for index in range(len(self.names)) if block[:, index].std() > 0]
        if len(active) < 2:
            return None
        names = [self.names[index] for index in active]
        return pd.DataFrame(np.corrcoef(block[:, active], rowvar=False), index=names, columns=names)

    def summary(self) -> dict[str, Any] | None:
        """Average correlation, the effective number of bets, the most correlated pair and the matrix itself; None when unmeasurable."""
        correlation = self.correlation()
        if correlation is None:
            return None
        matrix = correlation.to_numpy()
        upper = np.triu_indices(len(matrix), k=1)
        top = int(np.argmax(matrix[upper]))
        return {"units": len(matrix), "bars": len(self.rows), "average_correlation": average_correlation(matrix),
                "effective_bets": effective_bets(matrix),
                "most_correlated": [correlation.index[upper[0][top]], correlation.index[upper[1][top]], float(matrix[upper][top])],
                "names": list(correlation.index), "matrix": [[round(float(value), 4) for value in row] for row in matrix]}  # for the dashboard's heatmap

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready state."""
        return {"names": list(self.names), "length": self.length, "min_bars": self.min_bars, "rows": self.rows}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReturnWindow":
        """Rebuild from `to_dict` output."""
        window = cls(payload["names"], length=payload["length"], min_bars=payload["min_bars"])
        window.rows = [[float(value) for value in row] for row in payload["rows"]]
        return window
