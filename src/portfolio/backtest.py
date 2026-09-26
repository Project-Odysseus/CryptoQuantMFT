"""Research backtest of a whole portfolio config: sleeves, allocation, netting, risk overlay, then a simulated book.

It chains the same functions the runtime will use (`run_sleeve` replays
`SleeveRunner.step`, `allocate_history` applies `sleeve_scales`,
`net_history` sums per instrument, `array_overlay` wraps
`apply_portfolio_risk`), so a research result describes what the paper
runtime would do on the same bars.

Every sleeve is turned into weights on its own bar interval, then placed on
one time grid: the shortest interval any enabled sleeve uses. Bars are stamped
at their open, so a sleeve bar stamped `t` decides at `t + interval`. On the
grid that is the bar stamped `t + interval - grid`. A daily decision on a 4h
grid lands on the day's 20:00 bar (which closes at midnight) and holds until
the next one.

`prepare_inputs` loads bars and runs every sleeve once. `run_book` then
allocates, nets and simulates, cheaply, so several allocation methods and
each sleeve alone can be compared on the same inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import pandas as pd

from src.portfolio.allocation import allocate_history
from src.portfolio.config import InstrumentSpec, PortfolioConfig
from src.portfolio.netting import net_history
from src.portfolio.risk import array_overlay
from src.portfolio.sleeves import SleeveSpec, run_sleeve
from src.research.portfolio import PortfolioCosts, PortfolioResult, simulate_portfolio
from src.runtime.config import BAR_INTERVALS

# (instrument, interval) -> bars, oldest first, stamped at their open.
BarLoader = Callable[[InstrumentSpec, str], Sequence[Any]]


def default_bar_loader(instrument: InstrumentSpec, interval: str) -> list[Any]:
    """Cached Kraken history: `load_bars(..., source="perp")` for perps, Kraken spot otherwise.

    Nothing is downloaded when the cache already covers the range. Kraken
    spot only serves its last 720 candles, so spot sleeves have far less
    history than perps.
    """
    from src.research.engine import load_bars

    if instrument.venue not in ("kraken", "kraken_futures"):
        raise ValueError(f"no history loader for venue {instrument.venue!r} ({instrument.id}); pass bar_loader")
    return load_bars(instrument.symbol, interval, source="perp" if instrument.kind == "perp" else "spot")


@dataclass(slots=True)
class PortfolioInputs:
    """Everything `run_book` needs, computed once per config.

    Attributes:
        grid_interval: The interval of the time grid, e.g. "4h".
        prices: Grid x instrument closes (gaps inside an instrument's life
            filled forward; NaN before it lists or after it stops).
        sleeve_weights: Grid x sleeve: each sleeve's own target weight, as if it
            had the whole portfolio.
        sleeve_instrument: Sleeve id to instrument id.
        measure_start: The first grid bar at which every sleeve has had its
            `warmup_bars`; books start trading here.
    """

    grid_interval: str
    prices: pd.DataFrame
    sleeve_weights: pd.DataFrame
    sleeve_instrument: dict[str, str]
    measure_start: pd.Timestamp

    @property
    def bars_per_day(self) -> float:
        """Grid bars per day (6 for 4h)."""
        return 86400 / BAR_INTERVALS[self.grid_interval]


@dataclass(slots=True)
class PortfolioBacktest:
    """One simulated book: the weights at each stage and the simulated result."""

    allocation: str
    risk_overlay: bool
    sleeves: tuple[str, ...]
    allocated: pd.DataFrame  # grid x sleeve, after allocation
    targets: pd.DataFrame  # grid x instrument, after netting, before the risk overlay
    result: PortfolioResult


def _timestamps(bars: Sequence[Any]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime([bar.timestamp for bar in bars], utc=True))


def _closes(bars: Sequence[Any]) -> pd.Series:
    series = pd.Series([float(bar.close) for bar in bars], index=_timestamps(bars))
    return series[~series.index.duplicated(keep="last")].sort_index()


def prepare_inputs(config: PortfolioConfig, *, bar_loader: BarLoader | None = None, strategies: Mapping[str, Any] | None = None) -> PortfolioInputs:
    """Load bars for every enabled sleeve and its instrument, run each sleeve over its history, and align them on one grid.

    `strategies` maps a sleeve id to a ready-made StrategyFn that replaces
    the registry lookup for that sleeve (a strategy still in a notebook).
    """
    loader = bar_loader or default_bar_loader
    strategies = dict(strategies or {})
    sleeves = config.enabled_sleeves
    if not sleeves:
        raise ValueError("the config has no enabled sleeves")
    grid_interval = min((sleeve.interval for sleeve in sleeves), key=lambda interval: BAR_INTERVALS[interval])
    grid_step = pd.Timedelta(seconds=BAR_INTERVALS[grid_interval])

    cache: dict[tuple[str, str], Sequence[Any]] = {}

    def bars_for(instrument_id: str, interval: str) -> Sequence[Any]:
        key = (instrument_id, interval)
        if key not in cache:
            cache[key] = list(loader(config.instruments[instrument_id], interval))
            if len(cache[key]) < 2:
                raise ValueError(f"not enough {interval} history for {instrument_id}")
        return cache[key]

    instruments = list(dict.fromkeys(sleeve.instrument for sleeve in sleeves))
    raw = pd.DataFrame({instrument: _closes(bars_for(instrument, grid_interval)) for instrument in instruments}).sort_index()
    prices = raw.ffill().where(raw.bfill().notna())  # fill missing candles, but keep an instrument's end as its end
    grid = prices.index

    weights: dict[str, pd.Series] = {}
    starts: list[pd.Timestamp] = []
    for sleeve in sleeves:
        bars = bars_for(sleeve.instrument, sleeve.interval)
        shift = pd.Timedelta(seconds=BAR_INTERVALS[sleeve.interval]) - grid_step
        decided = _timestamps(bars) + shift
        series = pd.Series(run_sleeve(sleeve, bars, strategy=strategies.get(sleeve.id)).weights, index=decided)
        series = series[~series.index.duplicated(keep="last")]
        weights[sleeve.id] = series.reindex(grid.union(series.index)).ffill().reindex(grid).fillna(0.0)
        starts.append(decided[min(sleeve.warmup_bars, len(decided) - 1)])
    measure_start = max(starts)
    first = grid.searchsorted(measure_start)
    if first >= len(grid) - 1:
        raise ValueError(f"no history left after the sleeves' warmup (it ends {measure_start:%Y-%m-%d})")
    return PortfolioInputs(
        grid_interval=grid_interval,
        prices=prices.iloc[first:],
        sleeve_weights=pd.DataFrame(weights).iloc[first:],
        sleeve_instrument={sleeve.id: sleeve.instrument for sleeve in sleeves},
        measure_start=grid[first],
    )


def run_book(
    config: PortfolioConfig,
    inputs: PortfolioInputs,
    *,
    allocation: str | None = None,
    sleeves: Sequence[str] | None = None,
    risk_overlay: bool = True,
    funding_pct_per_day: float = 0.01,
) -> PortfolioBacktest:
    """Allocate, net and simulate one book from prepared inputs.

    Args:
        allocation: Overrides the config's method. Under `fixed`, budgets
            summing above 1 are scaled down to sum to 1, so a config written
            for `equal` can still be compared under `fixed`.
        sleeves: Only these sleeves. One sleeve under `equal` is that sleeve
            alone at full size.
        risk_overlay: Apply the config's `[risk]` limits.
        funding_pct_per_day: Funding longs pay on perps, in % of notional per
            day (0.01 is about the measured one-year mean on Kraken BTC/ETH;
            see `CostSettings.perp`).
    """
    method = allocation or config.allocation
    chosen = list(sleeves) if sleeves is not None else list(inputs.sleeve_weights.columns)
    budgets = {sleeve_id: budget for sleeve_id, budget in config.budgets().items() if sleeve_id in chosen}
    if method == "fixed" and sum(budgets.values()) > 1.0:
        total = sum(budgets.values())
        budgets = {sleeve_id: budget / total for sleeve_id, budget in budgets.items()}
    per_day = inputs.bars_per_day
    instrument_returns = pd.DataFrame({sleeve_id: inputs.prices[inputs.sleeve_instrument[sleeve_id]].pct_change() for sleeve_id in budgets})
    allocated = allocate_history(
        inputs.sleeve_weights[list(budgets)], budgets, method,
        instrument_returns=instrument_returns,
        lookback=max(2, round(config.allocation_lookback_days * per_day)),
        refit_every=max(1, round(config.allocation_refit_days * per_day)),
    )
    allocated = allocated * config.scale
    targets = net_history(allocated, inputs.sleeve_instrument).reindex(columns=inputs.prices.columns, fill_value=0.0)

    instruments = list(inputs.prices.columns)
    specs = [config.instruments[instrument] for instrument in instruments]
    index = inputs.prices.index
    costs = PortfolioCosts(
        fee_pct={spec.id: spec.taker_fee_pct for spec in specs},
        slippage_bps=pd.DataFrame({spec.id: spec.slippage_bps for spec in specs}, index=index),
    )
    per_bar = funding_pct_per_day / 100.0 / per_day
    funding = pd.DataFrame({spec.id: per_bar if spec.kind == "perp" else 0.0 for spec in specs}, index=index)
    overlay = array_overlay(instruments, config=config.risk, venues=config.venues(), can_short=config.can_short()) if risk_overlay else None
    # Simulated in money at the config's starting equity, so money limits (max_gross_notional) bind as in the runtime
    result = simulate_portfolio(inputs.prices, targets, funding=funding, costs=costs, rebalance_band=config.rebalance_band, adjust_targets=overlay,
                                initial_equity=config.initial_equity)
    return PortfolioBacktest(allocation=method, risk_overlay=risk_overlay, sleeves=tuple(budgets), allocated=allocated, targets=targets, result=result)


PERIOD_METRICS = ("sharpe", "cagr", "vol", "max_drawdown", "turnover_per_year", "cost_pct_per_year", "funding_pct_per_year", "avg_gross_exposure", "avg_net_exposure")


def period_metrics(book: PortfolioBacktest, holdout: pd.Timestamp, *, label: str | None = None) -> list[dict[str, Any]]:
    """One row per period ("is" before `holdout`, "ho" from it) with the book's main metrics."""
    rows = []
    for period, start, end in (("is", None, holdout), ("ho", holdout, None)):
        metrics = book.result.metrics(start, end)
        if metrics:
            rows.append({"book": label or book.allocation, "period": period, **{key: metrics[key] for key in PERIOD_METRICS}})
    return rows


