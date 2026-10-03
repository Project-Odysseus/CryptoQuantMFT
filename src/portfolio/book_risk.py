"""What the book is exposed to right now, in one summary, and the risk model wired to a portfolio config.

`exposure_summary` answers the questions gross and net alone can't: how much is long and short, how much sits in
each group, venue and coin, what the book amounts to in benchmark (BTC) terms, how volatile it is at the estimated
and at crash correlations, what a bad day costs, and which instruments and sleeves carry the risk. The runtime
stores it with every snapshot (the dashboard reads it from SQLite) and the research backtest prints it, both from
the same function.

`build_risk_model` builds the `RiskModel` a config asks for, and `estimate_stream` replays it over a research
backtest's grid one bar at a time, exactly as the runtime steps it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from src.portfolio.config import PortfolioConfig
from src.portfolio.risk_model import ES_95_Z, VAR_95_Z, RiskEstimate, RiskModel, average_correlation, effective_bets
from src.runtime.config import BAR_INTERVALS

DAYS_PER_YEAR = 365.0


def build_risk_model(config: PortfolioConfig, grid_interval: str) -> RiskModel:
    """The config's risk model on `grid_interval` bars, over the instruments its enabled sleeves trade."""
    per_day = 86400 / BAR_INTERVALS[grid_interval]
    risk = config.risk
    return RiskModel(config.traded_instruments(), halflife_bars=risk.vol_halflife_days * per_day, min_bars=max(2, round(risk.vol_min_days * per_day)),
                     periods_per_year=DAYS_PER_YEAR * per_day, shrinkage=risk.correlation_shrinkage, prior_correlation=risk.prior_correlation,
                     stress_correlation=risk.stress_correlation if risk.stress_correlation > 0 else None)


def default_benchmark(config: PortfolioConfig) -> str | None:
    """`[risk] benchmark`, or the book's BTC instrument (a perp before a spot pair); None when the book trades no BTC."""
    if config.risk.benchmark:
        return config.risk.benchmark
    bitcoin = [instrument for instrument in config.traded_instruments() if config.instruments[instrument].underlying == "BTC"]
    return next((instrument for instrument in bitcoin if config.instruments[instrument].kind == "perp"), bitcoin[0] if bitcoin else None)


def estimate_stream(config: PortfolioConfig, grid_interval: str, prices: pd.DataFrame, *, warmup_prices: pd.DataFrame | None = None) -> Callable[[int], RiskEstimate | None]:
    """`estimates(index)`: the risk model's snapshot after grid bar `index` of `prices`, for `risk.array_overlay`.

    The model is first stepped over `warmup_prices` (the bars before the backtest starts), then lazily over
    `prices` as later bars are asked for, so every snapshot uses bars up to its own only. Bars must be asked for
    in order, as `simulate_portfolio` does.
    """
    model = build_risk_model(config, grid_interval)
    names = [name for name in model.instruments if name in prices.columns]
    history = prices[names] if warmup_prices is None or warmup_prices.empty else pd.concat([warmup_prices.reindex(columns=names), prices[names]])
    returns = history.pct_change(fill_method=None).to_numpy(dtype=float)
    offset = len(history) - len(prices)
    for row in returns[:offset]:
        model.step(dict(zip(names, row)))
    state: dict[str, Any] = {"next": 0, "estimate": model.estimate()}

    def estimates(index: int) -> RiskEstimate | None:
        if index < state["next"] - 1:
            raise ValueError("risk estimates must be read in bar order")
        while state["next"] <= index:
            model.step(dict(zip(names, returns[offset + state["next"]])))
            state["next"] += 1
            state["estimate"] = None
        if state["estimate"] is None:
            state["estimate"] = model.estimate()
        return state["estimate"]

    return estimates


