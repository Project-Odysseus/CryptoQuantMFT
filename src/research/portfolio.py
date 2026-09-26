"""Multi-asset portfolio backtests: a target weight per coin per day, with fees, slippage, funding and delistings.

The single-symbol tools trade one coin with 100% of equity. Cross-sectional
strategies (long the coins ranked best, short the ones ranked worst) and any
portfolio of several signals need a book of many positions instead. This
module simulates one on daily closes:

1. Weights are decided at each day's close from data up to that close, as a
   signed share of equity per coin (0.1 = long 10% of equity, -0.1 = short).
2. Over the next day, each position earns its coin's return and pays (or
   receives) that coin's funding. Longs pay positive funding and shorts
   receive it.
3. At the next close, positions are traded back to the new target weights.
   The fee plus slippage is charged on the notional traded. Between
   rebalances (`rebalance_every` > 1) positions drift with prices and cost
   nothing.
4. A coin whose price stops (delisted) is closed at its last price, and a coin
   without a price can't be bought.

`rank_weights` and `liquid_universe` build the usual long/short ranking
portfolios. `PortfolioResult.metrics` summarises a run, including the long and
short legs separately and the cost and funding drag.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, slots=True)
class PortfolioCosts:
    """Trading costs, charged on traded notional.

    Attributes:
        fee_pct: Fee per side in % of notional (0.05 = Kraken Futures' entry taker tier).
        slippage_bps: Per side, either one number or a frame (date x symbol),
            e.g. from `slippage_by_liquidity`.
        charge_funding: Apply each coin's funding to held positions.
    """

    fee_pct: float = 0.05
    slippage_bps: float | pd.DataFrame = 5.0
    charge_funding: bool = True


@dataclass(slots=True)
class PortfolioResult:
    """Daily series from `simulate_portfolio`, all as fractions of the previous day's equity unless noted."""

    equity: pd.Series
    returns: pd.Series
    long_pnl: pd.Series
    short_pnl: pd.Series
    costs: pd.Series
    funding: pd.Series
    turnover: pd.Series
    gross_exposure: pd.Series
    net_exposure: pd.Series
    positions: pd.Series

    def metrics(self, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None) -> dict[str, float]:
        """CAGR, volatility, Sharpe, max drawdown, turnover and drags over [start, end)."""
        window = slice(None)
        if start is not None or end is not None:
            index = self.returns.index
            mask = np.ones(len(index), dtype=bool)
            if start is not None:
                mask &= index >= pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else index >= pd.Timestamp(start)
            if end is not None:
                mask &= index < pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else index < pd.Timestamp(end)
            window = mask
        returns = self.returns[window]
        if len(returns) < 2:
            return {}
        equity = (1.0 + returns).cumprod()
        years = len(returns) / DAYS_PER_YEAR
        std = float(returns.std())
        return {
            "cagr": float(equity.iloc[-1] ** (1 / years) - 1) if equity.iloc[-1] > 0 else -1.0,
            "vol": std * np.sqrt(DAYS_PER_YEAR),
            "sharpe": float(returns.mean() / std * np.sqrt(DAYS_PER_YEAR)) if std > 0 else 0.0,
            "max_drawdown": float((1 - equity / equity.cummax()).max()),
            "long_leg_pct_per_year": float(self.long_pnl[window].mean() * DAYS_PER_YEAR * 100),
            "short_leg_pct_per_year": float(self.short_pnl[window].mean() * DAYS_PER_YEAR * 100),
            "turnover_per_year": float(self.turnover[window].mean() * DAYS_PER_YEAR),
            "cost_pct_per_year": float(self.costs[window].mean() * DAYS_PER_YEAR * 100),
            "funding_pct_per_year": float(self.funding[window].mean() * DAYS_PER_YEAR * 100),
            "avg_gross_exposure": float(self.gross_exposure[window].mean()),
            "avg_net_exposure": float(self.net_exposure[window].mean()),
            "avg_positions": float(self.positions[window].mean()),
        }


def simulate_portfolio(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    funding: pd.DataFrame | None = None,
    costs: PortfolioCosts | None = None,
    rebalance_every: int = 1,
    initial_equity: float = 1.0,
) -> PortfolioResult:
    """Trade `weights` (date x symbol, decided at each close) on `prices` (daily closes, NaN when not trading).

    `funding` is the funding paid per unit of long notional over each day
    (date x symbol, e.g. the daily sums from `binance_archive.parse_funding`).
    """
    costs = costs or PortfolioCosts()
    prices = prices.sort_index()
    symbols, dates = prices.columns, prices.index
    close = prices.to_numpy(dtype=float)
    target = weights.reindex(index=dates, columns=symbols).fillna(0.0).to_numpy(dtype=float, copy=True)
    listed = np.isfinite(close)
    target[~listed] = 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        day_return = np.zeros_like(close)
        day_return[1:] = close[1:] / close[:-1] - 1.0
    day_return[~np.isfinite(day_return)] = 0.0  # no price today: closed at yesterday's (the last) price
    fund = np.zeros_like(close)
    if funding is not None and costs.charge_funding:
        fund = np.nan_to_num(funding.reindex(index=dates, columns=symbols).to_numpy(dtype=float))
    if isinstance(costs.slippage_bps, pd.DataFrame):
        slippage = costs.slippage_bps.reindex(index=dates, columns=symbols).ffill().fillna(costs.slippage_bps.max().max()).to_numpy(dtype=float) / 10_000.0
    else:
        slippage = np.full_like(close, float(costs.slippage_bps) / 10_000.0)
    fee = costs.fee_pct / 100.0

    count = len(dates)
    series = {name: np.zeros(count) for name in ("equity", "returns", "long", "short", "costs", "funding", "turnover", "gross", "net", "positions")}
    equity = initial_equity
    holdings = np.zeros(len(symbols))  # notional per coin after the last rebalance
    for day in range(count):
        start_equity = equity
        long_pnl = float(np.sum(np.where(holdings > 0, holdings, 0.0) * day_return[day]))
        short_pnl = float(np.sum(np.where(holdings < 0, holdings, 0.0) * day_return[day]))
        funding_paid = float(np.sum(holdings * fund[day]))
        equity = equity + long_pnl + short_pnl - funding_paid
        drifted = holdings * (1.0 + day_return[day])
        delisted = (holdings != 0.0) & ~listed[day]
        if day % rebalance_every == 0:
            # Targets are shares of the equity left after paying for the trade, and the cost depends on the
            # trade: a few fixed-point passes converge (each shrinks the error by the cost rate, ~0.1%).
            after_cost = max(equity, 0.0)
            for _ in range(4):
                new = target[day] * after_cost
                cost = float(np.sum(np.abs(new - drifted) * (fee + slippage[day])))
                after_cost = max(equity - cost, 0.0)
        else:
            new = np.where(delisted, 0.0, drifted)
        traded = np.abs(new - drifted)
        cost = float(np.sum(traded * (fee + slippage[day])))
        equity -= cost
        holdings = new
        base = start_equity if start_equity > 0 else 1.0
        series["equity"][day] = equity
        series["returns"][day] = (equity - start_equity) / base if day > 0 else (equity - initial_equity) / initial_equity
        series["long"][day] = long_pnl / base
        series["short"][day] = short_pnl / base
        series["costs"][day] = cost / base
        series["funding"][day] = funding_paid / base
        series["turnover"][day] = float(np.sum(traded)) / base
        series["gross"][day] = float(np.sum(np.abs(holdings))) / equity if equity > 0 else 0.0
        series["net"][day] = float(np.sum(holdings)) / equity if equity > 0 else 0.0
        series["positions"][day] = float(np.count_nonzero(holdings))
        if equity <= 0.0:  # ruined: stay flat and at zero
            holdings = np.zeros(len(symbols))
            equity = 0.0
    as_series = {name: pd.Series(values, index=dates) for name, values in series.items()}
    return PortfolioResult(
        equity=as_series["equity"], returns=as_series["returns"], long_pnl=as_series["long"], short_pnl=as_series["short"],
        costs=as_series["costs"], funding=as_series["funding"], turnover=as_series["turnover"],
        gross_exposure=as_series["gross"], net_exposure=as_series["net"], positions=as_series["positions"],
    )


def liquid_universe(quote_volume: pd.DataFrame, *, top_n: int = 50, lookback_days: int = 30, min_history_days: int = 60) -> pd.DataFrame:
    """Each day, the `top_n` coins by average quote volume over the last `lookback_days`, listed at least `min_history_days`.

    Chosen from data up to each close only, so the universe itself doesn't
    look ahead (a coin that later collapses is still in it before the collapse).
    """
    average = quote_volume.rolling(lookback_days, min_periods=lookback_days).mean()
    age = quote_volume.notna().cumsum()
    eligible = (age >= min_history_days) & average.notna() & (average > 0)
    rank = average.where(eligible).rank(axis=1, ascending=False, method="first")
    return rank <= top_n


def slippage_by_liquidity(quote_volume: pd.DataFrame, *, lookback_days: int = 30, tiers: tuple[tuple[float, float], ...] = ((1e9, 3.0), (2e8, 8.0), (5e7, 15.0))) -> pd.DataFrame:
    """Slippage in bps per coin and day from its average daily quote volume: the thinner the market, the more it costs.

    `tiers` maps a minimum average volume (USD) to bps, best first; below the
    last tier it is 25 bps. The defaults are deliberately conservative for
    executing on Kraken, whose altcoin books are thinner than Binance's.
    """
    average = quote_volume.rolling(lookback_days, min_periods=1).mean()
    out = pd.DataFrame(25.0, index=quote_volume.index, columns=quote_volume.columns)
    for minimum, bps in reversed(tiers):
        out = out.mask(average >= minimum, bps)
    return out


def rank_weights(
    scores: pd.DataFrame,
    eligible: pd.DataFrame,
    *,
    quantile: float = 0.2,
    gross: float = 1.0,
    long_only: bool = False,
    min_names: int = 10,
    risk: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Long the top `quantile` of eligible coins by score and short the bottom `quantile`.

    Each leg is equal weight, or inverse-`risk` weight (e.g. each coin's
    recent volatility) if `risk` is given. The legs are dollar-neutral, with
    `gross` total exposure split half long and half short (all long when
    `long_only`). Days with fewer than `min_names` eligible coins stay flat.
    """
    ranked = scores.where(eligible & scores.notna())
    names = ranked.notna().sum(axis=1)
    pct = ranked.rank(axis=1, pct=True, method="first")
    long_mask = pct > 1.0 - quantile
    short_mask = pct <= quantile
    base = pd.DataFrame(1.0, index=scores.index, columns=scores.columns)
    if risk is not None:
        base = 1.0 / risk.reindex_like(scores).where(lambda frame: frame > 0)
    long_raw = base.where(long_mask, 0.0).fillna(0.0)
    short_raw = base.where(short_mask, 0.0).fillna(0.0)
    long_leg = gross if long_only else gross / 2.0
    weights = long_raw.div(long_raw.sum(axis=1).replace(0.0, np.nan), axis=0).fillna(0.0) * long_leg
    if not long_only:
        weights -= short_raw.div(short_raw.sum(axis=1).replace(0.0, np.nan), axis=0).fillna(0.0) * (gross / 2.0)
    weights[names < min_names] = 0.0
    return weights


def cross_sectional_ic(scores: pd.DataFrame, forward: pd.DataFrame, eligible: pd.DataFrame, *, min_names: int = 10) -> pd.Series:
    """Per day, the rank correlation between the scores and the forward returns across eligible coins."""
    ranked_scores = scores.where(eligible & scores.notna() & forward.notna()).rank(axis=1)
    ranked_forward = forward.where(ranked_scores.notna()).rank(axis=1)
    names = ranked_scores.notna().sum(axis=1)
    score_centered = ranked_scores.sub(ranked_scores.mean(axis=1), axis=0)
    forward_centered = ranked_forward.sub(ranked_forward.mean(axis=1), axis=0)
    covariance = (score_centered * forward_centered).sum(axis=1)
    scale = np.sqrt((score_centered**2).sum(axis=1) * (forward_centered**2).sum(axis=1))
    return (covariance / scale).where(names >= min_names)
