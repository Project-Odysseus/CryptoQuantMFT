"""A Gaussian hidden Markov model with diagonal covariances: Baum-Welch fitting and causal (filtered) probabilities.

Written here rather than taken from a library, to keep the lab's dependencies small and to make sure only the forward
(filtered) probabilities are ever used for decisions: P(state at t | data up to t). The smoothed probabilities a
library returns by default use the future and must never drive a position.

    model = fit_hmm(train, n_states=2, seed=0)
    probs = filter_probabilities(model, data)      # rows = bars, columns = states; row t uses rows <= t only
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

VARIANCE_FLOOR = 1e-8


@dataclass(frozen=True)
class HMMParameters:
    """Initial distribution, transition matrix (rows sum to 1), and per-state means and variances (states x features)."""

    start: np.ndarray
    transition: np.ndarray
    means: np.ndarray
    variances: np.ndarray

    @property
    def n_states(self) -> int:
        return len(self.start)


def _log_emission(data: np.ndarray, params: HMMParameters) -> np.ndarray:
    """log N(x_t | state) for every bar and state (bars x states)."""
    diff = data[:, None, :] - params.means[None, :, :]
    variances = params.variances[None, :, :]
    return -0.5 * np.sum(np.log(2.0 * np.pi * variances) + diff ** 2 / variances, axis=2)


def _forward(log_b: np.ndarray, params: HMMParameters) -> tuple[np.ndarray, np.ndarray]:
    """Scaled forward pass: normalised alphas (bars x states) and each bar's log scale (summing to the log likelihood)."""
    count, states = log_b.shape
    alpha = np.zeros((count, states))
    scales = np.zeros(count)
    shift = log_b.max(axis=1, keepdims=True)
    b = np.exp(log_b - shift)
    previous = params.start
    for t in range(count):
        current = (previous @ params.transition if t else params.start) * b[t]
        total = current.sum()
        if not np.isfinite(total) or total <= 0:
            current, total = np.full(states, 1.0 / states), 1.0
        alpha[t] = current / total
        scales[t] = np.log(total) + shift[t, 0]
        previous = alpha[t]
    return alpha, scales


def fit_hmm(data: np.ndarray, *, n_states: int = 2, iterations: int = 200, tolerance: float = 1e-6, seed: int = 0) -> HMMParameters:
    """Maximum-likelihood parameters by Baum-Welch (EM), starting from a quantile split of the first feature."""
    data = np.asarray(data, dtype=float)
    data = data[np.isfinite(data).all(axis=1)]
    count, features = data.shape
    if count < 10 * n_states:
        raise ValueError("too little data to fit the model")
    rng = np.random.default_rng(seed)
    order = np.argsort(data[:, 0] ** 2 if features else data[:, 0])  # initialise states by the size of the first feature
    groups = np.array_split(order, n_states)
    means = np.array([data[group].mean(axis=0) for group in groups]) + rng.normal(0, 1e-6, (n_states, features))
    variances = np.array([np.maximum(data[group].var(axis=0), VARIANCE_FLOOR) for group in groups])
    transition = np.full((n_states, n_states), 0.05 / max(n_states - 1, 1))
    np.fill_diagonal(transition, 0.95)
    params = HMMParameters(np.full(n_states, 1.0 / n_states), transition, means, variances)
    previous_likelihood = -np.inf
    for _ in range(iterations):
        log_b = _log_emission(data, params)
        alpha, scales = _forward(log_b, params)
        b = np.exp(log_b - log_b.max(axis=1, keepdims=True))
        beta = np.ones((count, n_states))
        for t in range(count - 2, -1, -1):
            nxt = params.transition @ (b[t + 1] * beta[t + 1])
            beta[t] = nxt / max(nxt.sum(), 1e-300)
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True)
        xi_sum = np.zeros((n_states, n_states))
        for t in range(count - 1):
            xi = alpha[t][:, None] * params.transition * (b[t + 1] * beta[t + 1])[None, :]
            xi_sum += xi / max(xi.sum(), 1e-300)
        weights = gamma.sum(axis=0)
        means = (gamma.T @ data) / weights[:, None]
        variances = np.maximum((gamma.T @ data ** 2) / weights[:, None] - means ** 2, VARIANCE_FLOOR)
        transition = xi_sum / np.maximum(xi_sum.sum(axis=1, keepdims=True), 1e-300)
        params = HMMParameters(gamma[0] / gamma[0].sum(), transition, means, variances)
        likelihood = float(scales.sum())
        if abs(likelihood - previous_likelihood) < tolerance * max(1.0, abs(likelihood)):
            break
        previous_likelihood = likelihood
    return params


def filter_probabilities(params: HMMParameters, data: np.ndarray) -> np.ndarray:
    """P(state at t | data up to t) for every bar (rows with missing data repeat the previous row's prediction)."""
    data = np.asarray(data, dtype=float)
    out = np.full((len(data), params.n_states), np.nan)
    previous: np.ndarray | None = None
    for t, row in enumerate(data):
        prior = params.start if previous is None else previous @ params.transition
        if not np.isfinite(row).all():
            out[t] = prior if previous is not None else np.nan
            previous = prior if previous is not None else None
            continue
        log_likelihood = _log_emission(row[None, :], params)[0]
        likelihood = np.exp(log_likelihood - log_likelihood.max())
        posterior = prior * likelihood
        posterior = posterior / posterior.sum() if posterior.sum() > 0 else np.full(params.n_states, 1.0 / params.n_states)
        out[t] = posterior
        previous = posterior
    return out