def daily_returns(book: PortfolioBacktest) -> pd.Series:
    """The book's daily returns (for correlations between sleeves on different intervals)."""
    return book.result.equity.resample("1D").last().pct_change().dropna()


def candidate_report(
    config: PortfolioConfig,
    candidate: SleeveSpec,
    *,
    strategy: Any | None = None,
    holdout: pd.Timestamp | str = "2024-10-01",
    bar_loader: BarLoader | None = None,
    funding_pct_per_day: float = 0.01,
) -> dict[str, pd.DataFrame]:
    """Would `candidate` improve the book? The book with and without it, and how it correlates with each sleeve.

    A new signal is worth adding when the book gets better, not only when the
    signal looks good alone. A sleeve that repeats what the book already holds
    adds risk without adding return. Both books run on the same inputs (the
    candidate's warmup can move the start date) with the config's allocation
    and risk limits.

    Returns:
        {"books": metrics per period for "without" and "with", "alone": the
        candidate at full size, "correlation": its daily-return correlation
        with every existing sleeve}.
    """
    if candidate.instrument not in config.instruments:
        raise ValueError(f"{candidate.instrument} is not in the config's [instruments]; add an InstrumentSpec for it first")
    if any(sleeve.id == candidate.id for sleeve in config.sleeves):
        raise ValueError(f"the config already has a sleeve '{candidate.id}'")
    holdout = pd.Timestamp(holdout, tz="UTC") if pd.Timestamp(holdout).tzinfo is None else pd.Timestamp(holdout)
    extended = replace(config, sleeves=(*config.sleeves, candidate))
    inputs = prepare_inputs(extended, bar_loader=bar_loader, strategies={candidate.id: strategy} if strategy is not None else None)
    existing = [sleeve.id for sleeve in config.enabled_sleeves]
    without = run_book(extended, inputs, sleeves=existing, funding_pct_per_day=funding_pct_per_day)
    with_candidate = run_book(extended, inputs, funding_pct_per_day=funding_pct_per_day)
    alone = run_book(extended, inputs, allocation="equal", sleeves=[candidate.id], risk_overlay=False, funding_pct_per_day=funding_pct_per_day)
    candidate_returns = daily_returns(alone)
    correlation = {
        sleeve_id: candidate_returns.corr(daily_returns(run_book(extended, inputs, allocation="equal", sleeves=[sleeve_id], risk_overlay=False, funding_pct_per_day=funding_pct_per_day)))
        for sleeve_id in existing
    }
    return {
        "books": pd.DataFrame(period_metrics(without, holdout, label="without") + period_metrics(with_candidate, holdout, label=f"with {candidate.id}")),
        "alone": pd.DataFrame(period_metrics(alone, holdout, label=f"{candidate.id} alone")),
        "correlation": pd.Series(correlation, name=candidate.id).to_frame(),
    }
