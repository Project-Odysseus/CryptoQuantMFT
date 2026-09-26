"""Combine many weak features into one return forecast, fitted walk-forward with no look-ahead.

Single intraday features predict only a few basis points, less than a round
trip costs. The usual answer is to combine them and trade only when the
combined forecast is larger than the cost. `walk_forward_ridge` does the
combining honestly: at each refit point it trains only on rows whose target
had fully played out before that point (a purge of `horizon` bars), then
forecasts the following block. Every forecast is out-of-sample.

Ridge (linear, shrunk towards zero) is the deliberate first model: with
signals this weak, anything more flexible mostly fits noise. Swap in another
model once the features are strong enough to justify it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def walk_forward_ridge(
    features: pd.DataFrame | np.ndarray,
    target: np.ndarray,
    *,
    horizon: int,
    train_min: int,
    refit_every: int,
    ridge: float = 0.01,
    window: int | None = None,
) -> np.ndarray:
    """Out-of-sample ridge forecasts of `target` for every row (NaN before `train_min`).

    Args:
        features: One row per bar, known at that bar's close.
        target: What to predict, e.g. the forward return over `horizon` bars.
        horizon: Bars the target looks ahead. Training rows within `horizon`
            of a refit point are dropped, because their targets were not yet
            known then.
        train_min: Bars before the first forecast.
        refit_every: Bars between refits; forecasts in between use the last fit.
        ridge: Shrinkage per row, applied to standardized features.
        window: Train on only the last `window` rows (None = all history).

    Features are standardized with the training rows' mean and std at each
    fit. There is no intercept: the average return of the training period is
    not treated as a signal.
    """
    values = np.asarray(features, dtype=float)
    target = np.asarray(target, dtype=float)
    rows = len(values)
    forecast = np.full(rows, np.nan)
    for start in range(train_min, rows, refit_every):
        train_end = start - horizon
        train_start = 0 if window is None else max(0, train_end - window)
        if train_end - train_start < 2 * values.shape[1]:
            continue
        x, y = values[train_start:train_end], target[train_start:train_end]
        usable = np.isfinite(x).all(axis=1) & np.isfinite(y)
        x, y = x[usable], y[usable]
        if len(x) < 2 * values.shape[1]:
            continue
        mean, std = x.mean(axis=0), x.std(axis=0)
        std[std == 0.0] = 1.0
        z = (x - mean) / std
        beta = np.linalg.solve(z.T @ z + ridge * len(z) * np.eye(z.shape[1]), z.T @ (y - y.mean()))
        block = slice(start, min(start + refit_every, rows))
        forecast[block] = ((values[block] - mean) / std) @ beta
    return forecast


def threshold_positions(forecast: np.ndarray, threshold: float, *, allow_short: bool = True) -> np.ndarray:
    """Target positions from a forecast: enter when it exceeds `threshold`, hold until it changes sign.

    The gap between entering at `threshold` and exiting at zero stops the
    position flipping every bar around the threshold, which would pay the
    round trip for nothing.
    """
    from src.backtest.strategies import latch_series

    values = np.nan_to_num(np.asarray(forecast, dtype=float))
    positions = latch_series(values > threshold, values < 0.0, values < -threshold if allow_short else None, values > 0.0 if allow_short else None)
    return np.asarray(positions)
