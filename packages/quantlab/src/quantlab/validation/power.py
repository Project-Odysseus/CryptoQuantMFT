"""Can this test answer its question? Power analysis at pre-registration (plan 4.1).

Before a study runs, the pre-registration states the history it will use and the effect it hopes for. These
functions say whether that history can tell such an effect from zero under the strategy card's gate:

- `sharpe_error(sharpe, years)`: the standard error of an annual Sharpe estimated from `years` of returns (Lo, 2002;
  with skew and kurtosis per Mertens, 2002, and an inflation for autocorrelation).
- `pass_probability(sharpe, years)`: the chance the card's t-gate (t >= 2 by default) passes if the true Sharpe is
  `sharpe`. Trials raise the bar: with `trials` > 1 the gate is the deflated Sharpe's luck benchmark plus the gate.
- `detectable_sharpe(years)`: the smallest true Sharpe that passes with the stated power (80% by default).
- `years_needed(sharpe)`: the history needed to pass with that power.
- `detectable_ic(independent)`: the same for a signal's information coefficient.
- `power_report(...)`: all of it as one table, with a warning when the test cannot answer its question.

Example: 27 years of daily data and a hoped-for Sharpe of 0.3 pass the t >= 2 gate with probability about 0.35;
a Sharpe of 0.55 is needed for 80% power. H13 and H14 were such tests.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

from quantlab.validation.stats import expected_max_sharpe


def sharpe_error(sharpe: float, years: float, *, periods_per_year: float = 252.0, skew: float = 0.0, kurtosis: float = 3.0,
                 autocorrelation_inflation: float = 1.0) -> float:
    """Standard error of an annual Sharpe ratio from `years` of returns."""
    per_period = sharpe / math.sqrt(periods_per_year)
    count = max(years * periods_per_year, 2.0)
    variance = max(1e-12, 1.0 - skew * per_period + (kurtosis - 1.0) / 4.0 * per_period ** 2) / (count - 1.0)
    return math.sqrt(variance * periods_per_year) * max(autocorrelation_inflation, 1.0)


def luck_benchmark(trials: int, years: float, *, periods_per_year: float = 252.0) -> float:
    """The annual Sharpe the best of `trials` skill-less configurations would show (the deflated Sharpe's benchmark)."""
    count = max(years * periods_per_year, 2.0)
    return expected_max_sharpe(trials, 1.0 / count) * math.sqrt(periods_per_year)


def pass_probability(sharpe: float, years: float, *, gate_t: float = 2.0, trials: int = 1, periods_per_year: float = 252.0, **error: float) -> float:
    """Probability that the estimated Sharpe clears the gate if the true Sharpe is `sharpe`.

    The gate is `gate_t` standard errors above zero, or above the luck benchmark of `trials` configurations.
    """
    se = sharpe_error(sharpe, years, periods_per_year=periods_per_year, **error)
    bar = luck_benchmark(trials, years, periods_per_year=periods_per_year) if trials > 1 else 0.0
    return float(norm.sf(gate_t - (sharpe - bar) / se))


def detectable_sharpe(years: float, *, power: float = 0.8, gate_t: float = 2.0, trials: int = 1, periods_per_year: float = 252.0, **error: float) -> float:
    """The smallest true annual Sharpe that passes with probability `power`."""
    low, high = 0.0, 10.0
    for _ in range(60):
        middle = (low + high) / 2.0
        if pass_probability(middle, years, gate_t=gate_t, trials=trials, periods_per_year=periods_per_year, **error) >= power:
            high = middle
        else:
            low = middle
    return high


def years_needed(sharpe: float, *, power: float = 0.8, gate_t: float = 2.0, trials: int = 1, periods_per_year: float = 252.0, **error: float) -> float:
    """Years of history needed for a true Sharpe of `sharpe` to pass with probability `power` (inf if never)."""
    if sharpe <= 0:
        return float("inf")
    low, high = 0.1, 500.0
    if pass_probability(sharpe, high, gate_t=gate_t, trials=trials, periods_per_year=periods_per_year, **error) < power:
        return float("inf")
    for _ in range(60):
        middle = (low + high) / 2.0
        if pass_probability(sharpe, middle, gate_t=gate_t, trials=trials, periods_per_year=periods_per_year, **error) >= power:
            high = middle
        else:
            low = middle
    return high


def detectable_ic(independent: float, *, power: float = 0.8, level: float = 0.95) -> float:
    """The smallest true IC whose interval (two-sided `level`) excludes zero with probability `power`."""
    return float((norm.ppf(1.0 - (1.0 - level) / 2.0) + norm.ppf(power)) / math.sqrt(max(independent - 3.0, 1.0)))


def power_report(years: float, hypothesised_sharpe: float, *, trials: int = 1, power: float = 0.8, gate_t: float = 2.0,
                 periods_per_year: float = 252.0, **error: float) -> dict[str, object]:
    """The numbers a pre-registration should state, and a warning when the test cannot answer its question."""
    probability = pass_probability(hypothesised_sharpe, years, gate_t=gate_t, trials=trials, periods_per_year=periods_per_year, **error)
    detectable = detectable_sharpe(years, power=power, gate_t=gate_t, trials=trials, periods_per_year=periods_per_year, **error)
    needed = years_needed(hypothesised_sharpe, power=power, gate_t=gate_t, trials=trials, periods_per_year=periods_per_year, **error)
    report: dict[str, object] = {
        "years": years, "hypothesised Sharpe": hypothesised_sharpe, "trials": trials,
        "standard error of the Sharpe": sharpe_error(hypothesised_sharpe, years, periods_per_year=periods_per_year, **error),
        "luck benchmark (trials)": luck_benchmark(trials, years, periods_per_year=periods_per_year) if trials > 1 else 0.0,
        "probability of passing": probability, f"detectable Sharpe ({power:.0%} power)": detectable, f"years needed ({power:.0%} power)": needed,
    }
    if probability < 0.5:
        report["warning"] = (f"underpowered: a true Sharpe of {hypothesised_sharpe:g} passes only {probability:.0%} of the time with {years:g} years; "
                             f"a failure will not show the idea is wrong. Detectable: {detectable:.2f}. Use more markets, more history, or a stronger effect.")
    return report


def simulated_pass_rate(sharpe: float, years: float, *, runs: int = 2000, gate_t: float = 2.0, periods_per_year: float = 252.0, seed: int = 0) -> float:
    """Monte-Carlo check of `pass_probability` with normal returns (used by the tests; handy for odd designs)."""
    rng = np.random.default_rng(seed)
    count = int(years * periods_per_year)
    daily = sharpe / math.sqrt(periods_per_year) * 0.01
    samples = rng.normal(daily, 0.01, size=(runs, count))
    t = samples.mean(axis=1) / (samples.std(axis=1, ddof=1) / math.sqrt(count))
    return float(np.mean(t >= gate_t))