def exposure_summary(weights: Mapping[str, float], *, config: PortfolioConfig, estimate: RiskEstimate | None = None,
                     sleeve_weights: Mapping[str, tuple[str, float]] | None = None, units: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The book's exposure and risk for `weights` (instrument id to a signed share of equity).

    Args:
        estimate: The risk model's snapshot; without it the risk figures are None and the sums remain.
        sleeve_weights: Sleeve id to (instrument, allocated weight), for each sleeve's share of the book's risk.
        units: Sleeve id to its allocation unit; a basket's members are then reported as one line.

    Returns:
        A JSON-ready dict: `gross`, `net`, `long`, `short`; `groups`, `venues` and `underlyings` (each name to its
        gross and net); and, with an estimate, `beta_exposure` to `benchmark`, `volatility` and
        `stressed_volatility` (annualised), `var_95_1d` and `es_95_1d` (a normal day's loss at the stressed
        volatility, as shares of equity), `diversification_ratio`, `average_correlation` and `effective_bets` of the
        instruments held, and `risk_share` per instrument and per sleeve (shares of the variance, summing to 1).
    """
    held = {instrument: float(weight) for instrument, weight in weights.items() if weight}
    venues, groups, underlyings = config.venues(), config.groups(), config.underlyings()

    def bucket(mapping: Mapping[str, str], *, default: str | None = None) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for instrument, weight in held.items():
            name = mapping.get(instrument, default)
            if name is None:
                continue
            row = out.setdefault(name, {"gross": 0.0, "net": 0.0})
            row["gross"] += abs(weight)
            row["net"] += weight
        return dict(sorted(out.items()))

    summary: dict[str, Any] = {
        "gross": sum(abs(weight) for weight in held.values()), "net": sum(held.values()),
        "long": sum(weight for weight in held.values() if weight > 0), "short": -sum(weight for weight in held.values() if weight < 0),
        "positions": len(held),
        "groups": bucket(groups, default="ungrouped" if groups else None), "venues": bucket(venues), "underlyings": bucket(underlyings),
        "benchmark": default_benchmark(config), "beta_exposure": None, "volatility": None, "stressed_volatility": None,
        "var_95_1d": None, "es_95_1d": None, "diversification_ratio": None, "average_correlation": None, "effective_bets": None,
        "risk_share": {}, "sleeve_risk_share": {},
    }
    if estimate is None:
        return summary
    summary["volatility"] = estimate.volatility(held)
    summary["stressed_volatility"] = estimate.stressed_volatility(held)
    daily = summary["stressed_volatility"] / np.sqrt(DAYS_PER_YEAR)
    summary["var_95_1d"], summary["es_95_1d"] = VAR_95_Z * daily, ES_95_Z * daily
    if summary["benchmark"] in estimate.instruments:
        summary["beta_exposure"] = estimate.beta_exposure(held, summary["benchmark"])
    modelled = [instrument for instrument in held if instrument in estimate.instruments]
    if modelled:
        ratio = estimate.diversification_ratio(held)
        summary["diversification_ratio"] = ratio if np.isfinite(ratio) else None
        summary["risk_share"] = {instrument: share for instrument, share in estimate.risk_contributions(held).items() if instrument in held}
        if sleeve_weights:
            marginal = estimate.marginal_risk(held)
            shares: dict[str, float] = {}
            for sleeve, (instrument, weight) in sleeve_weights.items():
                if weight:
                    unit = (units or {}).get(sleeve, sleeve)
                    shares[unit] = shares.get(unit, 0.0) + float(weight) * marginal.get(instrument, 0.0)
            summary["sleeve_risk_share"] = shares
    if len(modelled) > 1:
        correlation = estimate.correlation().loc[modelled, modelled]
        # a short position turns a positive correlation into an offsetting one: sign each pair by the positions held
        signs = np.sign([held[instrument] for instrument in modelled])
        signed = correlation.to_numpy() * np.outer(signs, signs)
        summary["average_correlation"] = average_correlation(signed)
        summary["effective_bets"] = effective_bets(correlation.to_numpy())
    return summary


def exposure_history(config: PortfolioConfig, grid_interval: str, prices: pd.DataFrame, weights: pd.DataFrame, *,
                     warmup_prices: pd.DataFrame | None = None, every: int = 1) -> pd.DataFrame:
    """The exposure summary's main numbers at every `every`-th grid bar of a backtest (`weights`: the positions held).

    Columns: gross, net, long, short, beta_exposure, volatility, stressed_volatility, effective_bets, and
    `<group> gross` / `<group> net` per instrument group. Each row uses the risk model as of its own bar.
    """
    estimates = estimate_stream(config, grid_interval, prices, warmup_prices=warmup_prices)
    names = list(weights.columns)
    values = weights.to_numpy(dtype=float)
    rows, stamps = [], []
    for index in range(0, len(weights), max(1, every)):
        summary = exposure_summary(dict(zip(names, values[index])), config=config, estimate=estimates(index))
        row = {key: summary[key] for key in ("gross", "net", "long", "short", "beta_exposure", "volatility", "stressed_volatility", "effective_bets")}
        for group, figures in summary["groups"].items():
            row[f"{group} gross"], row[f"{group} net"] = figures["gross"], figures["net"]
        rows.append(row)
        stamps.append(weights.index[index])
    return pd.DataFrame(rows, index=pd.DatetimeIndex(stamps)).astype(float)


def format_exposure(summary: Mapping[str, Any], *, limits: Mapping[str, Any] | None = None) -> list[str]:
    """Readable lines for a summary (the dashboard and the research script print the same ones)."""
    limits = dict(limits or {})
    lines = [f"Long {summary['long']:.2f}x, short {summary['short']:.2f}x: gross {summary['gross']:.2f}x, net {summary['net']:+.2f}x in {summary['positions']} positions"]
    group_limits = limits.get("groups") or {}
    for label, key in (("Groups", "groups"), ("Venues", "venues"), ("Coins", "underlyings")):
        rows = summary.get(key) or {}
        if not rows or (key == "underlyings" and len(rows) > 12):
            continue
        parts = []
        for name, row in rows.items():
            cap = group_limits.get(name, {}) if key == "groups" else {}
            parts.append(f"{name} gross {row['gross']:.2f}x" + (f" of {cap['max_gross']:g}x" if "max_gross" in cap else "")
                         + f" net {row['net']:+.2f}x" + (f" of {cap['max_net']:g}x" if "max_net" in cap else ""))
        lines.append(f"{label}: " + "; ".join(parts))
    if summary.get("volatility") is None:
        lines.append("Risk model: not enough history yet")
        return lines
    beta = summary.get("beta_exposure")
    if beta is not None:
        cap = limits.get("max_beta_exposure")
        lines.append(f"In {summary['benchmark']} terms: {beta:+.2f}x" + (f" of {cap:g}x" if cap else "") + " (sum of weight x beta)")
    cap = limits.get("max_portfolio_vol")
    lines.append(f"Volatility {summary['volatility']:.0%} a year, {summary['stressed_volatility']:.0%} at crash correlations" + (f" (cap {cap:.0%})" if cap else "")
                 + f"; a 1-in-20 day loses {summary['var_95_1d']:.1%}, and {summary['es_95_1d']:.1%} on average beyond that")
    if summary.get("effective_bets") is not None:
        lines.append(f"Diversification: ratio {summary['diversification_ratio']:.2f}, {summary['effective_bets']:.1f} effective bets among the instruments held, "
                     f"average correlation as positioned {summary['average_correlation']:+.2f}")
    for label, key in (("Risk by instrument", "risk_share"), ("Risk by sleeve", "sleeve_risk_share")):
        shares = sorted((summary.get(key) or {}).items(), key=lambda item: -abs(item[1]))
        if shares:
            lines.append(f"{label}: " + ", ".join(f"{name} {share:+.0%}" for name, share in shares[:8]) + (f" (+{len(shares) - 8} more)" if len(shares) > 8 else ""))
    return lines
